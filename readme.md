# Neurovibes News Service (Scaffold)

Базовый каркас сервиса: API + Postgres(pgvector) + Redis через Docker Compose.

## Быстрый старт

```bash
docker compose up --build -d
curl http://localhost:8000/health
curl http://localhost:8000/config
```

## Переменные окружения

- Локальная конфигурация: `.env`
- Шаблон для репозитория: `.env.example`

`.env` исключен из git.

## Что дальше

1. Добавить ingestion воркер (RSS/API crawl).
2. Добавить embeddings + dedup (pgvector).
3. Добавить scoring + hourly picker.
4. Добавить summary/image pipeline.
5. Добавить admin + feedback loop + Telegram publish.
