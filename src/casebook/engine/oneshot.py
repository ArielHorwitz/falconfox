"""A one-shot, non-streaming query to a backend.

Used for short utility prompts — naming a session, for example — that should not
touch any live conversation. It spawns its own ephemeral subprocess, sends a
single prompt, collects the agent's message text, and tears the process down. No
events reach the engine bus, and the agent gets no filesystem access (a utility
query has no business writing files).
"""

from __future__ import annotations

import os
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from acp import PROTOCOL_VERSION, RequestPermissionResponse, spawn_agent_process, text_block
from acp.interfaces import Client, ClientCapabilities, Implementation
from acp.schema import DeniedOutcome, FileSystemCapabilities

from .. import logsetup
from ..config import Backend
from .client import capture_config_options, resolve_config_value

log = logsetup.get_logger("engine.oneshot")

_NO_FILES = ClientCapabilities(
    fs=FileSystemCapabilities(read_text_file=False, write_text_file=False),
    terminal=False,
)


class _CollectingClient(Client):
    """Collects agent message text; denies everything else."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if getattr(update, "session_update", None) == "agent_message_chunk":
            content = getattr(update, "content", None)
            self.parts.append(getattr(content, "text", None) or "")

    async def request_permission(self, *args: Any, **kwargs: Any) -> RequestPermissionResponse:
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def read_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("filesystem unavailable for one-shot queries")

    async def write_text_file(self, *args: Any, **kwargs: Any) -> Any:
        raise PermissionError("filesystem unavailable for one-shot queries")


async def _apply_defaults(conn: Any, session: Any, backend: Backend) -> None:
    """Best-effort: set the backend's configured config-option defaults on the
    one-shot session. Failures are logged, not raised — a naming query should
    proceed on the backend's own defaults rather than error."""
    if not backend.config_options:
        return
    options = capture_config_options(session)
    for config_id, preference in backend.config_options.items():
        option = next((o for o in options if o["id"] == config_id), None)
        if option is None:
            continue
        value = resolve_config_value(option, preference)
        if value is None or value == option["current_value"]:
            continue
        try:
            await conn.set_config_option(
                config_id=config_id, value=value, session_id=session.session_id
            )
        except Exception:
            log.debug("one-shot set_config_option(%s=%r) failed, using default",
                      config_id, value, exc_info=True)


async def one_shot(backend: Backend, project_root: Path, prompt: str) -> str:
    """Spawn `backend`, send one prompt, and return the agent's concatenated reply.

    The backend's `config_options` defaults (e.g. a cheap naming model, or a lower
    reasoning effort) are applied before prompting — the same mechanism a live
    session uses, so a naming backend is configured just like any other.
    """
    client = _CollectingClient()
    environment = {**os.environ, **backend.env}
    command, *args = backend.command
    log.debug("one-shot query via backend %s: %s", backend.name, [command, *args])
    async with AsyncExitStack() as stack:
        conn, _process = await stack.enter_async_context(
            spawn_agent_process(
                client,
                command,
                *args,
                cwd=str(project_root),
                env=environment,
                # See session.py for rationale on this limit.
                transport_kwargs={"limit": 100 * 1024 * 1024},
            )
        )
        await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=_NO_FILES,
            client_info=Implementation(name="casebook", version="0.1.0"),
        )
        session = await conn.new_session(cwd=str(project_root), mcp_servers=[])
        await _apply_defaults(conn, session, backend)
        await conn.prompt(
            prompt=[text_block(prompt)],
            session_id=session.session_id,
            message_id=str(uuid.uuid4()),
        )
    return "".join(client.parts)
