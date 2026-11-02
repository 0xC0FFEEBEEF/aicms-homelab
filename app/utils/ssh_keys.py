from __future__ import annotations

from pathlib import Path


def normalize_private_key_path(raw_path: str | None) -> str | None:
    if not raw_path:
        return None

    raw_text = str(raw_path).strip()
    if not raw_text:
        return None

    if raw_text.endswith(".pub"):
        raw_text = raw_text[:-4]

    if raw_text.startswith(".ssh/"):
        return f"~/{raw_text}"
    if raw_text.startswith("ssh/"):
        return f"~/.{raw_text}"
    return raw_text


def resolve_private_key_path(raw_path: str | None) -> Path | None:
    normalized = normalize_private_key_path(raw_path)
    if not normalized:
        return None

    path = Path(normalized).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def resolve_public_key_path(raw_path: str | None) -> Path:
    private_key_path = resolve_private_key_path(raw_path)
    if private_key_path is None:
        raise FileNotFoundError("A private SSH key path is required.")

    return Path(f"{private_key_path}.pub")
