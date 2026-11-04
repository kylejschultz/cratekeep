# Grouped Activity history and clearing

Status: approved by Kyle, 2026-11-03. Follow-up to process-progress.md;
continue feat/process-progress atop the accepted artist/location work. Lyrics stays
paused. Implementation continues from candidate 1684807.

## Problem and inspected behavior

The popup repeats automatic scans for each Inbox and keeps only five visible recent
jobs. Its API returns 30 recent records, storage prunes after 100 terminal jobs, and
client-only dismissal retains at most 100 IDs. Kyle wants one Inbox scans item,
repeat counts, timestamps, Clear per item, Clear all, and no upper history limit.

## Proposed behavior

- Collapse scans across all named Inboxes into one "Inbox scans" row. Display a
  repeat count (×2, ×3, etc.) and the latest run's local timestamp. Opening the row
  shows individual runs with Inbox names, timestamps, outcomes and existing details.
- Group other repeated operations by operation and scope, with the same repeat
  count, latest timestamp and expandable run history. Distinct operations/scopes
  remain distinct. Group only history presentation; every actual run/result stays
  independently identifiable.
- Groups containing active jobs show their active count and live state. Keep
  failures/partial/interrupted outcomes visible in the summary; a successful latest
  scan must not silently hide an earlier uncleared failure. Full details retain
  stage/counts, errors, receipts, safe stop/resume and existing result actions.
- Use compact rows. Show an explicit "Clear" action for a group's finished history
  instead of the current × dismissal. Individual finished runs can also be cleared
  in expanded history. Clearing never cancels a job; active runs remain visible.
- Add "Clear all" to the popup header. It clears all finished Activity history,
  including groups/runs beyond the currently loaded page. Jobs completing after
  the action's server snapshot remain visible. Disable it when there is nothing
  finished to clear. No additional confirmation for clearing this UI history.
- Clearing persists across reloads and browsers using server state. It hides only
  Activity history, preserving import receipts, filesystem/database outcomes and
  request-id deduplication. It never deletes media or triggers new execution.
- Remove the 5/30/100 overall-history limits and automatic terminal-history pruning.
  History is retained until explicitly cleared, with no fixed record-count or age
  ceiling. Fetch groups/expanded history in pages, with access to every remaining
  entry; paging is a response-size bound, not a history retention limit. Polling
  must not download the entire lifetime history every 1.5 seconds.

Confirmed: requested grouping, counts, timestamps, Clear/Clear all and no upper
limit. Proposed interpretation: "no upper limit" means no arbitrary history cap;
use incremental loading to keep the monitor responsive. Clear affects finished
history only, with server-persisted visibility. Kyle approved these choices.

## Constraints and non-goals

Reuse the current SQLite activity registry and worker pipeline. Keep existing
execution serialization, route validation, progress tracking, duplicate submission
protection, terminal receipts and interrupted-run behavior. Do not retain every
per-file progress tick or add a second job system. This is Activity history, not
external notifications, a full library audit, or a replacement import-results log.
Previously pruned records cannot be reconstructed; preserve all remaining records
and avoid resurrecting previously dismissed history during migration where known.

## Acceptance

- [ ] Scans of Manual, slskd and Lidarr appear as one Inbox scans row with an accurate
      repeat count, latest timestamp and individually accessible run history.
- [ ] Repeated other operations group by operation/scope; distinct work stays apart.
- [ ] Active counts and errors remain accurate; grouping does not conceal failures.
- [ ] Clear works per group and individual finished run, persists after reload and
      does not cancel active work or delete receipts/media/deduplication state.
- [ ] Clear all clears every finished entry, including unloaded pages, while active
      runs and later completions remain visible. Other clients reflect clearing.
- [ ] More than 100 completed runs survive restart and remain reachable with correct
      group counts; more than five distinct groups remain reachable.
- [ ] Active polling and expanded history use bounded responses; history pagination
      has stable ordering with concurrent completions and clearing.
- [ ] Labels, keyboard access, mobile rows, timestamps and active-ring behavior work.

## Validation and delivery

Add behavior tests for grouping across Inboxes, repeated operations, mixed outcomes,
clearing during active/concurrently finishing jobs, persisted clearing, restart with
more than 100 records, pagination and duplicate requests after clearing. Run the
required frontend/backend checks and focused synthetic browser checks across
390/768/1440px. Build and publish the updated combined linux/amd64 local-registry
candidate under existing task authority; verify its source/digest and provide a
short checklist. No merge, release, live deployment or progress PR is authorized.
