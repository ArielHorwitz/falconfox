# Overview

The daemon was **OOM-killed carrying ten sessions** on a 951 MB / 1 vCPU host
on 2026-08-29, nineteen minutes into a turn, after the watchdog had logged
sustained thrashing for minutes. Every session runs its own ACP backend
subprocess, so sessions are the unit of memory cost.

A flat count cap was built in response and works, but a count is a poor proxy
for the thing that actually runs out. This case is about bounding live
sessions properly: **by inactivity, by count, and by memory footprint, all
configurable**, so a deployment is safe on a small VPS without being
artificially crippled on a large one.

## Handoff (opened 2026-08-30, not yet started)

Opened deliberately without implementation, to be picked up by a fresh
session. Everything below is design settled in discussion plus facts measured
on the host — none of it is built.

**Read first:** the parallel-sessions case
([2026-08-28__cf552067](../2026-08-28__cf552067/overview.md)), specifically
`parallel-sessions-are-bounded-by-host-memory.md` (the OOM itself) and
`capping-live-sessions-design.md` (the count cap that shipped).

**Where the existing code is.** The count cap lives on the **unmerged
`topics` branch**, not on `falconfox`. `git log falconfox..topics` — look for
`_ensure_slot`, `_drain_queue` and `live_session_ids` in
`src/falconfox/coordinator.py`, and `max_active_sessions` in
`src/falconfox/config.py`. This case builds on that work, so it needs that
branch (or its merge) rather than the trunk.

**One correction already agreed and not yet applied.** Ephemeral sessions
(the Telegram client's manager and private-chat sessions) were changed to
*not* count toward the cap, because they were silently eating a user's budget
— a cap of 3 bought one work session. The user's judgment, which supersedes
that: they **should** count, because a limit that omits real processes is a
lie; the fix is a **minimum** (at least 3) plus infrastructure sorting
**last** for eviction rather than being exempt from it. Apply that as part of
this work.

## The three layers, all configurable

1. **Inactivity** — purge sessions idle longer than a configured duration.
   Voluntary and cheap: it reclaims memory nobody is waiting on, before any
   pressure exists. This is the layer that should do most of the work.
2. **Count** — a flat ceiling. Predictable and explainable, which is why it
   stays the user-facing contract even though it is the least accurate.
3. **Memory footprint** — a budget with high and low watermarks. Accurate,
   and the only one that reflects what actually runs out.

They are independent triggers over one shared policy. Any of them may fire;
what happens next is the same.

## The eviction policy is orthogonal to the trigger

Whatever fires, the victim rule does not change: **least-recently-used among
idle sessions**, by `last_active`. Idle sessions are the right victims under
every metric, because their subprocess is dead weight regardless of why the
limit was hit. A *working* session is never evicted — that destroys a turn in
flight. Infrastructure sessions sort **last**, regardless of their activity.

One ordering question is still open: where a *working* infrastructure session
sorts, given that evicting it also destroys a turn.

Evicting infrastructure is cheap but not free: both are lazily respawned (the
private chat on its next message, the manager on the next use of General), so
the channel comes back — but they are ephemeral, and stopping an ephemeral
session *deletes* it, so that conversation's history is lost.

## What was measured on the host, and what it settles

**cgroups already do the accounting.** systemd puts each unit in its own
cgroup covering the whole process tree, so per-daemon memory is one file read,
correctly accounted, with no RSS-versus-PSS ambiguity and no double-counting
of the shared Node runtime:

```
falconfox-daemon:      memory.current 284M  = anon 237M + file 34M + slab 2.5M
falconfox-dev-daemon:  memory.current 184M  = anon 163M + file 11M
```

**Threshold on `anon`, not `memory.current`.** The `file` portion is page
cache, which the kernel reclaims for free under pressure. Evicting a session
to reclaim page cache is pure loss, and would look like the daemon killing
sessions at random.

**Per-session attribution is not needed.** Above the high watermark, drop LRU
until below the low watermark. No process-tree walking. (Per-session figures
would only be needed to predict how much a given eviction buys, which the
watermark loop makes unnecessary.)

**Sessions vary by more than 2x**, which is why count is a poor proxy:

```
903362  185956 KB  claude --resume=…      (resumed, carrying history)
903896   83364 KB  claude --session-id=…  (fresh)
```

**PSI is the honest signal, and it is per-unit.** `memory.pressure` in each
unit's cgroup measures time lost waiting for memory rather than memory
consumed, so it reports actual harm. It read `some avg10=99.96 avg60=99.78`
during the OOM and `0.00` when idle. Being per-unit, it is correct on a shared
host — which matters here, because a dev daemon and the production daemon run
side by side and any absolute budget double-counts.

**Nothing currently limits the units.** `memory.max` is `max` on both, so the
host OOM killer is the only backstop — which is exactly how the daemon died.
Setting `MemoryHigh=` makes the kernel throttle and reclaim instead of
killing, and that back-pressure raises `memory.pressure`, which the daemon can
observe. Kernel throttles, daemon evicts gracefully, pressure falls; and if
the daemon fails to act, `MemoryHigh` still prevents host-wide thrash. It also
gives a portable way to express watermarks — a fraction of `MemoryHigh` rather
than absolute megabytes, so one number configures a host.

## Two traps that will bite

**Reclaim is asynchronous.** Stopping a session does not drop `memory.current`
— the subprocess must exit and the kernel must reclaim. A naive
`while over_limit: evict()` evicts *everything* in a fraction of a second,
because no earlier eviction has registered yet. The loop must await real
process exit, settle, and re-read before deciding again.

Mitigated by design: monitoring should run **continuously**, not only when a
session is spawned. Then a breach is usually resolved in the background before
anyone asks for a session, and spawn-time is normally a no-op. When it is not,
it reuses the queue-and-notify path the count cap already built — the user is
told "waiting for other sessions to close and free up memory" rather than
being refused.

Poll interval has to beat allocation rate: a turn can allocate fast, and a
30-second poll will let a runaway reach OOM between checks. The
`memory.pressure` read is one file and effectively free, so it can run every
few seconds even if fuller accounting runs less often.

**You can hit the floor and still be over.** Baseline plus infrastructure plus
any *working* sessions may leave no candidates. There must be a defined
give-up: stop evicting, say so, refuse or queue new spawns. Otherwise the loop
spins, or starts evicting what it was told not to.

## Legibility is a requirement, not a nicety

The count cap already reports evictions into the affected session's own topic
("stopped to free a session slot; nothing is lost, send a message here to pick
it up again"), using the measured fact that a closed topic still accepts bot
writes. That must survive.

The tension worth holding: memory is *accurate*, count is *predictable*. A
byte budget means the same three sessions are fine one day and evicted the
next, and "why did my session close?" becomes much harder to answer. Hence
count staying the user-facing contract, with memory and pressure underneath —
and **different messages for different causes**, so the reason stays legible.

## Environment facts that cost time to rediscover

- Host: **1 vCPU, 951 MB, 2.3 GB swap**. It swaps under load; the watchdog
  reports host pressure honestly and should be believed.
- Two daemons run side by side: production (`~/.local/state/falconfox`, port
  9721) and a dev instance (`~/.local/state/falconfox-dev`, port 9722, its own
  `XDG_CONFIG_HOME` at `~/.config/falconfox-dev`). Isolation is via
  `XDG_STATE_HOME`; the port is auto-selected.
- The dev instance exists precisely so this kind of work is not tested on the
  user's only phone interface. Use it.
- The daemon runs as a **systemd user unit**, which is what makes per-unit
  cgroup accounting and `MemoryHigh=` available at all.
