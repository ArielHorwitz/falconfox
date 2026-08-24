"""Flat REST/WebSocket surface for the FalconFox session daemon."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import config, get_version, logsetup, state
from ..coordinator import SessionCoordinator
from ..errors import FalconFoxError

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
        if write_info:
            state.write_server_info(bound_port)
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{bound_port}")
        try:
            yield
        finally:
            if write_info:
                state.remove_server_info()
            await coordinator.shutdown()

    async def index(_request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR.joinpath("index.html"))

    async def version_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"version": get_version()})

    async def sessions_endpoint(request: Request) -> JSONResponse:
        if request.method == "GET":
            include = request.query_params.get("include_ephemeral") in ("1", "true")
            return JSONResponse(coordinator.list_sessions(include_ephemeral=include))
        try:
            body = await request.json()
            session_id = await coordinator.add_session(
                path=body.get("path"),
                name=body.get("name"),
                backend_name=body.get("backend"),
                ephemeral=bool(body.get("ephemeral", False)),
            )
            return JSONResponse(coordinator.get_session(session_id), status_code=201)
        except (FalconFoxError, KeyError, OSError) as error:
            return JSONResponse({"error": str(error)}, status_code=400)

    async def session_endpoint(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        try:
            if request.method == "DELETE":
                await coordinator.delete_session(session_id)
                return JSONResponse({"deleted": session_id})
            return JSONResponse({
                **coordinator.get_session(session_id),
                "transcript": coordinator.transcript(session_id),
            })
        except FalconFoxError as error:
            return JSONResponse({"error": str(error)}, status_code=404)

    async def session_action(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        action = request.path_params["action"]
        try:
            body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
            if action == "send":
                await coordinator.send(session_id, body.get("text", ""))
            elif action == "resume":
                await coordinator.resume_session(session_id)
            elif action == "stop":
                await coordinator.stop_session(session_id)
            elif action == "rename":
                coordinator.rename_session(session_id, body.get("name", ""))
            elif action == "name":
                await coordinator.name_session(session_id)
            elif action == "cancel":
                await coordinator.cancel(session_id)
            elif action == "open":
                coordinator.open_session(session_id)
            else:
                return JSONResponse({"error": f"unknown action: {action}"}, status_code=404)
            return JSONResponse(coordinator.get_session(session_id))
        except FalconFoxError as error:
            return JSONResponse({"error": str(error)}, status_code=400)
        except Exception as error:
            log.debug("session action failed: %s %s", session_id, action, exc_info=True)
            return JSONResponse({"error": str(error)}, status_code=500)

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
        await _run_socket(websocket, coordinator)

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/api/version", version_endpoint),
            Route("/api/sessions", sessions_endpoint, methods=["GET", "POST"]),
            Route("/api/sessions/{session_id}", session_endpoint, methods=["GET", "DELETE"]),
            Route("/api/sessions/{session_id}/{action}", session_action, methods=["POST"]),
            Route("/api/backends", backends_endpoint),
            Route("/api/hotkeys", hotkeys),
            Route("/api/ui", ui_config),
            Route("/api/reload", reload_config, methods=["POST"]),
            WebSocketRoute("/ws", websocket_endpoint),
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
            Route("/", index),
        ],
    )


async def _run_socket(websocket: WebSocket, coordinator: SessionCoordinator) -> None:
    with coordinator.bus.subscribe() as queue:
        await websocket.send_json(coordinator.snapshot())
        sender = asyncio.create_task(_send_events(websocket, queue))
        try:
            while True:
                action = await websocket.receive_json()
                _dispatch(coordinator, action)
        except WebSocketDisconnect as disconnect:
            log.info("ws disconnect: code=%s", disconnect.code)
        except Exception:
            log.exception("ws error")
        finally:
            sender.cancel()


async def _send_events(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            await websocket.send_json(await queue.get())
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("ws send stopped", exc_info=True)


def _dispatch(coordinator: SessionCoordinator, action: dict) -> None:
    name = action.get("action")
    session_id = action.get("session_id")
    coordinator.log.info("action=%s session=%s", name, session_id)
    if name == "spawn":
        _spawn(coordinator.add_session(action.get("path"), action.get("name"),
                                       action.get("backend"), bool(action.get("ephemeral"))))
    elif name == "open":
        coordinator.open_session(session_id)
    elif name == "resume":
        _spawn(coordinator.resume_session(session_id))
    elif name == "rename":
        coordinator.rename_session(session_id, action.get("name", ""))
    elif name == "name":
        _spawn(coordinator.name_session(session_id))
    elif name == "set_config_option":
        _spawn(coordinator.set_config_option(session_id, action["config_id"], action["value"]))
    elif name == "send":
        _spawn(coordinator.send(session_id, action.get("text", "")))
    elif name == "cancel":
        _spawn(coordinator.cancel(session_id))
    elif name == "stop":
        _spawn(coordinator.stop_session(session_id))
    elif name == "delete":
        _spawn(coordinator.delete_session(session_id))
    elif name == "revert":
        _spawn(coordinator.revert_session(session_id, action["event_index"]))
    elif name == "fork":
        _spawn(coordinator.fork_session(session_id, action.get("event_index")))
    else:
        coordinator.log.warning("ignoring unknown action: %s", name)


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
