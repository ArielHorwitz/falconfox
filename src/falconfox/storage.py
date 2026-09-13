"""Central on-disk persistence for FalconFox sessions.

Sessions live under ``$XDG_STATE_HOME/falconfox/sessions/<session_id>/``. Their
working directories are metadata only: FalconFox never writes bookkeeping into
the repositories in which agents work.
"""

from __future__ import annotations

import json
import re
import shutil
import tomllib
import uuid
from pathlib import Path

from . import logsetup
from .state import state_dir

log = logsetup.get_logger("storage")

META_FILENAME = "meta.toml"
TRANSCRIPT_FILENAME = "transcript.jsonl"
INBOX_DIRNAME = "inbox"
# Long enough to read as an address and short enough to type on a phone, which
# is where these are tapped. The same width as a session id, for the same
# reason.
FILE_ID_WIDTH = 8
# A stored name is decoration with one hard requirement: it must be a single
# path component. Everything a filesystem or a shell would rather not see goes,
# and the length cap is about a phone screen rather than about any limit.
_UNSAFE_IN_NAME = re.compile(r"[\x00-\x1f/\\]")
_NAME_LIMIT = 120
_FALLBACK_NAME = "file"


class SessionStore:
    """Reads and writes flat, globally keyed session state."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or state_dir().joinpath("sessions")).resolve()

    def _session_dir(self, session_id: str) -> Path:
        return self.root.joinpath(session_id)

    def write_meta(self, meta: dict) -> None:
        """Replace a session's metadata, atomically.

        The same temp-then-replace the transcript rewrite uses, and for a
        harder reason: metadata is rewritten on every replayable event, so a
        write that can tear is a window open continuously during use, and a
        `meta.toml` that will not parse is a session the loader skips
        entirely -- gone from every listing, transcript orphaned on disk.
        """
        session_dir = self._session_dir(meta["session_id"])
        session_dir.mkdir(parents=True, exist_ok=True)
        meta_path = session_dir.joinpath(META_FILENAME)
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(_to_toml(meta))
        tmp.replace(meta_path)

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

    # --- the inbox: files given to a session from outside it ---------------
    #
    # A directory per file rather than an id for a filename, so collisions are
    # impossible while the real name survives intact: `prod-error.log` says
    # something that `a1b2c3d4.log` does not, and renaming would have to guess
    # at extensions like `.tar.gz`.
    #
    # The directory is the whole of the state. The id is its name, the file's
    # name is its filename, and when it arrived is its mtime, so there is no
    # record to keep in step with any of them. Anything else about a file --
    # the caption it came with, whether it has been handed over yet -- belongs
    # to whichever client received it, and stays there.

    def inbox_dir(self, session_id: str) -> Path:
        return self._session_dir(session_id).joinpath(INBOX_DIRNAME)

    def add_file(self, session_id: str, source: Path, name: str) -> tuple[str, Path]:
        """Copy a file into the session's inbox. Returns its id and path.

        Copied rather than moved: the caller may still want what it handed
        over, exactly as outbound `attach` does not consume what it sends.
        """
        inbox = self.inbox_dir(session_id)
        inbox.mkdir(parents=True, exist_ok=True)
        while True:
            file_id = uuid.uuid4().hex[:FILE_ID_WIDTH]
            try:
                # Refusing to reuse a directory is what makes the id unique,
                # rather than a lookup that another caller could race.
                inbox.joinpath(file_id).mkdir()
                break
            except FileExistsError:
                continue
        stored = inbox.joinpath(file_id, safe_filename(name))
        shutil.copyfile(source, stored)
        log.info("inbox add: session=%s file=%s name=%s bytes=%d",
                 session_id, file_id, stored.name, stored.stat().st_size)
        return file_id, stored

    def remove_file(self, session_id: str, file_id: str) -> bool:
        """Delete one stored file. False if there was nothing to delete."""
        if not _is_file_id(file_id):
            return False
        directory = self.inbox_dir(session_id).joinpath(file_id)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        log.info("inbox remove: session=%s file=%s", session_id, file_id)
        return True

    def clear_files(self, session_id: str) -> int:
        """Delete every stored file. Returns how many there were."""
        inbox = self.inbox_dir(session_id)
        if not inbox.is_dir():
            return 0
        count = sum(1 for child in inbox.iterdir() if child.is_dir())
        shutil.rmtree(inbox)
        log.info("inbox clear: session=%s files=%d", session_id, count)
        return count

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
                # Named by session, not only by path: this line is the single
                # trace of a session that is about to be missing from every
                # listing, and the id is what the reader will search for.
                log.warning("skipping unreadable metadata for session=%s (%s): %s",
                            session_dir.name, meta_path, error)
        return metas

    def read_transcript(self, session_id: str) -> list[dict]:
        return _read_transcript(self._session_dir(session_id).joinpath(TRANSCRIPT_FILENAME))


def safe_filename(name: str) -> str:
    """Reduce a name from anywhere to one harmless path component.

    The id directory already makes collisions impossible, so this is not about
    uniqueness. It is about a name that arrived over the network being used as
    a filename at all.
    """
    cleaned = _UNSAFE_IN_NAME.sub("", (name or "").strip()).strip(". ")
    return cleaned[:_NAME_LIMIT] or _FALLBACK_NAME


def _is_file_id(file_id: str) -> bool:
    """Guard the one place an id becomes a path: `..` must not address a
    session's transcript, and no id this store hands out looks like that."""
    return bool(file_id) and all(character in "0123456789abcdef" for character in file_id)


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
