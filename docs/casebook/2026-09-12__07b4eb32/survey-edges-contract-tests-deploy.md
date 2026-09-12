# Survey: the edges (API contract, CLI, tests, deployment)

Read-only survey by a delegated Opus 4.8 agent on 2026-09-12 against `dev` at 9b43889, from the brief in [survey-briefs.md](survey-briefs.md). Confidence labels are the surveyor's own; the lead's spot checks are recorded in [overview.md](overview.md).

## Map

The daemon exposes **one surface over two transports that are authored separately**. `web/server.py` defines HTTP routes (`/api/sessions…`, `/api/backends`, `/api/reload`, etc.) whose handlers call `SessionCoordinator`, and a `/ws` websocket that (a) sends a `snapshot` then streams every `coordinator.bus` event, and (b) receives client-initiated actions through `_dispatch`. The HTTP action set (`session_action`, server.py:106) and the WS action set (`_dispatch`, server.py:244) are two hand-maintained switch statements.

Two live consumers: `cli.py` talks **only HTTP** (`_request`, blocking `urllib`); the Telegram `DaemonApi` (`api.py`) talks HTTP for most calls **but the bot sends `send` and `attachment_result` over the WS** (bot.py:2611, 2040) while receiving the event stream there too. So "the contract" lives in four places and no schema binds them.

State owned here: `state.py` holds `server.json` (pid, port, `clients_dir`) and the run directory; the CLI and bot discover the daemon through it. Ports are **pinned** in the units (9721 stable, 9725 dev); `find_available_port` is the unpinned fallback. Version is produced by `get_version` (live git, else the `hatch_build.py` stamp) and announced by the bot on connect.

Lifecycle: `serve()` → `create_app()` → lifespan writes `server.json`, starts the watchdog, and on shutdown calls `coordinator.shutdown()`. The bot's `async for websocket in connect(...)` loop survives daemon restarts; each reconnect re-registers orientation, re-announces, and reconciles the persisted turn map against a fresh `daemon.sessions()` call. Events missed while disconnected are not replayed — session state comes from the snapshot, and an in-flight turn's undelivered text is rebuilt from the transcript via a `consumed` byte offset.

## Findings

### 1. The action contract is authored three times and has silently diverged
**Where:** `server.py:106-142` (HTTP), `server.py:244-278` (WS `_dispatch`), `cli.py:34-52`, `api.py:118-184` + `bot.py:2611`.
**What goes wrong:** The same action name means different things per transport, and neither surface knows what the other carries. `send` exists on both — but HTTP `send` **awaits the whole turn** (`await coordinator.send`) while WS `send` is fire-and-forget so events can stream. The bot uses WS `send`; the CLI uses HTTP `send`. `tag` and `attach` are HTTP-only. `set_config_option`, `revert`, `fork` are **WS-only and reachable by no live client** — only the dead web UI ever sent them, so the live daemon still dispatches dead actions. An unknown action is a silent no-op on WS (`log.warning("ignoring…")`) and a 404 on HTTP. Adding or renaming one action is a 4-site edit, and a mismatch surfaces as nothing happening rather than as an error.
**Evidence:** two disjoint switch statements; `bot.py:2611` sends `send` over `self._ws`; `cli.py:230` posts it over HTTP; no `revert`/`fork`/`set_config_option` in either client.
**Confidence:** CONFIRMED.
**Remedy:** define the action set once (a dict of name → handler/coro shared by both entrypoints), let each transport adapt arguments and response, and make an unknown action an error on both. ~1 day.

### 2. The CLI makes unbounded blocking calls (`timeout=None`), so a wedged daemon hangs the agent
**Where:** `cli.py:41`.
**What goes wrong:** agents run `falconfox send/attach/list` from inside sessions. HTTP `send` blocks until the turn finishes, and `attach` blocks in the coordinator until the **bot answers `attachment_result` back over the WS** — a round-trip that spans both transports and a second process. With `timeout=None`, any stall (the documented Telegram/socket hangs, a wedged bot that received the attach event but never acks, a daemon at its session cap) hangs the agent's command forever with no output. This is the same failure class the bot already hardened against (`api.py:96-103`, the read-timeout teardown), left open on the CLI.
**Evidence:** `with urllib.request.urlopen(request, timeout=None)`; `attach` waits on the client ack per README "Sending files out of a session."
**Confidence:** CONFIRMED (code); PLAUSIBLE that it has bitten in practice.
**Remedy:** a finite default read timeout, longer for `send`/`attach`, and a clear CliError on timeout. Hours. Related: buglist "Telegram calls … hang until the read timeout."

### 3. `install-units` runs on every restart including rollback, so a deploy can report healthy on the wrong unit text
**Where:** `update.sh:45-48, 82-100`, `setup.sh:15-16`.
**What goes wrong:** this is the structural parent of the buglist entry "`setup.sh install-units` writes units where systemd will not look." `UNIT_DIR`/`CONFIG_DIR` honour `XDG_CONFIG_HOME`, which is a *per-caller* variable (every dev session sets it) being used for a *$HOME-relative deployment* fact — so an agent-run `update.sh --detach-restart` renders units into a directory systemd never reads, exits 0, and the old unit text survives. The sibling the buglist hints at: `restart_services` re-renders units on **every** path, forward and rollback, and `healthy()` tests only that the daemon answers and the bot unit is active — never which unit text is loaded. A unit-file change (or its rollback) can therefore be reported healthy while the running unit is stale.
**Evidence:** `restart_services` called from both `restart_phase` branches; `healthy()` at `update.sh:50-61` checks liveness only.
**Confidence:** CONFIRMED.
**Remedy:** make the two paths `$HOME`-relative (drop the env var, per the buglist), and have the health check assert unit provenance (compare rendered vs loaded, or stamp a version the daemon reports). ~half a day. Related: buglist entry above.

### 4. The wire surface has no test, so the divergence in #1 cannot be caught
**Where:** `tests/test_falconfox_poc.py` — no `create_app`/`TestClient`/`_dispatch`/`_run_socket` reference; `DaemonApi` is exercised once with a patched `_json_request` (test:2452-2460).
**What goes wrong:** routing, HTTP status/error-shape mapping (`{"error": …}` vs Telegram's `{"description": …}`, handled in `api.py:88`), and the WS snapshot+dispatch path run untested. Clients are tested by driving the bot object directly with fabricated event dicts, bypassing the real socket. So the two action surfaces can drift (they have, #1) with a green suite.
**Confidence:** CONFIRMED.
**Remedy:** one Starlette `TestClient` test per route plus a WS snapshot/dispatch test; once the contract is single-sourced (#1) this becomes a table test over the action set. Half a day. This is most of what makes the server safe to refactor.

### 5. README and `FakeTelegram` still describe reactions the client removed
**Where:** `README.md:115-117`, `tests/test_falconfox_poc.py:573` (`FakeTelegram.set_reaction`); reactions removed `bot.py:57-58` ("reactions are gone, user decision 2026-09-12").
**What goes wrong:** the public README documents the 👀/🫡/👌/💔/😱 reaction system as a current feature of the only client; it no longer exists. The fake advertises a `set_reaction` method the real `TelegramApi` does not have (`api.py` has none), so a test can pass against an interface that is not real. This is the concrete form of "fakes drift from the real interface."
**Confidence:** CONFIRMED.
**Remedy:** delete the README section and the dead fake method; longer term, build the fake by asserting its method set against `TelegramApi`'s so drift fails a test. Hours.

### 6. `EventBus` queues are unbounded; a wedged WS consumer grows daemon memory without bound
**Where:** `engine/events.py:25-27`, `server.py:234-241`.
**What goes wrong:** `publish` never blocks and each subscriber queue is unbounded. Backpressure exists only on the drain (`send_json`). If the single bot consumer's socket wedges mid-turn (the known hang class), the daemon keeps enqueuing every chunk event with no ceiling. Loopback makes this low-probability but not impossible.
**Confidence:** PLAUSIBLE (not traced to an observed incident).
**Remedy:** bound the queue and on overflow drop the slow subscriber (disconnect forces the bot's reconnect+snapshot+transcript recovery, which already exists). Hours.

### 7. Version is produced but never checked at runtime
**Where:** `__init__.py:14` (`get_version`, `@lru_cache`), `bot.py:856-863`.
**What goes wrong:** the bot announces the daemon's version cosmetically; nothing gates the wire contract on it. Today skew is transient (both units restart together) and pinned ports prevent cross-instance talk, so consequence is low. But a future wire change between a long-lived client and a restarted daemon would fail as a silent no-op (#1), not a refused handshake.
**Confidence:** CONFIRMED (absence).
**Remedy:** send a protocol version in the WS snapshot and let the client warn/refuse on mismatch. Hours. Pair with #1.

## Seams

The cleanest cut is **a single action registry** between the transports and the coordinator: both HTTP and WS become thin adapters (parse args, shape response), and the CLI/DaemonApi consume a generated list. Safe because the coordinator methods already are the real boundary; the switch statements add nothing but divergence. A second clean seam is **state.py**, which is already self-contained (server discovery, ports, run dirs) and is the one piece with direct tests. Unsafe to cut without care: the **attach round-trip**, which deliberately spans HTTP (request) and WS (ack) through coordinator-held futures — splitting the transports naively would strand that future. The **deploy scripts** are separable from the Python entirely and should be treated as their own unit with their own (shell) tests.

## Test coverage

Exercised in my area: `CliSafetyTests` (self-target guard), `DaemonPortTests` (pinned vs searched port, `find_available_port`), `VersionTests` (`get_version` precedence), `UploadKindTests` (client upload-kind choice), and `_json_request`'s timeout-to-`ApiError` wrapping (test:1215-1221). `DaemonApi` URL construction is checked once with a patched request (2452).

**Not exercised — the risky parts:** the HTTP routing table and its error/status mapping; the WS `snapshot`+`_dispatch` path; the fact that `send` means different things per transport (#1); `update.sh`/`setup.sh` behaviour (no shell tests at all — rollback, `install-units` on every path, the `XDG_CONFIG_HOME` bug); and the attach HTTP↔WS round-trip end to end. The fakes are hand-mirrored and already drift (#5).

## Questions for the lead

1. Is `send`-over-WS vs `send`-over-HTTP (async vs blocking) a deliberate dual contract to preserve, or an accident to collapse? It drives whether #1 is a unification or just a dedup.
2. Are `revert`/`fork`/`set_config_option` meant to return with a future client, or delete with the dead web UI?
3. Do you want shell-level tests for `deploy/`, or is that out of scope for "hardening the edges"?