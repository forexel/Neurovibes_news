# Neurovibes News Service

Production-oriented pipeline for AI news curation:

`Ingestion -> Embedding/Dedup/Clustering -> Scoring -> Top Picker -> RU Summary+Image -> Admin+Feedback -> Preference Trainer -> Auto Decision -> Telegram`

## Implemented nodes

- `Ingestion` (non-MVP upgrades)
  - RSS fetch + page fetch + canonical extraction + full-text extraction (`trafilatura`)
  - stores `raw_feed_entries`, `raw_page_snapshots`, parsed article fields
  - idempotency by `(source_id, external_id)` and `content_hash`
  - source health metrics (`source_health_metrics`)
  - batched backfill mode
- `Embedding + Dedup + Clustering`
  - pgvector embeddings + cosine similarity dedup + cluster key
- `Scoring Engine`
  - multi-factor score + structured feature storage + uncertainty
- `Top Picker`
  - hourly candidate selection with cluster exclusions
- `RU Summary + Image Generator` (non-MVP upgrades)
  - 2-step generation: factual extraction -> RU rewrite
  - quality checks and fallback summary
  - brand card system (safe zones / variants)
  - content versioning (`content_versions`)
- `Admin Panel + Feedback`
  - Web UI (platform root): `/` (legacy `/admin` redirects to `/`)
  - production API (`/v1/*`) with auth/roles/audit
  - structured feedback fields
- `Preference Model Trainer`
  - selection decisions + ranking dataset build
  - logistic ranking model training + model artifacts
  - drift detection
- `Auto Decision Engine`
  - threshold decision: confidence + uncertainty
- `Telegram Publisher`
  - publish text/photo + job log + test endpoint

## Core folders

- `app/services/ingestion.py`
- `app/services/content_generation.py`
- `app/services/scoring.py`
- `app/services/preference.py`
- `app/services/auto_decision.py`
- `app/api_v1.py`
- `app/tasks/full_cycle.py`
- `app/tasks/celery_app.py`
- `app/tasks/celery_tasks/ingestion_tasks.py`
- `admin-web/` (React admin scaffold)

## Run (Docker)

```bash
docker compose up --build -d
```

API:

```bash
curl http://localhost:8001/health
curl http://localhost:8001/config
```

Legacy admin:

- Web UI: `http://localhost:8001/` (legacy `http://localhost:8001/admin` redirects to `/`)

React admin scaffold:

```bash
cd admin-web
npm install
npm run dev
```

## Auth and roles (v1 API)

Default seeded admin from `.env`:

- `ADMIN_EMAIL=admin@local`
- `ADMIN_PASSWORD=admin123`

Login:

```bash
curl -X POST http://localhost:8001/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@local","password":"admin123"}'
```

Use returned bearer token for `/v1/*`.

## Important v1 endpoints

- `POST /v1/auth/login`
- `GET /v1/articles?page=1&page_size=20&q=...&status=...`
- `POST /v1/articles/bulk/status`
- `GET /v1/articles/{id}/score-breakdown`
- `GET /v1/articles/{id}/neighbors`
- `GET /v1/articles/{id}/versions`
- `POST /v1/articles/{id}/feedback` (structured)
- `POST /v1/decisions`
- `POST /v1/trainer/build`
- `POST /v1/trainer/train/{batch_id}`
- `POST /v1/trainer/drift`
- `POST /v1/decision/auto`
- `GET /v1/analytics/overview`
- `GET /v1/sources/health`

## CLI

```bash
python -m app.tasks.reader --days-back 30
python -m app.tasks.full_cycle backfill --days 30 --batch-days 3
python -m app.tasks.full_cycle cycle --backfill-days 1
python -m app.tasks.full_cycle trainer --days 14
python -m app.tasks.full_cycle drift
python -m app.tasks.full_cycle auto-decision
python -m app.tasks.full_cycle publish --article-id 123
```

## Runtime mode

Current setup is intentionally simple:

- no Redis
- no Celery queues
- split services without queues:
  - `api` (public/API endpoints)
  - `admin` (separate admin container)
  - `pipeline` (single worker loop for ingest/score/prepare + Telegram polling + scheduled publish)
  - `db`
  - `minio` (generated image storage)

Web UI: `http://localhost:8001/`
Setup wizard: `http://localhost:8001/setup`
Score parameters: `http://localhost:8001/score`
API (same service): `http://localhost:8001`
MinIO console: `http://localhost:9011`

### Web auth (admin UI)

- Register user: `http://localhost:8001/register`
- Login: `http://localhost:8001/login`
- Logout: `http://localhost:8001/logout`
- First login redirects to setup wizard until onboarding is completed.

## Migrations

Alembic scaffold added:

- `alembic.ini`
- `alembic/env.py`
- `alembic/versions/0001_initial.py`

Current startup still uses SQLAlchemy `create_all`; next step is writing explicit alembic revisions and switching to migration-only deploy.

## Telegram review flow (manual posting from Telegram)

- Worker picks hourly top article.
- Bot sends post preview to your private chat with buttons:
  - `Опубликовать` (asks time: now / +1h / custom in your timezone)
  - `Скрыть` (soft-hide, keeps in DB, excluded from All)
  - `Удалить` (soft-delete, keeps in DB, excluded from All)
  - `Отправить позже` (keeps in DB and does not spam the review chat)
- After click, bot asks "почему?" and saves reasons for training.

Important:

1. Open bot dialog and send `/start` from your account first.
2. Set `TELEGRAM_REVIEW_CHAT_ID` in `.env` (`@username` or numeric id).
3. Test:

```bash
curl -X POST http://localhost:8001/telegram/review/poll
curl -X POST http://localhost:8001/telegram/review/send-latest
```

## UI styles

- Shared CSS is served from `app/static/app.css` as `/static/app.css`.
- All UI pages use the same stylesheet (no inline `<style>` blocks).

## Production deploy on server `77.222.55.88`

Assumption: server already has shared `infra-db` (Postgres) and `infra-minio` containers.

### 1. Clone and env

```bash
ssh root@77.222.55.88
mkdir -p /srv/apps && cd /srv/apps
git clone <your-private-repo-url> neurovibes_news
cd neurovibes_news
cp .env.example .env
```

Fill `.env`:

- `OPENROUTER_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHANNEL_ID`
- `TELEGRAM_REVIEW_CHAT_ID`

### 2. Separate Postgres database/user (inside infra-db)

Example (adjust container name/credentials):

```bash
docker exec -it infra-db psql -U postgres -d postgres
```

Run SQL:

```sql
CREATE USER neurovibes_user WITH PASSWORD 'STRONG_PASSWORD';
CREATE DATABASE neurovibes_news OWNER neurovibes_user;
GRANT ALL PRIVILEGES ON DATABASE neurovibes_news TO neurovibes_user;
```

Set in `.env`:

```env
DATABASE_URL=postgresql+psycopg://neurovibes_user:STRONG_PASSWORD@infra-db:5432/neurovibes_news
```

### 3. Separate MinIO bucket (inside infra-minio)

```bash
# if mc client not installed, run temporary client container
docker run --rm --network host minio/mc \
  alias set local http://127.0.0.1:9000 MINIO_ROOT_USER MINIO_ROOT_PASSWORD

docker run --rm --network host minio/mc mb local/neurovibes-images
docker run --rm --network host minio/mc anonymous set download local/neurovibes-images
```

Set in `.env`:

```env
MINIO_ENDPOINT=infra-minio:9000
MINIO_ACCESS_KEY=MINIO_ROOT_USER
MINIO_SECRET_KEY=MINIO_ROOT_PASSWORD
MINIO_BUCKET=neurovibes-images
MINIO_PUBLIC_BASE_URL=https://<your-minio-public-domain-or-gateway>
MINIO_COMPRESS_ENABLED=true
MINIO_COMPRESS_FORMAT=WEBP
MINIO_COMPRESS_QUALITY=82
MINIO_MAX_WIDTH=1920
```

### 4. Build and run app

```bash
docker compose up -d --build
docker compose ps
```

### 5. Scheduling options

Option A (recommended): built-in worker loop (already in `pipeline` service).

- hourly cycle by:
  - `WORKER_INTERVAL_SECONDS=3600`
- scheduled publish:
  - публикация по `scheduled_publish_at` происходит в worker loop автоматически

Timezone:

- Отложенная публикация (Schedule) интерпретирует время в `Setup → Telegram → Timezone` (по умолчанию `Europe/Moscow`).

Option B (cron on host):

```bash
crontab -e
```

Example cron:

```cron
# optional: run cron in Moscow time
CRON_TZ=Europe/Moscow

# every hour: ingest/dedup/score/pick/prepare
0 * * * * cd /srv/apps/neurovibes_news && flock -n /tmp/neurovibes-cycle.lock docker compose exec -T api python -m app.tasks.full_cycle cycle --backfill-days 1 >> /var/log/neurovibes-cycle.log 2>&1

# every hour: send top review message (if you are not using pipeline worker)
5 * * * * cd /srv/apps/neurovibes_news && curl -fsS -X POST http://127.0.0.1:8001/telegram/review/send-latest >> /var/log/neurovibes-tg-send.log 2>&1

# every minute: handle telegram callbacks/reasons
* * * * * cd /srv/apps/neurovibes_news && curl -fsS -X POST http://127.0.0.1:8001/telegram/review/poll >/dev/null 2>&1
```

Use either worker scheduling (`pipeline`) or cron, not both for the same task.

### 6. Useful checks

```bash
docker compose logs -f pipeline
docker compose logs -f api
curl -s http://127.0.0.1:8001/health
```

### 7. Backups you should configure

- Postgres daily dump (`pg_dump`) to separate disk.
- MinIO bucket lifecycle/backups.
- `.env` backup in secure secret storage.
