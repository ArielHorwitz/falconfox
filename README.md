# FalconFox

FalconFox is a small, vendor-neutral [Agent Client Protocol](https://agentclientprotocol.com)
session daemon. A session is a working directory plus metadata; the daemon keeps
the ACP subprocess, transcript, and resume information centrally, without writing
bookkeeping into the repository where the agent works.

What is here: the flattened daemon and API, the `falconfox` CLI control plane,
and a separate two-channel Telegram client, deployed and driven from a phone.
Telegram is the only client. The browser UI under `src/falconfox/web/static/`
is dead: flattening the session model removed the navigation it was built on,
and it is slated for deletion soon, with a repair possible after that.
`src/falconfox/web/server.py` is a different thing entirely, and very much
alive — that module is the daemon's own HTTP and websocket API.

The daemon binds loopback and expects to be reached through a co-located
client, so remote authentication is not planned — Telegram *is* the remote
access. Voice input and a real desktop client are wanted but deferred; see
[docs/wishlist.md](docs/wishlist.md) for those and
[docs/buglist.md](docs/buglist.md) for what is known broken.

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

At most `max_live_sessions` sessions (default 5) hold a live agent
subprocess at once — each one runs its own ACP backend process, so the limit
is about host memory rather than about your work. The Telegram client's own
manager and private-chat sessions are counted, queued and evicted exactly like
any other: they are resumable, so sleeping one costs a resume rather than its
conversation, and no class of session is allowed to exceed the number. A
session over the limit is created *stored* and activates when a slot frees;
making room stops the least-recently-used **idle** session, never a working
one, and its transcript and topic survive. Set `max_live_sessions = 0` to
disable the cap.

Configuration is daemon-global. There are no per-project overrides.

## Daemon and CLI

```bash
falconfox daemon                     # also --stop, --restart, --foreground
falconfox spawn --path ~/projects/example --name "example work"
falconfox list                       # --all includes hidden, --json for a script
falconfox send <session-id> "Inspect the failing tests"
falconfox read <session-id>          # --json for the raw transcript
falconfox stop <session-id>
falconfox resume <session-id>
falconfox rename <session-id> "better name"
falconfox tag <session-id> urgent    # no tags clears them
falconfox delete <session-id>
falconfox attach report.pdf          # from inside a session, to its chat
falconfox help [topic]
```

`send` resumes a stored session automatically, waits out the whole turn, and
prints the reply. `spawn --ephemeral` creates a live session that is never
persisted and is hidden from the default listing, and `spawn --role` is
repeatable. `attach` only works from inside a session, since what it sends to
is that session's chat: it hands the file to whichever client is showing the
session and waits for that client to confirm delivery, which is why a missing
client is an error rather than a silent drop. `help` reads what the running
clients have registered with the daemon, so it needs the daemon up.

Each backend subprocess receives its own id as `FALCONFOX_SESSION_ID`; the CLI
rejects self-stop, self-delete, and stopping the containing daemon. Every
request has a finite timeout, so an agent that runs `falconfox` against a
wedged daemon is told so instead of hanging for the rest of its turn.

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

**General** holds the session manager — a session carrying the daemon's own
`.manager` role, which is what tells it to own the session lifecycle:
spawning, renaming, stopping and deleting. `/new`, `/list`, `/home` and
`/status` are explicit fast paths; other General text is resolved naturally
by the manager agent. `/name` is not among them: it renames the session whose
topic you are writing in, so it belongs to a topic rather than to General.
`/help` is one text, the same in every chat, grouped by what a command acts
on: FalconFox and its sessions, one particular session, or the host. Where a
command is refused is said on its own line rather than by hiding it.
There is no focus pointer and no `/switch`: a topic *is* the address, so there
is nothing left to switch.

A turn is **two messages**. The first is posted as the turn starts and is
edited in place while the work proceeds. Its header carries a clock and a count
of anything queued behind the turn. Under it goes the work: the agent's remarks
between tool calls, the opening of each thinking block behind a `💭`, and one
compact line per tool call behind a `⚙️`. When the turn ends that message is
stamped with the outcome, the elapsed time, the number of tool calls and the
tokens it cost, and left standing as the record of how the answer was reached.
The second message is the answer itself, meaning the text after the last tool
call, sent on its own and threaded to the prompt it answers. A tool call never
gets a message of its own, which is the part of "tool calls are suppressed"
that still holds. Telegram's typing indicator runs for the whole turn, because
editing a message never notifies and being alive is the one thing the progress
message cannot say by itself.

Every session is told what it is running inside, once, on the first message it
ever receives. That **orientation** is composed rather than written in one
place: a global piece about being a FalconFox session, then one piece per
client that is running, then one piece per *role* the session holds. Roles
compose and are namespaced by whoever registered them, so `.manager` is the
daemon's own and `telegram.concierge` is this client's.

A client publishes its own text rather than the daemon carrying it: the daemon
makes a directory per run, names it in `server.json`, and each client writes
`<client>/orientation.md` and `<client>/roles/<role>.md` into it on startup and
on every reconnect. The directory name *is* the namespace, so two clients can
both offer a "concierge" without colliding, and a client that stops running
stops describing itself to new sessions.

Orientation is what a session cannot work without and is told once, unasked.
The other half is **help**: `falconfox help <topic>` reads nested markdown that
clients register in the same directory, so detail can grow without every
session paying for it. `falconfox help` lists what is registered, namespaced
the same way roles are, and the daemon composes that listing into the global
orientation so a session knows what is there to look up.

Orientation is recorded in the session's transcript, marked so clients do not
display it. Changing it does not reach sessions that already exist, which is
what `/clear` is for.

A session can carry **tags** - `/tags urgent` in its topic, or `falconfox tag`
- which are opaque labels that the forum draws as the topic's icon, one per
tag, mapped in `config.toml`.

A file sent to a chat lands in that session's **tray** and waits, because a
chat has no compose step: a photo is its own message, and an album arrives as
several with nothing marking the last. The next real message sweeps the tray
and carries one `attached: <path>` line per file, in arrival order, with each
file's caption on its own line. A caption is not that message, which is the one
surprise here and is why every file gets a receipt saying so. `/tray` shows
what is waiting and removes from it, the opposite sense to `/tags`. The bytes
live under the session's own state directory and go when it does. Telegram
will not let a bot download more than 20MB, against 50MB for upload, and a
larger file is refused with that reason.

A message written while a turn is running is **queued**, not refused: it goes
out when the turn ends, and several of them are joined into one prompt. `/stop`
ends the running turn, which is what makes the queue drain; `/unqueue` drops
what is queued and leaves the turn alone; `/fullstop` does both, since doing
them separately in that order races the flush.

Voice *input* and interactive permissions are intentionally deferred: nothing
is transcribed, so a voice message is stored as the audio file it is. The PoC
uses always-allow sessions; a permission request with no choices is denied
immediately instead of hanging.
