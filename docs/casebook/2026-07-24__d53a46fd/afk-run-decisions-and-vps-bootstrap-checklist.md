# AFK run (2026-08-24): decisions to review + VPS bootstrap checklist

The user went AFK with a broad mandate: get everything ready for the VPS
migration, frontload whatever possible, and report the decisions taken. This
file is that report. Everything below is implemented, tested, committed on the
`falconfox` branch, and pushed.

## Decisions made while AFK — review these

1. **Implemented Telegram markdown rendering now** (was filed as "later" in
   [telegram-markdown-parse-mode-finding.md](telegram-markdown-parse-mode-finding.md)).
   Rationale: it is the single biggest phone-UX gap for dogfooding, and fixing
   it after migration would mean reading raw markdown until then. Design as
   the finding recommended: convert agent markdown → **Telegram HTML** (never
   MarkdownV2), with a **plain-text fallback** when Telegram rejects entities,
   in a new `falconfox_telegram/rendering.py`.
   Sub-decisions:
   - **"One message per turn" relaxed to "as few messages as possible":** long
     turns now split at block boundaries into ≤4096-char messages instead of
     being truncated. Truncation destroyed content; splitting only costs
     notifications (and only for long turns).
   - Headings → bold lines; tables and fenced code → `<pre>` (monospace);
     giant code blocks split across multiple `<pre>` messages.
   - **Link previews disabled** on rendered replies to keep the chat compact.
   - Plain senders (notices, `/list` output, fallbacks) still truncate.
2. **Added a websocket reconnect loop to the bot** (the `async for … in
   connect()` retry idiom). The bot now survives daemon restarts —
   **verified live**: daemon stopped and restarted under a running bot; the
   same bot process reconnected and respawned its focus session in ~3s. This
   makes agent-driven self-updates far less fragile; systemd `Restart=always`
   remains as the backstop. On reconnect the focus session is rotated (old one
   deleted best-effort — after a daemon restart that delete fails with a
   logged warning traceback; expected, not a crash).
3. **Rewrote the focus-agent orientation** per the live-transcript findings
   ([focus-agent-live-transcript-and-instruction-gaps.md](focus-agent-live-transcript-and-instruction-gaps.md)):
   - The bot now materializes **both `AGENTS.md` and `CLAUDE.md`** in the
     focus workspace, plus a **`.claude/skills` → `.agents/skills` symlink**,
     so Claude-backed sessions load the role natively at start.
   - The orientation and skill now **explicitly permit spawn-then-focus**
     (`falconfox spawn` when no session matches) — the live run showed the
     agent had to override its own mandate to do what the user wanted.
   - Greeting guidance: ask "which session to focus", never "what would you
     like to work on".
4. **`deploy/provision.sh`** added for the root-level VPS prerequisites
   (apt-based distros; errors out with manual instructions elsewhere).
   Decision: `uv` installed system-wide to `/usr/local/bin` for simplicity;
   node must be ≥18 or the script refuses.
5. **Security posture unchanged**: always-allow, loopback daemon, outbound
   Telegram only — per the earlier discussion, no auth/ports for the PoC.
6. **The laptop stack is stopped** (daemon + bot) so the bot token is free
   for the VPS — only one `getUpdates` poller may exist per token. The two
   stored laptop sessions remain on the laptop's state dir; the VPS starts
   with a fresh session list.

## Verification performed

- 11/11 unit tests pass, including new rendering tests (markdown → HTML
  constructs, escaping, table monospacing, long-turn splitting, giant code
  blocks) and the updated turn test (typing + single rendered reply).
- Live reconnect test as described above.
- The AFK-completion Telegram ping was sent through `render_messages` +
  `sendMessage parse_mode=HTML` — a live test of the rendering path.
- Not live-tested (needs a real phone-side turn): the full
  message→agent→rendered-reply loop; the first real VPS turn will show it.

## VPS bootstrap checklist (the manual part, one time)

1. As root on the VPS: run `deploy/provision.sh` (copy it over first, e.g.
   `scp deploy/provision.sh vps:` — it's self-contained).
2. As the deploy user (a dedicated non-root user recommended):
   ```
   git clone -b falconfox git@github.com:ArielHorwitz/casebook.git ~/falconfox
   mkdir -p ~/.config/falconfox
   ```
   (For a private repo the VPS needs a GitHub-registered SSH key first — the
   same key enables pushing work back from VPS sessions.)
3. Copy the Telegram env from the laptop (same values, extra vars harmless):
   `scp .env vps:.config/falconfox/telegram.env`
4. `cp ~/falconfox/deploy/config.example.toml ~/.config/falconfox/config.toml`
   then paste a token from `claude setup-token` (run it on the laptop — only
   the browser OAuth step needs to happen somewhere graphical; the token is
   portable and lasts a year).
5. `~/falconfox/deploy/setup.sh` — refuses to start until the two config
   files are real; then installs units, enables linger, starts, health-checks.
6. Message the bot. From then on, updating falconfox = merge to the
   `falconfox` branch, push, and run `~/falconfox/deploy/update.sh
   --detach-restart` from any session (rollback is automatic on a failed
   health check; forensics in `~/.local/state/falconfox/update.log`).

## Bootstrap executed (2026-08-24, delegated)

The user delegated the bootstrap itself; it is **done** — falconfox runs on
`lemcel` (Ubuntu 22.04) under systemd user units with linger, and the full
update loop was proven live. Deviations/additions from the checklist above:

- Node was absent and Ubuntu 22.04's candidate is node 12; `provision.sh` now
  installs NodeSource node 22 automatically (and avoids the distro `npm`
  package, which conflicts with NodeSource's bundled npm).
- A VPS ed25519 key was generated and registered to the user's GitHub account
  ("lemcel (falconfox)", approved by the user); push access verified. Git
  identity configured.
- The first boot flushed out three real bugs, all fixed through the update
  loop itself (`update.sh` on the VPS pulling from the laptop):
  1. **Python floor was wrong** — code imports `tomllib` (3.11+) but
     `requires-python` said 3.10, and Ubuntu 22.04's system Python is 3.10:
     instant crash. Floor bumped to 3.11; uv provisions a managed 3.13.
  2. **Deploy re-locking** — `uv sync` rewrote `uv.lock` on the VPS (different
     uv version), dirtying the deploy checkout and tripping the update guard.
     Deploy scripts now use `uv sync --frozen`.
  3. **CLI discovery broken under systemd** — `serve()` writes `server.json`
     only with the `FALCONFOX_DAEMON=1` marker, which only the detached
     spawner set. The unit now sets it (journald keeps console logging).
- The rollback path executed for real once. Notable: rolling back code does
  not roll back the interpreter (the venv kept Python 3.13, so the "broken"
  old revision ran fine after rollback). Also, a rollback is useless when the
  baseline itself never worked — bootstrap-day special, not a loop flaw.
- Verified on the VPS: `falconfox list` discovery, a claude-backend session
  round-trip (proves the OAuth token), Telegram bot polling, linger, push
  access, and a clean `update.sh` no-op run.

## Post-bootstrap gaps found from the first VPS session (2026-08-24)

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

## Known-open items (not blockers)

- Voice, remote CLI/web access, and the rebuilt web UI remain deferred as
  before.
- Markdown tables render monospaced but unaligned if the agent didn't align
  them; lists render as plain hyphens. Acceptable on a phone.
- The update health check samples the bot once after a settle delay — a
  slow-crashing bot can slip through (documented in deploy/README.md).
- `docs/casebook/` on this branch still carries the historical name `casebook`
  in module paths (`src/casebook/`); the full package rename remains deferred.
