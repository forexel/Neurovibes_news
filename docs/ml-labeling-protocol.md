# ML Labeling Protocol (Publish vs Delete)

Цель: дать модели чистую, консистентную разметку для выбора статьи в review.

## 1) Класс `publish` (label=1)

Ставим `publish`, если одновременно:
- Новость релевантна теме AI/ML.
- Есть практическая ценность для аудитории канала (инструмент, рабочий кейс, важный рыночный сигнал).
- Источник/контент достаточно надежен для публикации.
- Текст не требует существенной ручной доработки перед публикацией.

## 2) Класс `delete` (label=0)

Ставим `delete`, если есть хотя бы один сильный критерий:
- Нерелевантно теме AI/ML.
- Шум/дубль/короткоживущий инфоповод без ценности.
- Слишком узко-технический материал без пользы для аудитории.
- Недостаточно контента (summary-only без возможности вытянуть полный текст).
- Низкое доверие к источнику или явные признаки низкого качества.

## 3) Правила разметки (обновлено под редактора)

Используем 4 группы правил:

### A. Hard delete (сразу удаляем)
- `ai_ml_relevance=0` (не про AI/ML)
- `content_completeness=0` (не удалось вытянуть контент, пустой summary)
- `is_duplicate=1` (дубль/повтор материала)

### B. Cooldown delete (временно не повторяем тему)
- если тема уже публиковалась недавно (например, "безработица из-за ИИ"), повтор в течение 30 дней -> `delete`
- поле: `cooldown_topic_hit=1`

### C. Причины удаления (теги)
- `too_technical`
- `politics_noise`
- `investment_noise`
- `hiring_roles_noise`
- `low_significance`
- `no_business_use`

### D. Причины публикации (теги)
- `practical_tool`
- `practical_case`
- `ru_relevance`
- `wow_positive`
- `future_impact`
- `business_impact`

Оцениваем основными 0/1 полями:
- `ai_ml_relevance`
- `content_completeness`
- `audience_fit`
- `practical_value`
- `risk_too_technical`
- `risk_politics`
- `risk_investment_noise`
- `risk_hiring_roles`

Решение:
- если сработал любой Hard delete -> `delete`
- иначе если `cooldown_topic_hit=1` -> `delete`
- иначе `publish` при `practical_value=1` и `audience_fit=1`

## 4) Обязательные поля разметки

- `decision`: publish/delete
- `reason_text`: короткая причина (минимум 20 символов)
- `tags` (рекомендуется): 1-3 тега причин (например `practical_tool`, `too_technical`, `duplicate`)

Шаблон записи (вставлять в комментарий/feedback):

```json
{
  "decision": "publish|delete",
  "reason_text": "краткая причина",
  "tags": ["practical_tool", "ru_relevance"],
  "criteria": {
    "ai_ml_relevance": 1,
    "audience_fit": 1,
    "practical_value": 1,
    "content_completeness": 1,
    "risk_too_technical": 0,
    "risk_politics": 0,
    "risk_investment_noise": 0,
    "risk_hiring_roles": 0,
    "cooldown_topic_hit": 0,
    "is_duplicate": 0
  }
}
```

## 5) Как избежать «грязной» разметки

- Не использовать для обучения решения без причины.
- Не смешивать «временно отложено» с окончательным `delete`.
- Для одной статьи хранить только последнее валидное решение редактора.

## 6) Баланс классов

- Для каждого батча обучения держать близко к 50/50 между `publish` и `delete`.
- Если данных меньше, компенсировать `class_weight="balanced"` (в коде уже включено).

## 7) Минимальный стартовый датасет

- Рабочий минимум: 150-300 статей с причинами.
- Лучше: 500+ с равномерным покрытием источников и форматов контента.

## 8) Экспорт «чистого» датасета

```bash
python3 scripts/build_clean_ml_dataset.py --days-back 120 --max-rows 300 --out artifacts/ml/clean_dataset.csv
```

Скрипт:
- берет последние решения из `training_events`,
- фильтрует записи без внятной причины,
- балансирует классы,
- сохраняет CSV для ручной ревизии и переобучения.

## 9) Переобучение только на clean-данных

```bash
PYTHONPATH=/app python /app/scripts/train_editor_choice_clean.py \
  --days-back 365 \
  --min-samples 20 \
  --min-reason-len 20 \
  --max-rows 300 \
  --balance
```

Что делает:
- берет только решения `publish/top_pick/hide/delete`;
- отбрасывает записи без внятной причины;
- оставляет по одной (последней) разметке на статью;
- учит модель отдельно от шумных событий.
