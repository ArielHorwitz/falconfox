"""Configuration and backend selection.

FalconFox reads a single global config at
``$XDG_CONFIG_HOME/falconfox/config.toml`` (falling back to
``~/.config/falconfox/config.toml``). The config declares the
available ACP agent *backends* — a backend is just a command to launch plus
environment — and which one is the default.

Exactly one backend is built in:

  - ``echo``: a tiny in-tree ACP agent that reflects messages back. Always
    available, so the app is runnable with no setup (useful for development).

A real agent such as ``claude`` (the ``claude-agent-acp`` adapter) is declared
under ``[backends.*]`` — run it via ``npx``, or install it and point ``command``
at the binary. See docs/configuration/backends.md.

This module knows nothing about cases.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import logsetup

log = logsetup.get_logger("config")

CONFIG_FILENAME = "config.toml"

ECHO_BACKEND_NAME = "echo"

# Default logging verbosity. Override with a top-level `log_level = "DEBUG"` in
# the global config, or the FALCONFOX_LOG_LEVEL environment variable.
DEFAULT_LOG_LEVEL = "INFO"

# How many of the USER's sessions may hold a live agent subprocess at once.
# Sessions are the unit of memory cost -- each runs its own ACP backend
# process -- so this is a ceiling on the host: a session over the limit is
# stored rather than refused, and activates when a slot frees. 0 disables it.
#
# A client's own infrastructure (the Telegram manager and its private chat)
# counts too: it is real memory, and a limit that omits real processes is a
# lie. Budget for it -- a Telegram deployment spends about two on plumbing --
# and note the floor below, which stops it consuming everything. Override with a top-level `max_active_sessions = N` in config.toml.
DEFAULT_MAX_ACTIVE_SESSIONS = 5

# A client's own infrastructure (the Telegram manager and its private chat)
# counts toward the limit, because a limit that omits real processes is a lie.
# This floor is what stops it consuming the whole budget: below this the user
# would have no room left at all.
MIN_ACTIVE_SESSIONS = 3

# The instructions handed to the model when asked to name a session. Override it
# in config.toml with a top-level `naming_prompt = "..."`.
DEFAULT_NAMING_PROMPT = (
    "You are naming a work session based on the transcript that follows. "
    "Reply with a single concise, descriptive title of at most six words. "
    "No surrounding quotes, no trailing punctuation, no preamble — reply with "
    "only the title."
)


# Default keyboard shortcuts (action -> key, or a list of keys). Override
# individually in config.toml under a `[hotkeys]` table. Keys are matched against
# the browser's KeyboardEvent `key` value, so e.g. "?" is shift+/, "]" is the
# literal bracket, and arrow keys are "ArrowDown"/"ArrowRight"/etc.
DEFAULT_HOTKEYS = {
    "new_session": "n",
    "home": "h",
    "scratch": "s",
    "focus_next": ["ArrowRight", "ArrowDown"],
    "focus_prev": ["ArrowLeft", "ArrowUp"],
    "open_focused": "Enter",
    "rename_session": "r",
    "autoname_session": "g",
    "close_session": "x",
    "delete_session": "d",
    "toggle_allow": "a",
    "toggle_commands": "/",
    "cancel_turn": "S",
    "cycle_width": "w",
    "help": "?",
}

# UI sizing for the session columns (panes). Values are CSS lengths, so any unit
# works — "33vw" / "30%" for a fraction of the screen, "px"/"em" for fixed sizes,
# "none" for no maximum. Override under a `[ui]` table in config.toml.
DEFAULT_UI = {
    "session_width": "50%",
    "session_min_width": "320px",
    "session_max_width": "none",
    # Widths the resize hotkey cycles through (any CSS lengths).
    "session_widths": ["20%", "33%", "50%", "66%", "75%", "100%"],
    # Status -> CSS color for case titles in the sidebar. Any status not listed
    # here inherits the default text color.  Override or extend under
    # [ui.case_colors] in config.toml.
    "case_colors": {
        "open": "#9ece6a",
        "closed": "#9a9db0",
    },
}


@dataclass(frozen=True)
class Backend:
    """A launchable ACP agent backend."""

    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    # Default values for the backend's ACP config options, keyed by option id
    # (e.g. {"model": "opus", "reasoning_effort": "high"}). Applied at session
    # start; the ids/values are the ones the backend advertises — visible in the
    # UI's session-options popover so a user knows what to write here.
    config_options: dict = field(default_factory=dict)


def global_config_dir() -> Path:
    """`$XDG_CONFIG_HOME/falconfox`, or `~/.config/falconfox` if unset."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home().joinpath(".config")
    return root.joinpath("falconfox")


def global_config_path() -> Path:
    return global_config_dir().joinpath(CONFIG_FILENAME)


def echo_backend() -> Backend:
    """The always-available in-tree echo agent (see falconfox.echo_backend)."""
    return Backend(
        name=ECHO_BACKEND_NAME,
        command=[sys.executable, "-m", "falconfox.echo_backend"],
    )


def builtin_backends() -> dict[str, Backend]:
    """The backends present without any config. Only echo is built in; real
    agents like claude are declared explicitly under ``[backends.*]``."""
    return {ECHO_BACKEND_NAME: echo_backend()}


@dataclass(frozen=True)
class Config:
    """The resolved FalconFox configuration."""

    backends: dict[str, Backend]
    default_backend: str
    naming_prompt: str = DEFAULT_NAMING_PROMPT
    # Which backend names sessions. Required for the naming feature — if unset,
    # naming is disabled. The naming backend's model (and any other option) comes
    # from its own [backends.<name>.config_options], like any backend.
    naming_backend: Optional[str] = None
    # Whether new sessions start with always-allow enabled.
    default_always_allow: bool = False
    # Ceiling on sessions holding a live agent subprocess. See the constant.
    max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS
    # Action -> key, or a list of keys (the browser binds each to that action).
    hotkeys: dict = field(default_factory=lambda: dict(DEFAULT_HOTKEYS))
    ui: dict = field(default_factory=lambda: dict(DEFAULT_UI))

    def select_backend(self, name: Optional[str] = None) -> Backend:
        chosen = name or self.default_backend
        if chosen not in self.backends:
            available = ", ".join(sorted(self.backends)) or "(none)"
            raise KeyError(f"unknown backend '{chosen}'. Available: {available}")
        return self.backends[chosen]


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError) as error:
        # A malformed config is a top user-facing failure (e.g. a hand-edited or
        # copied config.toml); name the file and the parse error rather than
        # letting a bare traceback bubble up from wherever the config was read.
        log.warning("failed to read config %s: %s", path, error)
        raise


def _parse_backends(raw: dict) -> dict[str, Backend]:
    return {
        name: Backend(
            name=name,
            command=list(spec["command"]),
            env=dict(spec.get("env", {})),
            config_options=dict(spec.get("config_options", {})),
        )
        for name, spec in raw.items()
    }


def _merge_ui(overrides: dict) -> dict:
    """Merge user UI overrides into DEFAULT_UI, deep-merging dict-valued keys."""
    merged = {**DEFAULT_UI, **overrides}
    for key, default_value in DEFAULT_UI.items():
        if isinstance(default_value, dict) and key in overrides:
            merged[key] = {**default_value, **overrides[key]}
    return merged


def load_config() -> Config:
    """Built-in backends overlaid with the daemon-global config.

    Config format (``config.toml``):

        default_backend = "claude"

        [backends.claude]
        command = ["claude-agent-acp"]

        [backends.gemini]
        command = ["gemini", "--experimental-acp"]
        env = { GEMINI_API_KEY = "..." }
    """
    data = _read_toml(global_config_path())

    backends = builtin_backends()
    backends.update(_parse_backends(data.get("backends", {})))

    default = data.get("default_backend")
    if default is None:
        # No preference set: default to the first real backend the user declared,
        # falling back to the built-in echo if they declared none.
        default = next((name for name in backends if name != ECHO_BACKEND_NAME),
                       ECHO_BACKEND_NAME)
    log.debug("global config loaded: backends=%s default=%s", sorted(backends), default)
    return Config(
        backends=backends,
        default_backend=default,
        naming_prompt=data.get("naming_prompt", DEFAULT_NAMING_PROMPT),
        naming_backend=data.get("naming_backend"),
        default_always_allow=bool(data.get("default_always_allow", False)),
        max_active_sessions=max(0, int(
            data.get("max_active_sessions", DEFAULT_MAX_ACTIVE_SESSIONS))),
        hotkeys={**DEFAULT_HOTKEYS, **data.get("hotkeys", {})},
        ui=_merge_ui(data.get("ui", {})),
    )

def global_hotkeys() -> dict:
    """Hotkeys from the daemon-global config."""
    data = _read_toml(global_config_path())
    return {**DEFAULT_HOTKEYS, **data.get('hotkeys', {})}


def log_level() -> str:
    """Log level from global config only (top-level `log_level`).

    Logging is a process-global concern set up once at server startup, so it is
    read from the global config.
    """
    data = _read_toml(global_config_path())
    return str(data.get("log_level", DEFAULT_LOG_LEVEL))
