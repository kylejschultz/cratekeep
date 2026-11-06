# v1.1 review polish

Approved 2026-11-04: Kyle asked to address the full-app Taste findings and include
these fixes in v1.1. Continue the accepted 2ff1e91 combined candidate in its active
worktree, keeping this follow-up in focused commits.

## Scope and acceptance

- Library removal: successful previews do not create headline Activity changes.
  Failed checks stay visible; apply has one intent-specific entry distinguishing
  removing catalog entries while keeping files from deleting the listed audio.
  Details retains access to its preflight and durable results. Reclassify proven
  legacy removal rows once, preserving requests, replay identity and receipts.
  Existing confirmation, fingerprints, path guards and deletion behavior remain.
- Setup: match the application's Public Sans, neutral light/dark surfaces, control
  sizing and focus treatment. Honor the existing theme preference (default dark,
  as in the app), expose an accessible theme toggle and carry it into the app.
  Keep the current fields, validation, named Inboxes, path browser and submission;
  no new wizard or frontend framework. Ensure 390/768/1440px layouts fit and the
  folder browser, errors and add/remove source rows remain usable in both themes.
- Metadata tabs: all four labels are readable and reachable on a phone, including
  Stored details after keyboard activation. Use a deliberate two-column tab layout
  on narrow screens, preserving the desktop row, editing state and reachable footer.
- Library removal Cancel: keep the escape action neutral and reserve danger styling
  for the consequential action. Trace the actual rendered cause before changing it.

Non-goals: paused lyrics, multiple Library destinations, import engine changes,
new retention rules, a general redesign, unrelated lint/spacing cleanup or release
publication. Source and mutation safety checks must remain intact.

## Validation and delivery

Use implement-spec/Ponytail lite and Taste's app-screen checks. Add meaningful
regressions for removal presentation/receipts and normal keep/delete outcomes;
verify phone tab bounds/keyboard access and setup theme persistence, validation,
folder browsing, adding/removing sources and successful first-run submission with
small generated fixtures. Compare desktop/phone screenshots in light/dark, plus
768px bounds. Run typecheck, lint, frontend tests/build, full backend tests and
diff checks. Review generated bundles and smoke-test the final amd64 image.

Deliver one combined candidate through the already-authorized local registry,
verify both tags/digest/source/platform and provide a focused checklist. Kyle tests
before PR delivery. No live service replacement or GitHub release is authorized.

Supporting review:
/Users/kyle/localDev/notes/cratekeep/reviews/2026-11-04-full-app-taste-review.md

## Acceptance evidence — 2026-11-04

Implemented the approved scope. Cancel's baseline computed style was already
neutral; React reused the destructive button node, briefly animating its old red
color into Cancel. A distinct element key prevents that flash and autofocus puts
the escape action first. No redundant color override was added.

Passed: 167 frontend tests, typecheck, lint/build, 667 backend tests (30 existing
skips), and diff checks. Native generated-media browser checks cover both themes
at 1440/768/390px, real phone wheel scrolling, directory browsing, validation and
setup submission, metadata drafts/keyboard access, neutral Cancel focus, six quiet
previews, and actual keep/delete outcomes with saved preflight receipts. No browser
page errors. Four fresh Taste sets (12 viewport renders) completed successfully.

Evidence: /Users/kyle/localDev/data/cratekeep/artifacts/v1-1-review-polish/
Final image smoke and registry identity are recorded in the candidate report.
