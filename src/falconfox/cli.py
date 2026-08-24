"""The ``falconfox`` daemon launcher and session control plane."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from . import state


class CliError(Exception):
    pass


def _base_url() -> str:
    explicit = os.environ.get("FALCONFOX_URL")
    if explicit:
        return explicit.rstrip("/")
    info = state.find_running_server()
    if info is None:
        raise CliError("FalconFox daemon is not running. Start it with `falconfox daemon`.")
    return f"http://127.0.0.1:{info.port}"


def _request(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{_base_url()}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=None) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", str(error))
        except Exception:
            detail = str(error)
        raise CliError(detail) from error
    except urllib.error.URLError as error:
        raise CliError(f"could not reach FalconFox daemon: {error.reason}") from error


def _wait_for_server(timeout: float = 5.0) -> state.ServerInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = state.read_server_info()
        if info is not None and state.is_port_responding(info.port):
            return info
        time.sleep(0.05)
    return None


def _start_daemon(host: str) -> state.ServerInfo:
    log_path = Path(os.environ.get("FALCONFOX_LOG_PATH") or state.log_path())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a")  # held by the child
    subprocess.Popen(
        [sys.executable, "-m", "falconfox", "daemon", "--foreground", "--host", host],
        start_new_session=True,
        stdout=log_file,
        stderr=log_file,
        env={**os.environ, "FALCONFOX_DAEMON": "1"},
    )
    info = _wait_for_server()
    if info is None:
        raise CliError(f"daemon failed to start; check {log_path}")
    return info


def _guard_self_target(session_id: str, action: str) -> None:
    current = os.environ.get("FALCONFOX_SESSION_ID")
    if current and current == session_id and action in ("stop", "delete"):
        raise CliError(f"a session cannot {action} itself ({session_id})")


def cmd_daemon(args) -> None:
    if args.stop:
        if os.environ.get("FALCONFOX_SESSION_ID"):
            raise CliError("a FalconFox session cannot stop its own daemon")
        info = state.stop_server(wait=True)
        if info is None:
            raise CliError("no running daemon found")
        print(f"Stopped FalconFox daemon (pid {info.pid}).")
        return
    if args.restart:
        if os.environ.get("FALCONFOX_SESSION_ID"):
            raise CliError("a FalconFox session cannot restart its own daemon")
        state.stop_server(wait=True)
    if args.foreground:
        from .web.server import serve
        serve(host=args.host, port=state.find_available_port(host=args.host),
              open_browser=args.browser)
        return
    info = state.find_running_server()
    if info is None:
        info = _start_daemon(args.host)
        print(f"FalconFox daemon started on port {info.port} (pid {info.pid}).")
    else:
        print(f"FalconFox daemon already running on port {info.port} (pid {info.pid}).")
    if args.browser:
        webbrowser.open(f"http://127.0.0.1:{info.port}")


def cmd_spawn(args) -> None:
    session = _request("POST", "/api/sessions", {
        "path": str(Path(args.path).expanduser()),
        "name": args.name,
        "backend": args.backend,
        "ephemeral": args.ephemeral,
    })
    print(session["session_id"])


def cmd_list(args) -> None:
    suffix = "?include_ephemeral=true" if args.all else ""
    sessions = _request("GET", f"/api/sessions{suffix}")
    if args.json:
        print(json.dumps(sessions, indent=2))
        return
    if not sessions:
        print("No sessions.")
        return
    widths = {
        "id": max(8, max(len(item["session_id"]) for item in sessions)),
        "name": min(32, max(4, max(len(item["name"]) for item in sessions))),
        "backend": max(7, max(len(item["backend"]) for item in sessions)),
    }
    print(f"{'ID':<{widths['id']}}  {'NAME':<{widths['name']}}  "
          f"{'STATE':<8}  {'BACKEND':<{widths['backend']}}  PATH")
    for item in sessions:
        name = item["name"][:widths["name"]]
        print(f"{item['session_id']:<{widths['id']}}  {name:<{widths['name']}}  "
              f"{item['state']:<8}  {item['backend']:<{widths['backend']}}  {item['path']}")


def _agent_reply(transcript: list[dict]) -> str:
    parts: list[str] = []
    for event in reversed(transcript):
        if event.get("type") == "message" and event.get("role") == "user":
            break
        if event.get("type") == "message" and event.get("role") == "agent":
            parts.append(event.get("text", ""))
    return "".join(reversed(parts)).strip()


def cmd_send(args) -> None:
    _request("POST", f"/api/sessions/{args.session_id}/send", {"text": args.message})
    detail = _request("GET", f"/api/sessions/{args.session_id}")
    reply = _agent_reply(detail["transcript"])
    if reply:
        print(reply)


def cmd_read(args) -> None:
    detail = _request("GET", f"/api/sessions/{args.session_id}")
    if args.json:
        print(json.dumps(detail["transcript"], indent=2))
        return
    for event in detail["transcript"]:
        if event.get("type") == "message" and event.get("role") in ("user", "agent"):
            print(f"{event['role']}: {event.get('text', '')}")
        elif event.get("type") == "notice" and event.get("level") == "error":
            print(f"error: {event.get('message', '')}")


def cmd_simple(args) -> None:
    _guard_self_target(args.session_id, args.command)
    if args.command == "delete":
        _request("DELETE", f"/api/sessions/{args.session_id}")
    else:
        _request("POST", f"/api/sessions/{args.session_id}/{args.command}", {})


def cmd_rename(args) -> None:
    _request("POST", f"/api/sessions/{args.session_id}/rename", {"name": args.name})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="falconfox",
                                     description="Remote ACP session daemon and control plane.")
    sub = parser.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="start or manage the daemon")
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--foreground", action="store_true")
    daemon.add_argument("--browser", action="store_true")
    daemon.add_argument("--stop", action="store_true")
    daemon.add_argument("--restart", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    spawn = sub.add_parser("spawn", help="spawn a session")
    spawn.add_argument("--path", default=str(Path.home()))
    spawn.add_argument("--name")
    spawn.add_argument("--backend")
    spawn.add_argument("--ephemeral", action="store_true")
    spawn.set_defaults(func=cmd_spawn)

    listing = sub.add_parser("list", help="list persisted/non-ephemeral sessions")
    listing.add_argument("--all", action="store_true", help="include live ephemeral sessions")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(func=cmd_list)

    send = sub.add_parser("send", help="send a prompt, resuming the session if needed")
    send.add_argument("session_id")
    send.add_argument("message")
    send.set_defaults(func=cmd_send)

    read = sub.add_parser("read", help="read a saved transcript")
    read.add_argument("session_id")
    read.add_argument("--json", action="store_true")
    read.set_defaults(func=cmd_read)

    for command in ("resume", "stop", "delete"):
        action = sub.add_parser(command)
        action.add_argument("session_id")
        action.set_defaults(func=cmd_simple)

    rename = sub.add_parser("rename")
    rename.add_argument("session_id")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_rename)
    return parser


def main() -> None:
    parser = build_parser()
    try:
        args = parser.parse_args()
        args.func(args)
    except CliError as error:
        parser.exit(1, f"falconfox: {error}\n")


if __name__ == "__main__":
    main()
