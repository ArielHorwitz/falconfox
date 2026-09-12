# Survey briefs

The briefs handed to the three surveyors. Kept so the reports can be read against what was asked.

## Common brief


You are one of three read-only surveyors of the FalconFox repository at
`/home/ariel/projects/falconfox` (branch `dev`). A lead will integrate your
report with the others. You do not edit anything. You do not create files.
Your final message IS the report; write it fully there.

## What FalconFox is

A small vendor-neutral Agent Client Protocol (ACP) session daemon plus a
Telegram bot client. Read `README.md` first (5 min). Then `docs/buglist.md`
and `docs/wishlist.md`: they hold the known problems and the decisions already
taken, so do not re-discover those, but DO connect your findings to them when
a structural cause explains a listed bug. `docs/casebook/` holds per-case
records; skim only if a finding needs history. Use `git log`/`git blame` where
"why is it like this" matters.

Ignore `src/falconfox/web/static/` entirely: it is a dead browser UI slated
for deletion. Ignore `.worktrees/`.

## What the lead wants, and what they do NOT want

The session is about **hardening and maintainability**: structure that
changes what can go wrong, coupling that makes changes risky, duplicated or
implicit state, unclear ownership, missing invariants, failure modes that
are silent or unrecoverable, and things that make the next change expensive.

The lead explicitly does NOT want: style, naming, "idiomatic" rewrites,
formatting, framework fashion, or taste. Every finding must name a concrete
consequence: a bug class, a specific race, a scenario that loses data or
strands a session, a change that today has to touch N places. If you cannot
name the consequence, drop the finding. Concrete beats broad: "the X handler
can run twice for one event because Y" beats "concurrency is fragile".

Be honest about confidence. If you traced a race end to end, say CONFIRMED
and give the interleaving. If you suspect but did not trace, say PLAUSIBLE
and what would confirm it. Do not pad. A report with five real findings beats
one with fifteen soft ones.

## Report format (your final message)

1. **Map** (≤300 words): the structure of your area as it actually is. What
   state exists, who owns it, what tasks/loops run, what the lifecycle is.
   Write for someone who will make decisions without reading the code.
2. **Findings**, ranked by consequence, each with:
   - Title
   - Where: `path:line` references
   - What goes wrong: the concrete scenario
   - Evidence: what you read that shows it (quote a few lines if needed)
   - Confidence: CONFIRMED / PLAUSIBLE
   - Remedy: the structural change that removes the class of problem, not a
     patch. One or two sentences. Rough size (hours / a day / multi-day).
   - Related buglist/wishlist entry, if any
3. **Seams** (≤200 words): if this area were to be decomposed or reshaped,
   where are the natural cut lines and what makes each one safe or unsafe.
4. **Test coverage** (≤150 words): what in your area is exercised by
   `tests/test_falconfox_poc.py` and what is not, specifically the risky parts.
5. **Questions for the lead**: anything you could not settle.

Aim for 1200–2000 words total. Precision over volume.

## Telegram client


Primary: `bot.py` (~2900 lines), plus `api.py`, `shell.py`, `rendering.py`,
`__main__.py`. This is the largest module in the repo and the one where most
of the buglist lives (hung API calls, extra "Working..." message, stranded
topics, lost icons, turn-end races).

Focus on:
- **State inventory.** Every piece of mutable state the bot holds (dicts keyed
  by session/topic/message, pointer files, `topics.json`, in-flight tasks),
  who writes it, whether it is persisted, and what happens to it on restart,
  on daemon reconnect, and on a Telegram API failure mid-operation.
- **Concurrency model.** What asyncio tasks exist, how they are started and
  cancelled, what shares state without a lock, and which sequences of daemon
  events + Telegram updates can interleave badly. Trace the turn lifecycle
  (message in → reaction → queue → send → progress edits → reply → reaction)
  end to end and name the points where two paths touch the same state.
- **Failure handling.** Where API errors are swallowed at debug level, what
  is retried and what is not, what leaves the user with silence.
- **Coupling.** What in `bot.py` knows about the daemon's wire format, what
  knows about Telegram's, and whether those are separable. Whether the
  command handlers, the forum/topic sync, the tray, and the turn feedback are
  entangled or merely co-located.
- **The buglist entries** for this area: for each, say whether the cause is
  local or structural.

## Daemon core


Primary: `coordinator.py` (~1000 lines), `engine/` (`session.py`,
`client.py`, `oneshot.py`, `events.py`), `state.py`, `storage.py`,
`config.py`, `watchdog.py`, `echo_backend.py`, `help.py`.

Focus on:
- **Session lifecycle and invariants.** States a session can be in (stored,
  live, working, idle, ephemeral, evicted), the transitions, and who is
  allowed to trigger each. Look for transitions that can happen concurrently
  (a send arriving during eviction, a stop during resume, a delete during a
  turn, two sends racing for the same slot under `max_live_sessions`).
- **Persistence and crash consistency.** What is written to disk, when, and
  whether a crash between two writes leaves a consistent state. Transcript
  writes vs metadata writes. Whether anything is written non-atomically.
  What happens on daemon restart with turns in flight (see the wishlist
  entry on interrupted turns).
- **The ACP subprocess boundary.** How backend processes are started,
  supervised, and killed; what happens when one hangs, exits unexpectedly,
  or floods events; whether a misbehaving backend can wedge the coordinator.
- **Event fan-out.** How `session_updated` and friends reach clients; what
  happens when a client is slow or disconnected; whether events can be lost
  or reordered relative to state.
- **Config.** How config reload works and what state survives it.
- **Ownership.** What the coordinator does that is not coordination, and
  what would have to move for it to shrink.

## Edges


Primary: `src/falconfox/web/server.py` (the live HTTP/websocket API; NOT the
`static/` assets), `src/falconfox/cli.py`, `src/falconfox_telegram/api.py`
(the daemon-client side, `DaemonApi`), `tests/test_falconfox_poc.py` (~4100
lines, one file), `deploy/` (units, `setup.sh`, `update.sh`, `provision.sh`,
`README.md`), `pyproject.toml`, `hatch_build.py`, `_version.py`.

Focus on:
- **The daemon↔client contract.** Enumerate the HTTP routes and websocket
  event types the server exposes and what `cli.py` and the Telegram
  `DaemonApi` consume. Is the contract defined once or three times? What
  happens when server and client disagree (version skew during a deploy,
  since stable and dev instances share a host)? How are errors carried over
  the wire and are they distinguishable on the client?
- **The websocket.** Reconnect, backlog, missed events on reconnect, and
  whether a client can resynchronise its view after a gap.
- **The test suite.** The file is 4100 lines with ~35 test classes. Map what
  layer each class exercises and how (real daemon? stubbed API?
  `FakeTelegram`?). Identify: what risky behaviour has no test, which tests
  are coupled to internals such that a refactor of `bot.py` or
  `coordinator.py` would break them without catching a regression, and
  whether the fakes drift from the real interfaces. What would make it safe
  to refactor the two big modules?
- **Deployment.** Two instances (stable + dev) on one host from two
  checkouts. What is shared (ports, state dirs, config, the node bridge)?
  What in `update.sh`/`setup.sh` can leave the host half-updated (the
  buglist has one such entry: connect to it, and look for siblings). What
  does rollback actually restore?
- **Versioning.** How the version is produced and whether anything checks
  it at runtime.
