# Configuration

FalconFox reads one daemon-global TOML file:
`$XDG_CONFIG_HOME/falconfox/config.toml`, or
`~/.config/falconfox/config.toml` when `$XDG_CONFIG_HOME` is unset.
There are no per-working-directory overrides: a session's `path` is metadata,
not a configuration scope.

Everything is optional. With no file, the built-in `echo` ACP backend is used.

```toml
default_backend = "codex"
naming_backend = "codex"
naming_prompt = "Reply with a concise title of at most six words."
log_level = "INFO"

[backends.codex]
command = ["codex-acp"]
env = { OPTIONAL_BACKEND_VALUE = "..." }

[backends.codex.config_options]
model = "gpt-5.5"
reasoning_effort = "high"
```

| Key | Default | Purpose |
|---|---|---|
| `default_backend` | first declared backend, else `echo` | Backend used by `spawn` unless `--backend` is supplied. |
| `naming_backend` | unset | Backend used by automatic session naming. |
| `naming_prompt` | built in | Prompt for automatic session naming. |
| `log_level` | `INFO` | Daemon logging level; `FALCONFOX_LOG_LEVEL` overrides it. |
| `[backends.<name>]` | `echo` only | ACP subprocess command, environment, and config-option defaults. |

The retained `[hotkeys]` and `[ui]` settings belong to the browser pane code.
That UI is intentionally unwired in the local PoC and will be documented again
when the flat session navigation is rebuilt.

See [backends.md](backends.md) for backend setup and config-option behavior.
