# Cratekeep UI follow-up validation

- Date: 2026-10-14
- Host: `mr-mac-mini.schultzlocal` (`macOS-26.6.2-arm64-arm-64bit`)
- Managed workspace: `/Volumes/morrow-dev-node/state/openclaw/node-host/gateway-1603e0fe80c35ca709b0766f6624c53b/workspaces/aa47ea5767146700/8189c4d6aa75f48321bdb5fbeb6e10cf/99`
- Starting source: `432db32786a9372caeb2018b4367079efb438637`
- Reviewed implementation commit: `d181f66d5b497e16035de585c272b62b009aa91b`
- Reviewed tree: `5a5c5a9be80e23168cd64951f1b742edc13f625b`

## Outcome

- Inbox and Library render the same shared `_app_shell.html` sidebar/mobile-header resource. Inbox now inherits the Library shell markup, controls, collapse persistence, mobile navigation, footer, and short build identifier.
- The build label renders only seven SHA characters and retains CSS ellipsis/overflow containment.
- Library Import keeps the backend's existing genuine-error semantics. A provider or execution error now renders its supplied reason directly in the affected folder row; pending candidate work remains `needs-review`/`Tagged` with no error reason.
- Shared folder/review workflow and progressive loading behavior were not otherwise changed.

## Changed files

- `beets_mvp/templates/_app_shell.html`
- `beets_mvp/templates/inbox.html`
- `beets_mvp/templates/library.html`
- `beets_mvp/static/app-shell.css`
- `beets_mvp/static/folder-workflow.js`
- `tests/test_app.py`
- `tests/runtime_smoke.py`
- `CRATEKEEP_FOLLOWUP_REPORT.md`

## Validation

- Focused regressions:
  - `PYTHONPATH=. uv run --python 3.12 --with-requirements requirements-dev.txt pytest -q tests/test_app.py -k 'inbox_and_library_reuse_shell or folder_queue_distinguishes_pending or shared_folder_status_and_progressive_loading_contract or selected_scan_reports_partial_album_failure' --maxfail=1`
  - Result: `4 passed, 272 deselected`.
- Full suite and compile:
  - `PYTHONPATH=. uv run --python 3.12 --with-requirements requirements-dev.txt pytest -q`
  - Result: `276 passed in 122.80s`.
  - `PYTHONPATH=. uv run --python 3.12 --with-requirements requirements-dev.txt python -m compileall -q beets_mvp tests`
  - Result: passed with no output.
- Candidate image:
  - `docker build --build-arg BUILD_SHA="$(git rev-parse HEAD)" -t cratekeep:candidate .`
  - Result: local-only image `sha256:7f58dad1d19a0ed720021f0219caa3a0f1ad582f44faab1b2adcc35f360c7773`; not pushed or published.
- Official deterministic container/browser smoke:
  - `PYTHON=.venv/bin/python tests/container_smoke.sh cratekeep:candidate /tmp/cratekeep-followup-evidence.cbbotx`
  - Result: passed against the reviewed source changes; Chromium `156.0.8078.4`; `page_errors=[]`; `console_errors=[]`.
  - Covered Library Import `Ready`, pending-only `Tagged`, and genuine provider `Error` rows with visible reason; Inbox desktop/narrow UI; all primary pages at desktop light/dark and mobile; no horizontal overflow; shared 14px typography and 188px sidebar; progressive pages and the existing review/import workflow.
  - Evidence includes `03-folder-inline-settled.png`, `17-inbox-review-desktop-light.png`, `17-inbox-review-narrow-dark.png`, `18-inbox-desktop-light.png`, `18-library-desktop-light.png`, `19-inbox-mobile-dark.png`, `runtime-results.json`, and `SHA256SUMS`.

## Environment note and blockers

The first official smoke startup could not read `/app/beets_mvp/__init__.py` because this managed checkout materialized files as mode `0600`, while the official wrapper runs the image as the host UID. Read permission was normalized on the local source copy (`chmod -R a+rX beets_mvp`), the candidate was rebuilt, and the unchanged official smoke then passed. This changed no tracked content. No remaining implementation or validation blocker is known.

No deployment, registry push, GitHub write, production mutation, or real-media operation was performed.

## Reconciliation review

The continuation review on 2026-10-14 found the candidate changes in the reconciled worktree. The diff against `432db32786a9372caeb2018b4367079efb438637` was reviewed for shared shell reuse, bounded build-SHA output, visible error reasons, and pending-only status semantics. No code correction was necessary. Lantern committed the reviewed reconciled tree as `d181f66d5b497e16035de585c272b62b009aa91b` with tree `5a5c5a9be80e23168cd64951f1b742edc13f625b`.

Focused reconciliation validation used the existing local Python 3.12 environment:

- `PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_app.py -k 'inbox_and_library_reuse_shell or folder_queue_distinguishes_pending or shared_folder_status_and_progressive_loading_contract or selected_scan_reports_partial_album_failure' --maxfail=1`
  - Result: `4 passed, 272 deselected in 3.44s`.
- `PYTHONPATH=. .venv/bin/python -m compileall -q beets_mvp tests`
  - Result: passed with no output.

Per the continuation boundary, the long browser/container smoke was not repeated because review required no code correction. Its existing deterministic evidence above remains tied to the same source changes, now committed as `d181f66d5b497e16035de585c272b62b009aa91b` with tree `5a5c5a9be80e23168cd64951f1b742edc13f625b`.
