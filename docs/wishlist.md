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

## Tell the user what a session is actually doing

*From the phone, 2026-08-24.*

Reported alongside the typing bug, and not fixed by fixing it: from the work
chat there is no way to tell whether a session is idle, working, or stuck. The
typing indicator is the only signal, and it is a poor one — it says the *bot*
believes a turn is in flight, which is not the same claim. A wedged backend and
a busy one look identical.

The daemon already knows more than the chat shows: `falconfox list` reports
`idle` / `working` / `stored` per session. Today only the focus chat can ask,
and asking there means leaving the conversation. Something like a `/status` in
the work chat would close most of the gap.

"Stuck" is the harder half and genuinely missing rather than merely unexposed:
nothing distinguishes a long turn from a hung one. That needs a notion of time
since the last event, not just the current state.

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
