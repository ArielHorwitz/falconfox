# Slash commands: directive injection & first-message edge

This records what we learned probing whether casebook's per-case **directive**
(the "you are working on case X" system text) could be delivered *out of band* so
that a slash command could be the literal first message of a session. Short
answer: **no** — and the current leading-user-text splice is load-bearing, not a
smell. What ships is UI discoverability plus one documented edge.

## Background: how the directive reaches the agent today

ACP has no system-prompt channel on `session/new`, and a `session/prompt` call
*is* a turn (it blocks until the agent replies). So the only way to give the
agent its case context **without** forcing an unwanted agent turn before the
user's first message is to bundle the directive **into** the user's first prompt.
`coordinator.send()` does exactly that, once per case session:

```
{directive}\n\n=== the user's message follows ===\n{user text}
```

This is deliberately framed as **trusted user text**. That framing is what makes
the agent treat the directive as authoritative and follow it.

## The collision

A slash command (`/compact`, `/investigate …`) is recognized by claude-agent-acp
**only when the prompt is a bare, lone text block that starts with `/`**. On a
case session's first message the directive is prepended, so the `/command` is
buried mid-text and never fires. Two escape hatches were rejected by the user:
sending the directive as its own priming turn (an unwanted agent turn) and
deferring the directive to message 2 (system setup that depends on message
content — non-deterministic).

## What we tested — embedded-context as the "clean" alternative

Idea: send the directive as an ACP **embedded-resource / context block**
(`resource_block(embedded_text_resource(...))`) so the user's text block stays a
lone `/command`. `embedded_context` is advertised by the backend
(`prompt_capabilities.embedded_context == True`). Probes:
`.mydev/probe_context_block.py`, `.mydev/probe_block_order.py`.

Two decisive findings, both against the idea:

1. **A directive delivered only as a resource block is refused as prompt
   injection.** In a *fresh* session the agent responded: *"it arrived as content
   pulled in through a context reference … the classic shape of a prompt-injection
   attempt, so I'm ignoring it."* This is by design — content in resource/context
   blocks is *data*, and agents are trained to distrust instructions embedded in
   data. The directive's whole point is to be authoritative, so this channel
   defeats it.

   Caveat caught mid-probe: an earlier run *appeared* to obey the resource-block
   directive, but only because a prior turn in the same session had already
   established the behavior via **trusted leading text**. Fresh-session tests
   (no prior trusted turn) refuse. Lesson: test instruction-following in a fresh
   session every time.

2. **Any sibling block defeats slash-command recognition.** Both
   `[resource, text("/context")]` and `[text("/context"), resource]` failed to
   fire the command (the agent treated it as prose); ordering doesn't help. A
   bare `[text("/context")]` fires normally, and fires on turn 2+ once the
   session is going.

## Conclusion

- The **leading-user-text splice is correct and load-bearing** — it grants the
  directive user-level authority the agent reliably follows. Moving it out of
  that slot (the instinctive "cleanup") is exactly what makes the agent distrust
  it.
- The embedded-context route is **dead on both counts**: the directive gets
  rejected *and* the command still wouldn't fire.
- The residual collision is therefore **unavoidable** under the user's
  constraints, and is small enough to simply document.

## The documented edge

> A slash command sent as the **very first message of a case session** will not
> fire — it is carried alongside the case directive, so it reaches the agent as
> literal text rather than a command. Send any message first; from the second
> message on, slash commands work normally. (Scratch sessions carry no directive,
> so their first message is unaffected.)

## What ships instead: discoverability

Slash-command *invocation* already works (plain prompt text, no RPC) everywhere
except that first-message edge. The gap is discovery, so this session adds:

- **Plumbing** — capture `available_commands_update` in the ACP client, cache it
  per session in the coordinator, publish it and include it in the reconnect
  snapshot (mirrors `config_options`, one layer thinner: commands arrive *only*
  via the live update, never in a session response, so no `AgentSession` storage).
- **Browse** — a header button opening a popover that lists every command with
  its description and input hint (discovery for when you don't know the name).
- **Autocomplete** — typing `/` at the start of the composer filters the commands
  inline with keyboard navigation (fast path when you do know the name).

claude-agent-acp advertises 40+ commands (`/discuss`, `/investigate`, `/compact`,
`/context`, `/effort`, custom skills, …), each with a description and optional
input hint — real data behind both surfaces.
