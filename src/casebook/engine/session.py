"""Agent sessions: one ACP subprocess per agent, and a manager over the set.

Design choice (see decisions doc): each agent is its *own* subprocess with its
own ACP connection and single session. This gives true concurrency and
independent lifecycles for the multiple agents that may work one case at once.
They are deliberately uncoordinated with each other — they sync through the
filesystem, never through a shared connection. The cost is one node process per
agent; acceptable, and swappable later for shared-connection multiplexing.
"""

from __future__ import annotations

import os
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from acp import PROTOCOL_VERSION, spawn_agent_process, text_block
from acp.interfaces import ClientCapabilities, Implementation
from acp.schema import FileSystemCapabilities

from .. import logsetup
from ..config import Backend
from .client import AgentClient, Emit, PermissionRequester, capture_config_options

log = logsetup.get_logger("engine.session")

CLIENT_CAPABILITIES = ClientCapabilities(
    fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
    terminal=False,
)


@dataclass
class AgentSession:
    agent_id: str
    label: str
    case_id: str
    project_root: Path
    backend: Backend
    emit: Emit
    request_permission: PermissionRequester

    _stack: AsyncExitStack = field(default_factory=AsyncExitStack, init=False)
    _conn: Any = field(default=None, init=False)
    _client: Any = field(default=None, init=False)
    _acp_session_id: Optional[str] = field(default=None, init=False)
    _busy: bool = field(default=False, init=False)
    _supports_load: bool = field(default=False, init=False)
    _suppress_emit: bool = field(default=False, init=False)
    # Every option the backend advertises via ACP `configOptions` — model,
    # reasoning effort, mode, toggles — all treated identically (the model is not
    # special; see client.capture_config_options).
    config_options: list[dict] = field(default_factory=list, init=False)

    @property
    def acp_session_id(self) -> Optional[str]:
        return self._acp_session_id

    def retag(self, case_id: str) -> None:
        """Move a live session to a different case (used when promoting a scratch
        session). Future events carry the new case id."""
        self.case_id = case_id
        if self._client is not None:
            self._client.case_id = case_id

    def _capture_config_options(self, response: Any) -> None:
        """Record all advertised options (model, reasoning effort, toggles, …).

        Normalized to the same dict shape a live `config_option_update` produces,
        so both feed one UI path.
        """
        self.config_options = capture_config_options(response)

    async def set_config_option(self, config_id: str, value: Any) -> None:
        if self._conn is None or self._acp_session_id is None:
            return
        # ACP requires the agent to echo back the full, updated option list, so
        # adopt that as authoritative rather than assuming our value stuck — the
        # agent may clamp or adjust related options.
        response = await self._conn.set_config_option(
            config_id=config_id, value=value, session_id=self._acp_session_id
        )
        self._capture_config_options(response)

    def _guarded_emit(self, event: dict) -> None:
        # Dropped while replaying a loaded session — that history is already on
        # disk, so re-emitting it would duplicate the transcript.
        if not self._suppress_emit:
            self.emit(event)

    async def _spawn(self) -> None:
        """Spawn the subprocess, initialize the connection, note its capabilities."""
        client = AgentClient(
            self.agent_id,
            self.case_id,
            self.project_root,
            self._guarded_emit,
            self.request_permission,
        )
        self._client = client
        # The backend is the user's own trusted agent; pass the full environment
        # (not the trimmed MCP default) so it keeps PATH and ambient auth.
        environment = {**os.environ, **self.backend.env}
        command, *args = self.backend.command
        # The command is logged before the spawn so that if it fails (backend not
        # on PATH, non-executable, immediate crash) the log names exactly what was
        # attempted — the single most common backend-setup failure.
        log.debug("spawning backend %s: %s (cwd=%s)",
                  self.backend.name, [command, *args], self.project_root)
        conn, _process = await self._stack.enter_async_context(
            spawn_agent_process(
                client,
                command,
                *args,
                cwd=str(self.project_root),
                env=environment,
                # asyncio's default StreamReader limit (64 KiB) is far too
                # small for JSON-RPC messages carrying file contents —
                # exceeding it crashes the connection. 100 MiB is well beyond
                # any realistic single message while still capping a buggy
                # subprocess before it can exhaust memory.
                transport_kwargs={"limit": 100 * 1024 * 1024},
            )
        )
        self._conn = conn
        initialized = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=CLIENT_CAPABILITIES,
            client_info=Implementation(name="casebook", version="0.1.0"),
        )
        capabilities = getattr(initialized, "agent_capabilities", None)
        self._supports_load = bool(getattr(capabilities, "load_session", False))
        log.debug("backend %s initialized (agent=%s, load_session=%s)",
                  self.backend.name, self.agent_id, self._supports_load)

    async def start(self) -> None:
        """Spawn the agent and open a fresh session, ready and idle.

        No prompt is sent: casebook does not query the agent on start. The
        directive is prepended to the user's first message by the coordinator, so
        a brand-new session doesn't speak until the user does.
        """
        await self._spawn()
        session = await self._conn.new_session(
            cwd=str(self.project_root), mcp_servers=[]
        )
        self._acp_session_id = session.session_id
        self._capture_config_options(session)
        self._set_state("idle")

    async def resume(self, acp_session_id: Optional[str]) -> bool:
        """Bring a stored session back to life. Returns True iff loaded natively.

        When the backend supports `session/load` and we have its ACP session id,
        the agent rehydrates its own history (the replayed updates are suppressed,
        since we already hold that transcript) — return True. Otherwise we open a
        fresh session and return False, leaving it to the caller to re-establish
        context (casebook re-sends the directive + saved transcript on the next
        message). Either way, no prompt is sent here.
        """
        await self._spawn()
        if self._supports_load and acp_session_id:
            self._acp_session_id = acp_session_id
            self._suppress_emit = True
            try:
                loaded = await self._conn.load_session(
                    cwd=str(self.project_root),
                    session_id=acp_session_id,
                    mcp_servers=[],
                )
            finally:
                self._suppress_emit = False
            self._capture_config_options(loaded)
            self._set_state("idle")
            return True
        session = await self._conn.new_session(
            cwd=str(self.project_root), mcp_servers=[]
        )
        self._acp_session_id = session.session_id
        self._capture_config_options(session)
        self._set_state("idle")
        return False

    async def send(
        self, text: str, *, system: bool = False, display_text: Optional[str] = None
    ) -> None:
        """Run one prompt turn. Rejected (with a notice) while a turn is active.

        `display_text` (when given) is what the UI shows for the user turn, while
        `text` is what the agent actually receives — used to attach hidden context
        (e.g. a re-sent transcript) without dumping it into the visible transcript.
        """
        if self._busy:
            self._notify("agent is still responding; wait for the current turn")
            return
        self._busy = True
        self.emit(
            {
                "agent_id": self.agent_id,
                "case_id": self.case_id,
                "type": "message",
                "role": "user",
                "text": display_text if display_text is not None else text,
                "system": system,
            }
        )
        self._set_state("working")
        try:
            response = await self._conn.prompt(
                prompt=[text_block(text)],
                session_id=self._acp_session_id,
                message_id=str(uuid.uuid4()),
            )
            self._report_usage(getattr(response, "usage", None))
        except Exception as error:  # surface, don't crash the engine
            # The user sees the summary as a notice; keep the traceback for DEBUG
            # so a backend/protocol failure mid-turn is diagnosable.
            log.debug("prompt failed for agent=%s", self.agent_id, exc_info=True)
            self._notify(f"agent error: {error}", level="error")
        finally:
            self._busy = False
            self._set_state("idle")

    async def cancel(self) -> None:
        if self._conn is not None and self._acp_session_id is not None:
            await self._conn.cancel(session_id=self._acp_session_id)

    async def stop(self) -> None:
        await self._stack.aclose()

    def _report_usage(self, usage: Any) -> None:
        """Emit cumulative token totals from a prompt response, if the backend gives them."""
        if usage is None:
            return
        self.emit(
            {
                "agent_id": self.agent_id,
                "case_id": self.case_id,
                "type": "usage",
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
            }
        )

    def _set_state(self, state: str) -> None:
        self.emit(
            {"agent_id": self.agent_id, "case_id": self.case_id,
             "type": "agent_state", "state": state}
        )

    def _notify(self, message: str, level: str = "info") -> None:
        self.emit(
            {"agent_id": self.agent_id, "case_id": self.case_id,
             "type": "notice", "level": level, "message": message}
        )


class SessionManager:
    """Owns all live agent sessions, keyed by agent_id, grouped by case."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}

    def new_agent_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def add(self, session: AgentSession) -> None:
        self._sessions[session.agent_id] = session

    def get(self, agent_id: str) -> Optional[AgentSession]:
        return self._sessions.get(agent_id)

    def pop(self, agent_id: str) -> Optional[AgentSession]:
        return self._sessions.pop(agent_id, None)

    def for_case(self, case_id: str) -> list[AgentSession]:
        return [s for s in self._sessions.values() if s.case_id == case_id]

    def all(self) -> list[AgentSession]:
        return list(self._sessions.values())
