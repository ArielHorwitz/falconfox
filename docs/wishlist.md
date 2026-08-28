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

## Don't drop a message sent mid-turn — queue it or interrupt

*From the phone, 2026-08-25.*

Today a message sent while a turn is running is refused with "send it again
once the reply arrives" — itself a fix over the previous behaviour, which
silently destroyed both the message and the in-flight reply. But refusal
still loses the user's words unless they retype them, which is exactly wrong
on a phone.

Wanted: the message is kept, not bounced. Two reasonable fates, and possibly
both offered as inline buttons on the bot's "still working" response:

- **Queue** — hold the text and forward it the moment the turn ends.
- **Interrupt** — cancel the running turn and send now (the daemon already
  supports `cancel`; the turn-feedback work already delivers partial output,
  so an interrupted turn's progress is not lost).

Notes for whoever builds it: queuing belongs in the bot (the daemon
deliberately refuses mid-turn prompts and should keep doing so); a queued
message needs to survive a bot restart (the persisted turn map in
`turns.json` is the established pattern); and the buttons need the bot's
first callback-query handling — the same machinery a future "cancel turn"
button on the progress message would use.

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

## A reaction on the prompt message as a turn marker

*From the turn-feedback case, 2026-08-28.*

Liked rather than rejected, and parked when that case closed. React to the
user's own prompt message to mark the turn — one glyph on receipt, another on
completion — giving turn-received and turn-done feedback at **zero message
cost**, in a chat where every added message is clutter on a phone screen.

Reply-threading already took the notification half of the original idea (the
reply quotes the prompt, so the phone notification carries its context). The
marker half is still available and independent of it. The case it came from
shipped enough turn feedback that this is now a refinement rather than a gap,
which is exactly why it is here and not in that case.

## Make use of Telegram message streaming

*From the phone, 2026-08-28.*

Bot API 9.3 (2025-12-31) added `sendMessageDraft`, "allowing partial messages
to be streamed to a user while being generated" — a message that fills in as
it is produced, rather than one that is sent whole or edited in place. It
takes `can_stop` / `keep_on_stop`, so the user can halt a generation from the
chat.

Wanted; **how is deliberately open**. Everything the bot shows today is built
from whole messages — a progress message created up front and edited as work
happens, a reply sent once the turn ends. Streaming is a different primitive
underneath both of those, and it postdates the design that chose them, so the
right question is not "where do we bolt this on" but "what would the turn look
like if this had existed". Whether it carries the reply, replaces the progress
message, does both, or neither, is exactly what has not been decided.

Worth reading the turn-feedback case
([2026-08-24__165f0606](casebook/2026-08-24__165f0606/overview.md)) first:
it settled the two-message turn against the constraints of whole messages,
and it records why each of those choices was made — which is what tells you
whether streaming actually improves on them or just moves them.

## Deliberately not planned

**Off-loopback remote access + bearer token.** Listed in the pivot case as the
daemon's feature 2 and "the genuinely new capability", then made unnecessary by
the architecture: the Telegram bot is co-located with the daemon, so the daemon
binds `127.0.0.1` and Telegram *is* the remote access. Exposing it would add an
auth surface nothing needs. Recorded here so it is not re-raised as an
oversight — it was deleted by the design, not skipped.
