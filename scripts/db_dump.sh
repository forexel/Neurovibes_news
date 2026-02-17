#!/usr/bin/env bash
set -euo pipefail

# Dumps the local Postgres DB from the docker-compose `db` service into a custom-format file.
#
# Usage:
#   ./scripts/db_dump.sh [output_path]
#
# Examples:
#   ./scripts/db_dump.sh backups/local_$(date -u +%Y%m%d_%H%M%S).dump
#
# Notes:
# - Requires the `db` service to be running (for local profile: `docker compose --profile local up -d db`).
# - Uses env vars from `.env` if present: POSTGRES_DB, POSTGRES_USER.

OUT_PATH="${1:-}"
if [[ -z "${OUT_PATH}" ]]; then
  TS="$(date -u +%Y%m%d_%H%M%S)"
  OUT_PATH="backups/news_publisher_${TS}.dump"
fi

mkdir -p "$(dirname "${OUT_PATH}")"

COMPOSE_CMD="${COMPOSE_CMD:-docker compose}"
DB_SERVICE="${DB_SERVICE:-db}"
DB_NAME="${POSTGRES_DB:-news_publisher}"
DB_USER="${POSTGRES_USER:-news_user}"

echo "[dump] service=${DB_SERVICE} db=${DB_NAME} user=${DB_USER}"
echo "[dump] writing ${OUT_PATH}"

# Custom format (-Fc) is best for pg_restore.
${COMPOSE_CMD} exec -T "${DB_SERVICE}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" -Fc > "${OUT_PATH}"

echo "[dump] done: ${OUT_PATH}"

