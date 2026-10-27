# Per-Inbox discovery modes and live updates

Status: Approved by Kyle on 2026-10-26; implementation in progress.
Date: 2026-10-26
Baseline: origin/dev at 2a5546871c240fef6f4d4db4f8fef1364f23918c.

## Problem and result

Inbox inventory currently refreshes when its page opens or the user chooses
Refresh. A running server with no browser open does not discover new arrivals.
Each named Inbox should have an independently saved discovery mode that discovers
arrivals while Cratekeep is running, without importing or changing source files.
An open Inbox should update after background discovery without a manual reload.

This is the first v1.1 feature. Multi-library support (#8) follows after this
candidate's review; lyric management (#10) follows multi-library support. Each
feature gets its own spec, approval, implementation and candidate.

## Confirmed product decisions

Confirmed from Kyle's review:

- Remove custom intervals. Default existing and new Inboxes to **5 minutes**.
- Keep manual Refresh available regardless of discovery mode.
- Offer an **Instant** watcher mode alongside the interval presets, with arrivals
  appearing in the open Inbox automatically.
- Each Inbox independently selects its own mode. Download clients that deliver
  one file at a time can use an interval or Manual only instead of Instant.
- Enabled Inboxes scan once at startup, with no replay of missed scans.
- Both interval and Instant discovery honor each Inbox's automatic matching opt-in.
  Matching only proposes metadata; importing and applying tags still require action.

Behavior covered by the complete spec:

1. Offer **Manual only**, **Instant**, **5, 15, 30, 60 minutes**, **6 hours** and
   **24 hours** per Inbox. Manual only replaces the earlier Off label: it disables
   background discovery, while page-open discovery and explicit Refresh remain.
   Instant uses filesystem events, coalesces bursts, and waits briefly for changed
   files to settle before reconciling inventory. Target a few seconds after a
   stable arrival under light load on an event-capable local mount; not zero
   latency and not a guarantee that a paused downloader has finished.
   Include a five-minute reconciliation scan in Instant mode to recover missed
   events. If a watcher cannot start or fails, show that the Inbox is using the
   five-minute fallback and retry watcher setup without a busy loop. Accessible
   network mounts can silently omit events; the reconciliation scan remains
   necessary even when watcher startup succeeds.
2. An interval or Instant Inbox gets one scan after server startup, then waits its interval
   after each completed attempt (the safety interval for Instant). Do not replay
   intervals missed while stopped.
   Enabling a schedule or changing its path/interval makes it due once after
   settings save; unrelated saves and renames do not reset its schedule.
3. Successful scheduled discovery honors that Inbox's existing automatic matching
   setting. Only Inboxes opted into matching can make MusicBrainz requests.
   This is matching for review, never automatic import. Existing page-open and
   explicit Refresh behavior remains discovery-only.

Kyle approved the complete spec and requested a scan event boundary for future
notifications. Emit a completion signal only after inventory/reviews are saved,
with a scan identity, stable Inbox ID, discovery trigger, new-arrival count and
bounded inventory IDs. Save these in the existing scan summary. Future notification
subscribers should enqueue work rather than perform delivery inside the signal.
Do not implement notifications, delivery or durable notification deduplication.
All product questions above are resolved. Routine implementation choices within
that approved scope do not require separate approval.

## Scope and observable acceptance

- [ ] Settings provides a clearly labeled discovery mode for every named Inbox,
      including first-run setup. Values follow stable Inbox IDs through renames,
      row edits, deletion of other rows, settings saves and server restarts.
- [ ] Legacy settings without a discovery mode migrate to the five-minute default,
      preserving Inbox identity, paths, matching opt-ins and existing inventory.
      Invalid submitted modes or non-preset intervals show a useful error without
      partially saving.
      Older submissions omitting intervals preserve existing values by ID.
- [ ] Two Inboxes with different enabled intervals scan independently while all
      browser windows are closed. Manual-only Inboxes do not scan on a timer or startup.
      First-run setup does not start scans before configuration is complete.
- [ ] Startup, enable/change behavior and restart catch-up follow the approved
      decision above. Disabling or removing an Inbox cancels future work; a scan
      already in progress may finish safely. Old paths must not be committed as
      current inventory after an Inbox path is changed.
- [ ] A long scan cannot overlap another scan of the same Inbox. Watcher, scheduled,
      page-open and manual scans coordinate with matching/import work and
      configuration changes. Busy work is deferred without growing an unbounded
      queue or creating a tight retry loop. One due or unavailable Inbox cannot
      permanently starve other Inboxes.
- [ ] Scheduled discovery updates persisted inventory and album reviews using
      existing discovery rules. It never imports, moves, deletes or retags music,
      removes empty folders, replaces artwork, or changes unrelated metadata.
      Source files, sidecars and download-in-progress directories stay intact.
- [ ] Missing, unreadable or unsafe roots yield an error scoped to that Inbox.
      Failed traversal preserves its last good inventory and reviews; healthy
      Inboxes continue and later attempts recover after the root returns.
      Background scans do not create roots. Startup must not recreate a missing
      previously configured Inbox and then interpret it as an empty source.
      Setup may still create explicitly configured new folders.
- [ ] Last scan time and an actionable latest error are visible per Inbox, using
      existing status surfaces where possible. A successful later attempt clears
      its prior scan error. No second notification system is introduced.
- [ ] Scheduled matching reuses bounded, deduplicated matching
      work for eligible unmatched/stale reviews, respects saved/manual decisions,
      and avoids repeated provider calls for unchanged completed matches. Matching
      disabled means no provider call from scheduled discovery.
- [ ] Opening or switching Inbox still performs its existing discovery-only scan.
      Paging/filtering does not scan disk. Manual Refresh retains its explicit
      discovery plus empty-directory cleanup behavior. Manual-only mode does
      not disable either action.

- [ ] Instant detects additions, modifications, rename/move-in and removals,
      including newly created nested directories. Burst events coalesce into
      bounded work rather than a full scan per event. Ignore unrelated file
      events where possible; incomplete or changing files are retried safely and
      never trigger imports. Recheck files before matching after a settling delay.
- [ ] Instant handles watcher startup failure, watch exhaustion, root loss,
      reconnection and shutdown without silently abandoning discovery. Show
      watcher errors/fallback per Inbox; reconciliation recovers missed events.
- [ ] A completed background discovery updates the open selected Inbox without
      page reload or another filesystem scan from the browser. Keep filters,
      pagination, selections and active review stable; defer applying new results
      during an active edit, matching/import operation or confirmation dialog,
      then reconcile against current server state. No discarded edits or stale
      selected album objects may be used for imports. Reconnection catches up.
      Manual-only sources need no ongoing live-update subscription.

## Implementation boundaries

Extend the existing persisted `inboxes_json` rows with mode/interval; do not add a
second source registry. Reuse inventory reconciliation and matching seams in
`beets_mvp/__init__.py`, with a small server-owned scheduling component if useful.
Prefer existing dependencies and the standard library; a small maintained
cross-platform watcher dependency is allowed for Instant rather than a custom
inotify implementation. No task broker or external scheduler is needed.
File events enqueue discovery; they must not mutate inventory or the DOM directly.
The browser obtains fresh queue data only after inventory commits. Choose a small
live-update mechanism compatible with the existing four request threads; do not
exhaust them with a long-lived connection per browser tab. Bounded polling of a
cheap persisted revision/status is acceptable; it must not poll the filesystem.
Live updates should arrive within a few seconds after a completed scan when idle. Use the current supported container model: one Gunicorn
worker with four threads. Ensure one scheduling loop per serving process, safe
shutdown, no duplicate loop under the development reloader, and no real timer
threads in ordinary unit tests. Do not claim multi-worker scheduling support.

Use existing lock ordering and audit all Inbox scan entry points touched by the
change. Revalidate source identity before committing scheduled results; coordinate
settings changes so obsolete paths and matching jobs cannot become current work.
Use monotonic time for runtime deadlines and persisted timestamps for display.
A scan duration does not produce a backlog of missed ticks. Page-open/manual scans
need not reset the scheduled cadence. Persist configuration, not every timer tick.

The scanner already fails on missing roots and traversal errors before marking
inventory absent. Reuse that behavior. An accessible but empty mountpoint is not
proof that the intended storage is mounted; automatic detection of an otherwise
indistinguishable empty mount requires a separate mount/sentinel policy and is
outside this feature. Do not promise such detection.

UI changes extend the existing Inbox settings and status patterns. Update both
React Settings and first-run setup, regenerate frontend bundles, and preserve
form validation, keyboard access and labels.

## Non-goals

Multiple libraries/routing, lyrics, Discord/Slack notifications, automatic import,
custom numeric intervals, cron expressions, daily clock-time scheduling, catch-up
replay, distributed/multi-worker scheduling, and streaming updates to every app
screen. Live updates cover the selected Inbox queue and its scan status. No media
relocation or schema redesign for the later v1.1 features.

## Validation and candidate

Automated behavior checks should cover settings migration/round-trip/invalid
input, two independent schedules and Manual only, startup and settings changes, slow/busy
scans, shutdown/reloader ownership, source identity changes, scoped failure and
recovery, and the confirmed automatic matching behavior. Use an injected clock or
explicit scheduler steps rather than waiting minutes in tests. Cover burst
coalescing, changing/partial files, moved nested folders, watcher failure/recovery,
missed-event reconciliation, and live updates during filters/selection/review,
reconnection and source switching. Verify watcher events in the Linux container
with disposable bind-mounted media; document the tested mount and limitations.
Retain page-open,
pagination and manual Refresh regressions. Use disposable tagged audio fixtures
and stub providers; no real library or account data.

Run the repository checks with Node 22 and isolated Python 3.12:
`npm run typecheck`, `npm run lint`, `npm test`, `npm run build`,
`python -m pytest -q`, and `git diff --check`. Review generated bundle changes.
Try a focused container/runtime check proving startup and discovery without a
browser, source preservation and recovery from an unavailable Inbox. Run the
container smoke check for the server lifecycle changes; record concrete gaps.

First implementation delivery is an isolated local test image with its source
identity, platform, image tag, disposable data paths, checks and remaining gaps.
Build/export linux/amd64 for Unraid testing under the task artifact directory,
with checksum and load/run instructions; do not replace a running service or
publish to a registry without task authorization. A build alone is not functional
acceptance. Retain the same worktree for Kyle's feedback.

Focused manual checklist for the candidate:

1. Save Instant and an interval for two disposable Inboxes, leave another Manual
   only, and
   confirm the values survive restart and rename.
2. With Inbox open, copy/rename a disposable album into the Instant source and
   confirm it appears without reload, preserving selection and active edits.
   Then close the browser, add small test albums, wait for enabled scans, and open
   Cratekeep and inspect scan timestamps/inventory. The runtime evidence must
   distinguish scheduled discovery from the extra page-open scan.
3. Confirm the configured matching behavior on one opted-in and one opted-out
   Inbox; review without importing and verify source bytes/folders are intact.
4. Temporarily make one disposable root unavailable: confirm its error and last
   good inventory, healthy Inbox progress, and recovery after restoring it.
5. Try a scan while matching/importing test fixtures; confirm safe deferral,
   then verify Manual only, page-open discovery and explicit Refresh still behave as
   described.

Research reference: [watchdog upstream documentation](https://github.com/gorakhargosh/watchdog)
describes native event observers and explicitly requires polling for CIFS. This
supports keeping reconciliation as a fallback; it does not prove event delivery
on Kyle's particular Unraid mounts.

Spec approval authorizes implementation of the agreed scope. Candidate delivery,
Kyle's acceptance, an authorized PR with fresh CI on its actual head, human merge,
release publication and deployment are separate milestones. No criteria above
are complete merely because this spec has been saved.

## Implementation evidence

The implementation exposes `beets_mvp.inbox_discovery.scan_completed` after both
inventory and album reviews commit. Its bounded payload includes scan identity,
Inbox identity, trigger, totals, added inventory IDs and a truncation flag. The
scan summary retains those IDs/counts; this is an extension seam, not a durable
notification queue or delivery guarantee.

Background discovery uses the supported one-worker lifecycle and shared locks.
Instant settles changes with two stat snapshots separated by two seconds. The
open queue polls a cheap revision marker every two seconds and defers replacement
of its data while a selection, review or operation is active. Manual-only status
is checked every 30 seconds to notice settings changed in another tab, without
refreshing its queue or traversing the filesystem.

Local automated validation: frontend type/lint/build, 103 frontend tests, and
509 backend tests passed (30 existing skips). Scheduler tests cover fake-clock
intervals, source isolation, source recovery, watcher fallback/replacement,
reloader ownership, matching opt-in and committed event payloads. Container/runtime
verification and Kyle's manual acceptance are recorded with the local candidate;
these automated results do not mark manual acceptance complete.

## Candidate feedback — 2026-10-27

Kyle requested the automatic matching preference beside each Inbox's discovery
mode as a checkbox. Remove the Metadata-tab multi-select and its Select all/Clear
controls; keep the match threshold on Metadata. Preserve the existing per-Inbox
saved boolean and form field, with new Inboxes still requiring their first save
before matching can be enabled. This is a settings presentation change, not a
change to scheduling, matching or import behavior. Verify independent toggles,
identity retention after rename/delete, and round-trip persistence.

## Candidate feedback — grouped Inbox editor — 2026-10-27

The inline Name/Path inputs became crowded when editing. Keep the normal rows
compact, but Edit/Add expands a full-width labeled form for Name, Inbox path,
Discovery and matching. Fields align in a two-column grid on desktop and stack on
phones. Done applies the entire draft to the unsaved Settings form; Cancel drops
all draft fields. The existing Save settings action remains the persistence step.
Preserve stable Inbox IDs, existing preferences, new-Inbox matching opt-in rules,
validation and the single-editor guard. Verify edit/add/cancel/Done/save plus focus
and no overflow at 1440, 768 and 390 pixels. Keep the surrounding design system.

## Candidate feedback — compact read-only summaries — 2026-10-27

Default Inbox rows show the name/path and static Discovery and Auto-match On/Off
values. Remove editable controls from normal rows; all preference changes belong
inside Edit. Use one compact summary line below the path, wrapping on phones.
Retain hidden form fields for every Inbox so Save settings from any tab preserves
unchanged preferences. Done updates the summary; Cancel leaves it unchanged.
Keep the grouped editor and stable IDs. Verify no checkbox/dropdown exists in a
collapsed row, form submission retains each value, and rows shrink without losing
readability at desktop/tablet/phone widths.

## Candidate feedback — results cleanup and page selection — 2026-10-27

Kyle accepted the Inbox settings and verified Lidarr arrivals. After a completed
Inbox batch, offer Clear imported from Inbox beside Close results. Target only
successful copy-import receipts from this batch, never failed/unknown/blocked or
not-started albums, replacements, previous imports, or Library Import. Reuse the
existing file-checking preview and explicit permanent-removal confirmation; retain
Library copies, durable lost-response recovery, and safe refusal of changed files.
Opening removal closes results; cancelling confirmation leaves files untouched.

Checkbox selection is scoped to the visible page in Inbox and Library Import.
Changing pages, filters, search, or source clears selection. Refresh reconciles
selection with visible albums so counts and batch requests cannot contain hidden
rows. Verify both directions of pagination, filtering/search, select-page, and
actual submitted album IDs. Browser coverage must check retained Library bytes
after results cleanup and no file changes on cancellation.
