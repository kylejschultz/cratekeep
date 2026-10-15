# Inbox and Library Import row workflow

Status: agreed direction for the next Cratekeep UI iteration
Base: merged `main` at `6236bc851de581921ceb7ba4236af26cdccdd90b`

## Intent

Bring Cratekeep's Inbox and Settings → Library import surfaces closer to the
beets-flask inbox pattern shown in the supplied references, while keeping the
existing Cratekeep shell, backend safety rules, and shared import review modal.

## Interaction contract

- Render each folder or album as a compact, full-width row rather than a card
  or multi-column tile.
- Keep the identity, file/album counts, lifecycle status, and match confidence
  visible in the collapsed row. Status and confidence are separate signals;
  confidence must not silently make an item importable.
- Keep a small square/icon Import action on rows where direct import is
  supported. The accessible name must identify the folder or album.
- Expand a row in place to show its contained albums/files, using the same
  compact tree treatment as the beets-flask reference. Expansion is lazy and
  must preserve the existing scan/review semantics.
- Use the existing Cratekeep review/import modal for metadata selection,
  duplicate policy, preflight, and execution. Do not introduce beets-flask's
  separate detail page or route.
- Apply the same visual grammar and keyboard/accessibility behavior to Inbox
  scopes and Library import folders, including narrow viewports without
  horizontal overflow.
- Preserve bulk selection, scanning, exception review, progressive loading,
  stale-inventory handling, and the current safe import boundaries.

## Visual direction

Use the existing Cratekeep palette and shell, with dense separators and modest
row height. The row should read in this order: selection/expand affordance,
identity, supporting counts/path, status, confidence, and a compact action.
Expanded children should be indented and visibly subordinate, with file names
readable without opening the modal. Avoid new dependencies or a second review
workflow.

## Acceptance checks

1. Inbox and Library import both render the row pattern from representative
   disposable fixtures.
2. A row expands and collapses with keyboard-accessible controls; its child
   files/albums appear only when requested.
3. Status and numeric match percentage remain visible and distinct where data
   exists; error/pending states retain their existing semantics.
4. The per-row Import action opens the existing modal or performs the existing
   direct-import path, and no separate beets-flask page is added.
5. Bulk scan/import and exception-review controls continue to work.
6. Browser checks cover Inbox and Library Import at desktop and narrow widths,
   including console errors and horizontal-overflow checks.
7. Existing Python tests, compilation, container build, and official browser
   smoke pass on the candidate source.

