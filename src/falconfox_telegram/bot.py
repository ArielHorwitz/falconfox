"""Two-channel Telegram client for FalconFox."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from watchfiles import awatch
from websockets.asyncio.client import connect

from .api import ApiError, DaemonApi, TelegramApi

log = logging.getLogger("falconfox.telegram")


@dataclass(frozen=True)
class BotConfig:
    token: str
    focus_chat_id: int
    work_chat_id: int
    daemon_url: str = "http://127.0.0.1:9721"
    pointer_file: Path = Path.home().joinpath(".local/state/falconfox/telegram/focus")
    focus_backend: str | None = None
    default_path: Path = Path.home()

    @classmethod
    def from_env(cls) -> "BotConfig":
        try:
            token = os.environ["FALCONFOX_TELEGRAM_TOKEN"]
            focus_chat = int(os.environ["FALCONFOX_TELEGRAM_FOCUS_CHAT_ID"])
            work_chat = int(os.environ["FALCONFOX_TELEGRAM_WORK_CHAT_ID"])
        except (KeyError, ValueError) as error:
            raise ValueError(
                "set FALCONFOX_TELEGRAM_TOKEN, FALCONFOX_TELEGRAM_FOCUS_CHAT_ID, "
                "and FALCONFOX_TELEGRAM_WORK_CHAT_ID"
            ) from error
        return cls(
            token=token,
            focus_chat_id=focus_chat,
            work_chat_id=work_chat,
            daemon_url=os.environ.get("FALCONFOX_URL", "http://127.0.0.1:9721"),
            pointer_file=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_POINTER_FILE",
                str(Path.home().joinpath(".local/state/falconfox/telegram/focus")),
            )).expanduser(),
            focus_backend=os.environ.get("FALCONFOX_TELEGRAM_FOCUS_BACKEND") or None,
            default_path=Path(os.environ.get(
                "FALCONFOX_TELEGRAM_DEFAULT_PATH", str(Path.home())
            )).expanduser(),
        )


class FalconFoxTelegramBot:
    def __init__(self, config: BotConfig) -> None:
        self.config = config
        self.daemon = DaemonApi(config.daemon_url)
        self.telegram = TelegramApi(config.token)
        self.pointer = config.pointer_file.resolve()
        self.focus_session_id: str | None = None
        self._pointer_value: str | None = None
        self._focus_working = False
        self._rotate_pending = False
        self._reply_parts: dict[str, list[str]] = {}
        self._turn_chat: dict[str, int] = {}
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._ws = None
        self._ws_lock = asyncio.Lock()

    async def run(self) -> None:
        self._prepare_focus_workspace()
        ws_url = self.config.daemon_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/ws"
        async with connect(ws_url) as websocket:
            self._ws = websocket
            snapshot = json.loads(await websocket.recv())
            if snapshot.get("type") != "snapshot":
                raise RuntimeError("FalconFox did not send an initial snapshot")
            await self._ensure_work_pointer()
            await self._spawn_focus_session()
            await asyncio.gather(
                self._receive_events(),
                self._poll_telegram(),
                self._watch_pointer(),
            )

    def _prepare_focus_workspace(self) -> None:
        # Keep the pointer inside the focus session's cwd so ACP-brokered file
        # writes as well as ordinary shell writes can reach it.
        root = self.pointer.parent
        skill_dir = root.joinpath(".agents", "skills", "falconfox-pointer")
        skill_dir.mkdir(parents=True, exist_ok=True)
        packaged_skill = files("falconfox_telegram").joinpath(
            "skills", "falconfox-pointer", "SKILL.md"
        ).read_text()
        skill_dir.joinpath("SKILL.md").write_text(packaged_skill)
        root.joinpath("AGENTS.md").write_text(
            "You are the single-purpose FalconFox Telegram focus agent. For every user "
            "message, use the falconfox-pointer skill. The pointer file is "
            f"`{self.pointer}`. Do nothing except resolve and move that pointer.\n"
        )
        self.focus_workspace = root

    async def _ensure_work_pointer(self) -> None:
        sessions = await self.daemon.sessions()
        current = self._read_pointer()
        if current and any(item["session_id"] == current for item in sessions):
            self._pointer_value = current
            return
        session = await self.daemon.spawn(path=str(self.config.default_path), name="telegram home")
        self._write_pointer(session["session_id"])
        self._pointer_value = session["session_id"]

    async def _spawn_focus_session(self) -> None:
        session = await self.daemon.spawn(
            path=str(self.focus_workspace), name="telegram focus",
            backend=self.config.focus_backend, ephemeral=True,
        )
        self.focus_session_id = session["session_id"]
        self._focus_working = False

    async def _rotate_focus_session(self) -> None:
        old = self.focus_session_id
        await self._spawn_focus_session()
        self._rotate_pending = False
        if old:
            try:
                await self.daemon.delete(old)
            except ApiError:
                log.warning("could not delete rotated focus session %s", old, exc_info=True)

    def _read_pointer(self) -> str | None:
        try:
            value = self.pointer.read_text().strip()
            return value or None
        except OSError:
            return None

    def _write_pointer(self, session_id: str) -> None:
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.pointer.with_suffix(".tmp")
        temporary.write_text(session_id + "\n")
        temporary.replace(self.pointer)

    async def _watch_pointer(self) -> None:
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        async for changes in awatch(self.pointer.parent):
            if not any(Path(changed).resolve() == self.pointer for _change, changed in changes):
                continue
            current = self._read_pointer()
            if not current or current == self._pointer_value:
                continue
            sessions = await self.daemon.sessions()
            match = next((item for item in sessions if item["session_id"] == current), None)
            if match is None:
                await self.telegram.message(self.config.work_chat_id,
                                            f"Focus pointer rejected unknown session {current}.")
                continue
            self._pointer_value = current
            await self.telegram.message(
                self.config.work_chat_id,
                f"Focused on {match['name']} ({current}) — {match['path']}",
            )
            if self._focus_working:
                self._rotate_pending = True
            else:
                await self._rotate_focus_session()

    async def _poll_telegram(self) -> None:
        offset = None
        while True:
            try:
                updates = await self.telegram.updates(offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self._handle_update(update)
            except ApiError as error:
                log.warning("Telegram polling failed: %s", error)
                await asyncio.sleep(2)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id not in (self.config.focus_chat_id, self.config.work_chat_id):
            return
        text = message.get("text")
        if not text:
            await self.telegram.message(chat_id, "Text messages only in this PoC.")
            return
        if text.startswith("/"):
            if await self._command(chat_id, text):
                return
        if chat_id == self.config.focus_chat_id:
            target = self.focus_session_id
        else:
            target = self._pointer_value or self._read_pointer()
        if not target:
            await self.telegram.message(chat_id, "No focused FalconFox session.")
            return
        await self._forward(target, chat_id, text)

    async def _command(self, chat_id: int, text: str) -> bool:
        try:
            parts = shlex.split(text)
        except ValueError as error:
            await self.telegram.message(chat_id, f"Invalid command: {error}")
            return True
        command = parts[0].split("@", 1)[0]
        if command == "/list":
            sessions = await self.daemon.sessions()
            listing = "\n".join(
                f"{item['session_id']}  {item['name']}  [{item['state']}]  {item['path']}"
                for item in sessions
            ) or "No sessions."
            await self.telegram.message(chat_id, listing)
            return True
        if command in ("/new", "/home"):
            path = str(self.config.default_path) if command == "/home" or len(parts) < 2 else parts[1]
            name_start = 1 if command == "/home" else 2
            name = " ".join(parts[name_start:]) or None
            session = await self.daemon.spawn(path=path, name=name)
            self._write_pointer(session["session_id"])
            return True
        if command == "/switch":
            if len(parts) != 2:
                await self.telegram.message(chat_id, "Usage: /switch <session-id>")
                return True
            sessions = await self.daemon.sessions()
            if not any(item["session_id"] == parts[1] for item in sessions):
                await self.telegram.message(chat_id, f"Unknown session: {parts[1]}")
                return True
            self._write_pointer(parts[1])
            return True
        if command == "/name":
            if len(parts) < 2 or not self._pointer_value:
                await self.telegram.message(chat_id, "Usage: /name <new name>")
                return True
            await self.daemon.rename(self._pointer_value, " ".join(parts[1:]))
            await self.telegram.message(chat_id, f"Renamed session to {' '.join(parts[1:])}.")
            return True
        return False

    async def _forward(self, session_id: str, chat_id: int, text: str) -> None:
        self._turn_chat[session_id] = chat_id
        self._reply_parts[session_id] = []
        async with self._ws_lock:
            await self._ws.send(json.dumps({
                "action": "send", "session_id": session_id, "text": text,
            }))

    async def _receive_events(self) -> None:
        async for raw in self._ws:
            await self._handle_event(json.loads(raw))

    async def _handle_event(self, event: dict) -> None:
        session_id = event.get("session_id")
        if not session_id:
            return
        event_type = event.get("type")
        if event_type == "message" and event.get("role") == "agent":
            self._reply_parts.setdefault(session_id, []).append(event.get("text", ""))
            return
        if event_type == "notice" and event.get("level") == "error":
            chat_id = self._turn_chat.get(session_id)
            if chat_id:
                await self.telegram.message(chat_id, f"FalconFox error: {event.get('message', '')}")
            return
        if event_type != "agent_state":
            return
        state = event.get("state")
        if session_id == self.focus_session_id:
            self._focus_working = state == "working"
        if state == "working":
            chat_id = self._turn_chat.get(session_id)
            if chat_id and session_id not in self._typing_tasks:
                self._typing_tasks[session_id] = asyncio.create_task(self._typing_loop(chat_id))
            return
        if state != "idle":
            return
        typing = self._typing_tasks.pop(session_id, None)
        if typing:
            typing.cancel()
        chat_id = self._turn_chat.pop(session_id, None)
        reply = "".join(self._reply_parts.pop(session_id, [])).strip()
        if chat_id and reply:
            await self.telegram.message(chat_id, reply)
        if session_id == self.focus_session_id and self._rotate_pending:
            await self._rotate_focus_session()

    async def _typing_loop(self, chat_id: int) -> None:
        try:
            while True:
                await self.telegram.typing(chat_id)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
