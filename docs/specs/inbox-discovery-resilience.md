# Inbox discovery reliability and release track titles

Approved fixes: Kyle authorized both findings from the 2026-10-27 bounded review,
and separately approved preferring release track titles over recording titles.

- An active/growing album must not postpone a different settled album in the same
  Inbox, including under continuous events beyond the safety interval. Settle at
  the existing physical album-folder boundary (including conventional disc folders).
  Keep unstable files out of background inventory/matching. Preserve last-good
  inventory, manual/preset behavior, source safety and no automatic import.
- Repeated live-update connection failures show a subdued inline Reconnecting
  status, without a toast/modal or the removed updates-waiting notice. Ignore a
  single transient failure, clear upon recovery, isolate state when changing
  Inboxes and preserve pending updates during reviews/selections.
- MusicBrainz release track titles are authoritative; use recording titles only
  when no release track title exists. Preserve live/demo/remix qualifiers and
  recording IDs/durations/artist-credit precedence. Re-querying a saved old match
  may be required because earlier normalized candidates discarded the track title.

Verify two folders under continuing events/growth, same-parent nested albums,
multi-disc grouping, delayed deletion, opt-in matching, missed-event fallback,
connection failure/recovery/source switch, and provider title/fallback behavior.
Run required backend/frontend checks, regenerate bundles, build a clean-source
linux/amd64 candidate and exercise container/browser checks. Give Kyle source,
platform, evidence and a focused retry checklist. Retain local candidate workflow;
no PR/merge/release/deployment is authorized by these fixes. Lyrics remains later.
