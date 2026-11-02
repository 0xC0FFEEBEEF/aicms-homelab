from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GuestInventory, Host, HostFact, HostStorageFact, ServiceInventory

RowT = TypeVar("RowT")


def _classification_value(classification: Any, key: str) -> Any:
    if classification is None:
        return None
    if isinstance(classification, dict):
        return classification.get(key)
    return getattr(classification, key, None)


def _storage_key_from_item(item: dict[str, Any]) -> tuple[str, str]:
    return (
        str(item.get("storage_name") or item.get("mount_point") or "storage"),
        str(item.get("mount_point") or ""),
    )


def _storage_key_from_row(row: HostStorageFact) -> tuple[str, str]:
    return row.storage_name, row.mount_point or ""


def _guest_key_from_item(item: dict[str, Any]) -> tuple[str, str]:
    return str(item.get("guest_type") or "unknown"), str(item.get("guest_id") or "unknown")


def _guest_key_from_row(row: GuestInventory) -> tuple[str, str]:
    return row.guest_type, row.guest_id


def _service_key_from_item(item: dict[str, Any]) -> str:
    return str(item.get("service_name") or "service")


def _service_key_from_row(row: ServiceInventory) -> str:
    return row.service_name


def _dedupe_items(
    items: Iterable[dict[str, Any]],
    *,
    key_fn: Callable[[dict[str, Any]], Hashable],
) -> dict[Hashable, dict[str, Any]]:
    deduped: dict[Hashable, dict[str, Any]] = {}
    for item in items:
        deduped[key_fn(item)] = item
    return deduped


def _sync_rows(
    session: Session,
    *,
    existing_rows: Iterable[RowT],
    incoming_items: Iterable[dict[str, Any]],
    item_key_fn: Callable[[dict[str, Any]], Hashable],
    row_key_fn: Callable[[RowT], Hashable],
    create_row: Callable[[dict[str, Any]], RowT],
    update_row: Callable[[RowT, dict[str, Any]], None],
) -> None:
    existing_by_key: dict[Hashable, RowT] = {}
    duplicate_rows: list[RowT] = []
    for row in existing_rows:
        key = row_key_fn(row)
        if key in existing_by_key:
            duplicate_rows.append(row)
            continue
        existing_by_key[key] = row
    incoming_by_key = _dedupe_items(incoming_items, key_fn=item_key_fn)

    for key, item in incoming_by_key.items():
        row = existing_by_key.pop(key, None)
        if row is None:
            row = create_row(item)
            session.add(row)
        update_row(row, item)

    for row in existing_by_key.values():
        session.delete(row)
    for row in duplicate_rows:
        session.delete(row)


def sync_host_fact_inventory(
    session: Session,
    *,
    host: Host,
    host_facts: dict[str, Any],
    classification: Any,
    seen_at: datetime,
) -> None:
    if not host_facts:
        return

    fact_record = session.execute(select(HostFact).where(HostFact.host_id == host.id)).scalar_one_or_none()
    if fact_record is None:
        fact_record = HostFact(host_id=host.id)
        session.add(fact_record)

    fact_record.machine_kind = _classification_value(classification, "machine_kind")
    fact_record.detected_platform = _classification_value(classification, "detected_platform")
    fact_record.detected_role = _classification_value(classification, "detected_role")
    fact_record.story_template_type = _classification_value(classification, "story_template_type")
    fact_record.confidence = _classification_value(classification, "confidence")
    fact_record.evidence_json = _classification_value(classification, "evidence") or []
    fact_record.facts_json = host_facts
    fact_record.collected_at = seen_at

    storage_items = list((host_facts.get("storage_summary") or [])[:64])
    guest_items = list((host_facts.get("guest_inventory") or [])[:128])
    service_items = list((host_facts.get("selected_running_services") or [])[:128])

    _sync_rows(
        session,
        existing_rows=session.execute(select(HostStorageFact).where(HostStorageFact.host_id == host.id)).scalars().all(),
        incoming_items=storage_items,
        item_key_fn=_storage_key_from_item,
        row_key_fn=_storage_key_from_row,
        create_row=lambda item: HostStorageFact(
            host_id=host.id,
            storage_name=_storage_key_from_item(item)[0],
            mount_point=item.get("mount_point"),
        ),
        update_row=lambda row, item: _update_storage_row(row, item, seen_at=seen_at),
    )
    _sync_rows(
        session,
        existing_rows=session.execute(select(GuestInventory).where(GuestInventory.host_id == host.id)).scalars().all(),
        incoming_items=guest_items,
        item_key_fn=_guest_key_from_item,
        row_key_fn=_guest_key_from_row,
        create_row=lambda item: GuestInventory(
            host_id=host.id,
            guest_id=_guest_key_from_item(item)[1],
            guest_type=_guest_key_from_item(item)[0],
        ),
        update_row=lambda row, item: _update_guest_row(row, item, seen_at=seen_at),
    )
    _sync_rows(
        session,
        existing_rows=session.execute(select(ServiceInventory).where(ServiceInventory.host_id == host.id)).scalars().all(),
        incoming_items=service_items,
        item_key_fn=_service_key_from_item,
        row_key_fn=_service_key_from_row,
        create_row=lambda item: ServiceInventory(
            host_id=host.id,
            service_name=_service_key_from_item(item),
        ),
        update_row=lambda row, item: _update_service_row(row, item, seen_at=seen_at),
    )
    session.flush()


def _update_storage_row(row: HostStorageFact, item: dict[str, Any], *, seen_at: datetime) -> None:
    row.storage_name = _storage_key_from_item(item)[0]
    row.mount_point = item.get("mount_point")
    row.storage_type = item.get("storage_type")
    row.total_bytes = item.get("total_bytes")
    row.used_bytes = item.get("used_bytes")
    row.available_bytes = item.get("available_bytes")
    row.notes = item.get("notes")
    row.raw_json = item
    row.last_seen_at = seen_at


def _update_guest_row(row: GuestInventory, item: dict[str, Any], *, seen_at: datetime) -> None:
    row.guest_id = _guest_key_from_item(item)[1]
    row.guest_type = _guest_key_from_item(item)[0]
    row.name = item.get("name")
    row.status = item.get("status")
    row.cores = item.get("cores")
    row.ram_mb = item.get("ram_mb")
    row.disk_summary = item.get("disk_summary")
    row.role_guess = item.get("role_guess")
    row.ip = item.get("ip")
    row.config_path = item.get("config_path")
    row.raw_json = item.get("raw_json") or item
    row.last_seen_at = seen_at


def _update_service_row(row: ServiceInventory, item: dict[str, Any], *, seen_at: datetime) -> None:
    row.service_name = _service_key_from_item(item)
    row.status = item.get("status")
    row.description = item.get("description")
    row.role_guess = item.get("role_guess")
    row.unit_path = item.get("unit_path")
    row.raw_json = item
    row.last_seen_at = seen_at
