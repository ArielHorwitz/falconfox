# Survey: the Telegram client (`src/falconfox_telegram/`)

Read-only survey by a delegated Opus 4.8 agent on 2026-09-12 against `dev` at 9b43889, from the brief in [survey-briefs.md](survey-briefs.md). Confidence labels are the surveyor's own; the lead's spot checks are recorded in [overview.md](overview.md).

## 1. Map

The client is one process: `FalconFoxTelegramBot` wraps two thin HTTP clients (`DaemonApi`, `TelegramApi` in `api.py`), a markdown→HTML renderer (`rendering.py`), and a detached-tmux shell runner (`shell.py`). `run()` opens a websocket to the daemon inside `async for websocket in connect(...)`, so a daemon restart is a reconnect, not a death. Per connection, `_run_connected` spawns three tasks: `_receive_events` (daemon event stream), `_poll_telegram` (30s long-poll `getUpdates`), and a `_reconcile_topics` task. Per live turn it also spawns two tasks: `_activity_loop` (keeps "typing…" alive every 4s) and `_progress_loop` (edits the one progress message).

**Persistent state** lives in five JSON files under the state dir: `topics.json` (session↔thread, plus applied title/icon), `turns.json` (the in-flight turn map, written atomically), `forum.json` (learned forum id), `infra.json` (manager/concierge session ids), `tray.json` (files awaiting a prompt). All are written temp-then-rename and treated as never-fatal on failure.

**In-memory turn state is the dominant fact of this module**: roughly twenty parallel dicts keyed by `session_id` (`bot.py:683-723`) hold one turn each — `_turn_dest`, `_turn_id`, `_reply_parts`, `_consumed`, `_delivered`, `_progress_msg`, `_progress_lines`, `_progress_sent`, `_seen_tools`, `_thought_parts`, `_usage_view`, `_queues`, `_activity_tasks`, `_progress_tasks`, and more. There is no `Turn` object; each field is added and popped by hand in `_forward` (start), `_finish_turn` (end), `_adopt_turn`/`_reconcile_persisted_turns` (restart), and `_reset_connection_state`.

**Turn lifecycle**: message in → `_handle_update` routes by chat/thread → `_forward` sets `_turn_dest`, sends `send` over the ws, creates the progress message, starts both loops → daemon streams `message`/`thought`/`tool_call`/`usage` events into progress lines and the reply buffer → `turn_ended` → `_finish_turn` cancels loops, stamps progress, sends the reply threaded to the prompt, accounts for delivery, then flushes any queue. A mid-turn message is queued and joined into the next prompt.

## 2. Findings

### F1. A reply-send failure is misread as a daemon outage and triggers a reconnect storm
**Where:** `_send_reply` `bot.py:2478-2502`; `html_message` `api.py:327-340`; `_receive_events` `bot.py:2628-2630`; `run()` `bot.py:772-779`.
**What goes wrong:** `_send_reply` awaits `telegram.html_message(...)` with no try/except. `html_message` catches the HTML send's `ApiError` and falls back to `self.message(...)` — but that fallback is *not* wrapped, so if the plain send also fails the `ApiError` propagates out of `_send_reply` → `_finish_turn` → `_handle_event` → `_receive_events`, which has no guard. The loop dies, `asyncio.wait` returns it, `finished.result()` re-raises, and `run()`'s `except (ConnectionClosed, ApiError, OSError)` catches it, announces **"Daemon connection lost"**, and reconnects — though the daemon was healthy. On reconnect the turn is reconciled and the recovered reply is sent to the same dead destination, failing again: a storm.
**Evidence:** the only `try/except ApiError` around a send in the turn path is on *progress* edits (`bot.py:2470`); the reply path has none. A hand-deleted topic (buglist) produces a permanent "message thread not found" on every send there.
**Confidence:** CONFIRMED by tracing.
**Remedy:** make the event path total — wrap per-event handling in `_receive_events` so a Telegram `ApiError` can never be mistaken for a daemon fault, and treat a persistent send failure to a bound thread as "unbind and recreate the topic" (the reactive fix the buglist already sketches). Half a day.
**Related:** buglist "Deleting a topic by hand strands its session" — the real consequence is worse than the "silently stops answering" recorded there.

### F2. `_finish_turn` races a new `_forward` for the same session and orphans a "Working…" message
**Where:** `_finish_turn` `bot.py:2813-2885`; `_forward` `bot.py:2568-2626`.
**What goes wrong:** `_finish_turn` pops `_turn_dest` up front (`:2822`) but then awaits three times — awaiting the cancelled loops (`:2823-2830`), `_update_progress` final note, and `_send_reply` (network). During those awaits the `_poll_telegram` task can run `_handle_update` for a fresh message to the *same* session. `_forward` sees `_turn_dest` absent, starts a new turn, sends a new progress message and stores its id in `_progress_msg`, and starts new loops. Control returns to `_finish_turn`, which at `:2873-2885` pops `_reply_parts`, `_progress_msg`, `_delivered`, `_consumed`, `_seen_tools`, etc. — erasing the *new* turn's freshly set state. The new turn's stored progress-message id is gone, so its progress loop creates a **second** "Working…" message; the first is orphaned and never edited again.
**Evidence:** teardown pops (`:2873-2885`) run after the `_send_reply` await (`:2872`), with no re-check that the popped state still belongs to the turn being finished.
**Confidence:** CONFIRMED mechanism; PLAUSIBLE as the cause of the buglist "extra Working… after the reply, never resolves" (trigger is a non-queued message arriving in the reply-send window).
**Remedy:** collapse per-turn state into one `Turn` object keyed by session, captured by identity at finish; teardown pops the object once, and a racing `_forward` installs a distinct object rather than racing loose fields. Removes the whole class. Multi-day (see Seams).
**Related:** buglist "An extra 'Working…' message appears after the reply".

### F3. `_usage_view` is never cleared per turn, so token/context stamps leak across turns
**Where:** set at `bot.py:2656-2660`, read at `:2864-2870`, cleared only in `_reset_connection_state` (`:888`); `_forward` (`:2590-2602`) does not clear it and `_finish_turn` does not pop it.
**What goes wrong:** a turn that emits no `usage` event inherits the previous turn's figures in its "Turn finished · N tokens / ctx X/Y" stamp. Wrong numbers are reported as if current, and the dict grows for the life of a connection.
**Confidence:** CONFIRMED.
**Remedy:** clear it in `_forward` and pop it in `_finish_turn` — or fold it into the `Turn` object from F2, which makes this impossible. An hour standalone.

### F4. Twenty parallel per-session dicts make every turn change touch N places
**Where:** `bot.py:683-723`, plus `_forward`, `_finish_turn`, `_adopt_turn`, `_reset_connection_state`, `_persist_turns`.
**What goes wrong:** the code's own comment calls the silent failures "a hidden state of" these dicts (`:1744`). Adding or removing a per-turn fact means editing five sites; a forgotten pop leaks (F3) and a forgotten init strands. F2 and F3 are direct instances.
**Confidence:** CONFIRMED (structural).
**Remedy:** a single `dict[str, Turn]`. This is the root the other findings hang off. Multi-day but high leverage.

### F5. Blocking disk writes on the hot event path
**Where:** `_persist_turns` `bot.py:892-918`, called from `_close_block` (`:2380`), `_close_thought` (`:2395`), `_enqueue_message`, `_drop_queue`, `_forward`, `_finish_turn`.
**What goes wrong:** every narration block and thought close serializes the *entire* turn map and does a synchronous `write_text`+`replace` on the single event loop. A chatty turn writes the whole map many times a second, stalling all sessions' event handling during each write.
**Confidence:** CONFIRMED (behavioural cost); PLAUSIBLE as a contributor to perceived stalls.
**Remedy:** debounce persistence (dirty flag flushed on a timer / at turn boundaries) and/or write per-session files. Half a day.

### F6. `_mirror_session` and the reconciler apply titles/icons without the topic lock
**Where:** `_mirror_session` `bot.py:2773-2789` vs `_reconcile_topics` `:1533-1536`; both call `_apply_title`/`_apply_icon`, which check-then-await-then-record around `_topic_names`/`_topic_icons`.
**What goes wrong:** a `session_updated` event and the concurrent reconciler can both read "title differs", both await `rename_topic`, producing two edits (two service messages, one budget hit; the second returns `TOPIC_NOT_MODIFIED`). For icons this is exactly the "several edits in quick succession" the buglist names as the aggravator of clients showing a stale icon.
**Confidence:** CONFIRMED double-apply; PLAUSIBLE contributor to the icon-intermittency bug.
**Remedy:** take `_topic_lock` (or a per-topic lock) across the check-and-record in `_apply_title`/`_apply_icon`. An hour.
**Related:** buglist "Topic icons intermittently do not reach clients".

## 3. Seams

`api.py`, `rendering.py`, and `shell.py` are already clean, independent cut lines — `shell.py` in particular is a self-contained feature with no turn coupling. Within `bot.py`, the co-located concerns are **forum discovery/membership**, **topic sync**, **command handlers**, **tray**, **infra-session management**, and **turn feedback**. The first five are only *co-located*: they touch the topic maps and the two API clients but almost nothing else. They are safe to extract behind a small shared context (the API clients plus the topic maps).

The **turn feedback** state machine is the unsafe cut, and the reason is F4: its state is ~20 loose dicts, not an object, so nothing can be lifted out without threading all of them. The enabling move is F2's `Turn` object — once per-turn state is one object with one owner, the turn machine becomes extractable and F2/F3 disappear as a side effect. Do that first; the rest of the split follows cheaply. The daemon-event decoder (`_handle_event`) and the Telegram-update decoder (`_handle_update`) are also separable, but both currently speak raw `dict.get("...")` against two undocumented wire formats, so a typed event layer would have to precede a clean split.

## 4. Test coverage

Coverage is unusually thorough (261 tests). Well exercised: routing per topic, forum discovery/adoption/migration, the two-message turn (narration vs reply, tool markers, trailing-tool answer, progress create-then-edit, clock pacing), queue hold/flush, turn persistence and the three reconciliation outcomes (adopt/recover/lost), silent-turn reporting, tray add/remove/sweep, attachments and fallbacks, orientation composition, and the "failed chat action doesn't silence the turn" regression.

The gaps are precisely the risky cross-task and failure paths. **F1** is invisible because `FakeTelegram.html_message` never raises — no test drives a permanent send failure to a bound thread. **F2** cannot appear: tests drive handlers sequentially with `await asyncio.sleep(0)` and never inject a `_forward` during a `_send_reply` await, so no interleaving across the real `_receive_events`/`_poll_telegram` tasks is tested. **F3** (stale `_usage_view`) has no test asserting a second turn without a usage event. **F6** (concurrent title/icon apply) is untested. In short: single-turn correctness is nailed; concurrent-turn and send-failure correctness is not.

## 5. Questions for the lead

- For F1/F2: is the reactive "unbind-and-recreate on send failure" fix now acceptable, given the buglist deferred it "on the eve of a stability soak"? It is the same change that fixes both the storm and the strand.
- Is the `Turn`-object refactor (F4) in scope for this hardening pass, or should F2/F3/F5 be patched locally first? They are individually cheap but collectively re-create the exact fragility F4 describes.
- `_report_attachment` sends over the ws without `_ws_lock` (`bot.py:2040`) while `_forward` holds it (`:2610`). Is `websockets` concurrent-send safe here by design, or is the lock meant to cover both?