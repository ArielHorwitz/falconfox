# Overview

An agent that does work outside a turn the user started has no way to tell
the user about it. Opened 2026-09-15 from the hardening case
([2026-09-12__07b4eb32](../2026-09-12__07b4eb32/overview.md)), where it was
found the hard way.

## What happened

During the hardening case, the lead session ran network probes in the
background. Each time one finished, the session's harness woke it and it
wrote the user a report. Nothing had come from the user, so no turn was
open. The user's last two messages from that session were "I'll report when
the probe finishes" and "the DNS probe is still running and will report on
its own." Four reports, including the diagnosis itself, went nowhere, and
the session had no way to know.

This is not specific to probes. Any agent doing background work has it: a
long build, a scheduled check, a watch on a deploy, a subagent finishing.
Today the only channel out of a session is the reply to the user's own
message.

## What was established, 2026-09-14

**The daemon receives and records unprompted output.** The four lost
reports are in the session's transcript. The ACP bridge streams
`agent_message_chunk` updates whether or not a prompt is in flight, and
`engine/client.py` forwards every `session_update` as an event without
looking for a turn. So the loss is not upstream.

**The bot drops it.** `_handle_event` in `src/falconfox_telegram/bot.py`
routes agent text into the session's `Turn`, and with no turn there is
nothing to route into. Since the hardening case the events are dropped
outright; before it they accumulated in buffers nothing read. Either way,
nothing reaches the chat.

**Nothing marks where an unprompted burst ends.** The daemon's turn
boundaries (`turn_started`, `turn_ended`, the `agent_state` working and
idle events) come from the prompt round trip in `engine/session.py`, so an
unprompted burst runs with the session reported idle throughout. Whether
the bridge sends anything usable at the end of such a burst (a
`usage_update`, say) has not been measured. That measurement is the first
task of this case.

**How the bot splits a turn today.** There is no semantic marker. The rule
is structural, in `_close_block` and `_send_reply`: text that arrives
before a tool call is narration and is folded into the progress message;
whatever text remains after the last tool call is the reply; if nothing
remains, the last narration paragraph is promoted. It works because agents
narrate before they act and conclude after, not because anything says
"this is the answer."

## Decisions already taken by the user

- **Yes, an agent may speak unprompted.** It will ping the phone without
  the user having asked. That is wanted.
- **Implicit, not explicit.** Delivery must not depend on the agent
  remembering to run a command. An explicit channel would be forgotten,
  ignored or misused.

## The premise to revisit

The two-message turn (one progress message amended as work happens, one
reply sent whole at the end) was settled in the turn-feedback case
([2026-08-24__165f0606](../2026-08-24__165f0606/overview.md)) and
simplified in
([2026-09-12__c716ca46](../2026-09-12__c716ca46/overview.md)). Its
premise is right about what a reader wants: one evolving status line and
one thing to read. It is wrong about what that is tied to. It is tied to
the user's message, and it should be tied to a bounded stretch of activity.

The proposed unit is the **episode**: it starts when output begins,
prompted or not, and ends when the agent goes quiet. The user's message is
one way to start an episode; a background task finishing is another. Each
gets the same two-message treatment. The user's worry, that an agent which
works, backgrounds a task, goes idle, wakes, and repeats would litter the
chat, becomes several episodes, and that is mostly right: each wake is
news. Two rules would keep it clean:

- **A silent episode posts nothing.** An episode whose only text is
  narration, with no conclusion after the last tool call, produces no
  reply, so a no-news check leaves no message. Whether its narration
  should still update a progress message, or the progress message should
  be skipped too, is open.
- **An unprompted reply threads to the user's last message**, so it reads
  as a continuation of that conversation rather than a bolt from the blue.

## Open questions

1. **Detecting quiet.** A short silence with no tool call in flight is the
   honest signal that an episode has ended, and the threshold has to be
   measured per backend. First: reproduce an unprompted burst (a session
   with a background watch that fires once) and record the full event
   stream the daemon sees around it, with timestamps, to learn whether the
   bridge marks the end in any way and how long the gaps inside a burst
   are. Then decide whether the boundary belongs in the daemon (an
   episode is a daemon-level fact, like a turn, and other clients would
   want it) or in the bot.
2. **Where the progress message lives for an unprompted episode.** A
   prompted turn creates it on `send`. An unprompted one has to create it
   on the first chunk, which is also the moment the bot learns the episode
   exists.
3. **The interrupted-turn marker.** The daemon now persists an open turn
   so a restart can tell the agent its turn was cut off. An unprompted
   episode has no such marker. Whether it needs one is part of the daemon
   or bot decision above.
4. **Orientation.** Agents should be told plainly that anything they say
   is delivered, prompted or not, so they keep unprompted output to what
   the user should read. Today's orientation says nothing about it, and
   the lead session assumed its reports were being delivered.
5. **What the bot does with unprompted output until this lands.** Dropping
   is silent loss. Logging at WARNING with the session and the size of the
   dropped text would at least leave a trace, and is a one-line stopgap.

## Where the code is

- `src/falconfox_telegram/bot.py`: `_handle_event` (routing by turn),
  `_forward` (a turn starts on send), `_close_block` and `_send_reply`
  (the narration versus reply rule), `_finish_turn` (the boundary).
- `src/falconfox/engine/client.py`: `session_update`, which forwards every
  ACP update as an event.
- `src/falconfox/engine/session.py`: the prompt round trip that defines
  `turn_started`, `turn_ended` and the working and idle states.
- `src/falconfox/coordinator.py`: the open-turn marker and the
  interrupted-turn notice.
