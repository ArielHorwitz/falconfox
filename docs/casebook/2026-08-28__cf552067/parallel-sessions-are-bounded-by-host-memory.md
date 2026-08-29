# Parallel sessions are bounded by host memory, not by the routing

*Found by running it, 2026-08-29.*

The dev daemon was **OOM-killed** while carrying ten sessions:

```
Active: failed (Result: oom-kill)
coordinator shutdown: sessions=10
watchdog: process-wide stall: no heartbeat for 39.5s ... host pressure
          (swap/CPU) pressure[some avg10=99.99 avg60=99.88 avg300=94.55]
```

The host is **951 MB with 1 vCPU**. Every session holds its own
`claude-agent-acp` subprocess, so sessions are the unit of memory cost. Ten of
them, plus two daemons and two bots, exhausted the machine; the watchdog
logged sustained thrashing for minutes, and the kernel killed the daemon in
the middle of a turn that had been running 19 minutes.

Nothing here is a defect in the topic work. The routing is cheap and the
client held up. The point is the opposite one:

**The client's cost model and the host's are different, and this case widened
the gap.** Making a session used to mean deciding to move the focus pointer.
Now it means adding a topic to a list — cheaper to do, and visibly inviting.
The interface encourages exactly the thing the machine cannot afford.

This is the same host capacity noted in the observability case
([2026-08-25__83e4b06e](../2026-08-25__83e4b06e/overview.md)) and recorded
there as the user's call. It is no longer a background fact: the feature this
case exists to build is the one that makes it bite.

## What makes it tractable: a stopped session already has a UI

The forum gives *stopped* a first-class representation, and it was measured
into this case for a different reason: a **closed topic still accepts bot
writes**. So stopping an idle session to reclaim its subprocess costs nothing
in the interface — the topic stays in the list as the record, the user cannot
prompt something that is not running, the transcript survives, and the next
message can reopen and resume it.

That turns a capacity problem into a **lifecycle policy the UI can already
express**, rather than a cap that has to be explained. Options, roughly in
order of how much they change:

- **Stop idle sessions** after some period, close their topics, resume on the
  next message. Costs a resume latency on a stale session; costs nothing else.
- **Cap concurrently *live* sessions**, stopping the least recently used when
  a new one starts. Same mechanism, bounded rather than timed.
- **Refuse to spawn past a limit** — the crude option. It denies the user
  something the interface is inviting them to do, and says nothing useful.

The first two both reduce to: *live* is a resource, *stored* is free, and the
forum already draws that distinction on screen.

## Not yet decided

Whether to do any of this here or file it. It is arguably its own case — the
subject is daemon-side session lifecycle, not Telegram routing — but it was
found by this work, it is made worse by this work, and the mitigation depends
on a property this work measured. Recorded here rather than lost.

An OOM during a 19-minute turn also cost that turn, which is the failure mode
the turn-feedback case cared most about. It reported correctly
(`outcome=error`), so the reporting held; the loss was real anyway.
