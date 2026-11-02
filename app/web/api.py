from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.clarifications.service import (
    answer_project_question,
    ask_project_question_follow_up,
    generate_project_clarification_questions,
    set_project_question_status,
)
from app.dependencies import flash, get_db
from app.github.service import check_for_updates
from app.investigations.service import (
    apply_investigation,
    confirm_investigation,
    create_investigation_session,
    delete_investigation_session,
    investigation_draft_state_payload,
    investigation_runtime_state,
    investigation_message_requires_recollect,
    load_investigation_session,
    post_investigation_message,
    queue_investigation_collect,
    reconcile_orphaned_investigation,
    request_investigation_stop,
)
from app.lmstudio.service import refresh_models, test_connection
from app.mcp.service import test_mcp_connection
from app.models import (
    GuestInventory,
    Host,
    HostRoot,
    InvestigationSession,
    MachineIdentityResult,
    MachineProfile,
    Project,
    ProjectQuestionThread,
    Scan,
    ScanEvent,
)
from app.publish_preview.service import build_investigation_working_preview
from app.profiles.service import (
    refresh_host_facts,
    refresh_machine_identity,
    refresh_project_mapping_preview,
    refresh_project_research,
)
from app.project_mapping.models import HostProfileOverridePayload
from app.project_mapping.service import persist_host_profile_override
from app.scanner.service import (
    queue_project_interpretation,
    queue_scan,
    reconcile_orphaned_scan,
    request_project_interpretation_stop,
    request_scan_stop,
)
from app.scanner.ssh import SSHExecutor
from app.schemas import (
    GENERAL_SETTINGS_KEY,
    GITHUB_SETTINGS_KEY,
    LMSTUDIO_SETTINGS_KEY,
    MCP_SETTINGS_KEY,
    PROFILE_SETTINGS_KEY,
    SCAN_SETTINGS_KEY,
    WEB_SEARCH_SETTINGS_KEY,
    GeneralSettingsPayload,
    GitHubSettingsPayload,
    HostUpsert,
    LMStudioSettingsPayload,
    MCPSettingsPayload,
    ProfileSettingsPayload,
    ProjectContextPayload,
    ScanSettingsPayload,
    WebSearchSettingsPayload,
)
from app.topology.service import (
    build_host_topology_context,
    link_discovered_guest_to_host,
    mark_discovered_guest_unmanaged,
    refresh_host_topology,
)
from app.updater.service import start_update_job
from app.utils.common import clean_multiline, truthy, utcnow
from app.utils.settings_store import get_settings, set_settings
from app.websearch.service import test_web_search_connection, test_web_search_search


router = APIRouter()


PROXMOX_ROOT_TEMPLATES = [
    "/etc/pve/nodes/{node_name}/qemu-server",
    "/etc/pve/nodes/{node_name}/lxc",
    "/var/lib/vz/snippets",
    "/opt",
    "/usr/local/bin",
    "/usr/local/sbin",
]


def _redirect(target: str) -> RedirectResponse:
    return RedirectResponse(url=target, status_code=303)


def _safe_return_target(target: str | None, *, default: str = "/web/projects") -> str:
    if target and target.startswith("/web/"):
        return target
    return default


def _nullable_toggle(value: str | None) -> bool | None:
    if value is None:
        return None
    return truthy(value)


def _flash_connection_result(request: Request, result, *, success_level: str = "success", warning_level: str = "warning") -> None:
    flash(request, success_level if result.success else warning_level, result.message)
    for root_result in result.root_results:
        if not root_result.exists:
            flash(request, warning_level, f"{root_result.root_path}: {root_result.message}")


def _default_scanner_key_path(session: Session, request: Request) -> str:
    settings = get_settings(session, request.app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    return settings.default_scanner_ssh_key_path


def _build_host_payload(
    *,
    name: str,
    address: str,
    ssh_port: int,
    ssh_user: str,
    ssh_key_path: str | None,
    enabled: str | None,
    roots_text: str,
    default_ssh_key_path: str | None = None,
    proxmox_node_name: str | None = None,
) -> HostUpsert:
    if proxmox_node_name and "<your-node>" in roots_text:
        roots_text = roots_text.replace("<your-node>", proxmox_node_name.strip())
    return HostUpsert(
        name=name,
        address=address,
        ssh_port=ssh_port,
        ssh_user=ssh_user,
        ssh_key_path=ssh_key_path or default_ssh_key_path,
        enabled=truthy(enabled),
        roots=clean_multiline(roots_text),
    )


def _save_host(session: Session, payload: HostUpsert, host: Host | None = None) -> Host:
    if host is None:
        host = Host()
        session.add(host)

    host.name = payload.name
    host.address = payload.address
    host.ssh_port = payload.ssh_port
    host.ssh_user = payload.ssh_user
    host.ssh_key_path = payload.ssh_key_path
    host.enabled = payload.enabled

    existing_by_path = {root.root_path: root for root in host.roots}
    desired_paths = set(payload.roots)

    for root in list(host.roots):
        if root.root_path not in desired_paths:
            host.roots.remove(root)
            session.delete(root)

    for root_path in payload.roots:
        existing_root = existing_by_path.get(root_path)
        if existing_root is not None:
            existing_root.enabled = True
            continue
        host.roots.append(HostRoot(root_path=root_path, enabled=True))

    session.commit()
    session.refresh(host)
    return host


def _build_proxmox_roots(node_name: str) -> list[str]:
    cleaned = node_name.strip()
    if not cleaned:
        raise ValueError("Proxmox node name is required.")
    if any(char in cleaned for char in "<> /"):
        raise ValueError("Use the real Proxmox node name, not a placeholder or path.")
    return [root.format(node_name=cleaned) for root in PROXMOX_ROOT_TEMPLATES]


def _bootstrap_host_access(
    request: Request,
    session: Session,
    *,
    host: Host,
    bootstrap_user: str,
    bootstrap_password: str | None,
    sudo_password: str | None,
) -> tuple[object, SSHExecutor]:
    executor = SSHExecutor(
        connect_timeout=request.app.state.config.ssh_connect_timeout_seconds,
        command_timeout=request.app.state.config.ssh_command_timeout_seconds,
    )
    result = executor.bootstrap_host_access(
        host,
        bootstrap_user=bootstrap_user,
        bootstrap_password=bootstrap_password or None,
        sudo_password=sudo_password or None,
        roots=[root.root_path for root in host.roots if root.enabled],
    )
    host.connection_status = "reachable" if result.success else "warning"
    if result.success:
        host.last_seen_at = utcnow()
    session.flush()
    return result, executor


def _investigation_redirect(investigation_id: int) -> RedirectResponse:
    return _redirect(f"/web/investigations/{investigation_id}")


@router.post("/api/hosts")
async def create_or_update_host(
    request: Request,
    session: Session = Depends(get_db),
    host_id: int | None = Form(default=None),
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(...),
    ssh_key_path: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    roots_text: str = Form(...),
    proxmox_node_name: str | None = Form(default=None),
):
    try:
        default_key_path = _default_scanner_key_path(session, request)
        payload = _build_host_payload(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            enabled=enabled,
            roots_text=roots_text,
            default_ssh_key_path=default_key_path,
            proxmox_node_name=proxmox_node_name,
        )
        host = session.get(Host, host_id) if host_id else None
        host = _save_host(session, payload, host)
        flash(request, "success", f"Saved host {host.name}.")
        return _redirect(f"/web/hosts/{host.id}")
    except Exception as exc:
        flash(request, "error", f"Failed to save host: {exc}")
        return _redirect("/web/hosts/new")


@router.post("/api/hosts/{host_id}/preset/proxmox")
async def apply_proxmox_preset(
    host_id: int,
    request: Request,
    session: Session = Depends(get_db),
    node_name: str = Form(...),
):
    host = session.execute(select(Host).where(Host.id == host_id).options(selectinload(Host.roots))).scalar_one()
    try:
        desired_roots = _build_proxmox_roots(node_name)
        payload = HostUpsert(
            name=host.name,
            address=host.address,
            ssh_port=host.ssh_port,
            ssh_user=host.ssh_user,
            ssh_key_path=host.ssh_key_path,
            enabled=host.enabled,
            roots=desired_roots,
        )
        _save_host(session, payload, host)
        flash(request, "success", f"Applied Proxmox roots for node {node_name}.")
    except Exception as exc:
        flash(request, "error", f"Failed to apply Proxmox preset: {exc}")
    return _redirect(f"/web/hosts/{host_id}")


@router.post("/api/hosts/bootstrap")
async def bootstrap_host(
    request: Request,
    session: Session = Depends(get_db),
    host_id: int | None = Form(default=None),
    name: str = Form(...),
    address: str = Form(...),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(...),
    ssh_key_path: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    roots_text: str = Form(...),
    proxmox_node_name: str | None = Form(default=None),
    bootstrap_user: str = Form(...),
    bootstrap_password: str | None = Form(default=None),
    sudo_password: str | None = Form(default=None),
):
    host: Host | None = None
    try:
        default_key_path = _default_scanner_key_path(session, request)
        payload = _build_host_payload(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            enabled=enabled,
            roots_text=roots_text,
            default_ssh_key_path=default_key_path,
            proxmox_node_name=proxmox_node_name,
        )
        host = session.get(Host, host_id) if host_id else None
        host = _save_host(session, payload, host)

        result, executor = _bootstrap_host_access(
            request,
            session,
            host=host,
            bootstrap_user=bootstrap_user,
            bootstrap_password=bootstrap_password,
            sudo_password=sudo_password,
        )
        if result.success:
            try:
                test_result = executor.test_connection(host, [root.root_path for root in host.roots if root.enabled])
                _flash_connection_result(request, test_result)
            except Exception as exc:
                flash(request, "warning", f"Post-bootstrap connectivity test failed: {exc}")
            try:
                refresh_host_facts(session, request.app.state.config, host.id)
                flash(request, "success", "Refreshed host facts after bootstrap.")
            except Exception as exc:
                flash(request, "warning", f"Bootstrap completed, but host fact refresh failed: {exc}")
        session.commit()
        _flash_connection_result(request, result)
        return _redirect(f"/web/hosts/{host.id}")
    except Exception as exc:
        if host is not None:
            host.connection_status = "unreachable"
            session.commit()
            flash(request, "error", f"Bootstrap failed: {exc}")
            return _redirect(f"/web/hosts/{host.id}")
        flash(request, "error", f"Bootstrap failed: {exc}")
        return _redirect("/web/hosts/new")


@router.post("/api/hosts/{host_id}/test")
async def test_host_connection(host_id: int, request: Request, session: Session = Depends(get_db)):
    host = session.execute(select(Host).where(Host.id == host_id).options(selectinload(Host.roots))).scalar_one()
    executor = SSHExecutor(
        connect_timeout=request.app.state.config.ssh_connect_timeout_seconds,
        command_timeout=request.app.state.config.ssh_command_timeout_seconds,
    )
    try:
        result = executor.test_connection(host, [root.root_path for root in host.roots if root.enabled])
        host.connection_status = "reachable" if result.success else "warning"
        if result.success:
            host.last_seen_at = utcnow()
        session.commit()
        _flash_connection_result(request, result)
    except Exception as exc:
        host.connection_status = "unreachable"
        session.commit()
        flash(request, "error", f"SSH test failed: {exc}")
    return _redirect(f"/web/hosts/{host_id}")


@router.post("/api/hosts/{host_id}/delete")
async def delete_host_api(
    host_id: int,
    request: Request,
    session: Session = Depends(get_db),
    return_to: str | None = Form(default="/web/hosts"),
):
    host = session.get(Host, host_id)
    target = _safe_return_target(return_to, default="/web/hosts")
    if host is None:
        flash(request, "warning", f"Host #{host_id} was already removed.")
        return _redirect(target)

    host_name = host.name
    session.delete(host)
    session.commit()
    flash(request, "success", f"Removed host {host_name}.")
    return _redirect(target)


@router.post("/api/hosts/{host_id}/facts/refresh")
async def refresh_host_facts_api(host_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        fact_snapshot = refresh_host_facts(session, request.app.state.config, host_id)
        session.commit()
        flash(request, "success", f"Refreshed host facts for host #{host_id}.")
        if fact_snapshot and fact_snapshot.detected_platform:
            flash(request, "success", f"Detected platform: {fact_snapshot.detected_platform}.")
    except Exception as exc:
        flash(request, "error", f"Failed to refresh host facts: {exc}")
    return _redirect(f"/web/hosts/{host_id}")


@router.post("/api/investigations")
async def create_investigation_api(
    request: Request,
    session: Session = Depends(get_db),
    target_type: str = Form(...),
    existing_host_id: int | None = Form(default=None),
    discovered_guest_id: int | None = Form(default=None),
    name: str | None = Form(default=None),
    address: str | None = Form(default=None),
    ssh_port: int = Form(default=22),
    ssh_user: str | None = Form(default=None),
    ssh_key_path: str | None = Form(default=None),
    roots_text: str | None = Form(default=None),
):
    try:
        investigation = create_investigation_session(
            session,
            request.app.state.config,
            target_type=target_type,
            existing_host_id=existing_host_id,
            discovered_guest_id=discovered_guest_id,
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            roots_text=roots_text,
        )
        session.commit()
        queue_investigation_collect(request.app, investigation.id, recollect=True)
        flash(request, "success", f"Started investigation session #{investigation.id}.")
        return _investigation_redirect(investigation.id)
    except Exception as exc:
        flash(request, "error", f"Failed to start investigation: {exc}")
        return _redirect("/web/investigations/new")


@router.post("/api/investigations/{investigation_id}/message")
async def investigation_message_api(
    investigation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    content: str = Form(...),
):
    wants_json = "application/json" in (request.headers.get("accept") or "")
    try:
        investigation = load_investigation_session(session, investigation_id)
        user_message = post_investigation_message(session, investigation, content)
        recollect = investigation_message_requires_recollect(investigation, content)
        session.commit()
        if investigation.confirmation_state != "confirmed":
            queue_investigation_collect(request.app, investigation.id, recollect=recollect)
            if not wants_json:
                if recollect:
                    flash(request, "success", "Saved your response and queued a fresh fact collection plus draft refresh.")
                else:
                    flash(request, "success", "Saved your response and queued a draft refresh.")
        else:
            if not wants_json:
                flash(request, "success", "Saved your response and marked the investigation confirmed.")
        if wants_json:
            return JSONResponse({
                "ok": True,
                "message_id": user_message.id,
                "created_at": user_message.created_at.isoformat() if user_message.created_at else None,
            })
    except Exception as exc:
        if wants_json:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        flash(request, "error", f"Failed to save investigation message: {exc}")
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/collect")
async def investigation_collect_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        investigation = load_investigation_session(session, investigation_id)
        session.commit()
        queue_investigation_collect(request.app, investigation.id, recollect=True)
        flash(request, "success", "Queued a fresh fact collection run for this investigation.")
    except Exception as exc:
        flash(request, "error", f"Failed to queue investigation collection: {exc}")
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/regenerate")
async def investigation_regenerate_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        investigation = load_investigation_session(session, investigation_id)
        session.commit()
        queue_investigation_collect(request.app, investigation.id, recollect=False)
        flash(request, "success", "Queued a draft regeneration using the latest answers and findings.")
    except Exception as exc:
        flash(request, "error", f"Failed to queue investigation regeneration: {exc}")
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/confirm")
async def investigation_confirm_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        investigation = load_investigation_session(session, investigation_id)
        confirm_investigation(session, investigation)
        session.commit()
        flash(request, "success", "Confirmed the investigation draft. Apply it when you are ready.")
    except Exception as exc:
        flash(request, "error", f"Failed to confirm investigation: {exc}")
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/apply")
async def investigation_apply_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        investigation = load_investigation_session(session, investigation_id)
        apply_investigation(session, request.app, investigation)
        session.commit()
        flash(request, "success", "Applied the confirmed investigation changes.")
    except Exception as exc:
        flash(request, "error", f"Failed to apply investigation: {exc}")
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/stop")
async def investigation_stop_api(investigation_id: int, request: Request):
    message = request_investigation_stop(request.app, investigation_id)
    level = "success" if message.startswith("Cancelled") or message.startswith("Stop requested") else "warning"
    flash(request, level, message)
    return _investigation_redirect(investigation_id)


@router.post("/api/investigations/{investigation_id}/delete")
async def investigation_delete_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        investigation = load_investigation_session(session, investigation_id)
        delete_investigation_session(request.app, session, investigation)
        session.commit()
        flash(request, "success", f"Deleted investigation #{investigation_id}.")
        return _redirect("/web/investigations")
    except Exception as exc:
        session.rollback()
        flash(request, "error", f"Failed to delete investigation: {exc}")
        return _investigation_redirect(investigation_id)


@router.get("/api/investigations/{investigation_id}/status")
async def investigation_status_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    investigation = load_investigation_session(session, investigation_id)
    if reconcile_orphaned_investigation(request.app, session, investigation):
        session.commit()
        investigation = load_investigation_session(session, investigation_id)
    draft_state = investigation_draft_state_payload(investigation)
    working_preview = build_investigation_working_preview(session, investigation)
    runtime_state = investigation_runtime_state(request.app, investigation_id)
    return JSONResponse(
        {
            "session": {
                "id": investigation.id,
                "status": investigation.status,
                "current_phase": investigation.current_phase,
                "confirmation_state": investigation.confirmation_state,
                "updated_at": investigation.updated_at.isoformat() if investigation.updated_at else None,
                "last_error": investigation.last_error,
                "draft_report_text": draft_state.get("report_text") or investigation.draft_report_text,
                "draft_report_json": draft_state,
            },
            "runtime": runtime_state,
            "working_preview": working_preview.model_dump(mode="json"),
            "messages": [
                {
                    "id": message.id,
                    "sender_type": message.sender_type,
                    "message_type": message.message_type,
                    "content": message.content,
                    "metadata": message.metadata_json or {},
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "hidden_internal": message.hidden_internal,
                }
                for message in investigation.messages
                if not message.hidden_internal and message.sender_type in {"user", "ai"}
            ],
            "findings": [
                {
                    "id": finding.id,
                    "finding_type": finding.finding_type,
                    "title": finding.title,
                    "summary": finding.summary,
                    "details": finding.details_json,
                    "confidence": finding.confidence,
                    "source_type": finding.source_type,
                }
                for finding in investigation.findings
            ],
            "proposals": [
                {
                    "id": proposal.id,
                    "proposal_type": proposal.proposal_type,
                    "target_kind": proposal.target_kind,
                    "target_id": proposal.target_id,
                    "field_name": proposal.field_name,
                    "old_value": proposal.old_value_json,
                    "new_value": proposal.new_value_json,
                    "source": proposal.source,
                    "rationale": proposal.rationale,
                    "confidence": proposal.confidence,
                    "status": proposal.status,
                }
                for proposal in investigation.proposals
            ],
        },
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@router.get("/api/investigations/{investigation_id}/debug")
async def investigation_debug_api(investigation_id: int, request: Request, session: Session = Depends(get_db)):
    investigation = load_investigation_session(session, investigation_id)
    draft_state = investigation_draft_state_payload(investigation)
    runtime_state = investigation_runtime_state(request.app, investigation_id)
    messages = [
        {
            "id": m.id,
            "sender": m.sender_type,
            "type": m.message_type,
            "hidden": m.hidden_internal,
            "content": m.content[:300] if m.content else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in investigation.messages[-20:]
    ]
    return JSONResponse(
        {
            "investigation_id": investigation.id,
            "status": investigation.status,
            "phase": investigation.current_phase,
            "confirmation_state": investigation.confirmation_state,
            "last_error": investigation.last_error,
            "updated_at": investigation.updated_at.isoformat() if investigation.updated_at else None,
            "target": f"{investigation.target_name} ({investigation.target_address})",
            "draft_visible_source": draft_state.get("visible_draft_source"),
            "draft_preview_source": draft_state.get("preview_draft_source"),
            "assistant_draft_version": draft_state.get("assistant_draft_version"),
            "assistant_draft_updated_at": draft_state.get("assistant_draft_updated_at"),
            "open_question": draft_state.get("open_question"),
            "runtime": runtime_state,
            "has_raw_facts": bool(investigation.raw_facts_json),
            "has_normalized_findings": bool(investigation.normalized_findings_json),
            "findings_count": len(investigation.findings),
            "proposals_count": len(investigation.proposals),
            "message_count": len(investigation.messages),
            "recent_messages": messages,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/api/hosts/{host_id}/topology/refresh")
async def refresh_host_topology_api(host_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        summary = refresh_host_topology(session, host_id)
        session.commit()
        flash(
            request,
            "success",
            f"Refreshed topology for host #{host_id}: {summary['updated_guests']} guest entries checked, {summary['new_guests']} new.",
        )
    except Exception as exc:
        flash(request, "error", f"Failed to refresh topology: {exc}")
    return _redirect(f"/web/hosts/{host_id}")


def _guest_prefill_name(guest: GuestInventory) -> str:
    return guest.name or f"{guest.host.name}-{guest.guest_type}-{guest.guest_id}"


def _guest_prefill_roots(guest: GuestInventory) -> list[str]:
    roots: list[str] = []
    if guest.name:
        roots.append(f"/home/{guest.name}")
    roots.extend(["/srv/projects", "/opt"])
    deduped: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if root not in seen:
            deduped.append(root)
            seen.add(root)
    return deduped


@router.post("/api/guests/{guest_id}/enroll")
async def enroll_guest_as_host_api(
    guest_id: int,
    request: Request,
    session: Session = Depends(get_db),
    existing_host_id: int | None = Form(default=None),
    name: str = Form(...),
    address: str = Form(default=""),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(default="aicms"),
    ssh_key_path: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    roots_text: str = Form(...),
):
    guest = session.execute(
        select(GuestInventory)
        .where(GuestInventory.id == guest_id)
        .options(selectinload(GuestInventory.host))
    ).scalar_one()
    try:
        if existing_host_id:
            child_host = session.get(Host, existing_host_id)
            if child_host is None:
                raise ValueError("Selected host record was not found.")
            link_discovered_guest_to_host(session, guest, child_host, source="manual", confidence=1.0)
            session.commit()
            flash(request, "success", f"Linked discovered guest {guest.guest_id} to host {child_host.name}.")
            return _redirect(f"/web/hosts/{child_host.id}")

        payload = _build_host_payload(
            name=name,
            address=address,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            enabled=enabled,
            roots_text=roots_text,
            default_ssh_key_path=_default_scanner_key_path(session, request),
        )
        child_host = _save_host(session, payload)
        link_discovered_guest_to_host(session, guest, child_host, source="proxmox_discovery", confidence=1.0)
        session.commit()
        flash(request, "success", f"Added host {child_host.name} from discovered guest inventory.")
        return _redirect(f"/web/hosts/{child_host.id}")
    except Exception as exc:
        flash(request, "error", f"Failed to add discovered guest as host: {exc}")
        return _redirect(f"/web/guests/{guest_id}/enroll")


@router.post("/api/guests/{guest_id}/link")
async def link_guest_api(
    guest_id: int,
    request: Request,
    session: Session = Depends(get_db),
    existing_host_id: int = Form(...),
):
    try:
        guest = session.execute(
            select(GuestInventory)
            .where(GuestInventory.id == guest_id)
            .options(selectinload(GuestInventory.host))
        ).scalar_one()
        child_host = session.get(Host, existing_host_id)
        if child_host is None:
            raise ValueError("Selected host record was not found.")
        link_discovered_guest_to_host(session, guest, child_host, source="manual", confidence=1.0)
        session.commit()
        flash(request, "success", f"Linked guest {guest.guest_type.upper()} {guest.guest_id} to host {child_host.name}.")
        return _redirect(f"/web/hosts/{guest.host_id}")
    except Exception as exc:
        flash(request, "error", f"Failed to link discovered guest: {exc}")
        return _redirect("/web/hosts")


@router.post("/api/guests/{guest_id}/unmanaged")
async def mark_guest_unmanaged_api(guest_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        guest = session.execute(select(GuestInventory).where(GuestInventory.id == guest_id)).scalar_one()
        host_id = guest.host_id
        mark_discovered_guest_unmanaged(session, guest)
        session.commit()
        flash(request, "success", f"Marked guest {guest.guest_type.upper()} {guest.guest_id} as unmanaged.")
        return _redirect(f"/web/hosts/{host_id}")
    except Exception as exc:
        flash(request, "error", f"Failed to mark guest unmanaged: {exc}")
        return _redirect("/web/hosts")


@router.post("/api/guests/{guest_id}/bootstrap")
async def bootstrap_discovered_guest_api(
    guest_id: int,
    request: Request,
    session: Session = Depends(get_db),
    existing_host_id: int | None = Form(default=None),
    name: str = Form(...),
    address: str = Form(default=""),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(default="aicms"),
    ssh_key_path: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    roots_text: str = Form(...),
    bootstrap_user: str = Form(...),
    bootstrap_password: str | None = Form(default=None),
    sudo_password: str | None = Form(default=None),
):
    guest = session.execute(
        select(GuestInventory)
        .where(GuestInventory.id == guest_id)
        .options(selectinload(GuestInventory.host))
    ).scalar_one()
    try:
        if existing_host_id:
            host = session.get(Host, existing_host_id)
            if host is None:
                raise ValueError("Selected host record was not found.")
        else:
            payload = _build_host_payload(
                name=name,
                address=address,
                ssh_port=ssh_port,
                ssh_user=ssh_user,
                ssh_key_path=ssh_key_path,
                enabled=enabled,
                roots_text=roots_text,
                default_ssh_key_path=_default_scanner_key_path(session, request),
            )
            host = _save_host(session, payload)

        result, executor = _bootstrap_host_access(
            request,
            session,
            host=host,
            bootstrap_user=bootstrap_user,
            bootstrap_password=bootstrap_password,
            sudo_password=sudo_password,
        )
        if result.success:
            link_discovered_guest_to_host(session, guest, host, source="bootstrap", confidence=1.0)
            try:
                test_result = executor.test_connection(host, [root.root_path for root in host.roots if root.enabled])
                _flash_connection_result(request, test_result)
            except Exception as exc:
                flash(request, "warning", f"Post-bootstrap connectivity test failed: {exc}")
            try:
                refresh_host_facts(session, request.app.state.config, host.id)
                flash(request, "success", "Refreshed host facts after guest bootstrap.")
                flash(request, "success", "Refresh the machine profile after the next scan if you want the new host facts reflected there.")
            except Exception as exc:
                flash(request, "warning", f"Guest bootstrap completed, but host fact refresh failed: {exc}")
        session.commit()
        _flash_connection_result(request, result)
        return _redirect(f"/web/hosts/{host.id}")
    except Exception as exc:
        flash(request, "error", f"Guest bootstrap failed: {exc}")
        return _redirect(f"/web/guests/{guest_id}/enroll")


@router.post("/api/hosts/{host_id}/scan/full")
async def scan_host_full_api(host_id: int, request: Request):
    scan_id = queue_scan(request.app, "full", [host_id])
    flash(request, "success", f"Queued full scan #{scan_id}.")
    return _redirect(f"/web/scans?scan_id={scan_id}")


@router.post("/api/hosts/{host_id}/scan/incremental")
async def scan_host_incremental_api(host_id: int, request: Request):
    scan_id = queue_scan(request.app, "incremental", [host_id])
    flash(request, "success", f"Queued incremental scan #{scan_id}.")
    return _redirect(f"/web/scans?scan_id={scan_id}")


@router.post("/api/scan/full")
async def scan_full_api(request: Request):
    scan_id = queue_scan(request.app, "full")
    flash(request, "success", f"Queued full scan #{scan_id}.")
    return _redirect(f"/web/scans?scan_id={scan_id}")


@router.post("/api/scan/incremental")
async def scan_incremental_api(request: Request):
    scan_id = queue_scan(request.app, "incremental")
    flash(request, "success", f"Queued incremental scan #{scan_id}.")
    return _redirect(f"/web/scans?scan_id={scan_id}")


@router.get("/api/scans/{scan_id}")
async def scan_status_api(scan_id: int, request: Request, session: Session = Depends(get_db)):
    scan = session.get(Scan, scan_id)
    if scan is None:
        return JSONResponse({"error": "Scan not found."}, status_code=404)
    if reconcile_orphaned_scan(request.app, session, scan):
        session.commit()
        session.refresh(scan)

    events = session.execute(
        select(ScanEvent).where(ScanEvent.scan_id == scan_id).order_by(ScanEvent.created_at.desc()).limit(200)
    ).scalars().all()
    return JSONResponse(
        {
            "scan": {
                "id": scan.id,
                "scan_type": scan.scan_type,
                "status": scan.status,
                "started_at": scan.started_at.isoformat() if scan.started_at else None,
                "finished_at": scan.finished_at.isoformat() if scan.finished_at else None,
                "hosts_scanned": scan.hosts_scanned,
                "projects_found": scan.projects_found,
                "errors_count": scan.errors_count,
                "summary": scan.summary,
            },
            "events": [
                {
                    "id": event.id,
                    "level": event.level,
                    "event_type": event.event_type,
                    "message": event.message,
                    "details": event.details_json,
                    "created_at": event.created_at.isoformat() if event.created_at else None,
                }
                for event in events
            ],
        }
    )


@router.post("/api/scans/{scan_id}/stop")
async def stop_scan_api(scan_id: int, request: Request):
    message = request_scan_stop(request.app, scan_id)
    flash(request, "warning", message)
    return _redirect(f"/web/scans?scan_id={scan_id}")


def _scan_is_active(scan: Scan) -> bool:
    return scan.status in {"queued", "running", "cancelling"}


@router.post("/api/scans/{scan_id}/delete")
async def delete_scan_api(scan_id: int, request: Request, session: Session = Depends(get_db)):
    scan = session.get(Scan, scan_id)
    if scan is None:
        flash(request, "warning", f"Scan #{scan_id} was already removed.")
        return _redirect("/web/scans")

    if _scan_is_active(scan):
        flash(request, "warning", f"Stop scan #{scan.id} before removing it from history.")
        return _redirect(f"/web/scans?scan_id={scan.id}")

    session.delete(scan)
    session.commit()
    flash(request, "success", f"Removed scan #{scan_id} from history.")
    return _redirect("/web/scans")


@router.post("/api/scans/delete-all")
async def delete_all_scan_history_api(request: Request, session: Session = Depends(get_db)):
    scans = session.execute(select(Scan).order_by(Scan.started_at.desc(), Scan.id.desc())).scalars().all()
    if not scans:
        flash(request, "warning", "No scan history to remove.")
        return _redirect("/web/scans")

    removable_scans = [scan for scan in scans if not _scan_is_active(scan)]
    active_scans = [scan for scan in scans if _scan_is_active(scan)]

    if not removable_scans:
        flash(request, "warning", "No finished scan history can be removed while active scans are still running.")
        return _redirect("/web/scans")

    for scan in removable_scans:
        session.delete(scan)
    session.commit()

    if active_scans:
        flash(
            request,
            "warning",
            f"Removed {len(removable_scans)} finished scan(s). Kept {len(active_scans)} active scan(s).",
        )
    else:
        flash(request, "success", f"Removed {len(removable_scans)} scan(s) from history.")
    return _redirect("/web/scans")


@router.post("/api/settings/general")
async def save_general_settings(
    request: Request,
    session: Session = Depends(get_db),
    app_name: str = Form(...),
    timezone: str = Form(...),
    default_max_tree_depth: int = Form(...),
    default_scanner_ssh_key_path: str | None = Form(default="~/.ssh/aicms_scanner_ed25519"),
):
    try:
        payload = GeneralSettingsPayload(
            app_name=app_name,
            timezone=timezone,
            default_max_tree_depth=default_max_tree_depth,
            default_scanner_ssh_key_path=default_scanner_ssh_key_path,
        )
        set_settings(session, GENERAL_SETTINGS_KEY, payload)
        flash(request, "success", "Saved general settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save general settings: {exc}")
    return _redirect("/web/settings?tab=general")


@router.post("/api/settings/scan")
async def save_scan_settings(
    request: Request,
    session: Session = Depends(get_db),
    ignore_patterns: str = Form(...),
    allowed_marker_files: str = Form(...),
    max_read_bytes: int = Form(...),
    collect_shallow_file_trees: str | None = Form(default=None),
    collect_recent_git_metadata: str | None = Form(default=None),
):
    try:
        payload = ScanSettingsPayload(
            ignore_patterns=clean_multiline(ignore_patterns),
            allowed_marker_files=clean_multiline(allowed_marker_files),
            max_read_bytes=max_read_bytes,
            collect_shallow_file_trees=truthy(collect_shallow_file_trees),
            collect_recent_git_metadata=truthy(collect_recent_git_metadata),
        )
        set_settings(session, SCAN_SETTINGS_KEY, payload)
        flash(request, "success", "Saved scan settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save scan settings: {exc}")
    return _redirect("/web/settings?tab=scan")


@router.post("/api/settings/lmstudio")
async def save_lmstudio_settings(
    request: Request,
    session: Session = Depends(get_db),
    enabled: str | None = Form(default=None),
    base_url: str = Form(...),
    api_token: str | None = Form(default=None),
    selected_model: str | None = Form(default=None),
    temperature: float = Form(...),
    use_max_output_tokens: str | None = Form(default=None),
    max_tokens: int = Form(...),
    use_custom_timeout: str | None = Form(default=None),
    request_timeout_seconds: int = Form(default=60),
):
    existing = get_settings(session, request.app.state.config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    try:
        payload = LMStudioSettingsPayload(
            enabled=truthy(enabled),
            base_url=base_url,
            api_token=api_token or existing.api_token,
            selected_model=selected_model,
            temperature=temperature,
            use_max_output_tokens=truthy(use_max_output_tokens),
            max_tokens=max_tokens,
            use_custom_timeout=truthy(use_custom_timeout),
            request_timeout_seconds=request_timeout_seconds,
            last_successful_contact=existing.last_successful_contact,
            last_error=existing.last_error,
            last_refresh_time=existing.last_refresh_time,
        )
        set_settings(session, LMSTUDIO_SETTINGS_KEY, payload)
        flash(request, "success", "Saved LM Studio settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save LM Studio settings: {exc}")
    return _redirect("/web/settings?tab=lmstudio")


@router.post("/api/settings/profiles")
async def save_profile_settings(
    request: Request,
    session: Session = Depends(get_db),
    enabled: str | None = Form(default=None),
    enable_lmstudio_formatting: str | None = Form(default=None),
    fallback_to_deterministic_profile: str | None = Form(default=None),
    max_guests_shown: int = Form(default=24),
    max_services_shown: int = Form(default=20),
    max_setup_bullets_shown: int = Form(default=10),
):
    try:
        payload = ProfileSettingsPayload(
            enabled=truthy(enabled),
            enable_lmstudio_formatting=truthy(enable_lmstudio_formatting),
            fallback_to_deterministic_profile=truthy(fallback_to_deterministic_profile),
            max_guests_shown=max_guests_shown,
            max_services_shown=max_services_shown,
            max_setup_bullets_shown=max_setup_bullets_shown,
        )
        set_settings(session, PROFILE_SETTINGS_KEY, payload)
        flash(request, "success", "Saved machine profile settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save machine profile settings: {exc}")
    return _redirect("/web/settings?tab=profiles")


@router.post("/api/settings/mcp")
async def save_mcp_settings(
    request: Request,
    session: Session = Depends(get_db),
    enabled: str | None = Form(default=None),
    server_url: str | None = Form(default=None),
    api_token: str | None = Form(default=None),
    allowed_tools: str | None = Form(default=None),
    read_only_search_only: str | None = Form(default=None),
    auto_enrich_after_scan: str | None = Form(default=None),
    max_searches_per_profile_refresh: int = Form(default=3),
    timeout_seconds: int = Form(default=15),
    max_results_items: int = Form(default=10),
):
    existing = get_settings(session, request.app.state.config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    try:
        payload = MCPSettingsPayload(
            enabled=truthy(enabled),
            server_url=server_url,
            api_token=api_token or existing.api_token,
            allowed_tools=allowed_tools if allowed_tools is not None else existing.allowed_tools,
            read_only_search_only=truthy(read_only_search_only),
            auto_enrich_after_scan=truthy(auto_enrich_after_scan),
            max_searches_per_profile_refresh=max_searches_per_profile_refresh,
            timeout_seconds=timeout_seconds,
            max_results_items=max_results_items,
            last_successful_contact=existing.last_successful_contact,
            last_error=existing.last_error,
        )
        set_settings(session, MCP_SETTINGS_KEY, payload)
        flash(request, "success", "Saved MCP integration settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save MCP settings: {exc}")
    return _redirect("/web/settings?tab=mcp")


@router.post("/api/settings/web-search")
async def save_web_search_settings(
    request: Request,
    session: Session = Depends(get_db),
    enabled: str | None = Form(default=None),
    engine: str = Form(default="searxng"),
    query_url_template: str = Form(...),
    language: str = Form(default="all"),
    result_count: int = Form(default=10),
    concurrent_requests: int = Form(default=10),
    domain_filters: str | None = Form(default=None),
    bypass_embedding_and_retrieval: str | None = Form(default=None),
    bypass_web_loader: str | None = Form(default=None),
    trust_proxy_environment: str | None = Form(default=None),
    loader_engine: str = Form(default="default"),
    timeout: int = Form(default=15),
    verify_ssl_certificate: str | None = Form(default=None),
    loader_concurrent_requests: int = Form(default=10),
    youtube_language: str = Form(default="en"),
    youtube_proxy_url: str | None = Form(default=None),
):
    existing = get_settings(session, request.app.state.config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    try:
        payload = WebSearchSettingsPayload(
            enabled=truthy(enabled),
            engine=engine,
            query_url_template=query_url_template,
            language=language,
            result_count=result_count,
            concurrent_requests=concurrent_requests,
            domain_filters=domain_filters if domain_filters is not None else existing.domain_filters,
            bypass_embedding_and_retrieval=truthy(bypass_embedding_and_retrieval),
            bypass_web_loader=truthy(bypass_web_loader),
            trust_proxy_environment=truthy(trust_proxy_environment),
            loader_engine=loader_engine,
            timeout=timeout,
            verify_ssl_certificate=truthy(verify_ssl_certificate),
            loader_concurrent_requests=loader_concurrent_requests,
            youtube_language=youtube_language,
            youtube_proxy_url=youtube_proxy_url,
            last_successful_connection=existing.last_successful_connection,
            last_error=existing.last_error,
        )
        set_settings(session, WEB_SEARCH_SETTINGS_KEY, payload)
        flash(request, "success", "Saved Web Search settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save Web Search settings: {exc}")
    return _redirect("/web/settings?tab=web-search")


@router.post("/api/settings/github")
async def save_github_settings(
    request: Request,
    session: Session = Depends(get_db),
    repo_owner: str | None = Form(default=None),
    repo_name: str | None = Form(default=None),
    branch: str = Form(default="main"),
    pat: str | None = Form(default=None),
):
    existing = get_settings(session, request.app.state.config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
    try:
        payload = GitHubSettingsPayload(
            repo_owner=repo_owner,
            repo_name=repo_name,
            branch=branch,
            pat=pat or existing.pat,
            last_checked_at=existing.last_checked_at,
            local_revision=existing.local_revision,
            remote_revision=existing.remote_revision,
            update_available=existing.update_available,
            last_error=existing.last_error,
        )
        set_settings(session, GITHUB_SETTINGS_KEY, payload)
        flash(request, "success", "Saved GitHub settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save GitHub settings: {exc}")
    return _redirect("/web/settings?tab=github")


@router.post("/api/lmstudio/test")
async def lmstudio_test_api(
    request: Request,
    session: Session = Depends(get_db),
    mode: str = Form(default="connection"),
):
    try:
        message = test_connection(session, request.app.state.config, prompt_test=(mode == "prompt"))
        flash(request, "success", message)
    except Exception as exc:
        flash(request, "error", f"LM Studio test failed: {exc}")
    return _redirect("/web/lmstudio")


@router.post("/api/lmstudio/models/refresh")
async def lmstudio_refresh_models_api(request: Request, session: Session = Depends(get_db)):
    try:
        count = refresh_models(session, request.app.state.config)
        flash(request, "success", f"Refreshed {count} LM Studio model(s).")
    except Exception as exc:
        flash(request, "error", f"Failed to refresh LM Studio models: {exc}")
    return _redirect("/web/lmstudio")


@router.post("/api/github/check-updates")
async def github_check_updates_api(request: Request, session: Session = Depends(get_db)):
    try:
        result = check_for_updates(session, request.app.state.config, request.app.state.repo_path)
        flash(request, "success", result.message)
    except Exception as exc:
        settings = get_settings(session, request.app.state.config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
        settings.last_error = str(exc)
        set_settings(session, GITHUB_SETTINGS_KEY, settings)
        flash(request, "error", f"GitHub update check failed: {exc}")
    return _redirect("/web/settings?tab=github")


@router.post("/api/github/run-update")
async def github_run_update_api(
    request: Request,
    session: Session = Depends(get_db),
    sudo_password: str | None = Form(default=None),
):
    try:
        github_settings = get_settings(session, request.app.state.config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
        result = start_update_job(
            config=request.app.state.config,
            repo_path=request.app.state.repo_path,
            sudo_password=(sudo_password or "").strip() or None,
            github_pat=github_settings.pat,
            github_branch=github_settings.branch,
        )
        flash(request, "success", f"Started updater job (PID {result['pid']}). The service may restart during the update.")
        flash(request, "success", "Reload the GitHub settings tab shortly to see the latest updater status and log output.")
    except Exception as exc:
        flash(request, "error", f"Failed to start updater: {exc}")
    return _redirect("/web/settings?tab=github")


@router.post("/api/mcp/test")
async def mcp_test_api(request: Request, session: Session = Depends(get_db)):
    try:
        summary, tools = test_mcp_connection(session, request.app.state.config)
        flash(request, "success", f"MCP connection succeeded. {summary}")
        if tools:
            flash(request, "success", f"Discovered {len(tools)} MCP tool(s).")
    except Exception as exc:
        flash(request, "error", f"MCP test failed: {exc}")
    return _redirect("/web/settings?tab=mcp")


@router.post("/api/web-search/test")
async def web_search_test_api(request: Request, session: Session = Depends(get_db)):
    try:
        preview = test_web_search_connection(session, request.app.state.config)
        flash(request, "success", f"Web Search connection succeeded: {preview}")
    except Exception as exc:
        flash(request, "error", f"Web Search test failed: {exc}")
    return _redirect("/web/settings?tab=web-search")


@router.post("/api/web-search/test-search")
async def web_search_test_search_api(
    request: Request,
    session: Session = Depends(get_db),
    query: str = Form(default="aicms test"),
):
    try:
        result = test_web_search_search(session, request.app.state.config, query=query)
        flash(request, "success", f"Web Search test returned {len(result['results'])} result(s).")
        flash(request, "success", f"Rendered request: {result['preview_url']}")
    except Exception as exc:
        flash(request, "error", f"Web Search test search failed: {exc}")
    return _redirect("/web/settings?tab=web-search")


@router.post("/api/projects/{project_id}/profile")
@router.post("/api/projects/{project_id}/interpret")
async def interpret_project_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        project = session.execute(select(Project).where(Project.id == project_id)).scalar_one()
        queue_project_interpretation(request.app, project_id)
        flash(request, "success", f"Started profile refresh for {project.path}.")
    except Exception as exc:
        flash(request, "error", f"Profile refresh failed: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/identity/refresh")
async def refresh_identity_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        identity = refresh_machine_identity(session, request.app.state.config, project_id)
        refresh_project_mapping_preview(session, request.app.state.config, project_id)
        session.commit()
        label = identity.primary_signature_name or identity.detected_role
        flash(request, "success", f"Refreshed deterministic identity: {label}.")
    except Exception as exc:
        flash(request, "error", f"Failed to refresh identity: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/mapping/refresh")
async def refresh_mapping_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        project = session.execute(
            select(Project)
            .where(Project.id == project_id)
            .options(selectinload(Project.identity_result))
        ).scalar_one()
        if project.identity_result is None:
            refresh_machine_identity(session, request.app.state.config, project_id)
        preview = refresh_project_mapping_preview(session, request.app.state.config, project_id)
        session.commit()
        flash(request, "success", f"Rebuilt project mapping preview: {preview.recommended_status}.")
    except Exception as exc:
        flash(request, "error", f"Failed to rebuild project mapping preview: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/research/refresh")
async def refresh_research_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        outcome = refresh_project_research(session, request.app.state.config, project_id, force=True)
        session.commit()
        label = outcome.title_hint or outcome.detected_platform_hint or "research findings"
        flash(request, "success", f"Completed bounded research refresh: {label}.")
    except Exception as exc:
        flash(request, "error", f"Failed to run research: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/card-copy/refresh")
async def refresh_card_copy_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        preview = refresh_project_mapping_preview(session, request.app.state.config, project_id)
        session.commit()
        flash(request, "success", f"Refreshed project card copy: {preview.card_title_candidate or preview.project_title_candidate}.")
    except Exception as exc:
        flash(request, "error", f"Failed to refresh card copy: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/publish-copy/refresh")
async def refresh_publish_copy_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        preview = refresh_project_mapping_preview(session, request.app.state.config, project_id)
        session.commit()
        flash(request, "success", "Refreshed publish-ready project copy.")
    except Exception as exc:
        flash(request, "error", f"Failed to refresh publish copy: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/profile/stop")
@router.post("/api/projects/{project_id}/interpret/stop")
async def stop_project_interpretation_api(project_id: int, request: Request):
    message = request_project_interpretation_stop(request.app, project_id)
    flash(request, "warning", message)
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/context")
async def save_project_context_api(
    project_id: int,
    request: Request,
    session: Session = Depends(get_db),
    added_info: str | None = Form(default=None),
    extra_instructions: str | None = Form(default=None),
    use_project_token_limit: str | None = Form(default=None),
    project_max_tokens: int = Form(default=2048),
):
    try:
        payload = ProjectContextPayload(
            added_info=added_info,
            extra_instructions=extra_instructions,
            use_project_token_limit=truthy(use_project_token_limit),
            project_max_tokens=project_max_tokens,
        )
        project = session.execute(select(Project).where(Project.id == project_id)).scalar_one()
        metadata = dict(project.metadata_json or {})
        operator_context = payload.model_dump(exclude_none=True)
        operator_context.pop("use_project_token_limit", None)
        operator_context.pop("project_max_tokens", None)
        if operator_context:
            metadata["operator_context"] = operator_context
        else:
            metadata.pop("operator_context", None)
        if payload.use_project_token_limit:
            metadata["interpretation_preferences"] = {"max_output_tokens": payload.project_max_tokens}
        else:
            metadata.pop("interpretation_preferences", None)
        project.metadata_json = metadata
        session.commit()
        flash(request, "success", "Saved project notes, instructions, and profile settings.")
    except Exception as exc:
        flash(request, "error", f"Failed to save project notes: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/questions/generate")
async def generate_project_questions_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        project = session.execute(
            select(Project)
            .where(Project.id == project_id)
            .options(
                selectinload(Project.host).selectinload(Host.parent_host),
                selectinload(Project.identity_result),
                selectinload(Project.mapping_preview),
                selectinload(Project.machine_profile),
                selectinload(Project.clarification_threads).selectinload(ProjectQuestionThread.messages),
                selectinload(Project.clarification_guidance),
            )
        ).scalar_one()
        created = generate_project_clarification_questions(session, project, force=False)
        session.commit()
        if created:
            flash(request, "success", f"Generated {len(created)} AI clarification question(s).")
        else:
            flash(request, "success", "No new AI questions were needed right now.")
    except Exception as exc:
        flash(request, "error", f"Failed to generate AI questions: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/questions/{thread_id}/answer")
async def answer_project_question_api(
    project_id: int,
    thread_id: int,
    request: Request,
    session: Session = Depends(get_db),
    answer_text: str = Form(...),
):
    try:
        thread = session.execute(
            select(ProjectQuestionThread)
            .where(ProjectQuestionThread.id == thread_id, ProjectQuestionThread.project_id == project_id)
            .options(selectinload(ProjectQuestionThread.project).selectinload(Project.clarification_guidance))
        ).scalar_one()
        answer_project_question(session, thread, answer_text)
        session.commit()
        flash(request, "success", "Saved answer and updated project guidance.")
    except Exception as exc:
        flash(request, "error", f"Failed to save answer: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/questions/{thread_id}/status")
async def update_project_question_status_api(
    project_id: int,
    thread_id: int,
    request: Request,
    session: Session = Depends(get_db),
    status: str = Form(...),
):
    try:
        thread = session.execute(
            select(ProjectQuestionThread)
            .where(ProjectQuestionThread.id == thread_id, ProjectQuestionThread.project_id == project_id)
        ).scalar_one()
        set_project_question_status(session, thread, status)
        session.commit()
        flash(request, "success", f"Marked question as {thread.status}.")
    except Exception as exc:
        flash(request, "error", f"Failed to update question status: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/questions/{thread_id}/follow-up")
async def follow_up_project_question_api(project_id: int, thread_id: int, request: Request, session: Session = Depends(get_db)):
    try:
        thread = session.execute(
            select(ProjectQuestionThread)
            .where(ProjectQuestionThread.id == thread_id, ProjectQuestionThread.project_id == project_id)
            .options(selectinload(ProjectQuestionThread.messages))
        ).scalar_one()
        ask_project_question_follow_up(session, thread)
        session.commit()
        flash(request, "success", "Asked a follow-up clarification question.")
    except Exception as exc:
        flash(request, "error", f"Failed to ask follow-up question: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/questions/regenerate")
async def regenerate_from_project_answers_api(
    project_id: int,
    request: Request,
    session: Session = Depends(get_db),
    scope: str = Form(default="mapping"),
):
    target_scope = scope if scope in {"identity", "title", "summary", "mapping", "public_copy", "all"} else "mapping"
    try:
        if target_scope in {"identity", "all"}:
            identity = refresh_machine_identity(session, request.app.state.config, project_id)
            flash(request, "success", f"Regenerated identity from answers: {identity.primary_signature_name or identity.detected_role}.")

        if target_scope in {"title", "summary", "mapping", "public_copy", "all", "identity"}:
            preview = refresh_project_mapping_preview(session, request.app.state.config, project_id)
            if target_scope == "title":
                flash(request, "success", f"Regenerated title from answers: {preview.publish_title_candidate or preview.project_title_candidate}.")
            elif target_scope == "summary":
                flash(request, "success", "Regenerated summary from answers.")
            elif target_scope == "mapping":
                flash(request, "success", "Regenerated project mapping from answers.")
            elif target_scope == "public_copy":
                flash(request, "success", "Rebuilt public copy using answered questions.")
            elif target_scope == "all":
                flash(request, "success", "Regenerated identity, mapping, and public copy from answers.")
        session.commit()
    except Exception as exc:
        flash(request, "error", f"Failed to regenerate from answers: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/overrides")
async def save_project_publishability_overrides_api(
    project_id: int,
    request: Request,
    session: Session = Depends(get_db),
    project_label_override: str | None = Form(default=None),
    public_title_override: str | None = Form(default=None),
    public_summary_hint: str | None = Form(default=None),
    publishable_project: str | None = Form(default=None),
    internal_only: str | None = Form(default=None),
    related_host_ids: list[str] | None = Form(default=None),
    public_tech_tags_override: str | None = Form(default=None),
    project_group: str | None = Form(default=None),
    notes_for_cms_export: str | None = Form(default=None),
):
    try:
        project = session.execute(
            select(Project)
            .where(Project.id == project_id)
            .options(selectinload(Project.host).selectinload(Host.profile_override))
        ).scalar_one()
        payload = HostProfileOverridePayload(
            project_label_override=project_label_override,
            public_title_override=public_title_override,
            public_summary_hint=public_summary_hint,
            publishable_project=_nullable_toggle(publishable_project),
            internal_only=_nullable_toggle(internal_only),
            related_host_ids=related_host_ids or [],
            public_tech_tags_override=public_tech_tags_override,
            project_group=project_group,
            notes_for_cms_export=notes_for_cms_export,
        )
        persist_host_profile_override(session, project.host, payload)
        refresh_project_mapping_preview(session, request.app.state.config, project_id)
        session.commit()
        flash(request, "success", "Saved publishability controls and rebuilt the mapping preview.")
    except Exception as exc:
        flash(request, "error", f"Failed to save publishability controls: {exc}")
    return _redirect(f"/web/projects/{project_id}")


@router.post("/api/projects/{project_id}/delete")
async def delete_project_api(
    project_id: int,
    request: Request,
    session: Session = Depends(get_db),
    return_to: str | None = Form(default="/web/projects"),
):
    target = _safe_return_target(return_to)
    try:
        project = session.execute(
            select(Project)
            .where(Project.id == project_id)
            .options(selectinload(Project.machine_profile), selectinload(Project.markers), selectinload(Project.files))
        ).scalar_one()
        project_label = project.name_guess or project.path
        session.delete(project)
        session.commit()
        with request.app.state.profile_lock:
            request.app.state.profile_runs.pop(project_id, None)
            request.app.state.profile_controls.pop(project_id, None)
        flash(request, "success", f"Removed project {project_label}.")
    except Exception as exc:
        flash(request, "error", f"Failed to remove project: {exc}")
        target = f"/web/projects/{project_id}"
    return _redirect(target)


@router.get("/api/projects/{project_id}/profile/status")
@router.get("/api/projects/{project_id}/interpret/status")
async def interpret_project_status_api(project_id: int, request: Request, session: Session = Depends(get_db)):
    with request.app.state.profile_lock:
        run_state = dict(request.app.state.profile_runs.get(project_id, {}))

    latest = session.execute(
        select(MachineProfile)
        .where(MachineProfile.project_id == project_id)
        .order_by(MachineProfile.last_generated_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    identity = session.execute(
        select(MachineIdentityResult)
        .where(MachineIdentityResult.project_id == project_id)
        .options(selectinload(MachineIdentityResult.signature_matches))
        .limit(1)
    ).scalar_one_or_none()
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.parent_host),
            selectinload(Project.host).selectinload(Host.guest_inventory).selectinload(GuestInventory.linked_host),
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.enrichment_evidence),
            selectinload(Project.mapping_preview),
        )
    ).scalar_one_or_none()
    topology = build_host_topology_context(project.host) if project and project.host else {}

    status = run_state.get("status") or ("completed" if latest else "idle")
    return JSONResponse(
        {
            "status": status,
            "output": run_state.get("output", ""),
            "error": run_state.get("error"),
            "updated_at": run_state.get("updated_at"),
            "topology": topology,
            "enrichment_evidence": [
                {
                    "source_type": item.source_type,
                    "title": item.title,
                    "evidence_text": item.evidence_text,
                    "source_url": item.source_url,
                }
                for item in (project.enrichment_evidence if project else [])
            ],
            "identity_result": (
                {
                    "primary_signature_id": identity.primary_signature_id,
                    "primary_signature_name": identity.primary_signature_name,
                    "machine_kind": identity.machine_kind,
                    "detected_platform": identity.detected_platform,
                    "detected_role": identity.detected_role,
                    "stack_tags_json": identity.stack_tags_json,
                    "identity_confidence": identity.identity_confidence,
                    "identity_evidence_json": identity.identity_evidence_json,
                    "alternate_matches_json": identity.alternate_matches_json,
                    "ambiguity_notes_json": identity.ambiguity_notes_json,
                    "local_facts_evidence_json": identity.local_facts_evidence_json,
                    "topology_evidence_json": identity.topology_evidence_json,
                    "signature_matches": [
                        {
                            "signature_id": match.signature_id,
                            "display_name": match.display_name,
                            "confidence": match.confidence,
                            "detected_role": match.detected_role,
                            "detected_platform": match.detected_platform,
                            "evidence_json": match.evidence_json,
                        }
                        for match in identity.signature_matches
                    ],
                }
                if identity
                else None
            ),
            "mapping_preview": (
                {
                    "project_title_candidate": project.mapping_preview.project_title_candidate,
                    "project_summary_candidate": project.mapping_preview.project_summary_candidate,
                    "tech_tags_candidate_json": project.mapping_preview.tech_tags_candidate_json,
                    "card_title_candidate": project.mapping_preview.card_title_candidate,
                    "card_summary_candidate": project.mapping_preview.card_summary_candidate,
                    "card_tech_tags_candidate_json": project.mapping_preview.card_tech_tags_candidate_json,
                    "publish_title_candidate": project.mapping_preview.publish_title_candidate,
                    "publish_summary_candidate": project.mapping_preview.publish_summary_candidate,
                    "publish_long_description_candidate": project.mapping_preview.publish_long_description_candidate,
                    "publish_sections_json": project.mapping_preview.publish_sections_json,
                    "publishable_kind": project.mapping_preview.publishable_kind,
                    "recommended_status": project.mapping_preview.recommended_status,
                    "demo_url_candidate": project.mapping_preview.demo_url_candidate,
                    "repo_url_candidate": project.mapping_preview.repo_url_candidate,
                    "publishability_reason": project.mapping_preview.publishability_reason,
                    "internal_only_reason": project.mapping_preview.internal_only_reason,
                    "ready_for_cms": project.mapping_preview.ready_for_cms,
                    "needs_review": project.mapping_preview.needs_review,
                }
                if project and project.mapping_preview
                else None
            ),
            "host_override": (
                {
                    "project_label_override": project.host.profile_override.project_label_override,
                    "public_title_override": project.host.profile_override.public_title_override,
                    "public_summary_hint": project.host.profile_override.public_summary_hint,
                    "publishable_project": project.host.profile_override.publishable_project,
                    "internal_only": project.host.profile_override.internal_only,
                    "related_host_ids_json": project.host.profile_override.related_host_ids_json,
                    "public_tech_tags_override_json": project.host.profile_override.public_tech_tags_override_json,
                    "project_group": project.host.profile_override.project_group,
                    "notes_for_cms_export": project.host.profile_override.notes_for_cms_export,
                }
                if project and project.host and project.host.profile_override
                else None
            ),
            "latest_profile": (
                {
                    "machine_kind": latest.machine_kind,
                    "story_template_type": latest.story_template_type,
                    "detected_platform": latest.detected_platform,
                    "detected_role": latest.detected_role,
                    "lead_paragraph": latest.lead_paragraph,
                    "project_role_summary": latest.project_role_summary,
                    "confidence": latest.confidence,
                    "host_resources_json": latest.host_resources_json,
                    "project_role_json": latest.project_role_json,
                    "setup_details_json": latest.setup_details_json,
                    "contained_workloads_json": latest.contained_workloads_json,
                    "detected_components_json": latest.detected_components_json,
                    "why_it_matters_json": latest.why_it_matters_json,
                    "notes_json": latest.notes_json,
                    "unknowns_json": latest.unknowns_json,
                    "evidence_json": latest.evidence_json,
                    "llm_model_used": latest.llm_model_used,
                    "last_generated_at": latest.last_generated_at.isoformat() if latest.last_generated_at else None,
                }
                if latest
                else None
            ),
        }
    )
