# Test Cases: Admin UI + Backend Wiring

## A. Навигация и меню

1. `TopNavigation` hover/click для `Действия` открывает меню и не схлопывается на зазоре.
2. `TopNavigation` hover/click для `Инструменты` открывает меню и не схлопывается на зазоре.
3. `TopNavigation` hover/click для `Аккаунт` открывает меню и не схлопывается на зазоре.
4. Все кликабельные элементы показывают курсор `pointer`.

## B. Articles Dashboard

1. Загрузка списка статей работает для всех секций (`all/backlog/unsorted/published/selected_day/selected_hour/deleted`).
2. Сбой `/admin-data/costs` или `/admin-data/worker-status` не ломает список статей.
3. Клик по строке открывает modal preview.
4. В modal preview:
   - HTML теги `<b>/<a>` рендерятся корректно.
   - `Original source` ведет на внешний URL.
   - `Открыть в редакторе` ведет в `/article/{id}`.
   - `Опубликовать` запрашивает причину, отправляет feedback и publish.
   - `Удалить` запрашивает причину и удаляет статью.
5. Для archived/published статей видны причины (если есть): `archived_reason`, `ml_recommendation_reason`, `feedback`.

## C. Article Editor (линейный режим)

1. Присутствуют блоки:
   - `Полный текст (English)`
   - `Краткое содержание (RSS)`
   - `Полный перевод (Russian)`
   - `Пост для Telegram`
   - `Image Prompt`
   - `Предпросмотр поста`
   - `Действия`
   - `Обратная связь`
2. В блоке `Действия` работают:
   - `Оценить`
   - `Подготовить`
   - `Выбрать на час`
   - `Выбрать на день`
   - `Опубликовать сейчас` (с причиной)
   - `В архив`
   - `Удалить` (с причиной)
3. Сохранение `Обратная связь` работает.
4. Блок `ML-вердикт`:
   - переключатель согласия,
   - комментарий,
   - сохранение в backend.

## D. ML Pipeline

1. Артефакт модели сохраняется в `/app/app/static/models` и переживает пересборку контейнера.
2. Активная модель `editor_choice` доступна для инференса (`ml_recommendation != unknown` для большинства статей).
3. Runtime:
   - `ml_review_every_n_hours=2`
   - `ml_review_min_confidence` в целевом диапазоне.
4. Пересчет рекомендаций `/admin-actions/ml-recommendations/refresh` проходит успешно.

