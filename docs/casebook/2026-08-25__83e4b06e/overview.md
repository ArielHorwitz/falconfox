# Overview

Every bug in the Telegram client so far has been **silent**, and every one has
been diagnosed the same way: read `journalctl` for two units on the host and
reconstruct a timeline by hand from log lines written for other purposes. That
works, and it has found real bugs — but it is not available from a phone, which
is the only interface this project claims to need. The diagnosis loop is now
the slowest part of the work.

This case makes the session and turn lifecycle **observable**: from the logs,
and from the chat itself.

It blocks [2026-08-24__165f0606](../2026-08-24__165f0606/overview.md) (turn
feedback), which is paused until this lands.

## Why now: the evidence

Three reply losses in two days, none of which announced itself.

1. **A Telegram read timeout tore down the daemon websocket** and discarded an
   in-flight turn (finding 7 of the pivot case). The log blamed the daemon,
   which was healthy throughout.
2. **The resume's `idle` discarded the turn's reply** — every first turn after
   a daemon restart. No exception, no log line, no notice: every layer behaved,
   so nothing had an error to report. Found only by noticing a stray
   `all sessions idle` three seconds after a send.
3. **The first live test of the state work ended in an unexplained stop**
   (2026-08-25, ~06:24). The turn *completed* — `turn complete:
   session=0911a39b name=ff-state-test` at 06:24:53, 32 seconds after the send
   — but the user saw nothing further after a command failed. Whether the agent
   gave up or the reply was dropped **is not answerable from any log we
   currently write.** That is the case in one sentence.

Alongside it, two `daemon connection lost (sent 1011 (internal error) keepalive
ping timeout; no close frame received)` at 06:21:30 and 06:23:44, plus a
`ws disconnect: code=1006` at 06:23:22. The bot reconnects and reports the
interrupted turns, which is why the focus chat filled with "anything not
already sent is gone". **The keepalive timeouts are their own open question:**
they mean the daemon's event loop stalled long enough to miss a ping, and
nothing records what it was doing. Chase this first.

## The case proving itself (2026-08-25, 07:03)

Within the hour of opening this case, the exact failure it describes happened
again — and cost a message in each direction.

```
07:00:59  action=send session=f1be8056                      user's message
07:03:13  action=send session=f1be8056                      user's next message
07:03:13  notice[info]: agent is still responding; wait ...  REFUSED
07:03:49  turn complete: session=f1be8056                    reply finished
```

Three faults in one chain, none of which announced itself:

1. **The daemon refuses a prompt while a turn is running** (`engine/session.py`
   `_busy`) and says so with an **info** notice. The client only surfaces
   `level: "error"`, so the user's message vanished with no acknowledgement of
   any kind.
2. **Forwarding it anyway destroyed the reply in flight.** `_forward` resets
   `_reply_parts` and discards `_turn_working` before sending — so the second
   message wiped everything the first turn had streamed, and marked the running
   turn as one that had never begun.
3. **The pre-turn-idle guard then stranded the session.** With
   `_turn_working` cleared, the genuine `idle` at 07:03:49 was ignored, so
   `_turn_chat` was never cleared and the activity loop was never cancelled.
   The indicator kept sending `record_voice` **every four seconds for 54
   minutes** — the "occasionally recording a voice message" the user saw — and
   every later message would have been refused by the same stale entry.

Fault 3 was a regression from the fix for the resume-idle bug the day before:
a guard that says "a turn ends only if it ever began" strands the session
permanently if anything corrupts the flag it trusts. The repair adds the
missing safety catch — streamed output is proof a turn began, whatever the
flags say — and `_forward` now refuses a mid-turn message locally, telling the
chat rather than feeding it to a daemon that will drop it silently.

**The lesson is this case's thesis, stated by the system itself:** every one of
the three faults was individually defensible, none logged anything above
`INFO`, and the only visible symptom was a chat action firing every four
seconds. An hour of silence, two lost messages, and the diagnosis still
required reading the daemon log by hand.

## The shape of the problem

`idle` was misread as "turn over" when it means "not currently running a
prompt". That mistake was available because **the turn is not a first-class
thing anywhere**. It exists only as an implication of state transitions, so
neither side can log it, count it, or notice that one ended without delivering
anything.

The client is where the silence accumulates. It holds `_turn_chat`,
`_reply_parts`, `_activity_state`, `_last_flush` and `_turn_working` as five
parallel dicts keyed by session, mutated from several branches of one event
handler, and **logs none of them**. Every failure so far has been one of those
dicts being in a state nobody could see.

## Directions (not yet decided)

- **Name the turn.** Give it an id at `_forward` and carry it through: started,
  first chunk, each flush, each state change, ended, bytes delivered. A turn
  that ends having delivered nothing then becomes a loggable anomaly rather
  than something the user reports the next day.
- **Log the client's happy path at INFO**, not just its errors. The bot
  currently logs almost nothing when things go well, which is exactly why a
  happy path that silently delivers nothing looks identical to one that works.
- **A `/status` or `/debug` in the chat.** The wishlist already wants session
  state visible; this case wants the *turn* visible too — what the bot thinks
  is in flight, for which chat, with how much buffered. Diagnosis from the
  phone is the actual goal, not prettier logs on the host.
- **Make silence an error.** The recurring shape is a turn ending with an empty
  buffer and nobody noticing. That is detectable at the moment it happens.
- **Chase the keepalive stalls.** What blocks the daemon's event loop long
  enough to miss a ping? Session spawn and the naming backend are the suspects,
  both near in time to the two occurrences.

## Constraint

The daemon stays opinion-light — that boundary is the pivot case's central
claim and this case must not erode it. Turn *facts* (when it started, whether
it produced output) are daemon-side; turn *presentation* is the client's.
Logging is not an opinion, so most of this belongs on both sides independently
rather than as a new protocol between them.
