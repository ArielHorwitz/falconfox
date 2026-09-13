# Package review: the daemon core

Independent read-only review by an Opus 4.8 delegate, 2026-09-13, of the seven commits on `hardening-daemon`. Verdict: merge with fix 1. All three items went back to the implementer as a follow-up.

I now have a complete picture. I verified the lock analysis, the turn-marker lifecycle, the event bus, wire compatibility (clean), and the tests (282 pass; the concurrency and HTTP tests genuinely prove their claims). I found one real data-loss bug (preexisting, faithfully carried over) and one narrow notice-loss edge.

---

# Review: `hardening-survey..hardening-daemon`

Seven commits, 282 tests green. The branch does what it set out to do: the cap race (F1), the transition-vs-turn races (F2), the torn `meta.toml` (F3), the turn marker (F4), the bounded bus, and the CLI timeout are all addressed, and the tests for them drive real concurrent tasks and assert effects rather than status codes. Wire compatibility with the unchanged Telegram client is intact (it reads only wire fields and `turn_ended`'s `turn_id/outcome/stop_reason/output_chars`, all still emitted; it sends only `tag`, never `set_tags`; no renamed or newly-required field).

**Lock order / deadlock: sound.** All three slot-needing entries (`add_session`, `resume_session`, `send`) take `_slot_lock` *before* any `record.lock`, and no session-lock holder ever requests `_slot_lock` (`_begin_turn` calls `_resume_locked`, not the public `resume_session`; `_drain_queue` holds nothing). The only two-session-lock holder is the single slot-lock holder evicting an idle victim, so there is no cycle. `send` releases both locks before awaiting the turn (`coordinator.py:814-821`), so `cancel`/`stop`/`delete` reach a busy session — confirmed by `test_a_running_turn_does_not_lock_the_session_out`. F6 is closed as a side effect: the second `send` waits on the session lock until the connection is up.

## Findings

### 1. `stop` deletes a persisted, auto-named session whose transcript isn't cached — CONFIRMED (mechanism), preexisting
`coordinator.py:956` decides keep-vs-delete with `not self._should_persist(record)`, and `_should_persist` (`coordinator.py:233-239`) inspects the *in-memory* `record.transcript` (`… or []`), not disk. `release()` sets `record.transcript = None` (`record.py:197`), and `load_persisted` starts every restored record at `transcript=None`. So for a session that was never renamed (`auto_named=True`, the default):

- **Post-restart stop:** `falconfox stop <id>` on a restored, never-read session → `_should_persist` sees `None or []` → False → `_delete_locked` → `store.delete` removes the directory and its transcript.
- **Double stop:** stop a live auto-named session (kept, cache dropped), stop it again → same deletion.

The manager orientation actively tells an agent to `falconfox stop <id>` to free slots, so this is reachable, and it is silent data loss. `_persist_meta` already guards correctly with `record.persisted or self._should_persist(...)` (`coordinator.py:350`); `_stop_locked` should use the same condition. This is **not a regression** — the parent has the identical shape (`self._transcripts.get(session_id, [])`) — but the branch is a hardening pass sitting right on this code, and the fix is one clause.

### 2. The interrupted-turn notice can be lost on resume-without-send → stop → send — PLAUSIBLE, low
`_adopt_interrupted_turn` queues the notice into `pending_context` once, at load (`coordinator.py:221`). A bare `resume_session` does not consume it; a subsequent `stop` runs `release()`, which clears `pending_context` (`record.py:200`) while leaving `open_turn` set. The next `send` does *not* re-adopt, and its `turn_started` overwrites `open_turn` then `turn_ended` clears the on-disk marker (`coordinator.py:262-269`) — so the news is never delivered and the marker is gone. The Telegram bot never issues a bare `resume` (only `send` and `attachment_result` over the socket; `resume` isn't in its HTTP set), so this is unreachable in normal operation and only hits the CLI `resume`+`stop`+`send` path. Worth a guard (re-check `open_turn` when building the prompt, or don't drop `pending_context` on `release`).

### 3. Nits
- `revert_session` (`coordinator.py:1033-1039`) does a hand-rolled partial teardown instead of `release()`, leaving `usage`/`commands`/`pending_context` stale. Unreachable by clients, but it re-creates exactly the by-hand field-listing the record was meant to end.
- The report's "verified failing first" for the one-event-history fix is loosely stated: the test and fix landed atomically in `0ac759c`, so it could only fail against a hand-reverted `_emit`, not the literal parent. The test does correctly discriminate the fix.
- `event_queue_limit=4096` is reasonable; a drop mid-chatty-turn relies on the bot's transcript-offset recovery (outside this branch) to not lose the in-flight reply.

## Verdict

**Merge with fix #1.** The branch is correct and well-tested for its stated scope, introduces no new races, and keeps the wire contract. Finding #1 is preexisting data loss, but it lives in the exact method this branch rewrote and costs one clause to close — fold it in rather than ship a hardening pass that leaves it standing. #2 and #3 are low priority.