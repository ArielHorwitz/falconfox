# FalconFox

FalconFox is a small, vendor-neutral [Agent Client Protocol](https://agentclientprotocol.com)
session daemon. A session is a working directory plus metadata; the daemon keeps
the ACP subprocess, transcript, and resume information centrally, without writing
bookkeeping into the repository where the agent works.

What is here: the flattened daemon and API, the `falconfox` CLI control plane,
and a separate two-channel Telegram client, deployed and driven from a phone.
Telegram is the mobile client; the web assets under `src/falconfox/web/` are
the desktop client and ship unwired, because flattening the session model
removed the navigation they were built on.

The daemon binds loopback and expects to be reached through a co-located
client, so remote authentication is not planned — Telegram *is* the remote
access. Voice input and rewiring the web UI are wanted but deferred; see
[docs/wishlist.md](docs/wishlist.md) for those and [docs/bugs.md](docs/bugs.md)
for what is known broken.

## Install and configure

For development:

```bash
uv sync
```

With no configuration FalconFox uses its built-in `echo` ACP backend. Declare a
real agent in `~/.config/falconfox/config.toml` (or beneath
`$XDG_CONFIG_HOME/falconfox`):

```toml
default_backend = "codex"

[backends.codex]
command = ["codex-acp"]

[backends.codex.config_options]
reasoning_effort = "high"
```

Configuration is daemon-global. There are no per-project overrides.

## Daemon and CLI

```bash
falconfox daemon
falconfox spawn --path ~/projects/example --name "example work"
falconfox list
falconfox send <session-id> "Inspect the failing tests"
falconfox read <session-id>
falconfox stop <session-id>
falconfox resume <session-id>
falconfox rename <session-id> "better name"
falconfox delete <session-id>
falconfox daemon --stop
```

`send` resumes a stored session automatically. `spawn --ephemeral` creates a
live session that is never persisted and is hidden from the default listing.
Each backend subprocess receives its own id as `FALCONFOX_SESSION_ID`; the CLI
rejects self-stop, self-delete, and stopping the containing daemon.

Session state lives at
`$XDG_STATE_HOME/falconfox/sessions/<session-id>/` (falling back to
`~/.local/state/falconfox/sessions/`).

## Telegram PoC

Create a Telegram bot and two chats: a single-purpose focus channel and a work
channel. Then run the client next to the daemon:

```bash
export FALCONFOX_TELEGRAM_TOKEN=…
export FALCONFOX_TELEGRAM_FORUM_CHAT_ID=…
export FALCONFOX_TELEGRAM_DEFAULT_PATH="$HOME"
# Optional: FALCONFOX_URL, FALCONFOX_TELEGRAM_POINTER_FILE,
# FALCONFOX_TELEGRAM_FOCUS_BACKEND
falconfox-telegram
```

Every session gets its **own topic** in the forum, created by the bot when the
session appears and kept in step with it: a rename retitles the topic, a stop
closes it (a closed topic still accepts the bot's writes, so the record and any
final notice survive), and a delete removes it. You talk to a session by
writing in its topic, so sessions run in parallel without interfering.

**General** holds the session manager — an ephemeral session with a skill for
spawning, renaming, stopping and deleting. `/new`, `/list`, `/name`, `/home`
and `/status` are explicit fast paths; other General text is resolved naturally
by the manager agent. There is no focus pointer and no `/switch`: a topic *is*
the address, so there is nothing left to switch. During turns the bot refreshes Telegram's
typing indicator, suppresses tool calls, and sends the final reply as one message.

Voice messages and interactive permissions are intentionally deferred. The PoC
uses always-allow sessions; a permission request with no choices is denied
immediately instead of hanging.
