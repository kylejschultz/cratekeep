# All Inboxes view

Status: approved by Kyle, 2026-11-04. Implement in feat/all-inboxes atop the
combined candidate 41dabc9, preserving separately reviewable feature commits.

## User-visible scope

Add an “All Inboxes” tab before the existing named Inbox tabs when more than one
Inbox is configured. It combines their albums into the same queue interface and
shows a small neutral pill with the source Inbox name on each row. Existing named
tabs retain their behavior. Preserve current default selection and direct links;
`?source=all` opens the combined view explicitly. With one Inbox, keep the current
single-source view without redundant tabs. These are approved defaults, not new
settings.

The combined view retains search, All/Pending/Matched/Imported filters, 25-album
pagination, current sorting, review, matching, imports, clear-imported, deletion
and undo. Selecting a page selects only that visible page; paging/search/filter/
tab changes clear hidden selection as today. The pill identifies provenance, not
match quality, and remains readable on mobile without widening the page.

## Required behavior and boundaries

- Read one globally sorted/paginated combined queue from saved inventory, not a
  concatenation of each Inbox's first page. Totals reflect the combined filter.
- Each album keeps its stable review ID and original Inbox ID. Identically named
  albums/folders from different Inboxes remain separate rows with source pills.
- Review/import/delete/undo always use the album's real Inbox. All is a view, not
  another filesystem root, and must never be passed as a real source ID.
- Mixed-Inbox selections support the same existing actions. Reuse validated
  per-album/per-Inbox matching, import and deletion handlers, locks, previews and
  durable receipts. Partition requests where a handler requires one Inbox; do not
  introduce an alternate import or deletion engine.
- A mixed-source deletion confirmation groups folders by Inbox so equal relative
  paths cannot be confused. Apply only confirmed plans and retain existing
  fingerprint/safety checks, exact-once execution and partial-result recovery.
  Do not automatically repeat a destructive request after a lost response.
- Activity/results identify an All Inboxes operation and its real per-album/source
  outcomes instead of incorrectly assigning the entire run to the first Inbox.
- Opening All reads saved rows immediately, with discovery separate as in #22.
  Refresh reconciles all configured Inboxes using their existing explicit-refresh
  semantics and background ownership. Per-Inbox schedules and auto-match settings
  remain unchanged. A failed source reports a scoped error without hiding the
  other sources or deleting their inventory.
- Live arrivals and matching completion preserve current review/selection, then
  reconcile safely. Support continued background work and navigation/reload.

Not included: multiple Library destinations, Inbox routing preferences, a new
import engine, lyrics or notifications. Current per-Inbox paths/settings remain.

## Existing implementation inspected

`Inbox` in frontend/src/pages.tsx chooses a stable source and mounts Queue per tab.
Queue and InboxActionDialog currently construct single-Inbox bulk/action URLs.
The backend queue is scoped by root/source ID; matching resolves album IDs to
source context. Activity currently derives a batch's scope from its first step.
An aggregate read path and source-aware action dispatch are required; simply
adding a frontend tab would show incorrect pagination or route mixed actions to
one Inbox. Reuse current endpoints for mutations and keep aggregate reads bounded.

## Acceptance and validation

- [x] Two Inboxes with more than 25 combined albums show correct global pages,
      totals, filtering and neutral source pills; named views stay unchanged.
- [x] Equal album/folder names stay separate, and All never becomes a disk root.
- [x] Visible-page-only selection and tab/filter transitions remain correct.
- [x] Review and mixed-source match/import operate on the chosen albums only;
      background completion updates their rows and preserves selection.
- [x] Mixed-source delete, imported cleanup and undo show source-aware previews,
      modify only confirmed files, and retain receipts/partial failure recovery.
- [x] Missing/unreadable sources leave other saved albums usable with scoped errors.
- [x] Refresh/discovery preserve existing schedules, safety and #22 responsiveness.
- [x] Activity and modal progress use the correct scope, placement and delay.
- [x] Desktop/mobile, direct-link, reload and one-Inbox behavior are verified.

Use isolated fixtures for at least two Inboxes, colliding relative folder names,
pagination, one unavailable source and mixed successes/failures. Run applicable
frontend/backend gates plus focused real-browser/container checks. Deliver one
combined linux/amd64 local-registry candidate for Kyle, retaining this feature in
separable commits. No merge, PR publication, release or live deployment implied.

Implementation complete. Backend/frontend gates and focused real-browser fixtures
passed; final image validation/publication in progress. Candidate acceptance in
Kyle's environment remains pending. Feature has its own branch/worktree and
commits atop the earlier combined candidate; no live deployment.
