# Telegram bot — PoC build spec

The bot is **new code in its own package/process**, so it is not covered by
[falconfox-pivot-plan-per-module.md](falconfox-pivot-plan-per-module.md) (which covers
changes to the existing repo). Design rationale lives in
[remote-control-ux-desktop-and-telegram.md](remote-control-ux-desktop-and-telegram.md);
this file is what to build.

## Shape

A separate process running on the same host as the daemon, talking to it over
**loopback** — the daemon's WS/REST API for live events, and the `falconfox` CLI (or
the same API) for session operations. Keep it a **separate package**: the daemon's
dependency list is currently four entries and should stay clean of a telegram library
and, later, a transcription client.

No auth is needed for the PoC (loopback, local only).

## The two channels

| | Focus channel | Work channel |
|---|---|---|
| Purpose | move the pointer, nothing else | all actual work |
| Backed by | an **ephemeral** session, rotated after every pointer change | whatever the pointer resolves to |
| Backend | pin a cheap/fast model via `config_options` | whatever the session was spawned with |

The focus channel must stay **single-purpose** — that is what makes "switch to the
admin session" unambiguous. Do not merge it with an admin/work session.

Its session is spawned with `--ephemeral`, so it never persists and never appears in
`falconfox list` — which matters because `list` is exactly what the focus agent reads
to find sessions. Rotate (spawn fresh) after each pointer change; the focus agent needs
no accumulated context.

## The pointer

- **Owned by the bot, on disk** — a file per chat at a well-known path. The daemon has
  no notion of focus and gains no `pointer` command.
- The focus agent writes it with ordinary shell access, guided by a **pointer skill
  shipped with the bot** (it describes the bot's file, not the daemon — so the bot is
  its home, not falconfox).
- The bot **watches the file** (`watchfiles`) and announces every change, regardless of
  who made it. Announcing on change — not on command — is the safety property: if a
  work session moves the pointer off itself, you still hear about it.
- `/switch <id>` writes the same file, so the manual fast path and the natural-language
  path share one mechanism.

## Message flow

**Inbound.** A small explicit vocabulary is intercepted by the bot (`/new`, `/list`,
`/switch`, `/name`, `/home`); everything else is forwarded verbatim to the session the
pointer resolves to. Every focus change is confirmed back to the chat, so a
misclassification is caught by hearing the wrong confirmation rather than by talking
into the wrong session.

**Outbound.**

1. On `agent_state` → `working`: start a `sendChatAction(action="typing")` refresh loop
   (the indicator expires after ~5s, so refresh roughly every 4s).
2. On `agent_state` → `idle`: cancel the loop and send the agent's final reply as one
   message.
3. **Tool calls are suppressed** — not forwarded to the chat.
4. Errors/notices are forwarded (they are rare and matter).

## Permissions — always-allow only

The PoC supports **no interactive approval**. Sessions run always-allow; anything else
is out of scope until a telegram approval flow is designed.

**Required fix:** `_request_permission()` in the coordinator auto-allows only when
`_auto_allow_option()` returns an option id. Given an empty options list it returns
`None`, falls through to creating the pending future, and blocks forever waiting for a
client that will never answer. The PoC must resolve that case as **denied/cancelled**
rather than hang.

## Configuration the bot needs

Bot token; the focus and work chat ids; the daemon's address; the pointer file path;
the backend/model to spawn focus sessions with; a default path for `/new`.

## Deferred (explicitly not in the PoC)

- **Voice** — transcription in front of the forward path. Add last, after text works.
- **Inline buttons / tap-to-switch** — natural language via the focus channel is the
  primary path.
- **Interactive permission prompts.**
- **History replay on switch** — the bot subscribes to live events only; use
  `falconfox read` (or ask the agent) for history.
- **Cross-session attention pushes** — the design calls for notifications from
  unfocused sessions; the PoC only needs the focused one.

## Acceptance

The case-level success criterion, end to end, locally:

1. From telegram, **spawn** a session in a project.
2. Have it **do real work** (edit a file, run a command) with replies coming back.
3. **Switch** to another session via the focus channel, in natural language, and see
   the change announced.
4. **Restart the daemon** and resume the session, with its transcript intact.

Plus: focus sessions leave **no trace** in `falconfox list` after rotation, and a long
agent turn shows "typing…" for its duration rather than appearing to hang.
