# Wishlist

Wanted but not built. This file exists so that closing a case does not lose the
work it deliberately pushed forward — an item here has been **decided against
doing now**, with the reason, rather than forgotten.

Keep entries short and honest about status. When something is picked up, delete
the entry; the reason it was wanted belongs in whatever case takes it on. See
[bugs.md](bugs.md) for things that are broken rather than missing.

## Desktop client — rewire the web UI onto flat sessions

*From the falconfox pivot case, 2026-08-24.*

The web assets under `src/*/web/static/` are the **desktop client**: the
counterpart to Telegram as the mobile client. Flattening the session model
(session keyed by id, carrying its path) broke the old case- and
project-centric navigation, so the UI is shipped unwired.

Deliberately a separate effort. Telegram is *enough* to dogfood falconfox while
developing it — that was the standard the pivot set for itself and met, and
rewiring the UI would have delayed the thing that proved the thesis. The
assets are kept rather than deleted precisely because a working UI is
substrate worth re-earning.

## Voice input

*From the falconfox pivot case, 2026-08-24.*

The original motivation: hands-free agent work on long commutes. Architecturally
it is a transcribe step in front of the existing forward path — orthogonal to
the daemon, which is why it was safe to defer at every step while the novel
parts were built.

Wanted, and not a blocker. Text first was the right order; voice is now its own
effort rather than an unfinished corner of the pivot.

## Persist the bot's turn→chat map across bot restarts

*From the observability case, 2026-08-25.*

A bot-only restart mid-turn keeps the turn alive daemon-side but loses the
bot's in-memory record of which chat the reply belongs to, so the reply is
never delivered and — since the new process is not tracking the turn — the
silent-turn report does not fire either. Observed live: a scheduled bot
restart landed five seconds into a fresh turn and orphaned its reply.

The fix is a small state file (the bot already owns the pointer file pattern):
persist `{session_id: chat_id}` on forward, reload on start, and let the new
process adopt in-flight turns. Deferred because bot restarts are rare, the
collision window is one turn, and the transcript still holds the reply — but
it is exactly the kind of silent loss this case exists to eliminate, so it
should be picked up with the turn-feedback case.

## Tell the user what a session is actually doing

*From the phone, 2026-08-24.*

Reported alongside the typing bug, and not fixed by fixing it: from the work
chat there is no way to tell whether a session is idle, working, or stuck. The
typing indicator is the only signal, and it is a poor one — it says the *bot*
believes a turn is in flight, which is a weaker claim than the session actually
working. A wedged backend and a busy one look identical.

The daemon already knows more than the chat shows. `falconfox list` reports
`idle` / `working` / `stored` per session, but today only the focus chat can
ask, and asking there means leaving the conversation. Something like a
`/status` in the work chat would close most of the gap.

There is a second, richer source the client currently ignores: the daemon emits
`session_updated` carrying `state: starting` while an ACP subprocess is being
resumed, and `src/falconfox_telegram/bot.py` does not consume that event at all
— `_handle_event` returns early on anything that is not `agent_state`.
Consuming it would let the chat distinguish *starting* from *working*, which is
the distinction a cold turn most needs, and it is the same change the typing
bug wants. See "The typing indicator dies on one transient Telegram error" in
[bugs.md](bugs.md); these two are worth doing together.

"Stuck" is the harder half and genuinely missing rather than merely unexposed:
nothing distinguishes a long turn from a hung one. That needs a notion of time
since the last event, not just the current state — which is new bookkeeping,
either in the bot or in the daemon's session metadata.

## Choose the model when spawning a session

*From the phone, 2026-08-25.*

`falconfox spawn` takes `--path`, `--name`, `--backend` and `--ephemeral` — but
not a model. Today the only way to run a session on a different model is to
declare a **second backend** in `config.toml` with its own `env` or
`config_options`, then `spawn --backend <name>`. That works (verified with
`ANTHROPIC_MODEL=claude-fable-5`), but it means every model is a config edit
plus a daemon config reload, and the choice is baked into a backend name rather
than made per session.

Wanted: `falconfox spawn --model <id>`, so a session can be started on a
different model without touching config — most usefully from the focus chat,
which is where sessions actually get spawned.

The design constraint is the case's own layer boundary: the daemon knows
nothing about models, and shouldn't start. The model is a **backend concern**,
already expressed two ways per backend (`env` for vendor-specific selection,
`config_options` for ACP-advertised options). A `--model` flag would have to
resolve to one of those rather than becoming a daemon-level concept — likely by
setting the ACP `model` config option at session start, with the env-var route
staying the escape hatch for values a backend does not advertise.

## Rename the `falconfox-pointer` skill to match its job

*From the falconfox pivot case, 2026-08-24.*

The skill now manages sessions — focus, spawn, rename, stop, delete — but its
directory is still named for the pointer alone, which undersells it to the very
agent reading it.

Blocked on a real hazard, not on effort: `_prepare_focus_workspace` writes the
packaged skill into the focus workspace but never prunes what is already there,
so renaming the directory today would leave **both** the old and new skill
discoverable, with conflicting instructions. Fix the pruning first — see
[bugs.md](bugs.md).

## Deliberately not planned

**Off-loopback remote access + bearer token.** Listed in the pivot case as the
daemon's feature 2 and "the genuinely new capability", then made unnecessary by
the architecture: the Telegram bot is co-located with the daemon, so the daemon
binds `127.0.0.1` and Telegram *is* the remote access. Exposing it would add an
auth surface nothing needs. Recorded here so it is not re-raised as an
oversight — it was deleted by the design, not skipped.
