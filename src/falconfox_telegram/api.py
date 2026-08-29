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

    async def version(self) -> dict:
        return await _json_request(f"{self.base_url}/api/version")

    async def sessions(self) -> list[dict]:
        return await _json_request(f"{self.base_url}/api/sessions")

    async def session(self, session_id: str) -> dict:
        """Session metadata plus its full transcript."""
        return await _json_request(f"{self.base_url}/api/sessions/{session_id}")

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

    @staticmethod
    def _reply(body: dict, reply_to: int | None) -> dict:
        # Threading a message to the prompt it answers is also the notification
        # lever: in a group, replies (like mentions) cut through a muted chat,
        # so progress can stay silent while the answer still pings.
        if reply_to is not None:
            body["reply_parameters"] = {"message_id": reply_to,
                                        "allow_sending_without_reply": True}
        return body

    @staticmethod
    def _thread(body: dict, thread: int | None) -> dict:
        # None means the General topic, which is addressed by *omitting* the
        # field -- not by sending null. A message with no thread id and no
        # reply lands in General; one that replies into a topic inherits that
        # topic even without this field (measured, not documented).
        if thread is not None:
            body["message_thread_id"] = thread
        return body

    async def message(self, chat_id: int, text: str, reply_to: int | None = None,
                      silent: bool = False, thread: int | None = None) -> int | None:
        # Bot API text is capped at 4096 characters. Plain sends (notices,
        # command output, fallbacks) are truncated rather than split.
        if len(text) > 4096:
            text = text[:4080] + "\n…[truncated]"
        body = self._thread(self._reply({"chat_id": chat_id, "text": text}, reply_to),
                            thread)
        if silent:
            # Delivered without a notification sound/banner -- the progress
            # message is ambient; only the actual response should ping.
            body["disable_notification"] = True
        result = await self.call("sendMessage", body)
        return (result or {}).get("message_id")

    async def html_message(self, chat_id: int, html_text: str, plain_fallback: str,
                           reply_to: int | None = None,
                           thread: int | None = None) -> None:
        # Telegram rejects the whole message on any HTML entity error, so a
        # failed formatted send falls back to the plain source text.
        try:
            await self.call("sendMessage", self._thread(self._reply({
                "chat_id": chat_id, "text": html_text, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }, reply_to), thread))
        except ApiError as error:
            log.warning("HTML send failed (%s); falling back to plain text", error)
            await self.message(chat_id, plain_fallback, reply_to=reply_to,
                               thread=thread)

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        if len(text) > 4096:
            text = text[:4080] + "\n…[truncated]"
        try:
            await self.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "text": text,
            })
        except ApiError as error:
            # Re-sending identical text is not an error worth surfacing.
            if "not modified" in str(error):
                return
            raise

    async def chat_action(self, chat_id: int, action: str,
                          thread: int | None = None) -> None:
        await self.call("sendChatAction",
                        self._thread({"chat_id": chat_id, "action": action}, thread))

    # --- forum topics ---------------------------------------------------
    #
    # Measured against the live API (see the topics case): a supergroup forum
    # supports the whole lifecycle, a private-chat forum refuses close/reopen
    # on chat type. `editMessageText` needs no thread id -- chat plus message
    # id is enough -- which is why there is no threaded variant of it.

    async def create_topic(self, chat_id: int, name: str) -> int:
        result = await self.call("createForumTopic",
                                 {"chat_id": chat_id, "name": name[:128]})
        return result["message_thread_id"]

    async def rename_topic(self, chat_id: int, thread: int, name: str) -> None:
        await self.call("editForumTopic", {
            "chat_id": chat_id, "message_thread_id": thread, "name": name[:128],
        })

    async def close_topic(self, chat_id: int, thread: int) -> None:
        # A closed topic still accepts *bot* writes; it only stops members
        # posting. That is what makes it the right shape for a stopped
        # session -- the record stays, the user cannot prompt a dead session,
        # and the bot can still deliver a final notice.
        await self.call("closeForumTopic",
                        {"chat_id": chat_id, "message_thread_id": thread})

    async def reopen_topic(self, chat_id: int, thread: int) -> None:
        await self.call("reopenForumTopic",
                        {"chat_id": chat_id, "message_thread_id": thread})

    async def delete_topic(self, chat_id: int, thread: int) -> None:
        await self.call("deleteForumTopic",
                        {"chat_id": chat_id, "message_thread_id": thread})

