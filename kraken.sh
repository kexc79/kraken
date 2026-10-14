#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
umask 077
export APP_UID="$(id -u)" APP_GID="$(id -g)"
mkdir -p config logs
dc() { docker compose --env-file /dev/null -p kraken-pharma -f compose.yaml "$@"; }
running() { [[ -n "$(dc ps -q --status running pharma)" ]]; }
ready() {
    for file in config/.env config/.req; do
        [[ -f "$file" ]] || { echo "Missing $file" >&2; exit 2; }
    done
    dc run --rm --no-deps -T pharma python check_config.py
}
idle() { if running; then echo 'Already running. Use status, logs or stop.' >&2; exit 1; fi; }
lock_run() { exec 9>.run.lock; flock -n 9 || { echo 'Another launch is in progress.' >&2; exit 1; }; }
case "${1:-start}" in
    init)
        for name in .env .req; do
            if [[ ! -e "config/$name" ]]; then
                [[ -f "$name.example" ]] || { echo "Missing template: $name.example" >&2; exit 2; }
                cp --no-clobber -- "$name.example" "config/$name"
                chmod 600 -- "config/$name"
                echo "Created config/$name from template."
            else
                echo "Preserved existing config/$name."
            fi
        done
        chmod u+x -- "$0"
        # Web uploads can store scripts as 0644. Ignore the local executable-bit
        # change so future git pulls can update the launcher without conflicts.
        if command -v git >/dev/null && [[ "$(git rev-parse --show-toplevel 2>/dev/null || true)" == "$PWD" ]]; then
            git config --local core.fileMode false
        fi
        echo 'Set real values in config/.env and config/.req, add the Google JSON key to config/, then use ./kraken.sh start.'
        ;;
    build) dc build ;;
    check) ready ;;
    start|run)
        lock_run
        idle
        # Also cancel the launcher if SSH disconnects during build/check.
        trap 'dc stop pharma >/dev/null 2>&1 || true; exit 130' HUP INT TERM
        dc build
        ready
        # Config stays mounted; each new process reads the latest server files.
        # Attach logs and propagate the application's exit code.
        dc up --no-build --no-deps --force-recreate --abort-on-container-exit --exit-code-from pharma pharma
        ;;
    logs) dc logs -f --tail 100 pharma ;;
    status) dc ps -a ;;
    stop) dc stop pharma ;;
    *) echo 'Usage: ./kraken.sh {init|build|check|start|run|logs|status|stop}' >&2; exit 2 ;;
esac
