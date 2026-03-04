# Test Report (2026-03-05)

Область: `admin-web` + `api` + `pipeline` после commit `bd0db53` и правки ArticleEditor parity.

## 1) Технические проверки сборки

- `npm run build` (`admin-web`) — PASS
- `python3 -m py_compile app/main.py app/services/preference.py app/core/config.py app/models.py app/db.py` — PASS

## 2) Прод-выкатка

- `git pull --rebase origin main` — PASS
- `docker compose build api admin pipeline` — PASS
- `docker compose up -d api admin pipeline` — PASS
- Контейнеры `api/admin/pipeline` в статусе `Up` — PASS

## 3) ML runtime / модель

- Runtime setting:
  - `ml_review_every_n_hours=2` — PASS
  - `ml_review_min_confidence=0.68` — PASS
- `train_editor_choice_model(days_back=120,min_samples=30)` — PASS
- `refresh_ml_recommendations(limit=6000, only_missing=False)` — PASS (`updated=5178`)
- Артефакт модели в контейнере:
  - `/app/app/static/models/editor_choice_20260304221709.json` — PASS
- Распределение рекомендаций:
  - `delete_candidate: 3415`
  - `review: 1558`
  - `publish_candidate: 206`

## 4) UI parity c News Publish

- `ArticleEditor` линейный режим:
  - `Предпросмотр поста` — PASS
  - `Действия` — PASS
  - `Обратная связь` — PASS
  - `Опубликовать сейчас` — PASS
  - `Сохранить обратную связь` — PASS

## 5) Ограничения текущего отчета

- Не выполнялся полный e2e UI прогон браузерными автотестами (Playwright сценарии не были написаны).
- Проверка части UX-деталей сделана по коду и деплой-валидации, не по автоматизированным скриншот-сравнениям.

