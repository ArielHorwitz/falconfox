# First phone-driven VPS session (2026-08-24): environment gaps and update-loop hardening

The first session run entirely from Telegram against the VPS, and the first
to advance the case under the laptop→VPS handoff. It was not meant to be a
work session — the user asked it to orient on the case — but orienting is what
exposed the gaps, because the session tried to *use* the environment the
bootstrap had left behind.

The theme: **everything the bootstrap did not install was invisible on the
laptop, where it had accumulated by hand over months.** Two of the five items
below are missing environment; the other three are the update loop failing to
be as safe as it looked, found by asking what would happen if the first fix
went wrong.


The first Telegram-driven VPS session hit two things the bootstrap never
covered. Both are fixed; both were invisible on the laptop, where the
environment had accumulated by hand.

1. **No agent skills were installed on the VPS.** `~/.agents/skills` did not
   exist, so no session could run the casebook workflow or the falconfox
   driving instructions — the very layers the case argues carry the work.
   Invoking `/casebook` in a VPS session simply failed. The bootstrap
   checklist had no skills step because on the laptop they were already there.
   Fixed by cloning
   [agent-skills](https://github.com/ArielHorwitz/agent-skills) to
   `~/agent-skills` and running `install.sh` + `fix-claude.sh ~` (the latter
   bridges `~/.claude/skills -> ../.agents/skills`, which Claude-backed
   sessions need). Now a documented bootstrap step. Deliberately *not*
   automated in `setup.sh`: skills are the user's, not the deployment's —
   which is the same layer boundary the case defends everywhere else.

2. **The `falconfox` CLI was not on PATH.** It only existed as
   `~/falconfox/.venv/bin/falconfox`, so the skill's and the focus agent's
   instructions to run `falconfox list` / `spawn` would fail for any session
   that did not know the venv path. Two halves to the fix, because there are
   two kinds of consumer:
   - `setup.sh` now symlinks both entry points into `~/.local/bin`, which
     stock Ubuntu's `~/.profile` already puts on PATH for **login shells**
     (SSH).
   - **Agent sessions** inherit their environment from the daemon process, so
     they needed the daemon unit itself changed: `Environment=PATH=…`, with the
     home path templated in as `@HOME@` alongside the existing `@REPO@`.

   Two systemd gotchas are worth recording, both verified on systemd 249 rather
   than assumed:
   - **`environment.d` cannot set `PATH`.** An identical
     `PATH2=$HOME/.local/bin:$PATH` line in the same file expands correctly
     while the `PATH=` line is silently dropped. The unit's `Environment=` is
     the only persistent user-level lever.
   - **`%h` did not expand** in a transient unit's `Environment=`, arriving in
     the process as a literal `%h`. Hence templating the real path at install
     time instead — which also matches how the unit already gets `@REPO@`.
     Verified by running the exact rendered PATH under `systemd-run --user`:
     `falconfox` resolves from `~/.local/bin` and `claude-agent-acp` is still
     found in `/usr/bin`.

   Consequence worth noting for the dogfooding loop: a PATH change in the unit
   only reaches sessions after a **daemon restart**, which kills the requesting
   agent's own turn — so it lands via `update.sh --detach-restart` like any
   other change, and the session that asked for it is not the session that
   sees it.

3. **The rollback had a hole, found while assessing the risk of shipping the
   above.** `update.sh`'s `restart_services()` ran under `set -euo pipefail`,
   so a non-zero `systemctl restart` aborted the script *before* the health
   check — and therefore before the rollback. Tested with throwaway canary
   units, the two failure modes diverge:
   - a unit that **loads but whose process fails** → `restart` exits 0 →
     health check fails → rollback runs (the path exercised on bootstrap day);
   - a unit that **fails to load** → `restart` exits 1 → script aborted, no
     rollback, daemon down.

   The uncovered mode was exactly the one a unit-file change can cause, which
   is what this very change was. Both restart steps (and the rollback's own
   checkout/sync) now log and carry on instead of aborting, so `healthy()`
   always gets to decide. Worth stating as a principle: **the recovery path
   must not be reachable only along the success path.**

4. **The update log lost its tail** — found when the deploy above restarted
   cleanly but logged `restarting services` and nothing after it, in neither
   the log file nor journald, on a deployment that was verifiably healthy.
   Cause: `exec > >(tee -a "$LOG_FILE")`. When the script exits, systemd tears
   down the transient unit's cgroup and kills `tee` before it has read the last
   chunk out of the pipe. Note the race is `tee` **reading**, not `tee`
   flushing: a canary run ten times scored 1/5 with `stdbuf -oL` but 10/10 once
   the script closes its fds and `wait`s for `tee` to drain. Worth flagging
   because the log is the only forensic record when an update goes quiet — a
   rollback that ran and left no trace is nearly as bad as one that never ran.

5. **"typing…" did not cover the whole turn** — user-reported from the
   phone, and a miss against the PoC's own success criterion that a long
   turn should show typing rather than appear to hang. The bot started the
   indicator on the daemon's `agent_state: working`, which only arrives once
   the backend is already producing. A stored session first resumes an ACP
   subprocess, and the daemon carries *that* as `state: starting` on
   `session_updated` — an event this client does not consume at all. So the
   entire startup/resume window, the slowest part of a cold turn, was silent.
   Fixed by starting the indicator in `_forward()` when the prompt is sent,
   with the `working` handler kept as an idempotent safety net.

   Rejected while fixing: cancelling typing on an error notice. `_warn_option`
   emits `level: "error"` for non-terminal problems (a config option that
   would not apply), so that would kill the indicator on turns that are fine.

6. **The casebook skill's CLI could not run** — found while updating these very
   files. `casebook.py` imports `tomllib` (3.11+) and the skill tells agents to
   invoke it as `python3`, which on Ubuntu 22.04 is 3.10: instant
   `ModuleNotFoundError`. This is the *same trap* as bootstrap-day bug 1, where
   `requires-python` claimed 3.10 and the daemon crashed on `tomllib`. falconfox
   itself is immune because uv provisions its own interpreter; anything invoked
   as plain `python3` is not. Fixed with `~/.local/bin/python3 ->
   /usr/bin/python3.12` (already present — it is what the falconfox venv uses),
   so no download and `/usr/bin/python3` is untouched.

   Trade-off taken knowingly: `~/.local/bin` is first on the daemon's PATH, so
   every agent session's `python3` is now 3.12 and no longer sees apt's
   `python3-*` site-packages (installed for 3.10). Nothing in falconfox invokes
   `python3` via PATH — daemon, bot and deploy scripts all use absolute venv
   paths — so the blast radius is ad-hoc scripting inside sessions, where a
   modern interpreter is the better default anyway. Reversible with
   `rm ~/.local/bin/python3`.

   Generalisation worth carrying: **an Ubuntu 22.04 LTS host means the bare word
   `python3` is 3.10.** Any tool documented as "run it with python3" needs
   checking, not assuming.

7. **A Telegram read timeout silently destroyed an agent's reply** — the worst
   bug of the session, and it ate this session's own case-update report before
   it was found. `_json_request` classifies `HTTPError` and `URLError` as
   `ApiError`, but a *read* timeout raises `TimeoutError`, which urllib does not
   wrap in `URLError` the way it wraps a connect failure. It therefore escaped
   `_poll_telegram`'s `except ApiError`, killed the polling task, and
   `asyncio.wait(FIRST_COMPLETED)` read that as the connection dying — so a
   hiccup on the **Telegram** side tore down the **daemon** websocket, and
   `_reset_connection_state()` wiped `_turn_chat` / `_reply_parts` with the
   turn's accumulated text in them. The log even blamed the daemon
   (`daemon connection lost`) while the daemon was healthy throughout: two
   teardowns inside one long turn, 14:35:17 and 14:38:09.

   Two fixes, because there are two failures. The timeout is now classified as
   `ApiError`, so the existing retry handles it and nothing is torn down. And
   `_reset_connection_state()` now returns the chats it interrupted, so a
   dropped connection **tells the chat its reply is gone** instead of leaving it
   waiting forever — the session still holds the turn, so the content is
   recoverable, but only if you know to ask.

   The lesson generalises past this bug: **silent loss is worse than an error.**
   The reconnect loop added during the AFK run was written to make restarts
   survivable, and it does — but "the connection recovered" was quietly allowed
   to mean "and the turn in flight simply vanished". A recovery path that
   discards data needs to say so.

## State at the end of the session

All five fixed, committed, pushed to `origin/falconfox`, and live on the VPS
at `1765dc2` through two staged deploys. Verified after the final restart:
services active, `falconfox` resolving by name, health check logged with its
tail intact — the last of which is itself the confirmation that finding 4 is
fixed, since the earlier restart lost exactly that line.

The deploy procedure that emerged, and which is worth reusing for any
unit-file change: fast-forward the deploy checkout and run `setup.sh
install-units` **without restarting**, confirm `systemctl show` reports the new
unit contents (proving systemd parsed it while the old process still serves),
then schedule the restart detached via `systemd-run --on-active`, running
`update.sh --restart-phase --rollback-to <rev>` so the health check and
rollback outlive the agent turn that triggered them. Note that
`--detach-restart` is *not* the right entry point once the checkout has already
been fast-forwarded by hand: its `apply_update` finds nothing to do and exits
before scheduling anything.
