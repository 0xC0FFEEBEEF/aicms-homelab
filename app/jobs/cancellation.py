from __future__ import annotations

from threading import Event


class JobCancelled(RuntimeError):
    pass


def ensure_not_cancelled(stop_event: Event | None, message: str = "Operation stopped by user.") -> None:
    if stop_event is not None and stop_event.is_set():
        raise JobCancelled(message)
