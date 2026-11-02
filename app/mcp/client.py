from __future__ import annotations

from typing import Any

import httpx


class MCPClient:
    def __init__(self, *, base_url: str, timeout: int, api_token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_token = api_token
        self._request_id = 0

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {}}
        response = httpx.post(self.base_url, json=payload, headers=self._headers(), timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        return data.get("result") or {}

    def initialize(self) -> dict[str, Any]:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "clientInfo": {"name": "AICMS", "version": "1.0"},
                "capabilities": {},
            },
        )

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list")
        tools = result.get("tools")
        if isinstance(tools, list):
            return [tool for tool in tools if isinstance(tool, dict)]
        if isinstance(result, list):
            return [tool for tool in result if isinstance(tool, dict)]
        return []

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = self._rpc(
            "tools/call",
            {
                "name": tool_name,
                "arguments": arguments,
            },
        )
        return result
