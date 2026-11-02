from __future__ import annotations

import re
import shlex
from collections import Counter
from typing import Any, Callable

from app.models import Host

RunCommand = Callable[..., tuple[int, str, str]]
RunLines = Callable[..., tuple[list[str], str | None]]


def _safe_int(value: str | None) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _bytes_to_gib(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / (1024**3), 2)


def _parse_key_value_lines(lines: list[str]) -> dict[str, str]:
    payload: dict[str, str] = {}
    for line in lines:
        key, _, value = line.partition("=")
        key = key.strip().lower()
        if key:
            payload[key] = value.strip()
    return payload


def _guess_service_role(service_name: str) -> str | None:
    lowered = service_name.lower()
    mapping = {
        "mcp": "tool-access gateway",
        "cloudflared": "tunnel endpoint",
        "docker": "container runtime",
        "podman": "container runtime",
        "tailscale": "mesh networking agent",
        "nginx": "reverse proxy or web server",
        "traefik": "reverse proxy",
        "caddy": "reverse proxy",
        "postgres": "database service",
        "mysql": "database service",
        "redis": "cache or queue service",
        "ssh": "remote access service",
    }
    for token, role in mapping.items():
        if token in lowered:
            return role
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
    }
    for token, role in mapping.items():
        if token in lowered:
            return role
    return None


def _parse_df_lines(lines: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        filesystem, size, used, available, _use_percent, mountpoint = parts[:6]
        rows.append(
            {
                "storage_name": filesystem,
                "mount_point": mountpoint,
                "storage_type": "filesystem",
                "total_bytes": _safe_int(size),
                "used_bytes": _safe_int(used),
                "available_bytes": _safe_int(available),
                "notes": None,
            }
        )
    return rows


def _parse_service_lines(lines: list[str], *, max_services: int) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    for line in lines[:max_services]:
        parts = line.split(None, 4)
        if not parts:
            continue
        service_name = parts[0]
        description = parts[4] if len(parts) >= 5 else ""
        services.append(
            {
                "service_name": service_name,
                "status": parts[3] if len(parts) >= 4 else "running",
                "description": description,
                "role_guess": _guess_service_role(service_name),
                "unit_path": None,
            }
        )
    return services


def _parse_pvesm_status(lines: list[str]) -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6:
            continue
        name, storage_type, _status = parts[:3]
        total = _safe_int(parts[3])
        used = _safe_int(parts[4])
        available = _safe_int(parts[5])
        pools.append(
            {
                "storage_name": name,
                "mount_point": None,
                "storage_type": storage_type,
                "total_bytes": total * 1024 if total is not None else None,
                "used_bytes": used * 1024 if used is not None else None,
                "available_bytes": available * 1024 if available is not None else None,
                "notes": "Collected from pvesm status.",
            }
        )
    return pools


def _parse_qm_list(lines: list[str]) -> list[dict[str, str]]:
    guests: list[dict[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        guests.append(
            {
                "guest_id": parts[0],
                "name": parts[1],
                "status": parts[2] if len(parts) >= 3 else None,
            }
        )
    return guests


def _parse_pct_list(lines: list[str]) -> list[dict[str, str]]:
    guests: list[dict[str, str]] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        guests.append(
            {
                "guest_id": parts[0],
                "status": parts[1] if len(parts) >= 2 else None,
                "name": parts[2] if len(parts) >= 3 else None,
            }
        )
    return guests


def _parse_proxmox_config(lines: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in lines:
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


def _collect_proxmox_guests(run_lines: RunLines, *, max_guests: int) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    pveversion_lines, _ = run_lines(
        "bash -lc " + shlex.quote("pveversion 2>/dev/null | head -n1"),
        phase="proxmox-version",
        allow_partial=True,
    )
    proxmox_version = pveversion_lines[0].strip() if pveversion_lines else None

    qemu_rows, _ = run_lines(
        "bash -lc " + shlex.quote("qm list 2>/dev/null | tail -n +2"),
        phase="proxmox-qm-list",
        allow_partial=True,
    )
    lxc_rows, _ = run_lines(
        "bash -lc " + shlex.quote("pct list 2>/dev/null | tail -n +2"),
        phase="proxmox-pct-list",
        allow_partial=True,
    )

    guests: list[dict[str, Any]] = []
    role_counter: Counter[str] = Counter()

    for row in _parse_qm_list(qemu_rows)[:max_guests]:
        config_lines, _ = run_lines(
            "bash -lc " + shlex.quote(f"qm config {row['guest_id']} 2>/dev/null"),
            phase="proxmox-qm-config",
            allow_partial=True,
        )
        config = _parse_proxmox_config(config_lines)
        role_guess = _guess_guest_role(row.get("name") or "", "\n".join(config_lines))
        if role_guess:
            role_counter[role_guess] += 1
        guests.append(
            {
                "guest_id": row["guest_id"],
                "guest_type": "qemu",
                "name": config.get("name") or row.get("name"),
                "status": row.get("status"),
                "cores": _safe_int(config.get("cores")),
                "ram_mb": _safe_int(config.get("memory")),
                "disk_summary": _summarize_guest_disk(config),
                "role_guess": role_guess,
                "ip": _extract_guest_ip(config),
                "config_path": f"/etc/pve/qemu-server/{row['guest_id']}.conf",
                "raw_json": config,
            }
        )

    for row in _parse_pct_list(lxc_rows)[:max_guests]:
        config_lines, _ = run_lines(
            "bash -lc " + shlex.quote(f"pct config {row['guest_id']} 2>/dev/null"),
            phase="proxmox-pct-config",
            allow_partial=True,
        )
        config = _parse_proxmox_config(config_lines)
        role_guess = _guess_guest_role(row.get("name") or "", "\n".join(config_lines))
        if role_guess:
            role_counter[role_guess] += 1
        guests.append(
            {
                "guest_id": row["guest_id"],
                "guest_type": "lxc",
                "name": config.get("hostname") or row.get("name"),
                "status": row.get("status"),
                "cores": _safe_int(config.get("cores")),
                "ram_mb": _safe_int(config.get("memory")),
                "disk_summary": _summarize_guest_disk(config),
                "role_guess": role_guess,
                "ip": _extract_guest_ip(config),
                "config_path": f"/etc/pve/lxc/{row['guest_id']}.conf",
                "raw_json": config,
            }
        )

    storage_rows, _ = run_lines(
        "bash -lc " + shlex.quote("pvesm status 2>/dev/null | tail -n +2"),
        phase="proxmox-storage",
        allow_partial=True,
    )
    storage_pools = _parse_pvesm_status(storage_rows)
    return proxmox_version, guests, storage_pools


def collect_machine_facts(
    *,
    host: Host,
    run_lines: RunLines,
    run_command: RunCommand,
    max_services: int,
    max_guests: int,
) -> dict[str, Any]:
    fact_lines, _ = run_lines(
        "bash -lc "
        + shlex.quote(
            "set +e; "
            "echo HOSTNAME=$(hostname 2>/dev/null); "
            "echo FQDN=$(hostname -f 2>/dev/null); "
            "echo KERNEL=$(uname -r 2>/dev/null); "
            "echo ARCH=$(uname -m 2>/dev/null); "
            "if [ -r /etc/os-release ]; then "
            ". /etc/os-release; "
            "echo OS_PRETTY_NAME=${PRETTY_NAME:-}; "
            "echo OS_ID=${ID:-}; "
            "echo OS_VERSION_ID=${VERSION_ID:-}; "
            "fi; "
            "echo CPU_CORES=$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null); "
            "echo CPU_THREADS=$(lscpu 2>/dev/null | awk -F: '/^CPU\\(s\\):/ {gsub(/ /, \"\", $2); print $2; exit}'); "
            "echo THREADS_PER_CORE=$(lscpu 2>/dev/null | awk -F: '/Thread\\(s\\) per core:/ {gsub(/ /, \"\", $2); print $2; exit}'); "
            "echo MEMORY_BYTES=$(awk '/MemTotal:/ {printf \"%d\", $2 * 1024; exit}' /proc/meminfo 2>/dev/null); "
            "echo SWAP_BYTES=$(awk '/SwapTotal:/ {printf \"%d\", $2 * 1024; exit}' /proc/meminfo 2>/dev/null); "
            "echo ROOT_SIZE_BYTES=$(df -B1 --output=size / 2>/dev/null | tail -n1 | tr -d ' '); "
            "echo ROOT_USED_BYTES=$(df -B1 --output=used / 2>/dev/null | tail -n1 | tr -d ' '); "
            "echo UPTIME_SECONDS=$(cut -d. -f1 /proc/uptime 2>/dev/null); "
            "echo VIRTUALIZATION=$(systemd-detect-virt 2>/dev/null || true); "
            "echo IP_ADDRESSES=$(hostname -I 2>/dev/null | xargs);"
        ),
        phase="host-facts",
        allow_partial=True,
    )
    raw = _parse_key_value_lines(fact_lines)

    filesystem_lines, _ = run_lines(
        "bash -lc " + shlex.quote("df -P -B1 -x tmpfs -x devtmpfs 2>/dev/null | tail -n +2"),
        phase="filesystem-summary",
        allow_partial=True,
    )
    service_lines, _ = run_lines(
        "bash -lc " + shlex.quote("systemctl list-units --type=service --state=running --no-pager --no-legend 2>/dev/null | head -n 120"),
        phase="service-summary",
        allow_partial=True,
    )
    process_lines, _ = run_lines(
        "bash -lc " + shlex.quote("ps -eo comm= 2>/dev/null | sort -u | head -n 200"),
        phase="process-summary",
        allow_partial=True,
    )

    proxmox_version, guest_inventory, storage_pools = _collect_proxmox_guests(
        run_lines,
        max_guests=max_guests,
    )
    filesystems = _parse_df_lines(filesystem_lines)
    services = _parse_service_lines(service_lines, max_services=max_services)
    storage_summary = storage_pools or filesystems

    hostname = raw.get("hostname") or host.name
    ip_addresses = [item for item in (raw.get("ip_addresses") or "").split() if item]
    cpu_cores = _safe_int(raw.get("cpu_cores"))
    cpu_threads = _safe_int(raw.get("cpu_threads")) or cpu_cores
    memory_bytes = _safe_int(raw.get("memory_bytes"))
    swap_bytes = _safe_int(raw.get("swap_bytes"))
    root_size_bytes = _safe_int(raw.get("root_size_bytes"))
    root_used_bytes = _safe_int(raw.get("root_used_bytes"))

    facts: dict[str, Any] = {
        "hostname": hostname,
        "host_name": host.name,
        "address": host.address,
        "fqdn": raw.get("fqdn") or None,
        "kernel": raw.get("kernel") or None,
        "arch": raw.get("arch") or None,
        "os_pretty_name": raw.get("os_pretty_name") or None,
        "os_id": raw.get("os_id") or None,
        "os_version_id": raw.get("os_version_id") or None,
        "cpu_cores": cpu_cores,
        "cpu_threads": cpu_threads,
        "threads_per_core": _safe_int(raw.get("threads_per_core")),
        "memory_bytes": memory_bytes,
        "memory_gib": _bytes_to_gib(memory_bytes),
        "swap_bytes": swap_bytes,
        "swap_gib": _bytes_to_gib(swap_bytes),
        "root_size_bytes": root_size_bytes,
        "root_size_gib": _bytes_to_gib(root_size_bytes),
        "root_used_bytes": root_used_bytes,
        "root_used_gib": _bytes_to_gib(root_used_bytes),
        "uptime_seconds": _safe_int(raw.get("uptime_seconds")),
        "virtualization": raw.get("virtualization") or None,
        "ip_addresses": ip_addresses,
        "management_ip": ip_addresses[0] if ip_addresses else host.address,
        "platform": "Proxmox VE" if proxmox_version else (raw.get("os_pretty_name") or "Linux"),
        "proxmox_version": proxmox_version,
        "mounted_filesystems": filesystems,
        "selected_running_services": services,
        "selected_process_names": [line.strip() for line in process_lines if line.strip()],
        "storage_summary": storage_summary,
        "guest_inventory": guest_inventory,
    }
    return facts
