#!/usr/bin/env bash
# One-time VPS bootstrap for FalconFox: sync the venv, install the systemd user
# units, enable lingering, and start everything. Idempotent — safe to re-run.
#
#   setup.sh                 full bootstrap
#   setup.sh install-units   (re)install only the bits that live outside the
#                            checkout — unit files and CLI shims (used by
#                            update.sh)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/falconfox"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
UNITS=(falconfox-daemon.service falconfox-telegram.service)

install_units() {
    mkdir -p "$UNIT_DIR"
    local unit
    for unit in "${UNITS[@]}"; do
        sed -e "s|@REPO@|$REPO|g" -e "s|@HOME@|$HOME|g" \
            "$REPO/deploy/$unit" > "$UNIT_DIR/$unit"
    done
    systemctl --user daemon-reload
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

(cd "$REPO" && uv sync --frozen)
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
