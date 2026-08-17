"""Small async clients for the FalconFox and Telegram HTTP APIs."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request


class ApiError(Exception):
    pass


async def _json_request(url: str, method: str = "GET", body: dict | None = None):
    def perform():
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = response.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read()).get("error") or str(error)
            except Exception:
                detail = str(error)
            raise ApiError(detail) from error
        except urllib.error.URLError as error:
            raise ApiError(str(error.reason)) from error

    return await asyncio.to_thread(perform)


class DaemonApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def sessions(self) -> list[dict]:
        return await _json_request(f"{self.base_url}/api/sessions")

    async def spawn(self, *, path: str, name: str | None = None,
                    backend: str | None = None, ephemeral: bool = False) -> dict:
        return await _json_request(f"{self.base_url}/api/sessions", "POST", {
            "path": path, "name": name, "backend": backend, "ephemeral": ephemeral,
        })

    async def delete(self, session_id: str) -> None:
        await _json_request(f"{self.base_url}/api/sessions/{session_id}", "DELETE")

    async def rename(self, session_id: str, name: str) -> None:
        await _json_request(f"{self.base_url}/api/sessions/{session_id}/rename",
                            "POST", {"name": name})


class TelegramApi:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    async def call(self, method: str, body: dict | None = None):
        payload = await _json_request(f"{self.base_url}/{method}", "POST", body or {})
        if not payload.get("ok"):
            raise ApiError(payload.get("description", f"Telegram {method} failed"))
        return payload.get("result")

    async def updates(self, offset: int | None) -> list[dict]:
        body = {"timeout": 30, "allowed_updates": ["message"]}
        if offset is not None:
            body["offset"] = offset
        return await self.call("getUpdates", body)

    async def message(self, chat_id: int, text: str) -> None:
        # Bot API text is capped at 4096 characters. Keep the PoC's one-message
        # per turn rule by truncating exceptionally long replies.
        if len(text) > 4096:
            text = text[:4080] + "\n…[truncated]"
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def typing(self, chat_id: int) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

