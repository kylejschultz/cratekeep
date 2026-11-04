# Album file location — approved 2026-11-03

## Problem and result

Kyle confirmed the gnash credit correction and dedicated metadata modal work well.
Previously renamed Doechii and Thirty Seconds to Mars albums reportedly remain in
their old folders. The current modal exposes paths under Stored details and computes
Beets destinations on review, including unchanged metadata; the reported live move
failure has not been diagnosed. Do not claim a cause without inspecting evidence.

Add a File location section to the existing metadata modal, allowing an explicit
album move without changing tags. Preserve the accepted modal and credit behavior.
Lyrics remains paused and excluded.

## Proposed scope

- Show current album folder(s) and filenames, including split or multi-disc layouts.
- Show where the configured Beets naming rules would place the album; provide an
  explicit Use Beets naming action even when no metadata fields changed.
- Provide a folder browser within the configured Library root, with breadcrumbs,
  existing child folders and a new-folder name option. Select the exact destination
  album folder, rather than an ambiguously defined artist/parent folder.
- Manual relocation preserves current filenames and relative disc subfolders.
  It changes file locations/catalog paths without changing Artist/Album tags just
  to obtain a different folder name. Reject ambiguous split-source layouts with a
  clear explanation rather than flattening or combining them silently.
- Include chosen audio destinations, owned lyric sidecars and known artwork in
  Review changes. Browsing, creating a destination draft and reviewing cause no
  filesystem writes; create needed directories only when the move is saved.
- Reuse the existing move journal, exclusive destination creation, stale-state
  validation and rollback. Never overwrite another file. Keep unrelated source
  files; remove only empty old directories. Update managed references and preserve
  stable album/track identities. Do not move whole folders blindly.
- Manual moves stay inside the configured Library. No cross-library routing,
  arbitrary filesystem manager, external player rescan integration or bulk library
  repair in this change.

## Confirmed persistence behavior

Kyle approved remembering a manual album folder until explicitly choosing Use
Beets naming. Later metadata changes may rename filenames within that folder, but
must preserve the selected album directory and existing disc subfolders. The
location policy is part of the preview fingerprint and rollback boundary.

## Acceptance and validation

- [ ] Current paths and Beets destinations are visible before making any tag edit.
- [ ] An existing album can move with tags unchanged, through review and save.
- [ ] Picker destination means the exact album folder; nested/multi-disc layouts
      retain their relative structure; ambiguous layouts are explained.
- [ ] Folder navigation and draft folder creation remain inside Library, rejecting
      traversal/symlinks and collisions. Cancel makes no filesystem changes.
- [ ] Sidecars/artwork and catalog paths follow; unrelated files remain; failures
      restore the prior state or expose retained recovery files accurately.
- [ ] Chosen persistence behavior is tested through a later metadata edit.
- [ ] Existing gnash credit editing and modal draft/discard behavior still work.
- [ ] Reproduce an old-folder/renamed-metadata fixture, verify the explicit Beets
      organize action, and investigate a concrete live path/config example only
      with available read-only access. Do not repair real media during diagnosis.

Run relevant backend/frontend gates and focused browser/container checks with
synthetic media. Inspect desktop/mobile layout and actual resulting file/catalog
state. Deliver a tested linux/amd64 local-registry candidate and a focused manual
checklist after spec approval; no PR, merge or live deployment in this scope.
