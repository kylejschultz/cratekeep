# Collaborative albums in Library Artists

Approved · 2026-11-06 · [Issue #34](https://github.com/kylejschultz/cratekeep/issues/34)

## Problem and result

Library Artists currently groups and filters by one album artist display string. A shared release credit can appear as a combined artist entry, leaving the album out of the individual artists’ views. Each primary album artist should lead to the same managed album, with one album identity and one entry in the unfiltered Albums view.

## Scope and decisions

- Use the ordered, structured Album Artists credit as the source of membership. Use the stored album list first and the existing managed album detail fallback to the first track’s **Album Artists** list when the album list is absent. For legacy records with neither list, retain the existing single-label fallback. Treat a compound display string as one label until structured credits are available through an explicit credit edit or a reviewed match.
- Preserve the combined Album Artist display string and its join phrases. Keep existing album labels and Beets naming semantics. Track Artist and Track Artists remain separate. A guest’s track credit alone never adds a Library artist membership.
- Use the same membership for the Artists list, artist search, artist links and filtered album results. Keep artist counts, genres and existing artist summaries consistent with that membership. A collaboration contributes one album to each credited artist, while collection totals still count the album and its tracks once. Preserve bounded pagination and the existing general Library search by album, artist or track.
- Preserve the individual names from structured MusicBrainz release credits through matched Inbox import, Library adoption and managed rematch, alongside the existing display credit. Keep the existing preview/save and tag/catalog write paths. Preserve known credit IDs where supported and keep names and IDs aligned. Keep existing behavior for older candidate snapshots without structured credits and for keep-existing imports, which should preserve valid local Album Artists lists. Existing explicit list edits remain the way to add or remove a credited name manually.
- Out of scope are automatic retagging or backfill, name splitting or alias merging, artist page redesign, missing-track cleanup and bulk metadata edits. Existing scalar-only albums can be corrected individually with the Album artist credits editor or reviewed rematch.

## Code evidence

On the verified dev baseline, `_library_page_data` in `beets_mvp/__init__.py` uses a scalar artist expression for grouping, counts and exact filtering. `_managed_album_payload` already exposes stored structured credits for editing. `_normalize_release` in `beets_mvp/musicbrainz.py` flattens MusicBrainz release credits into a display string, and `_candidate_item_values` maps that scalar into Album Artist. Trace the shared candidate data through preview and apply, including any path that filters writable fields, so the browse fix works with both existing and newly matched records.

## Acceptance criteria

- [ ] Synthetic releases model *Watch the Throne* and *Collision Course*. JAY-Z’s artist view shows both albums, Kanye West’s shows the first, and Linkin Park’s shows the second. Each link opens the same managed album ID as the unfiltered Albums view. A guest credited on one track gains no Artist listing from the album.
- [ ] Artist search and direct filtered links use the same case-insensitive membership. Repeated or case-variant credits count once per album, punctuation within one credit stays intact, and counts and pagination agree with the displayed results.
- [ ] Existing records with structured credits work on first browse without any credit rewrite. Item-level Album Artists fallback, single-artist albums and legacy scalar-only records remain usable. An ambiguous scalar such as a name containing an ampersand never creates inferred memberships.
- [ ] A matched import, adoption and rematch with structured release credits retain both names in managed records and written tags. Preview agrees with apply. Older candidate snapshots and keep-existing flows remain usable. Browse, search and pagination perform no credit, tag or file writes.
- [ ] Adding or removing a name through the existing Album artist credits editor changes browse membership after save. Display-only edits preserve collaborators. Existing file placement, review and failure behavior continue to follow the current Beets rules.

## Validation and candidate

Extend the existing MusicBrainz, import and artist-tag fixtures with generated audio and a mocked release payload. Cover real stored credit lists, both artist drill-downs, a track guest, a repeated credit, legacy data, and a result set spanning multiple pages. Run relevant backend and frontend checks, regenerate any affected bundles and inspect the diff. Attempt a focused browser check of Artists search, click-through and the metadata editor on disposable data.

After approval, deliver an isolated local test image with startup/health evidence, source SHA, platform and validation gaps. For manual acceptance, search each artist, open the collaboration from both views, confirm it appears once in Albums, and remove one credit on a disposable copy through Review changes and Save changes. Verify that artist’s listing updates and the guest still gains no listing.
