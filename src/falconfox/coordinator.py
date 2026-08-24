"""Global session coordinator over the vendor-neutral ACP engine."""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional

from . import config, logsetup, storage
from .engine import oneshot
from .engine.client import resolve_config_value
from .engine.events import EventBus
from .engine.session import AgentSession, SessionManager
from .errors import FalconFoxError

_REPLAYABLE = {"message", "tool_call", "notice", "plan", "usage"}
_LOG_INFO_EVENTS = {
    "session_added", "session_removed", "config_changed", "permission_request",
    "permission_resolved", "transcript_reset",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _clean_name(reply: str) -> str:
    first_line = reply.strip().splitlines()[0] if reply.strip() else ""
    return first_line.strip().strip("\"'").strip()[:80]


def _auto_allow_option(options: list[dict]) -> Optional[str]:
    for kind in ("allow_always", "allow_once"):
        for option in options:
            if option.get("kind") == kind:
                return option["option_id"]
    return options[0]["option_id"] if options else None


class SessionCoordinator:
    """Own all FalconFox sessions, regardless of their working directory."""

    def __init__(self, store_root: Path | None = None) -> None:
        self.log = logsetup.get_logger("coordinator")
        self.config = config.load_config()
        self.store = storage.SessionStore(store_root)
        self.bus = EventBus()
        self.sessions = SessionManager()
        self._metadata: dict[str, dict] = {}
        self._transcripts: dict[str, list[dict]] = {}
        self._acp_ids: dict[str, Optional[str]] = {}
        self._config_options: dict[str, list[dict]] = {}
        self._commands: dict[str, list[dict]] = {}
        self._pending_context: dict[str, str] = {}
        self._auto_named: dict[str, bool] = {}
        self._persisted: set[str] = set()
        self._usage: dict[str, dict] = {}
        self._busy_ids: set[str] = set()

    # --- persistence and event flow ------------------------------------

    def load_persisted(self) -> None:
        for meta in self.store.load_all_meta():
            session_id = meta["session_id"]
            named = bool(meta.get("named", False))
            self._metadata[session_id] = {
                "session_id": session_id,
                "name": meta.get("name", session_id),
                "path": meta.get("path", str(Path.home())),
                "backend": meta.get("backend", ""),
                "always_allow": True,
                "ephemeral": False,
                "state": "stored",
                "live": False,
                "created": meta.get("created"),
                "last_active": meta.get("last_active") or meta.get("created"),
            }
            self._acp_ids[session_id] = meta.get("acp_session_id")
            self._auto_named[session_id] = not named
            self._persisted.add(session_id)

    def _ensure_transcript(self, session_id: str) -> list[dict]:
        transcript = self._transcripts.get(session_id)
        if transcript is None:
            transcript = self.store.read_transcript(session_id) if session_id in self._metadata else []
            self._transcripts[session_id] = transcript
        return transcript

    def _evict_transcript(self, session_id: str) -> None:
        self._transcripts.pop(session_id, None)

    def _should_persist(self, session_id: str) -> bool:
        meta = self._metadata.get(session_id, {})
        if meta.get("ephemeral"):
            return False
        if not self._auto_named.get(session_id, True):
            return True
        return any(
            event.get("type") == "message"
            for event in self._transcripts.get(session_id, [])
        )

    def _emit(self, event: dict) -> None:
        event.setdefault("ts", _now_iso())
        session_id = event.get("session_id")
        event_type = event.get("type")
        previous_state = self._metadata.get(session_id, {}).get("state") if session_id else None
        if event_type == "agent_state" and session_id in self._metadata:
            self._metadata[session_id]["state"] = event.get("state")
        if event_type == "usage" and session_id in self._metadata:
            merged = self._usage.setdefault(session_id, {})
            for key, value in event.items():
                if key not in ("type", "session_id") and value is not None:
                    merged[key] = value
        if event_type == "config_options" and session_id in self._metadata:
            self._config_options[session_id] = event.get("options", [])
        if event_type == "commands" and session_id in self._metadata:
            self._commands[session_id] = event.get("commands", [])
        if session_id in self._metadata and event_type in _REPLAYABLE:
            self._metadata[session_id]["last_active"] = _now_iso()
            self._transcripts.setdefault(session_id, []).append(event)
            if session_id in self._persisted or self._should_persist(session_id):
                self._persist_meta(session_id)
                self.store.append_event(session_id, event)
        self.bus.publish(event)
        self._log_event(event, previous_state)
        if event_type in ("agent_state", "session_added", "session_updated", "session_removed"):
            self._report_activity()

    def _log_event(self, event: dict, previous_state: Optional[str]) -> None:
        event_type = event.get("type")
        session_id = event.get("session_id")
        name = self._metadata.get(session_id, {}).get("name") if session_id else None
        if event_type == "agent_state":
            if event.get("state") == "idle" and previous_state == "working":
                self.log.info("turn complete: session=%s name=%s", session_id, name)
            return
        if event_type == "notice":
            self.log.info("notice[%s]: session=%s msg=%s", event.get("level", "info"),
                          session_id, event.get("message"))
            return
        level = logging.INFO if event_type in _LOG_INFO_EVENTS else logging.DEBUG
        self.log.log(level, "event=%s session=%s", event_type, session_id)

    def _report_activity(self) -> None:
        busy = {
            session_id for session_id, meta in self._metadata.items()
            if meta.get("live") and meta.get("state") in ("starting", "working")
        }
        if busy == self._busy_ids:
            return
        self._busy_ids = busy
        if not busy:
            self.log.info("all sessions idle")
        else:
            running = ", ".join(
                f"{self._metadata[s]['name']} ({self._metadata[s]['state']})" for s in busy
            )
            self.log.info("%d session(s) running: %s", len(busy), running)

    def _persist_meta(self, session_id: str) -> None:
        meta = self._metadata.get(session_id)
        if meta is None or meta.get("ephemeral"):
            return
        if session_id not in self._persisted and not self._should_persist(session_id):
            return
        self._persisted.add(session_id)
        self.store.write_meta({
            "session_id": session_id,
            "name": meta["name"],
            "path": meta["path"],
            "backend": meta["backend"],
            "always_allow": True,
            "named": not self._auto_named.get(session_id, True),
            "acp_session_id": self._acp_ids.get(session_id),
            "created": meta.get("created"),
            "last_active": meta.get("last_active") or _now_iso(),
        })

    # --- metadata/config views -----------------------------------------

    def list_sessions(self, include_ephemeral: bool = False) -> list[dict]:
        sessions = [
            dict(meta) for meta in self._metadata.values()
            if include_ephemeral or not meta.get("ephemeral")
        ]
        return sorted(sessions, key=lambda item: item.get("created") or "")

    def get_session(self, session_id: str) -> dict:
        meta = self._require(session_id)
        return {**meta, "usage": self._usage.get(session_id, {})}

    def transcript(self, session_id: str) -> list[dict]:
        self._require(session_id)
        return list(self._ensure_transcript(session_id))

    def open_session(self, session_id: str) -> None:
        transcript = self.transcript(session_id)
        self._emit({"type": "transcript_reset", "session_id": session_id,
                    "transcript": transcript})

    def list_backends(self) -> dict:
        return {"backends": sorted(self.config.backends), "default": self.config.default_backend}

    def hotkeys(self) -> dict:
        return dict(self.config.hotkeys)

    def ui_config(self) -> dict:
        return dict(self.config.ui)

    def reload_config(self) -> None:
        self.config = config.load_config()
        self._emit({"type": "config_changed"})

    def _require(self, session_id: str) -> dict:
        meta = self._metadata.get(session_id)
        if meta is None:
            raise FalconFoxError(f"no such session: {session_id}")
        return meta

    # --- session lifecycle ---------------------------------------------

    async def add_session(
        self,
        path: str | Path | None = None,
        name: Optional[str] = None,
        backend_name: Optional[str] = None,
        ephemeral: bool = False,
    ) -> str:
        working_path = Path(path or Path.home()).expanduser().resolve()
        if not working_path.is_dir():
            raise FalconFoxError(f"session path is not a directory: {working_path}")
        try:
            backend = self.config.select_backend(backend_name)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        session_id = self.sessions.new_session_id()
        auto_named = not bool((name or "").strip())
        display_name = (name or "").strip() or f"Session {len(self._metadata) + 1}"
        session = AgentSession(
            session_id=session_id,
            name=display_name,
            path=working_path,
            backend=backend,
            emit=self._emit,
            request_permission=self._request_permission,
        )
        self.sessions.add(session)
        now = _now_iso()
        self._acp_ids[session_id] = None
        self._auto_named[session_id] = auto_named
        self._metadata[session_id] = {
            "session_id": session_id,
            "name": display_name,
            "path": str(working_path),
            "backend": backend.name,
            "always_allow": True,
            "ephemeral": bool(ephemeral),
            "state": "starting",
            "live": True,
            "created": now,
            "last_active": now,
        }
        self._persist_meta(session_id)
        self._emit({"type": "session_added", **self._metadata[session_id]})
        try:
            await session.start()
        except Exception as error:
            self.sessions.pop(session_id)
            self._metadata.pop(session_id, None)
            self._acp_ids.pop(session_id, None)
            self._auto_named.pop(session_id, None)
            self._persisted.discard(session_id)
            self.store.delete(session_id)
            self._emit({"type": "session_removed", "session_id": session_id})
            self._emit({"type": "notice", "session_id": session_id, "level": "error",
                        "message": f"failed to start session: {error}"})
            raise
        self._acp_ids[session_id] = session.acp_session_id
        await self._apply_config_options(session_id, session)
        self._persist_meta(session_id)
        return session_id

    async def resume_session(self, session_id: str) -> None:
        meta = self._require(session_id)
        if meta.get("live"):
            return
        path = Path(meta["path"])
        if not path.is_dir():
            raise FalconFoxError(f"session path is not a directory: {path}")
        try:
            backend = self.config.select_backend(meta["backend"] or None)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        session = AgentSession(
            session_id=session_id,
            name=meta["name"],
            path=path,
            backend=backend,
            emit=self._emit,
            request_permission=self._request_permission,
        )
        self.sessions.add(session)
        meta.update(state="starting", live=True)
        self._emit({"type": "session_updated", **meta})
        try:
            loaded = await session.resume(self._acp_ids.get(session_id))
        except Exception:
            self.sessions.pop(session_id)
            meta.update(state="stored", live=False)
            self._emit({"type": "session_updated", **meta})
            raise
        self._acp_ids[session_id] = session.acp_session_id
        await self._apply_config_options(session_id, session)
        self._persist_meta(session_id)
        transcript = self._ensure_transcript(session_id)
        self._emit({"type": "transcript_reset", "session_id": session_id,
                    "transcript": transcript})
        if not loaded and transcript:
            self._pending_context[session_id] = self._context_prompt(session_id)
            self._emit({"type": "notice", "session_id": session_id,
                        "message": "Context re-sent from saved transcript imperfectly — "
                                   "this backend has no native session loading."})

    def _context_prompt(self, session_id: str) -> str:
        body = self._transcript_text(session_id, limit=24000)
        return (
            "You are resuming a previous session that was interrupted. This backend "
            "cannot restore it natively, so below is the prior conversation. Re-read "
            "files as needed and continue from where it stopped.\n\n"
            f"=== prior conversation ===\n{body}\n=== end of prior conversation ==="
        )

    async def send(self, session_id: str, text: str) -> None:
        self._require(session_id)
        if not (text or "").strip():
            raise FalconFoxError("message must not be empty")
        if self.sessions.get(session_id) is None:
            await self.resume_session(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            raise FalconFoxError(f"could not resume session: {session_id}")
        pending = self._pending_context.pop(session_id, None)
        if pending:
            await session.send(f"{pending}\n\n=== the user's message follows ===\n{text}",
                               display_text=text)
        else:
            await session.send(text)

    async def cancel(self, session_id: str) -> None:
        self._require(session_id)
        session = self.sessions.get(session_id)
        if session is not None:
            await session.cancel()

    async def stop_session(self, session_id: str) -> None:
        meta = self._require(session_id)
        if meta.get("ephemeral") or not self._should_persist(session_id):
            await self.delete_session(session_id)
            return
        session = self.sessions.pop(session_id)
        if session is not None:
            await session.stop()
        meta.update(state="stored", live=False)
        self._config_options.pop(session_id, None)
        self._commands.pop(session_id, None)
        self._pending_context.pop(session_id, None)
        self._evict_transcript(session_id)
        self._persist_meta(session_id)
        self._emit({"type": "session_updated", **meta})

    async def delete_session(self, session_id: str) -> None:
        meta = self._require(session_id)
        self.log.info("deleting session=%s name=%s live=%s", session_id,
                      meta.get("name"), meta.get("live"))
        session = self.sessions.pop(session_id)
        if session is not None:
            await session.stop()
        self._metadata.pop(session_id, None)
        self._transcripts.pop(session_id, None)
        self._acp_ids.pop(session_id, None)
        self._config_options.pop(session_id, None)
        self._commands.pop(session_id, None)
        self._pending_context.pop(session_id, None)
        self._auto_named.pop(session_id, None)
        self._persisted.discard(session_id)
        self._usage.pop(session_id, None)
        self.store.delete(session_id)
        self._emit({"type": "session_removed", "session_id": session_id})

    def rename_session(self, session_id: str, name: str) -> None:
        meta = self._require(session_id)
        name = (name or "").strip()
        if not name:
            raise FalconFoxError("session name must not be empty")
        meta["name"] = name
        self._auto_named[session_id] = False
        self._persist_meta(session_id)
        self._emit({"type": "session_updated", **meta})

    # --- transcript utilities retained from the existing daemon --------

    async def revert_session(self, session_id: str, event_index: int) -> None:
        meta = self._require(session_id)
        transcript = self._ensure_transcript(session_id)
        if event_index < 0 or event_index >= len(transcript):
            raise FalconFoxError(f"event_index {event_index} out of range")
        target = transcript[event_index]
        if target.get("type") != "message" or target.get("role") != "user":
            raise FalconFoxError("revert target must be a user message")
        if meta.get("live"):
            session = self.sessions.pop(session_id)
            if session is not None:
                await session.stop()
            meta.update(state="stored", live=False)
        self._acp_ids[session_id] = None
        truncated = transcript[:event_index]
        self._transcripts[session_id] = truncated
        if session_id in self._persisted:
            self.store.rewrite_transcript(session_id, truncated)
            self._persist_meta(session_id)
        self._emit({"type": "session_updated", **meta})
        self._emit({"type": "transcript_reset", "session_id": session_id,
                    "transcript": truncated})

    async def fork_session(self, session_id: str, event_index: Optional[int] = None) -> str:
        source = self._require(session_id)
        transcript = list(self._ensure_transcript(session_id))
        if event_index is not None:
            if event_index < 0 or event_index > len(transcript):
                raise FalconFoxError(f"event_index {event_index} out of range")
            transcript = transcript[:event_index]
        new_id = self.sessions.new_session_id()
        now = _now_iso()
        self._metadata[new_id] = {
            **source,
            "session_id": new_id,
            "name": f"{source['name']} (fork)",
            "ephemeral": False,
            "state": "stored",
            "live": False,
            "created": now,
            "last_active": now,
        }
        self._transcripts[new_id] = transcript
        self._acp_ids[new_id] = None
        self._auto_named[new_id] = False
        self._persisted.add(new_id)
        self._persist_meta(new_id)
        self.store.rewrite_transcript(new_id, transcript)
        self._emit({"type": "session_added", **self._metadata[new_id]})
        return new_id

    async def name_session(self, session_id: str) -> None:
        meta = self._require(session_id)
        transcript_text = self._transcript_text(session_id)
        if not transcript_text.strip():
            raise FalconFoxError("nothing to name yet — the session has no messages")
        if not self.config.naming_backend:
            raise FalconFoxError("session naming is disabled — set naming_backend in config.toml")
        try:
            backend = self.config.select_backend(self.config.naming_backend)
        except KeyError as error:
            raise FalconFoxError(str(error)) from error
        prompt = f"{self.config.naming_prompt}\n\n--- transcript ---\n{transcript_text}"
        reply = await oneshot.one_shot(backend, Path(meta["path"]), prompt)
        name = _clean_name(reply)
        if name:
            self.rename_session(session_id, name)

    def _transcript_text(self, session_id: str, limit: int = 6000) -> str:
        lines = []
        for event in self._ensure_transcript(session_id):
            if event.get("type") != "message" or event.get("system"):
                continue
            if event.get("role") in ("user", "agent"):
                lines.append(f"{event['role']}: {event.get('text', '')}")
        return "\n".join(lines)[-limit:]

    # --- config options and permissions --------------------------------

    async def _apply_config_options(self, session_id: str, session: AgentSession) -> None:
        backend = self.config.select_backend(self._metadata[session_id]["backend"] or None)
        for config_id, preference in backend.config_options.items():
            option = next((o for o in session.config_options if o["id"] == config_id), None)
            if option is None:
                self._warn_option(session_id, f"backend advertises no option {config_id!r}")
                continue
            desired = resolve_config_value(option, preference)
            if desired is None:
                self._warn_option(session_id, f"configured value for {config_id!r} matched nothing")
                continue
            if desired != option["current_value"]:
                try:
                    await session.set_config_option(config_id, desired)
                except Exception as error:
                    self._warn_option(session_id, f"could not apply {config_id}: {error}")
        self._publish_config_options(session_id, session)

    def _warn_option(self, session_id: str, message: str) -> None:
        self._emit({"type": "notice", "session_id": session_id,
                    "level": "error", "message": message})

    def _publish_config_options(self, session_id: str, session: AgentSession) -> None:
        self._emit({"type": "config_options", "session_id": session_id,
                    "options": session.config_options})

    async def set_config_option(self, session_id: str, config_id: str, value) -> None:
        self._require(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            raise FalconFoxError("session must be live to change an option")
        await session.set_config_option(config_id, value)
        self._publish_config_options(session_id, session)

    async def _request_permission(self, payload: dict) -> Optional[str]:
        """Always allow when possible; empty options are an immediate denial.

        The old coordinator created an unresolved future for this edge case,
        hanging unattended clients forever. The PoC intentionally has no
        interactive permission posture.
        """
        session_id = payload.get("session_id")
        chosen = _auto_allow_option(payload.get("options", []))
        if chosen is None:
            self._emit({"type": "permission_resolved", "session_id": session_id,
                        "option_id": None, "denied": True})
            return None
        tool = payload.get("tool_call", {}).get("title") or "tool call"
        self._emit({"type": "notice", "session_id": session_id,
                    "message": f"auto-allowed: {tool}"})
        return chosen

    # --- socket state and lifecycle ------------------------------------

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "sessions": self.list_sessions(),
            "config_options": dict(self._config_options),
            "commands": dict(self._commands),
            "usage": dict(self._usage),
        }

    async def shutdown(self) -> None:
        self.log.info("coordinator shutdown: sessions=%d", len(self.sessions.all()))
        for session in self.sessions.all():
            await session.stop()
