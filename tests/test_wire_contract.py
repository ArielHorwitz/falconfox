"""The daemon's wire: one action table, two transports.

Nothing in the suite drove either transport before this file. The HTTP routing
table, the error shapes a client reads, the websocket's snapshot and its
dispatch were all exercised only by driving the coordinator directly, so the
two surfaces could (and did) drift with the suite green.

Starlette's own `TestClient` would be the obvious tool and it needs `httpx`,
which is not a dependency of this project. The drivers below speak ASGI to the
app instead, which is the same entry point uvicorn uses, so routing, status
codes and headers are all real.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from falconfox import cli as falconfox_cli
from falconfox.coordinator import SessionCoordinator
from falconfox.engine.events import EventBus
from falconfox.errors import FalconFoxError
from falconfox.web.actions import (ACKNOWLEDGED, ACTIONS, CREATED, RESULT,
                                   Action, lookup)
from falconfox.web.server import create_app

from test_falconfox_poc import make_record

SESSION_ID = "abcd1234"
# Every argument any action in the table asks for, in one request. Each
# action's own extractor takes what it needs and ignores the rest, which is
# what lets one table test cover all of them.
EVERY_FIELD = {
    "path": "/tmp", "name": "named", "backend": "echo", "text": "hello",
    "tags": ["urgent"], "caption": "a caption", "event_index": 0,
    "config_id": "reasoning_effort", "value": "high",
    "request_id": "req00001", "ok": True,
}


class RecordingCoordinator:
    """A coordinator that records the call instead of making it.

    The table test is about dispatch: that a name arrives at the method the
    table names, carrying the arguments the request carried. What those methods
    then do is the rest of the suite's business, and a real coordinator here
    would spawn agents to find out.

    Unknown attributes become recording coroutines, so nothing has to be
    written down twice. The price is that a typo in the table would be accepted
    here, which is why `ActionTableTests` also binds every entry against the
    real `SessionCoordinator`.
    """

    def __init__(self) -> None:
        self.bus = EventBus()
        self.log = logging.getLogger("falconfox.test.wire")
        self.calls: list[tuple[str, dict]] = []
        self.made = "fork5678"

    def load_persisted(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {"type": "snapshot", "sessions": [], "config_options": {},
                "commands": {}, "usage": {}}

    def get_session(self, session_id: str) -> dict:
        return {"session_id": session_id, "name": "work"}

    async def shutdown(self) -> None:
        pass

    def __getattr__(self, method: str):
        async def record(**kwargs):
            self.calls.append((method, kwargs))
            return self.made
        return record

    def called(self, method: str) -> dict:
        """The arguments of the one call made to `method`."""
        matching = [kwargs for name, kwargs in self.calls if name == method]
        if len(matching) != 1:
            raise AssertionError(f"{method} was called {len(matching)} times")
        return matching[0]


class Client:
    """The app over ASGI, as an HTTP client sees it."""

    def __init__(self, app) -> None:
        self.app = app

    async def request(self, method: str, target: str, body=None, raw=None):
        """Returns the status, the decoded body, and the response headers.

        `raw` sends bytes as they are, for the shapes a client is not supposed
        to send.
        """
        path, _, query = target.partition("?")
        payload = raw if raw is not None else (
            json.dumps(body).encode() if body is not None else b"")
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": method, "path": path, "raw_path": path.encode(),
            "query_string": query.encode(), "root_path": "", "scheme": "http",
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode())],
            "client": ("127.0.0.1", 5555), "server": ("127.0.0.1", 9721),
        }
        incoming = [{"type": "http.request", "body": payload, "more_body": False}]
        messages = []

        async def receive():
            return incoming.pop(0)

        async def send(message):
            messages.append(message)

        await self.app(scope, receive, send)
        start = next(m for m in messages if m["type"] == "http.response.start")
        body_bytes = b"".join(m.get("body", b"") for m in messages
                              if m["type"] == "http.response.body")
        headers = {key.decode().lower(): value.decode()
                   for key, value in start["headers"]}
        return start["status"], (json.loads(body_bytes) if body_bytes else None), headers


SOCKET_SCOPE = {
    "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
    "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "query_string": b"",
    "root_path": "", "headers": [], "client": ("127.0.0.1", 5556),
    "server": ("127.0.0.1", 9721), "subprotocols": [],
}


class Socket:
    """The app's /ws endpoint, driven as a connected client."""

    def __init__(self, app) -> None:
        self._app = app
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._outbound: asyncio.Queue = asyncio.Queue()
        self._task = None

    async def __aenter__(self) -> "Socket":
        self._inbound.put_nowait({"type": "websocket.connect"})
        self._task = asyncio.create_task(
            self._app(SOCKET_SCOPE, self._inbound.get, self._outbound.put))
        accepted = await asyncio.wait_for(self._outbound.get(), 2)
        assert accepted["type"] == "websocket.accept", accepted
        return self

    async def __aexit__(self, *_exception) -> None:
        self._inbound.put_nowait({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self._task, 2)

    def send(self, frame: dict) -> None:
        self._inbound.put_nowait({"type": "websocket.receive",
                                  "text": json.dumps(frame)})

    async def receive(self, timeout: float = 2) -> dict:
        message = await asyncio.wait_for(self._outbound.get(), timeout)
        if message["type"] != "websocket.send":
            raise AssertionError(f"expected a frame, got {message}")
        return json.loads(message["text"])


async def settle(condition, timeout: float = 2) -> None:
    """Wait for a detached action to have run. The websocket answers with
    events rather than with a result, so there is nothing else to await."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the action never ran")
        await asyncio.sleep(0.01)


def http_call(action: Action) -> tuple[str, str]:
    """Where an action lives on the HTTP surface."""
    if action.name == "spawn":
        return "POST", "/api/sessions"
    if action.name == "attachment_result":
        return "POST", "/api/attachments"
    return "POST", f"/api/sessions/{SESSION_ID}/{action.name}"


class ActionTableTests(unittest.IsolatedAsyncioTestCase):
    """Every registered action reaches its coordinator method on both
    transports, and an unregistered one is refused on both."""

    async def asyncSetUp(self):
        self.coordinator = RecordingCoordinator()
        self.app = create_app(coordinator=self.coordinator)
        self.client = Client(self.app)

    async def test_every_action_dispatches_over_http(self):
        for action in ACTIONS.values():
            with self.subTest(action=action.name):
                self.coordinator.calls.clear()
                method, target = http_call(action)
                status, body, _headers = await self.client.request(
                    method, target, {**EVERY_FIELD, "session_id": SESSION_ID})
                self.assertIn(status, (200, 201), body)
                called = self.coordinator.called(action.method)
                if action.session:
                    self.assertEqual(called.get("session_id"), SESSION_ID)

    async def test_every_action_dispatches_over_the_websocket(self):
        async with Socket(self.app) as socket:
            await socket.receive()  # the snapshot
            for action in ACTIONS.values():
                with self.subTest(action=action.name):
                    self.coordinator.calls.clear()
                    socket.send({"action": action.name,
                                 "session_id": SESSION_ID, **EVERY_FIELD})
                    await settle(lambda: self.coordinator.calls)
                    called = self.coordinator.called(action.method)
                    if action.session:
                        self.assertEqual(called.get("session_id"), SESSION_ID)

    async def test_an_unknown_action_is_an_error_over_http(self):
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/wibble", {})
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "unknown action: wibble"})
        self.assertEqual(self.coordinator.calls, [])

    async def test_an_unknown_action_is_an_event_over_the_websocket(self):
        # It used to be a log line and nothing else, so a client whose action
        # the daemon had never heard of saw exactly what it would have seen if
        # the action had worked: nothing.
        async with Socket(self.app) as socket:
            await socket.receive()
            socket.send({"action": "wibble", "session_id": SESSION_ID})
            event = await socket.receive()
            self.assertEqual(event["type"], "action_error")
            self.assertEqual(event["action"], "wibble")
            self.assertEqual(event["session_id"], SESSION_ID)
            self.assertIn("unknown action", event["error"])
            self.assertEqual(self.coordinator.calls, [])

    async def test_an_action_that_is_not_about_a_session_is_not_under_one(self):
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/spawn", {"path": "/tmp"})
        self.assertEqual(status, 404)
        self.assertIn("unknown action", body["error"])

    async def test_a_missing_argument_is_reported_not_swallowed(self):
        # `revert` reached into the frame for its event index, so a frame
        # without one raised a KeyError in the receive loop and took the whole
        # connection down with it.
        async with Socket(self.app) as socket:
            await socket.receive()
            socket.send({"action": "revert", "session_id": SESSION_ID})
            event = await socket.receive()
            self.assertEqual(event["type"], "action_error")
            self.assertIn("event_index", event["error"])
            # The connection is still good, which is the point.
            socket.send({"action": "cancel", "session_id": SESSION_ID})
            await settle(lambda: self.coordinator.calls)
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/revert", {})
        self.assertEqual(status, 400)
        self.assertIn("event_index", body["error"])

    async def test_a_body_that_is_not_a_json_object_is_refused(self):
        for payload, expected in ((b"{not json", "not JSON"),
                                  (b'"a string"', "JSON object")):
            status, body, _headers = await self.client.request(
                "POST", f"/api/sessions/{SESSION_ID}/rename", raw=payload)
            self.assertEqual(status, 400, payload)
            self.assertIn(expected, body["error"])
        # The dangerous one is `spawn`: a body read as "no fields" spawns a
        # session in the caller's home directory instead of refusing.
        status, _body, _headers = await self.client.request(
            "POST", "/api/sessions", raw=b"path=/tmp")
        self.assertEqual(status, 400)
        self.assertEqual(self.coordinator.calls, [])

    def test_every_entry_names_a_real_coordinator_call(self):
        # The recording coordinator above accepts any method with any
        # arguments, so this is what stands between the table and a typo.
        for action in ACTIONS.values():
            with self.subTest(action=action.name):
                method = getattr(SessionCoordinator, action.method, None)
                self.assertTrue(callable(method),
                                f"no coordinator method {action.method}")
                signature = inspect.signature(method)
                # `None` for self, since the bind is about the names.
                signature.bind(None, **action.kwargs(
                    {**EVERY_FIELD, "session_id": SESSION_ID}))

    def test_the_answer_kinds_are_the_ones_the_adapter_knows(self):
        for action in ACTIONS.values():
            self.assertIn(action.answer, ("session", CREATED, RESULT, ACKNOWLEDGED))

    def test_an_unknown_name_is_refused_by_the_table_itself(self):
        with self.assertRaises(FalconFoxError):
            lookup("wibble")
        with self.assertRaises(FalconFoxError):
            lookup(None)


class CliConsumerTests(unittest.TestCase):
    """The CLI posts action names it holds as strings of its own. It is a
    consumer of the table rather than part of it, so what can be tested is that
    the two still agree."""

    def test_every_action_the_cli_posts_is_registered(self):
        posted = set(re.findall(r"/api/sessions/\{[^{}]+\}/(\w+)",
                                inspect.getsource(falconfox_cli)))
        # The file store has routes of its own rather than actions, because
        # removal is a DELETE and the action route is POST-only.
        posted.discard("files")
        # These three post an action named by the command itself, so the path
        # in the source is an interpolation the pattern above cannot read.
        posted.update(falconfox_cli.SIMPLE_COMMANDS)
        self.assertEqual(sorted(name for name in posted if name not in ACTIONS), [])


class BlockingCoordinator(RecordingCoordinator):
    """A coordinator whose `send` does not return until it is let go."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.sending = 0

    async def send(self, session_id: str, text: str) -> None:
        self.calls.append(("send", {"session_id": session_id, "text": text}))
        self.sending += 1
        await self.release.wait()


class TwoSendSemanticsTests(unittest.IsolatedAsyncioTestCase):
    """`send` means two different things, on purpose: see actions.py.

    HTTP awaits the whole turn, because the CLI has no event stream and the
    response is the only way it can learn the turn is over. The websocket
    returns at once and the turn arrives as events, because that is how the
    Telegram client shows a reply building up.
    """

    async def asyncSetUp(self):
        self.coordinator = BlockingCoordinator()
        self.app = create_app(coordinator=self.coordinator)

    async def test_http_send_waits_for_the_turn(self):
        client = Client(self.app)
        request = asyncio.create_task(client.request(
            "POST", f"/api/sessions/{SESSION_ID}/send", {"text": "hello"}))
        await settle(lambda: self.coordinator.sending)
        await asyncio.sleep(0.05)
        self.assertFalse(request.done(), "HTTP answered before the turn ended")
        self.coordinator.release.set()
        status, body, _headers = await asyncio.wait_for(request, 2)
        self.assertEqual(status, 200)
        self.assertEqual(body["session_id"], SESSION_ID)

    async def test_websocket_send_returns_at_once(self):
        async with Socket(self.app) as socket:
            await socket.receive()
            socket.send({"action": "send", "session_id": SESSION_ID, "text": "one"})
            await settle(lambda: self.coordinator.sending)
            # The first send is still running. A transport that waited for it
            # could not take this one.
            socket.send({"action": "cancel", "session_id": SESSION_ID})
            await settle(lambda: any(name == "cancel"
                                     for name, _ in self.coordinator.calls))
            self.coordinator.release.set()


class RouteTests(unittest.IsolatedAsyncioTestCase):
    """One pass per HTTP route: the shape a client gets when the call works,
    and the shape it gets when it does not.

    A real coordinator, with its sessions put in by hand. These tests are about
    the transport, and a session that spawns nothing is still something to
    address. The statuses matter as much as the bodies: the CLI and the bot
    both read `{"error": ...}` out of a failure, and a 404 and a 400 are
    different answers to a client deciding what to do next.
    """

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        config_home = tempfile.TemporaryDirectory()
        self.addCleanup(config_home.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temporary.name)
        self.coordinator = SessionCoordinator(self.root)
        make_record(self.coordinator, SESSION_ID, name="work", path=str(self.root),
                    auto_named=False, persisted=True)
        self.client = Client(create_app(coordinator=self.coordinator))

    async def test_the_version_route(self):
        status, body, _headers = await self.client.request("GET", "/api/version")
        self.assertEqual(status, 200)
        self.assertTrue(body["version"])

    async def test_listing_sessions_and_the_hidden_ones(self):
        make_record(self.coordinator, "hidden00", hidden=True, auto_named=False)
        status, body, _headers = await self.client.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([item["session_id"] for item in body], [SESSION_ID])
        _status, body, _headers = await self.client.request(
            "GET", "/api/sessions?include_hidden=true")
        self.assertEqual({item["session_id"] for item in body},
                         {SESSION_ID, "hidden00"})

    async def test_spawning_a_session(self):
        # Stored rather than live, so nothing is started: what is under test is
        # the route, not the backend.
        with patch.object(self.coordinator, "_ensure_slot", return_value=False):
            status, body, _headers = await self.client.request(
                "POST", "/api/sessions", {"path": str(self.root), "name": "new"})
        self.assertEqual(status, 201)
        self.assertEqual(body["name"], "new")
        self.assertIn(body["session_id"], self.coordinator._records)

    async def test_spawning_somewhere_that_is_not_a_directory(self):
        status, body, _headers = await self.client.request(
            "POST", "/api/sessions", {"path": str(self.root.joinpath("nowhere"))})
        self.assertEqual(status, 400)
        self.assertIn("not a directory", body["error"])

    async def test_reading_one_session_and_its_transcript(self):
        self.coordinator._records[SESSION_ID].transcript = [
            {"type": "message", "role": "user", "text": "hello"}]
        status, body, _headers = await self.client.request(
            "GET", f"/api/sessions/{SESSION_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "work")
        self.assertNotIn("transcript", body, "a transcript is asked for, not sent")
        _status, body, _headers = await self.client.request(
            "GET", f"/api/sessions/{SESSION_ID}?include_transcript=true")
        self.assertEqual([event["text"] for event in body["transcript"]], ["hello"])

    async def test_reading_a_session_that_is_not_there(self):
        status, body, _headers = await self.client.request(
            "GET", "/api/sessions/nosuch00")
        self.assertEqual(status, 404)
        self.assertIn("no such session", body["error"])

    async def test_deleting_a_session(self):
        status, body, _headers = await self.client.request(
            "DELETE", f"/api/sessions/{SESSION_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"deleted": SESSION_ID})
        self.assertNotIn(SESSION_ID, self.coordinator._records)
        status, _body, _headers = await self.client.request(
            "DELETE", f"/api/sessions/{SESSION_ID}")
        self.assertEqual(status, 404, "deleting it again is a 404, not a 400")

    async def test_an_action_answers_with_the_session_it_acted_on(self):
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/rename", {"name": "renamed"})
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "renamed")
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/tag", {"tags": ["urgent"]})
        self.assertEqual(status, 200)
        self.assertEqual(body["tags"], ["urgent"])

    async def test_a_refused_action_is_a_bad_request_not_a_crash(self):
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/rename", {"name": "  "})
        self.assertEqual(status, 400)
        self.assertIn("must not be empty", body["error"])

    async def test_an_action_on_a_session_that_is_not_there(self):
        status, body, _headers = await self.client.request(
            "POST", "/api/sessions/nosuch00/cancel", {})
        self.assertEqual(status, 400)
        self.assertIn("no such session", body["error"])

    async def test_the_file_store(self):
        source = self.root.joinpath("notes.txt")
        source.write_text("notes")
        added = []
        for _twice in (1, 2):
            status, body, _headers = await self.client.request(
                "POST", f"/api/sessions/{SESSION_ID}/files", {"path": str(source)})
            self.assertEqual(status, 201)
            self.assertEqual(body["name"], "notes.txt")
            added.append(body["file_id"])
        status, removed, _headers = await self.client.request(
            "DELETE", f"/api/sessions/{SESSION_ID}/files/{added[0]}")
        self.assertEqual((status, removed), (200, {"removed": 1}))
        status, cleared, _headers = await self.client.request(
            "DELETE", f"/api/sessions/{SESSION_ID}/files")
        self.assertEqual((status, cleared), (200, {"removed": 1}))

    async def test_storing_a_file_that_is_not_there(self):
        status, body, _headers = await self.client.request(
            "POST", f"/api/sessions/{SESSION_ID}/files",
            {"path": str(self.root.joinpath("absent"))})
        self.assertEqual(status, 400)
        self.assertIn("not a file", body["error"])

    async def test_an_attachment_result_can_arrive_over_http(self):
        status, body, _headers = await self.client.request(
            "POST", "/api/attachments", {"request_id": "req00001", "ok": True})
        self.assertEqual((status, body), (200, {"ok": True}))
        status, body, _headers = await self.client.request(
            "POST", "/api/attachments", {"ok": True})
        self.assertEqual(status, 400)
        self.assertIn("request_id", body["error"])

    async def test_the_config_views(self):
        status, backends, _headers = await self.client.request("GET", "/api/backends")
        self.assertEqual(status, 200)
        self.assertIn("echo", backends["backends"])
        self.assertTrue(backends["default"])
        for path in ("/api/hotkeys", "/api/ui"):
            status, body, _headers = await self.client.request("GET", path)
            self.assertEqual((status, type(body)), (200, dict), path)

    async def test_reloading_the_config(self):
        status, body, _headers = await self.client.request("POST", "/api/reload", {})
        self.assertEqual((status, body), (200, {"reloaded": True}))


class SocketTests(unittest.IsolatedAsyncioTestCase):
    """A client's whole life on the socket: connect, take the snapshot, send an
    action, watch the event it caused come back."""

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        config_home = tempfile.TemporaryDirectory()
        self.addCleanup(config_home.cleanup)
        environment = patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.coordinator = SessionCoordinator(Path(self.temporary.name))
        make_record(self.coordinator, SESSION_ID, name="work", auto_named=False)
        self.app = create_app(coordinator=self.coordinator)

    async def test_the_snapshot_comes_first_and_then_the_events(self):
        async with Socket(self.app) as socket:
            snapshot = await socket.receive()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual([item["session_id"] for item in snapshot["sessions"]],
                             [SESSION_ID])
            socket.send({"action": "rename", "session_id": SESSION_ID,
                         "name": "renamed"})
            event = await socket.receive()
            self.assertEqual(event["type"], "session_updated")
            self.assertEqual(event["name"], "renamed")
