# Configuration

FalconFox reads one daemon-global TOML file:
`$XDG_CONFIG_HOME/falconfox/config.toml`, or
`~/.config/falconfox/config.toml` when `$XDG_CONFIG_HOME` is unset.
A named instance (`FALCONFOX_INSTANCE=dev`) reads
`falconfox-dev/falconfox/config.toml` beneath the same base instead.
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

# The icon every forum topic wears. Must be one of Telegram's own forum
# icons; anything else is skipped with a warning.
# default_topic_icon = "💬"

# Telegram tag glyphs, drawn at the front of the topic title. No default:
# uncomment and use your own vocabulary, since FalconFox attaches no meaning
# to a tag.
# [telegram.tag_icons]
# archived = "📁"
# urgent = "❗️"
# review = "👀"
```

| Key | Default | Purpose |
|---|---|---|
| `default_backend` | first declared backend, else `echo` | Backend used by `spawn` unless `--backend` is supplied. |
| `naming_backend` | unset | Backend used by automatic session naming. |
| `naming_prompt` | built in | Prompt for automatic session naming. |
| `log_level` | `INFO` | Daemon logging level; `FALCONFOX_LOG_LEVEL` overrides it. |
| `max_live_sessions` | `5` | How many sessions may hold a live agent subprocess at once. A session over the limit is stored and activates when a slot frees. `0` disables the cap. |
| `event_queue_limit` | `4096` | How many events a connected client may fall behind by before it is dropped and its connection closed, so that one wedged client cannot grow the daemon's memory without limit. Its reconnect takes a fresh snapshot. `0` disables the bound. |
| `[backends.<name>]` | `echo` only | ACP subprocess command, environment, and config-option defaults. |
| `default_topic_icon` | unset | The icon put on every forum topic. Must be a Telegram forum icon. |
| `[telegram.tag_icons]` | unset | Maps a session tag to the glyph drawn at the front of its topic title. |

## Tag glyphs and the topic icon

A topic shows two things, and they are configured separately because they
follow different rules.

**The title** is the session's name with its tag glyphs in front of it, so a
topic reads `❗️⚡️ my-session`. A session can carry tags
(`falconfox tag <id> <tags...>`), which are opaque labels: FalconFox stores
them, lists them and nothing else. `[telegram.tag_icons]` is where they
acquire a visible meaning.

**Every** mapped tag is drawn, in the order the tags were set, so tag order is
display order and nothing more. Tags with no glyph are skipped and stay
perfectly useful as labels. A title is capped at 128 characters, and the cap is
spent on the name rather than the glyphs.

A title is free text, so **any emoji works here** and nothing is validated.

**The icon** is the small picture beside the topic in the list. It is one
constant, `default_topic_icon`, the same on every topic, set when the topic is
created. The slot carries no signal yet.

Unlike tag glyphs, this one is **restricted**: Telegram allows only its own
fixed forum-icon set, resolved at bot startup from
`getForumTopicIconStickers`. An emoji outside that set is skipped with a
warning in the log, and a raw custom-emoji id is passed through for anything
the endpoint does not list.

Leaving it unset leaves the slot alone, which is not the same as clearing it. A
topic with no icon falls back to a badge drawn from the first character of its
title, and a title that starts with a tag glyph renders that badge as a
question mark.


The retained `[hotkeys]` and `[ui]` settings belong to the browser pane code.
That UI is intentionally unwired in the local PoC and will be documented again
when the flat session navigation is rebuilt.

See [backends.md](backends.md) for backend setup and config-option behavior.
