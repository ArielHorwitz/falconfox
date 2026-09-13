# Package: harden the daemon core

You are implementing, in a git worktree at
`/home/ariel/projects/falconfox/.worktrees/hardening-daemon` (branch
`hardening-daemon`). Work only there. Commit as you go, one commit per step,
with messages in the repo's existing style (`fix:`/`feat:`/`refactor:` prefix,
a short imperative subject, a body that says why). Do not push. Do not touch
`src/falconfox_telegram/`, `src/falconfox/web/static/`, or `.worktrees/`.
Do not restart any daemon or systemd unit.

## Read first

1. `docs/casebook/2026-09-12__07b4eb32/overview.md` for the whole picture.
2. `docs/casebook/2026-09-12__07b4eb32/survey-daemon-core.md` for your area
   in detail, with line references. The lead has spot-checked findings F1,
   F2 and F3 and they hold.
3. `README.md` for what the product promises.

The project's own conventions: `.claude/CLAUDE.md` in the repo root. The
user's preferences that matter here: no em-dashes in prose or docs,
descriptive names (never single-letter variables outside loop indices),
`Optional[T]` not `T | None` in new code, `pathlib` for paths. Follow the
existing code's conventions where they are explicit.

## Goal

Make the coordinator unable to exhibit the race and consistency classes the
survey found, by changing its shape rather than patching sites. The wire
format the Telegram client sees (event types, fields, the HTTP and websocket
actions) must not change in this package: another package owns the
contract, and the client on this host must keep working against your
coordinator unchanged.

## Steps, in order

Each step: write the test that reproduces the finding first and watch it
fail, then make it pass, then commit. Run the whole suite (`uv run pytest`)
before every commit.

1. **Atomic metadata write.** `Storage.write_meta` gets the same
   temp-then-replace treatment `rewrite_transcript` has. Test: a torn
   `meta.toml` is impossible to produce through the API, and `load_all_meta`
   still skips (with a WARNING naming the session) a torn file that exists on
   disk rather than crashing. Small.

2. **A `Session` record.** Replace the parallel per-session dicts in
   `SessionCoordinator` (`_metadata`, `_transcripts`, `_acp_ids`,
   `_config_options`, `_commands`, `_pending_context`, `_attachments`,
   `_auto_named`, `_usage`, `_busy_ids`, `_queued`, `_persisted`, and any
   others you find) with one `dict[str, SessionRecord]` where the record
   holds all of it. Decide and document whether "live" is answered by the
   record or by the engine's `SessionManager`, and make it one source of
   truth. `stop_session` and `delete_session` must clear state by discarding
   or resetting the record, not by hand-listing fields. This is a pure
   refactor: behaviour and wire format identical, existing tests pass. It
   may be several commits. Keep the public method names and signatures the
   coordinator exposes today, since `web/server.py` calls them.

3. **Serialise lifecycle transitions.** One `asyncio.Lock` per session held
   by every mutating coordinator method (add, resume, send, cancel, stop,
   delete, revert, fork, rename, set_config_option), plus one lock for slot
   accounting so that `_ensure_slot` and the activation it precedes are one
   critical section. Tests, each driven with real concurrent tasks rather
   than sequential awaits:
   - two concurrent slot-needing actions at the cap never exceed the cap
     (survey F1, with the exact interleaving it describes);
   - `delete_session` during an in-flight turn does not lose the turn
     silently: either it waits for the turn to end or it cancels the turn
     and the outcome is logged and emitted (choose, and say why in the
     commit);
   - `revert_session` during a turn cannot append the dying turn's events
     after the rewrite (survey F2);
   - a second `send` to a session still starting waits rather than raising
     (survey F6).
   Watch for deadlock: a method holding a session lock must not await
   another method that takes the same lock. Say in the commit body which
   methods take which locks and in what order.

4. **Persist turn boundaries.** Today `turn_started`/`turn_ended` are not
   replayable, so a restart mid-turn leaves no trace. Make the open turn a
   persisted fact (either by making those events replayable or by a marker
   in metadata; choose the one that keeps transcript replay to backends
   sensible, and say why). On startup, a session whose last persisted turn
   is open gets an interrupted-turn notice queued through the existing
   `_pending_context` channel, so its next prompt tells the agent that its
   previous turn was cut off and roughly where (last event type and time).
   Wording: plain, one short paragraph, in the voice of the existing
   orientation text. Test: persist a turn, simulate restart, assert the
   notice is delivered with the next send and only once.

5. **Bound the event bus.** `EventBus` subscriber queues get a bound. On
   overflow, the slow subscriber is dropped and its queue closed so the
   websocket for that client closes, which triggers the client's existing
   reconnect and snapshot path. Log at WARNING naming the subscriber. Test:
   a subscriber that never drains is disconnected and the others are
   unaffected. Pick the bound generously (thousands of events) and put it in
   config with a documented default.

6. **CLI timeout.** `cli.py` `_request` uses `timeout=None`. Give it a
   finite default read timeout, a longer one for `send` and `attach` (which
   legitimately block for a whole turn or a client round-trip), and a clear
   `CliError` on timeout. Small.

## Do not

- Do not widen scope into the Telegram client, the wire contract, or the
  deploy scripts. Note anything you see there in your final report instead.
- Do not remove `revert`/`fork`/`set_config_option`; another package owns
  that decision. They must keep working under the locks.
- Do not add dependencies.
- Do not add the case files to your commits; the lead owns the casebook.

## Report

Your final message: what landed (commit list with one line each), what each
test proves, any decision you made that the lead should know about, anything
you found outside your scope, and anything you could not finish and why.
Also the `uv run pytest` summary line.
