# Overview

The turn-feedback case
([2026-08-24__165f0606](../2026-08-24__165f0606/overview.md)) built three
answers to one question, and said so plainly at the time: "fault 2 (idle /
working / stuck) is answered three times over at three altitudes — the
five-state chat action, the live progress message, and the quiet-turn
warning." That was true, and it was not a problem while the answers were
being discovered.

It is a problem now. The progress message was the last of the three to arrive
and it turned out to subsume the other two. **This case removes the other
two**, plus the reactions on the user's own message, and moves the two things
they said that nothing else did into the progress message's header.

Reported from use, 2026-09-12: the reactions and the chat actions are "much
less important and actually more confusing or annoying than helpful."

## What the three were saying

Read as a set rather than one at a time, the overlap is total.

| | said | says now |
| --- | --- | --- |
| `✍` / `👌` / `😱` on the prompt | the turn is running, finished, failed | the progress message's stamp, with elapsed and tool counts |
| `typing` / `find_location` / `record_voice` / `upload_document` | thinking, streaming, running a tool | a narration line, a `💭` preview, a `⚙️` tool line |
| the quiet notice, once after 3 min | nothing has been heard for N minutes | a clock in the header, climbing over narration that stopped |

The chat action is the clearest case. It named the activity state because it
was once the only thing that could, and the vocabulary was always a stretch:
Telegram gives a bot five verbs about sending files and voice notes, none of
which is "thinking". A bot "recording voice" describes nothing a reader can
act on, while `💭` and the first 280 characters of the thought describe it
exactly. The mime was left miming a scene the subtitles already carry.

What does *not* survive the collapse is the one thing an edit cannot do:
**notify**. An edited progress message never pings, and a topic where nothing
new arrives for four minutes is indistinguishable from a dead one. So one
chat action stays, `typing`, for the whole turn — it says "alive", which is
the only bit the progress message structurally cannot carry.

## Two things the reactions were carrying alone

Worth naming, because "the progress message says all of it" is *nearly* true
and the exceptions are where a removal like this goes wrong.

**A queued message, second and after.** `QUEUED_FIRST` is said once per turn,
deliberately: the two ways out (`/stop`, `/unqueue`) need explaining once, and
saying them three times is the clutter the queue exists to avoid. The `👀` on
each message covered the rest. Removing it silently would mean the third
message you fire off mid-turn gets no acknowledgement at all. It is now a
count in the header, `📥 2 queued`, which is better than the mark was: a mark
per message says "kept" three times, a count says how deep the queue is.

**A file waiting in the tray.** Also `👀`, and this one needed nothing: every
file already gets its own threaded receipt naming it and its id
("🗂 Filed photo.jpg as f0. It goes out with your next message"), and `/tray`
answers what is still waiting. The reaction was the only *standing* signal,
and the standing question is what `/tray` is for.

Everything else the reactions marked — received, running, done, cancelled,
failed, dropped — was already said in words somewhere: the stamp on the
progress message, `🗑 Dropped 2 queued message(s)`, `🗑 Removed 1 file(s)`,
the silent-turn notice.

## The clock is paced apart from the tick that carries content

This is the one part of the change that is not a matter of taste, and it is
easy to get wrong by writing the obvious thing.

The progress message is edited only when there is new narration
(`_progress_dirty`). A clock in the header breaks that: the header changes
with no new content, so every tick it reads differently is an edit, and the
activity loop ticks every 4 seconds. A clock showing exact seconds would
therefore spend an edit every tick, for the whole length of every turn,
whether or not anything was happening.

So the clock is paced on its own: `PROGRESS_CLOCK_SECONDS` (15), applied only
to a tick whose *sole* change is the clock. Four refreshes a minute, which
reads as live. Narration is never throttled — anything with something to show
marks itself dirty and rides the next 4-second tick — and neither is the queue
depth, which is the receipt for something the user just did.

**What this is not is a calculated fit to a documented budget**, and the first
draft of this case claimed otherwise. Telegram publishes three rate limits
(one message per second per chat, 20 a minute to the same group, about 30 a
second overall) and every one of them is about *sending*. Neither the FAQ nor
the API reference says whether `editMessageText` counts against them, whether
`sendChatAction` does, or whether a forum's topics share one group allowance.
Checked at the source, 2026-09-12: those questions are simply not answered.

An earlier version of this design coarsened the clock to whole minutes past
the first, justified by that 20-a-minute figure. The figure was an inference
presented as a constraint. The real position is that the true budget is
unknown, four edits a minute is modest under any plausible answer, and the
cadence was chosen on how it reads.

The honest way to close the question is to measure rather than reason: a real
limit arrives as a 429, `api.py` has no 429 handling at all today, and a
failed progress edit is logged at `debug`. So the bot would sit at a limit
without ever saying so. Parsing `retry_after` and logging it loudly is the
open follow-up; a week of logs checked on 2026-09-12 held exactly one 429, on
`getUpdates`, none on edits.

Two smaller consequences fell out of the same reasoning:

- `_update_progress` used to refuse to edit when a turn had narrated nothing.
  That was right when the header was a constant and wrong the moment it
  carries a clock — the empty turn is exactly the one worth timing. It now
  refuses only to *create* a message it has nothing to put in.
- `_set_activity` no longer sends a chat action on a state change. It sent one
  because a change meant a *different* action; with one action there is
  nothing new to send, and the 4-second loop is the only thing that has to
  keep running. That takes the call off the event pipeline entirely, which is
  where it had been trying to get to: a hung send there once delayed a
  finished reply by 45 seconds (observed 2026-08-25, 09:06), and the fix at
  the time was to detach it into its own task. Now it is simply not there.

`sendChatAction` was never the expensive call. The loop that carries it is,
because that loop also edits.

## Also closed by this

A buglist entry, ["Chat actions lag the session's
state"](../../buglist.md) (reported 2026-09-08, never investigated): the
actions appeared to trail the state they reported. With one action for the
whole turn there is no state for it to trail. The entry is deleted rather
than answered — nobody ever found the cause, and the lag is now unobservable.

The unexplained ["extra 'Working...' message appears after the
reply"](../../buglist.md) is *not* fixed here and stays on the list.

## Where this stands

Built 2026-09-12, suite green at 258 tests. **Open until it has been used**,
which is the standard the case it supersedes set for itself: the 2026-08-24
case closed on a couple of days of phone use, not on a green suite, and the
things it got wrong were the desk guesses it named as desk guesses.

Two of those guesses are gone with this change (`TURN_ACTIONS` and
`QUIET_TURN_SECONDS` were both listed there as unproven). The new one is
`PROGRESS_CLOCK_SECONDS` (15), which is a one-line change if four refreshes a
minute turns out to read as stalled or as churn.

The open follow-up, filed rather than built: 429 handling in `api.py`, so the
budget question stops being a matter of reasoning.
