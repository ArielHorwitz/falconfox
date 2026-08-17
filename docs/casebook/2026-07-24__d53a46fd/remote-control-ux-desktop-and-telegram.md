# Remote control UX: entry points, desktop, and telegram

How falconfox is actually *used*. Companion to [overview.md](overview.md) (the why)
and [falconfox-pivot-plan-per-module.md](falconfox-pivot-plan-per-module.md) (the
what changes where).

> Supersedes an earlier draft built around a "manager agent". That framing was
> dropped — see below.

## There is no manager agent

An earlier draft treated the "manager" as a distinguished, long-lived session at `~`
with root-console orientation. That framing is **wrong and unnecessary**. It
decomposes into two things, neither of which is a falconfox concept:

1. **A default scope.** New sessions default to the home (or root) directory. That is
   a one-line daemon default, not a type.
2. **Two skills**, installed by the user like any others:
   - **`administrator`** — "you are a natural-language console to this machine."
   - **`falconfox`** — how to drive the daemon: list, spawn, stop, resume, delete.

So "the manager" was just *a session at home that invoked those two skills*. Naming
it a type added a concept and bought nothing.

**Consequence worth noticing:** skills installed to `~/.agents/skills/` are ambient —
available to *every* session regardless of cwd. So a project session at
`~/projects/b2` can also drive falconfox if asked. Session-management capability is
not tied to scope at all; it is tied to invoking a skill. That is a feature: any
session can spawn a sibling.

falconfox therefore needs **no manager, no preset, no orientation feature**. Only a
default path for new sessions, and a CLI.

## Entry-point sessions are ephemeral, not long-lived

When you connect remotely you do not necessarily know what scope you want yet, so you
start at the top: a **fresh session at home**. From there you administer the machine,
or list previous sessions and resume the one you want.

Crucially it need **not** be long-lived. Disconnecting and reconnecting gives you a
*new* home session — and that is fine, because the **session list is the memory**, not
the entry session.

This costs nothing in clutter, because the current code already handles it:
`_should_persist()` never writes a session with no messages and an auto-generated
name, and `close_agent()` deletes such trivial sessions outright. So a connect where
you did nothing evaporates; one where you actually did work persists and appears in
the list like any other session. **This machinery survives the pivot and directly
serves this design** — no new feature needed.

## The CLI surface

```
falconfox daemon
falconfox spawn [--path ~] [--name …] [--backend …]   # → session id
falconfox list                                         # id / name / path / state / activity
falconfox resume|stop|delete|rename <id>
falconfox send <id> "…"        # prompt a session you are not attached to
falconfox read <id>            # read its transcript (the necessary pair to `send`)
```

**On `send`/`read`:** `send` originally existed to deliver orientation after spawn;
skills made that obsolete. Its remaining use is giving a spawned session an initial
task without switching to it, plus plain symmetry with the API (prompting a session
is what every client does). `read` is required for `send` to be useful at all —
otherwise an agent can task another agent but never see the result. Together they
make falconfox usable as a delegation substrate; delegation *patterns* stay a skill
concern (cf. the user's `iac` skill), not a daemon feature.

**Self-identification:** since any session can drive the CLI, the daemon should inject
**`FALCONFOX_SESSION_ID`** into each session subprocess's environment, so a session
can avoid self-targeting and report coherently about sessions it spawned. Guard
against self-deletion and against stopping the daemon from within a session — a
voice-driven agent will reach for those casually.

## "The top" differs by client, because a chat can only point at one thing

- **Desktop's top is the session list.** The UI can *show* you everything, so landing
  there is natural: pick a session, or spawn a new one. A page load must **not**
  spawn a session (every refresh would create one); spawning stays an explicit action.
- **Telegram's top is a fresh home session.** A chat has to be pointed *somewhere* for
  your next message to have a destination, so a chat with no cursor gets a new home
  session and focuses it.

## Desktop (browser over Tailscale → the VPS)

- **Home is the flat session list** — name, path, backend/model, state,
  created/last-active. Open several as panes.
- **Navigation is direct manipulation**: click a session, or use the spawn dialog
  (path + name + backend).
- **A home-scoped session replaces ssh.** Open one when you want machine work done,
  as the alternative to a remote console — but it is optional here, because you
  already have buttons for everything session-related.
- **Parallelism is visual** — several panes, state visible at a glance.

## Telegram (bot on the VPS, loopback to the daemon)

Yes, this model works — and it makes the bot *simpler*:

- **One chat ↔ one session, via a focus cursor** held by the bot. The daemon has no
  notion of focus.
- **A chat with no cursor spawns a fresh home session** and focuses it. That is the
  whole "you start at the top" behaviour — no manager concept required.
- **Two chats give you the two-channel split for free**: one parked on a home/admin
  session, another on your work session. More chats or groups = more parallel
  sessions.
- **Voice in, text (or TTS) out.** A voice message is transcribed and sent as a prompt.
- **Navigation vs content:** the bot intercepts a small vocabulary (`/new`,
  `/list`, `/switch`, `/name`, maybe `/home`) and forwards everything else to the
  focused session. **Every focus change is confirmed**, so a misclassification is
  caught by hearing the wrong confirmation rather than by talking into the wrong
  session.
- **Attention is pushed, not polled.** Turn-complete, permission requests, and errors
  arrive tagged with their session and a one-tap "switch to it". This replaces the
  desktop's at-a-glance visibility and is what makes several sessions tractable
  eyes-free.

### Streaming, tool calls, and permissions

Telegram has no streaming surface, so a long agent turn would otherwise look like the
bot hanging. Three decisions (PoC-level; build spec in
[telegram-bot-poc-spec.md](telegram-bot-poc-spec.md)):

- **"typing…" instead of streaming.** The Bot API's `sendChatAction(action="typing")`
  gives the same affordance a human typing gets. It expires after ~5s or when a message
  is sent, so it needs a refresh loop for the duration of the turn. This maps onto
  existing daemon events with no daemon change: start refreshing on `agent_state` →
  `working`, stop on `idle` and send the reply.
- **One message per turn; tool calls suppressed.** The bot sends the agent's final
  reply when the turn completes. Tool-call activity is not forwarded — it is noise in
  a chat and unusable by voice. (Revisit later if turns feel opaque.)
- **Always-allow is the only supported posture.** Interactive approval in telegram is
  out of scope for the PoC, and the user already runs always-allow everywhere.
  **Edge to handle:** `_request_permission()` auto-allows only when
  `_auto_allow_option()` returns an option; on an empty options list it returns `None`,
  falls through, and waits on a future that no telegram client will resolve — a silent
  hang. The PoC must resolve that case as denied rather than block.

### Switching: a single-purpose focus channel over a bot-owned pointer

Reading a hex id aloud ("switch to one-a-two-b-three-c") is awful, and a bespoke bot
command vocabulary is exactly the kind of interaction this project avoids. The
resolution: **make pointer management an agent's job, in its own channel.**

- **Focus channel** — a chat whose session does **nothing but manage the pointer**
  (via a pointer skill). You say "switch to the admin session"; it runs
  `falconfox list`, finds the match, and writes the pointer.
- **Work channel** — messages are forwarded to whatever the pointer resolves to. All
  actual work happens here.

**Why the focus channel must be single-purpose.** Its whole justification is
*disambiguation of intent*: in a channel where every utterance is a pointer operation,
"switch to the admin session" cannot be read any other way. Say the same words in a
work channel and the agent there may interpret them as something else entirely. It
follows that **merging the focus channel with an admin session is wrong** — an earlier
draft proposed exactly that, and it reintroduces the very ambiguity the channel exists
to remove ("is that a pointer command, or a request to the admin agent?"). If a
home-scoped admin session is wanted, point at one from the focus channel and talk to
it in the work channel; it is just another work session.

**What the focus channel is for:** not parallelism, but escaping the confinement of a
single-cursor chat. It is the mechanism that makes one work channel able to reach
every session.

**The focus agent is stateless and rotates.** It needs no context, so a fresh session
after every pointer change is fine — and desirable, since no accumulated history can
confuse a later switch. Latency is a non-issue at the expected switching frequency,
and the backend can be pinned to a cheap/fast model through the existing per-backend
`config_options` — no new mechanism.

#### The pointer stays bot state (daemon knows nothing about focus)

An earlier draft argued that an agent-managed pointer forces the daemon to store it
("an agent can reach the CLI but not the bot's memory"). **That was wrong** — the
pointer can simply live **on disk**, owned by the bot:

- The bot keeps a pointer file (per chat, at a well-known path).
- The focus agent writes it with ordinary shell access — no daemon feature, no bot API.
- The bot **watches the file** and announces changes. `watchfiles` is already a
  dependency of this repo, so this is inotify, not polling.
- `/switch <id>` writes the same file, so a fast manual path and the natural-language
  path coexist with no second mechanism.

So the original position stands: **focus is a client concept; the daemon has no notion
of it.** No `falconfox pointer` command, no named-pointer registry.

Naming note: since the pointer belongs to the bot rather than the daemon, the pointer
skill is really a *bot* skill (it describes the bot's file), and the telegram bot is
its natural home — not falconfox.

#### Costs and limits, on the record

- **Rotation vs. clutter (needs deciding).** A rotated focus session still has
  messages, so `_should_persist()` writes it to disk — accumulating a trail of
  one-exchange sessions that then appear in `falconfox list`, which is precisely what
  the focus agent reads to find sessions. That is a feedback loop where the navigation
  mechanism pollutes its own input. Fix: **delete** the focus session after each
  pointer change rather than abandoning it, or spawn it with a marker the listing
  filters out.
- **Not a permission boundary.** Skills in `~/.agents/skills/` are ambient, so every
  session can see the pointer skill, including work sessions. The two-channel split is
  **organizational, not enforced**. Accepted deliberately — with one channel, the skill
  can simply be invoked directly.
- **Naming stays load-bearing** — names are the only speakable handle for sessions, so
  `rename` / LLM autonaming (`naming_backend`) are core UX, not cosmetic, and the flat
  session list UI should make naming prominent.

## Division of labour

| | Desktop | Telegram |
|---|---|---|
| The "top" | the session list | a fresh home session |
| Navigation | buttons, panes | focus cursor + the `falconfox` skill |
| Machine ops | a home session (replaces ssh) | a home session (only option) |
| Parallelism | several panes | several chats, one session each |
| Awareness | at a glance | pushed notifications |

Same daemon, same operations, same sessions — the clients differ only in whether you
have buttons.
