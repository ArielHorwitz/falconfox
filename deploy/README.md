# Deploying FalconFox on a VPS

Runs the daemon and the Telegram bot as **systemd user units** with automatic
restart, so the whole stack survives crashes, daemon restarts (which always
take the bot down — it has no reconnect loop), and VPS reboots. After the
one-time bootstrap below, all further work — including developing FalconFox
itself — happens through Telegram; the only reason to SSH back in is a failed
update that could not roll itself back.

## Prerequisites (once, as root or with sudo)

- git, [uv](https://docs.astral.sh/uv/), Node.js + npm
- `npm install -g @agentclientprotocol/claude-agent-acp`

## Bootstrap (once, as the deploy user)

```sh
git clone -b falconfox git@github.com:ArielHorwitz/casebook.git ~/falconfox
mkdir -p ~/.config/falconfox
cp ~/falconfox/deploy/telegram.env.example ~/.config/falconfox/telegram.env
cp ~/falconfox/deploy/config.example.toml ~/.config/falconfox/config.toml
# fill in telegram.env (bot token + the two chat ids)
# paste a `claude setup-token` token into config.toml
~/falconfox/deploy/setup.sh
```

`setup.sh` is idempotent: it syncs the venv, installs and starts both units,
enables lingering (so units run without a login session), and health-checks.

Notes:

- **Stop any other poller on the same bot token first** (e.g. the laptop bot):
  Telegram allows one `getUpdates` consumer per token and gives the rest 409s.
- To **push** work from VPS sessions, the deploy user needs a GitHub-registered
  SSH key and git identity (`user.name` / `user.email`).
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

Known limits: the health check can miss a bot that crashes slowly (it samples
`is-active` once after a settle delay), and an in-flight turn at restart time
is always lost. Stored sessions resume with transcripts intact; the bot reuses
its pointer file across restarts.
