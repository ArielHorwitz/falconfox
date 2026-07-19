"""Starlette app: REST for read-only case views, WebSocket for live work.

The server is project-agnostic at startup. Projects are selected through the UI;
each gets its own CaseCoordinator, lazily created and cached for the lifetime of
the process. REST endpoints under ``/api/projects/{project_id}/`` are scoped to a
single project; the project browser at ``/api/projects`` reads from the global
path cache.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from .. import cases, config, get_version, logsetup, projects, state
from ..coordinator import CaseCoordinator

STATIC_DIR = Path(__file__).parent.joinpath("static")

log = logsetup.get_logger("server")


def create_app(
    *,
    write_info: bool = False,
    open_browser: bool = False,
    bound_port: int = 0,
    project_path: str | None = None,
) -> Starlette:
    coordinators: dict[str, CaseCoordinator] = {}

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        if write_info:
            from .. import state
            state.write_server_info(bound_port)
        if open_browser:
            import webbrowser
            from urllib.parse import quote
            url = f"http://127.0.0.1:{bound_port}"
            if project_path is not None:
                resolved = str(Path(project_path).resolve())
                url = f"{url}/?path={quote(resolved, safe='/')}"
            webbrowser.open(url)
        try:
            yield
        finally:
            log.info("server shutting down (%d project(s) active)", len(coordinators))
            if write_info:
                from .. import state
                state.remove_server_info()
            for coordinator in coordinators.values():
                await coordinator.shutdown()

    def get_coordinator(project_id: str) -> CaseCoordinator:
        """Look up or lazily create a coordinator for the given project."""
        if project_id in coordinators:
            return coordinators[project_id]
        project_root = projects.resolve_project(project_id)
        log.info("creating coordinator for project=%s root=%s",
                 project_id, project_root)
        coordinator = CaseCoordinator(project_root)
        coordinator.load_persisted()
        coordinators[project_id] = coordinator
        projects.touch_project(project_id)
        return coordinator

    def coordinator_or_404(project_id: str):
        """``(coordinator, None)`` for a known project, else ``(None, response)``.

        Logs the miss at DEBUG — usually a stale browser tab pointed at a project
        the daemon no longer knows; benign, but otherwise an invisible 404.
        """
        try:
            return get_coordinator(project_id), None
        except cases.CasebookError as error:
            log.debug("no coordinator for project=%s: %s", project_id, error)
            return None, JSONResponse({"error": str(error)}, status_code=404)

    # --- HTML (single document for all client-side routes) ---------------

    async def index(_request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR.joinpath("index.html"))

    async def version_endpoint(_request: Request) -> JSONResponse:
        return JSONResponse({"version": get_version()})

    # --- project browser -------------------------------------------------

    async def projects_endpoint(request: Request) -> JSONResponse:
        if request.method == "POST":
            body = await request.json()
            path = Path(body.get("path", ""))
            try:
                entry = projects.open_project(path)
                # Eagerly create the coordinator so case counts are available.
                coordinator = get_coordinator(entry["id"])
                entry["cases"] = len(coordinator.list_cases())
                return JSONResponse(entry, status_code=201)
            except cases.CasebookError as error:
                # projects.open_project already WARNs the common failure (bad
                # path); keep a boundary trace for the rarer coordinator-create
                # failure without double-warning.
                log.debug("POST /api/projects failed: path=%s: %s", path, error)
                return JSONResponse({"error": str(error)}, status_code=400)
        # GET: list projects with case counts.
        entries = projects.list_projects()
        for entry in entries:
            try:
                coordinator = get_coordinator(entry["id"])
                entry["cases"] = len(coordinator.list_cases())
            except cases.CasebookError as error:
                # A registered project that no longer loads would otherwise show a
                # silent 0 on the home screen, indistinguishable from "empty".
                log.warning("project failed to load, listing 0 cases: id=%s: %s",
                            entry["id"], error)
                entry["cases"] = 0
        return JSONResponse(entries)

    async def remove_project_endpoint(request: Request) -> JSONResponse:
        pid = request.path_params["project_id"]
        if pid in coordinators:
            await coordinators[pid].shutdown()
            del coordinators[pid]
        if projects.remove_project(pid):  # logs the removal itself
            return JSONResponse({"removed": pid})
        log.warning("remove project: unknown id=%s", pid)
        return JSONResponse({"error": "unknown project"}, status_code=404)

    # --- project-scoped case endpoints -----------------------------------

    async def cases_endpoint(request: Request) -> JSONResponse:
        coordinator, error = coordinator_or_404(request.path_params["project_id"])
        if error is not None:
            return error
        if request.method == "POST":
            body = await request.json()
            summary = coordinator.create_case(body.get("title", "Unnamed case"))
            return JSONResponse(summary, status_code=201)
        return JSONResponse(coordinator.list_cases())

    async def promote(request: Request) -> JSONResponse:
        coordinator, error = coordinator_or_404(request.path_params["project_id"])
        if error is not None:
            return error
        body = await request.json()
        agent_id = body["agent_id"]
        new_case_id = await coordinator.promote_agent(
            agent_id, body.get("title", "Unnamed case")
        )
        if new_case_id is None:
            log.debug("promote: agent=%s is not a scratch session", agent_id)
            return JSONResponse({"error": "not a scratch session"}, status_code=400)
        log.info("promoted agent=%s to case=%s", agent_id, new_case_id)
        return JSONResponse({"case_id": new_case_id}, status_code=201)

    async def list_backends(request: Request) -> JSONResponse:
        coordinator, error = coordinator_or_404(request.path_params["project_id"])
        if error is not None:
            return error
        return JSONResponse(coordinator.list_backends())

    async def global_hotkeys(_request: Request) -> JSONResponse:
        return JSONResponse(config.global_hotkeys())

    async def hotkeys(request: Request) -> JSONResponse:
        coordinator, error = coordinator_or_404(request.path_params["project_id"])
        if error is not None:
            return error
        return JSONResponse(coordinator.hotkeys())

    async def ui_config(request: Request) -> JSONResponse:
        coordinator, error = coordinator_or_404(request.path_params["project_id"])
        if error is not None:
            return error
        return JSONResponse(coordinator.ui_config())

    async def case_detail(request: Request) -> JSONResponse:
        pid = request.path_params["project_id"]
        case_id = request.path_params["case_id"]
        try:
            coordinator = get_coordinator(pid)
            if request.method == "DELETE":
                await coordinator.delete_case(case_id)  # logs the deletion itself
                return JSONResponse({"deleted": case_id})
            return JSONResponse(coordinator.case_detail(case_id))
        except cases.CasebookError as error:
            log.debug("case %s (project=%s) request failed: %s", case_id, pid, error)
            return JSONResponse({"error": str(error)}, status_code=404)

    async def case_file(request: Request):
        pid = request.path_params["project_id"]
        filename = request.path_params["filename"]
        try:
            coordinator = get_coordinator(pid)
            content = coordinator.read_case_file(request.path_params["case_id"], filename)
            return PlainTextResponse(content)
        except (cases.CasebookError, OSError) as error:
            log.debug("case file %s (project=%s) request failed: %s", filename, pid, error)
            return PlainTextResponse(str(error), status_code=404)

    # --- global config reload -----------------------------------------------

    async def reload_config(request: Request) -> JSONResponse:
        log.info("config reload requested for %d project(s)", len(coordinators))
        for coordinator in coordinators.values():
            coordinator.reload_config()
        return JSONResponse({"reloaded": True})

    # --- WebSocket (project-scoped) --------------------------------------

    async def websocket_endpoint(websocket: WebSocket) -> None:
        pid = websocket.path_params["project_id"]
        case_id = websocket.query_params.get("case")
        client = websocket.client
        peer = f"{client.host}:{client.port}" if client else "?"
        log.info("ws connect: project=%s case=%s client=%s", pid, case_id, peer)
        try:
            coordinator = get_coordinator(pid)
        except cases.CasebookError as error:
            # A browser holding a URL for a project the daemon no longer knows
            # about (stale tab, wiped cache): reject cleanly, but say so — this
            # path is otherwise invisible and looks like a bare disconnect.
            log.warning("ws rejected: project=%s client=%s reason=%s",
                        pid, peer, error)
            await websocket.close(code=4000, reason="unknown project")
            return
        await websocket.accept()
        await _run_socket(websocket, coordinator, case_id)

    return Starlette(
        lifespan=lifespan,
        routes=[
            # Project browser
            Route("/api/version", version_endpoint),
            Route("/api/projects", projects_endpoint, methods=["GET", "POST"]),
            Route("/api/projects/{project_id}", remove_project_endpoint,
                  methods=["DELETE"]),
            Route("/api/hotkeys", global_hotkeys),
            Route("/api/reload", reload_config, methods=["POST"]),
            # Project-scoped API
            Route("/api/projects/{project_id}/cases", cases_endpoint,
                  methods=["GET", "POST"]),
            Route("/api/projects/{project_id}/cases/{case_id}", case_detail,
                  methods=["GET", "DELETE"]),
            Route("/api/projects/{project_id}/cases/{case_id}/files/{filename}",
                  case_file),
            Route("/api/projects/{project_id}/promote", promote, methods=["POST"]),
            Route("/api/projects/{project_id}/backends", list_backends),
            Route("/api/projects/{project_id}/hotkeys", hotkeys),
            Route("/api/projects/{project_id}/ui", ui_config),
            # Project-scoped WebSocket
            WebSocketRoute("/ws/{project_id}", websocket_endpoint),
            # Static assets
            Mount("/static", app=StaticFiles(directory=STATIC_DIR), name="static"),
            # Catch-all: serve index.html for all client-side routes.
            Route("/", index),
            Route("/project/{project_id:path}", index),
        ],
    )


async def _run_socket(
    websocket: WebSocket, coordinator: CaseCoordinator, case_id: str | None
) -> None:
    """Pump coordinator events out and user actions in until the socket closes."""
    with coordinator.bus.subscribe() as queue:
        snapshot = coordinator.snapshot(case_id)
        log.info("ws open: case=%s agents=%d", case_id, len(snapshot["agents"]))
        await websocket.send_json(snapshot)
        sender = asyncio.create_task(_send_events(websocket, queue))
        try:
            while True:
                action = await websocket.receive_json()
                _dispatch(coordinator, action)
        except WebSocketDisconnect as disconnect:
            log.info("ws disconnect: case=%s code=%s", case_id, disconnect.code)
        except Exception:
            # An unexpected error here closes the socket and reads as a bare
            # disconnect in the browser; the traceback is the only way to tell
            # this apart from a clean client-side close.
            log.exception("ws error: case=%s", case_id)
        finally:
            sender.cancel()


async def _send_events(websocket: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            event = await queue.get()
            await websocket.send_json(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        # The socket went away between receive and send; the receive side will
        # observe the disconnect. Note it at debug so a persistent send failure
        # isn't wholly silent.
        log.debug("ws send stopped", exc_info=True)


def _dispatch(coordinator: CaseCoordinator, action: dict) -> None:
    name = action.get("action")
    # The inbound audit trail: every user action, no message content. This is the
    # line that shows what a stray keypress actually triggered.
    coordinator.log.info("action=%s agent=%s case=%s", name,
                         action.get("agent_id"), action.get("case_id"))
    if name == "add_agent":
        _spawn(coordinator.add_agent(action["case_id"], action.get("label"),
                                     action.get("backend")))
    elif name == "resume_agent":
        _spawn(coordinator.resume_agent(action["agent_id"]))
    elif name == "rename_agent":
        coordinator.rename_agent(action["agent_id"], action.get("label", ""))
    elif name == "name_agent":
        _spawn(coordinator.name_agent(action["agent_id"]))
    elif name == "set_model":
        _spawn(coordinator.set_model(action["agent_id"], action["model_id"]))
    elif name == "send":
        _spawn(coordinator.send(action["agent_id"], action["text"]))
    elif name == "cancel":
        _spawn(coordinator.cancel(action["agent_id"]))
    elif name == "close_agent":
        _spawn(coordinator.close_agent(action["agent_id"]))
    elif name == "delete_agent":
        _spawn(coordinator.delete_agent(action["agent_id"]))
    elif name == "permission":
        coordinator.resolve_permission(action["request_id"], action.get("option_id"))
    elif name == "set_always_allow":
        coordinator.set_always_allow(action["agent_id"], action.get("value", False))
    elif name == "revert_agent":
        _spawn(coordinator.revert_agent(action["agent_id"], action["event_index"]))
    elif name == "fork_agent":
        _spawn(coordinator.fork_agent(action["agent_id"], action.get("event_index")))
    else:
        coordinator.log.warning("ignoring unknown action: %s", name)


def _spawn(coro) -> None:
    """Run a coordinator coroutine in the background; errors become notices."""
    asyncio.create_task(_guard(coro))


async def _guard(coro) -> None:
    try:
        await coro
    except Exception:
        # Surfaced to the user as notices where it matters; logged at debug so a
        # background failure still leaves a traceback for `CASEBOOK_LOG_LEVEL=DEBUG`.
        log.debug("background action failed", exc_info=True)


def serve(
    host: str = "127.0.0.1",
    port: int = 9721,
    open_browser: bool = False,
    project_path: str | None = None,
) -> None:
    # The daemon (spawned with CASEBOOK_DAEMON=1) is the singleton, discoverable
    # instance: it owns server.json and has no terminal, so the parent redirects
    # its stdout/stderr into one log file — our stream handler writes there too,
    # unifying structured events and raw crash/uvicorn output. A user-run
    # foreground instance is for development: it echoes to the console and, unless
    # CASEBOOK_LOG_PATH names a file, writes none (the console is enough live).
    daemon = os.environ.get("CASEBOOK_DAEMON") == "1"
    override = os.environ.get("CASEBOOK_LOG_PATH")
    if daemon:
        # The parent's redirect owns the file; we just stream into it.
        log_file = None
        destination = override or str(state.log_path())
    else:
        log_file = Path(override) if override else None
        destination = str(log_file) if log_file else "console only"
    level = os.environ.get("CASEBOOK_LOG_LEVEL") or config.log_level()
    logsetup.configure(log_file, level)
    # uvicorn's own logs (startup, and the per-request access log that records the
    # WebSocket upgrade) are otherwise hidden. Open them up under DEBUG so a user
    # reporting a connection issue captures the handshake; stay quiet otherwise.
    uvicorn_level = "info" if str(level).upper() == "DEBUG" else "warning"
    log.info(
        "casebook serving on http://%s:%s (pid=%s, log=%s, level=%s)",
        host, port, os.getpid(), destination, level,
    )
    app = create_app(
        write_info=daemon,
        open_browser=open_browser,
        bound_port=port,
        project_path=project_path,
    )
    uvicorn.run(app, host=host, port=port, log_level=uvicorn_level, access_log=True)
