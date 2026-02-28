#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-/opt/apps/neurovibes_news}"
cd "$ROOT_DIR"

CHECK_CMD=(docker compose exec -T pipeline python -m app.tasks.full_cycle watchdog-check --notify)

if "${CHECK_CMD[@]}"; then
  exit 0
fi

echo "[watchdog] unhealthy pipeline detected, restarting..." >&2
docker compose restart pipeline

# Optional second check (non-fatal) after restart to refresh Telegram alert state.
sleep 8
docker compose exec -T pipeline python -m app.tasks.full_cycle watchdog-check --notify || true

