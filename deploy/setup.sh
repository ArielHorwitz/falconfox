#!/usr/bin/env bash
# One-time VPS bootstrap for FalconFox: sync the venv, install the systemd user
# units, enable lingering, and start everything. Idempotent — safe to re-run.
#
#   setup.sh                     full bootstrap
#   setup.sh install-units       (re)install only the bits that live outside
#                                the checkout — unit files and CLI shims (used
#                                by update.sh)
#   setup.sh install-dev-units   render the dev instance's units from this
#                                checkout, without touching the deployment's
#                                units or the ~/.local/bin shims
#   setup.sh check-units         report whether systemd is running the unit
#                                text this checkout rendered (used by the
#                                health check in update.sh)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Both of these are $HOME-relative, and deliberately do not honour
# XDG_CONFIG_HOME. The systemd user manager was started at login and reads
# ~/.config/systemd/user whatever a later caller's environment says, and where
# the deployment keeps its config is a fact about the host in the same way.
# Honouring the variable meant that any caller with it set -- every dev agent
# session has it -- rendered units into a directory nothing reads, exited 0,
# and left the old unit text running.
CONFIG_DIR="$HOME/.config/falconfox"
UNIT_DIR="$HOME/.config/systemd/user"
BIN_DIR="$HOME/.local/bin"
DEV_BIN_DIR="$HOME/.local/state/falconfox-dev/bin"
UNITS=(falconfox-daemon.service falconfox-telegram.service)
DEV_UNITS=(falconfox-dev-daemon.service falconfox-dev-telegram.service)
# A checksum of the rendered text, written into the unit as a comment so that
# systemd hands it back. It is what lets `check-units` compare what is loaded
# against what was rendered rather than assume they are the same thing.
STAMP="# falconfox-unit-checksum: "

render_units() {
    mkdir -p "$UNIT_DIR"
    local unit text
    for unit in "$@"; do
        text="$(sed -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" \
            "$REPO/deploy/$unit")"
        { printf '%s\n' "$text"
          printf '%s%s\n' "$STAMP" \
              "$(printf '%s' "$text" | sha256sum | cut -c1-16)"
        } > "$UNIT_DIR/$unit"
    done
    systemctl --user daemon-reload
}

unit_stamp() {
    grep -m1 -o "${STAMP}[0-9a-f]*" || true
}

# Is systemd running the unit text we just wrote? Three questions, because
# each can be wrong on its own: the text systemd reads for the unit, the file
# it reads it from, and whether it has loaded the current contents of that
# file. A deploy that only checks liveness passes on stale units, which is how
# a unit-file change -- or its rollback -- can report itself healthy.
check_units() {
    local unit rendered loaded fragment reload failed=0
    for unit in "${UNITS[@]}"; do
        rendered="$(unit_stamp < "$UNIT_DIR/$unit" 2>/dev/null)"
        loaded="$(systemctl --user cat "$unit" 2>/dev/null | unit_stamp)"
        fragment="$(systemctl --user show -p FragmentPath --value "$unit" 2>/dev/null)"
        reload="$(systemctl --user show -p NeedDaemonReload --value "$unit" 2>/dev/null)"
        if [[ -z "$rendered" ]]; then
            echo "units: $UNIT_DIR/$unit is missing or carries no checksum" >&2
            failed=1
        elif [[ "$rendered" != "$loaded" ]]; then
            echo "units: systemd reads other text for $unit than $UNIT_DIR holds" >&2
            failed=1
        elif [[ "$(realpath -m "${fragment:-/nonexistent}")" != "$(realpath -m "$UNIT_DIR/$unit")" ]]; then
            # Resolved on both sides: systemd may report a canonicalised path
            # (a symlinked $HOME) that names the same file as $UNIT_DIR.
            echo "units: systemd loads $unit from ${fragment:-nowhere}, not $UNIT_DIR" >&2
            failed=1
        elif [[ "$reload" == "yes" ]]; then
            echo "units: $unit has changed on disk and has not been loaded" >&2
            failed=1
        fi
    done
    return "$failed"
}

install_units() {
    render_units "${UNITS[@]}"
}

# The dev instance is a second copy of the stack pointed at its own state,
# config and port. Its units are rendered from the same templates as the
# deployment's so that tearing it down loses nothing that has to be rewritten
# by hand -- which is exactly what happened the first time.
install_dev_units() {
    render_units "${DEV_UNITS[@]}"
    install_dev_shims
}

# Agent sessions inherit the daemon's PATH, so whichever `falconfox` is on it
# is the one an agent runs. ~/.local/bin belongs to the deployment, so without
# a shim of its own a dev session drives the dev daemon with the *deployment's*
# CLI -- and no CLI change is testable from the instance that exists to test
# them. Observed exactly once: an agent reported `falconfox attach` missing
# from a build that had it.
install_dev_shims() {
    mkdir -p "$DEV_BIN_DIR"
    local name
    for name in falconfox falconfox-telegram; do
        ln -sfn "$REPO/.venv/bin/$name" "$DEV_BIN_DIR/$name"
    done
}

# Put the CLI on PATH. ~/.local/bin is already picked up by the stock Ubuntu
# ~/.profile for login shells; agent sessions get it from the daemon unit's
# Environment=PATH (systemd ignores PATH set via environment.d).
install_shims() {
    mkdir -p "$BIN_DIR"
    local name
    for name in falconfox falconfox-telegram; do
        ln -sfn "$REPO/.venv/bin/$name" "$BIN_DIR/$name"
    done
}

if [[ "${1:-}" == "install-units" ]]; then
    install_units
    install_shims
    exit 0
fi

# No shims: `falconfox` on PATH means the deployment, never the dev twin.
if [[ "${1:-}" == "install-dev-units" ]]; then
    install_dev_units
    exit 0
fi

if [[ "${1:-}" == "check-units" ]]; then
    check_units || exit 1
    echo "units: systemd runs what $UNIT_DIR holds"
    exit 0
fi

# Anything else is a mistake, and running the full bootstrap on a VPS is not
# the place to find out: a subcommand this script does not have used to fall
# through to the whole thing, which is the one path that restarts services.
if [[ -n "${1:-}" ]]; then
    echo "error: unknown argument: $1" >&2
    exit 1
fi

command -v uv >/dev/null || {
    echo "error: uv is not installed (https://docs.astral.sh/uv/)" >&2
    exit 1
}
command -v claude-agent-acp >/dev/null || echo \
    "warning: claude-agent-acp not on PATH — npm install -g @agentclientprotocol/claude-agent-acp" >&2

mkdir -p "$CONFIG_DIR"
missing_config=0
if [[ ! -f "$CONFIG_DIR/telegram.env" ]]; then
    echo "error: $CONFIG_DIR/telegram.env is missing. Create it and fill it in:" >&2
    echo "    cp $REPO/deploy/telegram.env.example $CONFIG_DIR/telegram.env" >&2
    missing_config=1
fi
if [[ ! -f "$CONFIG_DIR/config.toml" ]]; then
    echo "error: $CONFIG_DIR/config.toml is missing. Create it and paste your token:" >&2
    echo "    cp $REPO/deploy/config.example.toml $CONFIG_DIR/config.toml" >&2
    missing_config=1
fi
[[ "$missing_config" == 0 ]] || exit 1
if grep -q "paste-token-here" "$CONFIG_DIR/config.toml"; then
    echo "error: $CONFIG_DIR/config.toml still has the placeholder token" >&2
    echo "    generate one with: claude setup-token" >&2
    exit 1
fi

(cd "$REPO" && uv sync --frozen --no-dev)
install_units
install_shims
systemctl --user enable --now "${UNITS[@]}"
loginctl enable-linger "$USER" 2>/dev/null \
    || echo "warning: could not enable linger — services will stop when you log out" >&2

daemon_ok=0
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$REPO/.venv/bin/falconfox" list >/dev/null 2>&1; then
        daemon_ok=1
        break
    fi
    sleep 1
done
if [[ "$daemon_ok" == 1 ]]; then
    echo "daemon: ok"
else
    echo "daemon: NOT responding — journalctl --user -u falconfox-daemon" >&2
    exit 1
fi
if systemctl --user is-active --quiet falconfox-telegram.service; then
    echo "bot: running"
else
    echo "bot: NOT running — journalctl --user -u falconfox-telegram" >&2
    exit 1
fi
echo "FalconFox is up. Message your bot."
