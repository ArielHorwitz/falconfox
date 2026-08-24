#!/usr/bin/env bash
# Self-update for a running FalconFox deployment: fast-forward the checkout,
# sync the venv, restart the services, health-check, and roll back to the
# previous revision if the new one fails to come up.
#
#   update.sh                   full update, inline (for SSH / a terminal)
#   update.sh --detach-restart  pull + sync now, then restart detached a few
#                               seconds later — for FalconFox agent sessions,
#                               whose own turn dies when the daemon restarts
#   update.sh --restart-phase --rollback-to <rev>
#                               internal: the detached restart/health/rollback
#
# All output is appended to $XDG_STATE_HOME/falconfox/update.log.
#
# The braces force bash to parse the whole script before running it, so the
# `git merge` rewriting this very file mid-run cannot corrupt execution.
{
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/falconfox"
LOG_FILE="$STATE_DIR/update.log"
UNITS=(falconfox-daemon.service falconfox-telegram.service)

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

mkdir -p "$STATE_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

restart_services() {
    "$REPO/deploy/setup.sh" install-units
    systemctl --user restart "${UNITS[@]}"
}

healthy() {
    for _attempt in $(seq 1 15); do
        if "$REPO/.venv/bin/falconfox" list >/dev/null 2>&1; then
            # Daemon is up; give the bot a moment to reconnect and settle
            # (it restarts whenever the daemon does).
            sleep 5
            systemctl --user is-active --quiet falconfox-telegram.service && return 0
        fi
        sleep 2
    done
    return 1
}

apply_update() {
    cd "$REPO"
    if [[ -n "$(git status --porcelain)" ]]; then
        log "refusing to update: $REPO has uncommitted changes (development belongs in .worktrees/)"
        exit 1
    fi
    previous_revision="$(git rev-parse HEAD)"
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    git fetch origin "$branch"
    git merge --ff-only "origin/$branch"
    if [[ "$(git rev-parse HEAD)" == "$previous_revision" ]]; then
        log "already up to date at $previous_revision — nothing to do"
        exit 0
    fi
    uv sync
    log "updated $previous_revision -> $(git rev-parse HEAD)"
}

restart_phase() {
    local rollback_to="$1"
    log "restarting services"
    restart_services
    if healthy; then
        log "healthy at revision $(git -C "$REPO" rev-parse HEAD)"
        return 0
    fi
    log "UNHEALTHY — rolling back to $rollback_to"
    (cd "$REPO" && git reset --hard "$rollback_to" && uv sync)
    restart_services
    if healthy; then
        log "rolled back to $rollback_to and recovered"
    else
        log "still unhealthy after rollback — manual intervention (SSH) required"
    fi
    return 1
}

case "${1:-}" in
    "")
        apply_update
        restart_phase "$previous_revision"
        ;;
    --detach-restart)
        apply_update
        log "scheduling detached restart in 5s — the current agent turn will be cut off"
        systemd-run --user --collect --unit "falconfox-update-$(date +%s)" \
            --on-active=5 "$REPO/deploy/update.sh" --restart-phase \
            --rollback-to "$previous_revision"
        ;;
    --restart-phase)
        if [[ "${2:-}" != "--rollback-to" || -z "${3:-}" ]]; then
            log "usage: update.sh --restart-phase --rollback-to <rev>"
            exit 1
        fi
        restart_phase "$3"
        ;;
    *)
        log "unknown argument: $1"
        exit 1
        ;;
esac
exit
}
