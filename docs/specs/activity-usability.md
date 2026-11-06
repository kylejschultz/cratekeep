# Activity usability follow-up — 2026-11-04

## Approved corrections

Kyle accepted All Inboxes and requested the following candidate adjustments:

- A live operation owned by the album inspector displays progress there once;
  omit that exact job from the left queue while preserving unrelated jobs and
  the global Activity indicator. Release ownership when the inspector finishes
  or unmounts. Retain the existing three-second detailed-progress threshold.
- Activity closes on a click/tap outside, Escape, or its toggle. Controls inside
  remain usable; opening grouped history closes the popover without closing history.
- The feed shows approximately three compact rows and scrolls internally, with
  keyboard access. Keep Clear all and pagination accessible outside the scroller.
  All history remains available; this is a viewport bound, not a retention limit.

## Approved next increment — 2026-11-04

The popover should answer: **What is running, and does anything need me?**
Use the existing Cratekeep type, neutral surfaces, accent and heartbeat control.
No new settings, dependencies or separate Activity dashboard.

1. Order active work first, then attention-required outcomes, then recent success.
   Keep at most three rows visible in the scrollable viewport. Use one status
   mark per row, a concrete action/source, and a quiet latest timestamp.
2. Name operations by user intent: importing an album, deleting Inbox files,
   saving metadata or moving files. Replace generic "Updating library files"
   where the request supplies enough context. Keep unknown legacy entries honest.
3. Show one entry per user action: successful internal preflight/check stages
   belong to that action's details. Never hide failed checks or lose their receipts.
   Do not group independent mutations merely because their endpoint is similar.
4. Show meaningful outcomes, such as imported albums or deleted files. Omit a
   misleading "0 / 1 albums processed" or "1 / 1 Inboxes" when the job's counters
   do not describe that operation. Missing counts are omitted, never invented.
5. Keep routine empty Inbox scans as the existing one quiet health/time entry;
   new arrivals and failures remain distinct. Completed work should recede.
6. Keep Clear all; replace repeated trailing "Clear" text with an accessible
   compact dismiss control. Preserve individual clearing, paged unlimited history,
   details, recovery/resume and import cleanup controls.

Acceptance: a mixed fixture with active import, failed match, successful metadata
edit, arrivals, repeated previews and quiet scans makes the active/failed jobs
identifiable without reading every row. One import is one feed item; a failed
preflight remains discoverable. Clearing preserves active jobs and execution
receipts. Verify desktop/tablet/phone, keyboard, outside click, empty/error/loading
states and existing pagination/recovery tests. Deliver a local amd64 candidate
before PR delivery. Kyle approved this scope in chat on 2026-11-04. Also fix short source pills (e.g. Lidarr) clipping beside short album titles while retaining long-name bounds.


## Implementation evidence

- Server-prioritized active/attention/recent groups, with priority-aware bounded
  cursor pages and unchanged unlimited retention.
- Concrete intent labels for import, matching, metadata, explicit file moves,
  artwork and Inbox actions; independent mutations keep their own entries.
- Successful internal checks are quiet but retain their request/results. Current
  single/bulk import (including All Inboxes), metadata and Inbox-action flows link them from action details; failed
  checks remain visible. Existing legacy receipts migrate once without deleting
  mutation evidence. Unknown jobs keep their honest original labels.
- Compact SVG state marks and accessible dismiss buttons, meaningful terminal
  outcomes and quiet timestamps. Cached matches are labeled cached, not assumed
  to be new matches. Empty scan status and separate arrivals remain.
- Short source names occupy their natural width; long names stay bounded with a
  full-name tooltip. Native browser verified Lidarr beside Eve 6 and long titles
  at 1440/768/390, alongside bounded long source names.
- Native browser passed real WAV import, inspector-owned progress, old failed
  checks prioritized over 25 new successes, linked preflight detail access,
  receipt-preserving clearing, Older/Newer, outside dismissal and keyboard scroll.
- Taste app inspector passed after SVG status marks replaced text checkmarks.
  Allowed covered-text because this temporary, dismissible popover intentionally
  overlays the page underneath; the closed screen is separately checked. The
  existing inspector blank-band warning remains outside this feed's scope.

Final full gates and registry identity are recorded in the excluded local handoff
and the dated candidate report; image packaging follows the clean checkpoint.

Bulk previews use the same quiet-success policy. A mixed import retains a list
of successful per-Inbox check references under its one action entry. Bounded
reference validation checks those source routes before attaching the links;
original action steps, fingerprints, mutation validation and idempotency remain.
Final native All Inboxes regression also passed mixed imports, imported cleanup,
deleting companions, selection/pagination, offline sources and long-name bounds.
