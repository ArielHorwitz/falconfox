# Deploying FalconFox on a VPS

Runs the daemon and the Telegram bot as **systemd user units** with automatic
restart, so the whole stack survives crashes, daemon restarts (which always
take the bot down — it has no reconnect loop), and VPS reboots. After the
one-time bootstrap below, all further work — including developing FalconFox
itself — happens through Telegram; the only reason to SSH back in is a failed
update that could not roll itself back.

## Prerequisites (once, as root or with sudo)

On an apt-based VPS, run `deploy/provision.sh` as root (copy the script over,
or run the manual equivalent): git, curl, Node.js + npm (>=18),
[uv](https://docs.astral.sh/uv/), and
`npm install -g @agentclientprotocol/claude-agent-acp`.

## Bootstrap (once, as the deploy user)

```sh
git clone -b falconfox git@github.com:ArielHorwitz/falconfox.git ~/falconfox
mkdir -p ~/.config/falconfox
cp ~/falconfox/deploy/telegram.env.example ~/.config/falconfox/telegram.env
cp ~/falconfox/deploy/config.example.toml ~/.config/falconfox/config.toml
# fill in telegram.env (bot token + the two chat ids)
# paste a `claude setup-token` token into config.toml
~/falconfox/deploy/setup.sh
```

`setup.sh` is idempotent: it syncs the venv, installs and starts both units,
symlinks the `falconfox` / `falconfox-telegram` CLIs into `~/.local/bin`,
enables lingering (so units run without a login session), and health-checks.

Then install the agent skills, which is what makes sessions able to run the
casebook workflow and drive the daemon:

```sh
git clone https://github.com/ArielHorwitz/agent-skills ~/agent-skills
~/agent-skills/install.sh        # -> ~/.agents/skills
~/agent-skills/fix-claude.sh ~   # ~/.claude/skills -> ../.agents/skills
```

Skills live outside the deploy checkout because they are the user's, not the
deployment's — `setup.sh` deliberately does not install them. The
`fix-claude.sh` bridge is required for Claude-backed sessions, which only read
`.claude/`. Update path: `git pull` in the clone, then `install.sh --upgrade`.

The `casebook` skill's CLI needs `python3` to be 3.11+ (it imports `tomllib`).
Ubuntu 22.04's `python3` is 3.10, so shim a modern one onto PATH ahead of it:
`ln -sfn /usr/bin/python3.12 ~/.local/bin/python3`. This does not touch
`/usr/bin/python3`; it does mean sessions no longer see apt's `python3-*`
modules, which nothing here depends on.

Notes:

- **Stop any other poller on the same bot token first** (e.g. the laptop bot):
  Telegram allows one `getUpdates` consumer per token and gives the rest 409s.
- To **push** work from VPS sessions, the deploy user needs a GitHub-registered
  SSH key and git identity (`user.name` / `user.email`).
- The CLI is **not** on PATH by default. `setup.sh` shims it into
  `~/.local/bin` (which the stock Ubuntu `~/.profile` adds for login shells),
  and the daemon unit sets `Environment=PATH` so agent sessions — which
  inherit the daemon's environment — can invoke `falconfox` by name. PATH
  cannot be set via `environment.d`; systemd ignores that one variable.
- The clone location is free (units are rendered with the real path); `-b
  falconfox` pins the deploy branch — switching the checkout's branch later
  changes what `update.sh` follows.

## Updating (the dogfooding loop)

Development happens in `.worktrees/` inside the deploy checkout (or anywhere
else), gets merged to the deploy branch, and pushed to origin. The deploy
checkout itself must stay clean — `update.sh` refuses to run otherwise. Python
loads code only at process start, so the running daemon is untouched until the
restart.

- **From a FalconFox agent session (Telegram):**
  `~/falconfox/deploy/update.sh --detach-restart` — pulls and syncs inline,
  then restarts *detached* a few seconds later, because the restart kills the
  daemon and with it the agent's own turn. The agent should announce the
  update and end its turn; the session itself survives and resumes on the next
  message.
- **From SSH:** `~/falconfox/deploy/update.sh` — everything inline.

After restarting, the script health-checks (daemon answers `falconfox list`,
bot unit active). On failure it **rolls back** to the previous revision,
re-syncs, restarts, and re-checks. Unit-file changes deploy too (units are
re-rendered on every update). Everything is appended to
`~/.local/state/falconfox/update.log` — the first thing to read after an
update went quiet. Deeper forensics: `journalctl --user -u falconfox-daemon`
/ `-u falconfox-telegram`, and `~/.local/state/falconfox/falconfox.log`.

A restart step that fails is logged and does **not** abort the update — the
health check and rollback are what recover from it. This matters for unit-file
changes specifically: a malformed unit makes `systemctl restart` exit non-zero,
which under `set -e` would otherwise skip rollback entirely.

Known limits: the health check can miss a bot that crashes slowly (it samples
`is-active` once after a settle delay), and an in-flight turn at restart time
is always lost. Stored sessions resume with transcripts intact; the bot reuses
its pointer file across restarts.
