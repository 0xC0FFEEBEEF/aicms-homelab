from __future__ import annotations

from typing import Any


def summarize_changes(old_payload: dict[str, Any] | None, new_payload: dict[str, Any]) -> list[str]:
    if not old_payload:
        return ["new"]

    changed: list[str] = []
    for key, value in new_payload.items():
        if old_payload.get(key) != value:
            changed.append(key)
    return changed
