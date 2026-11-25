from __future__ import annotations

from typing import Any, Protocol

import httpx


class TelegramApi(Protocol):
    async def call(self, method: str, body: dict[str, Any] | None = None) -> Any: ...


class TelegramApiError(Exception):
    def __init__(self, method: str, status: int, description: str) -> None:
        super().__init__(f"Telegram {method} failed: {status} {description}")
        self.method, self.status, self.description = method, status, description


class HttpTelegramApi:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, body: dict[str, Any] | None = None) -> Any:
        res = await self._client.post(f"https://api.telegram.org/bot{self._token}/{method}", json=body or {}, timeout=40)
        try:
            data = res.json()
        except ValueError:
            data = {}
        if not res.is_success or not data.get("ok"):
            raise TelegramApiError(method, res.status_code, data.get("description", "unknown"))
        return data.get("result")
