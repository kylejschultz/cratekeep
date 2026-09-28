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
- `GET /api/library-import/reviews` — list persisted track groups and album review records, paginated by artist (`limit`/`offset`, 25 artists by default)
- `POST /api/library-import/candidates` — generate and persist bounded MusicBrainz release candidates
- `PATCH /api/library-import/albums/<id>` — save an album decision, selected candidate, and track exceptions; send `candidate_id: null` with an approved decision to explicitly keep the incoming metadata “As Is” with no MusicBrainz association
- `POST /api/library-import/albums/<id>/rematch` — validate a MusicBrainz release UUID and rematch through the configured provider
- `POST /api/imports/preview` — snapshot an inbox selection for review
- `POST /api/imports/<id>/execute` — execute a reviewed import
- `PATCH /api/items/<id>` — update `title`, `artist`, `album`, `albumartist`, `genre`, `year`, `track`, or `disc`
- `POST /api/navidrome/rescan` — request a scan from the configured endpoint

### Library import inventory statuses

The Library import inventory mirrors the source filesystem as expandable, nested folders and preserves each complete relative folder and track path. Albums appear within their actual folders, with artist and album tags used only as supporting metadata; this also supports loose files, artist/album layouts, and deeper mixed layouts in the same library. Album review presents every MusicBrainz result as a compact, expandable candidate row with album-level metadata, media, track-coverage, and duration/track-delta highlights rather than a per-track rundown. Persisted artwork is shown when available. An explicit **As Is** row queues the album with its incoming metadata unchanged and no selected MusicBrainz release. The review response exposes `current_metadata` for that row and `selection_mode` (`suggested`, `candidate`, or `as-is`); MusicBrainz scoring and ranking remain unchanged. Inventory, matching, rematching, and review operations show visible progress while requests are active. The inventory reports existing workflow state; it does not trigger imports or add state transitions:

| Status | Meaning |
|---|---|
| `Matched` | A metadata candidate is available for review. |
| `Queued` | The selected metadata candidate is queued for a future import. |
| `Imported` | Every track in the album is already tracked in the library. |
| `Needs attention` | Matching is incomplete or failed, or the album was rejected or skipped during review. |

Lifecycle status and match confidence are separate indicators. Match confidence pills use three bands: below 75% is red, 75–89% is amber, and 90% or higher is green. The 90% band is only the initial indicator cutoff for a future auto-import workflow; Cratekeep does not automatically import albums. Queuing an album records the review decision; it does not execute an import.

## Tests

```sh
pytest -q
python -m compileall -q beets_mvp tests
```

## Limitations

- Cratekeep is designed for one trusted user and one process. It has no authentication, authorization, CSRF protection, job queue, or background workers.
- Imports are synchronous and may occupy the sole Gunicorn worker for up to one hour.
- MusicBrainz matching is an explicit, bounded metadata-only lookup. Each search hit is resolved to canonical release/release-group, media, position, recording, title, and duration data before a deterministic beets-inspired distance is calculated. The MusicBrainz search score remains retrieval metadata and never contributes to confidence. A supplied release ID selects that canonical candidate but never overrides evidence-based confidence or recommendation. Recommendations and per-field penalties are bounded and explainable, and track-count mismatches can never report 100%. Candidate and review decisions are persisted, but they do not write tags, move files, fetch artwork, or execute a beets import.
- There is no automatic duplicate resolution, artwork workflow, progress stream, undo, or delete endpoint.
- A metadata database update occurs before its file-tag write, so a failed tag write can leave them temporarily inconsistent. Keep backups and ensure library files are writable.
- Navidrome integration is a generic POST with optional bearer authentication and is not automatically run after imports.
- Artwork fetching is disabled by default and is enabled only through the Settings checkbox.
- The Flask signing key is generated on first boot and stored as `$STATE_PATH/secret.key`; keep the config volume persistent. It is not displayed in the UI because Cratekeep does not yet have authentication.
