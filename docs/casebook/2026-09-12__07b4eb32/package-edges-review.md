# Package review: the wire contract, deploy scripts and copy

Independent read-only review by an Opus 4.8 delegate, 2026-09-13, of the six commits on `hardening-edges`. Verdict: merge with one README fix. The lead applied both findings directly and folded them into the commits they belong to.

Review complete. Findings below.

---

# Review: the wire contract and deploy branch

I verified the range `hardening-survey..hardening-edges` (6 commits) against the source, ran the new tests and the full suite (both green: 353 passed, 48 subtests), and checked the report's claims rather than trusting them. The refactor is sound: action parity holds on both transports, the two `send` contracts are preserved and now tested, the attach HTTP↔WS round trip is intact, malformed bodies are now a 400 rather than a 500, the unknown-action event is handled by the bot above its session guard, the protocol check is once-per-connection and degrades on a header-less daemon, and the deploy rollback re-renders before it health-checks so it never compares against new-checkout units. Two findings, both minor.

## Findings

### 1. README claims a `help` behaviour the code does not have — CONFIRMED
`README.md:81`. The rewritten CLI paragraph says `help` "lists more in a session than in a bare shell." It does not. `cmd_help` (`cli.py:212-243`) reads the listing from the daemon's published `clients_dir` via `ffhelp.index(run_dir)`; the output depends only on the daemon running and what clients registered on disk, never on `FALCONFOX_SESSION_ID` or any in-session context. Its own docstring says "the daemon adds nothing to them" and that being in a session only makes the daemon's presence "true by construction." Failure scenario: a user reads the README, runs `falconfox help` in a bare shell with the daemon up, and sees the identical listing they would see inside a session — the documented difference is absent. Documentation only, but point 6 of the brief makes any untrue statement a finding. (Every other README claim I checked — the two-message turn, the typing indicator, `--role` repeatable, finite timeouts, `tag` clears, `attach` semantics — holds.)

### 2. `check-units` matches `FragmentPath` by exact string — PLAUSIBLE
`deploy/setup.sh:72`. `healthy()` gates on `check-units`, which requires `systemctl show -p FragmentPath` to equal `"$UNIT_DIR/$unit"` character for character. On a host where systemd reports a path that differs textually but not semantically — a symlinked `$HOME`, or any canonicalisation of `~/.config/systemd/user` — the comparison fails, `check-units` returns 1, and the deploy calls itself unhealthy and rolls back. Failure scenario: once `master` is past this commit (so the rollback target also carries `check-units`), such a host fails the check on both the forward and the rollback render and lands on "manual intervention (SSH) required" on every deploy. The first adoption is safe (the pre-change rollback target has no `check-units`, so `healthy()` skips it and falls to liveness), and a normal VPS `$HOME` matches, so this is low probability. It is also untestable as written: the fake `systemctl` echoes `$loaded/$unit` built from the same `$HOME`, so it matches by construction and never exercises a divergent real `FragmentPath`. The stamp/`NeedDaemonReload`/loaded-text checks are well chosen; only the path equality is brittle. Consider comparing resolved paths (`realpath`) or membership rather than string equality.

## Notes (not findings)

- **Behaviour change, intended:** HTTP `stop`/`delete` of a session the coordinator then removes now answers `200 {"deleted": id}` where it used to be a `400` error (`server.py:_http_action`, the `get_session` fallback). This is the tested fix for "stopping a never-renamed session," and the CLI treats it as success. Correct, but it is a user-visible wire change the commit messages do not call out.
- **`revert`/`fork`/`set_config_option` are now reachable over HTTP too**, not just the websocket, because they carry the default `session=True` and fall under `/api/sessions/{id}/{action}`. No live client sends them, and the case decision is to keep them registered until the dead web UI leaves, so this is harmless — just noting they gained a transport rather than staying WS-only.
- **WS `attachment_result` and `open` now run detached** (`_spawn(_invoke(...))`) where `_dispatch` ran them inline. The attach future is still set, one loop tick later; no correctness impact, and detaching means a raising `resolve_attachment` no longer tears the socket down.
- **XDG_CONFIG_HOME:** `setup.sh` no longer honours it for `UNIT_DIR`/`CONFIG_DIR`. The remaining references (`config.py` app config, the dev-twin `Environment=` in the dev units, `deploy/README.md`) are all correct and unrelated to the deploy-path bug.
- **Restart warning:** first miss is INFO ("waiting for the daemon"), a miss after a prior success is WARNING, and orientation still re-registers on retry (`bot.py:_clients_dir`, `_register_orientation`). The flag never resets, but on a reconnect `server.json` is already written by the time the socket accepts, so the WARNING only fires when the directory is genuinely gone. Correct.

## Verdict

**Merge with fix 1** (a one-line README correction). Finding 2 is a real but low-probability robustness gap on the deploy health check; I would take it as a follow-up rather than a blocker, since the deployment host is a known plain-`$HOME` VPS and first adoption is safe. Nothing here breaks a client, loses parity, or can leave the host worse off than the liveness-only check it replaces.