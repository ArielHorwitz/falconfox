# Overview

This case is a hardening and maintainability pass over FalconFox. It is
about structure that changes what can go wrong: races, silent failures,
state that can tear, and shapes that make the next change expensive. It is
explicitly **not** about style, naming, or idiom. A finding earns its place
only by naming a concrete consequence.

Opened 2026-09-12 against `dev` at 9b43889.

## Method

The lead session decomposed the codebase into three areas and delegated a
read-only survey of each to an Opus 4.8 agent, from the briefs in
[survey-briefs.md](survey-briefs.md). The reports are kept verbatim:

- [survey-telegram-client.md](survey-telegram-client.md)
- [survey-daemon-core.md](survey-daemon-core.md)
- [survey-edges-contract-tests-deploy.md](survey-edges-contract-tests-deploy.md)

The lead then spot-checked the highest-stakes claims against the source.
Every one checked held up (table below). Claims not spot-checked carry only
the surveyor's own confidence label.

## The picture

Read together, the three reports describe **one problem in two places and a
handful of independent ones**.

### 1. Per-entity state is a spread of parallel dicts, in both processes

The bot holds a turn as roughly twenty dicts keyed by session id
(`bot.py:683-723`), and the coordinator holds a session as roughly twelve
(`coordinator.py`). Neither has a `Turn` or `Session` record. Every
lifecycle site (start, finish, adopt on restart, reset on reconnect) pops
and initialises each field by hand.

Consequences already observed or traced:

- **The orphaned "Working..." message** in the buglist. `_finish_turn` pops
  the turn destination first, then awaits the loops and the reply send, then
  pops the remaining fields. A fresh message for the same session arriving
  in that window starts a new turn, whose fresh state the old teardown then
  erases. Confirmed mechanism, plausible as the buglist cause.
- **Usage figures leak across turns.** `_usage_view` is never cleared per
  turn, so a turn with no usage event reports the previous turn's numbers.
- **Partial cleanup in the coordinator.** `stop_session` and
  `delete_session` clear slightly different field lists, so a forgotten pop
  is a latent leak with nothing to catch it.

This is the enabling refactor. Both surveyors independently concluded that
splitting either big module is unsafe until the record exists, and cheap
afterwards. Multi-day per process, high leverage.

### 2. Lifecycle transitions are not serialised in the daemon

The websocket dispatches every action as a detached task. Nothing holds a
lock across a transition, and the engine's busy flag guards only two
prompts, not coordinator-level teardown.

- **`max_live_sessions` can be exceeded.** Two slot-needing actions both
  read the live count, both pick the same idle victim, and both proceed,
  because `stop_session` marks the victim not-live only after an await.
  This is the OOM the cap exists to prevent. Confirmed by lead.
- **Delete or revert during a turn.** Revert rewrites the transcript while
  the dying prompt's error notice is still pending, then appends it to the
  rewritten file. Delete during a turn drops the in-flight reply with no
  log line. Confirmed unserialised.
- **Send to a still-starting session** raises a spurious agent error.
  Plausible.

Remedy: one lock per session plus a slot lock, held by every mutating
coordinator method. About a day. Fixes the whole class.

### 3. The bot cannot tell a Telegram failure from a daemon failure

The reply send has no error handling and the event loop has no guard, so a
persistent Telegram send failure propagates up to the reconnect handler,
which announces "daemon connection lost" and reconnects to a healthy daemon.
On reconnect the recovered turn is resent to the same dead destination. A
hand-deleted topic produces exactly this permanent failure, so the buglist's
"stranded session" is in fact a reconnect storm. Confirmed by lead.

Remedy: make per-event handling total, and treat a persistent send failure
to a bound thread as "unbind and recreate the topic", which is the reactive
fix the buglist already sketched and deferred. Half a day.

### 4. Crash consistency

- **`meta.toml` is written non-atomically** while the transcript rewrite
  already uses temp-then-replace. A crash mid-write leaves a torn file, and
  on restart the loader skips that session entirely: it vanishes from every
  listing with its transcript orphaned on disk. Metadata is rewritten on
  every replayable event, so the window is open continuously. Confirmed by
  lead. One hour.
- **Turn boundaries are never persisted.** A restart mid-turn leaves a
  prompt with a partial reply and no marker. This is the structural reason
  the wishlist's "tell a session its turn was interrupted" has not landed.
  Half a day, and it unblocks that item.

### 5. The wire contract is authored three or four times and untested

HTTP routes and websocket actions are two hand-maintained switch statements.
`send` over HTTP awaits the whole turn while `send` over websocket is
fire-and-forget. `revert`, `fork` and `set_config_option` are reachable by
no live client (only the dead web UI sent them). An unknown websocket action
is a logged no-op. No test exercises either transport, so drift is invisible
to a green suite. The version is announced but never checked.

Remedy: one action registry with thin transport adapters, an error on
unknown actions, a protocol version in the snapshot, and a table test over
the registry. About a day plus half a day of tests. This is most of what
makes `server.py` safe to change.

### 6. Backpressure and blocking

- Event bus queues are unbounded, so a wedged consumer grows daemon memory
  without limit on the same host the session cap protects. Hours to bound.
- No event backfill exists across a websocket gap. A reply produced during
  a blip is lost unless the client re-opens the session. Whether the bot
  does so is an open question below.
- The bot serialises and writes the whole turn map synchronously on every
  narration block. A chatty turn stalls every session's event handling on
  each write. Half a day to debounce.
- The CLI uses no timeout at all, so an agent running `falconfox send` or
  `attach` against a wedged daemon or bot hangs forever. Confirmed by lead.
  Hours.

### 7. Deployment can report healthy on stale units

`install-units` runs on every restart path including rollback, honours a
per-caller environment variable for a host-level fact, and the health check
tests liveness only. The buglist entry is one symptom of this shape. Half a
day.

### 8. Small and cheap

- Title and icon application runs outside the topic lock, so a
  `session_updated` event and the reconciler can double-apply. Plausible
  contributor to the intermittent icon bug. One hour.
- README still documents the reactions that were removed on 2026-09-12, and
  `FakeTelegram` still exposes a `set_reaction` the real API lacks. Hours.

## Lead spot checks

| claim | where | held up |
| --- | --- | --- |
| metadata write is non-atomic | `storage.py:46-49` vs `rewrite_transcript` | yes |
| reply send and event loop have no guard, reconnect handler catches `ApiError` | `bot.py:2478-2502`, `:2628-2630`, `:772-779`, `api.py:327-340` | yes |
| slot check-then-evict spans an await, victim marked not-live after await | `coordinator.py:414-460`, `:820-835` | yes |
| finish-turn pops destination first, awaits, then pops the rest | `bot.py:2813-2885` | yes |
| websocket dispatch is detached per action, dead actions still routed | `server.py:244-282` | yes |
| CLI request has no timeout | `cli.py:41` | yes |

## Proposed order

Cheap and independent first, so the soak keeps improving while the large
refactors are planned:

1. Atomic `meta.toml` write. Bound the event bus queues. CLI timeout.
2. Bot: total event handling, and unbind-and-recreate on persistent send
   failure. Title and icon apply under the lock.
3. Daemon: per-session and slot locks.
4. Persist turn boundaries, then the interrupted-turn notice from the
   wishlist.
5. Single action registry with tests and a protocol version. Delete the
   dead actions with the dead web UI.
6. The `Turn` record in the bot, then the `Session` record in the
   coordinator. Each unlocks the module split that follows it.
7. Deploy: home-relative paths, unit provenance in the health check.

Every step is a separate commit with its own test, and none needs the
others to land first except 6, which should come after 2 and 3 so the
records are introduced into code whose races are already closed.

## Open decisions

These change the work materially and belong to the user:

- **Unbind-and-recreate on send failure** was deferred before a stability
  soak. Is it now acceptable? It fixes both the storm and the strand.
- **Records before or after patches?** The `Turn` and `Session` records
  remove three findings at once but are multi-day. Patching those findings
  locally first re-creates the fragility the record exists to end.
- **Is the dual `send` contract deliberate?** Blocking over HTTP for the
  CLI, streaming over websocket for the bot. Unification or dedup depends
  on the answer.
- **Delete `revert`, `fork`, `set_config_option`** with the dead web UI, or
  keep them for a future client?
- **Single coordinator lock or per-session locks?** Per-session is more
  concurrent and more surface. On a one-bot host a single lock may do.
- **Shell tests for `deploy/`**, or out of scope?
- Does the bot re-open every session on reconnect, or rely on live events?
  This decides whether replies during a websocket gap are actually lost.

## Progress

*Updated 2026-09-13.*

Decisions taken with the user: structural work first rather than patches,
since a separate stable instance covers dogfooding while dev is in flux. A
hand-deleted topic is replaced silently on the next delivery. Rewind, fork
and per-session config options stay in the daemon, unreachable, and leave
together with the dead web UI. The interrupted-turn notice is in scope.

Each package is implemented by an Opus 5 delegate from a written brief,
reviewed read-only by an Opus 4.8 delegate, and the review's findings go
back to the same implementer session before the branch is integrated. The
briefs, reports and reviews are filed beside this overview.

| package | state | tests |
| --- | --- | --- |
| Telegram client | integrated onto the case branch, 10 commits | 261 to 280 |
| Daemon core | reviewed, fixes and rebase in progress, 7 commits so far | 282 on its branch |
| Wire contract and deploy | not started, waits for the daemon branch | |

Buglist entries closed so far: the stranded session after a hand-deleted
topic, and the extra "Working..." message after a reply. Bonus fixes found
by the implementers and confirmed by tests: a resumed session could show a
one-event history in place of its transcript, and stopping a never-renamed
session after a restart deleted it from disk (found by the daemon review,
fix in progress).

## Follow-ups surfaced, not taken up here

- **The bot handles every daemon event on one task**, so any pause while
  delivering one session's reply stalls event handling for every session.
  This is why the reply retry is capped at a few seconds rather than
  honouring a long `retry_after`. Per-session handling tasks would remove
  the ceiling.
- **Proactive topic liveness on reconnect.** Editing a topic with unchanged
  values answers "not modified", which the buglist already measured, and
  that doubles as a probe. One call per bound session at startup and
  reconnect would repair dead topics before anything needs them, and would
  cover renames and tags, which the reactive repair does not. Offered to
  the user, not yet decided.
- **Rate-limit handling** in the bot's Telegram client (the wishlist entry)
  is still open. The reply retry now honours `retry_after` up to a cap, but
  nothing else does.
