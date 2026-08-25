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
