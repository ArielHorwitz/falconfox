# Package: harden the Telegram client

You are implementing, in a git worktree at
`/home/ariel/projects/falconfox/.worktrees/hardening-bot` (branch
`hardening-bot`). Work only there. Commit as you go, one commit per step,
with messages in the repo's existing style (`fix:`/`feat:`/`refactor:` prefix,
a short imperative subject, a body that says why). Do not push. Do not touch
`src/falconfox/` (the daemon: another package is refactoring it right now),
`src/falconfox/web/static/`, or `.worktrees/`. Do not restart any daemon,
bot, or systemd unit. Do not send anything to Telegram.

Put every new test in a NEW file, `tests/test_telegram_hardening.py`, not in
`tests/test_falconfox_poc.py`: the other package is appending to that file
and the two branches must merge without conflict. You may import
`FakeTelegram` and helpers from the existing test module. If you must change
an existing test because behaviour legitimately changed, keep that edit
minimal and say so in the commit.

## Read first

1. `docs/casebook/2026-09-12__07b4eb32/overview.md` for the whole picture.
2. `docs/casebook/2026-09-12__07b4eb32/survey-telegram-client.md` for your
   area in detail, with line references. The lead has spot-checked F1 and
   F2 and they hold.
3. `README.md` for what the product promises, and `docs/buglist.md` for the
   entries this package should close.

The project's own conventions: `.claude/CLAUDE.md` in the repo root. The
user's preferences that matter here: no em-dashes in prose or docs,
descriptive names (never single-letter variables outside loop indices),
`Optional[T]` not `T | None` in new code, `pathlib` for paths. Follow the
existing code's conventions where they are explicit.

## Goal

Make the bot unable to exhibit the failure classes the survey found, by
changing its shape rather than patching sites. The daemon's wire format is
fixed for this package: you consume it as it is today.

## Steps, in order

Each step: write the test that reproduces the finding first and watch it
fail, then make it pass, then commit. Run the whole suite (`uv run pytest`)
before every commit.

1. **A Telegram failure is never a daemon failure.** Per-event handling in
   `_receive_events` becomes total: an `ApiError` (or any exception) raised
   while handling one daemon event is logged at WARNING with the session id
   and event type, and the loop continues. The reconnect path in `run()`
   fires only on the socket itself failing (`ConnectionClosed`, `OSError`),
   never on `ApiError`. Test: a reply send that raises does not trigger the
   "daemon connection lost" announcement and the next event is still
   handled.

2. **A dead topic is replaced, not mourned.** When a send to a session's
   bound thread fails with Telegram's "thread not found" class of error
   (identify the exact error text or code from the Bot API; check
   `docs/buglist.md` "Deleting a topic by hand strands its session" and
   `git log` for anything already known), the bot unbinds the session from
   that thread, creates a fresh topic through the existing `_ensure_topic`
   path, delivers the pending message there, and posts one short line in
   the new topic saying the previous topic was gone so a new one was made.
   Other send failures (rate limit, network) are retried on the existing
   paths and never trigger unbinding. Test: a send to a bound thread that
   answers "thread not found" results in exactly one new topic, the reply
   landing there, and the binding updated and persisted. Close the buglist
   entry by deleting it in `docs/buglist.md` in the same commit.

3. **A `Turn` record.** Replace the per-session per-turn dicts in
   `FalconFoxTelegramBot` (`_turn_dest`, `_turn_id`, `_reply_parts`,
   `_consumed`, `_delivered`, `_progress_msg`, `_progress_lines`,
   `_progress_sent`, `_progress_dirty`, `_seen_tools`, `_thought_parts`,
   `_usage_view`, `_prompt_msg`, `_activity_tasks`, `_progress_tasks`,
   `_activity_state`, `_last_event_at`, `_adopted`, `_turn_working`, and any
   others you find; the queue may stay separate if it outlives a turn) with
   one `dict[str, Turn]`. `_forward` installs a new `Turn`; `_finish_turn`
   captures the `Turn` object it is finishing by identity at its start, and
   tears down *that object* at its end, so a racing `_forward` that installed
   a new `Turn` for the same session during the awaits is untouched.
   `_adopt_turn`, `_reconcile_persisted_turns`, `_reset_connection_state`,
   and `_persist_turns` operate on the record. Usage figures live on the
   `Turn`, so they cannot leak across turns (survey F3).
   Tests: (a) the exact interleaving of survey F2: a new message for the
   same session arrives while `_finish_turn` is awaiting the reply send, and
   afterwards there is exactly one progress message for the new turn and
   the old one is finalised; (b) a second turn without a usage event does
   not report the first turn's numbers; (c) turn persistence and the three
   reconciliation outcomes (adopt, recover, lost) still pass unchanged.
   Delete the buglist entry "An extra Working... message appears after the
   reply" if and only if your test (a) reproduces the orphaned message on
   the old code; otherwise leave the entry and say so.
   This step may be several commits.

4. **Title and icon under the lock.** `_apply_title` and `_apply_icon` do
   their check-then-await-then-record under `_topic_lock` (or a per-topic
   lock, your call, say why), so a `session_updated` event and the
   reconciler cannot double-apply. Test: concurrent apply of the same title
   makes exactly one API call.

5. **Persistence off the hot path.** `_persist_turns` is called on every
   narration block and thought close and writes the whole map
   synchronously. Debounce it: mark dirty, flush on a short timer and
   always at turn boundaries and before disconnect. Test: many narration
   events in one tick produce one write.

6. **Websocket send discipline.** `_report_attachment` sends over the
   websocket without `_ws_lock` while `_forward` holds it. Decide whether
   the lock is needed for concurrent sends on this `websockets` version and
   make every send consistent with that decision. Say why in the commit.

7. **Fakes and docs that describe a client that no longer exists.**
   Remove `set_reaction` from `FakeTelegram` (the real `TelegramApi` has
   none). Add one test that asserts every public method of `FakeTelegram`
   exists on `TelegramApi`, so the fake cannot drift again. Delete the
   README paragraph describing message reactions (👀 🫡 ✍ 👌 💔 😱), which
   were removed on 2026-09-12.

## Do not

- Do not widen scope into the daemon, the wire contract, or the deploy
  scripts. Note anything you see there in your final report instead.
- Do not add dependencies.
- Do not add or edit files under `docs/casebook/`; the lead owns the
  casebook. `docs/buglist.md` and `README.md` edits named above are fine.

## Report

Your final message: what landed (commit list with one line each), what each
test proves, any decision you made that the lead should know about, anything
you found outside your scope, and anything you could not finish and why.
Also the `uv run pytest` summary line.
