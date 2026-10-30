# Artist tags and metadata-driven paths

Approved implementation scope — Kyle, 2026-10-30. Independent fix branch from
origin/dev 39963ea; preserve paused Lyrics and broader settings/planning worktrees.
Use implement-spec with Ponytail lite. No Beets importer migration, presets/settings
redesign, discovery/title fixes or Lyrics workspace in this branch.

- Keep Artist/Artists and Album Artist/Album Artists consistent in file tags and
  Beets on Inbox import, Library adoption, edits and rematch. Re-saving a corrected
  name repairs stale singleton lists. Preserve genuine collaborators and IDs; Track
  Artist and Album Artist remain separate. No-op imports stay no-op.
- Explicit metadata saves/rematches synchronize managed paths using configured
  Beets templates: artist/album changes affect folders, title/track/disc changes
  affect filenames where those fields occur. Honor custom templates and do not
  force albumartist when a user chose artist. Re-saving metadata can fix an old path.
  Existing Library adoption remains in place; Inbox originals stay untouched.
- Move owned same-stem .lrc/.txt sidecars, preserve embedded lyrics and artwork,
  update item/album references, retain IDs, and prune only empty old folders.
  Preserve unrelated files. Known artwork follows moves without losing copies
  still used by remaining tracks or other albums.
- Preflight destinations, shared sidecar ownership, symlinks and library-root
  containment; never overwrite another file or silently choose a suffix. Roll back
  tags, moves, companion lists, artwork references and catalog after failures.
- Existing Save applies this focused fix directly; broader organization preferences,
  presets and move-preview UX remain in their separately approved settings scope.
  UI explains configured naming behavior and surfaces collision/recovery errors.

Validate generated audio fixtures for artist/list repairs, import/adoption no-ops,
folder/file changes, custom track-artist templates, unrelated tag/lyric preservation,
sidecars/artwork, stable IDs, collisions, symlinks and write/move/finalization rollback.
Run required backend/frontend checks, regenerate bundles, inspect diff and build an
isolated linux/amd64 candidate. Attempt container/browser checks. Deliver source/
platform/evidence and a focused checklist. Registry candidate authority carries
forward; no GitHub PR/push, merge, live-media repair or deployment requested.


### Explicit credit-list editing — approved 2026-10-30

Kyle reported a single stage identity credited under both a stage and legal name.
The prior fix retained ambiguous multi-name lists; scalar-only edits could not
remove the second credit. Add editable album/track artist-credit lists. Only an
explicit list edit changes membership; ordinary display edits preserve collaborators.
Synchronize list, credit/sort fields and corresponding artist-ID entries when
membership changes, preserving retained IDs and never attaching a removed ID to
another artist. Keep commas inside names intact. Include validation, rollback,
serialized file tags and UI behavior tests, then publish a fresh local candidate.

Names with commas remain one list entry. IDs for retained, identifiable names are
kept; new names never inherit a removed credit's ID, and ambiguous ID lists are
cleared rather than misattributed. Unknown new identities can be resolved by a
later rematch. Album Artist and Track Artist credit lists remain separate.
