# Test Cases: AI News Hub Admin Platform

## 0) Preconditions
- App URL: `http://77.222.55.88:18100`
- Valid user credentials exist.
- Telegram bot/review/channel configured in setup.
- At least 1 source enabled.

## 1) Auth
1. Open `/login`, submit valid credentials -> redirect to app (not `auth_required`).
2. Submit invalid credentials -> error visible and form remains usable.
3. Refresh authenticated page -> session remains valid.
4. Logout from account menu -> redirected to `/login`.
5. Open protected route without session (e.g. `/dashboard`) -> redirected to `/login`.

## 2) Navigation and Menus
1. Top menu items open expected sections (`Статьи`, `Источники`, `Публикация`, `Бот`, `Оценка`, `Setup`).
2. `Действия` menu triggers API jobs and shows success/error toast.
3. `Аккаунт` menu is stable and clickable (no instant disappearing).
4. On all clickable controls cursor is pointer.

## 3) Articles Dashboard
1. Open `/dashboard` -> table loads (no blank/error screen).
2. Change section tabs (`Все`, `Бэклог`, `Несортированные`, `Опубликованные`, `Выбрано на день/час`, `Удаленные`) -> list updates correctly.
3. Toggle `Без дублей` -> no full-screen flicker; only list updates.
4. Search by title/source -> results filtered.
5. Change page size -> list and pagination update.
6. Click row -> preview modal opens.
7. Click `Открыть в редакторе` in modal -> opens `/article/:id`.
8. `source` external link opens canonical URL in new tab.
9. 3-dot actions:
   - `Открыть редактор`
   - `Выбрать/снять день`
   - `Выбрать/снять час`
   - `Archive`
   - `Delete` (requires reason)
   - `Publish` (requires reason)
10. Collection block:
   - Period selector (`Час/День/Неделя/Месяц`)
   - `Собрать статьи` starts job
   - Progress/status updates
   - Final inserted count displayed

## 4) Article Preview Modal
1. Shows status, score, source, summary/preview.
2. `Открыть в редакторе` works.
3. Closing modal returns to same table state.

## 5) Article Editor (`/article/:id`)
1. Page loads with no crash for existing article.
2. Non-existing article -> graceful "Статья не найдена" page.
3. `Линейный режим` / `Рабочее пространство` tabs switch correctly.
4. `Загрузить с сайта` updates full text.
5. `Сохранить текст`, `Сохранить RU`, `Generate/Save prompt`, `Generate Picture`, upload image all return success/error correctly.
6. `Schedule` / `Clear schedule` updates scheduled timestamp.
7. `Publish` requests reason then publishes.
8. `Delete` requests reason then deletes and redirects.
9. `Archive` moves article to deleted/archived view.
10. No `Unexpected Application Error` on any action.

## 6) Publish Center
1. Main cards (`Нужно сделать`, `Запланировано`, `Удалённые`, `Опубликовано`) switch table dataset.
2. Counters correspond to real article statuses.
3. `Опубликовать по расписанию` triggers due publication and refreshes counters.
4. Row actions (open, publish now, unschedule, delete where applicable) work and require reason when expected.

## 7) Sources
1. Sources list loads with activity state.
2. Add source works (rss/html).
3. Edit source works.
4. Toggle active source does not reload whole page/flicker.
5. 3-dot/context actions open and execute.
6. Habr sources present as configured and ingestable.

## 8) Score Settings
1. Params table loads.
2. Add new param works.
3. Edit existing param works.
4. Active toggle persists.
5. Delete param works.
6. Reload reflects current DB state.

## 9) Bot Control
1. Bot status/config widgets load real data (not permanent loading).
2. Operations (`Telegram Test`, `Poll TG Now`, `Send 24h Backfill`, `Send Backfill custom`) execute and log results.
3. Bot log panel updates.

## 10) Runtime/Worker Health
1. Worker status endpoint returns healthy cycle timestamps.
2. No stale `worker_last_cycle_error`.
3. Scheduled publisher loop processes due items.
4. Review polling loop processes callbacks promptly.

## 11) ML Selection and Learning
1. Runtime settings:
   - `hourly_default_selection_strategy=ml`
   - `ml_review_every_n_hours=2`
   - `hourly_slot_strategy_csv=''`
2. In non-2h slots strategy resolves to `off` -> no review candidate message.
3. In 2h slots strategy resolves to `ml` -> candidate sent to TG review chat.
4. Review message includes `Критерии ML` (confidence, model version if available, factors).
5. Nightly maintenance runs once per day (check `worker_daily_ml_*` keys).
6. Training artifacts exist and model version updates after training command.

## 12) Regression Guards
1. Hard refresh not required for normal updates (index no-store headers work).
2. No stale bundle crashes after deployment.
3. All pages render under mobile width without blocked controls.
4. API failures show user-visible errors (not silent fails).
