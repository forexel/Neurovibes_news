#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${1:-app@77.222.55.88}"
REMOTE_DIR="${2:-/opt/apps/neurovibes_news}"
APP_BUILD_SHA="${APP_BUILD_SHA:-$(git rev-parse --short=12 HEAD)}"

echo "[deploy] host=${REMOTE_HOST}"
echo "[deploy] dir=${REMOTE_DIR}"
echo "[deploy] sha=${APP_BUILD_SHA}"

scp app/main.py "${REMOTE_HOST}:${REMOTE_DIR}/app/main.py"
scp app/sources.py "${REMOTE_HOST}:${REMOTE_DIR}/app/sources.py"
scp app/services/runtime_settings.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/runtime_settings.py"
scp app/services/scoring.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/scoring.py"
scp app/services/ingestion.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/ingestion.py"
scp app/services/pipeline.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/pipeline.py"
scp app/services/telegram_review.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/telegram_review.py"
scp app/services/telegram_publisher.py "${REMOTE_HOST}:${REMOTE_DIR}/app/services/telegram_publisher.py"
scp admin-web/src/app/pages/PublishCenterPage.tsx "${REMOTE_HOST}:${REMOTE_DIR}/admin-web/src/app/pages/PublishCenterPage.tsx"
scp admin-web/src/app/lib/api.ts "${REMOTE_HOST}:${REMOTE_DIR}/admin-web/src/app/lib/api.ts"
scp docker-compose.yml "${REMOTE_HOST}:${REMOTE_DIR}/docker-compose.yml"

ssh "${REMOTE_HOST}" "cd '${REMOTE_DIR}' && \
  APP_BUILD_SHA='${APP_BUILD_SHA}' docker compose up -d --build api admin && \
  docker compose exec -T -e PYTHONPATH=/app api sh -lc 'cd /app && alembic -c alembic.ini upgrade head' && \
  docker compose exec -T api python - <<'PY'
import os
import app.main as m
import re
txt = open('/app/app/main.py', 'r', encoding='utf-8').read()
ok_import = bool(re.search(r'^from sqlalchemy import .*\\bnot_\\b', txt, re.M))
print('container_app_build_sha=', os.getenv('APP_BUILD_SHA', ''))
print('app_version=', m.app.version)
print('has_not_import=', ok_import)
PY"

echo "[deploy] done"
