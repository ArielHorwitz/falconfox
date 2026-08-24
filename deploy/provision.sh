#!/usr/bin/env bash
# Root-level VPS prerequisites for FalconFox, for an apt-based distro
# (Debian/Ubuntu). Run once as root; everything after this is per-user
# (clone, config, setup.sh — see README.md).
set -euo pipefail

if [[ "$(id -u)" != 0 ]]; then
    echo "error: run as root (sudo $0)" >&2
    exit 1
fi
if ! command -v apt-get >/dev/null; then
    echo "error: not an apt-based system." >&2
    echo "Install manually: git, curl, nodejs+npm (>=18), uv, then:" >&2
    echo "    npm install -g @agentclientprotocol/claude-agent-acp" >&2
    exit 1
fi

apt-get update
apt-get install -y git curl ca-certificates nodejs npm

node_major="$(node --version | sed 's/^v\([0-9]*\).*/\1/')"
if [[ "$node_major" -lt 18 ]]; then
    echo "error: node $(node --version) is too old for claude-agent-acp (need >=18)." >&2
    echo "Use NodeSource (https://github.com/nodesource/distributions) and re-run." >&2
    exit 1
fi

npm install -g @agentclientprotocol/claude-agent-acp

if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi

echo
echo "Provisioned: git $(git --version | awk '{print $3}'), node $(node --version)," \
     "npm $(npm --version), uv $(uv --version | awk '{print $2}'), claude-agent-acp."
echo "Continue as the deploy user with the bootstrap steps in deploy/README.md."
