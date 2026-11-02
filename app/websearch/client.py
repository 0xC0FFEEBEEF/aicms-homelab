from __future__ import annotations

from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.schemas import WebSearchSettingsPayload
from app.websearch.config import format_query_url
from app.websearch.schemas import WebSearchResultItem


class WebSearchClient:
    def __init__(self, settings: WebSearchSettingsPayload):
        self.settings = settings
        self.client = httpx.Client(
            timeout=settings.timeout,
            verify=settings.verify_ssl_certificate,
            trust_env=settings.trust_proxy_environment,
        )

    def _apply_filters(self, items: list[WebSearchResultItem]) -> list[WebSearchResultItem]:
        if not self.settings.domain_filters:
            return items
        filtered: list[WebSearchResultItem] = []
        for item in items:
            if not item.url:
                continue
            domain = urlparse(item.url).netloc.lower()
            if any(domain == allowed or domain.endswith(f".{allowed}") for allowed in self.settings.domain_filters):
                filtered.append(item)
        return filtered

    def _searxng_url(self, query: str) -> str:
        url = format_query_url(self.settings.query_url_template, query)
        if "format=" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}format=json"
        if "language=" not in url and self.settings.language:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}language={self.settings.language}"
        return url

    def test_connection(self) -> str:
        url = self._searxng_url("aicms healthcheck") if self.settings.engine == "searxng" else format_query_url(
            self.settings.query_url_template, "aicms healthcheck"
        )
        response = self.client.get(url)
        response.raise_for_status()
        return str(response.url)

    def search(self, query: str) -> list[WebSearchResultItem]:
        if self.settings.engine == "disabled":
            return []
        if self.settings.engine == "searxng":
            try:
                response = self.client.get(self._searxng_url(query))
                response.raise_for_status()
                data = response.json()
                items = [
                    WebSearchResultItem(
                        title=result.get("title") or result.get("url") or "Untitled result",
                        url=result.get("url"),
                        snippet=result.get("content") or result.get("snippet"),
                        relevance=result.get("score"),
                        raw=result,
                    )
                    for result in (data.get("results") or [])[: self.settings.result_count]
                    if isinstance(result, dict)
                ]
                filtered = self._apply_filters(items)
                if filtered:
                    return filtered[: self.settings.result_count]
            except Exception:
                pass

        response = self.client.get(format_query_url(self.settings.query_url_template, query))
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        items: list[WebSearchResultItem] = []
        for anchor in soup.select("a[href]"):
            href = anchor.get("href")
            title = " ".join(anchor.stripped_strings)
            if not href or not title:
                continue
            items.append(WebSearchResultItem(title=title[:200], url=href, snippet=None, raw={"href": href}))
            if len(items) >= self.settings.result_count:
                break
        return self._apply_filters(items)[: self.settings.result_count]

    def load_result(self, item: WebSearchResultItem) -> str | None:
        if self.settings.bypass_web_loader or not item.url:
            return item.snippet
        response = self.client.get(item.url, timeout=self.settings.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        text = " ".join(soup.stripped_strings)
        return text[:1000] if text else item.snippet
