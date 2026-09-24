# Overview

**Status: CLOSED (2026-09-24).** Opened and built 2026-09-12 against `dev` at
6450b8c, merged the same day as 2fa389d and 9b43889, and in continuous use on
the dev instance since. Closed on use rather than on tests, which is the
standard the case it supersedes set: twelve days of phone use, no complaint
against any of the three removals, and the one desk guess it named
(`PROGRESS_CLOCK_SECONDS`) never touched. See ["Where this
stands"](#where-this-stands-closed-2026-09-24).

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

## What dogfooding found the same day

Within an hour of going live: **"typing…" stopped appearing.** Reported from
the phone as intermittent, with the progress message arriving in a
48-second batch.

It was a regression, and not where it looked. The indicator loop had always
carried two jobs — send the chat action, then edit the progress message — and
a Telegram call from this host intermittently hangs until its 40-second read
timeout. While the edit hung, the action queued behind it in the same loop
never went out. That stall predates this case by weeks. What this case removed
was the thing hiding it: `_set_activity` used to fire a chat action as its own
detached task on every state change, which kept the indicator alive across a
stalled loop, at the cost of an API call per event.

So the collapse from three signals to one was right, and it left the one
remaining signal riding a loop that could stall. The two jobs are now two
tasks, neither able to stop the other; the chat action gets an 8-second
timeout, since one that has been in flight longer than the ~5 seconds it
survives is worthless; and `_json_request` logs any call over 10 seconds at
WARNING, because the whole reason this took an afternoon is that a 40-second
stall was invisible in the logs.

**How it was found** is worth keeping, because none of the obvious suspects
were it. The host has one CPU and the turn that showed the symptom was running
the test suite in a loop, which looked conclusive — but the bot's run-queue
wait across that stretch measured 0ms, and the first measurement that said
otherwise had been taken against the *stable* bot's pid rather than the dev
one. `sendChatAction` with the exact live chat and thread returned `ok:true`
by hand. What settled it was counting the bot's sockets: `urllib` opens a
fresh connection per call, so 62 seconds of a live turn should show fifteen
short-lived connections and showed about six, two of them held for ~30s.

## The buglist entry this reopens

["Chat actions lag the session's state"](../../buglist.md), reported
2026-09-08 and never investigated, was this same fault seen from the phone:
the action trailed the state because the loop that sent it was periodically
stuck. An earlier draft of this case deleted that entry, on the reasoning that
one action for the whole turn leaves no state to lag. That reasoning was
wrong — the lag was never about which action was being sent.

It went back on the list, rewritten around what was known at the time:
Telegram calls from this host hang until the read timeout, cause unknown. The
symptom was contained, not explained.

**It has since been explained, and the explanation came out of this case's one
line of logging.** The `slow request` WARNING added here is what turned a
40-second stall from something a reader had to notice on a phone into something
the journal counts, and two days later the hardening case measured it: a TCP
connection attempt from this host to `api.telegram.org` over IPv6 goes
unanswered about one time in ten, and the client applied a single 40-second
timeout to connecting and reading alike. The buglist entry is now ["Telegram's
IPv6 front end ignores about one connection attempt in
ten"](../../buglist.md), and the containment there is to try IPv4 first and cap
each connection attempt at three seconds.

The numbers, from the journal, are the clearest thing this case produced:

| | `slow request` lines |
| --- | --- |
| 12 Sep (logging added, IPv6 still preferred) | 936 |
| 13 Sep | 2557 |
| 14 Sep (IPv4-first lands) | 2199 |
| 17–24 Sep | 1 to 4 a day |

Two thousand a day was the rate at which this deployment had been silently
losing forty seconds at a time, before and during this case, with nothing
anywhere saying so.

The ["extra 'Working...' message appears after the reply"](../../buglist.md)
entry, left open here, was also closed by the hardening case: `_finish_turn`
tore a turn down by session key across three awaits, so a message arriving in
that window had the id of its own progress message erased under it. Turns are
torn down by identity now.

## Where this stands (closed 2026-09-24)

Built 2026-09-12, dogfooded the same day (see above), suite green at 261 tests
on merge. Twelve days of use later, all of it still standing:

- **One chat action, `typing`, for the whole turn.** No complaint since, and
  the buglist entry about actions lagging the state is gone for a reason that
  turned out to be the network rather than the mapping.
- **No reactions.** The two things they carried alone are carried where this
  case put them: the queue depth in the header, the tray by its own threaded
  receipt and `/tray`.
- **The clock instead of the quiet notice.** `PROGRESS_CLOCK_SECONDS` was the
  one desk guess this case named as unproven, and it has not been touched.
  Four refreshes a minute reads as live.

The code has moved since, and survived the move. The hardening case
([2026-09-12__07b4eb32](../2026-09-12__07b4eb32/overview.md)) opened against
9b43889, this case's last commit, and rebuilt per-turn state as a single `Turn`
record — so the two loops are `turn.activity_task` and `turn.progress_task`,
the header's clock reads off `turn.started_at`, and the stale-tick guards ask
whether a tick's turn is still the session's current turn rather than whether
the session is mid-turn at all. One behaviour of this case's was refined there:
the header always carries a clock now, where `_progress_header` used to omit
one for a turn with no recorded start.

This case also answers a wishlist entry it never referenced, and they are the
same want a day apart: "Remove the reactions mechanism" (from the phone,
2026-09-11) recorded that the mechanism should go, said no reason had been
given, and asked that whoever picked it up get one first. The request that
opened this case, the next day, is that reason — the markers are "more
confusing or annoying than helpful" beside a progress message that says the
same things in words. That entry is deleted here, per the wishlist's own rule
that a picked-up item goes.

One follow-up stays filed rather than built: ["Notice when Telegram
rate-limits us"](../../wishlist.md). Half of it landed here as the slow-request
WARNING; the 429 itself is still unhandled, so `retry_after` is still
unparsed and the edit budget this case declined to guess at is still unmeasured.
Nothing has hit it — one 429 in the week before this case, on `getUpdates` —
which is why it is a wish and not a bug.
