# Overview

Two faults reported from the phone on 2026-08-24, at the close of the falconfox
pivot case ([2026-07-24__d53a46fd](../2026-07-24__d53a46fd/overview.md)):

1. **"typing…" is intermittent** — it appears, then stops while the agent is
   still working, so a long turn looks like it may have hung.
2. **There is no way to tell whether a session is idle, working, or stuck.**

These arrived together and read as one complaint, but they are different in
kind. (1) is a defect with a known cause and a two-line fix. (2) is a missing
capability that (1)'s fix does not provide — a *reliable* typing indicator still
only says the bot believes a turn is in flight, which is a weaker claim than the
session actually working, and it says nothing at all about stuck.

The case's real question is (2). Fix (1) first because it is cheap and it stops
the bleeding, then decide what turn feedback should actually be.

Filed detail, written while the falconfox case was closing:
[docs/bugs.md](../../bugs.md) ("The typing indicator dies on one transient
Telegram error") and [docs/wishlist.md](../../wishlist.md) ("Tell the user what
a session is actually doing"). Both entries carry context this case should not
re-derive — in particular the two earlier decisions that look like bugs and are
not.

## Fault 1: why the indicator dies

`src/falconfox_telegram/bot.py`. `_typing_loop` re-sends `sendChatAction` every
4 seconds and catches only `CancelledError`, but `telegram.typing()` raises
`ApiError` on any HTTP, URL or OS error — a 429 from the rate limiter, or one of
the read timeouts this deployment already sees. One error ends the task, and
`_start_typing`'s guard

    if session_id not in self._typing_tasks:

reads the *finished* task as still running, so nothing restarts it. The
`agent_state: working` safety net goes through the same guard and cannot recover
it either. The exception is never retrieved, so nothing is logged at the time —
which is why it presents as random.

Fix: survive `ApiError` in the loop, and treat a `done()` task as absent.
`tests/test_falconfox_poc.py` covers the lifecycle but has no test for
`telegram.typing()` raising; that is the test to add.

**Do not undo two earlier decisions** (both from the first phone session, both
recorded in bugs.md): typing starts in `_forward()` rather than on
`agent_state: working` because the daemon reports a resuming ACP subprocess as
`state: starting` on an event this client ignores, and cancelling typing on an
error notice was considered and rejected because `_warn_option` emits
`level: "error"` for non-terminal problems.

## What the chat-action channel can and cannot say

Checked rather than assumed, because it bounds the whole design. Telegram's
`sendChatAction` accepts exactly eleven values (confirmed against
python-telegram-bot's documented `ChatAction` constants; the core.telegram.org
page could not be retrieved — both fetches truncated before the method):

`typing`, `upload_photo`, `record_video`, `upload_video`, `record_voice`,
`upload_voice`, `upload_document`, `choose_sticker`, `find_location`,
`record_video_note`, `upload_video_note`.

**Every one of them describes the bot producing a specific kind of content.**
None means thinking, working, waiting, or stuck. The vocabulary is about
*output type*, not *state* — which is why no amount of fixing fault 1 can
answer fault 2. The channel is also ephemeral and unlabelled: the action clears
after about 5 seconds unless re-sent (hence the 4-second loop), carries no text,
and has no levels.

So the honest reading is that a chat action is a **liveness blink, not a
status**. It is the right tool for "something is happening right now" and the
wrong tool for everything this case actually wants.

One value is worth keeping in mind rather than using now: **`record_voice` is
the correct action when the reply itself will be a voice message**, which is
exactly what the deferred voice effort would produce. Using it to mean "the
agent is thinking" would misdescribe the bot to the user's own UI, and would
then collide with voice when it lands.

## Directions for fault 2 (not yet decided)

- **A status message, edited in place.** Send one message when the turn starts
  and `editMessageText` it as state changes — starting → working → a note when
  a turn has been quiet for a long time — then delete it or leave it above the
  reply. The only option here that can express *stuck*, because it can carry
  time. Costs one message per turn, which is real clutter on a phone; the
  falconfox UX notes already treat chat clutter as a first-order concern.
- **A reaction on the user's own prompt message.** Near-zero clutter, and it
  marks *which* prompt is in flight. The Bot API's reaction surface was not
  confirmed during this write-up (the docs fetch truncated) — establishing what
  is actually available is a task for this case, not an assumption.
- **`/status` in the work chat.** Pull rather than push. Cheap, and it composes
  with the others. `falconfox list` already reports `idle` / `working` /
  `stored`, but today only the focus chat can ask, and asking there means
  leaving the conversation.

**The daemon already knows more than the chat shows.** It emits
`session_updated` with `state: starting` while an ACP subprocess resumes, and
`_handle_event` returns early on anything that is not `agent_state` — so the
client discards the very distinction that makes a cold turn legible. Consuming
that event is likely the shared foundation under both faults, which is the
argument for treating them as one case rather than two.

**Stuck is the genuinely hard part.** Nothing today distinguishes a long turn
from a hung one; that needs time-since-last-event, which is new bookkeeping
either in the bot or in the daemon's session metadata. Decide where it belongs
before building it — the falconfox case's layer boundary says the daemon stays
opinion-light and clients hold the opinions, but "when did this session last
emit anything" is arguably daemon fact, not client opinion.
