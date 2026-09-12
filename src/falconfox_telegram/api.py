"""Small async clients for the FalconFox and Telegram HTTP APIs."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import shutil
import urllib.error
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("falconfox.telegram.api")

# These calls are network-bound, so the pool is sized for concurrent requests
# rather than for CPUs. `asyncio.to_thread` uses the default executor, which is
# min(32, cpu_count + 4) -- five threads on a 1-vCPU host, one of them held
# permanently by the 30-second getUpdates long poll. That was survivable with a
# single conversation; with a topic per session, every session's activity loop
# competes for the remaining four and they queue behind each other. Measured
# before this existed: a topic took 30-40s to appear after the daemon had
# already announced the session.
_REQUESTS = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="falconfox-http")


class ApiError(Exception):
    pass


def _multipart(fields: dict, file_field: str, file_path: Path) -> tuple[bytes, str]:
    """Build a multipart/form-data body: the one Telegram call that is not JSON.

    Hand-rolled rather than pulled in as a dependency -- sending a file is a
    header, two delimiters and the bytes, and the alternative is a package for
    it.
    """
    boundary = f"----falconfox{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
        f"filename=\"{file_path.name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode())
    parts.append(file_path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


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
                # "error" is the daemon's shape, "description" is Telegram's.
                # Reading only the first turned every Bot API failure into a
                # bare "HTTP Error 400: Bad Request", which says nothing about
                # what was wrong with the request.
                reported = json.loads(error.read())
                detail = (reported.get("error") or reported.get("description")
                          or str(error))
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

    return await asyncio.get_running_loop().run_in_executor(_REQUESTS, perform)


class DaemonApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def version(self) -> dict:
        return await _json_request(f"{self.base_url}/api/version")

    async def sessions(self, include_hidden: bool = False) -> list[dict]:
        suffix = "?include_hidden=true" if include_hidden else ""
        return await _json_request(f"{self.base_url}/api/sessions{suffix}")

    async def session(self, session_id: str,
                      include_transcript: bool = False) -> dict:
        """Session metadata, and the transcript only when it is wanted.

        Off by default because most callers want a field: whether the session
        exists at all, or the directory it runs in. A transcript is megabytes
        on a session that has been useful for a while.
        """
        suffix = "?include_transcript=true" if include_transcript else ""
        return await _json_request(
            f"{self.base_url}/api/sessions/{session_id}{suffix}")

    async def spawn(self, *, path: str, name: str | None = None,
                    backend: str | None = None, ephemeral: bool = False,
                    hidden: bool | None = None,
                    roles: list[str] | None = None) -> dict:
        return await _json_request(f"{self.base_url}/api/sessions", "POST", {
            "path": path, "name": name, "backend": backend,
            "ephemeral": ephemeral, "hidden": hidden, "roles": roles,
        })

    async def delete(self, session_id: str) -> None:
        await _json_request(f"{self.base_url}/api/sessions/{session_id}", "DELETE")

    async def tag(self, session_id: str, tags: list[str]) -> dict:
        """Replace a session's tags; the response carries what stuck."""
        return await _json_request(f"{self.base_url}/api/sessions/{session_id}/tag",
                                   "POST", {"tags": tags})

    async def cancel(self, session_id: str) -> None:
        """End the running turn. Harmless when there is none."""
        await _json_request(f"{self.base_url}/api/sessions/{session_id}/cancel",
                            "POST", {})

    async def rename(self, session_id: str, name: str) -> None:
        await _json_request(f"{self.base_url}/api/sessions/{session_id}/rename",
                            "POST", {"name": name})

    async def add_file(self, session_id: str, path: str,
                       name: str | None = None) -> dict:
        """Copy a file into the session's store. Answers with id and path.

        The daemon and this client share a filesystem, so what crosses is a
        path rather than the bytes -- the same handoff `attach` makes in the
        other direction.
        """
        return await _json_request(f"{self.base_url}/api/sessions/{session_id}/files",
                                   "POST", {"path": path, "name": name})

    async def remove_file(self, session_id: str, file_id: str) -> dict:
        return await _json_request(
            f"{self.base_url}/api/sessions/{session_id}/files/{file_id}", "DELETE")

    async def clear_files(self, session_id: str) -> dict:
        return await _json_request(f"{self.base_url}/api/sessions/{session_id}/files",
                                   "DELETE")


class TelegramApi:
    # `getFile` will not serve a file larger than this, against 50MB for
    # upload. An asymmetry of the Bot API rather than a policy of ours, which
    # is why a refusal says so.
    DOWNLOAD_LIMIT_BYTES = 20 * 1024 * 1024

    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.file_url = f"https://api.telegram.org/file/bot{token}"

    async def call(self, method: str, body: dict | None = None):
        payload = await _json_request(f"{self.base_url}/{method}", "POST", body or {})
        if not payload.get("ok"):
            raise ApiError(payload.get("description", f"Telegram {method} failed"))
        return payload.get("result")

    async def updates(self, offset: int | None) -> list[dict]:
        # my_chat_member reports being added to a group and promoted in it,
        # which is how the forum is discovered without the operator reading a
        # log. A plain `message` subscription never delivers it.
        body = {"timeout": 30,
                "allowed_updates": ["message", "my_chat_member"]}
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

    async def send_file(self, chat_id: int, file_path: Path, method: str = "sendDocument",
                        field: str = "document", caption: str | None = None,
                        thread: int | None = None) -> None:
        """Upload a file to a chat. Raises ApiError with Telegram's own reason.

        `method` and `field` are how the same upload becomes a photo, a video
        or a plain file: Telegram varies only the endpoint and the form field
        name, and the body is built the same way for all of them.
        """
        fields = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if thread is not None:
            fields["message_thread_id"] = str(thread)
        body, content_type = _multipart(fields, field, file_path)

        def perform():
            request = urllib.request.Request(
                f"{self.base_url}/{method}", data=body, method="POST",
                headers={"Content-Type": content_type})
            try:
                # Longer than the JSON timeout: this is an upload, and the
                # limit is 50MB of it.
                with urllib.request.urlopen(request, timeout=110) as response:
                    payload = json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as error:
                try:
                    detail = json.loads(error.read()).get("description") or str(error)
                except Exception:
                    detail = str(error)
                raise ApiError(detail) from error
            except (urllib.error.URLError, OSError) as error:
                raise ApiError(f"{type(error).__name__}: {error}") from error
            if not payload.get("ok"):
                raise ApiError(payload.get("description", f"{method} failed"))

        await asyncio.get_running_loop().run_in_executor(_REQUESTS, perform)

    async def file_path(self, file_id: str) -> str:
        """Where a file lives on Telegram's side, ready to be downloaded.

        Kept apart from the download because the caller wants the answer for
        its own sake: this path carries the only extension a photo ever has,
        and photos arrive with no name at all.
        """
        described = await self.call("getFile", {"file_id": file_id})
        remote = (described or {}).get("file_path")
        if not remote:
            raise ApiError("Telegram did not say where the file is")
        return remote

    async def download(self, remote_path: str, into: Path) -> Path:
        """Fetch what `file_path` pointed at, writing it to `into`.

        Files come from a different host to the API calls, which is the only
        reason this is not just another `call`.
        """
        url = f"{self.file_url}/{remote_path}"

        def perform() -> None:
            request = urllib.request.Request(url, method="GET")
            try:
                # A download, sized like the upload timeout rather than the
                # JSON one, and streamed so a 20MB file is never held twice.
                with urllib.request.urlopen(request, timeout=110) as response:
                    with into.open("wb") as sink:
                        shutil.copyfileobj(response, sink)
            except urllib.error.HTTPError as error:
                raise ApiError(f"HTTP {error.code} fetching the file") from error
            except (urllib.error.URLError, OSError) as error:
                raise ApiError(f"{type(error).__name__}: {error}") from error

        await asyncio.get_running_loop().run_in_executor(_REQUESTS, perform)
        log.info("downloaded %s to %s (%d bytes)", remote_path, into,
                 into.stat().st_size)
        return into

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

    async def get_chat(self, chat_id: int) -> dict:
        return await self.call("getChat", {"chat_id": chat_id}) or {}

    async def get_member(self, chat_id: int, user_id: int) -> dict:
        return await self.call("getChatMember",
                               {"chat_id": chat_id, "user_id": user_id}) or {}

    async def create_topic(self, chat_id: int, name: str,
                           icon: str | None = None) -> int:
        body = {"chat_id": chat_id, "name": name[:128]}
        if icon:
            # Set at creation the icon would otherwise cost an edit -- and an
            # edit is a service message in the topic, where creation is not.
            body["icon_custom_emoji_id"] = icon
        result = await self.call("createForumTopic", body)
        return result["message_thread_id"]

    async def set_topic_icon(self, chat_id: int, thread: int, icon: str) -> None:
        """Change a topic's icon. An empty string removes it.

        `name` is omitted deliberately: editForumTopic keeps the current title
        when the field is absent, so this cannot race a rename into reverting
        it.
        """
        await self.call("editForumTopic", {
            "chat_id": chat_id, "message_thread_id": thread,
            "icon_custom_emoji_id": icon,
        })

    async def icon_stickers(self) -> list[dict]:
        """The custom emoji allowed as topic icons. No arguments, no rights."""
        return await self.call("getForumTopicIconStickers") or []

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

