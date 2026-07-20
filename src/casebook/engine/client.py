"""Our ACP client: the bridge from one agent's callbacks to engine events.

There is one AgentClient per agent subprocess. The ACP connection calls these
methods; we translate each into a UI-neutral event (or, for permission/fs
requests, into a real answer the agent waits on). Because the agent's file reads
and writes are routed through here, casebook is the broker for filesystem access
— the same property that lets it keep the UI in sync.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from acp import ReadTextFileResponse, RequestPermissionResponse, WriteTextFileResponse
from acp.interfaces import Client
from acp.schema import AllowedOutcome, DeniedOutcome

from .. import logsetup

log = logsetup.get_logger("engine.client")

# Emit a plain event dict; PermissionRequester returns the chosen option_id, or
# None to deny.
Emit = Callable[[dict], None]
PermissionRequester = Callable[[dict], Awaitable[Optional[str]]]


def _block_text(content: Any) -> str:
    """Best-effort text extraction from an ACP content block."""
    return getattr(content, "text", None) or ""


def capture_config_options(response: Any) -> list[dict]:
    """Normalize a session response (or a live update) to a uniform option list.

    ACP's `config_options` is the stable mechanism for every knob — model,
    reasoning effort, mode, toggles — and casebook treats them all identically
    (the model is not special; it's just the option with `category == "model"`).

    The ACP `models` field / `session/set_model` (marked UNSTABLE in the spec) is
    deliberately ignored: claude and codex both expose the model through
    `config_options`, so relying on the stable path costs us nothing and avoids
    codex's redundant model×effort cross-product.
    """
    return [_config_option_dict(option)
            for option in getattr(response, "config_options", None) or []]


def resolve_config_value(option: dict, preference: Any) -> Optional[Any]:
    """Resolve a config-file preference to a valid value for `option`, or None.

    Booleans pass through only if actually a bool. Selects match `preference`
    against choice values (exact first, then value/name substring), so a loose
    `"opus"` resolves to the full model id. Returns None when nothing matches, so
    callers can report a preference that went nowhere.
    """
    if option.get("type") == "boolean":
        return preference if isinstance(preference, bool) else None
    if preference is None:
        return None
    choices = option.get("options") or []
    values = [choice["value"] for choice in choices]
    if preference in values:
        return preference
    lowered = str(preference).lower()
    for choice in choices:
        if lowered in choice["value"].lower() or lowered in (choice.get("name") or "").lower():
            return choice["value"]
    return None


def capture_commands(update: Any) -> list[dict]:
    """Normalize an `available_commands_update` to a uniform command list.

    Slash commands have no dedicated RPC — they are invoked as plain prompt text
    — so casebook only needs their names/descriptions for discoverability. The
    optional `input.hint` is a free-text prompt for the command's argument.
    """
    return [
        {
            "name": command.name,
            "description": getattr(command, "description", None),
            "input_hint": _command_input_hint(command),
        }
        for command in getattr(update, "available_commands", None) or []
    ]


def _command_input_hint(command: Any) -> Optional[str]:
    command_input = getattr(command, "input", None)
    root = getattr(command_input, "root", None) if command_input else None
    return getattr(root, "hint", None) if root else None


def _config_option_dict(option: Any) -> dict:
    result = {
        "id": option.id,
        "name": option.name,
        "type": getattr(option, "type", None),
        "category": getattr(option, "category", None),
        "description": getattr(option, "description", None),
        "current_value": option.current_value,
    }
    if getattr(option, "type", None) == "select":
        result["options"] = _select_choices(getattr(option, "options", None))
    return result


def _select_choices(options: Any) -> list[dict]:
    """Flatten a select's options — flat or grouped — to `{value, name, description}`."""
    choices = []
    for entry in options or []:
        if getattr(entry, "value", None) is not None:  # a plain SessionConfigSelectOption
            choices.append({
                "value": entry.value,
                "name": entry.name,
                "description": getattr(entry, "description", None),
            })
        else:  # a SessionConfigSelectGroup: {group, name, options}
            for choice in getattr(entry, "options", None) or []:
                choices.append({
                    "value": choice.value,
                    "name": choice.name,
                    "description": getattr(choice, "description", None),
                })
    return choices


class AgentClient(Client):
    def __init__(
        self,
        agent_id: str,
        case_id: str,
        project_root: Path,
        emit: Emit,
        request_permission: PermissionRequester,
    ) -> None:
        self.agent_id = agent_id
        self.case_id = case_id
        self.project_root = project_root.resolve()
        self._emit = emit
        self._request_permission = request_permission

    def _event(self, **payload) -> None:
        self._emit({"agent_id": self.agent_id, "case_id": self.case_id, **payload})

    # --- agent → UI: streamed session updates -----------------------------
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        kind = getattr(update, "session_update", None)
        if kind in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
            role = {
                "agent_message_chunk": "agent",
                "agent_thought_chunk": "thought",
                "user_message_chunk": "user",
            }[kind]
            self._event(
                type="message",
                role=role,
                text=_block_text(update.content),
                message_id=getattr(update, "message_id", None),
            )
        elif kind in ("tool_call", "tool_call_update"):
            self._event(
                type="tool_call",
                tool_call_id=update.tool_call_id,
                title=getattr(update, "title", None),
                tool_kind=getattr(update, "kind", None),
                status=getattr(update, "status", None),
            )
        elif kind == "config_option_update":
            self._event(type="config_options", options=capture_config_options(update))
        elif kind == "available_commands_update":
            self._event(type="commands", commands=capture_commands(update))
        elif kind == "agent_plan":
            self._event(type="plan", raw=_dump(update))
        elif kind == "usage_update":
            cost = getattr(update, "cost", None)
            self._event(
                type="usage",
                used=getattr(update, "used", None),
                size=getattr(update, "size", None),
                cost_amount=getattr(cost, "amount", None) if cost else None,
                cost_currency=getattr(cost, "currency", None) if cost else None,
            )
        # Other update kinds (modes, live model changes) are ignored for now.

    # --- agent → user: permission prompts ---------------------------------
    async def request_permission(
        self, options: list, session_id: str, tool_call: Any, **kwargs: Any
    ) -> RequestPermissionResponse:
        payload = {
            "agent_id": self.agent_id,
            "case_id": self.case_id,
            "tool_call": {
                "title": getattr(tool_call, "title", None),
                "kind": getattr(tool_call, "kind", None),
            },
            "options": [
                {"option_id": option.option_id, "name": option.name, "kind": option.kind}
                for option in options
            ],
        }
        chosen = await self._request_permission(payload)
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=chosen, outcome="selected")
        )

    # --- agent → filesystem (brokered through casebook) -------------------
    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: Optional[int] = None,
        line: Optional[int] = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        target = self._resolve_in_project(path)
        log.debug("agent %s reads %s (line=%s limit=%s)",
                  self.agent_id, target, line, limit)
        text = target.read_text()
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = (line - 1) if line else 0
            end = (start + limit) if limit else None
            text = "".join(lines[start:end])
        return ReadTextFileResponse(content=text)

    async def write_text_file(
        self, content: str, path: str, session_id: str, **kwargs: Any
    ) -> Optional[WriteTextFileResponse]:
        target = self._resolve_in_project(path)
        log.debug("agent %s writes %s (%d bytes)",
                  self.agent_id, target, len(content))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        # The filesystem watcher will surface this as a files_changed event.
        return WriteTextFileResponse()

    def _resolve_in_project(self, path: str) -> Path:
        """Confine agent file access to the project tree."""
        resolved = Path(path).resolve()
        if self.project_root not in resolved.parents and resolved != self.project_root:
            log.warning("agent %s denied path outside project root: %s",
                        self.agent_id, path)
            raise PermissionError(f"path outside project root: {path}")
        return resolved


def _dump(model: Any) -> Any:
    dump = getattr(model, "model_dump", None)
    return dump(mode="json") if dump else str(model)
