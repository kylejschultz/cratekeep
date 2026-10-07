# Cratekeep

![Cratekeep logo](beets_mvp/static/cratekeep-logo.png)

Cratekeep is a lightweight web interface for managing a [beets](https://beets.io/) music library. It lists music waiting in an inbox, provides a review-and-execute import flow, edits common metadata and file tags, and can request a Navidrome rescan.

> **Security:** Cratekeep does not include authentication. Run it only on a trusted network or behind an authenticating reverse proxy. Do not expose it directly to the public internet.

## Quick start with Docker Compose

Requirements: Docker Engine with the Compose plugin.

```sh
git clone https://github.com/kylejschultz/cratekeep.git
cd cratekeep
mkdir -p data/inbox data/library data/config
APP_UID="$(id -u)" APP_GID="$(id -g)" docker compose up -d
```

Open <http://localhost:8788>. Put albums or individual audio files in `data/inbox`. Imported files are moved to `data/library`; application and beets databases are stored in `data/config`.

On first launch, Cratekeep opens a setup screen. Type paths directly or use **Browse** to navigate the container and choose the mounted inbox and library directories, then optionally enter Navidrome rescan details. The choices are stored in `data/config/app.db` and remain editable from the Settings link. Browser paths are container paths; host paths are never exposed to the app.

When upgrading an existing Compose installation, stop the container and move the contents of `data/state` to `data/config` before starting the new image. The database filenames and formats are unchanged.

The compose file uses `ghcr.io/kylejschultz/cratekeep:latest` and also includes a local build definition. To build from your checkout, run `docker compose up -d --build`. On systems where `id` is unavailable, Compose defaults to UID/GID 1000.

## Unraid

Create an **Add Container** entry with these settings:

- **Repository:** `ghcr.io/kylejschultz/cratekeep:latest`
- **Web UI:** `http://[IP]:[PORT:8788]/`
- **Port:** container `8788`, mapped to a host port of your choice
- **Paths:**
  - `/data/inbox` → an inbox share
  - `/data/library` → your music library share
  - `/data/config` → `/mnt/user/appdata/cratekeep`
- **Variables:** none required; Cratekeep generates its signing key on first boot

The container runs as UID 10001 by default. Ensure the mapped directories are writable by that UID, or use Unraid's container advanced settings to set an appropriate numeric user such as `99:100`.

For an Unraid deployment, mount each host directory at any clear container path and choose those container paths in the setup browser. For example, `/mnt/user/media-download-cache/cratekeep` can map to `/userMedia/inbox`, and `/mnt/user/media-fast/music` can map to `/userMedia/library`. The host paths never need to be entered in Cratekeep.

## Local development

Python 3.11 or newer is recommended.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
mkdir -p data/inbox data/library data/config
flask --app beets_mvp run --debug
```

Open <http://127.0.0.1:5000> and complete the first-run setup. The form suggests the local `data/inbox` and `data/library` directories.

For a production-like local process:

```sh
gunicorn --bind 127.0.0.1:8788 --workers 1 --threads 4 'beets_mvp:create_app()'
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `STATE_PATH` | `./data/config` | SQLite databases and generated beets config. |
| `SECRET_KEY` | generated in `$STATE_PATH/secret.key` | Optional override for advanced deployments. The generated key is retained across restarts when `$STATE_PATH` is mounted persistently. |

Inbox and library paths are chosen during first-run setup. The in-app Settings page also manages optional Navidrome details, an explicit artwork-fetching opt-in, and an advanced YAML editor for `$STATE_PATH/config.yaml`. On save, Cratekeep validates and normalizes the YAML; library/database paths, core import safety options, and `fetchart` plugin state remain controlled by the form.

The overview remains at `/`. The dedicated `/inbox` page opens the pending library-import review workspace, while `/library` provides collection counts, recently added tracks, top artists, searchable/sortable browsing, and the existing metadata edit forms. To safely correct a completed import, open its track in Library, change only **Artist**, leave **Album artist** unchanged, and save; the editor updates both the beets database and the existing file tag in place.

Artwork fetching is opt-in. When enabled, saving `cover.jpg` is on by default for backward compatibility; embedding is off. The two destinations are independent. Existing sidecars and embedded images are preserved unless **Replace existing sidecar and embedded artwork** is explicitly enabled. Cratekeep uses Cover Art Archive front art for the selected exact MusicBrainz release, falling back to release-group front art only when exact-release art is absent. Missing art is a visible no-op.

## API overview

Imports require a preview followed by an explicit execute call:

```sh
curl -sS -X POST http://localhost:8788/api/imports/preview \
  -H 'Content-Type: application/json' -d '{"path":"My Album"}'
curl -sS -X POST http://localhost:8788/api/imports/1/execute
```

Execution verifies that the previewed files have not changed, then runs `beet import --quiet --noautotag --move`.

- `GET /healthz` — liveness check
- `GET /api/browse?path=/userMedia` — list directories beneath the container's browse root
- `GET /api/inbox` — list immediate import candidates
- `GET /api/items` — list beets library metadata
- `GET /api/library-import/reviews` — list persisted track groups and album review records, paginated by album (`limit`/`offset`, 25 albums by default)
- `GET /api/library-import/folders` — list literal immediate-child library folders, 25 per page, with search and persisted-state status filtering
- `GET /api/library-import/folders/albums?folder=…` — lazily load reviewable albums for one validated folder scope
- `GET /api/library-import/albums/<id>` — fetch one current album-review payload before a sequential review transition
- `POST /api/library-import/folders/scan` — inventory and MusicBrainz-match only the submitted folder scopes; never imports or writes media
- `POST /api/library-import/bulk/preview` — summarize exact/current ready albums and excluded exceptions in selected folders
- `POST /api/library-import/bulk/execute` — after explicit `confirmed: true`, revalidate and import only still-ready albums through the existing per-album dry-run/execute boundary
- `POST /api/library-import/bulk/exceptions` — load uncertain selected-folder albums for sequential review
- `POST /api/library-import/candidates` — generate and persist bounded MusicBrainz release candidates
- `PATCH /api/library-import/albums/<id>` — save an album decision, selected candidate, track exceptions, and optional persisted `duplicate_action` (`merge`, `replace`, `keep-both`, or `skip`); send `candidate_id: null` with an approved decision to explicitly keep the incoming metadata “As Is” with no MusicBrainz association
- `POST /api/library-import/albums/<id>/rematch` — validate a MusicBrainz release UUID and rematch through the configured provider
- `POST /api/library-import/albums/<id>/preview` — save the match-screen selection and dry-run the in-place import without queueing it through Inbox
- `POST /api/library-import/albums/<id>/execute` — preview or execute an approved album in place; the match screen sends `candidate_id` and optional `duplicate_action` so the selection is saved before execution, while API clients may send `{"dry_run":true}` to validate without mutation
- `POST /api/imports/preview` — snapshot an inbox selection for review
- `POST /api/imports/<id>/execute` — execute a reviewed import
- `PATCH /api/items/<id>` — update `title`, `artist`, `album`, `albumartist`, `genre`, `year`, `track`, or `disc`
- `POST /api/navidrome/rescan` — request a scan from the configured endpoint

### Library import folder statuses

The Library import queue pages 25 literal top-level filesystem folders at a time. A separate `Library root` scope appears when supported audio files live directly at the root. Filesystem case, punctuation, and spelling are preserved; search, status filtering, direct page entry, and browser-session selection operate on folders rather than inferred metadata albums. Expanding one folder lazily loads only that folder's active album reviews. The default queue contains pending/reviewable albums only, so successful full-library imports disappear immediately while their SQLite and beets history remains intact; the API-only `status=imported` filter provides the compatibility archive view. **Scan selected** inventories and matches only selected scopes without changing media or the beets library, with progress and results shown on the selected folder rows. **Import ready** requires an explicit confirmation and server-side revalidation, then imports only exact, untied, complete, current, conflict-free albums; excluded albums remain available through sequential **Review exceptions**.

| Status | Meaning |
|---|---|
| `not scanned` | The literal folder has no current inventory snapshot. |
| `ready` | At least one pending album passes the strict bulk-readiness predicate. |
| `needs review` | Scanned albums exist but none is currently safe for bulk import. |
| `error` | Candidate lookup or an earlier execution failed for an album in the folder. |
| `imported` | Every known album in the folder completed successfully. |

Within an expanded folder, lifecycle status and match confidence are separate indicators. Match confidence pills use three bands: below 75% is red, 75–89% is amber, and 90% or higher is green; confidence alone never makes an album bulk-ready. Album review presents every MusicBrainz result as a compact candidate row with its numeric match percentage and reusable field-status icons. Candidate dates are presented only as four-digit release years. Expanding a row shows artist, album, genre evidence, release year, changed-track count, release type/media, release region, local-track online coverage, and online-track local coverage. MusicBrainz genre absence is explicit and preserves the current genre; Cratekeep proposes or writes genre only when a provider supplies a real value. For reviewed Library adoption only, an existing trailing parenthesized or bracketed album qualifier is preserved exactly when the canonical release title matches the local base title and a complete comma/semicolon-delimited MusicBrainz release-disambiguation phrase confirms the qualifier. The raw release note remains visible as evidence; Cratekeep never appends arbitrary notes or synthesizes a suffix for unqualified local metadata. This does not alter Inbox or beets CLI behavior. The changed-track evidence can expand again to identify affected local and proposed tracks and show available title, position, and material duration deltas while remaining collapsed by default. Label, barcode, and matching service are omitted. Persisted artwork is shown when available; Cover Art Archive 404/missing-front results are distinguished from retryable lookup failures, and neither makes otherwise usable MusicBrainz metadata fail. An explicit **As Is** row queues the album with its incoming metadata unchanged and no selected MusicBrainz release. The review response exposes `current_metadata` for that row and `selection_mode` (`suggested`, `candidate`, or `as-is`); MusicBrainz scoring and ranking remain unchanged. Candidate evidence is schema-versioned: after an upgrade, older rows remain visible with decisions intact and are marked stale for refresh. The direct per-album **Import** action remains available: it saves the selected candidate and duplicate policy, performs the server preflight, and executes in place. After success the modal remains open with its success message and the control becomes **Close**; a failure leaves **Import** available to retry.

Genre proposals use canonical MusicBrainz release-group genres only. Cratekeep chooses one genre when the top positive count has at least two votes and strictly exceeds the runner-up; tied, weak, missing, or non-canonical evidence preserves the existing genre. Existing and reliable proposed genres are presentation-equivalent only when whitespace-normalized Unicode casefolded text matches; punctuation, synonyms, taxonomies, and multiple genres are not mapped. Equivalent values preserve the existing spelling and cause no tag write. New or genuinely different values use Python's deterministic Unicode title casing after whitespace cleanup, so `alternative rock`, `post-punk`, and `r&b` become `Alternative Rock`, `Post-Punk`, and `R&B`. This intentionally small rule preserves punctuation but does not implement language-specific title conventions or acronym dictionaries. Provider source and positive counts remain in persisted candidate evidence for audit.

Library execution is review-gated and synchronous. Before changing anything, Cratekeep verifies each approved track against the size, modification time, device, and inode captured by the latest inventory preview. Missing or changed files are rejected with `409 inventory_stale`; run a fresh inventory preview and review again. Track exceptions are honored, so rejected or skipped tracks are not registered or tagged. **As Is** registers approved files in the beets database without rewriting tags. A selected candidate registers untracked files and writes the approved album/track metadata, including MusicBrainz per-track artist credits when available, to each file at its existing path. It never invokes `beet import`, moves, or copies a file. When an incoming track shares a selected MusicBrainz recording ID with a managed track, execution requires the persisted duplicate decision: **skip** leaves the incoming duplicate untouched and unregistered, **keep both** registers it separately, **replace** points the one unambiguous managed record at the incoming path, and **merge** updates the one unambiguous managed track's metadata while leaving both files in place. Merge and replace reject ambiguous identity or a stale/unsafe managed path. Execution and dry-run attempts are audited in `adoption_jobs`; album records retain execution status, errors, the last execution job, and completion time. After successful execution, every track in that album is removed from pending review responses without deleting inventory, review, album, or job history. A completed execution can be safely retried and returns its stored result without another write.

## Tests

```sh
pytest -q
python -m compileall -q beets_mvp tests
```

### Browser and container smoke validation

Install the development requirements and Chromium once, then run the smoke test against a fresh,
already-running candidate. The state and fixture paths must be private to the run:

```sh
python -m playwright install chromium
python tests/runtime_smoke.py --base-url http://127.0.0.1:8788 \
  --fixture-root /tmp/cratekeep-smoke/fixtures --state-path /tmp/cratekeep-smoke/state \
  --evidence-dir /tmp/cratekeep-smoke/evidence
```

The procedure uses valid generated WAV files. It labels and persists deterministic mocked
MusicBrainz genre evidence, routes a deterministic mocked CAA thumbnail, and injects deterministic
server-side JPEG bytes through a smoke-only app factory without external provider traffic. It verifies
artwork-visible/missing states, sidecar-only and embed-only writes, preservation, explicit replacement,
missing-art no-op, reliable/weak/tied/missing genre outcomes, direct single-request execution, an intentional visible failure
and retry, persistent success with Import-to-Close transition, database registration, exact artwork
bytes, and unchanged file paths. It also covers As-Is import, guarded dirty backdrop/Escape
closure, busy close lockout, focus restoration, empty and restored session storage, dark-mode
persistence, fresh-inventory candidate gating, unexpected browser console/network errors,
desktop/narrow changed-track screenshots (including a trailing featured-artist title change), and a SHA256 evidence manifest. Unit/API coverage also verifies both destinations together.

To validate an exact local image with isolated mounts, use:

```sh
tests/container_smoke.sh cratekeep:candidate /tmp/cratekeep-container-evidence
```

The CI build job runs this container procedure, uploads bounded smoke evidence, and passes the
already-tested image artifact to the main-push publish job. Pull requests never publish images.

## Limitations

- Cratekeep is designed for one trusted user and one process. It has no authentication, authorization, CSRF protection, job queue, or background workers.
- Imports are synchronous and may occupy the sole Gunicorn worker for up to one hour.
- Folder scan and bulk import return bounded summaries after their synchronous requests; there is no background progress stream. Selection supports the visible page and persists for the browser session; there is deliberately no server-side “select all matching” mode.
- MusicBrainz matching is an explicit, bounded metadata lookup. Each search hit is resolved to canonical release/release-group type and region, media, position, recording, title, duration, genre-vote evidence, and CAA artwork availability before a deterministic beets-inspired distance is calculated. The MusicBrainz search score remains retrieval metadata and never contributes to confidence. A supplied release ID selects that canonical candidate but never overrides evidence-based confidence or recommendation. Recommendations and per-field penalties are bounded and explainable, and track-count mismatches can never report 100%. Candidate lookup and review decisions do not mutate media; only the separate execution action for an approved review writes tags and optional artwork. Library execution never runs a beets import.
- Duplicate resolution is explicit and recording-ID based. Without a selected candidate recording ID Cratekeep cannot classify a track as a duplicate, and merge/replace reject multiple managed matches; Cratekeep surfaces ambiguity instead of approximating identity. There is no automatic duplicate policy, progress stream, undo, or delete endpoint.
- A metadata database update occurs before its file-tag write, so a failed tag write can leave them temporarily inconsistent. Keep backups and ensure library files are writable.
- Navidrome integration is a generic POST with optional bearer authentication and is not automatically run after imports.
- Artwork fetching is disabled by default and is enabled only through Settings. Fetches are bounded and use constructed Cover Art Archive identity URLs; arbitrary provider URLs are never fetched server-side.
- The Flask signing key is generated on first boot and stored as `$STATE_PATH/secret.key`; keep the config volume persistent. It is not displayed in the UI because Cratekeep does not yet have authentication.
