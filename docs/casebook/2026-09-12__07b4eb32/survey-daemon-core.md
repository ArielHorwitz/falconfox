# Survey: the daemon core (`src/falconfox/`)

Read-only survey by a delegated Opus 4.8 agent on 2026-09-12 against `dev` at 9b43889, from the brief in [survey-briefs.md](survey-briefs.md). Confidence labels are the surveyor's own; the lead's spot checks are recorded in [overview.md](overview.md).

## 1. Map

The daemon core is one god-object, `SessionCoordinator`, plus a thin engine under `engine/`. The coordinator owns every session as a spread of parallel dicts keyed by session id: `_metadata`, `_transcripts`, `_acp_ids`, `_config_options`, `_commands`, `_pending_context`, `_attachments`, `_auto_named`, `_usage`, `_busy_ids`, `_queued`, and the `_persisted` set. There is no `Session` object holding these together; a session *is* the agreement that all these dicts carry the same key. `SessionManager` (engine) separately holds the live `AgentSession` objects, so "is this session live" is answered two ways: `meta["live"]` and `sessions.get(id) is not None`.

Lifecycle: a session is created *live* (`add_session` → `AgentSession.start`) or *stored* if over `max_live_sessions` (queued in `_queued`). `send` resumes a stored session (`resume_session`), replaying the transcript as context when the backend lacks native `session/load`. `stop_session` tears down the subprocess but keeps disk state (falls through to `delete_session` for ephemeral/never-used). Eviction (`_ensure_slot`) stops the least-recently-used *idle* session to free a slot; `_drain_queue` reactivates queued sessions when any session goes idle.

State flows one way out through `_emit` → `EventBus` (unbounded per-subscriber queues). `_emit` is also where authoritative state is *written*: `agent_state` events set `meta["state"]`, `usage` merges into `_usage`, replayable events append to the transcript and trigger persistence. So publishing and state-mutation are the same call.

Engine: `AgentSession` runs one ACP subprocess via an `AsyncExitStack`, serializes turns with an in-memory `_busy` flag, and counts turn facts (`_TurnStats`). `AgentClient` translates ACP callbacks to events and brokers/​confines filesystem access. `oneshot` spawns a throwaway subprocess for session naming.

Persistence (`storage.py`): per-session directory with `meta.toml`, `transcript.jsonl`, `inbox/`. Config is daemon-global and reloadable. Client orientation/help is read from a per-run tmpfs directory on every spawn.

## 2. Findings

### F1. `max_live_sessions` can be exceeded under concurrent create/resume — CONFIRMED
**Where:** `coordinator.py:414-460` (`_ensure_slot`), `coordinator.py:820-835` (`stop_session`); driven concurrently by `web/server.py:280-281` (`_spawn` = unguarded `create_task`).

**What goes wrong:** `_ensure_slot` checks the live count and evicts a victim across an `await`, with no lock. Two actions that each need a slot interleave and both succeed, leaving `live == limit + 1` — the exact "number the daemon can exceed" the code comment says a limit must never be, and the OOM the cap exists to prevent.

**Interleaving:** limit 5, 5 live. Task A (`add_session`) and Task B (`resume_session`) both reach `_ensure_slot`. A computes `live==5`, selects idle victim V, `await self.stop_session(V)`. `stop_session` pops V from the manager, then `await session.stop()` — yielding *before* it sets `meta["live"]=False`. B now runs `_ensure_slot`: V's `meta["live"]` is still `True`, so B also counts 5 live, also selects V, `await stop_session(V)` (pops → None, no-op, sets stored). Both return `True`; both add a live session. One slot freed, two consumed → 6 live.

**Evidence:** `stop_session` sets `meta.update(state="stored", live=False)` (line 828) only *after* `await session.stop()` (827); `_ensure_slot` reads `meta.get("live")` (412) with nothing held across the eviction await.

**Confidence:** CONFIRMED (structural; requires two overlapping slot-needing actions, which the websocket path makes routine by dispatching every action as a detached task).

**Remedy:** a single `asyncio.Lock` (or a small slot-accounting semaphore) serializing the acquire-slot-then-activate critical section, or reserving the slot synchronously before any await. Half a day.

**Related:** README "no class of session is allowed to exceed the number"; the OOM history in `_ensure_slot`.

### F2. No per-session serialization: stop / resume / revert / delete race an in-flight turn — CONFIRMED (class), consequences vary
**Where:** whole coordinator; `web/server.py:244-278` fires every action as `create_task` with no per-session queue; `engine/session.py:239` `_busy` only guards two *prompts*, not coordinator-level teardown.

**What goes wrong:** `_busy` prevents two concurrent prompts on one `AgentSession`, but nothing prevents `delete_session`/`stop_session`/`revert_session` from running *while* a turn is awaiting `conn.prompt`. `revert_session` (882-903) pops and stops the session mid-turn, truncates the transcript, and `rewrite_transcript`s it; the dying prompt then raises, and its error `notice` (a `_REPLAYABLE` event) is `append_event`'d *onto the freshly rewritten transcript* — a stray line after the rewrite. `delete_session` racing a turn silently drops the in-flight reply (the `notice`/`turn_ended` land after `_metadata` is popped and are discarded by the `session_id in self._metadata` guard in `_emit`), so the user's answer vanishes with nothing logged.

**Evidence:** `_emit` gates recording on `session_id in self._metadata` (165); `delete_session` pops metadata (844) after `await session.stop()` (843); `revert_session` rewrites the transcript (899) with the old turn's error emission still pending.

**Confidence:** CONFIRMED that these run unserialized; the transcript-corruption-on-revert interleaving is CONFIRMED, the lost-reply-on-delete is the benign end of the same class.

**Remedy:** one lock per session id held by every mutating coordinator method (the same lock as F1), so lifecycle transitions and turns cannot interleave. This is the structural fix for the whole "concurrent transition" family. A day.

### F3. `meta.toml` is written non-atomically; a torn write drops the whole session on restart — CONFIRMED
**Where:** `storage.py:46-49` (`write_meta` uses `write_text`) vs `storage.py:62-68` (`rewrite_transcript` uses tmp + `replace`).

**What goes wrong:** the transcript rewrite is atomic (temp file, `replace`) but the metadata write is not. A crash or OOM-kill mid-`write_text` leaves a truncated `meta.toml`. On restart, `load_all_meta` catches `TOMLDecodeError` and **skips that session entirely** (`storage.py:143-144`), so the session disappears from every listing while its transcript sits orphaned on disk. `_persist_meta` runs on *every* replayable event, so the window is open continuously during active use.

**Evidence:** `write_meta`: `session_dir.joinpath(META_FILENAME).write_text(_to_toml(meta))`; `load_all_meta` skips on decode error; the coordinator persists meta from inside `_emit` (168-169).

**Confidence:** CONFIRMED.

**Remedy:** give `write_meta` the same temp-then-`replace` treatment `rewrite_transcript` already uses. One hour.

### F4. Turn boundaries are never persisted, so an interrupted turn is invisible after restart — CONFIRMED (structural), matches wishlist
**Where:** `coordinator.py:19` `_REPLAYABLE` excludes `turn_started`/`turn_ended`; `engine/session.py:258-299`.

**What goes wrong:** the user's prompt is persisted (a `message` event), and agent chunks up to the crash are persisted, but the *fact that a turn was open* is not (`turn_started`/`turn_ended` aren't replayable). A daemon restart or eviction mid-turn therefore leaves a persisted user message with a partial or absent reply and no marker that work was in flight. The next send opens as if nothing happened. This is exactly the wishlist "Tell a session when its turn was interrupted," and the structural reason it's hard today is that turn state lives only in `AgentSession._busy`/`_turn` in memory, never on disk.

**Confidence:** CONFIRMED as a persistence gap.

**Remedy:** persist a turn-open marker (or make `turn_started`/`turn_ended` replayable) so restart can detect an unclosed turn and inject the interrupted-turn notice through the existing `_pending_context` channel. Half a day, and it directly enables the wishlist item.

**Related:** wishlist "Tell a session when its turn was interrupted."

### F5. Events have no per-client backfill; a brief disconnect loses everything sent while away — PLAUSIBLE
**Where:** `engine/events.py` (unbounded queues, membership = subscription window); `web/server.py:218-241`.

**What goes wrong:** a subscriber only receives events published while its queue exists. On reconnect it gets `snapshot()`, which is metadata only — no transcript, no in-flight reply. Any `message`/`turn_ended` emitted during the disconnect is gone for that client unless it explicitly re-opens the session (`open_session` → `transcript_reset`). For Telegram, a reply produced during a websocket blip is silently dropped. Secondarily, the queues are unbounded, so a stalled consumer grows daemon memory without limit — dangerous on the 1 GB host the cap already exists for.

**Confidence:** PLAUSIBLE for specific reply loss (depends on client reconnect logic in the bot, outside this area); CONFIRMED that no sequence/replay mechanism exists and queues are unbounded.

**Remedy:** either bound the queues with an overflow signal, or carry a monotonic event sequence so a reconnecting client can request a backfill from the transcript. Multi-day if done fully; bounding the queue is an hour.

**Related:** possibly the buglist "extra 'Working…' after the reply" and lost-reply reports.

### F6. Sending to a still-starting session surfaces a spurious "agent error" — PLAUSIBLE
**Where:** `coordinator.py:702-722` (`send`), `engine/session.py:231-276`.

**What goes wrong:** if a second `send` arrives after the first has added the `AgentSession` to the manager but before `resume`/`start` finished, the second `send` fetches the session and calls `session.send` while `self._conn is None`, raising `AttributeError`, caught and reported to the user as `agent error: ...`. Rare (the window between `sessions.add` and the first `await` is small) but reachable under the same detached-task dispatch.

**Confidence:** PLAUSIBLE.

**Remedy:** subsumed by the per-session lock (F1/F2); alternatively gate `send` on a "ready" state. Covered by F2.

## 3. Seams

The cleanest cut line is **orientation/help/registration**: `_orientation*`, `_help_index`, `_client_registrations`, `_read_orientation`, `_role_orientation`, `_context_prompt`, and the coupling to `state.clients_dir()` (roughly `coordinator.py:572-700`) are a self-contained "what a session is told" concern that reads client files off disk on every spawn. It touches only `_metadata[id]["oriented"]/["roles"]` and could move to its own module behind a `build_orientation(session_id)` call — safe, because it has no lifecycle state.

The **file store / inbox** (`add_file`/`remove_file`/`clear_files`) is already delegated to `storage` and is a thin pass-through; it can leave the coordinator entirely.

The unsafe seam is **the parallel dicts**. Splitting the coordinator without first collapsing the twelve id-keyed dicts into one `Session` record is dangerous: every method assumes they move together, and any partial update (e.g. metadata popped but `_usage` retained — see the slightly inconsistent cleanup lists in `stop_session` vs `delete_session`) is a latent leak. Introduce the record first, then decompose.

## 4. Test coverage

`tests/test_falconfox_poc.py` covers the coordinator's *steady-state* logic well: the live-session cap selection (`LiveSessionCapTests` — LRU-idle victim, working sessions never evicted, infrastructure not privileged, queued send holds text), persistence round-trip and the hidden/ephemeral flags, the file store's lifetime and path-safety, the turn as a first-class fact (`EngineTurnTests`), and the empty-permission denial.

What is **not** exercised is precisely the risky part: every test drives the coordinator single-threaded, so **no concurrency is tested at all** — F1 (slot race), F2 (transition vs turn) and F6 have no coverage, and the cap tests stub `stop_session` to a synchronous no-op, hiding the eviction await that F1 exploits. Crash consistency (F3) is untested — no test tears a `meta.toml` or asserts atomic replacement. Interrupted-turn persistence (F4) and event backfill across reconnect (F5) are untested. `resume_session`'s stale-`acp_id` fallback and the real subprocess spawn path are not covered (the engine tests use a `FakeConn`).

## 5. Questions for the lead

- Is the daemon assumed single-writer in practice (one Telegram bot, serialized), making F1/F2 latent-but-rare, or do multiple clients / the CLI issue overlapping actions? The websocket dispatch is built to fan out concurrently, which argues the races are reachable.
- For F5, does the Telegram bot re-open every session on reconnect (pulling `transcript_reset`), or rely on live events? That determines whether disconnect-window replies are actually lost.
- Is the intended fix direction a single coordinator lock, or per-session locks? Per-session is more concurrent but multiplies the surface; given the memory constraints, a single lock may be acceptable.