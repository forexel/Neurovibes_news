#!/usr/bin/env bash
set -euo pipefail

# Restores a pg_dump custom-format dump into a Postgres running in a docker container
# (typically your shared infra DB on the server).
#
# Usage:
#   ./scripts/db_restore_infra.sh <dump_path>
#
# Env (optional):
#   INFRA_DB_CONTAINER=apps-infra-db-1   # container name on server
#   TARGET_DB=neurovibes_news           # target database name
#   TARGET_USER=neurovibes_user         # target db user
#   TARGET_PASSWORD=...                 # if password is required (sets PGPASSWORD)
#
# This restores with:
#   --clean --if-exists --no-owner --no-privileges
# so it can be replayed safely and won't try to recreate roles.

DUMP_PATH="${1:-}"
if [[ -z "${DUMP_PATH}" ]]; then
  echo "Usage: $0 <dump_path>" >&2
  exit 2
fi
if [[ ! -f "${DUMP_PATH}" ]]; then
  echo "Dump file not found: ${DUMP_PATH}" >&2
  exit 2
fi

INFRA_DB_CONTAINER="${INFRA_DB_CONTAINER:-apps-infra-db-1}"
TARGET_DB="${TARGET_DB:-neurovibes_news}"
TARGET_USER="${TARGET_USER:-neurovibes_user}"
TARGET_PASSWORD="${TARGET_PASSWORD:-}"

echo "[restore] container=${INFRA_DB_CONTAINER} db=${TARGET_DB} user=${TARGET_USER}"
echo "[restore] source=${DUMP_PATH}"

if [[ -n "${TARGET_PASSWORD}" ]]; then
  docker exec -i -e PGPASSWORD="${TARGET_PASSWORD}" "${INFRA_DB_CONTAINER}" \
    pg_restore -U "${TARGET_USER}" -d "${TARGET_DB}" --clean --if-exists --no-owner --no-privileges < "${DUMP_PATH}"
else
  docker exec -i "${INFRA_DB_CONTAINER}" \
    pg_restore -U "${TARGET_USER}" -d "${TARGET_DB}" --clean --if-exists --no-owner --no-privileges < "${DUMP_PATH}"
fi

echo "[restore] done"

