"""The daemon's action surface, written down once.

An action is three things: the name a client sends, the coordinator method it
calls, and how that method's arguments are read out of the request. Both
transports in `server.py` are adapters over this table. HTTP takes the session
id from the path and everything else from the JSON body; the websocket takes
the lot from the action frame. Before this table existed the two were separate
switch statements, and they had already drifted: actions on one transport and
not the other, an unknown name refused over HTTP and silently ignored over the
websocket.

What the transports do *not* share is completion, and that is deliberate.

HTTP awaits the action and answers with its result. That is why `falconfox
send` blocks for as long as the turn it starts: the CLI has no event stream to
watch, so the response is the only thing that can tell it the turn is over.

The websocket detaches every action and answers through the event stream. That
is why the Telegram client sees a turn arrive in pieces, and why it can handle
other sessions while one is thinking.

This paragraph is the only place that asymmetry is stated. The table below says
nothing about it, because it is a fact about each transport rather than about
any one action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..errors import FalconFoxError

# The wire version. Bumped when a change here, to the events or to the
# snapshot stops a client built against the previous one from working
# correctly. Adding an action or an event type does not qualify: a client that
# has never heard of an action does not send it, and both clients ignore event
# types they do not know. Renaming or removing one does, and so does changing
# what a field means.
#
# The websocket carries it in the snapshot and HTTP carries it in a response
# header, so a client learns it from a message it was already going to read.
# Nothing is refused over a mismatch and nobody is asked to upgrade: the client
# says what it sees, at WARNING, and carries on. A daemon and a client in this
# repository are restarted together, so a mismatch means two checkouts are in
# play -- the dev CLI against the deployment's daemon, say -- and that is worth
# a line in a log rather than an outage.
PROTOCOL_VERSION = 1
PROTOCOL_HEADER = "X-FalconFox-Protocol"

# What HTTP answers with once the action has run. The websocket answers with
# events and so ignores this entirely.
SESSION = "session"        # the session that was acted on
CREATED = "created"        # a session the action made, 201
RESULT = "result"          # whatever the coordinator method returned
ACKNOWLEDGED = "ok"        # nothing to report but that it was done


def _required(fields: dict, name: str, action: str):
    """A field the action cannot run without.

    A `FalconFoxError` rather than a `KeyError`, because both adapters already
    know how to report one: a 400 with a message over HTTP, an error event over
    the websocket. The websocket used to take a `KeyError` out of the receive
    loop and close the connection over it.
    """
    if name not in fields or fields[name] is None:
        raise FalconFoxError(f"{action} requires {name}")
    return fields[name]


@dataclass(frozen=True)
class Action:
    """One name a client can send, and what the daemon does with it."""

    name: str
    # The coordinator method, by name rather than by reference: the table is
    # built once at import and every app has a coordinator of its own.
    method: str
    # The method's own arguments, pulled out of one flat dict of request
    # fields. The session id is not among them; `session` decides that.
    arguments: Callable[[dict], dict] = lambda _fields: {}
    answer: str = SESSION
    # Whether this action acts on a session, which is also what decides
    # whether it can be reached under /api/sessions/{id}/.
    session: bool = True
    # Arguments worth a log line. Not all of them: `send` carries the whole
    # prompt, and the log is not the place for it.
    log_arguments: tuple[str, ...] = ()

    def kwargs(self, fields: dict) -> dict:
        arguments = dict(self.arguments(fields))
        if self.session:
            arguments["session_id"] = fields.get("session_id")
        return arguments


ACTIONS: dict[str, Action] = {action.name: action for action in (
    Action("spawn", "add_session", lambda fields: {
        "path": fields.get("path"),
        "name": fields.get("name"),
        "backend_name": fields.get("backend"),
        "ephemeral": bool(fields.get("ephemeral", False)),
        "hidden": fields.get("hidden"),
        "roles": fields.get("roles"),
    }, answer=CREATED, session=False,
        log_arguments=("path", "name", "backend_name")),
    Action("open", "open_session"),
    Action("resume", "resume_session"),
    Action("send", "send", lambda fields: {"text": fields.get("text", "")}),
    Action("cancel", "cancel"),
    Action("stop", "stop_session"),
    Action("delete", "delete_session"),
    Action("rename", "rename_session",
           lambda fields: {"name": fields.get("name", "")}),
    Action("name", "name_session"),
    Action("tag", "set_tags", lambda fields: {"tags": fields.get("tags") or []}),
    Action("attach", "attach", lambda fields: {
        "path": fields.get("path", ""),
        "caption": fields.get("caption"),
        "ack": bool(fields.get("ack", True)),
        "raw": bool(fields.get("raw", False)),
    }, answer=RESULT, log_arguments=("path",)),
    # Reachable by no client that runs today: the dead web UI was the only one
    # that ever sent these three. They stay registered, and tested, until they
    # leave with it.
    Action("revert", "revert_session", lambda fields: {
        "event_index": _required(fields, "event_index", "revert")}),
    Action("fork", "fork_session",
           lambda fields: {"event_index": fields.get("event_index")},
           answer=CREATED),
    Action("set_config_option", "set_config_option", lambda fields: {
        "config_id": _required(fields, "config_id", "set_config_option"),
        "value": _required(fields, "value", "set_config_option"),
    }),
    # A client reporting back on a file it was handed, so it names an
    # attachment rather than a session.
    Action("attachment_result", "resolve_attachment", lambda fields: {
        "request_id": _required(fields, "request_id", "attachment_result"),
        "ok": bool(fields.get("ok")),
        "error": fields.get("error"),
    }, answer=ACKNOWLEDGED, session=False, log_arguments=("request_id", "ok")),
)}


def lookup(name: Optional[str]) -> Action:
    """The action by name. An unknown one is an error on every transport."""
    action = ACTIONS.get(name or "")
    if action is None:
        raise FalconFoxError(f"unknown action: {name}")
    return action
