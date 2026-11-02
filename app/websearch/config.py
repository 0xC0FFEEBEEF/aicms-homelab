from __future__ import annotations

from urllib.parse import quote_plus


def format_query_url(template: str, query: str) -> str:
    return template.replace("<query>", quote_plus(query))


def render_request_preview(template: str, query: str = "aicms test") -> str:
    return format_query_url(template, query)


def parse_domain_filters(raw: str | list[str] | None) -> list[str]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str):
        items = raw.replace(",", "\n").splitlines()
    else:
        items = []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item).strip().lower()
        if not text:
            continue
        text = text.removeprefix("http://").removeprefix("https://").strip("/")
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned
