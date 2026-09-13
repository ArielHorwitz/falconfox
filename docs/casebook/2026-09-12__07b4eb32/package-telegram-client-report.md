# Package report: the Telegram client

Implementer's own report, Opus 5 delegate, 2026-09-13, branch `hardening-bot` (seven commits on top of the case). Kept verbatim; the independent review is in [package-telegram-client-review.md](package-telegram-client-review.md).

All seven steps landed on `hardening-bot`, suite green: **275 passed in 5.65s** (261 before, 14 new).

## Commits

| commit | what |
| --- | --- |
| `1811dd1` | `fix: a Telegram failure is never a daemon failure` — per-event handling total; `run()` catches `ConnectionClosed`/`OSError` only |
| `410c3a5` | `fix: a dead topic is replaced, not mourned` — unbind and recreate on "message thread not found"; closes that buglist entry |
| `0cfab7d` | `refactor: a turn is one record, not twenty dicts` — `Turn` dataclass, `_finish_turn` tears down by identity; closes the "extra Working..." entry |
| `9d9dd19` | `fix: a title and an icon are applied under the topic lock` |
| `ab2ac69` | `perf: the turn map is written on a timer, not on every narration block` |
| `3cc3e37` | `refactor: one writer to the daemon socket, and no lock on it` |
| `da6e548` | `chore: the fake and the README describe the client that exists` |

## What the tests prove

F1: a failing reply send no longer ends `_receive_events` (next event still handled, dropped one logged with session and type); an `ApiError` out of `_run_connected` is not announced as a daemon outage and does not reconnect, while a `ConnectionClosed` still does both. Dead topic: one refused send makes exactly one new topic, the reply lands there, the binding is persisted, and a rate limit never unbinds. F2: the exact interleaving leaves one progress message per turn (three on the old code) with the old turn still stamped. F3: a second turn reports no figures of the first. F6: concurrent applies make one call each. F5: five narration blocks produce zero writes on the event path and one on the timer; a disconnect flushes what was pending. Plus: nothing writes to the socket outside `_ws_send`, and no `FakeTelegram` method is missing from `TelegramApi`.

## Decisions worth your attention

- **Both buglist entries are closed.** Test (a) reproduced the orphaned "Working…" on the pre-refactor code (three messages where two belong), which was the condition you set.
- **The websocket lock is gone, not extended.** websockets 16.0 writes an unfragmented `str` frame into the protocol buffer without awaiting and waits out any fragmented send first, so concurrent sends cannot interleave. Evidence is in `asyncio/connection.py`; the reasoning lives in `_ws_send`'s docstring.
- **The existing topic lock, not per-topic locks.** These calls are already paced against a budget the whole forum shares, so per-topic concurrency gives back nothing the pacing does not take.
- **The topic repair rides the reply only.** Progress edits and chat actions swallow their errors by design, so the first reply of the turn is what notices a dead topic. That is the send the buglist was about.
- **`run()` can now exit on a stray `ApiError`.** Every path into `_run_connected` is guarded, so this should not happen; if it does, `Restart=always` restarts the bot in 3s with a traceback in the journal, which is a better failure than a silent reconnect storm.
- **Two existing tests changed meaning** (named in the commit): a mid-turn fixture now always has a start time, so its header carries a clock; and "both loops stopped" is now "the turn is gone". The other ~100 edits to `test_falconfox_poc.py` are mechanical field renames, all mid-file, so the other package's appends should merge cleanly.
- **Two small behaviour changes fell out of the record**, both in the commit body: events for a session this client carries no turn for no longer accumulate in buffers nobody reads, and a flushed queue no longer inherits the previous turn's prompt message when its own messages carry no id.

## Outside my scope, noted not touched

- `README.md:111` still says a turn "suppresses tool calls and sends the final reply as one message" with no mention of the progress message that now carries the narration and tool markers. Accurate as far as it goes, incomplete.
- The daemon side of the same `Turn`/`Session` shape, the coordinator's lifecycle locks, `meta.toml`'s non-atomic write, and the CLI timeout are all untouched, per the brief.

Nothing was left unfinished.