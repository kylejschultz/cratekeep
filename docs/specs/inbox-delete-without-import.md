# Delete from Inbox without importing

Status: approved by Kyle on 2026-10-27; implemented candidate awaiting functional acceptance.
Validation evidence and candidate identity are recorded in .local-dev/CANDIDATE.md
and the existing local candidate report.

## Problem and result

Bad downloads and unwanted individual songs currently require an external file
browser. Add permanent deletion directly in the named Inbox without creating an
import, modifying Library files, or requiring a match decision.

## Scope and controls

- **Delete from Inbox** in an unimported album's inspector, and on the current
  page's selected-album toolbar. Multiple albums can be deleted together.
- A local-track list in the inspector offers **Delete track** for each original
  file, even when no MusicBrainz candidate exists. This list is independent of
  the candidate comparison so missing/extra mappings cannot target the wrong file.
- Album deletion removes that album's audio and accompanying files/subfolders
  using the existing physical album grouping. Preserve unrelated/unselected audio
  and accompanying files in shared folders; never expand into the Inbox root or
  unrelated artist folders. Follow the existing whole-album cleanup rules.
- Individual-track deletion removes only that selected audio file and prunes
  empty directories. Other tracks and companion files stay. To remove an entire
  single-song download including its accompanying files, use album deletion.
- Imported albums retain the existing Remove imported action and its Library-copy
  checks. Do not use this new action to bypass import recovery or receipt guards.
- Permanent deletion is explicit, with no undo/trash implementation in this scope.
  No generic file browser, archive management, filesystem move or Library removal.

## Confirmation and safety

Use the existing server-side preview, fingerprint, no-follow checked unlink and
durable action-result pattern in beets_mvp/inbox_imports.py. Extend it with an
explicit unimported album/track selection scoped to one configured Inbox; never
accept an arbitrary filesystem path or a missing selection meaning delete all.

Show selected album/track names, affected file paths and total count in a themed
confirmation. Clearly state that files are permanently deleted without importing,
and that Library files are unchanged. Cancel is always non-mutating. Freeze the
selection during preview/apply and disable conflicting matching/import/removal.

Validate current source ownership, inventory membership, root boundaries and file
identities. Refuse symlinks, shared hard links, changed files, overlapping roots,
active/recovering imports and imported receipts through this action. Revalidate
at apply time. Preserve new arrivals outside the confirmed plan. A lost response
reads the recorded result and never silently repeats deletion. Partial failure
reports exact removed/remaining files and retains a safe recovery path.

After deletion reconcile inventory/reviews, remove vanished rows and stale
selections, and retain unaffected matches. Surviving albums whose track membership
changed must refresh their match evidence before importing against the old mapping.

## Acceptance checklist

- [ ] Delete an unmatched/bad album without matching, approving or importing it.
- [ ] Its accompanying .nfo/.jpg/nested artwork leave with its exclusive folder;
      unrelated siblings, shared/unselected audio and the Inbox root remain.
- [ ] Delete one song from a multi-track album; remaining audio/companions stay,
      its track count updates and stale matching cannot drive an import.
- [ ] Delete a single-song album including companions via the album action.
- [ ] Batch delete targets only explicit checked albums on the visible page.
- [ ] Cancel, stale previews, source changes and unsafe links delete nothing.
- [ ] Concurrent/recovering/imported albums cannot use the unimported-delete path.
- [ ] Library files and Beets records remain unchanged; no import job is created.
- [ ] Partial failure and lost responses retain durable, scoped results and never
      cause a repeated blind delete; late arrivals survive.
- [ ] Both desktop and phone can reach the local-track list and confirmation.

## Validation and delivery

Add backend behavior tests for ownership/selection, one-track reconciliation,
folder companions, shared/root boundaries, changed previews, links, concurrency
and durable partial/lost-response handling. Add UI coverage for album/track/batch
entry points, explicit scopes, cancellation and refresh. Run project typecheck,
lint, frontend tests/build, backend suite and diff checks. Attempt focused real
browser/container checks using disposable media; do not touch Kyle's live Inbox.

Deliver the next linux/amd64 local registry candidate through the already
authorized candidate workflow, verified against its source SHA, with checks/gaps
and a short manual checklist. Keep the current task worktree; no GitHub push,
merge, release or service deployment is authorized by this spec. Kyle tests the
candidate before PR delivery. Implementation uses implement-spec and Ponytail lite.

## Decisions

Confirmed: deletion without importing, including an individual unwanted song.
Approved defaults: permanent deletion with explicit confirmation; album/batch
folder cleanup and individual-file deletion as described above. Kyle approved this scope before implementation.

## Approved follow-up

Remove the transient Inbox updates waiting message. Retain the background
revision polling and review/selection deferral, along with actionable scan errors.
