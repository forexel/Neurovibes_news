# UI Parity Matrix (News Publish -> admin-web)

Дата: 2026-03-05  
Эталон: `News Publish/src/app/pages/*`

## Статус по страницам

1. `LoginPage` — parity OK
2. `RegisterPage` — parity OK
3. `SetupWizard` — parity OK (структурно), требует визуальной финальной сверки
4. `SourcesPage` — parity PARTIAL (нужна финальная сверка заголовков/подписей/пустых состояний)
5. `ScoreSettingsPage` — parity PARTIAL (есть расхождения в заголовках блоков и табличных секциях)
6. `BotControlPage` — parity PARTIAL (расхождение секции действий/лейблов)
7. `ArticlesDashboard` — parity PARTIAL (блок сбора синхронизирован, нужна финальная сверка мелких подписей/бейджей)
8. `ArticleEditor` — parity PARTIAL (линейный режим выровнен, workspace режим требует полной финальной подгонки)
9. `PublishCenterPage` — parity PARTIAL (нужна сверка empty-state блоков и текстовых лейблов)

## Что уже исправлено

- Возвращены блоки `Действия` и `Обратная связь` в `ArticleEditor` (линейный режим).
- `Предпросмотр поста` в `ArticleEditor` приведен к карточному виду эталона.
- Для modal preview в списке статей добавлены действия/рендер ссылок/причины.
- Исправлено поведение dropdown-меню (hover gap / click fallback).

## Что осталось довести до 1:1

- Полная финальная сверка `ArticleEditor` в режиме `workspace`.
- Приведение `BotControlPage`, `ScoreSettingsPage`, `PublishCenterPage`, `SourcesPage` к точному соответствию текстов и блоков эталона.
- Финальный regression-pass по всем интеракциям после выравнивания.

