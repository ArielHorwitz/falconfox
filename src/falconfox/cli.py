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
from typing import Optional

from . import help as ffhelp, state


class CliError(Exception):
    pass


# How long the CLI waits for the daemon before giving up. It had no timeout
# at all, so an agent running `falconfox` inside a session against a wedged
# daemon hung for the rest of its turn with nothing to show for it. These are
# read timeouts on a loopback socket: anything but a wedged daemon answers at
# once, so the number is only ever the cost of finding out.
DEFAULT_TIMEOUT = 30.0
# `send` waits out the whole turn it starts, which is as long as the agent
# takes to think.
TURN_TIMEOUT = 1800.0
# `attach` waits for a client to take the file and report back. The daemon
# caps that itself (coordinator.ATTACHMENT_TIMEOUT, 120s), and this sits
# above it so the daemon's own answer arrives rather than being cut off here.
ATTACH_TIMEOUT = 150.0


def _base_url() -> str:
    explicit = os.environ.get("FALCONFOX_URL")
    if explicit:
        return explicit.rstrip("/")
    info = state.find_running_server()
    if info is None:
        raise CliError("FalconFox daemon is not running. Start it with `falconfox daemon`.")
    return f"http://127.0.0.1:{info.port}"


def _request(method: str, path: str, body: dict | None = None,
             timeout: float = DEFAULT_TIMEOUT):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{_base_url()}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            return json.loads(payload) if payload else None
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read()).get("error", str(error))
        except Exception:
            detail = str(error)
        raise CliError(detail) from error
    except urllib.error.URLError as error:
        # A read that times out arrives here wrapped, and a connect that
        # times out arrives bare below. Both are the same answer to the
        # caller, and neither is the "nothing is listening" that the
        # unqualified message would imply.
        if isinstance(error.reason, TimeoutError):
            raise CliError(_timed_out(timeout)) from error
        raise CliError(f"could not reach FalconFox daemon: {error.reason}") from error
    except TimeoutError as error:
        raise CliError(_timed_out(timeout)) from error


def _timed_out(timeout: float) -> str:
    return (f"the FalconFox daemon did not answer within {timeout:.0f}s. It is "
            f"running but not responding; check its log.")


def _wait_for_server(timeout: float = 5.0) -> state.ServerInfo | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = state.read_server_info()
        if info is not None and state.is_port_responding(info.port):
            return info
        time.sleep(0.05)
    return None


def _start_daemon(host: str, port: Optional[int] = None) -> state.ServerInfo:
    log_path = Path(os.environ.get("FALCONFOX_LOG_PATH") or state.log_path())
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a")  # held by the child
    subprocess.Popen(
        [sys.executable, "-m", "falconfox", "daemon", "--foreground", "--host", host]
        + (["--port", str(port)] if port is not None else []),
        start_new_session=True,
        stdout=log_file,
        stderr=log_file,
        env={**os.environ, "FALCONFOX_DAEMON": "1"},
    )
    info = _wait_for_server()
    if info is None:
        raise CliError(f"daemon failed to start; check {log_path}")
    return info


# Telegram's bot API refuses a document over 50MB, and it is the only client
# that can send one today. Checked here so the agent is told by the command it
# ran, rather than by a failure that surfaces two processes away.
MAX_ATTACHMENT_BYTES = 50 * 1000 * 1000


def cmd_attach(args) -> None:
    session_id = os.environ.get("FALCONFOX_SESSION_ID")
    if not session_id:
        raise CliError("not running inside a FalconFox session, so there is no "
                       "chat to send a file to")
    for name in args.paths:
        source = Path(name).expanduser()
        if not source.is_file():
            raise CliError(f"not a file: {source}")
        size = source.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise CliError(f"{source.name} is {size / 1_000_000:.0f}MB, over the "
                           f"{MAX_ATTACHMENT_BYTES // 1_000_000}MB limit")
        _request("POST", f"/api/sessions/{session_id}/attach",
                 {"path": str(source.resolve()), "caption": args.caption,
                  "ack": not args.no_ack, "raw": args.raw},
                 timeout=ATTACH_TIMEOUT)
        print(f"sent {source.name}" if not args.no_ack
              else f"handed {source.name} to the client")


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
        # A pinned port is bound as given and never searched past: two
        # instances on one host (a deployment and its development twin) are
        # only reliably separable if each one's port is a fact rather than
        # whatever was free when it happened to start. Failing to bind is
        # then the honest outcome -- drifting onto the neighbour's port is
        # how a bot ends up driving the wrong daemon's sessions.
        port = args.port if args.port is not None else state.find_available_port(host=args.host)
        serve(host=args.host, port=port, open_browser=args.browser)
        return
    info = state.find_running_server()
    if info is None:
        info = _start_daemon(args.host, args.port)
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
        "roles": args.role or None,
    })
    print(session["session_id"])


def cmd_help(args) -> None:
    """Print a help module, or list what is registered.

    Read straight off disk rather than through the daemon: the files are
    already there and the daemon adds nothing to them. It does have to be
    running, since it is what publishes the directory they live in, which for
    an agent inside a session is true by construction.
    """
    info = state.read_server_info()
    if info is None:
        raise CliError("the daemon is not running, so there is no help to read")
    # A daemon that publishes no directory is just a daemon with no help,
    # which is the same answer as an empty one. Naming the reason -- an older
    # daemon, say -- would be a special case that stops being true shortly and
    # says less than "nothing is registered" in every other case.
    directory = getattr(info, "clients_dir", None)
    run_dir = Path(directory).parent if directory else None
    listing = ffhelp.index(run_dir) if run_dir else ""
    if not args.topic:
        print(f"Help modules ({ffhelp.READ_ONE}):\n\n{listing}" if listing
              else "No help is registered.")
        return
    body = ffhelp.lookup(run_dir, args.topic) if run_dir else None
    if body is None:
        raise CliError(f"no help found for {args.topic!r}"
                       + (f". Help modules ({ffhelp.READ_ONE}):\n{listing}"
                          if listing else ". Nothing is registered."))
    # A branch has no text of its own, so what comes back is a listing and
    # needs the same invitation a bare call gets.
    if body == ffhelp.index(run_dir, args.topic):
        body = f"Help modules under {args.topic} ({ffhelp.READ_ONE}):\n\n{body}"
    print(body)


def cmd_list(args) -> None:
    suffix = "?include_hidden=true" if args.all else ""
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
        "tags": max(4, max(len(",".join(item.get("tags") or [])) for item in sessions)),
    }
    print(f"{'ID':<{widths['id']}}  {'NAME':<{widths['name']}}  "
          f"{'STATE':<8}  {'BACKEND':<{widths['backend']}}  "
          f"{'TAGS':<{widths['tags']}}  PATH")
    for item in sessions:
        name = item["name"][:widths["name"]]
        tags = ",".join(item.get("tags") or [])
        print(f"{item['session_id']:<{widths['id']}}  {name:<{widths['name']}}  "
              f"{item['state']:<8}  {item['backend']:<{widths['backend']}}  "
              f"{tags:<{widths['tags']}}  {item['path']}")


def _agent_reply(transcript: list[dict]) -> str:
    parts: list[str] = []
    for event in reversed(transcript):
        if event.get("type") == "message" and event.get("role") == "user":
            break
        if event.get("type") == "message" and event.get("role") == "agent":
            parts.append(event.get("text", ""))
    return "".join(reversed(parts)).strip()


def cmd_send(args) -> None:
    # This one call is the whole turn: the daemon answers when the agent has
    # finished, so it is the one place a long wait is the correct behaviour.
    _request("POST", f"/api/sessions/{args.session_id}/send",
             {"text": args.message}, timeout=TURN_TIMEOUT)
    detail = _request(
        "GET", f"/api/sessions/{args.session_id}?include_transcript=true")
    reply = _agent_reply(detail["transcript"])
    if reply:
        print(reply)


def cmd_read(args) -> None:
    detail = _request(
        "GET", f"/api/sessions/{args.session_id}?include_transcript=true")
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


def cmd_tag(args) -> None:
    session = _request("POST", f"/api/sessions/{args.session_id}/tag",
                       {"tags": args.tags})
    tags = session.get("tags") or []
    print(" ".join(tags) if tags else "(no tags)")


# Commands that are a session id and nothing else. Each posts the daemon action
# of its own name, so the names here and the daemon's action table (see
# falconfox/web/actions.py) have to agree; a test holds them to it.
SIMPLE_COMMANDS = ("resume", "stop", "delete")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="falconfox",
                                     description="Remote ACP session daemon and control plane.")
    sub = parser.add_subparsers(dest="command", required=True)

    attach = sub.add_parser("attach", help="send a file to this session's chat")
    attach.add_argument("paths", nargs="+", help="files to send")
    attach.add_argument("--caption", default=None, help="text shown with the file")
    attach.add_argument("--no-ack", action="store_true",
                        help="do not wait for the client to confirm delivery")
    attach.add_argument("--raw", action="store_true",
                        help="send the file as-is, without the client "
                             "compressing it (images are compressed by default "
                             "so they display in the chat)")
    attach.set_defaults(func=cmd_attach)

    daemon = sub.add_parser("daemon", help="start or manage the daemon")
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=None,
                        help="bind this exact port instead of searching from 9721")
    daemon.add_argument("--foreground", action="store_true")
    daemon.add_argument("--browser", action="store_true")
    daemon.add_argument("--stop", action="store_true")
    daemon.add_argument("--restart", action="store_true")
    daemon.set_defaults(func=cmd_daemon)

    spawn = sub.add_parser("spawn", help="spawn a session")
    spawn.add_argument("--path", default=str(Path.home()))
    spawn.add_argument("--name")
    spawn.add_argument("--backend")
    # Clients register help alongside their orientation, so what is available
    # depends on what is running rather than on this parser.
    help_command = sub.add_parser("help", help="read registered help")
    help_command.add_argument("topic", nargs="?", metavar="module",
                              help="dotted module path, e.g. telegram.commands")
    help_command.set_defaults(func=cmd_help)
    spawn.add_argument("--ephemeral", action="store_true")
    # Repeatable, because roles compose: nothing about running the session
    # lifecycle conflicts with a session also being something else. Names are
    # namespaced by whoever registered them -- `telegram.concierge`, or a bare
    # `.manager` for the daemon's own.
    spawn.add_argument("--role", action="append", metavar="[client.]role",
                       help="give the session a role (repeatable)")
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

    for command in SIMPLE_COMMANDS:
        action = sub.add_parser(command)
        action.add_argument("session_id")
        action.set_defaults(func=cmd_simple)

    rename = sub.add_parser("rename")
    rename.add_argument("session_id")
    rename.add_argument("name")
    rename.set_defaults(func=cmd_rename)

    tag = sub.add_parser("tag", help="replace a session's tags (no tags clears them)")
    tag.add_argument("session_id")
    tag.add_argument("tags", nargs="*",
                     help="lowercase, whitespace-free labels; order is kept")
    tag.set_defaults(func=cmd_tag)
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
