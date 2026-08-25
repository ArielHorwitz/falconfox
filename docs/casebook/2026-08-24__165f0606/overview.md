# Overview

**UNPAUSED 2026-08-25 — the blocker landed and closed the same day.** The
observability case ([2026-08-25__83e4b06e](../2026-08-25__83e4b06e/overview.md))
shipped what this case was waiting for, and it changes this case's ground:
turns are now first-class daemon facts (`turn_started`/`turn_ended` events
with id, duration, output stats, outcome, stop reason — `turn_ended` arrives
*before* the trailing idle), the bot finalizes turns on those events instead
of inferring from `idle`, a turn that delivers nothing already tells the chat,
`/status` exists in both chats, and delivery is accounted per turn
(`buffered`/`delivered`). The unexplained stop that paused this case is the
kind of thing the logs now answer in one line. Build the presentation layer on
the turn events, not on state transitions. Related items worth picking up
together, both in [wishlist.md](../../wishlist.md): persisting the turn→chat
map across bot restarts, and the original "tell the user what a session is
actually doing" entry. *(Both picked up the same day — see "no turn left
behind" below; their wishlist entries are gone.)*

*(Original pause note, kept for history: the first live test of the state code
ended in a failure nobody could explain from the outside, and diagnosis meant
reading daemon logs and reconstructing timelines by hand — that bottleneck,
not this case's remaining work, was the blocker.)*


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

**Do not undo two earlier decisions** (both from the first phone session; the
first is also commented at its site in `bot.py`, the second lives only here now
that the fixed bug has left bugs.md): typing starts in `_forward()` rather than on
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

## Built: the fix and the state code (2026-08-24)

Fault 1 is fixed and the granular indicator is in, together, because the
second is worthless on a loop that dies.

`_typing_loop`/`_start_typing` became `_activity_loop`/`_start_activity`/
`_set_activity`. The loop now swallows `ApiError` around the send, and the
restart guard tests `task.done()` rather than mere presence in the dict — the
two halves of the bug. Two regression tests cover exactly those: a chat action
that raises must not end the loop, and a loop that has died must be revived by
the next state change.

`TURN_ACTIONS` in `bot.py` is the whole mapping, one line per state, meant to
be reshuffled once we have watched it. Five states are distinguished, each with
its own action, from events the client previously discarded: `starting`
(`session_updated`), `working`, `thinking` (thought chunks), `streaming`
(message chunks), `tool` (tool calls, as a state signal only — they stay
suppressed as content). An action is sent on *state change*, not per event, so
a chunk-by-chunk stream costs one call rather than hundreds; the 4-second loop
refreshes whatever the current state is.

**The mapping is provisional by decision, not by neglect** — the point of this
pass is to see the states become visible at all. Which glyph means what comes
after watching it.

## Built: flushing the reply when the stream stops (2026-08-25)

The payoff step. `_flush_reply` sends what has accumulated the moment the state
leaves `streaming` — the agent has gone to a tool, so output has stopped while
the turn has not. The remainder follows at `idle` as usual, and nothing is sent
twice: the buffer is emptied rather than dropped, because the rest of the reply
belongs to the same turn.

A partial flush has to earn its message, so three guards, all in
`_flush_reply`:

- **At least `MIN_FLUSH_CHARS` (240).** Below a paragraph it is noise.
- **Not inside a code block.** An odd number of fences means the stream stopped
  mid-block. The renderer tolerates an unclosed fence — `_split_blocks` handles
  it — but the reader would get half a block and an unfenced remainder, so the
  flush waits for the next opportunity instead.
- **`MIN_FLUSH_SECONDS` (15) between partial flushes.** A tool-heavy turn is
  where this design is most wanted and most likely to hit Telegram's per-chat
  message limit.

The final flush at `idle` ignores all three: the turn is over, so whatever is
left is sent regardless of length, fences, or timing.

Follow-on: `INTERRUPTED_TURN` now says "anything not already sent is gone"
rather than "that reply is gone", which is the honest wording once part of a
turn may already have been delivered. This is the blast-radius reduction the
design predicted — text already handed over cannot be lost with the
connection.

**Still unverified in use.** Both this and the state code are deployed but have
only been exercised by tests; whether the flush fires at useful moments, and
whether the guards are tuned anywhere near right, needs real turns.

## The real bug behind "no reply in Telegram" (2026-08-25)

Reported as a missing reply and initially mistaken — by me — for a stale
message from the pre-restart build. It was neither. **Every first turn after a
daemon restart silently lost its reply**, and had done since before any of this
case's work.

Evidence, in order:

- The daemon's own log for the lost turn: `action=send` at 05:28:57, then
  **`all sessions idle` at 05:29:00**, then `transcript_reset`, then
  `dogfood (working)` at 05:29:01, then `turn complete` at 05:29:49.
- `falconfox read` showed the reply had been generated in full, so it died
  between the bot and Telegram.
- A direct `sendMessage` to the work chat returned `ok:true`, so delivery
  worked and the fault was in the bot.

Cause: sending to a **stored** session resumes it, and `engine/session.py`
sets `idle` once the ACP subprocess is up — *before the prompt runs*
(`_set_state("idle")` at lines 145, 170, 177; `working` only at 202). The bot
treated any `idle` as the end of the turn, so it popped `_turn_chat`, cancelled
the indicator and cleared the buffer before a single chunk had arrived. The
real reply then streamed into a session with nowhere to send it, and the final
`idle` found no chat. No exception, no log line, no notice — the quietest
possible failure.

Fix: a turn ends only if it ever began. `_turn_working` records that a session
has reported `working` since `_forward`; an `idle` for a session with an open
turn that never did is ignored as the resume artefact it is.

**Not caused by the flush work** — the previous code had the same structure and
the same hole. What the flush work did was change the symptom: with output
delivered mid-turn, some of a lost turn would now have survived. The reason it
looked new is that it only fires on the first turn after a restart, and this
case has caused a lot of restarts.

Two lessons worth carrying:

- **`idle` is not an event, it is a state that can be entered for more than one
  reason.** The client read it as "turn over" when it means "not currently
  running a prompt" — true during a resume, before anything has been asked.
- The failure was silent because every layer behaved: the daemon emitted a true
  state, the bot took a defensible branch, Telegram was never called. Nothing
  had an error to report. Silence is the failure mode this client keeps
  producing, and the third time it has cost a reply.

## Built: no turn left behind, and quiet turns say so (2026-08-25)

The round the unpause note asked for, both wishlist items picked up together
(their entries are deleted from wishlist.md per its policy; the context lives
here now).

**The turn map is persisted and reconciled.** `_persist_turns` writes
`{session_id: {chat, turn_id, consumed, delivered, started}}` to `turns.json`
beside the pointer file on every forward, flush and turn end — and the file
deliberately survives both a bot restart (SIGTERM runs no cleanup) and a
connection reset. On every connect, `_reconcile_persisted_turns` settles the
map against the daemon, with three outcomes:

- **Session still `working`/`starting` → adopt.** The new process takes the
  turn as its own: indicator resumes, `turn_ended` finalizes normally.
- **Turn ended while the bot was away → recover.** The undelivered remainder
  is rebuilt from the session transcript and delivered, behind a one-line
  "♻️ recovered" note. If nothing was ever produced, the silent-turn report
  fires instead of nothing firing at all.
- **Session gone → the only true loss**, and the only case that says so.

The recovery arithmetic rides on a new `consumed` counter — raw stream
characters removed from the buffer by flushes, unlike `delivered` which counts
stripped characters actually sent. The transcript stores the same chunk events
the websocket streams, so `turn_text[consumed:]` is exactly what the chat has
not seen. An adopted turn's buffer is missing whatever streamed while the bot
was away, so partial flushes are suppressed for it and the whole reply is
rebuilt from the settled transcript at `turn_ended` — end-state arithmetic on
a settled transcript, rather than trying to splice a live stream around a gap.

Two consequences worth naming:

- **`INTERRUPTED_TURN` is retired.** "Anything not already sent is gone" was
  usually false — the daemon keeps every chunk — and during the keepalive
  stalls it filled the chat with copies of itself. A dropped connection now
  keeps quiet about turns (the daemon-down announcement still fires); the next
  connection either recovers them or reports the truth.
- **Focus-chat turns are dropped on reconcile, silently.** The focus session
  is ephemeral and rotated on every connect; reviving its session-management
  chatter would be noise.

**Quiet turns are the "stuck" answer.** `_last_event_at` records every event
per session; the activity loop (already ticking every 4 s) checks it, and a
turn quiet for `QUIET_TURN_SECONDS` (180) gets one message per spell: how long
the silence has lasted and what the last activity was, with the explicit
caveat that a long tool call and a stuck turn look identical from outside —
the bot states the observable fact and leaves the judgement to the reader. An
event ends the spell, so the next long silence is its own news. `/status` now
carries `quiet=<n>s` per in-flight turn.

**Still unverified in use**, like the state code before it: the adoption path,
the recovery path and the 180-second threshold all need real restarts and real
long turns. The first live restart mid-turn is the test that matters.

## Reported from use: the reply is a run-on of narration (2026-08-25)

First live feedback on the flush design, from the phone, with a sample:

> Now the new recovery test class, inserted before RenderingTests:Add the
> quiet field to the /status test, then run the suite:All 44 tests pass, …

Diagnosis: those are three separate remarks the agent made *between tool
calls* — narration written to introduce actions the chat deliberately does not
show. Two compounding causes:

1. **Message boundaries are lost.** The client concatenates every agent chunk
   of the turn into one buffer; where one remark ends and the next begins
   (i.e. where a tool call sat between them), no separator is inserted — hence
   the missing newlines.
2. **The narration's referents are invisible.** Each remark introduces a tool
   call ("inserted before RenderingTests:") that is suppressed as content, so
   the text dangles.

**The early flush is not the culprit** — held to the end of the turn, the
final message would contain the same glued text. The real shape: the stream
carries two kinds of agent text, *working narration* and *the actual answer*,
and the chat renders their concatenation minus the context that makes
narration legible.

Option space, discussed with the user (decision pending):

- **A. Separators only** — paragraph break where a tool call interrupted the
  text. Minimum fix; needed under every option below except E.
- **B. Compact transcript** — A, plus one-line tool markers inline in the
  same batched messages ("⚙️ edit tests/…"), repeats collapsed. Message count
  unchanged; every message becomes self-explanatory. A bounded reversal of
  "tool calls stay suppressed": they appear as inline lines, not messages.
- **C. Chat-native updates** — each narration block is its own small message
  at the moment it happens. Most like texting a colleague; a tool-heavy turn
  is ~15 messages and ~15 notification pings.
- **D. One live progress message per turn, edited in place** (narration +
  recent tool titles), with the turn's reply carrying only the final answer.
  Cleanest chat; the most engineering; no pings for progress.
- **E. Suppress narration; deliver only the final answer.** Quietest. Riskier
  than it sounds: "final answer" is structurally "text after the last tool
  call", and a turn ending in a trivial tool call would misfile its real
  answer as narration. The old blast-radius argument for flushing is weaker
  now (transcript recovery exists), which makes E viable at all.

## Design direction (user, 2026-08-24)

Three proposals, recorded before they are built. The first two are decisions to
take; the third changes what the case is really about.

### Two states out of a one-state channel

Spend a second chat action to double the alphabet: **`record_voice` while output
tokens are streaming**, **`typing` while the agent is working but not
producing** — waiting on the backend, running a tool, thinking.

This is the right instinct for a channel with no state vocabulary: if the only
lever is *which* content-type the bot claims to be producing, then use two of
them and let the difference carry the meaning. It buys the single most valuable
distinction — *alive and producing* versus *alive and busy* — without any new
message traffic.

Two things to settle before building it:

- **Which way round?** The proposal maps `typing` to the non-producing state,
  which inverts the intuitive reading (typing = making text). The argument for
  the proposal is that `typing` is the unremarkable default, so the *distinctive*
  "recording voice message…" fires precisely when something notable is
  happening — and a failed or lapsed indicator degrades to the generic one.
  The argument against is that a reader who has not been told the mapping will
  guess the opposite. Both defensible; pick deliberately rather than by
  accident.
- **`record_voice` collides with actual voice.** It is the honest action when
  the reply itself will be a voice message, which is exactly what the deferred
  voice effort produces. Spending it here is a real cost, not a free reuse.
  Mitigations if voice lands: move audio to `upload_voice` (recording the
  reply vs. sending it), or move the streaming indicator to one of the other
  unused actions — `choose_sticker`, `find_location`, `upload_document`. None
  is more honest than the others; that is the nature of the channel.

### Flush the reply when the stream stops

Today the bot accumulates `_reply_parts` and sends one message per turn on
`agent_state: idle`. The proposal: when the output stream stops while the turn
continues — the agent has moved to a tool call or other non-output work — push
what has accumulated *immediately*, because with a trustworthy state indicator
it is now unambiguous that the agent is still working rather than finished.

This is the strongest of the three, and it reframes the case. The other two
improve a *signal about* progress; this delivers the progress itself. A long
turn stops feeling dead because real content arrives during it, which is a
better answer to "is it stuck" than any indicator can be.

**The daemon already emits everything this needs** — checked, not assumed.
`engine/client.py` normalises ACP session updates into a richer event stream
than the bot consumes:

- `message` with `role: "agent"` — streamed `agent_message_chunk`s. Consumed.
- `message` with `role: "thought"` — `agent_thought_chunk`s. **Discarded.**
- `tool_call` with `tool_call_id`, `title`, `tool_kind`, `status` — from
  `tool_call` and `tool_call_update`. **Discarded.**
- `plan`, `commands`, `usage`. **Discarded.**

`_handle_event` returns early on anything that is not `message`, an error
`notice`, or `agent_state`, so all of it is thrown away today. A `tool_call`
event arriving while `_reply_parts` is non-empty **is** the "stream stopped,
turn continues" signal — precise, already delivered, no daemon change required.
Thought chunks give a third distinguishable state for free, and `title` would
allow saying *what* the agent is doing.

Constraints this must respect:

- **Suppressing tool calls as content stays.** The falconfox case deliberately
  chose one message per turn with tool calls hidden, to keep a phone chat
  readable. Using `tool_call` as a *state signal* is not a reversal of that;
  rendering tool calls as messages would be. Keep the distinction.
- **Chat clutter is a first-order concern**, per the same UX notes. A
  tool-heavy turn could flush many times; debounce, and consider a minimum
  accumulated length before flushing.
- **Telegram rate-limits messages per chat** far more tightly than chat
  actions. A chatty turn is the case where this design is most wanted and most
  likely to hit limits.

Unlooked-for benefit worth stating: incremental flush shrinks the blast radius
of the failure that motivated finding 7 of the phone session, where a dropped
connection silently discarded a whole turn's accumulated reply. Text already
delivered cannot be lost.
