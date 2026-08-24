"""Build-time hook that stamps the source git commit into the wheel/sdist.

At build time the checkout still has ``.git`` (uv clones it before building), so we
resolve the commit here and write ``src/falconfox/_version.py`` into the artifact. The
installed package then reports the commit without needing git at runtime. Running from a
plain checkout (no build) falls back to live git in ``falconfox.get_version``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

VERSION_FILE = "src/falconfox/_version.py"


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        commit = _git(self.root, "rev-parse", "--short", "HEAD")
        if commit is None:
            return  # not a checkout; leave get_version to fall back to the bare version
        dirty = bool(_git(self.root, "status", "--porcelain", "--untracked-files=no"))
        Path(self.root).joinpath(VERSION_FILE).write_text(
            "# Generated at build time by hatch_build.py. Do not edit.\n"
            f"COMMIT = {commit!r}\n"
            f"DIRTY = {dirty}\n"
        )
        build_data["artifacts"].append(VERSION_FILE)


def _git(root: str, *args: str):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
