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

## Handoff (2026-08-25, 08:40 UTC)

Picked up next by a session on the **fable backend**, working alone in
`~/falconfox`. State at handoff:

- Repo at `7f43045`; `origin/master`, `origin/falconfox` and the working tree
  all agree, tree clean. The deployed code **is** the committed code.
- `falconfox-daemon` and `falconfox-telegram` both running; no timers pending;
  no leftover test sessions.
- Restart announcements shipped (see the section below) and verified live on
  the 08:34 bot restart.

**One change lives outside git:** `[backends.fable]` in
`~/.config/falconfox/config.toml`, carrying
`ANTHROPIC_MODEL=claude-fable-5`. It is the same shape of hazard as the Python
3.12 shim in [bugs.md](../../bugs.md) — a host-side edit with no commit behind
it, which a rebuilt host silently loses. Mitigated by documenting it as a
commented example in `deploy/config.example.toml`, so the recipe survives even
though the value does not.

**Start with the keepalive stalls.** Everything else in this case is known work
with a known shape; that one is a live unknown, and unknowns are what this case
exists to make visible.

**Environment facts that are not guessable**, and cost a session real time to
rediscover:

- You run inside the system you are changing. Restarting the daemon kills your
  own turn, so changes go out as a **detached** restart — procedure in the
  pivot case [2026-07-24__d53a46fd](../2026-07-24__d53a46fd/overview.md), worth
  reading before the first deploy rather than after.
- `~/falconfox` is both your working tree and the deploy checkout. **Commit and
  push before triggering an update** — `update.sh` refuses a dirty tree.
- A message sent while your turn is running is refused and the sender is told
  so. To take input, end the turn.
- A bot-only restart (`systemctl --user restart falconfox-telegram`) picks up
  client changes without killing your turn — much cheaper than a full deploy
  when the change is confined to the Telegram client.

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
already sent is gone". ~~They mean the daemon's event loop stalled long enough
to miss a ping~~ — **investigated 2026-08-25, and that framing was wrong**: the
aligned logs show *both* processes silent in overlapping windows, next to a
session spawn, on a 1-vCPU/951MB host that sits 1.1GB into swap — pointing at
host-wide memory thrash, not the daemon's loop specifically. **Resolved the
same day:** a fresh occurrence at 10:08:33 with the new watchdog live ruled
out both loop stall and host freeze — the cause was the bot's event pipeline
wedging on hung Telegram API calls, which backpressured the websocket until
both sides' keepalives starved. Fixed in `0de482f`. Full reconstruction,
mechanism and the watchdog's one blind spot:
[keepalive-stalls-finding.md](keepalive-stalls-finding.md).

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

## Shipped: the daemon announces its own restarts (2026-08-25)

The first observability fix, and the one the user asked for directly: restarts
were **completely opaque from the phone**. The bot reconnected in silence, and
unless a turn happened to be in flight — the only path that said anything —
nothing was ever reported. Since self-updating from inside a session makes
restarts routine, the most common event in this system was also its least
visible.

The bot now announces both edges to the work chat: a lost daemon connection
when it happens, and `FalconFox is up (<revision>)` on every connection. Two
decisions worth keeping:

- **Announced on every connection, not only on a reconnect.** A deploy restarts
  the bot too, so the process that watched the daemon go down is rarely the one
  that sees it return — pairing the messages in memory would have produced
  silence in exactly the case that matters. A bare "up" after a bot-only
  restart is worth saying anyway, since it reports that restart.
- **The revision comes from `GET /api/version`, not from importing
  `falconfox`.** The bot is a client of the daemon; the number it prints should
  be the daemon's own, and asking over the API keeps the layer boundary intact.

`_announce` swallows every exception. It is called from the reconnect path, so
an announcement that raised would turn a blip into an outage — and the version
lookup is a nicety that must not be able to swallow the announcement itself.

## Shipped: the turn is a first-class fact, and silence is an error (2026-08-25)

The round that answers the case's central sentence. What landed, in one deploy:

- **The daemon names the turn.** `engine/session.py` gives every prompt turn an
  id and emits `turn_started` / `turn_ended` events — duration, message chunks,
  output chars, thought chunks, tool calls, outcome, and the ACP stop reason.
  `turn_ended` is emitted *before* the trailing `idle`, because the end of a
  turn is a fact with contents while idle is a state a session can be in for
  other reasons — conflating them is how replies got dropped. These are turn
  *facts*; presentation stays in the clients, per the constraint below.
- **The coordinator logs turns from the events**, replacing the working→idle
  inference. A turn that completes with zero output chars is logged WARNING
  (`turn produced NO output`) at the moment it happens.
- **The bot finalizes turns on `turn_ended`**, not on inferred idle — the
  pre-turn-idle guard and the streamed-output safety catch remain only as a
  backstop for a daemon that never sent one. `_finish_turn` is idempotent, so
  the idle that follows finds nothing to do.
- **Silence reaches the chat.** A turn that ends with nothing delivered (and
  not errored or cancelled — those already speak) sends `⚠️ The turn ended
  without delivering a reply (…)`, distinguishing "the agent produced no
  output; stop reason: X" from "the agent wrote N characters that were lost on
  the way to this chat" — the resume-idle bug's exact shape, now audible. The
  unexplained 06:24 stop in the evidence above would have printed one of these
  two sentences.
- **The bot logs its happy path at INFO**: forward, refusal, each flush
  (partial/final, chars), turn end with delivered totals — the five parallel
  dicts stop being invisible.
- **`/status` in either chat**: daemon version, session list with states,
  focused session, and the bot's own view of in-flight turns (chat, activity
  state, buffered vs delivered chars, age). Diagnosis from the phone.
- **HTTP API actions are logged** (`action=… via=http`). The focus agent drives
  the daemon over the CLI→HTTP path, which previously left no trace at all —
  its spawns/sends/deletes appeared only as side effects.
- **The stall watchdog** in both processes (see
  [keepalive-stalls-finding.md](keepalive-stalls-finding.md)).

## First live catch (2026-08-25, 09:06 — within the hour of deploying)

The very first turn after the observability deploy told a story the old logs
could not have told, end to end from the journal alone:

- `turn start … turn=954ff824 prompt_chars=55` → `turn complete … outcome=error
  … duration=8.27s … chars=2043` in the daemon, and the cause sitting next to
  it as a notice: **the backend hit its session usage limit** ("resets 9:50am
  UTC"). The chat saw the error notice and the 2043 streamed chars were still
  delivered. Under the old logs this was "the bot went quiet".
- The bot's timestamps exposed a second, older defect: `turn started` at
  09:06:13, then **nothing until a burst at 09:06:58** — the exact 40-second
  signature of a Telegram read timeout. A hung `sendChatAction`, awaited
  inline in the event handler, had head-of-line-blocked every queued daemon
  event and delayed the finished reply by 45 seconds. **Fixed the same hour:**
  the indicator send is now a detached task — it is droppable decoration, and
  the pipeline is not allowed to wait for it. Content sends stay inline,
  because reply ordering is a correctness property.

Both diagnoses took minutes, from timestamps in ordinary INFO lines. That is
the case doing what it was opened to do.

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

## Directions (all five taken — see the shipped section above)

One deliberate deviation: the turn id is minted in the **daemon** at
`engine/session.py`'s `send`, not at the client's `_forward` as sketched below.
A turn is a daemon fact; the client carries the daemon's id rather than
inventing a parallel one.

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
