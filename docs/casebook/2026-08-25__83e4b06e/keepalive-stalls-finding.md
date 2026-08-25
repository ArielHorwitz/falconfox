# The keepalive stalls: what the logs actually support (2026-08-25)

The case told its successor to chase the two `keepalive ping timeout`
disconnects (06:21:30 and 06:23:44) first, and framed them as "the daemon's
event loop stalled long enough to miss a ping." Reading both logs side by side,
**that framing is not what the evidence shows**, and the overview has been
corrected. This file is the full reconstruction.

## The aligned timeline

Second incident, both processes interleaved (all 2026-08-25):

```
06:22:15  daemon  action=send session=ffb5acf0          focus turn starts
06:22:25  daemon  session_added 0911a39b                ff-state-test SPAWNED (new ACP subprocess)
06:22:28  daemon  ff-state-test finished starting
06:22:31  bot     watchfiles: 1 change detected         focus agent wrote the pointer
06:22:33  daemon  turn complete: ffb5acf0               focus turn ends; rotate is pending
           --- bot logs nothing for 73s; daemon logs nothing for 49s ---
06:23:22  daemon  ws disconnect: code=1006
06:23:44  bot     daemon connection lost (sent 1011 keepalive ping timeout;
                  no close frame received); reconnecting
06:23:46  daemon  ws connect + session_added 32bb4755   bot back, focus respawned
```

Three observations that do not fit "the daemon's loop stalled":

1. **Both processes went silent, in overlapping windows** (bot 06:22:31→06:23:44,
   daemon 06:22:33→06:23:22). A stall inside one process does not silence the
   other.
2. **The expected focus-rotate spawn never reached the daemon.** The bot should
   have POSTed a spawn within seconds of 06:22:33 (the rotate was pending); no
   `session_added` appears until after reconnect. So the *bot* wasn't making
   progress either.
3. In the **first** incident, the daemon logged its `ws disconnect: code=1011`
   at 06:20:18 — 72 seconds *before* the bot reported the loss at 06:21:30. A
   daemon whose loop was stalled through that window could not have written
   that line; a bot whose loop was healthy would not take 72 seconds to notice
   its own failed connection.

## The likelier culprit: the host, not either loop

Facts gathered on the box (08:50 the same morning, at rest):

- **1 vCPU, 951 MB RAM**, and **1.1 GB of a 2.3 GB swapfile in use**.
- Five resident `claude` backend processes (60–200 MB RSS each) plus their
  `node claude-agent-acp` wrappers, the daemon, and the bot — the working set
  simply does not fit.
- PSI shows nonzero memory stall time (`some avg10≈1.7%`) even idle;
  ~1.2M major faults and ~2.5M swap-outs cumulative.

Both incidents sit right next to **session spawn** — incident 2 is 60 seconds
after `ff-state-test`'s ACP subprocess (a fresh node + claude process, the most
memory-hungry thing this system does) came up, with the focus session's own
turn running concurrently. On a 1-core machine that deep in swap, a spawn
plausibly puts *every* process into major-fault slow motion: pings go
unanswered on both sides, each side's websocket layer times the other out, and
each process's log shows a gap — which is exactly what the logs show. The
spawn correlation is also why the case's two named suspects (session spawn,
naming backend) both fit: not because their *code* blocks the loop, but
because both fork a fresh backend subprocess.

This is a supported hypothesis, not a verdict. The honest statement is the
case's own thesis: **nothing recorded what either process was doing**, so the
logs cannot decide between a blocked loop and a starved host.

## What shipped so the next stall answers the question itself

`falconfox/watchdog.py`, running in **both** processes: a daemon thread posts a
heartbeat onto the event loop every second. If no heartbeat runs for 5s:

- **Loop blocked in-process** (watchdog thread still on time): it logs WARNING
  *mid-stall* with the main thread's current stack — naming the exact blocking
  call the case went looking for.
- **Process-wide freeze** (the watchdog thread overslept about as much as the
  loop): it says so instead — host pressure, not a blocked loop — and attaches
  the PSI memory line and the major-fault delta as evidence.

Recovery is logged with the stall's duration either way. The next keepalive
timeout will sit next to a watchdog line that says which kind it was, in which
process, and — if it was in-process — where.

## Resolved (2026-08-25, 10:08): the watchdog adjudicated, and it was neither

A fresh keepalive timeout fired at 10:08:33 with the instrumentation live —
and **the watchdog stayed silent in both processes**. No loop stall, no
process-wide freeze; the daemon logged continuously through the window and
handled the reconnect within seconds. Both earlier hypotheses (a blocked
daemon loop; host-wide thrash) are ruled out for this occurrence.

What the logs showed instead: a 252-second turn with 11 tool calls was
running, and the **pre-fix** bot — chat actions still awaited inline — logged
nothing from 10:05:52 to 10:08:33, the width of a few back-to-back 40s
Telegram read timeouts. The mechanism, end to end:

1. A hung Telegram API call wedges `_handle_event`, so `_receive_events`
   stops draining the websocket.
2. The websocket library's recv queue fills; backpressure pauses the TCP
   transport.
3. Paused transport means the bot neither answers the daemon's pings nor
   reads the daemon's pongs. Both sides' keepalives starve: the daemon cuts
   the connection (`1006`), the bot reports `sent 1011 keepalive ping
   timeout` when its own timer fires.

This also retro-explains the original 06:2x incidents — bot silent for
minutes while the daemon logged normally, disconnects with no close frame,
the bot reporting the loss long after the daemon saw it. The swap-starved
host remains real and surely worsens Telegram call latency, but it was the
aggravator, not the cause.

**The fix was already live two minutes later** (`0de482f`, deployed 10:10:02):
the indicator send is detached from the event pipeline. The 10:08:33 timeout
was the last gasp of the pre-fix bot.

**A blind spot worth keeping:** the watchdog watches the *event loop*, not
the *event pipeline*. A consumer coroutine wedged on an await is invisible to
it, because the loop stays responsive. Its silence was still decisive — that
is what ruled the loop-stall theory out — but a pipeline-level instrument
(warn when daemon events sit unprocessed for tens of seconds, or when one
Telegram call exceeds a sane budget) is the follow-on if this shape recurs.

## If it is the host, the fix is capacity or load, not code — but it wasn't the cause

Recorded here rather than acted on, because it is the user's call: the box is
too small for five resident backends. Options, cheapest first: stop idle
sessions so their subprocesses exit (stored sessions resume on demand — this
is what the ephemeral/stored design is for); a bigger VPS; or swap tuning
(lower swappiness helps latency-critical daemons under thrash only modestly).
Worth deciding once the watchdog confirms the diagnosis.
