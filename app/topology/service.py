from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import GuestInventory, Host, Project
from app.utils.common import utcnow


def _safe_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_proxmox_config_text(content: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in content.splitlines():
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            parsed[key] = value.strip()
    return parsed


def _summarize_guest_disk(config: dict[str, str]) -> str | None:
    disk_fragments: list[str] = []
    for key, value in config.items():
        if key.startswith(("scsi", "sata", "virtio", "ide", "rootfs", "mp")):
            size_match = re.search(r"size=([^,]+)", value)
            if size_match:
                disk_fragments.append(f"{key}:{size_match.group(1)}")
            else:
                disk_fragments.append(f"{key}:{value.split(',')[0]}")
    if not disk_fragments:
        return None
    return ", ".join(disk_fragments[:4])


def _extract_guest_ip(config: dict[str, str]) -> str | None:
    for value in config.values():
        ip_match = re.search(r"\bip=([^,]+)", value)
        if ip_match:
            ip = ip_match.group(1)
            if ip and ip.lower() != "dhcp":
                return ip
    return None


def _guess_guest_role(name: str, config_text: str) -> str | None:
    lowered = f"{name}\n{config_text}".lower()
    mapping = {
        "mcp": "AI control or gateway workload",
        "golem": "Golem project workload",
        "provider": "provider node",
        "cloud": "networking or edge service",
        "db": "database workload",
        "postgres": "database workload",
        "mysql": "database workload",
        "redis": "cache or queue workload",
        "media": "media workload",
        "plex": "media workload",
        "jellyfin": "media workload",
        "git": "code or SCM workload",
        "backup": "backup or maintenance workload",
        "vpn": "network gateway workload",
        "proxy": "proxy workload",
        "web": "web workload",
        "ai": "AI or model workload",
    }
    for token, role in mapping.items():
        if token in lowered:
            return role
    return None


def _guest_sort_key(item: dict[str, Any]) -> tuple[str, int | str]:
    guest_id = str(item.get("guest_id") or "")
    return item.get("guest_type") or "unknown", int(guest_id) if guest_id.isdigit() else guest_id


def build_synthetic_guest_inventory_from_project(project: Project, *, max_items: int | None = None) -> list[dict[str, Any]]:
    metadata = project.metadata_json or {}
    proxmox_inventory = metadata.get("proxmox_inventory") or {}
    guests: dict[tuple[str, str], dict[str, Any]] = {}

    for file_record in project.files:
        if file_record.entry_type != "file" or not file_record.path.endswith(".conf"):
            continue
        guest_type: str | None = None
        if "/qemu-server/" in file_record.path:
            guest_type = "qemu"
        elif "/lxc/" in file_record.path:
            guest_type = "lxc"
        if guest_type is None:
            continue

        guest_id = PurePosixPath(file_record.path).stem
        if not guest_id:
            continue

        content = file_record.content_sample or ""
        config = _parse_proxmox_config_text(content)
        name = config.get("name") if guest_type == "qemu" else config.get("hostname")
        guests[(guest_type, guest_id)] = {
            "guest_id": guest_id,
            "guest_type": guest_type,
            "name": name,
            "status": None,
            "cores": _safe_int(config.get("cores")),
            "ram_mb": _safe_int(config.get("memory")),
            "disk_summary": _summarize_guest_disk(config),
            "role_guess": _guess_guest_role(name or guest_id, content),
            "ip": _extract_guest_ip(config),
            "config_path": file_record.path,
        }

    for guest_id in proxmox_inventory.get("qemu_vm_ids") or []:
        guests.setdefault(
            ("qemu", str(guest_id)),
            {
                "guest_id": str(guest_id),
                "guest_type": "qemu",
                "name": None,
                "status": None,
                "cores": None,
                "ram_mb": None,
                "disk_summary": None,
                "role_guess": None,
                "ip": None,
                "config_path": f"/etc/pve/qemu-server/{guest_id}.conf",
            },
        )
    for guest_id in proxmox_inventory.get("lxc_container_ids") or []:
        guests.setdefault(
            ("lxc", str(guest_id)),
            {
                "guest_id": str(guest_id),
                "guest_type": "lxc",
                "name": None,
                "status": None,
                "cores": None,
                "ram_mb": None,
                "disk_summary": None,
                "role_guess": None,
                "ip": None,
                "config_path": f"/etc/pve/lxc/{guest_id}.conf",
            },
        )

    values = sorted(guests.values(), key=_guest_sort_key)
    if max_items is not None:
        return values[:max_items]
    return values


def _merge_guest_payloads(*payload_sets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for payload_set in payload_sets:
        for item in payload_set:
            key = (str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown"))
            record = merged.setdefault(key, {"guest_type": key[0], "guest_id": key[1]})
            for field, value in item.items():
                if value is not None and value != "" and value != []:
                    record[field] = value
    return sorted(merged.values(), key=_guest_sort_key)


def _host_summary_project(host: Host) -> Project | None:
    for project in host.projects:
        if (project.metadata_json or {}).get("setup_kind") == "host-summary":
            return project
    return None


def refresh_host_topology(session: Session, host_id: int) -> dict[str, int]:
    host = session.execute(
        select(Host)
        .where(Host.id == host_id)
        .options(
            selectinload(Host.projects).selectinload(Project.files),
            selectinload(Host.guest_inventory).selectinload(GuestInventory.linked_host),
            selectinload(Host.parent_host),
        )
    ).scalar_one()

    now = utcnow()
    updated = 0
    discovered_added = 0

    host_summary_project = _host_summary_project(host)
    if host_summary_project is not None:
        synthetic_inventory = build_synthetic_guest_inventory_from_project(host_summary_project)
        existing_by_key = {(item.guest_type, item.guest_id): item for item in host.guest_inventory}
        for guest in _merge_guest_payloads(
            [
                {
                    "guest_id": item.guest_id,
                    "guest_type": item.guest_type,
                    "name": item.name,
                    "status": item.status,
                    "cores": item.cores,
                    "ram_mb": item.ram_mb,
                    "disk_summary": item.disk_summary,
                    "role_guess": item.role_guess,
                    "ip": item.ip,
                    "config_path": item.config_path,
                }
                for item in host.guest_inventory
            ],
            synthetic_inventory,
        ):
            row = existing_by_key.get((guest["guest_type"], guest["guest_id"]))
            if row is None:
                row = GuestInventory(
                    host_id=host.id,
                    guest_id=guest["guest_id"],
                    guest_type=guest["guest_type"],
                    management_state="discovered",
                    relationship_source="proxmox_discovery",
                    relationship_confidence=1.0,
                    last_relationship_verified_at=now,
                )
                session.add(row)
                discovered_added += 1
            row.name = guest.get("name") or row.name
            row.status = guest.get("status") or row.status
            row.cores = guest.get("cores") if guest.get("cores") is not None else row.cores
            row.ram_mb = guest.get("ram_mb") if guest.get("ram_mb") is not None else row.ram_mb
            row.disk_summary = guest.get("disk_summary") or row.disk_summary
            row.role_guess = guest.get("role_guess") or row.role_guess
            row.ip = guest.get("ip") or row.ip
            row.config_path = guest.get("config_path") or row.config_path
            row.relationship_source = row.relationship_source or "proxmox_discovery"
            row.relationship_confidence = row.relationship_confidence or 1.0
            row.last_relationship_verified_at = now
            row.last_seen_at = now
            if not row.management_state:
                row.management_state = "discovered"
            updated += 1

    if host.parent_host_id and host.parent_host is not None:
        host.parent_name = host.parent_host.name
        host.parent_platform = host.parent_platform or (host.parent_host.fact_snapshot.detected_platform if host.parent_host.fact_snapshot else None)
        host.last_relationship_verified_at = now
        updated += 1

    session.flush()
    return {"updated_guests": updated, "new_guests": discovered_added}


def link_discovered_guest_to_host(
    session: Session,
    guest: GuestInventory,
    child_host: Host,
    *,
    source: str,
    confidence: float = 1.0,
) -> None:
    now = utcnow()
    guest.linked_host_id = child_host.id
    guest.management_state = "enrolled" if source in {"bootstrap", "proxmox_discovery"} else "linked"
    guest.relationship_source = source
    guest.relationship_confidence = confidence
    guest.last_relationship_verified_at = now

    child_host.parent_host_id = guest.host_id
    child_host.parent_platform = guest.host.fact_snapshot.detected_platform if guest.host.fact_snapshot else guest.host.parent_platform
    child_host.parent_name = guest.host.name
    child_host.guest_type = guest.guest_type
    child_host.guest_id = guest.guest_id
    child_host.relationship_source = source
    child_host.relationship_confidence = confidence
    child_host.last_relationship_verified_at = now
    session.flush()


def mark_discovered_guest_unmanaged(session: Session, guest: GuestInventory) -> None:
    guest.management_state = "unmanaged"
    guest.last_relationship_verified_at = utcnow()
    session.flush()


def build_host_topology_context(host: Host) -> dict[str, Any]:
    parent = None
    if host.parent_host_id:
        parent = {
            "host_id": host.parent_host_id,
            "name": host.parent_host.name if host.parent_host else (host.parent_name or "Unknown"),
            "platform": host.parent_platform,
            "guest_type": host.guest_type,
            "guest_id": host.guest_id,
            "source": host.relationship_source,
            "confidence": host.relationship_confidence,
        }

    guest_rows = sorted(host.guest_inventory, key=lambda item: (item.guest_type, item.guest_id))
    enrolled_guests = [guest for guest in guest_rows if guest.linked_host_id and guest.management_state != "unmanaged"]
    discovered_guests = [guest for guest in guest_rows if not guest.linked_host_id and guest.management_state != "unmanaged"]
    unmanaged_guests = [guest for guest in guest_rows if guest.management_state == "unmanaged"]

    return {
        "parent": parent,
        "enrolled_guests": enrolled_guests,
        "discovered_guests": discovered_guests,
        "unmanaged_guests": unmanaged_guests,
        "guest_counts": {
            "enrolled": len(enrolled_guests),
            "discovered": len(discovered_guests),
            "unmanaged": len(unmanaged_guests),
        },
    }
