# Package report: the daemon core

Implementer's own report, Opus 5 delegate, 2026-09-13, branch `hardening-daemon` (seven commits on top of the case). Kept verbatim; the independent review is in [package-daemon-core-review.md](package-daemon-core-review.md).

All six steps landed on `hardening-daemon`, worktree clean, `282 passed in 5.99s`.

## What landed

| commit | what |
|---|---|
| `43fdcfb` | `write_meta` is temp-then-replace, like `rewrite_transcript`; the loader's warning names the session id |
| `0ac759c` | `SessionRecord` (`src/falconfox/record.py`) replaces the parallel dicts; `SessionManager` is gone |
| `c4d7572` | `_slot_lock` + one lock per record, plus `_settle_turn` |
| `a36a920` | the open turn is a marker in `meta.toml`; an interrupted-turn notice is queued on startup |
| `91fc402` | `EventBus` queues bounded by `event_queue_limit` (default 4096); overflow drops and closes the subscriber |
| `1b950c3` | CLI read timeouts: 30s default, 1800s `send`, 150s `attach` |
| `18f8cf8` | two HTTP tests over the real ASGI app, guarding the handlers that became awaits |

## Decisions you should know about

- **"Live" is answered by the record**, which holds the `AgentSession`. `SessionManager` is deleted and the engine exports `new_session_id`. This alone closes half of F1: the victim stops being live before the await rather than after.
- **Lock order is slot-then-session, never the reverse**, so only the slot-lock holder ever holds two session locks and there is no cycle. Cost: session *setup* is serialised globally. Turns are not.
- **Delete cancels the turn rather than waiting**, logged at WARNING and emitted as an error notice *before* `turn_ended` (a client that finalizes on `turn_ended` has nowhere to put a later one). A cancelled turn reports `stop_reason: cancelled`, exactly what a backend-side cancel already produces, so the wire is unchanged.
- **Turn boundaries persist as a metadata marker, not replayable events** — the transcript stays a conversation, so what the client reads back and what a non-loading backend is replayed both stay as they were.
- **`rename_session` and `set_tags` are now coroutines** (they take the lock). `web/server.py` awaits them; no wire change.
- **`_attachments` stays keyed by request id.** It is the state of one call, not of a session.

## Beyond the brief

- A real bug the record exposed: `_emit` appended to whatever transcript was cached, so an event reaching a session whose cache was dropped on stop left it holding a **one-event history**. A resume showed that to the client as the whole transcript, and `revert_session` would have written it to disk over the real one. Fixed with its own test, verified failing first.
- `_drain_queue` now drops a session that fails to activate instead of retrying it at every idle event forever, and holds the queued text until it is actually sent.
- I could not run a live daemon smoke test (binding a port needs approval), so I ran an in-process one against the real `echo` backend instead: spawn, send, rename, tag, stop, resume-with-replay, delete, and two concurrent adds at a cap of 1 all behaved. That script was not committed.

## Outside my scope, for the record

`README.md:112` still says the bot "suppresses tool calls", and the reactions paragraph at `:114` documents behaviour the survey says was removed on 2026-09-12. `docs/casebook/2026-06-20__e854f4f4/overview.md:74` refers to `SessionManager`, now gone; historical, so I left it. Nothing was pushed.