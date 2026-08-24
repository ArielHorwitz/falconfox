"""Small async clients for the FalconFox and Telegram HTTP APIs."""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("falconfox.telegram.api")


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
        except OSError as error:
            # A *read* timeout raises TimeoutError, which urllib does not wrap
            # in URLError (unlike a connect failure). Uncaught it escapes the
            # caller's `except ApiError`, kills the polling task, and takes the
            # daemon websocket down with it -- discarding any in-flight turn's
            # reply. Observed live: two teardowns during one long turn, blamed
            # on the daemon, which was healthy throughout.
            raise ApiError(f"{type(error).__name__}: {error}") from error

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
        # Bot API text is capped at 4096 characters. Plain sends (notices,
        # command output, fallbacks) are truncated rather than split.
        if len(text) > 4096:
            text = text[:4080] + "\n…[truncated]"
        await self.call("sendMessage", {"chat_id": chat_id, "text": text})

    async def html_message(self, chat_id: int, html_text: str, plain_fallback: str) -> None:
        # Telegram rejects the whole message on any HTML entity error, so a
        # failed formatted send falls back to the plain source text.
        try:
            await self.call("sendMessage", {
                "chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            })
        except ApiError as error:
            log.warning("HTML send failed (%s); falling back to plain text", error)
            await self.message(chat_id, plain_fallback)

    async def chat_action(self, chat_id: int, action: str) -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": action})

