# Frontend Migration Checklist (`News Publish` -> `admin-web`)

## Target
- Source of truth for UX/layout/interactions: `News Publish/src/**`
- Rule: no custom UX inventions, only backend wiring and bug fixes.

## Status Legend
- `DONE`: aligned with template and integrated with backend.
- `IN PROGRESS`: partially aligned, remaining UI/interaction diffs exist.
- `TODO`: not yet aligned 1:1.

## Global
- `TopNavigation`: `DONE` (template structure restored, actions wired to API).
- `Root`: `IN PROGRESS` (forced dark + toaster + route error handling retained; verify against template).
- `routes.tsx`: `IN PROGRESS` (extra routes kept for backward compatibility; verify exact template behavior per entry points).
- `theme.css`: `IN PROGRESS` (contains production fixes; compare token-by-token with template).

## Pages
- `LoginPage.tsx`: `IN PROGRESS`
- `RegisterPage.tsx`: `IN PROGRESS`
- `SetupWizard.tsx`: `IN PROGRESS`
- `ArticlesDashboard.tsx`: `IN PROGRESS`
  - Click row -> preview modal: aligned.
  - Open editor only from modal/actions: aligned.
  - Remaining: match micro-layout/text/controls 1:1 with template.
- `ArticleEditor.tsx`: `IN PROGRESS`
  - Extra external action block removed.
  - Remaining: reconcile linear/workspace tab content and controls with template while keeping all API actions.
- `SourcesPage.tsx`: `IN PROGRESS`
- `ScoreSettingsPage.tsx`: `IN PROGRESS`
- `BotControlPage.tsx`: `IN PROGRESS`
- `PublishCenterPage.tsx`: `IN PROGRESS`

## Backend Integration Requirements (must preserve)
- Auth/session redirects.
- Article list with views/filters/pagination/search.
- Article actions: score, prepare, pull content, publish, delete with reason, schedule/unschedule.
- Sources CRUD + activation toggle.
- Score params/runtime settings.
- Bot control operations + status/logs.
- Publish center actions and counters.

## ML Runtime Checks (server)
- `hourly_default_selection_strategy = ml`
- `ml_review_every_n_hours = 2`
- `hourly_slot_strategy_csv = ''`
- Nightly maintenance present via worker daily run keys.
- Current note: `ml_review_min_confidence` should be reviewed (currently runtime may be relaxed during manual operations).
