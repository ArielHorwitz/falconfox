# Deploying FalconFox on a VPS

Runs the daemon and the Telegram bot as **systemd user units** with automatic
restart, so the whole stack survives crashes, daemon restarts (which always
take the bot down — it has no reconnect loop), and VPS reboots. After the
one-time bootstrap below, all further work — including developing FalconFox
itself — happens through Telegram; the only reason to SSH back in is a failed
update that could not roll itself back.

## Prerequisites (once, as root or with sudo)

On an apt-based VPS, run `deploy/provision.sh` as root (copy the script over,
or run the manual equivalent): git, curl, Node.js + npm (>=18),
[uv](https://docs.astral.sh/uv/), and
`npm install -g @agentclientprotocol/claude-agent-acp`.

## Bootstrap (once, as the deploy user)

```sh
REPO=~/projects/falconfox-stable   # the location is free; this host uses this one
git clone -b master git@github.com:ArielHorwitz/falconfox.git "$REPO"
mkdir -p ~/.config/falconfox
cp "$REPO"/deploy/telegram.env.example ~/.config/falconfox/telegram.env
cp "$REPO"/deploy/config.example.toml ~/.config/falconfox/config.toml
# fill in telegram.env (bot token + the two chat ids)
# paste a `claude setup-token` token into config.toml
"$REPO"/deploy/setup.sh
```

`setup.sh` is idempotent: it syncs the venv, installs and starts both units,
symlinks the `falconfox` / `falconfox-telegram` CLIs into `~/.local/bin`,
enables lingering (so units run without a login session), and health-checks.

Then install the agent skills, which is what makes sessions able to run the
casebook workflow and drive the daemon:

```sh
git clone https://github.com/ArielHorwitz/agent-skills ~/agent-skills
~/agent-skills/install.sh        # -> ~/.agents/skills
~/agent-skills/fix-claude.sh ~   # ~/.claude/skills -> ../.agents/skills
```

Skills live outside the deploy checkout because they are the user's, not the
deployment's — `setup.sh` deliberately does not install them. The
`fix-claude.sh` bridge is required for Claude-backed sessions, which only read
`.claude/`. Update path: `git pull` in the clone, then `install.sh --upgrade`.

The `casebook` skill's CLI needs `python3` to be 3.11+ (it imports `tomllib`).
Ubuntu 22.04's `python3` is 3.10, so shim a modern one onto PATH ahead of it:
`ln -sfn /usr/bin/python3.12 ~/.local/bin/python3`. This does not touch
`/usr/bin/python3`; it does mean sessions no longer see apt's `python3-*`
modules, which nothing here depends on.

Notes:

- **Stop any other poller on the same bot token first** (e.g. the laptop bot):
  Telegram allows one `getUpdates` consumer per token and gives the rest 409s.
- To **push** work from VPS sessions, the deploy user needs a GitHub-registered
  SSH key and git identity (`user.name` / `user.email`).
- The CLI is **not** on PATH by default. `setup.sh` shims it into
  `~/.local/bin` (which the stock Ubuntu `~/.profile` adds for login shells),
  and the daemon unit sets `Environment=PATH` so agent sessions — which
  inherit the daemon's environment — can invoke `falconfox` by name. PATH
  cannot be set via `environment.d`; systemd ignores that one variable.
- The clone location is free (units are rendered with the real path); `-b
  master` pins the deploy branch — switching the checkout's branch later
  changes what `update.sh` follows.

## Branches

- **`dev`** is the development branch. Everything lands here first, and it is
  what the development instance runs.
- **`master`** is the stable branch and what a deployment follows. It lags
  behind `dev` at a commit that has been *proven in use*, and it moves **only
  by fast-forward** — nothing is ever committed to it directly.

So the release step is: run `dev` somewhere real until you trust a commit,
then fast-forward `master` to it and update. Committing to `master` directly
breaks the fast-forward guarantee that makes this cheap, and the breakage only
shows up later as a merge conflict at deploy time.

(A `falconfox` branch existed during the casebook-to-FalconFox pivot and is
retired. Anything referring to it predates that.)

## Checkouts

Two checkouts on this host, one per instance, each an independent clone:

| path | branch | instance | units |
| --- | --- | --- | --- |
| `~/projects/falconfox` | `dev` | development | `falconfox-dev-daemon`, `falconfox-dev-telegram` |
| `~/projects/falconfox-stable` | `master` | stable | `falconfox-daemon`, `falconfox-telegram` |

The **development** checkout is the one an agent lands in, which is why it
holds `dev`: new work should branch from it without anyone having to first
work out which of several trees is the right one. Agents develop in
`.worktrees/` under it and merge back into `dev`, exactly as before.

The **stable** checkout exists only to be deployed. Nothing is developed
there, and it is a separate clone rather than a worktree so that it does not
share an object store, or a directory that gets moved, with the tree agents
are editing.

Which of the two is the *daily driver* is worth saying plainly, because the
names suggest the opposite. Dev is: it is where the work happens and where the
sessions live. Stable is the fallback, kept running and proven so there is
something to fall back **to** when dev breaks. Its capacity is sized for that
role rather than for a workload, which is why it carries a much smaller
`max_live_sessions` than dev does.

The two instances are separated by more than the branch: the dev units set
`FALCONFOX_INSTANCE=dev`, which moves every directory FalconFox derives under
`~/.local/state/falconfox-dev/falconfox` and `~/.config/falconfox-dev/falconfox`,
so dev has its own config, its own state, its own bot token and its own port.
It is FalconFox's own variable rather than `XDG_STATE_HOME`/`XDG_CONFIG_HOME`,
which is what the units used to set: agent sessions inherit the daemon's
environment, so the XDG variables redirected every tool an agent ran too (git
lost its global ignore file, for one). The instance name is meant to be
inherited, since it is what points a session's `falconfox` at its own daemon.
The doubled `falconfox-dev/falconfox` is the layout the XDG variables produced,
kept because session files, transcripts and the agents' own histories record
absolute paths beneath it.

Stable owns the `falconfox` and `falconfox-telegram` shims in `~/.local/bin`,
so a bare `falconfox` in a shell always means stable, which is worth
remembering now that dev is the one being worked in. Dev has its own pair in
`~/.local/state/falconfox-dev/bin`, prepended to its units' `PATH` and
therefore to every dev session's, so an agent there runs the dev CLI against
the dev daemon rather than the deployment's CLI against it:

```sh
FALCONFOX_INSTANCE=dev ~/projects/falconfox/.venv/bin/falconfox list
```

**Ports are pinned, not searched.** Stable binds 9721 and dev binds 9725,
both passed as `daemon --port` in the units. Unpinned, a daemon searches
upward from 9721 and takes the first free port, so which instance holds which
port depends on the order they started in, and it can change on any restart.
The bot has no discovery — it reads `FALCONFOX_URL` and otherwise defaults to
9721 — so its URL is a literal that a reassignment silently invalidates,
pointing it at the other instance's daemon and that daemon's sessions. Pinned,
the assignment is a fact and a taken port fails loudly instead.

Both units are rendered from `deploy/*.service`:

```sh
~/projects/falconfox-stable/deploy/setup.sh install-units     # stable pair + shims
~/projects/falconfox/deploy/setup.sh install-dev-units        # dev pair, no shims
```

Run each from its own checkout: `@REPO@` is filled in from the script's own
location, so running the wrong one points a unit at the wrong tree.

## Chat commands

`/help` lists them, which is the only description that cannot go stale. The
list lives in `COMMANDS` in `bot.py`, and a test reads the command literals
back out of the dispatcher to catch one added without a line there.

`/clear` starts General or the private chat over: it deletes that chat's
session and spawns a replacement with the same name and workspace. The new one
is told what it is running inside on its first message, which is the one piece
of context a cleared session should keep. It is deliberately not available in a
work session's topic, where the conversation is the work and clearing would be
a delete with a gentler name.

Worth knowing about `/id`: it answers with the session id of the topic it was
sent in, as a block to tap and copy. Names are what the chat shows and names
are ambiguous exactly when it matters, so `/id` is how to point the manager at
a session without describing it.

## Running commands from the chat

`/sh <command>` runs a command on the host and replies with its output. In a
session's topic it runs in that session's working directory; anywhere else it
runs in the bot's default path. `/jobs` lists what this bot process started,
`/tail <id>` re-reads a job's output, `/kill <id>` stops one.

Each command runs in its own **tmux** session rather than as a child of the
bot, which is what makes the interesting cases work:

- A command outlives the bot, so `/sh` can restart the daemon, or the bot
  itself, without killing the process that was asked to do the restarting.
- A command that hangs can be attached to from a terminal, `tmux attach -t
  ff-<id>`, which is the only way to see *where* it is stuck. The pane is left
  open after the command exits, so a finished job can still be inspected.
- Output is teed to `<state dir>/shell/<id>.log` as it is produced, so the
  reply can show a tail while the job is still running and the whole thing
  survives for later reading.

Output comes back as a code block, for the reason any terminal is monospace:
alignment carries meaning that a proportional font destroys. The tail is
budgeted in *escaped* characters, since output containing markup costs several
characters per one it shows.

The reply waits about 45 seconds and then says the job is still running rather
than hanging the chat. Nothing is killed at that point; the job keeps going
and stays readable.

This is the recovery path when the daemon is wedged or at its session cap,
which is when asking an agent to do it cannot work. It is also *ungated* in a
way the agent path is not: an agent's commands are subject to its backend's
own sandbox and permission rules, and `/sh` has none of that. It is owner-only
for the same reason every other command is, and every invocation is logged.

## What a session is told about itself

An agent cannot discover that it is running under FalconFox. It cannot see the
client, the daemon, or the other sessions, and left to guess it guesses wrong
with confidence. So every session is handed a short block of context ahead of
the first message it receives (`config.SESSION_CONTEXT`), covering what the
user can and cannot see, that nothing will pause for approval, that it cannot
speak until spoken to, and how to send a file.

It rides the same hidden-context channel that re-sends a transcript to a
backend without native resume: the agent receives it, and the chat shows only
what the user typed. It is sent once, on the first message, because that is
the earliest moment anything can be said to an agent at all, and because every
line is paid for out of the session's context.

The known gap is that a message can be compacted away, while a system prompt
could not. ACP has no field for system instructions (`NewSessionRequest`
carries `cwd`, `additionalDirectories`, `mcpServers` and `_meta`, and nothing
else), and no compaction signal either, so re-injection has to be inferred
from a drop in a turn's input tokens. Not built yet.

## Sending files out of a session

A session hands a file to whoever is reading it with `falconfox attach
<path>`, which the agent runs like any other command. It needs no argument
naming the chat: the daemon puts `FALCONFOX_SESSION_ID` in every session's
environment, so the command knows which session it belongs to, and the bot
knows which topic that session owns.

The daemon cannot send anything itself, so `attach` is a request to whichever
client is showing the session, and the answer comes back from that client. The
call waits for it by default, because an agent that cannot distinguish a
delivered file from a dropped one will confidently tell you it sent something
it did not. `--no-ack` returns as soon as the request is handed over.

Failures are answered rather than logged: no client connected, no topic to
send to, over Telegram's 50MB limit, or whatever Telegram itself said. The
size check happens in the CLI so the agent hears it from the command it ran.

The bot picks how to send it from the file's type: images become photos, GIFs
animations, MP4s videos, and everything else arrives as a plain file. That is
the client's decision, not the command's -- `attach` knows only that it has a
file. `--raw` says fidelity matters more than display, which matters because
Telegram re-encodes photos: invisible on a photograph, very visible on a
screenshot of text. Images past 10MB, and photos Telegram refuses for their
dimensions, fall back to a plain file rather than failing.

## Updating (the dogfooding loop)

Development happens in `.worktrees/` under the development checkout (or
anywhere else), gets merged to `dev`, proven by the dev instance, then
fast-forwarded into `master` and deployed. Either checkout must be clean for
`update.sh` to run in it, which is why development belongs in a worktree.
Python loads code only at process start, so a running daemon is untouched
until the restart.

`update.sh` follows whatever branch its own checkout is on, so the same script
serves both: run it in `~/projects/falconfox-stable` to deploy `master`, or in
`~/projects/falconfox` to move the dev instance to the tip of `dev`.

- **From a FalconFox agent session (Telegram):**
  `~/projects/falconfox-stable/deploy/update.sh --detach-restart` — pulls and
  syncs inline, then restarts *detached* a few seconds later, because it kills
  the daemon and with it the agent's own turn. The agent should announce the
  update and end its turn; the session itself survives and resumes on the next
  message. **Any** restart of the daemon an agent is running under cuts that
  agent off mid-turn, `systemctl restart` included, so detach those too.
- **From SSH:** `~/projects/falconfox-stable/deploy/update.sh` — everything
  inline.

After restarting, the script health-checks: systemd is running the unit text
this checkout just rendered (`setup.sh check-units`), the daemon answers
`falconfox list`, and the bot unit is active. On failure it **rolls back** to
the previous revision, re-syncs, restarts, and re-checks. Unit-file changes
deploy too (units are re-rendered on every update), and each rendered unit
carries a checksum comment so the check can tell the text systemd loaded from
the text on disk. Everything is appended to
`~/.local/state/falconfox/update.log` (`falconfox-dev` for dev) — the first thing to read after an
update went quiet. Deeper forensics: `journalctl --user -u falconfox-daemon`
/ `-u falconfox-telegram`, and `~/.local/state/falconfox/falconfox.log`.

A restart step that fails is logged and does **not** abort the update — the
health check and rollback are what recover from it. This matters for unit-file
changes specifically: a malformed unit makes `systemctl restart` exit non-zero,
which under `set -e` would otherwise skip rollback entirely.

Known limits: the health check can miss a bot that crashes slowly (it samples
`is-active` once after a settle delay), and an in-flight turn at restart time
is always lost. Stored sessions resume with transcripts intact; the bot reuses
its pointer file across restarts.
