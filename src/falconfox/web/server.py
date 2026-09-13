"""Flat REST/WebSocket surface for the FalconFox session daemon."""

from __future__ import annotations

import asyncio
import inspect
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config, get_version, logsetup, state
from ..coordinator import SessionCoordinator
from ..engine.events import CLOSED
from ..errors import FalconFoxError
from ..watchdog import StallWatchdog
from .actions import (ACKNOWLEDGED, ACTIONS, CREATED, PROTOCOL_HEADER,
                      PROTOCOL_VERSION, RESULT, Action, lookup)

STATIC_DIR = Path(__file__).parent.joinpath("static")
log = logsetup.get_logger("server")


def create_app(
    *,
    write_info: bool = False,
    open_browser: bool = False,
    bound_port: int = 0,
    coordinator: SessionCoordinator | None = None,
) -> Starlette:
    coordinator = coordinator or SessionCoordinator()
    coordinator.load_persisted()

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        # The keepalive-timeout stalls of 2026-08-25 were unattributable because
        # nothing recorded what (or whether) this process was doing. See watchdog.py.
        watchdog = StallWatchdog(logsetup.get_logger("watchdog"))
        watchdog.start()
        if write_info:
            # The directory before the file that advertises it: a client that
            # reads server.json must find somewhere to write.
            state.prepare_clients_dir()
            state.write_server_info(bound_port)
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{bound_port}")
        try:
            yield
        finally:
            watchdog.stop()
            if write_info:
                state.remove_server_info()
            await coordinator.shutdown()

    async def index(_request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR.joinpath("index.html"))

    async def version_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"version": get_version()})

    async def sessions_endpoint(request: Request) -> JSONResponse:
        if request.method == "GET":
            include = request.query_params.get("include_hidden") in ("1", "true")
            return JSONResponse(coordinator.list_sessions(include_hidden=include))
        return await _http_action(coordinator, ACTIONS["spawn"], request)

    async def session_endpoint(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        try:
            # A session that is not there is a 404 on this route and a 400 on
            # the action route, which is why the delete is not simply handed
            # to the adapter: the adapter cannot tell "no such session" from
            # any other refusal, and the status is part of the contract.
            detail = coordinator.get_session(session_id)
        except FalconFoxError as error:
            return JSONResponse({"error": str(error)}, status_code=404)
        if request.method == "DELETE":
            return await _http_action(coordinator, ACTIONS["delete"], request,
                                      session_id)
        # The transcript is asked for, not assumed. Most reads of a session
        # want a field or two -- does it exist, where does it run -- and a
        # transcript grows without bound between clears, so sending one by
        # default made an existence check cost megabytes.
        if request.query_params.get("include_transcript") in ("1", "true"):
            detail["transcript"] = coordinator.transcript(session_id)
        return JSONResponse(detail)

    async def session_action(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        name = request.path_params["action"]
        action = ACTIONS.get(name)
        # An action that is not about a session is not under this path, and from
        # here that is the same mistake as a name nobody knows.
        if action is None or not action.session:
            return JSONResponse({"error": f"unknown action: {name}"},
                                status_code=404)
        return await _http_action(coordinator, action, request, session_id)

    async def attachments_endpoint(request: Request) -> JSONResponse:
        """A client reporting what became of a file the daemon handed it.

        The websocket is how the only client that sends files answers, since it
        is already connected. This route exists so the action is reachable on
        both transports rather than only one.
        """
        return await _http_action(coordinator, ACTIONS["attachment_result"],
                                  request)

    async def session_files(request: Request) -> JSONResponse:
        """The session's file store: add one, or clear the lot.

        Its own routes rather than another `action`, because removal is a
        DELETE and the action route is POST-only. The store is the one part of
        a session with a resource of its own to address.
        """
        session_id = request.path_params["session_id"]
        try:
            if request.method == "DELETE":
                coordinator.log.info("action=clear_files via=http session=%s", session_id)
                return JSONResponse(coordinator.clear_files(session_id))
            body = await _body(request)
            coordinator.log.info("action=add_file via=http session=%s name=%s",
                                 session_id, body.get("name"))
            return JSONResponse(coordinator.add_file(
                session_id, body.get("path", ""), body.get("name")), status_code=201)
        except (FalconFoxError, OSError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    async def session_file(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        file_id = request.path_params["file_id"]
        coordinator.log.info("action=remove_file via=http session=%s file=%s",
                             session_id, file_id)
        try:
            return JSONResponse(coordinator.remove_file(session_id, file_id))
        except (FalconFoxError, OSError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    async def backends_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse(coordinator.list_backends())

    async def reload_config(_request: Request) -> JSONResponse:
        coordinator.reload_config()
        return JSONResponse({"reloaded": True})

    async def hotkeys(_request: Request) -> JSONResponse:
        return JSONResponse(coordinator.hotkeys())

    async def ui_config(_request: Request) -> JSONResponse:
        return JSONResponse(coordinator.ui_config())

    async def websocket_endpoint(websocket: WebSocket) -> None:
        client = websocket.client
        peer = f"{client.host}:{client.port}" if client else "?"
        log.info("ws connect: client=%s", peer)
        await websocket.accept()
        await _run_socket(websocket, coordinator, peer)

    return Starlette(
        lifespan=lifespan,
        middleware=[Middleware(ProtocolHeader)],
        routes=[
            Route("/api/version", version_endpoint),
            Route("/api/sessions", sessions_endpoint, methods=["GET", "POST"]),
            Route("/api/sessions/{session_id}", session_endpoint, methods=["GET", "DELETE"]),
            # Before the catch-all action route, which would otherwise
            # answer POST /files as an unknown action.
            Route("/api/sessions/{session_id}/files", session_files,
                  methods=["POST", "DELETE"]),
            Route("/api/sessions/{session_id}/files/{file_id}", session_file,
                  methods=["DELETE"]),
            Route("/api/sessions/{session_id}/{action}", session_action, methods=["POST"]),
            Route("/api/attachments", attachments_endpoint, methods=["POST"]),
            Route("/api/backends", backends_endpoint),
            Route("/api/hotkeys", hotkeys),
            Route("/api/ui", ui_config),
            Route("/api/reload", reload_config, methods=["POST"]),
            WebSocketRoute("/ws", websocket_endpoint),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
            Route("/", index),
        ],
    )


class ProtocolHeader:
    """Stamp the wire version onto every HTTP answer.

    A header rather than a field in a body, so a client reads it off the first
    request it was going to make anyway and no call exists only to ask. The
    websocket says the same thing in its snapshot. See actions.PROTOCOL_VERSION.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def stamped(message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[PROTOCOL_HEADER] = str(PROTOCOL_VERSION)
            await send(message)

        await self.app(scope, receive, stamped)


async def _body(request: Request) -> dict:
    """The request's JSON body, or nothing when it has none.

    No body is legitimate: most actions take nothing but the session in the
    path. A body that is not a JSON object is not, and saying so is the
    difference between a 400 and an action running with its arguments quietly
    missing.
    """
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = await request.json()
    except ValueError as error:
        raise FalconFoxError(f"request body is not JSON: {error}") from error
    if not isinstance(body, dict):
        raise FalconFoxError("request body must be a JSON object")
    return body


async def _invoke(coordinator: SessionCoordinator, action: Action,
                  arguments: dict):
    """Make the action's call.

    Parts of the table are plain methods rather than coroutines (opening a
    session, resolving an attachment), which is a fact about the method and not
    about the action, so no adapter has to know which is which.
    """
    call = getattr(coordinator, action.method)(**arguments)
    return await call if inspect.isawaitable(call) else call


def _log_action(coordinator: SessionCoordinator, action: Action,
                arguments: dict, via: str) -> None:
    # The CLI drives the daemon over HTTP and the bot over the websocket, so
    # without the transport in the line there is no telling which of them asked
    # for something.
    detail = "".join(f" {name}={arguments.get(name)}"
                     for name in action.log_arguments)
    coordinator.log.info("action=%s via=%s session=%s%s", action.name, via,
                         arguments.get("session_id"), detail)


async def _http_action(coordinator: SessionCoordinator, action: Action,
                       request: Request,
                       session_id: Optional[str] = None) -> JSONResponse:
    """The HTTP adapter: await the action, answer with its result.

    One try around reading the body, reading the arguments out of it and making
    the call, because a client reads the same `{"error": ...}` whichever of the
    three refused it.
    """
    try:
        fields = await _body(request)
        if session_id is not None:
            # The path is the address, never the body.
            fields["session_id"] = session_id
        arguments = action.kwargs(fields)
        _log_action(coordinator, action, arguments, "http")
        result = await _invoke(coordinator, action, arguments)
    except (FalconFoxError, KeyError, OSError) as error:
        return JSONResponse({"error": str(error)}, status_code=400)
    except Exception as error:
        log.debug("action failed: %s %s", action.name, session_id, exc_info=True)
        return JSONResponse({"error": str(error)}, status_code=500)
    if action.answer == RESULT:
        return JSONResponse(result)
    if action.answer == ACKNOWLEDGED:
        return JSONResponse({"ok": True})
    if action.answer == CREATED:
        return JSONResponse(coordinator.get_session(result), status_code=201)
    try:
        return JSONResponse(coordinator.get_session(session_id))
    except FalconFoxError:
        # The action took the session with it: a delete, or a stop of a session
        # that had nothing worth keeping. That it is gone is the only true
        # answer, and it used to be reported as a failed request.
        return JSONResponse({"deleted": session_id})


def _run_socket_action(coordinator: SessionCoordinator,
                       frame: dict) -> Optional[dict]:
    """The websocket adapter: start the action, answer with events.

    Detached on purpose, per the contract in actions.py: the reply to a
    websocket action is the event stream, so nothing waits here. Returns an
    event for the client that sent a frame the daemon would not run, which was
    a logged no-op before and so was indistinguishable from silence.
    """
    try:
        action = lookup(frame.get("action"))
        # Arguments are read before detaching, so a frame that is missing one
        # is reported to the client that sent it rather than swallowed by a
        # background task.
        arguments = action.kwargs(frame)
    except FalconFoxError as error:
        coordinator.log.warning("refusing an action: %s", error)
        return {"type": "action_error", "action": frame.get("action"),
                "session_id": frame.get("session_id"), "error": str(error)}
    _log_action(coordinator, action, arguments, "ws")
    _spawn(_invoke(coordinator, action, arguments))
    return None


async def _run_socket(websocket: WebSocket, coordinator: SessionCoordinator,
                      peer: str = "?") -> None:
    with coordinator.bus.subscribe(peer) as queue:
        await websocket.send_json({**coordinator.snapshot(),
                                   "protocol": PROTOCOL_VERSION})
        sender = asyncio.create_task(_send_events(websocket, queue))
        try:
            while True:
                frame = await websocket.receive_json()
                refusal = _run_socket_action(coordinator, frame)
                if refusal is not None:
                    # Through this client's own queue rather than straight down
                    # the socket: the sender task is the only writer, and two
                    # tasks writing frames is how a socket ends up with half of
                    # each.
                    try:
                        queue.put_nowait(refusal)
                    except asyncio.QueueFull:
                        log.debug("no room to report a refused action")
        except WebSocketDisconnect as disconnect:
            log.info("ws disconnect: code=%s", disconnect.code)
        except Exception:
            log.exception("ws error")
        finally:
            sender.cancel()


async def _send_events(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            event = await queue.get()
            if event is CLOSED:
                # The bus gave up on this subscriber for falling too far
                # behind. Closing the socket is how the client is told, and
                # its reconnect re-subscribes and re-snapshots, which is the
                # recovery: nothing new had to be built for this.
                log.warning("closing a socket that was not reading its events")
                await websocket.close(code=1011)
                return
            await websocket.send_json(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("ws send stopped", exc_info=True)


def _spawn(coro) -> None:
    asyncio.create_task(_guard(coro))


async def _guard(coro) -> None:
    try:
        await coro
    except Exception:
        log.debug("background action failed", exc_info=True)


def serve(host: str = "127.0.0.1", port: int = 9721, open_browser: bool = False) -> None:
    daemon = os.environ.get("FALCONFOX_DAEMON") == "1"
    override = os.environ.get("FALCONFOX_LOG_PATH")
    if daemon:
        log_file = None
        destination = override or str(state.log_path())
    else:
        log_file = Path(override) if override else None
        destination = str(log_file) if log_file else "console only"
    level = os.environ.get("FALCONFOX_LOG_LEVEL") or config.log_level()
    logsetup.configure(log_file, level)
    uvicorn_level = "info" if str(level).upper() == "DEBUG" else "warning"
    log.info("falconfox serving on http://%s:%s (pid=%s, log=%s, level=%s)",
             host, port, os.getpid(), destination, level)
    uvicorn.run(create_app(write_info=daemon, open_browser=open_browser, bound_port=port),
                host=host, port=port, log_level=uvicorn_level, access_log=True)
