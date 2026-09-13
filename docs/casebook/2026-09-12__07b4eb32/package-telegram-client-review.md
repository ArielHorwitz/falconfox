# Package review: the Telegram client

Independent read-only review by an Opus 4.8 delegate, 2026-09-13, of the seven commits on `hardening-bot`. Verdict: merge with fixes. The three findings went back to the implementer as a follow-up.

The suite is green with no skips (275 passed), confirming the report's headline. My verification turned up one hole the seven commits open. Here is the review.

---

## Review: `hardening-survey..hardening-bot`

I verified each of the six focus areas against the source and the installed packages. Most of the package is sound. The turn-record refactor (F2/F3), the debounced persistence (F5), the topic-lock (F6), and the lock removal (F3-ws) all hold up. Two related defects sit in the reconnect path, and one behaviour change is unstated.

### Findings, most severe first

**1. `_reconcile_persisted_turns` aborts wholesale on any per-turn send failure, and never cleans the map. — CONFIRMED — `bot.py:1003-1036`**
The loop over persisted entries has no per-turn guard. A single `_say`/`html_message` `ApiError` from any entry (a dead topic, or a `429` in the burst of sends a busy reconnect fires) propagates to the one `except Exception` in `_run_connected` (`:853-856`). Consequence: every turn ordered *after* the failing one is skipped, and the `self._persist_turns()` cleanup at `:1036` is never reached, so the offending entry stays on disk and re-aborts reconciliation on *every* future reconnect. The turns after it in iteration order are stranded indefinitely, not just for one connection. This is the same class of "one Telegram refusal poisons everything" that F1 set out to kill, surviving in the path F1 did not cover.

**2. The recovered-reply path does not recreate a dead topic, unlike the live path. — CONFIRMED — `bot.py:1066-1082`**
`_deliver_recovered_turn` sends straight through `telegram.html_message`/`_say` (`:1074-1078`), not through `_send_for_turn`. So the exact scenario the survey named — "on reconnect the recovered reply is resent to the same dead destination" — is only half-fixed: it no longer storms (F1), but the reply is now lost, and via Finding 1 it also aborts the rest of reconciliation. The live turn path (`_send_reply` → `_send_for_turn` → `_replace_topic`) is the only place topic recreation is wired; a turn that *ended while the bot was away* to a since-deleted topic gets none of it. No test drives this: `DeadTopicTests` exercise only `_finish_turn`.

**3. A transient reply-send failure silently drops the turn's reply. — PLAUSIBLE — `bot.py:2626, 3050`**
In `_finish_turn`, a non-"thread not found" `ApiError` from `_send_reply` (a `429`, a read timeout) propagates. The turn is already popped and its loops cancelled at `:3001`; `_receive_events` swallows the exception as a dropped event; `turn.delivered` is still 0 but the `SILENT_TURN` notice at `:3056-3070` is never reached because the raise skips it. So a rate-limited final reply vanishes with only a debug log — no retry, no chat notice. The old code eventually redelivered it via reconnect. This is partly the intended "per-event totality" tradeoff, but the silence (not even `SILENT_TURN`) is the sharp edge.

### The six focus areas

1. **`_finish_turn` by identity.** Sound. The turn is popped synchronously at `:3001` before any await, and teardown/`take_loops` operate on the captured object, so a turn installed by a racing `_forward` during the reply-send await is untouched, and its loops are its own. Double teardown is idempotent (second `pop` returns `None`). The F2 test drives the real interleaving with a blocking send and holds.

2. **Dead-topic path.** `TOPIC_GONE` matches only the substring "message thread not found" (lowercased); a `429` cannot match, and the guard also requires the binding to still point at the failing thread. The recreate retries exactly once per send, so no runaway. The persisted binding is correct after recreation (`_unbind`→`_create_topic`→`_bind`, test-confirmed), and the tray (keyed by session) and queue survive. The gap is only the recovery path (Finding 2).

3. **Removing the ws lock.** Confirmed correct. `websockets` is `16.0`; `send()` for a `str` writes the whole frame into the protocol buffer synchronously inside `send_context` *before* the `drain()` await (`connection.py:311-317, 917-920`), so two concurrent str sends cannot interleave at the byte level. Every bot send is `json.dumps(...)` through `_ws_send`, never an iterable. The claim holds for all sends, including large payloads.

4. **Debounced persistence.** Three outcomes still hold; a crash in the ≤1s dirty window costs at most a replayed progress line or a few double-delivered characters, as documented. Flush is guaranteed on `_reset_connection_state` (`:927`, gated on `_turns_dirty`) and at turn boundaries. Note the reset flush is conditional on `_turns_dirty`, which interacts with Finding 3's stale-record recovery, but the debounce itself is correct.

5. **`run()` propagating `ApiError`.** I enumerated every path into `_run_connected`: `_announce_daemon_up`, `_reconcile_persisted_turns`, `_ensure_manager`, `_load_icons` are each guarded; `_reconcile_topics_guarded` is a task gathered with `return_exceptions`; both loops are total. No unguarded `ApiError` reaches `run()`. The intentional exit-on-stray-`ApiError` is real but unreachable in practice. Clean.

6. **The ~100 test edits.** Mechanical. Net four fewer asserts, all from equivalent consolidations (`_activity_tasks == {} and _progress_tasks == {}` → `_turns == {}`; `_turn_dest == {} and _reply_parts == {}` → `_turns == {}`). Dropped `_turn_started_at` assignments are preserved by `Turn`'s `default_factory`. The two named meaning-changes are the only semantic ones. No test lost its teeth.

### Verdict

**Merge with fixes.** The refactor is correct and the suite proves what it claims for the paths it covers. But Finding 1 reintroduces a poison-the-whole-batch failure on the reconnect path, and Finding 2 leaves the buglist's dead-topic scenario only half-closed for recovered turns. Wrap the reconcile loop body per-entry (log and continue, still cleaning the map), and route `_deliver_recovered_turn` through `_send_for_turn` so recovery recreates a dead topic like the live path does. Finding 3 is worth a decision: at minimum let a dropped reply still emit `SILENT_TURN`.