from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.clarifications.service import project_question_counts
from app.config import AppConfig
from app.dependencies import base_template_context, get_config_from_request, get_db, get_templates, mask_secret
from app.investigations.service import (
    investigation_draft_state_payload,
    list_investigation_sessions,
    load_investigation_session,
    reconcile_orphaned_investigation,
)
from app.lmstudio.service import latest_logs
from app.models import (
    GuestInventory,
    Host,
    LMStudioModel,
    MachineIdentityResult,
    Project,
    ProjectQuestionThread,
    Scan,
    ScanEvent,
)
from app.publish_preview.service import (
    build_investigation_working_preview,
    build_investigation_website_preview,
    build_project_website_preview,
    build_projects_website_previews,
)
from app.scanner.service import reconcile_orphaned_scan
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
    LMStudioSettingsPayload,
    MCPSettingsPayload,
    ProfileSettingsPayload,
    ScanSettingsPayload,
    WebSearchSettingsPayload,
)
from app.topology.service import build_host_topology_context
from app.updater.service import read_update_status
from app.utils.common import utcnow
from app.utils.connectivity import build_connectivity_commands
from app.utils.settings_store import get_settings


router = APIRouter()


def _templates_response(request: Request, template_name: str, session: Session, **context):
    templates = get_templates(request)
    payload = base_template_context(request, session, **context)
    return templates.TemplateResponse(request, template_name, payload)


def _project_listing_query(
    *,
    host_id: int | None,
    project_type: str | None,
    git_state: str,
    recently_changed: bool,
    q: str | None,
    sort: str,
    preview: bool = False,
):
    options = [selectinload(Project.host), selectinload(Project.markers)]
    if preview:
        options = [
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.machine_profile),
            selectinload(Project.identity_result),
            selectinload(Project.mapping_preview),
        ]
    query = select(Project).options(*options)

    if host_id:
        query = query.where(Project.host_id == host_id)
    if project_type:
        query = query.where(Project.project_type == project_type)
    if git_state == "git":
        query = query.where(Project.is_git_repo.is_(True))
    elif git_state == "non-git":
        query = query.where(Project.is_git_repo.is_(False))
    if recently_changed:
        query = query.where(Project.last_modified_at >= utcnow() - timedelta(days=7))
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(Project.path.ilike(pattern), Project.name_guess.ilike(pattern)))

    sort_options = {
        "name": Project.name_guess.asc(),
        "path": Project.path.asc(),
        "type": Project.project_type.asc(),
        "host": Project.host_id.asc(),
        "last_seen": Project.last_seen_at.desc(),
        "modified": Project.last_modified_at.desc(),
    }
    return query.order_by(sort_options.get(sort, Project.last_seen_at.desc()))


def _preview_mode_urls(
    base_url: str,
    *,
    query_params: dict[str, str] | None = None,
    modes: tuple[str, ...] = ("projects", "card", "fields"),
) -> dict[str, str]:
    resolved = dict(query_params or {})
    urls: dict[str, str] = {}
    for mode in modes:
        params = {"mode": mode, **resolved}
        urls[mode] = f"{base_url}?{urlencode(params)}"
    return urls


@router.get("/web", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    total_hosts = session.scalar(select(func.count(Host.id))) or 0
    reachable_hosts = session.scalar(select(func.count(Host.id)).where(Host.connection_status == "reachable")) or 0
    total_projects = session.scalar(select(func.count(Project.id))) or 0
    latest_scans = session.execute(select(Scan).order_by(Scan.started_at.desc()).limit(6)).scalars().all()
    recent_errors = session.execute(
        select(ScanEvent).where(ScanEvent.level == "error").order_by(ScanEvent.created_at.desc()).limit(10)
    ).scalars().all()
    lmstudio_settings = get_settings(session, request.app.state.config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    github_settings = get_settings(session, request.app.state.config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)

    return _templates_response(
        request,
        "dashboard.html",
        session,
        total_hosts=total_hosts,
        reachable_hosts=reachable_hosts,
        total_projects=total_projects,
        latest_scans=latest_scans,
        recent_errors=recent_errors,
        lmstudio_settings=lmstudio_settings,
        github_settings=github_settings,
    )


@router.get("/web/hosts", response_class=HTMLResponse)
def hosts_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    hosts = session.execute(select(Host).options(selectinload(Host.roots)).order_by(Host.name)).scalars().all()
    latest_scan_status: dict[int, str] = {}
    for host in hosts:
        latest = session.execute(
            select(Scan.status)
            .join(ScanEvent, ScanEvent.scan_id == Scan.id)
            .where(ScanEvent.host_id == host.id)
            .order_by(Scan.started_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        latest_scan_status[host.id] = latest or "Never"

    return _templates_response(
        request,
        "hosts.html",
        session,
        hosts=hosts,
        latest_scan_status=latest_scan_status,
    )


@router.get("/web/hosts/new", response_class=HTMLResponse)
def host_new_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    general_settings = get_settings(session, request.app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    return _templates_response(
        request,
        "host_new.html",
        session,
        default_ssh_key_path=general_settings.default_scanner_ssh_key_path,
    )


@router.get("/web/hosts/{host_id}", response_class=HTMLResponse)
def host_detail_page(host_id: int, request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    general_settings = get_settings(session, request.app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    host = session.execute(
        select(Host)
        .where(Host.id == host_id)
        .options(
            selectinload(Host.roots),
            selectinload(Host.projects),
            selectinload(Host.scan_events),
            selectinload(Host.fact_snapshot),
            selectinload(Host.parent_host),
            selectinload(Host.guest_inventory).selectinload(GuestInventory.linked_host),
            selectinload(Host.profile_override),
        )
    ).scalar_one()
    recent_scans = session.execute(
        select(Scan)
        .join(ScanEvent, ScanEvent.scan_id == Scan.id)
        .where(ScanEvent.host_id == host_id)
        .order_by(Scan.started_at.desc())
        .limit(10)
    ).scalars().all()
    host_events = session.execute(
        select(ScanEvent)
        .where(ScanEvent.host_id == host_id)
        .order_by(ScanEvent.created_at.desc())
        .limit(20)
    ).scalars().all()
    topology_context = build_host_topology_context(host)
    linkable_hosts = session.execute(select(Host).where(Host.id != host.id).order_by(Host.name)).scalars().all()

    return _templates_response(
        request,
        "host_detail.html",
        session,
        host=host,
        recent_scans=recent_scans,
        host_events=host_events,
        topology_context=topology_context,
        linkable_hosts=linkable_hosts,
        default_ssh_key_path=general_settings.default_scanner_ssh_key_path,
    )


@router.get("/web/investigations", response_class=HTMLResponse)
def investigations_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    investigations = list_investigation_sessions(session)
    return _templates_response(
        request,
        "investigations.html",
        session,
        investigations=investigations,
    )


@router.get("/web/investigations/new", response_class=HTMLResponse)
def investigation_new_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    general_settings = get_settings(session, request.app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    hosts = session.execute(select(Host).options(selectinload(Host.roots)).order_by(Host.name)).scalars().all()
    guests = session.execute(
        select(GuestInventory)
        .options(selectinload(GuestInventory.host), selectinload(GuestInventory.linked_host))
        .order_by(GuestInventory.updated_at.desc())
    ).scalars().all()
    return _templates_response(
        request,
        "investigation_new.html",
        session,
        hosts=hosts,
        guests=guests,
        default_ssh_key_path=general_settings.default_scanner_ssh_key_path,
    )


@router.get("/web/investigations/{investigation_id}", response_class=HTMLResponse)
def investigation_detail_page(investigation_id: int, request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    investigation = load_investigation_session(session, investigation_id)
    if reconcile_orphaned_investigation(request.app, session, investigation):
        session.commit()
        investigation = load_investigation_session(session, investigation_id)
    draft_state = investigation_draft_state_payload(investigation)
    working_preview = build_investigation_working_preview(session, investigation)
    return _templates_response(
        request,
        "investigation_detail.html",
        session,
        investigation=investigation,
        draft_state=draft_state,
        working_preview=working_preview,
    )


@router.get("/web/investigations/{investigation_id}/preview", response_class=HTMLResponse)
def investigation_website_preview_page(
    investigation_id: int,
    request: Request,
    session: Session = Depends(get_db),
    mode: str = Query(default="projects"),
) -> HTMLResponse:
    preview_mode = "detail" if mode == "full" else mode
    if preview_mode not in {"projects", "card", "detail", "fields"}:
        preview_mode = "projects"
    investigation = load_investigation_session(session, investigation_id)
    preview = build_investigation_website_preview(session, investigation)
    preview_mode_urls = _preview_mode_urls(
        f"/web/investigations/{investigation.id}/preview",
        modes=("projects", "card", "detail", "fields"),
    )
    templates = get_templates(request)
    payload = base_template_context(
        request,
        session,
        project=investigation.linked_project,
        preview=preview,
        preview_items=[preview],
        preview_mode=preview_mode,
        preview_base_url=f"/web/investigations/{investigation.id}/preview",
        preview_mode_urls=preview_mode_urls,
        back_url=f"/web/investigations/{investigation.id}",
    )
    return templates.TemplateResponse(request, "public_preview/project_preview.html", payload)


@router.get("/web/projects", response_class=HTMLResponse)
def projects_page(
    request: Request,
    session: Session = Depends(get_db),
    host_id: int | None = Query(default=None),
    project_type: str | None = Query(default=None),
    git_state: str = Query(default="all"),
    recently_changed: bool = Query(default=False),
    q: str | None = Query(default=None),
    sort: str = Query(default="last_seen"),
) -> HTMLResponse:
    query = _project_listing_query(
        host_id=host_id,
        project_type=project_type,
        git_state=git_state,
        recently_changed=recently_changed,
        q=q,
        sort=sort,
    )
    projects = session.execute(query).scalars().all()
    hosts = session.execute(select(Host).order_by(Host.name)).scalars().all()
    project_types = sorted({project.project_type for project in projects if project.project_type})
    preview_query_params: dict[str, str] = {}
    if host_id:
        preview_query_params["host_id"] = str(host_id)
    if project_type:
        preview_query_params["project_type"] = project_type
    if git_state != "all":
        preview_query_params["git_state"] = git_state
    if recently_changed:
        preview_query_params["recently_changed"] = "1"
    if q:
        preview_query_params["q"] = q
    if sort != "last_seen":
        preview_query_params["sort"] = sort
    projects_preview_url = _preview_mode_urls("/web/projects/preview", query_params=preview_query_params)["projects"]

    return _templates_response(
        request,
        "projects.html",
        session,
        projects=projects,
        hosts=hosts,
        project_types=project_types,
        filters={"host_id": host_id, "project_type": project_type, "git_state": git_state, "recently_changed": recently_changed, "q": q or "", "sort": sort},
        projects_preview_url=projects_preview_url,
    )


@router.get("/web/projects/preview", response_class=HTMLResponse)
def projects_website_preview_page(
    request: Request,
    session: Session = Depends(get_db),
    host_id: int | None = Query(default=None),
    project_type: str | None = Query(default=None),
    git_state: str = Query(default="all"),
    recently_changed: bool = Query(default=False),
    q: str | None = Query(default=None),
    sort: str = Query(default="last_seen"),
    mode: str = Query(default="projects"),
) -> HTMLResponse:
    preview_mode = mode if mode in {"projects", "card", "fields"} else "projects"
    query = _project_listing_query(
        host_id=host_id,
        project_type=project_type,
        git_state=git_state,
        recently_changed=recently_changed,
        q=q,
        sort=sort,
        preview=True,
    )
    projects = session.execute(query).scalars().all()
    preview_items = build_projects_website_previews(session, projects)
    query_params: dict[str, str] = {}
    if host_id:
        query_params["host_id"] = str(host_id)
    if project_type:
        query_params["project_type"] = project_type
    if git_state != "all":
        query_params["git_state"] = git_state
    if recently_changed:
        query_params["recently_changed"] = "1"
    if q:
        query_params["q"] = q
    if sort != "last_seen":
        query_params["sort"] = sort
    preview_mode_urls = _preview_mode_urls("/web/projects/preview", query_params=query_params, modes=("projects", "card", "fields"))
    templates = get_templates(request)
    payload = base_template_context(
        request,
        session,
        project=None,
        preview=None,
        preview_items=preview_items,
        preview_mode=preview_mode,
        preview_base_url="/web/projects/preview",
        preview_mode_urls=preview_mode_urls,
        back_url="/web/projects",
    )
    return templates.TemplateResponse(request, "public_preview/project_preview.html", payload)


@router.get("/web/projects/{project_id}", response_class=HTMLResponse)
def project_detail_page(project_id: int, request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.parent_host),
            selectinload(Project.host).selectinload(Host.fact_snapshot),
            selectinload(Project.host).selectinload(Host.guest_inventory).selectinload(GuestInventory.linked_host),
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.markers),
            selectinload(Project.machine_profile),
            selectinload(Project.identity_result).selectinload(MachineIdentityResult.signature_matches),
            selectinload(Project.mapping_preview),
            selectinload(Project.files),
            selectinload(Project.enrichment_evidence),
            selectinload(Project.enrichment_runs),
            selectinload(Project.clarification_threads).selectinload(ProjectQuestionThread.messages),
            selectinload(Project.clarification_guidance),
        )
    ).scalar_one()
    lmstudio_settings = get_settings(session, request.app.state.config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    profile_settings = get_settings(session, request.app.state.config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    mcp_settings = get_settings(session, request.app.state.config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    web_search_settings = get_settings(session, request.app.state.config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    with request.app.state.profile_lock:
        profile_run = dict(request.app.state.profile_runs.get(project_id, {}))
    topology_context = build_host_topology_context(project.host) if project.host else {}
    enrichment_enabled = bool(
        mcp_settings.enabled or (web_search_settings.enabled and web_search_settings.engine != "disabled")
    )

    return _templates_response(
        request,
        "project_detail.html",
        session,
        project=project,
        latest_profile=project.machine_profile,
        identity_result=project.identity_result,
        mapping_preview=project.mapping_preview,
        host_override=project.host.profile_override if project.host else None,
        lmstudio_settings=lmstudio_settings,
        profile_settings=profile_settings,
        profile_run=profile_run,
        topology_context=topology_context,
        enrichment_enabled=enrichment_enabled,
        project_enrichment_evidence=project.enrichment_evidence,
        clarification_counts=project_question_counts(project),
        mcp_settings=mcp_settings,
        web_search_settings=web_search_settings,
        all_hosts=session.execute(select(Host).order_by(Host.name)).scalars().all(),
    )


@router.get("/web/projects/{project_id}/preview", response_class=HTMLResponse)
def project_website_preview_page(
    project_id: int,
    request: Request,
    session: Session = Depends(get_db),
    mode: str = Query(default="projects"),
) -> HTMLResponse:
    preview_mode = "detail" if mode == "full" else mode
    if preview_mode not in {"projects", "card", "detail", "fields"}:
        preview_mode = "projects"
    project = session.execute(
        select(Project)
        .where(Project.id == project_id)
        .options(
            selectinload(Project.host).selectinload(Host.profile_override),
            selectinload(Project.machine_profile),
            selectinload(Project.identity_result),
            selectinload(Project.mapping_preview),
        )
    ).scalar_one()
    preview = build_project_website_preview(session, project)
    preview_mode_urls = _preview_mode_urls(
        f"/web/projects/{project.id}/preview",
        modes=("projects", "card", "detail", "fields"),
    )
    templates = get_templates(request)
    payload = base_template_context(
        request,
        session,
        project=project,
        preview=preview,
        preview_items=[preview],
        preview_mode=preview_mode,
        preview_base_url=f"/web/projects/{project.id}/preview",
        preview_mode_urls=preview_mode_urls,
        back_url=f"/web/projects/{project.id}",
    )
    return templates.TemplateResponse(request, "public_preview/project_preview.html", payload)


@router.get("/web/scans", response_class=HTMLResponse)
def scans_page(
    request: Request,
    session: Session = Depends(get_db),
    scan_id: int | None = Query(default=None),
) -> HTMLResponse:
    scans = session.execute(select(Scan).order_by(Scan.started_at.desc()).limit(50)).scalars().all()
    repaired = False
    for scan in scans:
        repaired = reconcile_orphaned_scan(request.app, session, scan) or repaired
    if repaired:
        session.commit()
        scans = session.execute(select(Scan).order_by(Scan.started_at.desc()).limit(50)).scalars().all()
    selected_scan = session.get(Scan, scan_id) if scan_id else (scans[0] if scans else None)
    selected_events = []
    if selected_scan:
        selected_events = session.execute(
            select(ScanEvent).where(ScanEvent.scan_id == selected_scan.id).order_by(ScanEvent.created_at.desc())
        ).scalars().all()

    return _templates_response(
        request,
        "scans.html",
        session,
        scans=scans,
        selected_scan=selected_scan,
        selected_events=selected_events,
    )


@router.get("/web/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    session: Session = Depends(get_db),
    tab: str = Query(default="general"),
    host_id: int | None = Query(default=None),
) -> HTMLResponse:
    config: AppConfig = get_config_from_request(request)
    general_settings = get_settings(session, config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    scan_settings = get_settings(session, config, SCAN_SETTINGS_KEY, ScanSettingsPayload)
    lmstudio_settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    github_settings = get_settings(session, config, GITHUB_SETTINGS_KEY, GitHubSettingsPayload)
    profile_settings = get_settings(session, config, PROFILE_SETTINGS_KEY, ProfileSettingsPayload)
    mcp_settings = get_settings(session, config, MCP_SETTINGS_KEY, MCPSettingsPayload)
    web_search_settings = get_settings(session, config, WEB_SEARCH_SETTINGS_KEY, WebSearchSettingsPayload)
    updater_status = read_update_status(config)
    github_token_masked = mask_secret(github_settings.pat)
    lmstudio_models = session.execute(select(LMStudioModel).order_by(LMStudioModel.model_id.asc())).scalars().all()
    hosts = session.execute(select(Host).options(selectinload(Host.roots)).order_by(Host.name)).scalars().all()
    selected_host = next((host for host in hosts if host.id == host_id), None)
    connectivity_commands = build_connectivity_commands(
        selected_host,
        default_key_path=general_settings.default_scanner_ssh_key_path,
    )

    return _templates_response(
        request,
        "settings.html",
        session,
        tab=tab,
        general_settings=general_settings,
        scan_settings=scan_settings,
        lmstudio_settings=lmstudio_settings,
        lmstudio_models=lmstudio_models,
        github_settings=github_settings,
        profile_settings=profile_settings,
        mcp_settings=mcp_settings,
        web_search_settings=web_search_settings,
        updater_status=updater_status,
        github_token_masked=github_token_masked,
        hosts=hosts,
        selected_host=selected_host,
        connectivity_commands=connectivity_commands,
    )


@router.get("/web/guests/{guest_id}/enroll", response_class=HTMLResponse)
def guest_enroll_page(guest_id: int, request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    general_settings = get_settings(session, request.app.state.config, GENERAL_SETTINGS_KEY, GeneralSettingsPayload)
    guest = session.execute(
        select(GuestInventory)
        .where(GuestInventory.id == guest_id)
        .options(
            selectinload(GuestInventory.host),
            selectinload(GuestInventory.linked_host),
        )
    ).scalar_one()
    existing_hosts = session.execute(select(Host).order_by(Host.name)).scalars().all()
    return _templates_response(
        request,
        "guest_enroll.html",
        session,
        guest=guest,
        existing_hosts=existing_hosts,
        default_ssh_key_path=general_settings.default_scanner_ssh_key_path,
        suggested_name=guest.name or f"{guest.host.name}-{guest.guest_type}-{guest.guest_id}",
        suggested_roots_text="",
    )


@router.get("/web/lmstudio", response_class=HTMLResponse)
def lmstudio_page(request: Request, session: Session = Depends(get_db)) -> HTMLResponse:
    config = get_config_from_request(request)
    lmstudio_settings = get_settings(session, config, LMSTUDIO_SETTINGS_KEY, LMStudioSettingsPayload)
    models = session.execute(select(LMStudioModel).order_by(LMStudioModel.model_id.asc())).scalars().all()
    logs = latest_logs(session)
    return _templates_response(
        request,
        "lmstudio.html",
        session,
        lmstudio_settings=lmstudio_settings,
        models=models,
        logs=logs,
    )
