from __future__ import annotations

from pydantic import BaseModel


class WebSearchResultItem(BaseModel):
    title: str
    url: str | None = None
    snippet: str | None = None
    source_type: str = "web_search"
    relevance: float | None = None
    raw: dict | None = None
