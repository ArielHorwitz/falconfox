"""Central on-disk persistence for FalconFox sessions.

Sessions live under ``$XDG_STATE_HOME/falconfox/sessions/<session_id>/``. Their
working directories are metadata only: FalconFox never writes bookkeeping into
the repositories in which agents work.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from . import logsetup
from .state import state_dir

log = logsetup.get_logger("storage")

META_FILENAME = "meta.toml"
TRANSCRIPT_FILENAME = "transcript.jsonl"


class SessionStore:
    """Reads and writes flat, globally keyed session state."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or state_dir().joinpath("sessions")).resolve()

    def _session_dir(self, session_id: str) -> Path:
        return self.root.joinpath(session_id)

    def write_meta(self, meta: dict) -> None:
        session_dir = self._session_dir(meta["session_id"])
        session_dir.mkdir(parents=True, exist_ok=True)
        session_dir.joinpath(META_FILENAME).write_text(_to_toml(meta))

    def append_event(self, session_id: str, event: dict) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        with session_dir.joinpath(TRANSCRIPT_FILENAME).open("a") as file:
            file.write(json.dumps(event) + "\n")

    def delete(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def rewrite_transcript(self, session_id: str, events: list[dict]) -> None:
        session_dir = self._session_dir(session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = session_dir.joinpath(TRANSCRIPT_FILENAME)
        tmp = transcript_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(event) + "\n" for event in events))
        tmp.replace(transcript_path)

    def load_all_meta(self) -> list[dict]:
        """Read every session's small metadata file, never its transcript."""
        if not self.root.exists():
            return []
        metas: list[dict] = []
        for session_dir in sorted(self.root.iterdir()):
            if not session_dir.is_dir():
                continue
            meta_path = session_dir.joinpath(META_FILENAME)
            if not meta_path.exists():
                continue
            try:
                metas.append(tomllib.loads(meta_path.read_text()))
            except (tomllib.TOMLDecodeError, OSError) as error:
                log.warning("skipping unreadable session meta %s: %s", meta_path, error)
        return metas

    def read_transcript(self, session_id: str) -> list[dict]:
        return _read_transcript(self._session_dir(session_id).joinpath(TRANSCRIPT_FILENAME))


def _read_transcript(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as error:
            log.warning("skipping bad transcript line %s:%d: %s", path, number, error)
    return events


def _to_toml(meta: dict) -> str:
    lines = [
        f"{key} = {_format_toml_value(value)}"
        for key, value in meta.items()
        if value is not None
    ]
    return "\n".join(lines) + "\n"


def _format_toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_format_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML metadata value: {type(value).__name__}")
