FROM node:20-alpine AS web-build

WORKDIR /web
COPY admin-web/package*.json ./
RUN npm ci
COPY admin-web ./
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY app ./app
COPY scripts ./scripts
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY --from=web-build /web/dist ./admin-web/dist
RUN chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
