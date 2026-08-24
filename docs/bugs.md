# Known bugs

Defects that are known and not yet fixed. An entry here is a thing that is
**wrong**, as opposed to [wishlist.md](wishlist.md), which is a thing that is
**missing**.

Record what fails, under what conditions, and how bad it is — enough that
whoever picks it up does not have to rediscover it. Delete the entry when the
fix lands.

## The typing indicator dies on one transient Telegram error

*Reported from the phone, 2026-08-24.* All in `src/falconfox_telegram/bot.py`:
`_typing_loop`, `_start_typing`, and the `agent_state` branch of
`_handle_event`.

"typing…" is intermittent: it appears, then stops while the agent is still
working, so a long turn once again looks like it may have hung.

`_typing_loop` re-sends `sendChatAction` every 4 seconds (the action expires
after about 5), and catches only `CancelledError`. But `telegram.typing()`
reaches `_json_request`, which raises `ApiError` on any HTTP, URL or OS error —
including a 429 from Telegram's rate limiter, which a turn long enough to need
an indicator is exactly the kind to provoke, and including the read timeouts
this deployment is already known to see.

One such error ends the task. Nothing restarts it, and the reason is the guard
in `_start_typing`:

    if session_id not in self._typing_tasks:

A dead task is still *in* `_typing_tasks` — the dict holds the finished object —
so the guard reads it as still running and declines to start a new one. The
`agent_state: working` safety net cannot recover it either, because it goes
through the same guard. The turn stays silent from then on, and the exception is
never retrieved, so nothing is logged at the time.

Fix has two halves, matching the two faults: make the loop survive an `ApiError`
(log and keep looping — a failed keystroke animation is never worth ending a
turn's feedback over), and make `_start_typing` treat a task that is `done()` as
absent so the safety net can genuinely restart it.

### Before changing this, know what is deliberate

This is the third pass over the typing indicator, and two earlier decisions are
easy to undo by accident:

- **Typing starts in `_forward()`, when the prompt is sent — not on
  `agent_state: working`.** That placement is deliberate. A stored session
  resumes an ACP subprocess first, and the daemon reports *that* as
  `state: starting` on `session_updated`, an event this client does not consume
  at all. Waiting for `working` leaves the entire startup window — the slowest
  part of a cold turn, and exactly when a turn looks hung — silent. Do not move
  it back.
- **Do not cancel typing on an error notice.** It was considered and rejected:
  `_warn_option` emits `level: "error"` for non-terminal problems, such as a
  config option that would not apply, so cancelling on it would kill the
  indicator on turns that are perfectly fine.

Unrelated but adjacent, so it does not read as this bug:
`_reset_connection_state` cancels every typing task when the daemon connection
drops. That is correct — the turn is gone with it.

### Tests

`tests/test_falconfox_poc.py` already covers the lifecycle:
`test_typing_starts_when_the_prompt_is_sent` and
`test_turn_uses_typing_and_one_final_message`. The first asserts that a later
`working` event must **not** start a second typing loop while one is live, and
that `_typing_tasks` is empty after `idle` — a `done()`-aware guard keeps both
true, but a fix that simply drops the guard will not. Neither test currently
covers a `telegram.typing()` that raises, which is the whole of this bug; that
is the test to add.

### The broader problem this only half solves

A reliable indicator still only says *the bot believes a turn is in flight*.
Knowing whether a session is idle, working or stuck is a separate gap, tracked
as "Tell the user what a session is actually doing" in
[wishlist.md](wishlist.md). Whoever fixes this should read that entry too —
consuming `session_updated` serves both, and doing them together is likely less
work than doing either alone.

## The focus workspace never prunes stale skills

*Found while widening the focus skill, 2026-08-24.*

`_prepare_focus_workspace` in the Telegram bot writes the packaged
`falconfox-pointer` skill into `<pointer dir>/.agents/skills/` on every start,
but never removes skill directories that are already there. Nothing is broken
today, because the name has never changed.

It breaks the moment one does: renaming or splitting the skill leaves the old
directory in place, and the focus agent discovers **both**, with conflicting
instructions about what it may do. That is a silent failure — the agent behaves
oddly rather than erroring — and it currently blocks a wanted rename.

Fix: treat the skills directory as owned by the bot and reconcile it to exactly
what the package ships, rather than writing into it additively.

## A rebuilt VPS loses the Python 3.12 shim

*From the first phone session, 2026-08-24.*

On Ubuntu 22.04 the bare word `python3` is **3.10**, which cannot import
`tomllib`. Any tool documented as "run it with python3" therefore fails on a
stock host — the casebook skill's CLI did exactly that.

The fix in place is a host-local symlink, `~/.local/bin/python3 ->
/usr/bin/python3.12`, made by hand. It carries **no commit**, so it is not part
of any deployment: a rebuilt or second host silently reverts to 3.10 and the
same failure returns. `setup.sh` does not create it, deliberately — but the
bootstrap checklist has to, or this recurs.

falconfox itself is immune, since uv provisions its own interpreter and the
daemon, bot and deploy scripts all use absolute venv paths. The blast radius is
tools invoked as plain `python3` from inside a session.

## Known-broken by design

**The web UI does not work against the flat session model.** Flattening removed
the case/project navigation it was built on, and it ships unwired. This is a
deliberate state, not an accident — it is tracked as the desktop-client effort
in [wishlist.md](wishlist.md), and is listed here only so that finding a broken
UI does not read as an undiscovered bug.
