"""Casebook: a coordinator connecting a filesystem casebook to ACP agent backends."""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

__version__ = "0.1.0"


@lru_cache(maxsize=1)
def get_version() -> str:
    """Package version, suffixed with the git commit and dirty flag when that is known.

    Prefers the commit stamped into ``_version.py`` at build time (present in installed
    wheels), then falls back to live git for a plain source checkout, then to the bare
    ``__version__`` when neither is available.
    """
    baked = _baked_version()
    if baked is not None:
        return baked
    commit = _git_commit()
    if commit is None:
        return __version__
    return _format_version(commit, _git_dirty())


def _format_version(commit: str, dirty: bool) -> str:
    suffix = f"{commit}-dirty" if dirty else commit
    return f"{__version__}-{suffix}"


def _baked_version() -> Optional[str]:
    """Version from the build-time-generated ``_version.py``, if it was bundled."""
    try:
        from . import _version
    except ImportError:
        return None
    commit = getattr(_version, "COMMIT", None)
    if not commit:
        return None
    return _format_version(commit, bool(getattr(_version, "DIRTY", False)))


def _git_commit() -> Optional[str]:
    """Short HEAD commit of the source checkout, or None when git/the repo is unavailable."""
    result = _git("rev-parse", "--short", "HEAD")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _git_dirty() -> bool:
    """Whether the source checkout has uncommitted changes (tracked files or the index)."""
    result = _git("status", "--porcelain", "--untracked-files=no")
    return result is not None and result.returncode == 0 and bool(result.stdout.strip())


def _git(*args: str) -> "Optional[subprocess.CompletedProcess[str]]":
    repo_root = Path(__file__).resolve().parents[2]
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return None
