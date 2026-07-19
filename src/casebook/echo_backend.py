"""A minimal, built-in ACP agent that echoes user messages back.

This is the always-available fallback backend (see config.py). It speaks ACP over
stdio like any real backend, so casebook is runnable and developable without a
model installed. It has no memory and does not support session loading — it
simply reflects each prompt's text back as an agent message.

Run as ``python -m casebook.echo_backend``; that is exactly the command the
built-in ``echo`` backend launches.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from acp import PROTOCOL_VERSION, AgentSideConnection, stdio_streams, text_block
from acp.interfaces import Implementation
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    SessionConfigOptionBoolean,
    SessionConfigOptionSelect,
    SessionConfigSelectOption,
    SetSessionConfigOptionResponse,
)

# Pretend config options, so the config-option UI is demonstrable without a real
# backend. Like claude and codex, the model is exposed as a `category=="model"`
# config option (not the unstable `models` field). Their values are reflected in
# each echo reply so a change is observable end to end.
_MODEL_CHOICES = [
    SessionConfigSelectOption(value="echo-small", name="Echo Small"),
    SessionConfigSelectOption(value="echo-large", name="Echo Large"),
]
_EFFORT_CHOICES = [
    SessionConfigSelectOption(value="low", name="Low"),
    SessionConfigSelectOption(value="medium", name="Medium"),
    SessionConfigSelectOption(value="high", name="High"),
]
_DEFAULT_CONFIG = {"model": "echo-small", "effort": "medium", "loud": False}


def _config_options(current: dict) -> list:
    return [
        SessionConfigOptionSelect(
            id="model", name="Model", category="model", type="select",
            current_value=current["model"], options=_MODEL_CHOICES,
        ),
        SessionConfigOptionSelect(
            id="effort", name="Effort", category="reasoning", type="select",
            current_value=current["effort"], options=_EFFORT_CHOICES,
        ),
        SessionConfigOptionBoolean(
            id="loud", name="Loud", category="output", type="boolean",
            current_value=current["loud"],
        ),
    ]


class EchoAgent:
    """An ACP agent whose every reply is the prompt text, prefixed with `echo:`."""

    def __init__(self, connection: AgentSideConnection) -> None:
        self._connection = connection
        self._config_by_session: dict[str, dict] = {}

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(load_session=False),
            agent_info=Implementation(name="echo", version="0.1.0"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        session_id = f"echo-{uuid.uuid4().hex}"
        config = dict(_DEFAULT_CONFIG)
        self._config_by_session[session_id] = config
        return NewSessionResponse(
            session_id=session_id,
            config_options=_config_options(config),
        )

    async def set_config_option(
        self, config_id: str, session_id: str, value: Any, **kwargs: Any
    ) -> SetSessionConfigOptionResponse:
        config = self._config_by_session.setdefault(session_id, dict(_DEFAULT_CONFIG))
        config[config_id] = value
        # ACP requires the updated option list back in the response.
        return SetSessionConfigOptionResponse(config_options=_config_options(config))

    async def prompt(self, prompt: list, session_id: str, **kwargs: Any) -> PromptResponse:
        text = "".join(getattr(block, "text", "") for block in prompt)
        config = self._config_by_session.get(session_id, _DEFAULT_CONFIG)
        reply = f"echo[{config['model']}/{config['effort']}]: {text}"
        if config["loud"]:
            reply = reply.upper()
        await self._connection.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                content=text_block(reply),
                session_update="agent_message_chunk",
            ),
        )
        return PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        return None


async def _main() -> None:
    reader, writer = await stdio_streams()
    connection = AgentSideConnection(
        lambda conn: EchoAgent(conn),
        writer,
        reader,
        listening=False,
        use_unstable_protocol=True,
    )
    await connection.listen()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
