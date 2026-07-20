"""CaseCoordinator: the casebook-specific brain over the generic engine.

Maps cases to their sessions, injects the directive as system instructions when
a session is spawned, watches case directories so user/agent edits stay visible,
and brokers the permission round-trip between an agent and the UI. The engine
below it knows nothing about cases; this layer does.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import shutil
import uuid
from pathlib import Path
from typing import Optional

from watchfiles import awatch

from . import cases, config, logsetup, storage, templates
from .engine import oneshot
from .engine.client import resolve_config_value
from .engine.events import EventBus
from .engine.session import AgentSession, SessionManager

# Event types worth replaying to a browser that connects/reloads mid-case, and
# worth persisting so a session survives a restart.
_REPLAYABLE = {"message", "tool_call", "notice", "plan", "usage"}

# Reserved case id for caseless ("scratch") sessions: no case directory, no
# directive, no file watching. Real case ids are `YYYY-MM-DD__hex`, so this never
# collides. Scratch sessions are persisted under .casebook/sessions/scratch/.
SCRATCH_CASE_ID = "scratch"

# Event types logged at INFO — the lifecycle/audit trail worth seeing at the
# default level. Everything else emitted (streaming message chunks, agent_state,
# tool_call, usage, config_options, files_changed, agent_updated) is logged at DEBUG.
# Notices and agent_state turn-boundaries get their own handling in _log_event.
_LOG_INFO_EVENTS = {
    "agent_added", "agent_removed", "case_created", "case_deleted",
    "config_changed", "permission_request", "permission_resolved",
    "transcript_reset",
}


def _now_iso() -> str:
    return datetime.datetime.now().isoformat()


def _clean_name(reply: str) -> str:
    """Reduce a model reply to a single short label."""
    first_line = reply.strip().splitlines()[0] if reply.strip() else ""
    return first_line.strip().strip("\"'").strip()[:80]


def _auto_allow_option(options: list[dict]) -> Optional[str]:
    """Pick the option to grant when a session is in always-allow mode."""
    for kind in ("allow_always", "allow_once"):
        for option in options:
            if option.get("kind") == kind:
                return option["option_id"]
    return options[0]["option_id"] if options else None


class CaseCoordinator:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.log = logsetup.get_logger(f"coordinator.{self.project_root.name}")
        self.casebook_root = self.project_root.joinpath(cases.CASEBOOK_DIR)
        self.config = config.load_config(self.project_root)
        self.store = storage.SessionStore(self.project_root)
        self.bus = EventBus()
        self.sessions = SessionManager()
        self._agents: dict[str, dict] = {}
        self._transcripts: dict[str, list[dict]] = {}
        self._acp_ids: dict[str, Optional[str]] = {}
        self._created: dict[str, Optional[str]] = {}
        # All config options (model, reasoning effort, toggles) per session, kept in
        # sync from new/load responses and live config_option_update events.
        self._config_options: dict[str, list[dict]] = {}
        # Slash commands the backend advertises, per session. Unlike config
        # options these arrive only via a live `available_commands_update` (never
        # in a session response), so the coordinator cache is the single source —
        # used for UI discoverability; invocation is plain prompt text.
        self._commands: dict[str, list[dict]] = {}
        # Transcript to prepend to a resumed session's next message, for backends
        # that lack native session/load. Keyed by agent_id, consumed once.
        self._pending_context: dict[str, str] = {}
        # Whether a session still has its auto-assigned name, and which sessions
        # have been written to disk. A session with no messages and an auto name is
        # "trivial" — identical to a brand-new one — and is never persisted.
        self._auto_named: dict[str, bool] = {}
        self._persisted: set[str] = set()
        # Latest usage (context size/used, token totals, cost) per session, merged
        # from ACP usage updates and prompt responses.
        self._usage: dict[str, dict] = {}
        # Which sessions are currently busy, for console activity reporting.
        self._busy_ids: set[str] = set()
        self._permissions: dict[str, tuple[asyncio.Future, Optional[str]]] = {}
        self._watchers: dict[str, tuple[asyncio.Task, asyncio.Event]] = {}

    def load_persisted(self) -> None:
        """Restore every session on disk as a (non-live) stored session.

        Only metadata is read here — transcripts are loaded lazily when a session
        is opened (see _ensure_transcript), so this stays cheap no matter how many
        stored sessions have accumulated. Everything on disk is loaded as-is —
        startup never deletes sessions. (New trivial sessions simply aren't written
        in the first place; see _emit.)
        """
        for meta in self.store.load_all_meta():
            agent_id = meta["agent_id"]
            named = bool(meta.get("named", False))
            self._agents[agent_id] = {
                "agent_id": agent_id,
                "case_id": meta["case_id"],
                "label": meta.get("label", agent_id),
                "backend": meta.get("backend", ""),
                "always_allow": bool(meta.get("always_allow", False)),
                "state": "stored",
                "live": False,
                "last_active": meta.get("last_active") or meta.get("created"),
            }
            self._acp_ids[agent_id] = meta.get("acp_session_id")
            self._created[agent_id] = meta.get("created")
            self._auto_named[agent_id] = not named
            self._persisted.add(agent_id)
            # Transcript intentionally left unloaded — read on first open.

    def _ensure_transcript(self, agent_id: str) -> list[dict]:
        """Return a session's transcript, reading it from disk on first access.

        Stored sessions carry only metadata in memory (see load_persisted); their
        transcript is loaded lazily the moment something needs it — opening the
        session, forking, reverting, or naming it. Live sessions already hold their
        transcript, so this is a no-op for them.
        """
        transcript = self._transcripts.get(agent_id)
        if transcript is None:
            agent = self._agents.get(agent_id)
            transcript = (
                self.store.read_transcript(agent["case_id"], agent_id)
                if agent is not None
                else []
            )
            self._transcripts[agent_id] = transcript
        return transcript

    def _evict_transcript(self, agent_id: str) -> None:
        """Drop a stored session's transcript from memory; disk stays the truth.

        Called when a session collapses back to stored, so a long-running daemon
        that opens many sessions over its lifetime doesn't accumulate every
        transcript it has ever touched in memory.
        """
        self._transcripts.pop(agent_id, None)

    def _should_persist(self, agent_id: str) -> bool:
        """A session is worth saving once it has a message or a custom name."""
        if not self._auto_named.get(agent_id, True):
            return True
        return any(
            event.get("type") == "message"
            for event in self._transcripts.get(agent_id, [])
        )

    # --- single emit choke point: record, persist, then publish ----------
    def _emit(self, event: dict) -> None:
        event.setdefault("ts", _now_iso())
        agent_id = event.get("agent_id")
        event_type = event.get("type")
        # Captured before the agent_state overwrite below so _log_event can
        # detect the working -> idle boundary (one "turn complete" line per reply).
        previous_state = self._agents.get(agent_id, {}).get("state") if agent_id else None
        if event_type == "agent_state" and agent_id in self._agents:
            self._agents[agent_id]["state"] = event.get("state")
        if event_type == "usage" and agent_id in self._agents:
            merged = self._usage.setdefault(agent_id, {})
            for key, value in event.items():
                if key not in ("type", "agent_id", "case_id") and value is not None:
                    merged[key] = value
        # Single store for config options — whether the event came from a session
        # open (_publish_config_options) or a live update the client forwarded.
        if event_type == "config_options" and agent_id in self._agents:
            self._config_options[agent_id] = event.get("options", [])
        # Advertised slash commands, forwarded live by the client for discoverability.
        if event_type == "commands" and agent_id in self._agents:
            self._commands[agent_id] = event.get("commands", [])
        if agent_id in self._agents and event_type in _REPLAYABLE:
            self._agents[agent_id]["last_active"] = _now_iso()
            self._transcripts.setdefault(agent_id, []).append(event)
            # Only commit to disk once the session is non-trivial; the first real
            # content writes the metadata so meta.toml and the transcript stay paired.
            if agent_id in self._persisted or self._should_persist(agent_id):
                self._persist_meta(agent_id)
                self.store.append_event(self._agents[agent_id]["case_id"], agent_id, event)
        self.bus.publish(event)
        self._log_event(event, event_type, previous_state)
        if event_type in ("agent_state", "agent_added", "agent_updated", "agent_removed"):
            self._report_activity()

    def _log_event(self, event: dict, event_type: Optional[str],
                   previous_state: Optional[str]) -> None:
        """Write one audit line for an emitted event (see _LOG_INFO_EVENTS)."""
        agent_id = event.get("agent_id")
        case_id = event.get("case_id")
        label = self._agents.get(agent_id, {}).get("label") if agent_id else None
        if event_type == "agent_state":
            state = event.get("state")
            # A working -> idle transition is one agent reply finishing: the
            # "message received" marker (the matching "sent" is the send action).
            if state == "idle" and previous_state == "working":
                self.log.info("turn complete: agent=%s label=%s case=%s",
                              agent_id, label, case_id)
            else:
                self.log.debug("agent_state=%s agent=%s label=%s", state, agent_id, label)
            return
        if event_type == "notice":
            self.log.info("notice[%s]: agent=%s case=%s msg=%s",
                          event.get("level", "info"), agent_id, case_id,
                          event.get("message"))
            return
        level = logging.INFO if event_type in _LOG_INFO_EVENTS else logging.DEBUG
        detail = ""
        if event_type == "agent_added":
            detail = f" label={label!r} backend={event.get('backend')}"
        elif event_type == "case_created":
            detail = f" title={event.get('title')!r}"
        self.log.log(level, "event=%s agent=%s case=%s%s",
                     event_type, agent_id, case_id, detail)

    def _report_activity(self) -> None:
        """Log a line when the set of busy sessions changes.

        Lets you glance at the terminal running `casebook --fg` (or the log file)
        before Ctrl+C to see whether any session is still working.
        """
        busy = {
            agent_id
            for agent_id, agent in self._agents.items()
            if agent.get("live") and agent.get("state") in ("starting", "working")
        }
        if busy == self._busy_ids:
            return
        self._busy_ids = busy
        if not busy:
            self.log.info("all sessions idle")
        else:
            running = ", ".join(
                f"{self._agents[a]['label']} ({self._agents[a]['state']})" for a in busy
            )
            self.log.info("%d session(s) running: %s", len(busy), running)

    def _persist_meta(self, agent_id: str) -> None:
        if agent_id not in self._agents:
            return
        if agent_id not in self._persisted and not self._should_persist(agent_id):
            return  # trivial session: keep it off disk entirely
        self._persisted.add(agent_id)
        agent = self._agents[agent_id]
        self.store.write_meta(
            {
                "agent_id": agent_id,
                "case_id": agent["case_id"],
                "label": agent["label"],
                "backend": agent["backend"],
                "always_allow": agent.get("always_allow", False),
                "named": not self._auto_named.get(agent_id, True),
                "acp_session_id": self._acp_ids.get(agent_id),
                "created": self._created.get(agent_id),
                "last_active": agent.get("last_active") or _now_iso(),
            }
        )

    # --- cases (read-only views for the UI) ------------------------------
    def list_cases(self) -> list[dict]:
        return [self._case_summary(case) for case in cases.list_cases(self.casebook_root)]

    def list_backends(self) -> dict:
        return {
            "backends": sorted(self.config.backends),
            "default": self.config.default_backend,
        }

    def hotkeys(self) -> dict:
        return dict(self.config.hotkeys)

    def ui_config(self) -> dict:
        # project_name is runtime, not on-disk config — injected here so the
        # frontend (which already fetches /ui on every scoped page) can title tabs.
        return {**dict(self.config.ui), "project_name": self.project_root.name}

    def reload_config(self) -> None:
        """Re-read config from disk and notify connected frontends."""
        self.log.info("reloading config from disk")
        self.config = config.load_config(self.project_root)
        self._emit({"type": "config_changed"})

    def create_case(self, title: str) -> dict:
        """Create a case on disk and announce it so open browsers refresh."""
        case = cases.create_case(self.casebook_root, title or "Unnamed case")
        summary = self._case_summary(case)
        self._emit({"type": "case_created", **summary})
        return summary

    async def delete_case(self, case_id: str) -> None:
        """Delete a case: stop and erase its sessions, then remove the directory."""
        case = cases.resolve_case(self.casebook_root, case_id)
        cid = case.case_id
        doomed = [aid for aid, a in self._agents.items() if a["case_id"] == cid]
        # Destructive and irreversible (rmtree below): log it plainly before the
        # fact, with the session count, so an accidental delete leaves a trace.
        self.log.info("deleting case=%s (%d session(s)) at %s", cid, len(doomed), case.path)
        for agent_id in doomed:
            await self.delete_agent(agent_id)
        watcher = self._watchers.pop(cid, None)
        if watcher is not None:
            watcher[1].set()
        shutil.rmtree(case.path)
        self._emit({"type": "case_deleted", "case_id": cid})

    def case_detail(self, case_id: str) -> dict:
        case = cases.resolve_case(self.casebook_root, case_id)
        detail = self._case_summary(case)
        detail["files"] = case.files()
        detail["agents"] = [
            agent for agent in self._agents.values() if agent["case_id"] == case.case_id
        ]
        return detail

    def read_case_file(self, case_id: str, filename: str) -> str:
        case = cases.resolve_case(self.casebook_root, case_id)
        target = case.path.joinpath(filename).resolve()
        if target.parent != case.path.resolve():
            raise cases.CasebookError("file is not in the case directory")
        return target.read_text()

    def _case_summary(self, case: cases.Case) -> dict:
        case_agents = [a for a in self._agents.values() if a["case_id"] == case.case_id]
        last_active = max(
            (a.get("last_active") or "" for a in case_agents),
            default=case.metadata.get("created") or "",
        ) or case.metadata.get("created")
        return {
            "case_id": case.case_id,
            "title": case.title,
            "status": case.status,
            "keywords": case.keywords,
            "created": case.metadata.get("created"),
            "last_active": last_active,
            "sessions": len(case_agents),
            "hidden": case.hidden,
        }

    # --- agents ----------------------------------------------------------
    async def add_agent(
        self,
        case_id: str,
        label: Optional[str] = None,
        backend_name: Optional[str] = None,
    ) -> str:
        scratch = case_id == SCRATCH_CASE_ID
        case = None if scratch else cases.resolve_case(self.casebook_root, case_id)
        cid = SCRATCH_CASE_ID if scratch else case.case_id
        backend = self.config.select_backend(backend_name)
        existing = sum(1 for a in self._agents.values() if a["case_id"] == cid)
        agent_id = self.sessions.new_agent_id()
        label = label or f"Session {existing + 1}"
        session = AgentSession(
            agent_id=agent_id,
            label=label,
            case_id=cid,
            project_root=self.project_root,
            backend=backend,
            emit=self._emit,
            request_permission=self._request_permission,
        )
        self.sessions.add(session)
        now = _now_iso()
        self._created[agent_id] = now
        self._acp_ids[agent_id] = None
        self._auto_named[agent_id] = True
        self._agents[agent_id] = {
            "agent_id": agent_id,
            "case_id": cid,
            "label": label,
            "backend": backend.name,
            "always_allow": self.config.default_always_allow,
            "state": "starting",
            "live": True,
            "last_active": now,
        }
        if case is not None:
            self._watch_case(case)
        self._persist_meta(agent_id)
        self._emit({"type": "agent_added", **self._agents[agent_id]})
        try:
            await session.start()
        except Exception as error:
            self.sessions.pop(agent_id)
            self._agents.pop(agent_id, None)
            self._acp_ids.pop(agent_id, None)
            self._created.pop(agent_id, None)
            self._auto_named.pop(agent_id, None)
            self._persisted.discard(agent_id)
            self.store.delete(cid, agent_id)
            self._emit({"type": "agent_removed", "agent_id": agent_id, "case_id": cid})
            self.log.debug("start failed for agent=%s", agent_id, exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id, "case_id": cid,
                        "level": "error", "message": f"failed to start agent: {error}"})
            raise
        self._acp_ids[agent_id] = session.acp_session_id
        await self._apply_config_options(agent_id, session)
        # Case sessions get the directive prepended to their first message; a
        # caseless (scratch) session gets nothing — it's a plain agent.
        if not scratch:
            self._pending_context[agent_id] = templates.system_instructions(cid)
        self._persist_meta(agent_id)
        return agent_id

    async def resume_agent(self, agent_id: str) -> None:
        """Bring a stored session back to life (idempotent if already live)."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise cases.CasebookError(f"no such session: {agent_id}")
        if agent.get("live"):
            return
        scratch = agent["case_id"] == SCRATCH_CASE_ID
        case = None if scratch else cases.resolve_case(self.casebook_root, agent["case_id"])
        try:
            backend = self.config.select_backend(agent["backend"] or None)
        except KeyError as error:
            self.log.debug("resume backend lookup failed for agent=%s", agent_id,
                           exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": f"cannot resume session: {error}"})
            raise
        session = AgentSession(
            agent_id=agent_id,
            label=agent["label"],
            case_id=agent["case_id"],
            project_root=self.project_root,
            backend=backend,
            emit=self._emit,
            request_permission=self._request_permission,
        )
        self.sessions.add(session)
        agent["state"] = "starting"
        agent["live"] = True
        if case is not None:
            self._watch_case(case)
        self._emit({"type": "agent_updated", **agent})
        try:
            loaded = await session.resume(self._acp_ids.get(agent_id))
        except Exception as error:
            self.sessions.pop(agent_id)
            agent["state"] = "stored"
            agent["live"] = False
            self._emit({"type": "agent_updated", **agent})
            self.log.debug("resume failed for agent=%s", agent_id, exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": f"failed to resume session: {error}"})
            raise
        self._acp_ids[agent_id] = session.acp_session_id
        await self._apply_config_options(agent_id, session)
        self._persist_meta(agent_id)
        # The client drops a stored session's transcript to keep connect light, so
        # replay it now that the session is opening and its pane exists. Loading it
        # here also primes _context_prompt below for non-natively-resumed backends.
        transcript = self._ensure_transcript(agent_id)
        self._emit({"type": "transcript_reset", "agent_id": agent_id,
                    "case_id": agent["case_id"], "transcript": transcript})
        if not loaded:
            # No native session/load: re-send the saved transcript as context on
            # the next message (prefixed with the directive for case sessions).
            prefix = "" if scratch else f"{templates.system_instructions(agent['case_id'])}\n\n"
            self._pending_context[agent_id] = f"{prefix}{self._context_prompt(agent_id)}"
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "message": "Context re-sent from saved transcript "
                                   "imperfectly — this backend has no native "
                                   "session loading."})

    def _context_prompt(self, agent_id: str) -> str:
        """Frame the saved transcript as context for a non-natively-resumed session."""
        body = self._transcript_text(agent_id, limit=24000)
        return (
            "You are resuming a previous session that was interrupted. This backend "
            "cannot restore it natively, so below is a transcript of the prior "
            "conversation, for context. Tool calls and file edits from before are "
            "not included — re-read files as needed. Continue from where this left "
            "off.\n\n=== prior conversation ===\n"
            f"{body}\n=== end of prior conversation ==="
        )

    async def promote_agent(self, agent_id: str, title: str) -> Optional[str]:
        """Promote a caseless (scratch) session into a new case, migrating it.

        Creates the case, moves the session's on-disk data into it, and re-tags the
        live session — the subprocess keeps running. Returns the new case id (or
        None if the session isn't a scratch session).
        """
        agent = self._agents.get(agent_id)
        if agent is None or agent["case_id"] != SCRATCH_CASE_ID:
            return None
        case = cases.create_case(self.casebook_root, title or "Unnamed case")
        new_cid = case.case_id
        self.store.relocate(SCRATCH_CASE_ID, new_cid, agent_id)
        agent["case_id"] = new_cid
        session = self.sessions.get(agent_id)
        if session is not None:
            session.retag(new_cid)
        # Queue the casebook directive so the agent learns about its case on the
        # next user message (the subprocess was started without one).
        self._pending_context[agent_id] = templates.system_instructions(new_cid)
        self._watch_case(case)
        self._persist_meta(agent_id)  # rewrite meta.toml under the new case dir
        # Drop the pane from scratch views; refresh the home cases list.
        self._emit({"type": "agent_removed", "agent_id": agent_id,
                    "case_id": SCRATCH_CASE_ID})
        self._emit({"type": "case_created", **self._case_summary(case)})
        return new_cid

    async def revert_agent(self, agent_id: str, event_index: int) -> None:
        """Revert a session's transcript to just before `event_index`.

        The event at `event_index` must be a user message — everything from that
        point onward is discarded. If the session is live, the ACP subprocess is
        torn down (its history is now stale); the session becomes stored and can
        be resumed, which re-sends the truncated transcript as context.
        """
        agent = self._agents.get(agent_id)
        if agent is None:
            raise cases.CasebookError(f"no such session: {agent_id}")
        transcript = self._ensure_transcript(agent_id)
        if event_index < 0 or event_index >= len(transcript):
            raise cases.CasebookError(f"event_index {event_index} out of range")
        target = transcript[event_index]
        if target.get("type") != "message" or target.get("role") != "user":
            raise cases.CasebookError("revert target must be a user message")
        # Tear down the live ACP session — its history no longer matches.
        if agent.get("live"):
            session = self.sessions.pop(agent_id)
            if session is not None:
                await session.stop()
            agent["state"] = "stored"
            agent["live"] = False
            self._config_options.pop(agent_id, None)
            self._commands.pop(agent_id, None)
            self._pending_context.pop(agent_id, None)
        # Force a fresh ACP session on resume — the old one has stale history.
        self._acp_ids[agent_id] = None
        # Truncate in memory and rewrite on disk.
        truncated = transcript[:event_index]
        self._transcripts[agent_id] = truncated
        case_id = agent["case_id"]
        if agent_id in self._persisted:
            self.store.rewrite_transcript(case_id, agent_id, truncated)
            self._persist_meta(agent_id)
        self._emit({"type": "agent_updated", **agent})
        self._emit({"type": "transcript_reset", "agent_id": agent_id,
                     "case_id": case_id, "transcript": truncated})

    async def fork_agent(self, agent_id: str, event_index: Optional[int] = None) -> str:
        """Duplicate a session (optionally truncated) into a new stored session.

        If `event_index` is given, the fork's transcript is truncated to
        events[:event_index] (same semantics as revert — everything from that
        user message onward is excluded). The new session starts in stored state.
        """
        source = self._agents.get(agent_id)
        if source is None:
            raise cases.CasebookError(f"no such session: {agent_id}")
        transcript = list(self._ensure_transcript(agent_id))
        if event_index is not None:
            if event_index < 0 or event_index > len(transcript):
                raise cases.CasebookError(f"event_index {event_index} out of range")
            transcript = transcript[:event_index]
        case_id = source["case_id"]
        new_agent_id = self.sessions.new_agent_id()
        label = f"{source['label']} (fork)"
        now = _now_iso()
        # Register in coordinator state.
        self._agents[new_agent_id] = {
            "agent_id": new_agent_id,
            "case_id": case_id,
            "label": label,
            "backend": source["backend"],
            "always_allow": source.get("always_allow", False),
            "state": "stored",
            "live": False,
            "last_active": now,
        }
        self._transcripts[new_agent_id] = transcript
        self._acp_ids[new_agent_id] = None  # fresh session on resume
        self._created[new_agent_id] = now
        self._auto_named[new_agent_id] = False  # custom name, always persist
        self._persisted.add(new_agent_id)
        # Write to disk.
        self.store.write_meta({
            "agent_id": new_agent_id,
            "case_id": case_id,
            "label": label,
            "backend": source["backend"],
            "always_allow": source.get("always_allow", False),
            "named": True,
            "acp_session_id": None,
            "created": now,
            "last_active": now,
        })
        self.store.rewrite_transcript(case_id, new_agent_id, transcript)
        self._emit({"type": "agent_added", **self._agents[new_agent_id]})
        # Send the full transcript so the frontend can display it immediately.
        self._emit({"type": "transcript_reset", "agent_id": new_agent_id,
                     "case_id": case_id, "transcript": transcript})
        return new_agent_id

    async def _apply_config_options(self, agent_id: str, session: AgentSession) -> None:
        """Apply the backend's [config_options] defaults, then publish the option
        list. Model is just the option keyed `model` — no special case."""
        agent = self._agents[agent_id]
        backend = self.config.select_backend(agent["backend"] or None)
        for config_id, preference in backend.config_options.items():
            await self._apply_default_option(agent_id, session, config_id, preference)
        self._publish_config_options(agent_id, session)

    async def _apply_default_option(self, agent_id: str, session: AgentSession,
                                    config_id: str, preference) -> None:
        """Set one configured default, warning (not failing) if it doesn't apply."""
        agent = self._agents[agent_id]
        option = next((o for o in session.config_options if o["id"] == config_id), None)
        if option is None:
            offered = ", ".join(o["id"] for o in session.config_options) or "(none)"
            self._warn_option(agent_id, agent["case_id"],
                              f"config_options.{config_id} is set, but this backend advertises "
                              f"no such option. Available: {offered}")
            return
        desired = resolve_config_value(option, preference)
        if desired is None:
            if option.get("type") == "boolean":
                detail = "expected true or false"
            else:
                offered = ", ".join(choice["value"] for choice in option.get("options") or [])
                detail = f"expected one of: {offered}"
            self._warn_option(agent_id, agent["case_id"],
                              f"config_options.{config_id} = {preference!r} matched nothing — {detail}")
            return
        if desired != option["current_value"]:
            try:
                await session.set_config_option(config_id, desired)
            except Exception as error:
                self.log.debug("default option %s failed for agent=%s", config_id,
                               agent_id, exc_info=True)
                self._warn_option(agent_id, agent["case_id"],
                                  f"could not apply config_options.{config_id}: {error}")

    def _warn_option(self, agent_id: str, case_id: str, message: str) -> None:
        self.log.debug("config default for agent=%s: %s", agent_id, message)
        self._emit({"type": "notice", "agent_id": agent_id, "case_id": case_id,
                    "level": "error", "message": message})

    def _publish_config_options(self, agent_id: str, session: AgentSession) -> None:
        """Store (via _emit) and broadcast a session's current config options."""
        self._emit({"type": "config_options", "agent_id": agent_id,
                    "case_id": self._agents[agent_id]["case_id"],
                    "options": session.config_options})

    async def set_config_option(self, agent_id: str, config_id: str, value) -> None:
        session = self.sessions.get(agent_id)
        agent = self._agents.get(agent_id)
        if session is None or agent is None:
            return
        try:
            await session.set_config_option(config_id, value)
        except Exception as error:
            self.log.debug("set_config_option failed for agent=%s id=%s",
                           agent_id, config_id, exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": f"could not set option: {error}"})
            return
        # Config options are live session state, re-read from the agent each
        # session — not persisted (nothing to write to meta here).
        self._publish_config_options(agent_id, session)

    def rename_agent(self, agent_id: str, label: str) -> None:
        agent = self._agents.get(agent_id)
        label = (label or "").strip()
        if agent is None or not label:
            return
        agent["label"] = label
        self._auto_named[agent_id] = False  # a custom name makes it worth keeping
        self._persist_meta(agent_id)
        self._emit({"type": "agent_updated", **agent})
        self._emit({"type": "notice", "agent_id": agent_id,
                    "case_id": agent["case_id"],
                    "message": f"named: {label}"})

    async def name_agent(self, agent_id: str) -> None:
        """Ask the model to name a session from its transcript (configurable prompt)."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise cases.CasebookError(f"no such session: {agent_id}")
        transcript_text = self._transcript_text(agent_id)
        if not transcript_text.strip():
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": "nothing to name yet — the session has no messages"})
            return
        # Naming needs an explicit backend; without one the feature is disabled.
        # That backend's model (and any option) comes from its own config_options.
        naming_backend = self.config.naming_backend
        if not naming_backend:
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": "session naming is disabled — set "
                                   "naming_backend in config.toml to enable it"})
            return
        try:
            backend = self.config.select_backend(naming_backend)
        except KeyError as error:
            self.log.debug("naming backend lookup failed for agent=%s", agent_id,
                           exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": f"cannot name session: {error}"})
            return
        prompt = f"{self.config.naming_prompt}\n\n--- transcript ---\n{transcript_text}"
        self._emit({"type": "notice", "agent_id": agent_id,
                    "case_id": agent["case_id"], "message": "autonaming session…"})
        try:
            reply = await oneshot.one_shot(backend, self.project_root, prompt)
        except Exception as error:
            self.log.debug("naming query failed for agent=%s", agent_id, exc_info=True)
            self._emit({"type": "notice", "agent_id": agent_id,
                        "case_id": agent["case_id"],
                        "level": "error", "message": f"naming failed: {error}"})
            return
        name = _clean_name(reply)
        if name:
            self.rename_agent(agent_id, name)

    def _transcript_text(self, agent_id: str, limit: int = 6000) -> str:
        """Plain user/agent text of a session, most recent `limit` characters."""
        lines = []
        for event in self._ensure_transcript(agent_id):
            if event.get("type") != "message" or event.get("system"):
                continue
            role = event.get("role")
            if role in ("user", "agent"):
                lines.append(f"{role}: {event.get('text', '')}")
        return "\n".join(lines)[-limit:]

    async def send(self, agent_id: str, text: str) -> None:
        session = self.sessions.get(agent_id)
        if session is None:
            raise cases.CasebookError(f"no such live session: {agent_id}")
        pending = self._pending_context.pop(agent_id, None)
        if pending:
            # Attach the re-sent transcript to the agent's turn, but show only the
            # user's own message in the transcript (the history is already visible).
            await session.send(
                f"{pending}\n\n=== the user's message follows ===\n{text}",
                display_text=text,
            )
        else:
            await session.send(text)

    async def cancel(self, agent_id: str) -> None:
        session = self.sessions.get(agent_id)
        if session is not None:
            await session.cancel()

    async def close_agent(self, agent_id: str) -> None:
        """Collapse a session to stored, or discard it if it's trivial.

        A session with no messages and an auto name is identical to a brand-new
        one, so closing it deletes it rather than leaving a pointless resumable.
        """
        if agent_id in self._agents and not self._should_persist(agent_id):
            await self.delete_agent(agent_id)
            return
        session = self.sessions.pop(agent_id)
        agent = self._agents.get(agent_id)
        if session is not None:
            await session.stop()
        if agent is not None:
            agent["state"] = "stored"
            agent["live"] = False
            self._config_options.pop(agent_id, None)
            self._commands.pop(agent_id, None)
            self._pending_context.pop(agent_id, None)
            self._evict_transcript(agent_id)
            self._persist_meta(agent_id)
            self._emit({"type": "agent_updated", **agent})

    async def delete_agent(self, agent_id: str) -> None:
        """Stop (if live) and erase the session and its stored history."""
        existing = self._agents.get(agent_id)
        if existing is not None:
            # Destructive (drops the on-disk transcript): log with context before
            # the state is torn down. Covers WS, REST, and cascade-from-delete_case.
            self.log.info("deleting session=%s label=%s case=%s live=%s",
                          agent_id, existing.get("label"), existing.get("case_id"),
                          existing.get("live"))
        session = self.sessions.pop(agent_id)
        agent = self._agents.pop(agent_id, None)
        self._transcripts.pop(agent_id, None)
        self._acp_ids.pop(agent_id, None)
        self._created.pop(agent_id, None)
        self._config_options.pop(agent_id, None)
        self._commands.pop(agent_id, None)
        self._pending_context.pop(agent_id, None)
        self._auto_named.pop(agent_id, None)
        self._persisted.discard(agent_id)
        self._usage.pop(agent_id, None)
        if session is not None:
            await session.stop()
        if agent is not None:
            self.store.delete(agent["case_id"], agent_id)
            self._emit({"type": "agent_removed", "agent_id": agent_id,
                        "case_id": agent["case_id"]})

    def set_always_allow(self, agent_id: str, value: bool) -> None:
        agent = self._agents.get(agent_id)
        if agent is None:
            return
        agent["always_allow"] = bool(value)
        self._persist_meta(agent_id)
        self._emit({"type": "agent_updated", **agent})

    # --- permission round-trip (agent waits on the user) -----------------
    async def _request_permission(self, payload: dict) -> Optional[str]:
        agent_id = payload.get("agent_id")
        if agent_id and self._agents.get(agent_id, {}).get("always_allow"):
            chosen = _auto_allow_option(payload.get("options", []))
            if chosen is not None:
                tool = payload.get("tool_call", {}).get("title") or "tool call"
                self._emit({"type": "notice", "agent_id": agent_id,
                            "case_id": payload.get("case_id"),
                            "message": f"auto-allowed (always allow): {tool}"})
                return chosen
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._permissions[request_id] = (future, payload.get("agent_id"))
        self._emit({"type": "permission_request", "request_id": request_id, **payload})
        try:
            return await future
        finally:
            self._permissions.pop(request_id, None)

    def resolve_permission(self, request_id: str, option_id: Optional[str]) -> None:
        entry = self._permissions.get(request_id)
        if entry is not None:
            future, agent_id = entry
            if not future.done():
                future.set_result(option_id)
        else:
            agent_id = None
        self._emit({"type": "permission_resolved", "request_id": request_id,
                     "agent_id": agent_id, "option_id": option_id})

    # --- filesystem watching --------------------------------------------
    def _watch_case(self, case: cases.Case) -> None:
        if case.case_id in self._watchers:
            return
        stop = asyncio.Event()
        task = asyncio.create_task(self._watch_loop(case.case_id, case.path, stop))
        self._watchers[case.case_id] = (task, stop)

    async def _watch_loop(self, case_id: str, case_path: Path, stop: asyncio.Event) -> None:
        try:
            async for _changes in awatch(case_path, stop_event=stop):
                case = cases.load_case(case_path)
                self._emit({"type": "files_changed", "case_id": case_id,
                            "files": case.files()})
        except Exception as error:
            self.log.debug("file watcher stopped for case=%s", case_id, exc_info=True)
            self._emit({"type": "notice", "case_id": case_id,
                        "level": "error", "message": f"file watcher stopped: {error}"})

    # --- lifecycle / reconnection ---------------------------------------
    def snapshot(self, case_id: Optional[str] = None) -> dict:
        """State for a connecting browser, scoped to the case it is viewing.

        A session page passes its case_id and gets only that case's sessions —
        crucially, only their transcripts, which are the bulk of the payload.
        The home/project pages pass None: they render no sessions, so an empty
        snapshot is enough (they still receive live case_created/deleted events).
        """
        if case_id is None:
            agents = []
        else:
            agents = [a for a in self._agents.values() if a["case_id"] == case_id]
        agent_ids = {a["agent_id"] for a in agents}
        return {
            "type": "snapshot",
            "agents": agents,
            "transcripts": {
                aid: events
                for aid, events in self._transcripts.items()
                if aid in agent_ids
            },
            "config_options": {aid: o for aid, o in self._config_options.items()
                               if aid in agent_ids},
            "commands": {aid: c for aid, c in self._commands.items()
                         if aid in agent_ids},
            "usage": {aid: u for aid, u in self._usage.items() if aid in agent_ids},
        }

    async def shutdown(self) -> None:
        self.log.info("coordinator shutdown: watchers=%d sessions=%d",
                      len(self._watchers), len(self.sessions.all()))
        for _task, stop in self._watchers.values():
            stop.set()
        for session in self.sessions.all():
            await session.stop()
