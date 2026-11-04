# Inbox scan latency (#22)

Authorized 2026-11-04: investigate v1.0.1 scan latency and compare v1.1, then fix Inbox loading while scans run. Independent fix based on dev e52a416; preserve the process-progress candidate and existing PRs.

- Read persisted queue albums immediately; opening discovery runs separately without the album-read timeout.
- Keep saved albums, paging/search and read-only review available during discovery. Show scan status/errors separately; do not claim an empty source while discovering.
- Reload committed results after opening discovery, including Manual only. Defer replacement while selecting/reviewing; keep existing automatic discovery/matching and explicit Refresh cleanup semantics.
- Preserve import/mutation serialization, file identity checks, source isolation and recovery receipts.
- Allow database readers to see the last committed inventory during large scan writes. Read changed-file tags before the inventory write transaction; remove quadratic completed-membership lookup during synchronization.
- Verify generated 5,472-track / 3,070-album benchmarks against v1.0.1 and the current v1.1 candidate. Local storage fixtures do not establish the reporter's exact Unraid I/O cause.
- Run frontend/backend gates, focused slow-scan checks and a local linux/amd64 image. No registry publication, merge, release or service replacement is requested.


## Combined v1.1 candidate — 2026-11-04

Kyle authorized combining this fix with merged metadata/location PR #26, Activity
and Beacon. Keep actual tag-reading progress before the inventory write phase;
report saving separately. Verify slow opening scans with Activity enabled, saved
rows/paging/review/selection, completion catch-up and failure recovery. Retain
browser dismissal/disclosure and metadata/file-location flows. Publish one combined
linux/amd64 candidate; no live deployment, PR publication or release is authorized.
