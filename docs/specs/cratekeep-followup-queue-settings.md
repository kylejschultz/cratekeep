# Cratekeep queue, review, settings, and album-detail follow-up

## Shared queue contract

Library Import and every named Inbox display top-level folder scopes in pages of
25. Selection is durable while navigating pages; “Select this page” affects only
the currently visible page. Expanding a scope exposes its albums and accessible
album checkboxes. The folder checkbox reflects checked, unchecked, and mixed
album state.

Bulk import is explicit and confirmed. It attempts every selected album whose
selected candidate meets the configured match threshold and all existing track,
freshness, and duplicate-safety checks. One album failure does not stop later
ready albums. Failed, unmatched, below-threshold, or otherwise unsafe albums
remain retryable and are presented in order through the shared review dialog.

## Shared review contract

Candidate choices are compact while collapsed and summarize artwork, score,
artist/title, year, MusicBrainz release ID, and actual changed fields. Exactly one
candidate detail panel is open at a time; the selected candidate (or otherwise
the highest score) opens initially. “As Is” is the final choice and explicitly
keeps local metadata without a MusicBrainz association. Match, exact-MBID search,
duplicate preflight, retry, and source-specific execution semantics are retained.

## Settings contract

General separates named Inboxes from the managed Library path and explains the
optional Navidrome rescan integration. Matching has one automatic-matching
section, explains threshold behavior, and exposes one checkbox for each Inbox.
Metadata explains Feature in Title. Advanced gives Beets YAML the full content
width. Context-sensitive `?` controls have accessible names and explanatory
titles. Existing field names, defaults, validation, and persistence remain the
source of truth.

## Managed album contract

The album dialog opens read-only with artwork and track information. Manual
metadata and artwork replacement require explicit Edit mode; cancel discards
unsaved UI state. Artwork still uses preview then apply. MusicBrainz rematch is a
separate explicit preview/apply workflow. Existing fingerprint concurrency,
errors, and track-edit endpoints remain unchanged.
