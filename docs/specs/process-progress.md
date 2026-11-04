# Process progress and activity — issue #24

Approved for implementation by Kyle — 2026-11-03. Shared Activity monitor, page-specific progress and background batch ownership confirmed.
Issue: https://github.com/kylejschultz/cratekeep/issues/24

## Problem and evidence

The reporter cannot tell what Library Import → Refresh is doing or when it ends.
Kyle wants progress across scans, refreshes and other processes, not just imports.

Existing matching and bulk-import dialogs already show album-level progress and
results. Matching reports cooldown/retry state. These loops are owned by React
components and protect against page departure; they are not durable server-owned
batch runs. Library refresh synchronously walks/scans folders without intermediate
progress. Named Inbox discovery runs on the server, with committed scan events and
last-scan status, but no user-facing work counters. Queue page loading also has a
30-second abort path that must not misrepresent a continuing scan as failed.

Originally planned from origin/dev 04e95f2. Kyle subsequently requested that the
progress candidate build on the accepted artist/credits/location commits (28a60a7),
so this task is stacked on that branch. Artist/location PR #26 remains independently
reviewable. Paused Lyrics and broader Beets settings work remain excluded.

## Proposed user experience

- A compact Activity control in the shared header shows the active run count and
  opens a list of active/recent runs. Relevant pages/dialogs show the same state
  beside the initiating action; do not create contradictory duplicate indicators.
- Each run has an understandable operation, scope (Library or named Inbox), stage,
  elapsed time, measured work count and current album/file when useful. Display
  relative paths only when needed; do not expose arbitrary host paths.
- Show determinate progress only for a real, stable total within the labeled stage:
  e.g. "Reading tags: 120 of 450 files" or "Importing: 8 of 20 albums". While walking
  an unknown tree show "Finding files… 340 found" with indeterminate progress.
  Changing stages changes the label and counter together. No fabricated global
  percentage or ETA; do not scan the entire tree twice merely to obtain a total.
- Distinguish waiting for another operation, provider cooldown/retry, working,
  stopping, completed, completed with errors, failed, stopped and interrupted.
  Do not leave progress at 100% while catalog updates or finalization still run.
- Keep failures and completion summaries available in Activity until acknowledged
  or replaced by bounded recent history. Existing result actions remain reachable,
  including review, safe retry of unfinished work and Clear imported from Inbox.
- Quiet background Inbox scans use the same activity data, without repeatedly
  opening dialogs or emitting success toasts. Existing committed scan boundaries
  remain authoritative for discovered content and future notification hooks.
- Starting an action immediately acknowledges it. Duplicate clicks attach to the
  existing run or explain the conflicting operation; never launch duplicate work.

## Initial operation coverage

1. Library inventory / Import existing files refresh; folder enumeration, tag reads,
   review synchronization and completion, including empty/unavailable folders.
2. Named Inbox page-open/manual scans, timed/Instant discovery scans, scope refresh
   and existing empty-folder cleanup. Identify the Inbox and trigger.
3. Matching: selected batches, single/exact-release requests and automatic matching.
   Keep bounded retries, provider cooldowns and saved-match reuse visible.
4. Inbox copy import and Library adoption: existing single/bulk execution and its
   validation, per-album/file work, artwork and catalog-finalization stages. Existing
   previews/results/receipts remain the authority for outcomes.
5. Other user-visible file operations (metadata writes/organization, artwork work,
   deletion/clear/undo) use consistent pending/stage/result feedback. Add measured
   file counters where they already iterate work; short actions need no artificial
   job dialog. Do not change destructive confirmations or add automatic retries.

Lyrics jobs are excluded while that feature is paused. No new auto-import feature,
scan scheduling preferences, external notifications, distributed task system,
full audit/history product, or cancellation halfway through unsafe file writes.

## Agreed activity and navigation direction

Kyle confirmed the shared header Activity monitor and context-specific pending
status on the relevant page. Progress remains available across page navigation/
reload, and confirmed matching/import batches continue on the server until
completion or an explicit safe stop. Existing Stop after current album semantics
remain. Closing a progress view does not cancel a run. Navigating elsewhere does
not reset progress or drop queued albums. Reopening attaches to the same known run.

The header stays compact when idle and shows an active-job count while working.
Opening it reveals operation, scope, stage, counts/waits and a link to the relevant
page or results. The relevant page displays the same operation's pending/progress
state; background work does not require a modal to remain open. Ordinary page data
loads are shown inline rather than turning every HTTP request into an Activity job.

## Implementation constraints

- Reuse the existing execution functions, job/receipt state, locks, discovery service
  and retry rules. Add a small shared progress/status contract and instrument actual
  work boundaries. Do not fork the import pipeline or add a broker/runtime dependency.
- Status reads remain responsive during work. Browser polling can reconnect and
  must not own the operation lifetime. Bound polling and recent-history retention;
  batch/throttle updates so every file does not cause excessive database writes.
- Bind operation IDs, scope, progress unit/total, stage and terminal results. Keep
  resolved/failed/skipped/successful counts distinct; a failed item is processed
  work, not a successful import. Aggregates and per-item results must agree.
- Progress bookkeeping must not undermine mutation serialization, fingerprint/
  collision checks, result receipts, rollback, filesystem stability or deferred
  queue updates during active review/selection. A scan is complete only after its
  committed inventory/review synchronization succeeds.
- If server ownership is approved, submission confirms the intended scope and saved
  review choices; a repeated submission/reconnection cannot duplicate imports.
  Revalidate input evidence before each existing mutation just as today.
- On app restart, runs that cannot be proven complete become interrupted/check
  result, not success. Do not automatically resume destructive work or retry an
  uncertain mutation. Preserve committed results and stop safely between albums.
- A browser network failure means status unavailable/reconnecting; consult server
  operation/result state before telling the user an operation failed or retrying.

## Acceptance and validation

- [ ] Refresh acknowledges immediately and reports live stages/counts for a large
      generated Library/Inbox fixture; empty/error cases finish with useful results.
- [ ] No fake total/ETA; totals become determinate only when actually known.
- [ ] Timed/Instant scans are visible without noisy dialogs/toast repetition.
- [ ] Matching cooldowns and lock waits are distinguishable from stalled work.
- [ ] Batch import/matching counts match item results, including failures/skips.
- [ ] Navigation/reload behavior matches the explicitly approved ownership choice.
- [ ] Existing result actions and safe Stop after current album remain available.
- [ ] Duplicate starts, reconnects and repeated clicks cannot duplicate execution.
- [ ] Errors/restarts/uncertain outcomes do not falsely claim completion or rollback.
- [ ] Existing user review selections and queue pagination remain stable.
- [ ] Desktop/mobile, accessible progress/status announcements and keyboard access
      are verified without announcing every individual file as a live-region alert.

Use generated fixtures and controlled slow providers/file loops to test intermediate
progress, not just final snapshots. Cover actual server APIs, responsive status
reads, reconnect, competing starts, partial failure and safe stopping. Run relevant
repository gates, build the frontend, review the diff and attempt exact-head amd64
container/browser checks. Deliver a local candidate and focused manual checklist
for Kyle after spec approval. PR/publication/deployment remain separate authority;
no issue comments or GitHub changes are authorized by this planning request.

## Candidate feedback — 2026-11-03

Kyle approved the combined behavior and requested cumulative audio-track discovery
across folders, retained through finalization and completion. Count unique audio
paths for the whole refresh; do not reset the displayed discovery count for each
folder or invent a total before discovery finishes. Compact the Activity popup
into short summary rows with inline dismissal; opening a job retains full details,
progress, results and existing actions.

Kyle's further UI feedback: replace the header Settings, Activity and theme text
with accessible gear, heartbeat and sun/moon controls. An indeterminate rotating
ring surrounds Activity while jobs are active; stop animation when idle and use
a static ring for reduced-motion preference. Preserve the popup interaction,
settings navigation, theme persistence, keyboard labels and mobile touch targets.


### Background matching completion correction — 2026-11-04

Kyle found that “Continue in background” finished matching but left selected queue
rows Pending until manual refresh. Completion events must remain pending during
selection/review instead of being discarded. Once not editing/reviewing, patch
finished visible albums from their saved review endpoints while preserving row
order, filters and selection, including Pending-filter rows. Apply the full queue
reload once selection/review is cleared. Retry transient reads. Detached matching
must signal completion even when the run finishes between Activity feed polls.
Validate Inbox and Library Import, review deferral, selection and same-page
background completion in the real container flow before publishing the candidate.
