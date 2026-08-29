# Capping live sessions

*User's policy, 2026-08-29: a configured maximum of active sessions
(defaulting to 5); when one more activates — by spawn or by resume — it waits
until the oldest closes, which happens as soon as that one goes idle.*

## Where it belongs

The **daemon**, not the Telegram client. `spawn_session` and `resume_session`
are the only two doors into `live=True`, which makes them the chokepoint, and
`send` already resumes a stored session implicitly. Putting the cap in the bot
would let `falconfox spawn` from the CLI or the web UI walk straight past it,
and memory is a daemon-wide resource rather than a Telegram concern.

Configuration is daemon-global (`config.toml`), matching the project's
existing position that there are no per-project overrides:

```toml
max_active_sessions = 5
```

## What falls out for free

The Telegram side needs **nothing new** for the eviction itself. Stopping a
session emits `session_updated` with `state="stored"`, which the forum client
already mirrors by closing that session's topic; resuming reopens it. The
record stays, the user cannot prompt a session that is not running, and the
transcript survives. That was measured into this case for an unrelated reason
and now carries the capacity policy too.

## The one thing that cannot work as stated

"It waits" cannot mean *the caller blocks*. `send` is an HTTP request, and the
Telegram client's HTTP timeout is 40 seconds; a turn can run for many minutes.
A 6th session waiting for a slot inside the request would simply time out, and
the client would report a failure that had not happened.

So the wait has to be **asynchronous**: the call returns promptly having
*accepted* the message, and the daemon activates the session and delivers it
when a slot frees. That needs a queued state the daemon owns and announces, so
the chat can say "waiting for a free slot" instead of looking hung — which is
precisely the failure mode the turn-feedback case exists to prevent.

The cheaper alternative is to **refuse** at capacity with a clear message
naming what is holding the slots. It is honest and much smaller, but it puts
the user's words back on them, which is the same fault already filed under
"Don't drop a message sent mid-turn" in [wishlist.md](../../wishlist.md).

## Decisions

1. **Queue or refuse.** Queuing matches the stated policy and avoids losing
   input; refusing is a fraction of the work. Queuing needs: a pending
   activation record, an event for it, and a line in the client.
2. **"Oldest" by what?** Oldest *activation* evicts the session you have had
   open all day. Least *recently used* (`last_active`, already tracked) evicts
   the one you have actually stopped touching. LRU is almost certainly the
   intent.
3. **Does the manager count?** It holds a real `claude-agent-acp` subprocess,
   so for memory it must. But it is ephemeral, and stopping an ephemeral
   session *deletes* it — so it must be exempt from eviction while still
   counting toward the total. With a cap of 5 that leaves 4 evictable, which
   should be said out loud rather than discovered.

## Deadlock, and saying so

If every live session is busy, or the only candidates are exempt, a queued
activation waits indefinitely. That is correct behaviour, but it must be
*visible*: a queued session that has waited a long time should say so, in the
same spirit as the quiet-turn warning. Silence here would reproduce exactly
the class of bug the observability case was opened to kill.
