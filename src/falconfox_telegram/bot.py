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
from websockets.exceptions import ConnectionClosed

from .api import ApiError, DaemonApi, TelegramApi
from .rendering import render_messages

log = logging.getLogger("falconfox.telegram")

INTERRUPTED_TURN = (
    "Lost the connection mid-turn, so that reply is gone. The session still "
    "has it — ask it to repeat."
)


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


# Telegram has no "thinking", "working" or "stuck" chat action: every one of the
# eleven valid values describes the bot producing a kind of content. So the
# vocabulary gets spent as a code -- one distinct action per state we can
# actually tell apart -- which is the most this channel can carry. Each lasts
# about five seconds, hence the refresh loop below.
#
# Which glyph means what is deliberately arbitrary for now. What matters is that
# the states are distinguishable in the chat; the mapping is a table of one-line
# choices to reshuffle here, in one place, once we have watched it in use.
#
# Note `record_voice` is on loan: it is the honest action for a reply that is
# itself a voice message, which is what the deferred voice work would produce.
# If that lands, move audio to `upload_voice` or move streaming to one of the
# unused actions (`choose_sticker` aside, `find_location`, `upload_photo`, the
# video ones).
TURN_ACTIONS = {
    "starting": "choose_sticker",    # resuming or launching the ACP subprocess
    "working": "typing",             # alive, but producing nothing right now
    "thinking": "find_location",     # agent_thought_chunk
    "streaming": "record_voice",     # agent_message_chunk -- output is flowing
    "tool": "upload_document",       # a tool call is running
}
DEFAULT_ACTION = TURN_ACTIONS["working"]
ACTION_REFRESH_SECONDS = 4


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
        self._activity_tasks: dict[str, asyncio.Task] = {}
        self._activity_state: dict[str, str] = {}
        self._ws = None
        self._ws_lock = asyncio.Lock()

    async def run(self) -> None:
        self._prepare_focus_workspace()
        ws_url = self.config.daemon_url.replace("http://", "ws://", 1).replace(
            "https://", "wss://", 1
        ) + "/ws"
        # `async for ... in connect(...)` retries the connection with backoff,
        # so the bot survives daemon restarts (e.g. a self-update) instead of
        # dying with the connection.
        async for websocket in connect(ws_url):
            try:
                await self._run_connected(websocket)
            except (ConnectionClosed, ApiError, OSError) as error:
                log.warning("daemon connection lost (%s); reconnecting", error)
                await asyncio.sleep(2)
                continue

    async def _run_connected(self, websocket) -> None:
        self._ws = websocket
        snapshot = json.loads(await websocket.recv())
        if snapshot.get("type") != "snapshot":
            raise RuntimeError("FalconFox did not send an initial snapshot")
        await self._ensure_work_pointer()
        # Rotate rather than plain-spawn: after a reconnect that was not a
        # daemon restart, this also cleans up the previous focus session.
        await self._rotate_focus_session()
        loops = [asyncio.create_task(coroutine) for coroutine in (
            self._receive_events(), self._poll_telegram(), self._watch_pointer(),
        )]
        try:
            # All three loops are endless, so any completion means the
            # connection (or a loop) is gone; surface its outcome.
            done, _pending = await asyncio.wait(loops, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
        finally:
            for loop in loops:
                loop.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
            # Dropping the connection discards whatever each in-flight turn
            # had accumulated. Say so, rather than leaving a chat waiting on
            # a reply that can no longer arrive -- the session still holds the
            # turn, so the content is recoverable, but only if you know.
            for chat_id in self._reset_connection_state():
                try:
                    await self.telegram.message(chat_id, INTERRUPTED_TURN)
                except Exception:
                    log.warning("could not report the lost turn to %s", chat_id)

    def _reset_connection_state(self) -> list[int]:
        """Clear per-connection state; return chats left mid-turn."""
        self._ws = None
        self._focus_working = False
        self._rotate_pending = False
        for activity in self._activity_tasks.values():
            activity.cancel()
        self._activity_tasks.clear()
        self._activity_state.clear()
        interrupted = sorted(set(self._turn_chat.values()))
        self._turn_chat.clear()
        self._reply_parts.clear()
        return interrupted

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
        # Claude discovers skills under .claude/skills; bridge with a symlink.
        claude_dir = root.joinpath(".claude")
        claude_dir.mkdir(exist_ok=True)
        skills_link = claude_dir.joinpath("skills")
        if not skills_link.is_symlink() and not skills_link.exists():
            skills_link.symlink_to(Path("..", ".agents", "skills"))
        orientation = (
            "You are the FalconFox Telegram session manager: the single-purpose "
            "session behind the focus chat. You are NOT a work agent — you manage "
            "sessions, you do not work inside them. That includes deciding which "
            "session the work chat talks to, and also spawning, renaming, stopping "
            "and deleting sessions. For every user message, follow the "
            "falconfox-pointer skill (.agents/skills/falconfox-pointer). "
            f"The pointer file is `{self.pointer}`. You may run any `falconfox` "
            "command and write that pointer file — nothing else. Confirm the target "
            "back to the user before `stop` or `delete`. Never orient on or work in "
            "any project. When greeting or unsure, ask what the user wants to do.\n"
        )
        # Both files, so every agent runtime picks the orientation up natively.
        root.joinpath("AGENTS.md").write_text(orientation)
        root.joinpath("CLAUDE.md").write_text(orientation)
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

    def _start_activity(self, session_id: str, chat_id: int) -> bool:
        """Ensure a refresh loop is running. True if this call started one."""
        task = self._activity_tasks.get(session_id)
        # `task.done()` matters: a finished task is still *in* the dict, and the
        # old `session_id not in self._typing_tasks` guard read that as live. One
        # failed sendChatAction therefore silenced a turn permanently, with the
        # `working` safety net unable to restart it because it hit the same
        # guard.
        if task is not None and not task.done():
            return False
        self._activity_tasks[session_id] = asyncio.create_task(
            self._activity_loop(session_id, chat_id))
        return True

    async def _set_activity(self, session_id: str, state: str) -> None:
        """Record what the session is doing and show it in the chat."""
        chat_id = self._turn_chat.get(session_id)
        if chat_id is None:
            return
        # Before the equality check, so this doubles as the safety net that
        # revives a loop which died mid-turn.
        started = self._start_activity(session_id, chat_id)
        if self._activity_state.get(session_id) == state:
            return
        self._activity_state[session_id] = state
        # Streamed output fires an event per chunk; only a *change* is worth an
        # API call. A fresh loop sends immediately, so it needs no second one.
        if not started:
            await self._send_action(session_id, chat_id)

    async def _send_action(self, session_id: str, chat_id: int) -> None:
        action = TURN_ACTIONS.get(
            self._activity_state.get(session_id, ""), DEFAULT_ACTION)
        try:
            await self.telegram.chat_action(chat_id, action)
        except ApiError as error:
            # Never fatal to the loop. A 429 from the rate limiter -- likeliest
            # on exactly the long turn that needs an indicator -- or one of the
            # read timeouts this deployment sees used to end the task outright
            # and leave the turn silent for the rest of its life.
            log.debug("chat action %s failed for %s: %s", action, session_id, error)

    async def _forward(self, session_id: str, chat_id: int, text: str) -> None:
        self._turn_chat[session_id] = chat_id
        self._reply_parts[session_id] = []
        # Type from the moment the prompt goes out. Waiting for the daemon to
        # report `working` leaves the whole backend-startup window silent: a
        # stored session resumes an ACP subprocess first, and the daemon carries
        # that as a `starting` state on session_updated, which this client does
        # not consume. That gap is exactly when a turn looks like it hung.
        await self._set_activity(session_id, "working")
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
        if event_type == "message":
            role = event.get("role")
            if role == "agent":
                self._reply_parts.setdefault(session_id, []).append(event.get("text", ""))
                await self._set_activity(session_id, "streaming")
            elif role == "thought":
                # Never part of the reply -- only a sign of what is happening.
                await self._set_activity(session_id, "thinking")
            return
        if event_type == "tool_call":
            # Consumed as a *state signal* only. Tool calls stay suppressed as
            # content: one message per turn is a deliberate choice for a phone
            # chat, and rendering them would undo it.
            status = event.get("status")
            await self._set_activity(
                session_id, "working" if status in ("completed", "failed") else "tool")
            return
        if event_type == "session_updated":
            # The daemon carries a resuming ACP subprocess as `starting`, the
            # slowest part of a cold turn. The client used to ignore this event
            # entirely, so that whole window looked identical to working.
            if event.get("state") == "starting":
                await self._set_activity(session_id, "starting")
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
            # Normally already active since _forward; this covers a turn that
            # began before the indicator did, and revives a loop that has died.
            await self._set_activity(session_id, "working")
            return
        if state != "idle":
            return
        activity = self._activity_tasks.pop(session_id, None)
        if activity:
            activity.cancel()
        self._activity_state.pop(session_id, None)
        chat_id = self._turn_chat.pop(session_id, None)
        reply = "".join(self._reply_parts.pop(session_id, [])).strip()
        if chat_id and reply:
            for rendered in render_messages(reply):
                await self.telegram.html_message(chat_id, rendered.html, rendered.plain)
        if session_id == self.focus_session_id and self._rotate_pending:
            await self._rotate_focus_session()

    async def _activity_loop(self, session_id: str, chat_id: int) -> None:
        try:
            while True:
                await self._send_action(session_id, chat_id)
                await asyncio.sleep(ACTION_REFRESH_SECONDS)
        except asyncio.CancelledError:
            raise
