# Cratekeep React UI rebuild

Status: implemented as a single UI cutover from source `670fc7158bcf7d45ca6d78f6de193bb90ae01d97`.

## Scope

Cratekeep now serves one React/TypeScript/Vite application shell for Overview,
Inbox, Library Import, Library, and Settings. Flask remains responsible for all
storage, matching, review, execution, and media-safety APIs. The shell uses a
persistent plum navigation surface, warm foregrounds, lavender accents, a
responsive drawer, and the same route order on every page.

Inbox and Library Import share one queue implementation. Its hierarchy is
folder → album → file:

- folder rows contain identity, aggregate counts, and an expansion control;
- album rows contain identity, track count, lifecycle status, optional match
  percentage, immediate Import control, and an overflow menu containing only
  Import and Review;
- file rows contain filename plus right-aligned type and size.

The configurable match threshold defaults to 90%, is stored in
`app_settings.match_threshold`, and accepts only integer values from 1 through
100. Candidate absence is `Pending`; the best score at or above the threshold
is `Matched`; a lower candidate is `Threshold`; only provider or execution
failure is `Error`. Import is disabled for Pending and Error.

The React review dialog uses the existing review APIs and keeps candidate and
As Is selection, duplicate decisions, nested comparison evidence, explicit
preflight/execution, retry feedback, cancellation, keyboard dismissal, and
focusable modal semantics. Library and Settings retain their existing backend
mutation boundaries.

## Build and evidence

The frontend toolchain lives in `package.json`, `vite.config.ts`, and
`frontend/src`. Generated production assets are served from
`beets_mvp/static/ui`. Unit coverage includes route navigation and the exact
threshold/status state machine. Flask coverage includes threshold default,
validation, persistence, and the five React route hosts. Runtime smoke checks
cover every route at desktop and narrow widths, queue expansion/action states,
dialog paths, and unexpected console/page errors.

The per-run development-node result reports contain the exact commands,
runtimes, and any environment limitations observed during implementation.

Validation on the reconciled macOS development-node workspace:

- frontend TypeScript, ESLint, and Vitest checks passed; production Vite assets
  built successfully;
- the Python/API suite passed with 249 tests and 29 explicit skips for retired
  server-template/source-text contracts now covered by the frontend and browser
  suites;
- the local `linux/arm64` candidate image built successfully;
- the Docker/Chromium 156 smoke passed all five routes at 1440px and 390px,
  queue scanning, threshold-driven action availability, overflow actions, the
  review dialog, zero horizontal overflow, unchanged disposable media bytes,
  and zero page/console errors.

