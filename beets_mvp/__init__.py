from __future__ import annotations

import json
import hashlib
from io import BytesIO
import math
import os
import secrets
import sqlite3
import subprocess
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from beets import config as beets_config
from beets.library import Item, Library
from beetsplug.ftintitle import (
    DEFAULT_BRACKET_KEYWORDS,
    FtInTitlePlugin,
    contains_feat,
    find_feat_part,
    split_on_feat,
)
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from mediafile import Image as MediaImage, ImageType, MediaFile, UnreadableFileError
from PIL import Image as PillowImage, UnidentifiedImageError

from .musicbrainz import ProviderError, search_releases
from .matching import score_release

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".ape", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wv"}
EDITABLE_FIELDS = {"title", "artist", "album", "albumartist", "genre", "year", "track", "disc"}
SETTING_KEYS = (
    "inbox_path", "library_path", "navidrome_rescan_url", "navidrome_token", "fetch_art",
    "art_sidecar", "art_embed", "art_replace",
)
MAX_ARTWORK_BYTES = 10 * 1024 * 1024
TRUSTED_ARTWORK_HOSTS = {"coverartarchive.org", "archive.org"}
MAX_BEETS_CONFIG_BYTES = 128 * 1024
MAX_INVENTORY_SAMPLE = 50
MAX_LIBRARY_IMPORT_ALBUMS = 50
MAX_CANDIDATE_ALBUMS = 25
MAX_CANDIDATES_PER_ALBUM = 5
FOLDER_PAGE_SIZE = 25
CANDIDATE_SCHEMA_VERSION = 5
LIBRARY_IMPORT_DECISIONS = {"approved", "rejected", "skipped"}
DUPLICATE_ACTIONS = {"merge", "replace", "keep-both", "skip"}
MUSICBRAINZ_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
FTINTITLE_DEFAULTS = {
    "enabled": False,
    "format": "feat. {0}",
    "keep_in_artist": False,
    "preserve_album_artist": True,
    "drop": False,
    "auto": True,
    "custom_words": [],
    "bracket_keywords": list(DEFAULT_BRACKET_KEYWORDS),
    "projection_requires_recheck": False,
}


class LibraryImportExecutionError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 409, job_id: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.job_id = job_id


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.add_template_filter(_format_bytes, "format_bytes")
    app.config.from_mapping(
        BUILD_SHA=os.getenv("BUILD_SHA", os.getenv("GITHUB_SHA", "unknown")),
        SECRET_KEY=os.getenv("SECRET_KEY", ""),
        STATE_PATH=os.getenv("STATE_PATH", str(Path.cwd() / "data/config")),
        MUSICBRAINZ_PROVIDER=search_releases,
        ARTWORK_FETCHER=_fetch_and_normalize_artwork,
    )
    if test_config:
        app.config.update(test_config)

    state_path = Path(app.config["STATE_PATH"])
    state_path.mkdir(parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = _load_or_create_secret_key(state_path / "secret.key")
    app.config["BEETS_DB"] = str(state_path / "library.db")
    app.config["APP_DB"] = str(state_path / "app.db")
    app.config["BEETS_CONFIG"] = str(state_path / "config.yaml")
    _init_db(app.config["APP_DB"])
    _apply_settings(app, _load_settings(app.config["APP_DB"]))

    @app.before_request
    def require_setup():
        if app.config["SETUP_COMPLETE"] or request.endpoint in {"setup", "settings", "browse", "healthz", "static"}:
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="setup is required", setup=url_for("setup")), 503
        return redirect(url_for("setup"))

    @app.route("/setup", methods=("GET", "POST"))
    def setup():
        if app.config["SETUP_COMPLETE"]:
            return redirect(url_for("settings"))
        return _settings_response(app, first_run=True)

    @app.route("/settings", methods=("GET", "POST"))
    def settings():
        if not app.config["SETUP_COMPLETE"]:
            return redirect(url_for("setup"))
        return _settings_response(app, first_run=False)

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            build_sha=app.config["BUILD_SHA"],
            candidates=_inbox_candidates(app),
            items=_items(app),
        )

    @app.get("/inbox")
    def inbox_page():
        return _settings_response(app, first_run=False, active_tab="library-import", page="inbox")

    @app.get("/library")
    def library_page():
        items = _items(app)
        return render_template(
            "library.html",
            build_sha=app.config["BUILD_SHA"],
            **_library_page_data(items, request.args.get("q", ""), request.args.get("sort", "artist"),
                                 request.args.get("order", "asc"), request.args.get("artist", ""),
                                 request.args.get("album", "")),
        )

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/browse")
    def browse():
        requested = request.args.get("path", "")
        roots = _browse_roots(app)
        try:
            directory = _safe_browse_path(app, requested, roots)
        except ValueError as exc:
            abort(400, str(exc))
        entries = []
        for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            if entry.is_dir() and not entry.is_symlink():
                entries.append({"name": entry.name, "path": str(entry)})
        parent = _browse_parent(directory, roots)
        return jsonify(
            path=str(directory),
            parent=parent,
            entries=entries,
            roots=[{"name": root.name or str(root), "path": str(root)} for root in roots],
        )

    @app.get("/api/inbox")
    def inbox():
        return jsonify(_inbox_candidates(app))

    @app.get("/api/items")
    def items():
        return jsonify(_items(app))

    @app.post("/api/library/inventory/preview")
    def preview_library_inventory():
        try:
            summary = _preview_library_inventory(app)
            _sync_album_reviews(app)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(summary), 201

    @app.get("/api/library/inventory")
    def latest_library_inventory():
        summary = _latest_inventory_summary(app)
        if summary is None:
            return jsonify(error="no library inventory has been run"), 404
        return jsonify(summary)

    @app.get("/api/library-import/reviews")
    def library_import_reviews():
        try:
            limit = int(request.args.get("limit", "25"))
            offset = int(request.args.get("offset", "0"))
        except ValueError:
            return jsonify(error="limit and offset must be integers"), 400
        if not 1 <= limit <= MAX_LIBRARY_IMPORT_ALBUMS:
            return jsonify(error=f"limit must be between 1 and {MAX_LIBRARY_IMPORT_ALBUMS}"), 400
        if offset < 0:
            return jsonify(error="offset must be zero or greater"), 400
        album_ids, has_more, total_albums = _library_import_review_album_page(app, limit, offset)
        items = _library_import_review_items(app, album_ids)
        return jsonify(items=items, groups=_group_library_import_review_items(items),
                       albums=_album_review_payloads(app, album_ids), limit=limit, offset=offset,
                       album_count=len(album_ids), total_albums=total_albums, has_more=has_more,
                       next_offset=(offset + len(album_ids)) if has_more else None)

    @app.get("/api/library-import/folders")
    def library_import_folders():
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            return jsonify(error="page must be an integer"), 400
        if page < 1:
            return jsonify(error="page must be one or greater"), 400
        status = request.args.get("status", "all").strip().lower()
        query = request.args.get("q", "").strip()
        try:
            result = _folder_queue_page(app, page=page, query=query, status=status)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(result)

    @app.get("/api/library-import/folders/albums")
    def library_import_folder_albums():
        try:
            scope = _validated_folder_scope(app, request.args.get("folder", ""))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        album_ids = _album_ids_for_scopes(app, [scope], include_complete=False)
        items = _library_import_review_items(app, album_ids)
        albums = _album_review_payloads(app, album_ids)
        return jsonify(folder=scope, items=items, albums=[_with_readiness(album) for album in albums])

    @app.post("/api/library-import/folders/scan")
    def scan_library_import_folders():
        try:
            scopes = _request_folder_scopes(app)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        result = _scan_folder_scopes(app, scopes)
        return jsonify(result), (207 if result["errors"] else 201)

    @app.post("/api/library-import/bulk/preview")
    def preview_ready_library_imports():
        try:
            scopes = _request_folder_scopes(app)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(_bulk_ready_summary(app, scopes))

    @app.post("/api/library-import/bulk/exceptions")
    def selected_library_import_exceptions():
        try:
            scopes = _request_folder_scopes(app)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        album_ids = _album_ids_for_scopes(app, scopes, include_complete=False)
        albums = [_with_readiness(album) for album in _album_review_payloads(app, album_ids)]
        return jsonify(albums=[album for album in albums if not album["ready"]])

    @app.post("/api/library-import/bulk/execute")
    def execute_ready_library_imports():
        values = request.get_json(silent=True)
        if not isinstance(values, dict) or values.get("confirmed") is not True:
            return jsonify(error="explicit confirmation is required", code="confirmation_required"), 400
        try:
            scopes = _request_folder_scopes(app, values)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        result = _execute_bulk_ready(app, scopes)
        return jsonify(result), (207 if result["failed"] else 201)

    @app.post("/api/library-import/candidates")
    def generate_library_import_candidates():
        _sync_album_reviews(app)
        try:
            limit = int((request.get_json(silent=True) or {}).get("limit", MAX_CANDIDATE_ALBUMS))
        except (TypeError, ValueError):
            return jsonify(error="limit must be an integer"), 400
        if not 1 <= limit <= MAX_CANDIDATE_ALBUMS:
            return jsonify(error=f"limit must be between 1 and {MAX_CANDIDATE_ALBUMS}"), 400
        result = _generate_musicbrainz_candidates(app, limit)
        return jsonify(result), (207 if result["errors"] else 201)

    @app.patch("/api/library-import/albums/<int:album_review_id>")
    def update_library_import_album_review(album_review_id: int):
        values = request.get_json(silent=True) or {}
        try:
            item = _update_album_review(app, album_review_id, values)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        if item is None:
            abort(404)
        return jsonify(item)

    @app.get("/api/library-import/albums/<int:album_review_id>")
    def library_import_album_review(album_review_id: int):
        item = next(iter(_album_review_payloads(app, [album_review_id])), None)
        if item is None:
            return jsonify(error="Album review not found", code="album_review_not_found"), 404
        return jsonify(_with_readiness(item))

    @app.post("/api/library-import/albums/<int:album_review_id>/rematch")
    def rematch_library_import_album(album_review_id: int):
        values = request.get_json(silent=True)
        if not isinstance(values, dict) or set(values) != {"musicbrainz_id"}:
            return jsonify(error="request must contain only musicbrainz_id", code="invalid_request"), 400
        musicbrainz_id = values.get("musicbrainz_id")
        if not isinstance(musicbrainz_id, str) or not MUSICBRAINZ_ID_PATTERN.fullmatch(musicbrainz_id):
            return jsonify(
                error="Enter a canonical MusicBrainz release UUID (for example, 123e4567-e89b-42d3-a456-426614174000).",
                code="invalid_musicbrainz_id",
            ), 400
        try:
            item = _rematch_album_by_musicbrainz_id(app, album_review_id, musicbrainz_id.lower())
        except ProviderError as exc:
            status = 404 if exc.code == "release_not_found" else (503 if exc.retryable else 502)
            payload = {"error": str(exc), "code": exc.code, "retryable": exc.retryable}
            if exc.upstream_status is not None:
                payload["upstream_status"] = exc.upstream_status
            return jsonify(payload), status
        except (OSError, ValueError, TypeError) as exc:
            return jsonify(error=str(exc) or "candidate provider failed", code="provider_error", retryable=False), 502
        if item is None:
            return jsonify(error="Album review not found", code="album_review_not_found"), 404
        return jsonify(item)

    @app.post("/api/library-import/albums/<int:album_review_id>/execute")
    def execute_library_import_album(album_review_id: int):
        values = request.get_json(silent=True)
        if values is None:
            values = {}
        if (not isinstance(values, dict) or set(values) - {"dry_run", "candidate_id", "duplicate_action"}
                or not isinstance(values.get("dry_run", False), bool)):
            return jsonify(
                error="request may contain only dry_run, candidate_id, and duplicate_action",
                code="invalid_request",
            ), 400
        if "duplicate_action" in values and "candidate_id" not in values:
            return jsonify(error="duplicate_action requires candidate_id", code="invalid_request"), 400
        if "candidate_id" in values:
            root = str(Path(app.config["LIBRARY_PATH"]).resolve())
            with _connect(app.config["APP_DB"]) as db:
                existing = db.execute(
                    "SELECT execution_status FROM album_reviews WHERE id = ? AND root_path = ?",
                    (album_review_id, root),
                ).fetchone()
            if existing is None:
                return jsonify(error="Album review not found", code="album_review_not_found"), 404
            # Preserve execution idempotency if a client retries after losing the
            # successful response; selection persistence must not reset completion.
            if existing["execution_status"] == "complete":
                result = _execute_library_import_album(app, album_review_id, dry_run=values.get("dry_run", False))
                return jsonify(result), 200
            selection = {"decision": "approved", "candidate_id": values["candidate_id"]}
            if "duplicate_action" in values:
                selection["duplicate_action"] = values["duplicate_action"]
            try:
                item = _update_album_review(app, album_review_id, selection)
            except ValueError as exc:
                return jsonify(error=str(exc), code="invalid_request"), 400
            if item is None:
                return jsonify(error="Album review not found", code="album_review_not_found"), 404
        try:
            result = _execute_library_import_album(app, album_review_id, dry_run=values.get("dry_run", False))
        except LibraryImportExecutionError as exc:
            return jsonify(error=str(exc), code=exc.code, job_id=exc.job_id), exc.status
        if result is None:
            return jsonify(error="Album review not found", code="album_review_not_found"), 404
        return jsonify(result), (201 if result["status"] == "complete" and not result.get("already_complete") else 200)

    @app.post("/api/library-import/albums/<int:album_review_id>/preview")
    def preview_library_import_album(album_review_id: int):
        """Save a match-screen selection and preview the existing in-place execution."""
        values = request.get_json(silent=True)
        if (not isinstance(values, dict) or "candidate_id" not in values
                or set(values) - {"candidate_id", "duplicate_action"}):
            return jsonify(
                error="request must contain candidate_id and may contain duplicate_action",
                code="invalid_request",
            ), 400
        root = str(Path(app.config["LIBRARY_PATH"]).resolve())
        with _connect(app.config["APP_DB"]) as db:
            existing = db.execute(
                "SELECT execution_status FROM album_reviews WHERE id = ? AND root_path = ?",
                (album_review_id, root),
            ).fetchone()
        if existing is None:
            return jsonify(error="Album review not found", code="album_review_not_found"), 404
        if existing["execution_status"] == "complete":
            result = _execute_library_import_album(app, album_review_id, dry_run=False)
            return jsonify(result)
        selection = {"decision": "approved", "candidate_id": values["candidate_id"]}
        if "duplicate_action" in values:
            selection["duplicate_action"] = values["duplicate_action"]
        try:
            item = _update_album_review(app, album_review_id, selection)
        except ValueError as exc:
            return jsonify(error=str(exc), code="invalid_request"), 400
        if item is None:  # The row can only disappear if storage changed between the two transactions.
            return jsonify(error="Album review not found", code="album_review_not_found"), 404
        try:
            result = _execute_library_import_album(app, album_review_id, dry_run=True)
        except LibraryImportExecutionError as exc:
            return jsonify(error=str(exc), code=exc.code, job_id=exc.job_id), exc.status
        return jsonify(result)

    @app.patch("/api/library-import/reviews/<int:inventory_id>")
    def update_library_import_review(inventory_id: int):
        values = request.get_json(silent=True) or {}
        decision = values.get("decision")
        if decision not in LIBRARY_IMPORT_DECISIONS:
            return jsonify(error="decision must be approved, rejected, or skipped"), 400
        root = str(Path(app.config["LIBRARY_PATH"]).resolve())
        with _connect(app.config["APP_DB"]) as db:
            row = db.execute(
                """SELECT reviews.inventory_id
                     FROM adoption_reviews AS reviews
                     JOIN library_inventory AS inventory ON inventory.id = reviews.inventory_id
                    WHERE reviews.inventory_id = ? AND inventory.root_path = ? AND inventory.present = 1""",
                (inventory_id, root),
            ).fetchone()
            if row is None:
                abort(404)
            db.execute(
                "UPDATE adoption_reviews SET state = ?, updated_at = ? WHERE inventory_id = ?",
                (decision, _now(), inventory_id),
            )
        return jsonify(_library_import_review_item(app, inventory_id))

    @app.post("/api/imports/preview")
    def preview_import():
        relative = _request_value("path")
        source = _safe_inbox_path(app, relative)
        files = _audio_files(source)
        if not files:
            abort(400, "selection contains no supported audio files")
        file_names = [str(p.relative_to(Path(app.config["INBOX_PATH"]).resolve())) for p in files]
        with _connect(app.config["APP_DB"]) as db:
            cursor = db.execute(
                "INSERT INTO import_jobs(source, files_json, status, created_at) VALUES (?, ?, 'review', ?)",
                (relative, json.dumps(file_names), _now()),
            )
            job_id = cursor.lastrowid
        return jsonify(id=job_id, source=relative, files=file_names, status="review"), 201

    @app.post("/api/imports/<int:job_id>/execute")
    def execute_import(job_id: int):
        with _connect(app.config["APP_DB"]) as db:
            job = db.execute("SELECT * FROM import_jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                abort(404)
            if job["status"] != "review":
                abort(409, "job has already been executed")
            source = _safe_inbox_path(app, job["source"])
            expected = json.loads(job["files_json"])
            root = Path(app.config["INBOX_PATH"]).resolve()
            current = [str(p.relative_to(root)) for p in _audio_files(source)]
            if current != expected:
                abort(409, "inbox contents changed; create a new preview")
            command = ["beet", "-c", app.config["BEETS_CONFIG"], "import", "--quiet", "--noautotag", "--move", str(source)]
            command_env = os.environ.copy()
            command_env["BEETSDIR"] = app.config["STATE_PATH"]
            result = subprocess.run(command, text=True, capture_output=True, timeout=3600, check=False, env=command_env)
            output = (result.stdout + result.stderr)[-12000:]
            status = "complete" if result.returncode == 0 else "failed"
            db.execute(
                "UPDATE import_jobs SET status = ?, output = ?, finished_at = ? WHERE id = ?",
                (status, output, _now(), job_id),
            )
        return jsonify(id=job_id, status=status, returncode=result.returncode, output=output), (200 if result.returncode == 0 else 502)

    @app.patch("/api/items/<int:item_id>")
    @app.post("/api/items/<int:item_id>")
    def edit_item(item_id: int):
        values = request.get_json(silent=True) or request.form.to_dict()
        unknown = set(values) - EDITABLE_FIELDS
        if unknown:
            abort(400, f"unsupported fields: {', '.join(sorted(unknown))}")
        library = _library(app)
        item = library.get_item(item_id)
        if item is None:
            abort(404)
        for field, value in values.items():
            if field in {"year", "track", "disc"}:
                try:
                    value = int(value or 0)
                except (TypeError, ValueError):
                    abort(400, f"{field} must be an integer")
            _set_item_value(item, field, value)
        item.store()
        try:
            item.write()
        except Exception as exc:
            abort(500, f"database updated but tag write failed: {exc}")
        if request.is_json:
            return jsonify(_serialize_item(item))
        flash("Metadata and file tags updated.")
        return redirect(url_for("library_page"))

    @app.post("/api/navidrome/rescan")
    def navidrome_rescan():
        target = app.config["NAVIDROME_RESCAN_URL"]
        if not target:
            abort(503, "NAVIDROME_RESCAN_URL is not configured")
        headers = {"Accept": "application/json"}
        if app.config["NAVIDROME_TOKEN"]:
            headers["Authorization"] = f"Bearer {app.config['NAVIDROME_TOKEN']}"
        upstream = urllib.request.Request(target, data=b"", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(upstream, timeout=15) as response:
                body = response.read(4096).decode("utf-8", "replace")
                return jsonify(status="requested", upstream_status=response.status, response=body)
        except urllib.error.URLError as exc:
            return jsonify(status="failed", error=str(exc)), 502

    return app


def _load_or_create_secret_key(path: Path) -> str:
    try:
        key = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        key = ""
    if key:
        return key
    key = secrets.token_urlsafe(48)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(key + "\n")
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


def _write_beets_config(app: Flask, content: str | None = None) -> None:
    path = Path(app.config["BEETS_CONFIG"])
    if path.exists() and content is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        content = _build_beets_config(app, {}, "", False)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _beets_config_document(content: str) -> dict:
    if len(content.encode("utf-8")) > MAX_BEETS_CONFIG_BYTES:
        raise ValueError("Beets configuration must be smaller than 128 KB.")
    try:
        document = yaml.safe_load(content) if content.strip() else {}
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0]
        raise ValueError(f"Beets configuration is not valid YAML: {detail}") from exc
    if not isinstance(document, dict):
        raise ValueError("Beets configuration must be a YAML mapping.")
    return document


def _plugin_names(document: dict) -> list[str]:
    plugins = document.get("plugins", [])
    if isinstance(plugins, str):
        plugins = plugins.split()
    if plugins is None:
        plugins = []
    if not isinstance(plugins, list) or any(not isinstance(plugin, str) for plugin in plugins):
        raise ValueError("The beets plugins setting must be a list or space-separated string.")
    return list(dict.fromkeys(plugins))


def _ftintitle_settings(content: str) -> dict:
    values = dict(FTINTITLE_DEFAULTS)
    try:
        document = _beets_config_document(content)
        plugins = _plugin_names(document)
    except ValueError:
        return values
    values["enabled"] = "ftintitle" in plugins
    values["projection_requires_recheck"] = values["enabled"] or "ftintitle" in document
    try:
        section = _validated_ftintitle_section(document)
        if isinstance(section.get("format"), str):
            values["format"] = section["format"]
        for key in ("keep_in_artist", "preserve_album_artist", "drop", "auto"):
            if isinstance(section.get(key), bool):
                values[key] = section[key]
        values["custom_words"] = _validated_string_list(section, "custom_words", [])
        values["bracket_keywords"] = _validated_string_list(
            section, "bracket_keywords", list(DEFAULT_BRACKET_KEYWORDS)
        )
    except ValueError:
        # Keep plugin activation truthful so the first-class controls can fix
        # a malformed advanced section on the next save.
        return values
    return values


def _validated_ftintitle_section(document: dict) -> dict:
    section = document.get("ftintitle")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("The ftintitle section must be a YAML mapping.")
    _validated_string_list(section, "custom_words", [])
    _validated_string_list(section, "bracket_keywords", list(DEFAULT_BRACKET_KEYWORDS))
    return section


def _validated_string_list(section: dict, key: str, default: list[str]) -> list[str]:
    value = section.get(key, default)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"The ftintitle {key} setting must be a list of non-empty strings.")
    return [item.strip() for item in value]


def _validate_ftintitle_settings(values: dict) -> None:
    if not values.get("enabled"):
        return
    feat_format = values.get("format")
    if not isinstance(feat_format, str) or not feat_format.strip():
        raise ValueError("FtInTitle format must include a valid guest placeholder.")
    guest = "CratekeepFeaturedArtist"
    try:
        rendered = feat_format.format(guest)
    except (AttributeError, IndexError, KeyError, ValueError) as exc:
        raise ValueError("FtInTitle format must format one guest using {} or {0}.") from exc
    if guest not in rendered:
        raise ValueError("FtInTitle format must include the guest using {} or {0}.")


def _build_beets_config(
    app: Flask, settings: dict[str, str], content: str, fetch_art: bool, ftintitle: dict | None = None
) -> str:
    document = _beets_config_document(content)

    import_config = document.get("import", {})
    if import_config is None:
        import_config = {}
    if not isinstance(import_config, dict):
        raise ValueError("The beets import section must be a YAML mapping.")
    import_config.update(move=True, write=True, autotag=False, resume=False)

    plugins = [plugin for plugin in _plugin_names(document) if plugin != "fetchart"]
    section = _validated_ftintitle_section(document)
    if ftintitle is not None:
        _validate_ftintitle_settings(ftintitle)
        plugins = [plugin for plugin in plugins if plugin != "ftintitle"]
        if ftintitle.get("enabled"):
            plugins.append("ftintitle")
            section.update(
                auto=True,
                drop=False,
                format=ftintitle["format"].strip(),
                keep_in_artist=bool(ftintitle.get("keep_in_artist")),
                preserve_album_artist=True,
            )
            document["ftintitle"] = section
    if fetch_art:
        plugins.append("fetchart")

    library_path = settings.get("library_path") or app.config.get("LIBRARY_PATH", "")
    document["directory"] = str(Path(library_path).expanduser().resolve())
    document["library"] = str(Path(app.config["BEETS_DB"]).resolve())
    document["import"] = import_config
    document["plugins"] = plugins
    rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    lines = rendered.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("directory:"):
            lines[index] = f"directory: {json.dumps(document['directory'])}"
        elif line.startswith("library:"):
            lines[index] = f"library: {json.dumps(document['library'])}"
    return "\n".join(lines) + "\n"


def _init_db(path: str) -> None:
    with _connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS import_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            files_json TEXT NOT NULL,
            status TEXT NOT NULL,
            output TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS adoption_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            root_path TEXT NOT NULL,
            status TEXT NOT NULL,
            summary_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            finished_at TEXT
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS library_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            relative_path TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            beets_item_id INTEGER,
            present INTEGER NOT NULL DEFAULT 1,
            first_seen_job_id INTEGER NOT NULL REFERENCES adoption_jobs(id),
            last_seen_job_id INTEGER NOT NULL REFERENCES adoption_jobs(id),
            UNIQUE(root_path, relative_path)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS adoption_reviews (
            inventory_id INTEGER PRIMARY KEY REFERENCES library_inventory(id),
            state TEXT NOT NULL DEFAULT 'pending',
            candidates_json TEXT NOT NULL DEFAULT '[]',
            note TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS album_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            root_path TEXT NOT NULL,
            artist_key TEXT NOT NULL,
            album_key TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            selected_candidate_id INTEGER,
            candidate_status TEXT NOT NULL DEFAULT 'not-run',
            candidate_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            UNIQUE(root_path, artist_key, album_key)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS album_review_tracks (
            album_review_id INTEGER NOT NULL REFERENCES album_reviews(id) ON DELETE CASCADE,
            inventory_id INTEGER NOT NULL REFERENCES library_inventory(id) ON DELETE CASCADE,
            exception_state TEXT,
            PRIMARY KEY(album_review_id, inventory_id),
            UNIQUE(inventory_id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS metadata_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_review_id INTEGER NOT NULL REFERENCES album_reviews(id) ON DELETE CASCADE,
            provider TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            rank INTEGER NOT NULL,
            confidence REAL NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            year TEXT,
            proposed_diff_json TEXT NOT NULL,
            provider_data_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            UNIQUE(album_review_id, provider, provider_id)
        )""")
        _ensure_column(db, "album_reviews", "execution_status", "TEXT NOT NULL DEFAULT 'not-run'")
        _ensure_column(db, "album_reviews", "execution_error", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(db, "album_reviews", "last_execution_job_id", "INTEGER")
        _ensure_column(db, "album_reviews", "executed_at", "TEXT")
        _ensure_column(db, "album_reviews", "duplicate_action", "TEXT")
        _ensure_column(db, "metadata_candidates", "schema_version", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(db, "adoption_reviews", "imported_at", "TEXT")
        # Older candidate evidence remains visible for audit and keeps all
        # review decisions, but is queued ahead of complete rows for a bounded
        # refresh after evidence or presentation rules change.
        db.execute(
            """UPDATE album_reviews
                  SET candidate_status = 'stale',
                      candidate_error = 'Candidate evidence predates the current schema; refresh candidates.'
                WHERE execution_status != 'complete'
                  AND candidate_status = 'complete'
                  AND EXISTS (
                      SELECT 1 FROM metadata_candidates
                       WHERE album_review_id = album_reviews.id AND schema_version < ?
                  )""",
            (CANDIDATE_SCHEMA_VERSION,),
        )
        db.execute(
            """UPDATE adoption_reviews
                  SET imported_at = COALESCE((
                        SELECT albums.executed_at
                          FROM album_review_tracks AS tracks
                          JOIN album_reviews AS albums ON albums.id = tracks.album_review_id
                         WHERE tracks.inventory_id = adoption_reviews.inventory_id
                           AND albums.execution_status = 'complete'
                         LIMIT 1
                      ), updated_at)
                WHERE imported_at IS NULL
                  AND EXISTS (
                        SELECT 1
                          FROM album_review_tracks AS tracks
                          JOIN album_reviews AS albums ON albums.id = tracks.album_review_id
                         WHERE tracks.inventory_id = adoption_reviews.inventory_id
                           AND albums.execution_status = 'complete'
                      )"""
        )


def _ensure_column(db: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _load_settings(path: str) -> dict[str, str]:
    with _connect(path) as db:
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
    settings = {row["key"]: row["value"] for row in rows if row["key"] in SETTING_KEYS}
    # Existing installations that opted into fetching retain beets' historical
    # cover.jpg behavior. Embedding and replacement always require a new opt-in.
    if settings.get("fetch_art") == "1" and "art_sidecar" not in settings:
        settings["art_sidecar"] = "1"
    return settings


def _save_settings(path: str, settings: dict[str, str]) -> None:
    with _connect(path) as db:
        db.executemany(
            "INSERT INTO app_settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(key, settings.get(key, "")) for key in SETTING_KEYS],
        )


def _apply_settings(app: Flask, settings: dict[str, str]) -> None:
    app.config.update(
        INBOX_PATH=settings.get("inbox_path", ""),
        LIBRARY_PATH=settings.get("library_path", ""),
        NAVIDROME_RESCAN_URL=settings.get("navidrome_rescan_url", ""),
        NAVIDROME_TOKEN=settings.get("navidrome_token", ""),
        FETCH_ART=settings.get("fetch_art", "") == "1",
        ART_SIDECAR=settings.get("art_sidecar", "") == "1",
        ART_EMBED=settings.get("art_embed", "") == "1",
        ART_REPLACE=settings.get("art_replace", "") == "1",
    )
    app.config["SETUP_COMPLETE"] = bool(app.config["INBOX_PATH"] and app.config["LIBRARY_PATH"])
    if app.config["SETUP_COMPLETE"]:
        for key in ("INBOX_PATH", "LIBRARY_PATH"):
            Path(app.config[key]).mkdir(parents=True, exist_ok=True)
        _write_beets_config(app)
        app.config["FTINTITLE"] = _ftintitle_settings(
            Path(app.config["BEETS_CONFIG"]).read_text(encoding="utf-8")
        )
        beets_config.set_file(app.config["BEETS_CONFIG"])
        beets_config.read(user=False, defaults=True)


def _settings_response(app: Flask, first_run: bool, *, active_tab: str = "general", page: str = "settings"):
    current = _load_settings(app.config["APP_DB"])
    config_path = Path(app.config["BEETS_CONFIG"])
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    ftintitle = _ftintitle_settings(config_text)
    if request.method == "POST":
        inbox_path = request.form.get("inbox_path", "").strip()
        library_path = request.form.get("library_path", "").strip()
        rescan_url = request.form.get("navidrome_rescan_url", "").strip()
        errors = []
        if not inbox_path:
            errors.append("Inbox path is required.")
        if not library_path:
            errors.append("Library path is required.")
        for label, value in (("Inbox", inbox_path), ("Library", library_path)):
            if value:
                candidate = Path(value).expanduser().resolve()
                if not any(candidate == root or root in candidate.parents for root in _browse_roots(app)):
                    errors.append(f"{label} path must be inside a mounted directory shown by Browse.")
        parsed_rescan_url = urllib.parse.urlparse(rescan_url)
        if rescan_url and (parsed_rescan_url.scheme not in {"http", "https"} or not parsed_rescan_url.netloc):
            errors.append("Navidrome rescan URL must be a complete http or https URL.")

        token = request.form.get("navidrome_token", "")
        if not token and current.get("navidrome_token") and not request.form.get("clear_navidrome_token"):
            token = current["navidrome_token"]
        values = {
            "inbox_path": inbox_path,
            "library_path": library_path,
            "navidrome_rescan_url": rescan_url,
            "navidrome_token": token,
            "fetch_art": "1" if request.form.get("fetch_art") else "",
            "art_sidecar": "1" if request.form.get("art_sidecar") else "",
            "art_embed": "1" if request.form.get("art_embed") else "",
            "art_replace": "1" if request.form.get("art_replace") else "",
        }
        submitted_config = request.form.get("beets_config", config_text)
        submitted_ftintitle = {
            "enabled": bool(request.form.get("ftintitle_enabled")),
            "format": request.form.get("ftintitle_format", ftintitle["format"]),
            "keep_in_artist": bool(request.form.get("ftintitle_keep_in_artist")),
            # Cratekeep's reviewed import always preserves the release artist.
            "preserve_album_artist": True,
        }
        rendered_config = None
        if not first_run:
            try:
                rendered_config = _build_beets_config(
                    app, values, submitted_config, values["fetch_art"] == "1", submitted_ftintitle
                )
            except ValueError as exc:
                errors.append(str(exc))
        if not errors:
            try:
                Path(inbox_path).expanduser().mkdir(parents=True, exist_ok=True)
                Path(library_path).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"Could not create a configured directory: {exc}")
        if not errors:
            values["inbox_path"] = str(Path(inbox_path).expanduser().resolve())
            values["library_path"] = str(Path(library_path).expanduser().resolve())
            if first_run:
                rendered_config = _build_beets_config(app, values, "", values["fetch_art"] == "1")
            _write_beets_config(app, rendered_config)
            _save_settings(app.config["APP_DB"], values)
            _apply_settings(app, values)
            flash("Settings saved.")
            return redirect(url_for("index"))
        for error in errors:
            flash(error, "error")
        current = values
        config_text = submitted_config
        ftintitle = submitted_ftintitle

    defaults = _default_paths()
    return render_template(
        "settings.html",
        first_run=first_run,
        settings=current,
        default_inbox=defaults["inbox_path"],
        default_library=defaults["library_path"],
        has_token=bool(current.get("navidrome_token")),
        beets_config=config_text,
        ftintitle=ftintitle,
        build_sha=app.config["BUILD_SHA"],
        active_tab=active_tab,
        page=page,
    )


def _default_paths() -> dict[str, str]:
    container_data = Path("/data")
    root = container_data if (container_data / "inbox").is_dir() and (container_data / "library").is_dir() else Path.cwd() / "data"
    return {"inbox_path": str(root / "inbox"), "library_path": str(root / "library")}


def _browse_roots(app: Flask) -> list[Path]:
    configured = app.config.get("BROWSE_ROOTS")
    # A container mount destination is user-defined, so the default browser
    # must be able to reach arbitrary mount names such as /userMedia.
    # Tests and hardened deployments can still provide explicit roots.
    candidates = configured or ("/",)
    roots = []
    for candidate in candidates:
        path = Path(candidate).expanduser().resolve()
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _safe_browse_path(app: Flask, requested: str, roots: list[Path] | None = None) -> Path:
    roots = roots if roots is not None else _browse_roots(app)
    if not requested:
        if not roots:
            raise ValueError("no browseable mounted directories are available")
        return roots[0]
    path = Path(requested).expanduser().resolve()
    if not path.is_dir() or not any(path == root or root in path.parents for root in roots):
        raise ValueError("path is not inside a browseable mounted directory")
    return path


def _browse_parent(directory: Path, roots: list[Path]) -> str | None:
    if directory in roots:
        return None
    parent = directory.parent
    return str(parent) if any(parent == root or root in parent.parents for root in roots) else None


def _connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def _library(app: Flask) -> Library:
    return Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])


def _items(app: Flask) -> list[dict]:
    return [_serialize_item(item) for item in _library(app).items()]


def _library_page_data(
    items: list[dict], query: str, sort: str, order: str, artist_filter: str = "", album_filter: str = ""
) -> dict:
    """Build the small, read-only browse model used by the Library landing page."""
    query = _normalize_metadata(query)
    artist_filter = _normalize_metadata(artist_filter)
    album_filter = _normalize_metadata(album_filter)
    sort = sort if sort in {"artist", "album", "title"} else "artist"
    order = order if order in {"asc", "desc"} else "asc"
    searchable = ("artist", "album", "title")
    filtered = [
        item for item in items
        if (not query or query.casefold() in " ".join(str(item.get(field) or "") for field in searchable).casefold())
        and (not artist_filter or _metadata_key(item.get("albumartist") or item.get("artist")) == _metadata_key(artist_filter))
        and (not album_filter or _metadata_key(item.get("album")) == _metadata_key(album_filter))
    ]
    tie_breakers = tuple(field for field in searchable if field != sort) + ("id",)

    def item_key(item: dict) -> tuple:
        return tuple(str(item.get(field) or "").casefold() for field in (sort, *tie_breakers))

    filtered.sort(key=item_key, reverse=order == "desc")
    album_keys = {
        (_metadata_key(item.get("albumartist") or item.get("artist")), _metadata_key(item.get("album")))
        for item in items
    }
    artist_counts: dict[str, int] = {}
    for item in items:
        artist = _normalize_metadata(item.get("albumartist") or item.get("artist")) or "Unknown artist"
        artist_counts[artist] = artist_counts.get(artist, 0) + 1
    top_artists = sorted(artist_counts.items(), key=lambda pair: (-pair[1], pair[0].casefold()))[:5]
    recent = sorted(items, key=lambda item: int(item.get("id") or 0), reverse=True)[:5]
    return {
        "items": filtered,
        "recent_items": recent,
        "top_artists": [{"name": name, "tracks": count} for name, count in top_artists],
        "summary": {
            "tracks": len(items), "albums": len(album_keys), "artists": len(artist_counts),
            "bytes": sum(item.get("bytes") or 0 for item in items),
        },
        "query": query,
        "sort": sort,
        "order": order,
        "artist_filter": artist_filter,
        "album_filter": album_filter,
    }


FOLDER_STATUSES = {"all", "not-scanned", "ready", "needs-review", "error", "imported"}


def _validated_folder_scope(app: Flask, value: object) -> str:
    """Return a literal immediate-child scope without permitting traversal."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("folder scope is required")
    scope = value.strip()
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    if scope == ".":
        return scope
    candidate = Path(scope)
    if candidate.is_absolute() or len(candidate.parts) != 1 or candidate.name in {"", ".", ".."}:
        raise ValueError("folder scope must be an immediate child of the library root")
    path = root / candidate.name
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("folder scope does not exist") from exc
    if resolved.parent != root or path.is_symlink() or not resolved.is_dir():
        raise ValueError("folder scope must resolve to a non-symlinked folder beneath the library root")
    return candidate.name


def _request_folder_scopes(app: Flask, values: dict | None = None) -> list[str]:
    values = values if values is not None else request.get_json(silent=True)
    if not isinstance(values, dict) or not isinstance(values.get("folders"), list):
        raise ValueError("folders must be an array of folder scopes")
    if not values["folders"]:
        raise ValueError("select at least one folder")
    if len(values["folders"]) > 1000:
        raise ValueError("no more than 1000 folder scopes may be submitted at once")
    scopes = []
    for value in values["folders"]:
        scope = _validated_folder_scope(app, value)
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def _scope_for_relative_path(relative_path: str) -> str:
    parts = Path(relative_path).parts
    return parts[0] if len(parts) > 1 else "."


def _relative_in_scope(relative_path: str, scope: str) -> bool:
    return _scope_for_relative_path(relative_path) == scope


def _literal_folder_scopes(root: Path) -> list[str]:
    if not root.is_dir():
        raise ValueError("configured library path is not a directory")
    scopes = sorted(
        (entry.name for entry in root.iterdir() if entry.is_dir() and not entry.is_symlink()),
        key=lambda value: (value.casefold(), value),
    )
    if any(entry.is_file() and not entry.is_symlink() and entry.suffix.lower() in AUDIO_EXTENSIONS
           for entry in root.iterdir()):
        scopes.insert(0, ".")
    return scopes


def _album_bulk_readiness(album: dict) -> tuple[bool, str]:
    """Central exact/current/safe predicate used by queue display and execution."""
    if album.get("execution_status") == "complete":
        return False, "already imported"
    if album.get("execution_status") == "failed" or album.get("candidate_status") == "error":
        return False, "album has an error"
    if album.get("candidate_status") != "complete" or album.get("candidate_error"):
        return False, "candidate evidence is not current"
    if album.get("decision") in {"rejected", "skipped"}:
        return False, "album was excluded during review"
    selected_id = album.get("selected_candidate_id")
    candidate = next((item for item in album.get("candidates", []) if item.get("id") == selected_id), None)
    if candidate is None:
        return False, "no candidate is selected"
    if candidate.get("confidence") != 1.0:
        return False, "selected candidate is not an exact 100% match"
    alternatives = [item for item in album.get("candidates", []) if item.get("id") != selected_id]
    if any(item.get("confidence") == candidate["confidence"] for item in alternatives):
        return False, "best candidate is tied"
    tracks = album.get("tracks", [])
    details = candidate.get("track_details", [])
    if (not tracks or len(details) != len(tracks)
            or candidate.get("track_count_mismatch")
            or candidate.get("unmatched_track_count")
            or candidate.get("hard_mismatches")
            or candidate.get("matched_track_count") != len(tracks)
            or any(detail.get("status") != "matched" or detail.get("local") is None
                   or detail.get("proposed") is None for detail in details)):
        return False, "track pairing is incomplete or ambiguous"
    if any(track.get("exception") is not None for track in tracks):
        return False, "album has track exceptions"
    if album.get("duplicate_preflight", {}).get("status") != "clear":
        return False, "album has a duplicate conflict"
    return True, "exact, current, conflict-free match"


def _with_readiness(album: dict) -> dict:
    ready, reason = _album_bulk_readiness(album)
    return {**album, "ready": ready, "readiness_reason": reason}


def _album_ids_for_scopes(app: Flask, scopes: list[str], *, include_complete: bool) -> list[int]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            """SELECT albums.id, albums.execution_status, inventory.relative_path
                 FROM album_reviews AS albums
                 JOIN album_review_tracks AS tracks ON tracks.album_review_id = albums.id
                 JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                WHERE albums.root_path = ? AND inventory.present = 1
                ORDER BY albums.id""",
            (root,),
        ).fetchall()
    wanted = set(scopes)
    return list(dict.fromkeys(
        row["id"] for row in rows
        if _scope_for_relative_path(row["relative_path"]) in wanted
        and (include_complete or row["execution_status"] != "complete")
    ))


def _folder_queue_page(app: Flask, *, page: int, query: str, status: str) -> dict:
    if status not in FOLDER_STATUSES:
        raise ValueError("status must be all, not-scanned, ready, needs-review, error, or imported")
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    scopes = _literal_folder_scopes(root)
    root_text = str(root)
    with _connect(app.config["APP_DB"]) as db:
        inventory = db.execute(
            """SELECT inventory.relative_path, inventory.beets_item_id, inventory.present,
                      albums.id AS album_id, albums.artist, albums.album, albums.execution_status,
                      albums.execution_error, albums.candidate_status, albums.candidate_error, albums.state,
                      selected.confidence AS selected_confidence
                 FROM library_inventory AS inventory
                 LEFT JOIN album_review_tracks AS tracks ON tracks.inventory_id = inventory.id
                 LEFT JOIN album_reviews AS albums ON albums.id = tracks.album_review_id
                 LEFT JOIN metadata_candidates AS selected ON selected.id = albums.selected_candidate_id
                WHERE inventory.root_path = ?""",
            (root_text,),
        ).fetchall()
        scan_rows = db.execute(
            "SELECT summary_json FROM adoption_jobs WHERE root_path=? AND kind='folder_inventory' AND status='complete'",
            (root_text,),
        ).fetchall()
    scanned_scopes = {
        summary.get("folder") for row in scan_rows
        for summary in [json.loads(row["summary_json"])] if summary.get("folder")
    }
    # Inventory rows already belong to exactly one literal top-level scope. Group
    # them once instead of rescanning the complete inventory for every folder.
    rows_by_scope = {scope: [] for scope in scopes}
    for row in inventory:
        if row["present"]:
            rows_by_scope.setdefault(_scope_for_relative_path(row["relative_path"]), []).append(row)

    records = []
    needle = query.casefold()
    for scope in scopes:
        rows = rows_by_scope.get(scope, [])
        album_ids = {row["album_id"] for row in rows if row["album_id"] is not None}
        pending = {row["album_id"] for row in rows if row["album_id"] is not None
                   and row["execution_status"] != "complete"}
        imported = {row["album_id"] for row in rows if row["album_id"] is not None
                    and row["execution_status"] == "complete"}
        errors = {row["album_id"] for row in rows if row["album_id"] is not None
                  and row["execution_status"] != "complete"
                  and (row["execution_status"] == "failed" or row["candidate_status"] == "error")}
        error_message = next((row["execution_error"] or row["candidate_error"] for row in rows
                              if row["album_id"] in errors
                              and (row["execution_error"] or row["candidate_error"])), "")
        if not rows and scope not in scanned_scopes:
            fixed_status = "not-scanned"
        elif errors:
            fixed_status = "error"
        elif pending:
            fixed_status = None
        elif imported:
            fixed_status = "imported"
        else:
            # A scanned empty scope, or one containing only already-managed
            # tracks with no active review album, has no queue work to show.
            continue
        if fixed_status == "imported" and status == "all":
            continue
        if fixed_status != "imported" and status == "imported":
            continue
        visible_ids = imported if fixed_status == "imported" else pending
        visible_rows = [row for row in rows if row["album_id"] in visible_ids]
        haystack = " ".join([
            scope, *(row["relative_path"] for row in visible_rows),
            *(str(row["artist"] or "") for row in visible_rows),
            *(str(row["album"] or "") for row in visible_rows),
        ]).casefold()
        if needle and needle not in haystack:
            continue
        records.append({
            "path": scope, "name": "Library root" if scope == "." else scope,
            "status": fixed_status, "track_count": len(visible_rows), "album_count": len(visible_ids),
            "error": error_message,
            "available_album_count": len(pending), "imported_album_count": len(imported),
            "_pending_ids": pending,
            "_readiness_ids": {
                row["album_id"] for row in rows
                if row["album_id"] is not None and row["execution_status"] not in {"complete", "failed"}
                and row["candidate_status"] == "complete" and not row["candidate_error"]
                and row["state"] not in {"rejected", "skipped"}
                # A managed projection can make the persisted provider score
                # stale after settings change. Include it for payload-level
                # recomputation; _album_bulk_readiness remains the strict gate.
                and (app.config.get("FTINTITLE", {}).get("projection_requires_recheck")
                     or row["selected_confidence"] == 1.0)
            },
        })

    # Status filters must classify every matching folder. The ordinary all-status
    # path paginates first, so candidate construction, duplicate checks, and audio
    # duration inspection are bounded to albums on the displayed 25-folder page.
    if status != "all":
        fixed_matches = [record for record in records if record["status"] == status]
        unresolved = [record for record in records if record["status"] is None]
        if status in {"ready", "needs-review"}:
            readiness_ids = sorted({album_id for record in unresolved for album_id in record["_readiness_ids"]})
            ready_ids = {
                album["id"] for album in map(_with_readiness, _album_review_payloads(app, readiness_ids))
                if album["ready"]
            }
            for record in unresolved:
                record["status"] = "ready" if record["_pending_ids"] & ready_ids else "needs-review"
            records = fixed_matches + [record for record in unresolved if record["status"] == status]
        else:
            records = fixed_matches

    total = len(records)
    page_count = max(1, math.ceil(total / FOLDER_PAGE_SIZE))
    page = min(page, page_count)
    start = (page - 1) * FOLDER_PAGE_SIZE
    page_records = records[start:start + FOLDER_PAGE_SIZE]
    if status == "all":
        unresolved = [record for record in page_records if record["status"] is None]
        readiness_ids = sorted({album_id for record in unresolved for album_id in record["_readiness_ids"]})
        ready_ids = {
            album["id"] for album in map(_with_readiness, _album_review_payloads(app, readiness_ids))
            if album["ready"]
        }
        for record in unresolved:
            record["status"] = "ready" if record["_pending_ids"] & ready_ids else "needs-review"
    for record in page_records:
        ready = record["_pending_ids"] & (ready_ids if record["status"] in {"ready", "needs-review"} else set())
        record["ready_album_count"] = len(ready)
        record["exception_album_count"] = len(record["_pending_ids"] - ready)
        record.pop("_pending_ids")
        record.pop("_readiness_ids")
    return {"folders": page_records, "page": page, "page_count": page_count,
            "total": total, "limit": FOLDER_PAGE_SIZE, "query": query, "status": status}


def _scope_audio_stats(root: Path, scope: str) -> list[tuple[Path, os.stat_result]]:
    if scope != ".":
        return _library_audio_stats(root / scope)
    files = []
    for path in sorted(root.iterdir(), key=lambda item: (item.name.casefold(), item.name)):
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in AUDIO_EXTENSIONS:
            files.append((path.resolve(), path.stat()))
    return files


def _preview_folder_inventory(app: Flask, scope: str) -> dict:
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    created_at = _now()
    with _connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('folder_inventory', ?, 'running', ?)",
            (str(root), created_at),
        ).lastrowid
    try:
        discovered = _scope_audio_stats(root, scope)
        beets_paths = {str(Path(os.fsdecode(item.path)).resolve()): item.id for item in _library(app).items() if item.path}
        with _connect(app.config["APP_DB"]) as db:
            existing = db.execute(
                "SELECT id, relative_path FROM library_inventory WHERE root_path = ?", (str(root),)
            ).fetchall()
            scoped_ids = [(row["id"],) for row in existing if _relative_in_scope(row["relative_path"], scope)]
            if scoped_ids:
                db.executemany("UPDATE library_inventory SET present = 0, beets_item_id = NULL WHERE id = ?", scoped_ids)
            tracked = 0
            for path, stat in discovered:
                relative = path.relative_to(root).as_posix()
                item_id = beets_paths.get(str(path)); tracked += item_id is not None
                db.execute(
                    """INSERT INTO library_inventory(root_path, relative_path, size_bytes, mtime_ns, device, inode,
                              beets_item_id, present, first_seen_job_id, last_seen_job_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(root_path, relative_path) DO UPDATE SET size_bytes=excluded.size_bytes,
                         mtime_ns=excluded.mtime_ns, device=excluded.device, inode=excluded.inode,
                         beets_item_id=excluded.beets_item_id, present=1, last_seen_job_id=excluded.last_seen_job_id""",
                    (str(root), relative, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino,
                     item_id, job_id, job_id),
                )
                inventory_id = db.execute(
                    "SELECT id FROM library_inventory WHERE root_path = ? AND relative_path = ?", (str(root), relative)
                ).fetchone()["id"]
                db.execute("INSERT OR IGNORE INTO adoption_reviews(inventory_id, updated_at) VALUES (?, ?)",
                           (inventory_id, created_at))
                candidates = ([{"kind": "beets-item", "beets_item_id": item_id}] if item_id is not None
                              else [{"kind": "needs-review", "match": None}])
                db.execute("UPDATE adoption_reviews SET candidates_json = ?, updated_at = ? WHERE inventory_id = ?",
                           (json.dumps(candidates), created_at, inventory_id))
            summary = {"id": job_id, "folder": scope, "status": "complete", "files": len(discovered),
                       "tracked": tracked, "untracked": len(discovered) - tracked, "finished_at": _now()}
            db.execute("UPDATE adoption_jobs SET status='complete', summary_json=?, finished_at=? WHERE id=?",
                       (json.dumps(summary), summary["finished_at"], job_id))
        return summary
    except Exception as exc:
        message = f"folder inventory failed for {scope}: {exc}"
        with _connect(app.config["APP_DB"]) as db:
            db.execute("UPDATE adoption_jobs SET status='failed', error=?, finished_at=? WHERE id=?",
                       (message[:1000], _now(), job_id))
        raise ValueError(message) from exc


def _scan_folder_scopes(app: Flask, scopes: list[str]) -> dict:
    folders, errors = [], []
    successful = []
    for scope in scopes:
        try:
            folders.append(_preview_folder_inventory(app, scope)); successful.append(scope)
        except ValueError as exc:
            errors.append({"folder": scope, "error": str(exc)})
    albums = []
    if successful:
        _sync_album_reviews(app)
        album_ids = _album_ids_for_scopes(app, successful, include_complete=False)
        if album_ids:
            with _connect(app.config["APP_DB"]) as db:
                db.executemany(
                    """UPDATE album_reviews SET candidate_status='stale',
                              candidate_error='Inventory refreshed; candidate evidence requires refresh.'
                         WHERE id=? AND execution_status!='complete'""", [(album_id,) for album_id in album_ids]
                )
            generated = _generate_musicbrainz_candidates(app, len(album_ids), album_ids=album_ids)
            error_by_id = {item["album_review_id"]: item for item in generated["errors"]}
            for album in map(_with_readiness, _album_review_payloads(app, album_ids)):
                albums.append({"album_review_id": album["id"], "artist": album["artist"], "album": album["album"],
                               "status": "error" if album["id"] in error_by_id else "ready" if album["ready"] else "needs-review",
                               "ready": album["ready"], "error": error_by_id.get(album["id"], {}).get("error", "")})
            errors.extend({"album_review_id": item["album_review_id"], "error": item["error"]}
                          for item in generated["errors"])
        by_id = {album["album_review_id"]: album for album in albums}
        for folder in folders:
            folder_ids = _album_ids_for_scopes(app, [folder["folder"]], include_complete=False)
            folder["albums"] = [by_id[album_id] for album_id in folder_ids if album_id in by_id]
            folder["errors"] = [album for album in folder["albums"] if album["status"] == "error"]
    return {"folders": folders, "albums": albums, "errors": errors,
            "selected_folder_count": len(scopes), "scanned_folder_count": len(successful)}


def _bulk_ready_summary(app: Flask, scopes: list[str]) -> dict:
    album_ids = _album_ids_for_scopes(app, scopes, include_complete=False)
    albums = [_with_readiness(album) for album in _album_review_payloads(app, album_ids)]
    ready = [album for album in albums if album["ready"]]
    excluded = [album for album in albums if not album["ready"]]
    return {"selected_folder_count": len(scopes), "available_album_count": len(albums),
            "ready_album_count": len(ready), "track_count": sum(len(album["tracks"]) for album in ready),
            "exception_count": len(excluded),
            "ready_albums": [{"id": album["id"], "artist": album["artist"], "album": album["album"],
                              "tracks": len(album["tracks"])} for album in ready],
            "exceptions": [{"id": album["id"], "artist": album["artist"], "album": album["album"],
                            "reason": album["readiness_reason"]} for album in excluded]}


def _execute_bulk_ready(app: Flask, scopes: list[str]) -> dict:
    summary = _bulk_ready_summary(app, scopes)
    imported, failed = [], []
    for album in summary["ready_albums"]:
        try:
            current = next(iter(_album_review_payloads(app, [album["id"]])), None)
            ready, reason = _album_bulk_readiness(current or {})
            if not ready:
                raise LibraryImportExecutionError(f"Album is no longer ready: {reason}", code="not_ready")
            _update_album_review(app, album["id"], {"decision": "approved"})
            _execute_library_import_album(app, album["id"], dry_run=True)
            result = _execute_library_import_album(app, album["id"], dry_run=False)
            imported.append({"album_review_id": album["id"], "files": result["files"],
                             "artist": album["artist"], "album": album["album"]})
        except Exception as exc:
            failed.append({"album_review_id": album["id"], "artist": album["artist"],
                           "album": album["album"], "error": str(exc)})
    return {"selected_folder_count": len(scopes), "imported": imported,
            "skipped": summary["exceptions"], "failed": failed}


def _preview_library_inventory(app: Flask) -> dict:
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    created_at = _now()
    with _connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('inventory_preview', ?, 'running', ?)",
            (str(root), created_at),
        ).lastrowid

    try:
        discovered = _library_audio_stats(root)
        beets_paths = {}
        for item in _library(app).items():
            item_path = item.get("path")
            if item_path:
                beets_paths[str(Path(os.fsdecode(item_path)).resolve())] = item.id
    except (OSError, sqlite3.Error) as exc:
        message = f"library inventory failed: {exc}"
        with _connect(app.config["APP_DB"]) as db:
            db.execute(
                "UPDATE adoption_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
                (message, _now(), job_id),
            )
        raise ValueError(message) from exc

    tracked = 0
    total_bytes = 0
    sample = []
    with _connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE library_inventory SET present = 0, beets_item_id = NULL WHERE root_path = ?", (str(root),))
        for path, stat in discovered:
            relative = path.relative_to(root).as_posix()
            item_id = beets_paths.get(str(path))
            tracked += item_id is not None
            total_bytes += stat.st_size
            db.execute(
                """INSERT INTO library_inventory(
                       root_path, relative_path, size_bytes, mtime_ns, device, inode, beets_item_id,
                       present, first_seen_job_id, last_seen_job_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(root_path, relative_path) DO UPDATE SET
                       size_bytes = excluded.size_bytes, mtime_ns = excluded.mtime_ns,
                       device = excluded.device, inode = excluded.inode,
                       beets_item_id = excluded.beets_item_id, present = 1,
                       last_seen_job_id = excluded.last_seen_job_id""",
                (str(root), relative, stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino, item_id, job_id, job_id),
            )
            inventory_id = db.execute(
                "SELECT id FROM library_inventory WHERE root_path = ? AND relative_path = ?", (str(root), relative)
            ).fetchone()["id"]
            db.execute(
                "INSERT OR IGNORE INTO adoption_reviews(inventory_id, updated_at) VALUES (?, ?)",
                (inventory_id, created_at),
            )
            candidates = (
                [{"kind": "beets-item", "beets_item_id": item_id}]
                if item_id is not None
                else [{"kind": "needs-review", "match": None}]
            )
            db.execute(
                "UPDATE adoption_reviews SET candidates_json = ?, updated_at = ? WHERE inventory_id = ?",
                (json.dumps(candidates), created_at, inventory_id),
            )
            if item_id is None and len(sample) < MAX_INVENTORY_SAMPLE:
                sample.append(relative)

        missing = db.execute(
            "SELECT COUNT(*) AS count FROM library_inventory WHERE root_path = ? AND present = 0", (str(root),)
        ).fetchone()["count"]
        summary = {
            "id": job_id,
            "status": "complete",
            "root": str(root),
            "files": len(discovered),
            "tracked": tracked,
            "untracked": len(discovered) - tracked,
            "missing": missing,
            "bytes": total_bytes,
            "sample": sample,
            "sample_truncated": len(discovered) - tracked > len(sample),
            "created_at": created_at,
            "finished_at": _now(),
        }
        db.execute(
            "UPDATE adoption_jobs SET status = 'complete', summary_json = ?, finished_at = ? WHERE id = ?",
            (json.dumps(summary), summary["finished_at"], job_id),
        )
    return summary


def _library_audio_stats(root: Path) -> list[tuple[Path, os.stat_result]]:
    if not root.is_dir():
        raise OSError("configured library path is not a directory")
    files = []
    for directory, names, filenames in os.walk(root, followlinks=False, onerror=_raise_os_error):
        names[:] = sorted(name for name in names if not (Path(directory) / name).is_symlink())
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            files.append((path.resolve(), path.stat()))
    return files


def _raise_os_error(error: OSError) -> None:
    raise error


def _latest_inventory_summary(app: Flask) -> dict | None:
    with _connect(app.config["APP_DB"]) as db:
        row = db.execute(
            "SELECT summary_json FROM adoption_jobs WHERE kind = 'inventory_preview' AND status = 'complete' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.loads(row["summary_json"]) if row else None


def _library_import_review_album_page(app: Flask, limit: int, offset: int) -> tuple[list[int], bool, int]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        albums = db.execute(
            """SELECT albums.id FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND albums.execution_status != 'complete'
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                ORDER BY albums.artist_key, albums.album_key, albums.id
                LIMIT ? OFFSET ?""",
            (root, limit + 1, offset),
        ).fetchall()
        total = db.execute(
            """SELECT COUNT(*) FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND albums.execution_status != 'complete'
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)""",
            (root,),
        ).fetchone()[0]
    return [row["id"] for row in albums[:limit]], len(albums) > limit, total


def _library_import_review_items(app: Flask, album_ids: list[int]) -> list[dict]:
    if not album_ids:
        return []
    placeholders = ", ".join("?" for _ in album_ids)
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            f"""SELECT inventory.id, inventory.relative_path, inventory.beets_item_id,
                       reviews.state, reviews.candidates_json, reviews.updated_at
                  FROM album_review_tracks AS tracks
                  JOIN album_reviews AS albums ON albums.id = tracks.album_review_id
                  JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                  JOIN adoption_reviews AS reviews ON reviews.inventory_id = inventory.id
                 WHERE albums.root_path = ? AND albums.id IN ({placeholders})
                   AND inventory.present = 1
                 ORDER BY albums.artist_key, albums.album_key, albums.id,
                          inventory.relative_path COLLATE NOCASE, inventory.id""",
            (root, *album_ids),
        ).fetchall()
    library = _library(app)
    return [
        _serialize_library_import_review(row, library.get_item(row["beets_item_id"]) if row["beets_item_id"] else None)
        for row in rows
    ]


def _library_import_review_item(app: Flask, inventory_id: int) -> dict:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        row = db.execute(
            """SELECT inventory.id, inventory.relative_path, inventory.beets_item_id,
                      reviews.state, reviews.candidates_json, reviews.updated_at
                 FROM library_inventory AS inventory
                 JOIN adoption_reviews AS reviews ON reviews.inventory_id = inventory.id
                WHERE inventory.id = ? AND inventory.root_path = ? AND inventory.present = 1""",
            (inventory_id, root),
        ).fetchone()
    if row is None:
        abort(404)
    item = _library(app).get_item(row["beets_item_id"]) if row["beets_item_id"] else None
    return _serialize_library_import_review(row, item)


def _normalize_metadata(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split()).strip()


def _item_genre(item: Item) -> str:
    """Read genre across beets' legacy scalar and current multi-value fields."""
    genre = _normalize_metadata(item.get("genre"))
    if genre:
        return genre
    genres = item.get("genres")
    if isinstance(genres, (list, tuple)):
        for value in genres:
            normalized = _normalize_metadata(value)
            if normalized:
                return normalized
        return ""
    return _normalize_metadata(genres)


def _set_item_value(item: Item, field: str, value: object) -> None:
    item[field] = value
    if field == "genre" and "genres" in item._fields:
        normalized = _normalize_metadata(value)
        item["genres"] = [normalized] if normalized else []


def _metadata_key(value: object) -> str:
    return _normalize_metadata(value).casefold()


def _genre_presentation_key(value: object) -> str:
    """Compare genre presentation using only whitespace folding and Unicode casefolding."""
    return " ".join(str(value or "").split()).casefold()


def _format_genre(value: object) -> str:
    """Title-case one genre after whitespace cleanup while preserving its punctuation."""
    return " ".join(str(value or "").split()).title()


def _normalize_track_title(value: object) -> str:
    title = _normalize_metadata(value)
    return re.sub(r"^\s*(?:\d+[.-]?\s*[-–—.]?\s*)", "", title) or title


def _track_title_key(value: object) -> str:
    title = _normalize_track_title(value).casefold()
    return title.translate(str.maketrans({
        "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'", "＇": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"', "＂": '"',
    }))


def _all_library_import_review_items(app: Flask) -> list[dict]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            """SELECT inventory.id, inventory.relative_path, inventory.beets_item_id,
                      reviews.state, reviews.candidates_json, reviews.updated_at
                 FROM library_inventory AS inventory
                 JOIN adoption_reviews AS reviews ON reviews.inventory_id = inventory.id
                WHERE inventory.root_path = ? AND inventory.present = 1
                  AND reviews.imported_at IS NULL
                ORDER BY inventory.relative_path COLLATE NOCASE, inventory.id""",
            (root,),
        ).fetchall()
    library = _library(app)
    return [
        _serialize_library_import_review(row, library.get_item(row["beets_item_id"]) if row["beets_item_id"] else None)
        for row in rows
    ]


def _sync_album_reviews(app: Flask) -> None:
    """Persist stable album groups without touching media or the beets database."""
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    groups: dict[tuple[str, str, str], dict] = {}
    for item in _all_library_import_review_items(app):
        artist = _normalize_metadata(item["artist"]) or "Unknown artist"
        album = _normalize_metadata(item["album"]) or "Unknown album"
        # Folder scope is part of workflow identity. Identical tags in sibling
        # top-level folders must never make selecting one folder operate on the
        # other's files. Multi-disc subfolders inside one top-level scope still
        # remain one matching/import unit.
        scope_key = _scope_for_relative_path(item["path"])
        group = groups.setdefault(
            (scope_key, _metadata_key(artist), _metadata_key(album)),
            {"artist": artist, "album": album, "items": []},
        )
        group["items"].append(item)
    now = _now()
    with _connect(app.config["APP_DB"]) as db:
        _migrate_legacy_album_scope_keys(db, root, now)
        exceptions = {
            row["inventory_id"]: row["exception_state"]
            for row in db.execute(
                """SELECT tracks.inventory_id, tracks.exception_state
                     FROM album_review_tracks AS tracks
                     JOIN album_reviews AS albums ON albums.id = tracks.album_review_id
                    WHERE albums.root_path = ? AND tracks.exception_state IS NOT NULL""", (root,)
            )
        }
        db.execute(
            """DELETE FROM album_review_tracks
                 WHERE album_review_id IN (
                   SELECT id FROM album_reviews WHERE root_path = ? AND execution_status != 'complete'
                 )""",
            (root,),
        )
        for (scope_key, metadata_artist_key, album_key), group in groups.items():
            artist_key = f"{scope_key.casefold()}\x1f{metadata_artist_key}"
            db.execute(
                """INSERT INTO album_reviews(root_path, artist_key, album_key, artist, album, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(root_path, artist_key, album_key) DO UPDATE SET
                     artist = excluded.artist, album = excluded.album""",
                (root, artist_key, album_key, group["artist"], group["album"], now),
            )
            album_id = db.execute(
                "SELECT id FROM album_reviews WHERE root_path = ? AND artist_key = ? AND album_key = ?",
                (root, artist_key, album_key),
            ).fetchone()["id"]
            db.executemany(
                "INSERT INTO album_review_tracks(album_review_id, inventory_id, exception_state) VALUES (?, ?, ?)",
                [(album_id, item["id"], exceptions.get(item["id"])) for item in group["items"]],
            )


def _migrate_legacy_album_scope_keys(db: sqlite3.Connection, root: str, now: str) -> None:
    """Keep pre-folder-workflow review IDs and choices when adding scope identity.

    Earlier databases keyed an album only by normalized artist and album. Most
    such rows belong to one top-level folder and can be upgraded in place. If a
    legacy row spans sibling folders, retain it for the first deterministic
    scope but mark its merged candidate evidence stale; subsequent scopes get
    new review rows during the normal sync.
    """
    legacy_rows = db.execute(
        """SELECT id, artist, artist_key, album_key
             FROM album_reviews
            WHERE root_path = ? AND execution_status != 'complete'
              AND instr(artist_key, char(31)) = 0
            ORDER BY id""",
        (root,),
    ).fetchall()
    for album in legacy_rows:
        paths = db.execute(
            """SELECT inventory.relative_path
                 FROM album_review_tracks AS tracks
                 JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                WHERE tracks.album_review_id = ? AND inventory.present = 1
                ORDER BY inventory.relative_path COLLATE NOCASE, inventory.id""",
            (album["id"],),
        ).fetchall()
        scopes = sorted({_scope_for_relative_path(row["relative_path"]) for row in paths}, key=str.casefold)
        if not scopes:
            continue
        scoped_artist_key = f"{scopes[0].casefold()}\x1f{_metadata_key(album['artist'])}"
        conflict = db.execute(
            """SELECT id FROM album_reviews
                WHERE root_path = ? AND artist_key = ? AND album_key = ? AND id != ?""",
            (root, scoped_artist_key, album["album_key"], album["id"]),
        ).fetchone()
        if conflict is not None:
            # A prior folder-first sync may already have created an empty
            # replacement row. Prefer the legacy row when it carries the only
            # candidate history, so upgrades are idempotent across restarts.
            legacy_candidates = db.execute(
                "SELECT COUNT(*) AS count FROM metadata_candidates WHERE album_review_id = ?", (album["id"],)
            ).fetchone()["count"]
            conflict_candidates = db.execute(
                "SELECT COUNT(*) AS count FROM metadata_candidates WHERE album_review_id = ?", (conflict["id"],)
            ).fetchone()["count"]
            if legacy_candidates and not conflict_candidates:
                db.execute("DELETE FROM album_review_tracks WHERE album_review_id = ?", (conflict["id"],))
                db.execute("DELETE FROM album_reviews WHERE id = ?", (conflict["id"],))
            else:
                continue
        assignments = ["artist_key = ?", "updated_at = ?"]
        values: list[object] = [scoped_artist_key, now]
        if len(scopes) > 1:
            assignments.extend(["candidate_status = 'stale'", "candidate_error = ?"])
            values.append("Album was split by top-level folder; refresh candidate evidence.")
        db.execute(
            f"UPDATE album_reviews SET {', '.join(assignments)} WHERE id = ?",
            (*values, album["id"]),
        )


def _candidate_album_rows(app: Flask, limit: int, album_ids: list[int] | None = None) -> list[sqlite3.Row]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    album_filter = ""
    parameters: list[object] = [root]
    if album_ids is not None:
        if not album_ids:
            return []
        album_filter = f" AND albums.id IN ({', '.join('?' for _ in album_ids)})"
        parameters.extend(album_ids)
    parameters.append(limit)
    with _connect(app.config["APP_DB"]) as db:
        return db.execute(
            """SELECT albums.id, albums.artist, albums.album
                 FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND albums.execution_status != 'complete'
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                  {album_filter}
                ORDER BY CASE albums.candidate_status
                           WHEN 'stale' THEN 0 WHEN 'not-run' THEN 1 WHEN 'error' THEN 2 ELSE 3
                         END,
                         albums.artist_key, albums.album_key, albums.id LIMIT ?""".format(album_filter=album_filter),
            tuple(parameters),
        ).fetchall()


def _album_query(app: Flask, album: sqlite3.Row) -> dict:
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            """SELECT inventory.relative_path, inventory.beets_item_id
                 FROM album_review_tracks AS tracks
                 JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                WHERE tracks.album_review_id = ? ORDER BY inventory.relative_path COLLATE NOCASE""",
            (album["id"],),
        ).fetchall()
    library = _library(app)
    tracks = []
    years = []
    dates = []
    release_ids = []
    release_types = []
    media_types = []
    countries = []
    genres = []
    artwork_url = None
    for row in rows:
        item = library.get_item(row["beets_item_id"]) if row["beets_item_id"] else None
        if item is None:
            try:
                item = Item.from_path(Path(app.config["LIBRARY_PATH"]) / row["relative_path"])
            except Exception:
                # Inventory may contain unreadable or synthetic fixtures. Path
                # fallback still provides a reviewable, non-mutating query.
                item = None
        title = item.get("title") if item and item.get("title") else Path(row["relative_path"]).stem
        track = {"title": _normalize_track_title(title), "path": row["relative_path"]}
        stored_duration = item.get("length") if item and item.get("length") not in (None, "", 0) else None
        duration = _audio_duration(Path(app.config["LIBRARY_PATH"]) / row["relative_path"], stored_duration)
        if duration is not None:
            track["duration"] = duration
        if item:
            track["artist"] = _normalize_metadata(item.get("artist"))
            for source, target in (("disc", "disc"), ("track", "track"),
                                   ("mb_trackid", "recording_id")):
                if item.get(source) not in (None, "", 0):
                    track[target] = item.get(source)
            if item.get("year"):
                years.append(item.get("year"))
                month = int(item.get("month") or 0)
                day = int(item.get("day") or 0)
                dates.append(f"{int(item.get('year')):04d}" + (f"-{month:02d}" if month else "")
                             + (f"-{day:02d}" if month and day else ""))
            if item.get("mb_albumid"):
                release_ids.append(str(item.get("mb_albumid")).lower())
            for field, values in (("albumtype", release_types), ("media", media_types), ("country", countries)):
                if item.get(field):
                    values.append(str(item.get(field)))
            if _item_genre(item):
                genres.append(_item_genre(item))
            if artwork_url is None:
                artwork_url = _persisted_artwork_source({
                    key: item.get(key) for key in ("artwork_url", "image_url", "cover_url", "cover_art")
                }, allow_data=True)
        tracks.append(track)
    query = {"artist": _normalize_metadata(album["artist"]), "album": _normalize_metadata(album["album"]), "tracks": tracks}
    if years:
        query["year"] = years[0]
        query["date"] = dates[0]
    if release_ids and len(set(release_ids)) == 1:
        query["release_mbid"] = release_ids[0]
    for field, values in (("release_type", release_types), ("media", media_types), ("country", countries)):
        if values and len(set(values)) == 1:
            query[field] = values[0]
    if genres and len(set(genres)) == 1:
        query["genre"] = genres[0]
    if artwork_url:
        query["artwork_url"] = artwork_url
    return query


def _audio_duration(path: Path, fallback: object = None) -> float | object | None:
    """Read an audio stream's duration without changing the file or library."""
    try:
        measured = MediaFile(str(path)).length
        duration = float(measured)
        if math.isfinite(duration) and duration > 0:
            return duration
    except (OSError, TypeError, ValueError, UnreadableFileError):
        pass
    return fallback


def _candidate_match(query: dict, candidate: dict, *, exact_mbid: bool = False) -> dict:
    exact = exact_mbid or bool(query.get("release_mbid") and
                               str(candidate.get("provider_id", "")).lower() == query["release_mbid"])
    retrieval = candidate.get("retrieval") if isinstance(candidate.get("retrieval"), dict) else {}
    exact = exact or retrieval.get("source") == "release-id"
    return score_release(query, candidate, exact_mbid=exact)


def _effective_candidate(app: Flask, candidate: dict, query: dict | None = None) -> dict:
    """Project reviewed-Library behavior without mutating provider evidence.

    Provider JSON remains the audit source. Album-edition preservation requires
    local Library metadata and is deliberately not applied to Inbox/beets CLI
    imports. FtInTitle projection only calls bundled parser helpers; it never
    constructs a plugin or runs import hooks.
    """
    projected = dict(candidate)
    local_album = _normalize_metadata(query.get("album")) if isinstance(query, dict) else ""
    canonical_album = _normalize_metadata(candidate.get("album"))
    suffix = re.fullmatch(
        r"(?P<base>.+?)\s*(?:\((?P<parenthesized>[^\[\]()]+)\)|\[(?P<bracketed>[^\[\]()]+)\])",
        local_album,
    )
    if suffix:
        local_base = _normalize_metadata(suffix.group("base"))
        qualifier = _normalize_metadata(suffix.group("parenthesized") or suffix.group("bracketed"))
        release_notes = [
            _normalize_metadata(part) for part in
            re.split(r"[,;]", str(candidate.get("release_disambiguation") or ""))
        ]
        if (_metadata_key(canonical_album) == _metadata_key(local_base)
                and _metadata_key(qualifier) in {_metadata_key(note) for note in release_notes if note}):
            projected["album"] = local_album

    options = app.config.get("FTINTITLE", FTINTITLE_DEFAULTS)
    if not options.get("enabled"):
        return projected
    albumartist = _normalize_metadata(candidate.get("artist"))
    projected_tracks = []
    drop_feat = bool(options.get("drop", False))
    feat_format = str(options.get("format") or FTINTITLE_DEFAULTS["format"])
    keep_in_artist = bool(options.get("keep_in_artist"))
    preserve_album_artist = bool(options.get("preserve_album_artist", True))
    custom_words = list(options.get("custom_words") or [])
    bracket_keywords = list(options.get("bracket_keywords") or [])
    for raw_track in candidate.get("tracks", []) if isinstance(candidate.get("tracks"), list) else []:
        if not isinstance(raw_track, dict):
            projected_tracks.append(raw_track)
            continue
        track = dict(raw_track)
        artist = _normalize_metadata(track.get("track_artist"))
        title = _normalize_track_title(track.get("title"))
        structurally_valid = artist.count("(") == artist.count(")") and artist.count("[") == artist.count("]")
        if preserve_album_artist and albumartist and artist == albumartist:
            projected_tracks.append(track)
            continue
        for_artist = not albumartist or albumartist in artist
        if artist and structurally_valid:
            _, featured = split_on_feat(
                artist, for_artist=for_artist, custom_words=custom_words
            )
            feat_part = find_feat_part(artist, albumartist, custom_words) if featured else None
            if feat_part:
                if not keep_in_artist:
                    track["track_artist"] = split_on_feat(
                        artist, custom_words=custom_words
                    )[0]
                if not drop_feat and not contains_feat(title, custom_words):
                    try:
                        track["title"] = FtInTitlePlugin.insert_ft_into_title(
                            title, feat_format.format(feat_part), bracket_keywords
                        )
                    except (AttributeError, IndexError, KeyError, ValueError):
                        # A hand-edited invalid YAML format remains visible in
                        # Settings and is rejected on save; it cannot alter a
                        # reviewed candidate in the meantime.
                        track = dict(raw_track)
        projected_tracks.append(track)
    projected["tracks"] = projected_tracks
    return projected


def _candidate_confidence(query: dict, candidate: dict, *, exact_mbid: bool = False) -> float:
    return _candidate_match(query, candidate, exact_mbid=exact_mbid)["confidence"]


def _candidate_diff(query: dict, candidate: dict) -> dict:
    proposed = {}
    for key in ("artist", "album"):
        value = _normalize_metadata(candidate.get(key))
        if value and _metadata_key(value) != _metadata_key(query[key]):
            proposed[key] = {"from": query[key], "to": value}
    genre = _candidate_genre(candidate)
    if genre and _genre_presentation_key(genre) != _genre_presentation_key(query.get("genre")):
        proposed["genre"] = {"from": query.get("genre"), "to": genre}
    count = candidate.get("track_count")
    if isinstance(count, int) and count != len(query["tracks"]):
        proposed["track_count"] = {"from": len(query["tracks"]), "to": count}
    candidate_tracks = candidate.get("tracks")
    if isinstance(candidate_tracks, list):
        normalized_tracks = [_normalize_track_title(track.get("title") if isinstance(track, dict) else track)
                             for track in candidate_tracks]
        local_tracks = [_normalize_track_title(track.get("title") if isinstance(track, dict) else track)
                        for track in query["tracks"]]
        if [_track_title_key(track) for track in normalized_tracks] != [_track_title_key(track) for track in local_tracks]:
            proposed["tracks"] = {"from": local_tracks, "to": normalized_tracks}
        details = _candidate_track_details(query, candidate)
        local_artists = [detail.get("local_artist") or "" for detail in details if detail.get("local") is not None]
        proposed_artists = [detail.get("proposed_artist") or "" for detail in details if detail.get("local") is not None]
        if [_metadata_key(value) for value in local_artists] != [_metadata_key(value) for value in proposed_artists]:
            proposed["track_artists"] = {"from": local_artists, "to": proposed_artists}
    return proposed


def _candidate_reasons(query: dict, candidate: dict) -> list[str]:
    return _candidate_match(query, candidate)["reasons"]


def _candidate_genre(candidate: dict) -> str:
    evidence = candidate.get("genre_evidence")
    if not isinstance(evidence, dict) or evidence.get("source") != "musicbrainz-release-group-genres":
        return ""
    selected = " ".join(str(evidence.get("selected") or "").split())
    genre = " ".join(str(candidate.get("genre") or "").split())
    return _format_genre(genre) if genre and _genre_presentation_key(genre) == _genre_presentation_key(selected) else ""


def _candidate_track_details(query: dict, candidate: dict) -> list[dict]:
    return _candidate_match(query, candidate)["track_details"]


def _rematch_album_by_musicbrainz_id(app: Flask, album_review_id: int, musicbrainz_id: str) -> dict | None:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        album = db.execute(
            "SELECT id, artist, album FROM album_reviews WHERE id = ? AND root_path = ?",
            (album_review_id, root),
        ).fetchone()
    if album is None:
        return None
    query = _album_query(app, album)
    provider_query = {**query, "musicbrainz_id": musicbrainz_id}
    raw = app.config["MUSICBRAINZ_PROVIDER"](provider_query, limit=1)
    if not isinstance(raw, list):
        raise ProviderError("MusicBrainz provider returned invalid candidates", code="provider_invalid_response")
    if not raw:
        raise ProviderError(
            "No MusicBrainz release exists for that ID. Check that it is a release ID, not a release-group ID.",
            code="release_not_found",
        )
    candidate = next(
        (value for value in raw if isinstance(value, dict) and str(value.get("provider_id", "")).lower() == musicbrainz_id),
        None,
    )
    if candidate is None:
        raise ProviderError("MusicBrainz provider returned a different release ID", code="provider_invalid_response")
    candidate = dict(candidate)
    candidate["retrieval"] = {**(candidate.get("retrieval") or {}), "source": "release-id", "search_score": None}
    now = _now()
    with _connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE metadata_candidates SET rank = rank + 1 WHERE album_review_id = ?", (album_review_id,))
        existing = db.execute(
            "SELECT id FROM metadata_candidates WHERE album_review_id = ? AND provider = 'musicbrainz' AND provider_id = ?",
            (album_review_id, musicbrainz_id),
        ).fetchone()
        values = (
            1, _candidate_confidence(query, _effective_candidate(app, candidate, query), exact_mbid=True), _normalize_metadata(candidate.get("artist")),
            _normalize_metadata(candidate.get("album")), str(candidate.get("year")) if candidate.get("year") else None,
            json.dumps(_candidate_diff(query, _effective_candidate(app, candidate, query)), sort_keys=True), json.dumps(candidate, sort_keys=True), now,
        )
        if existing:
            candidate_id = existing["id"]
            db.execute(
                """UPDATE metadata_candidates SET rank = ?, confidence = ?, artist = ?, album = ?, year = ?,
                          proposed_diff_json = ?, provider_data_json = ?, schema_version = ?, created_at = ?
                    WHERE id = ?""",
                (*values[:-1], CANDIDATE_SCHEMA_VERSION, values[-1], candidate_id),
            )
        else:
            candidate_id = db.execute(
                """INSERT INTO metadata_candidates(
                       album_review_id, provider, provider_id, rank, confidence, artist, album, year,
                       proposed_diff_json, provider_data_json, schema_version, created_at
                   ) VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (album_review_id, musicbrainz_id, *values[:-1], CANDIDATE_SCHEMA_VERSION, values[-1]),
            ).lastrowid
        db.execute(
            """UPDATE album_reviews SET selected_candidate_id = ?, candidate_status = 'complete',
                      candidate_error = '', updated_at = ? WHERE id = ?""",
            (candidate_id, now, album_review_id),
        )
    return next(iter(_album_review_payloads(app, [album_review_id])), None)


def _generate_musicbrainz_candidates(app: Flask, limit: int, *, album_ids: list[int] | None = None) -> dict:
    provider = app.config["MUSICBRAINZ_PROVIDER"]
    albums = _candidate_album_rows(app, limit, album_ids)
    errors = []
    generated = 0
    with _connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('musicbrainz_candidates', ?, 'running', ?)",
            (str(Path(app.config["LIBRARY_PATH"]).resolve()), _now()),
        ).lastrowid
    for album in albums:
        query = _album_query(app, album)
        try:
            raw = provider(query, limit=MAX_CANDIDATES_PER_ALBUM)
            if not isinstance(raw, list):
                raise ProviderError("MusicBrainz provider returned invalid candidates")
            candidates = [
                candidate for candidate in raw[:MAX_CANDIDATES_PER_ALBUM]
                if isinstance(candidate, dict) and candidate.get("provider_id")
            ]
            candidates.sort(key=lambda candidate: (
                -_candidate_confidence(query, _effective_candidate(app, candidate, query)), str(candidate["provider_id"])
            ))
            with _connect(app.config["APP_DB"]) as db:
                selected = db.execute(
                    """SELECT candidates.provider, candidates.provider_id
                         FROM album_reviews AS albums
                         JOIN metadata_candidates AS candidates ON candidates.id = albums.selected_candidate_id
                        WHERE albums.id = ?""",
                    (album["id"],),
                ).fetchone()
                if selected is not None and not any(
                    selected["provider"] == "musicbrainz"
                    and str(candidate["provider_id"]) == selected["provider_id"]
                    for candidate in candidates
                ):
                    message = "The selected candidate was not returned during refresh; review it before importing."
                    db.execute(
                        """UPDATE album_reviews SET candidate_status = 'stale', candidate_error = ?,
                                  updated_at = ? WHERE id = ?""",
                        (message, _now(), album["id"]),
                    )
                    errors.append({"album_review_id": album["id"], "error": message, "retryable": False})
                    continue
                db.execute("DELETE FROM metadata_candidates WHERE album_review_id = ?", (album["id"],))
                for rank, candidate in enumerate(candidates, 1):
                    db.execute(
                        """INSERT INTO metadata_candidates(
                               album_review_id, provider, provider_id, rank, confidence, artist, album,
                               year, proposed_diff_json, provider_data_json, schema_version, created_at
                           ) VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (album["id"], str(candidate["provider_id"]), rank,
                         _candidate_confidence(query, _effective_candidate(app, candidate, query)),
                         _normalize_metadata(candidate.get("artist")), _normalize_metadata(candidate.get("album")),
                         str(candidate.get("year")) if candidate.get("year") else None,
                         json.dumps(_candidate_diff(query, _effective_candidate(app, candidate, query)), sort_keys=True),
                         json.dumps(candidate, sort_keys=True), CANDIDATE_SCHEMA_VERSION, _now()),
                    )
                selected_id = None
                if selected is not None:
                    refreshed = db.execute(
                        """SELECT id FROM metadata_candidates
                            WHERE album_review_id = ? AND provider = ? AND provider_id = ?""",
                        (album["id"], selected["provider"], selected["provider_id"]),
                    ).fetchone()
                    selected_id = refreshed["id"] if refreshed else None
                elif candidates:
                    matches = [_candidate_match(query, _effective_candidate(app, candidate, query)) for candidate in candidates]
                    best = matches[0]
                    tied = len(matches) > 1 and matches[1]["confidence"] == best["confidence"]
                    exact_tracks = (
                        best["confidence"] == 1.0 and not tied and not best["track_count_mismatch"]
                        and not best["unmatched_track_count"] and not best["hard_mismatches"]
                        and best["matched_track_count"] == len(query["tracks"])
                        and all(detail.get("status") == "matched" for detail in best["track_details"])
                    )
                    if exact_tracks:
                        selected_id = db.execute(
                            "SELECT id FROM metadata_candidates WHERE album_review_id=? ORDER BY rank, id LIMIT 1",
                            (album["id"],),
                        ).fetchone()["id"]
                db.execute(
                    """UPDATE album_reviews SET selected_candidate_id = ?, candidate_status = 'complete',
                              candidate_error = '', updated_at = ? WHERE id = ?""",
                    (selected_id, _now(), album["id"]),
                )
            generated += len(candidates)
        except (ProviderError, OSError, ValueError, TypeError) as exc:
            message = str(exc) or "candidate provider failed"
            errors.append({"album_review_id": album["id"], "error": message, "retryable": bool(getattr(exc, "retryable", False))})
            with _connect(app.config["APP_DB"]) as db:
                db.execute(
                    "UPDATE album_reviews SET candidate_status = 'error', candidate_error = ?, updated_at = ? WHERE id = ?",
                    (message[:500], _now(), album["id"]),
                )
    result = {"id": job_id, "albums": len(albums), "candidates": generated, "errors": errors}
    with _connect(app.config["APP_DB"]) as db:
        db.execute(
            "UPDATE adoption_jobs SET status = ?, summary_json = ?, error = ?, finished_at = ? WHERE id = ?",
            ("partial" if errors else "complete", json.dumps(result), "; ".join(error["error"] for error in errors)[:1000], _now(), job_id),
        )
    return result


def _persisted_artwork_source(provider_data: dict, *, allow_data: bool = False) -> str | None:
    """Expose only an already-persisted, browser-renderable artwork source."""
    values = [provider_data.get(key) for key in ("artwork_url", "image_url", "cover_url")]
    artwork = provider_data.get("artwork")
    if isinstance(artwork, dict) and artwork.get("source") == "cover-art-archive":
        values.insert(0, artwork.get("thumbnail_url"))
    cover_art = provider_data.get("cover_art")
    if isinstance(cover_art, dict):
        values.extend(cover_art.get(key) for key in ("url", "image", "data"))
    for value in values:
        if not isinstance(value, str):
            continue
        source = value.strip()
        lowered = source.lower()
        parsed = urllib.parse.urlparse(source)
        host = (parsed.hostname or "").lower()
        trusted_remote = parsed.scheme == "https" and (
            host in TRUSTED_ARTWORK_HOSTS or host.endswith(".archive.org")
        )
        if trusted_remote or (allow_data and lowered.startswith(
            ("data:image/png;", "data:image/jpeg;", "data:image/gif;", "data:image/webp;", "data:image/avif;")
        )):
            return source
    return None


def _duplicate_preflight(
    app: Flask, tracks: list[sqlite3.Row], query: dict, details: list[dict] | None = None,
    managed_items: list[Item] | None = None,
) -> dict:
    """Report recording-ID collisions without choosing identity or changing state."""
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    managed_by_recording: dict[str, list[Item]] = {}
    for item in managed_items if managed_items is not None else _library(app).items():
        recording_id = str(item.get("mb_trackid") or "").strip()
        if recording_id:
            managed_by_recording.setdefault(recording_id, []).append(item)
    proposed_by_position = {
        detail["position"]: str(detail.get("recording_id") or "").strip()
        for detail in (details or []) if detail.get("local") is not None and detail.get("recording_id")
    }
    warnings = []
    for position, (track, source) in enumerate(zip(tracks, query["tracks"]), 1):
        if track["beets_item_id"] is not None:
            continue
        recording_id = proposed_by_position.get(position) or str(source.get("recording_id") or "").strip()
        matches = managed_by_recording.get(recording_id, []) if recording_id else []
        if not matches:
            continue
        managed_paths = []
        for item in matches:
            try:
                path = Path(os.fsdecode(item.path)).resolve()
                managed_paths.append(path.relative_to(root).as_posix() if root in path.parents else str(path))
            except (OSError, ValueError, TypeError):
                managed_paths.append("Managed path unavailable")
        warnings.append({
            "incoming_path": track["relative_path"],
            "recording_id": recording_id,
            "managed_paths": sorted(managed_paths, key=str.casefold),
            "ambiguous": len(matches) != 1,
        })
    ambiguous = any(warning["ambiguous"] for warning in warnings)
    return {
        "status": "ambiguous" if ambiguous else "possible" if warnings else "clear",
        "count": len(warnings),
        "warnings": warnings,
        "message": (
            "Managed recording identity is ambiguous; queueing is blocked until the library identity is corrected."
            if ambiguous else
            f"{len(warnings)} incoming file{' may' if len(warnings) == 1 else 's may'} already be managed. Review the duplicate decision before queueing."
            if warnings else "No managed recording-ID duplicate was found during preflight."
        ),
    }


def _album_review_payloads(app: Flask, album_ids: list[int] | None = None) -> list[dict]:
    if album_ids == []:
        return []
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    album_filter = ""
    parameters: tuple = (root,)
    if album_ids is not None:
        album_filter = f" AND albums.id IN ({', '.join('?' for _ in album_ids)})"
        parameters = (root, *album_ids)
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            f"""SELECT albums.* FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND albums.execution_status != 'complete'
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                  {album_filter}
                ORDER BY albums.artist_key, albums.album_key, albums.id""", parameters
        ).fetchall()
        payloads = []
        managed_items = list(_library(app).items())
        for row in rows:
            query = _album_query(app, row)
            tracks = db.execute(
                """SELECT inventory.id, inventory.relative_path, inventory.beets_item_id, tracks.exception_state
                     FROM album_review_tracks AS tracks
                     JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                    WHERE tracks.album_review_id = ? ORDER BY inventory.relative_path COLLATE NOCASE""", (row["id"],)
            ).fetchall()
            candidates = db.execute(
                "SELECT * FROM metadata_candidates WHERE album_review_id = ? ORDER BY rank, id", (row["id"],)
            ).fetchall()
            serialized_candidates = []
            for candidate in candidates:
                provider_data = json.loads(candidate["provider_data_json"])
                effective_candidate = _effective_candidate(app, provider_data, query)
                match = _candidate_match(query, effective_candidate)
                serialized_candidates.append({
                    "id": candidate["id"], "provider": candidate["provider"],
                    "provider_id": candidate["provider_id"], "rank": candidate["rank"],
                    "confidence": match["confidence"], "distance": match["distance"],
                    "recommendation": match["recommendation"], "confidence_reasons": match["reasons"],
                    "penalties": match["penalties"], "track_count_mismatch": match["track_count_mismatch"],
                    "matched_track_count": match["matched_track_count"],
                    "unmatched_track_count": match["unmatched_track_count"],
                    "hard_mismatches": match["hard_mismatches"],
                    "selected_by_mbid": match["selected_by_mbid"],
                    "artist": _normalize_metadata(effective_candidate.get("artist")),
                    "album": _normalize_metadata(effective_candidate.get("album")), "year": candidate["year"],
                    "genre": _candidate_genre(effective_candidate) or None,
                    "genre_evidence": provider_data.get("genre_evidence"),
                    "date": provider_data.get("date"), "release_group_id": provider_data.get("release_group_id"),
                    "release_type": provider_data.get("release_type"),
                    "release_status": provider_data.get("status"), "country": provider_data.get("country"),
                    "release_disambiguation": provider_data.get("release_disambiguation"),
                    "artwork_url": _persisted_artwork_source(provider_data),
                    "artwork": provider_data.get("artwork"),
                    "media": provider_data.get("media", []), "recordings": effective_candidate.get("tracks", []),
                    "retrieval": provider_data.get("retrieval", {}),
                    "proposed_diff": _candidate_diff(query, effective_candidate),
                    "track_details": match["track_details"],
                    "duplicate_preflight": _duplicate_preflight(
                        app, tracks, query, match["track_details"], managed_items
                    ),
                })
            selected_id = row["selected_candidate_id"]
            proposed_match = next((candidate for candidate in serialized_candidates if candidate["id"] == selected_id), None)
            if proposed_match is None and serialized_candidates:
                proposed_match = serialized_candidates[0]
            duplicate_preflight = (
                proposed_match["duplicate_preflight"] if proposed_match is not None
                else _duplicate_preflight(app, tracks, query, managed_items=managed_items)
            )
            highest_confidence = max((candidate["confidence"] for candidate in serialized_candidates), default=None)
            current_metadata = {
                "artist": query["artist"], "album": query["album"],
                "year": query.get("year"), "date": query.get("date"),
                "genre": query.get("genre"),
                "track_count": len(query["tracks"]),
                "disc_count": len({track.get("disc") or 1 for track in query["tracks"]}),
                "artwork_url": query.get("artwork_url"),
            }
            current_metadata.update({field: query[field] for field in ("release_type", "media", "country")
                                     if query.get(field)})
            payloads.append({
                "id": row["id"], "artist": row["artist"], "album": row["album"], "decision": row["state"],
                "duplicate_action": row["duplicate_action"],
                "duplicate_actions": ["merge", "replace", "keep-both", "skip"],
                "source_paths": sorted({str(Path(track["relative_path"]).parent.as_posix()) for track in tracks}, key=str.casefold),
                "selected_candidate_id": selected_id, "proposed_match": proposed_match,
                "selection_mode": ("as-is" if row["state"] == "approved" and selected_id is None
                                   else "candidate" if selected_id is not None else "suggested"),
                "current_metadata": current_metadata,
                "highest_confidence": highest_confidence,
                "duplicate_preflight": duplicate_preflight,
                "candidate_status": row["candidate_status"],
                "candidate_error": row["candidate_error"],
                "execution_status": row["execution_status"],
                "execution_error": row["execution_error"],
                "last_execution_job_id": row["last_execution_job_id"],
                "executed_at": row["executed_at"],
                "tracks": [{"id": track["id"], "path": track["relative_path"],
                            "title": query["tracks"][index]["title"],
                            "artist": query["tracks"][index].get("artist"),
                            "duration": query["tracks"][index].get("duration"),
                            "disc": query["tracks"][index].get("disc"),
                            "position": query["tracks"][index].get("track"),
                            "recording_id": query["tracks"][index].get("recording_id"),
                            "exception": track["exception_state"],
                            "effective_decision": track["exception_state"] or row["state"]}
                           for index, track in enumerate(tracks)],
                "candidates": serialized_candidates,
                "updated_at": row["updated_at"],
            })
    return payloads


def _update_album_review(app: Flask, album_review_id: int, values: dict) -> dict | None:
    allowed = {"decision", "candidate_id", "track_exceptions", "duplicate_action"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    decision = values.get("decision")
    if decision is not None and decision not in LIBRARY_IMPORT_DECISIONS:
        raise ValueError("decision must be approved, rejected, or skipped")
    duplicate_action = values.get("duplicate_action")
    if "duplicate_action" in values and duplicate_action not in DUPLICATE_ACTIONS:
        raise ValueError("duplicate_action must be merge, replace, keep-both, or skip")
    exceptions = values.get("track_exceptions", {})
    if not isinstance(exceptions, dict):
        raise ValueError("track_exceptions must be an object keyed by track id")
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        if db.execute("SELECT id FROM album_reviews WHERE id = ? AND root_path = ?", (album_review_id, root)).fetchone() is None:
            return None
        candidate_id = values.get("candidate_id")
        if candidate_id is not None and db.execute(
            "SELECT id FROM metadata_candidates WHERE id = ? AND album_review_id = ?", (candidate_id, album_review_id)
        ).fetchone() is None:
            raise ValueError("candidate_id must belong to this album review")
        for raw_id, state in exceptions.items():
            try:
                inventory_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("track exception ids must be integers") from exc
            if state is not None and state not in LIBRARY_IMPORT_DECISIONS:
                raise ValueError("track exception decisions must be approved, rejected, skipped, or null")
            if db.execute(
                "SELECT 1 FROM album_review_tracks WHERE album_review_id = ? AND inventory_id = ?",
                (album_review_id, inventory_id),
            ).fetchone() is None:
                raise ValueError("track exception must belong to this album review")
            db.execute(
                "UPDATE album_review_tracks SET exception_state = ? WHERE album_review_id = ? AND inventory_id = ?",
                (state, album_review_id, inventory_id),
            )
        assignments, parameters = [], []
        if decision is not None:
            assignments.append("state = ?"); parameters.append(decision)
        if "candidate_id" in values:
            assignments.append("selected_candidate_id = ?"); parameters.append(candidate_id)
        if "duplicate_action" in values:
            assignments.append("duplicate_action = ?"); parameters.append(duplicate_action)
        if assignments or exceptions:
            assignments.extend(["execution_status = 'not-run'", "execution_error = ''",
                                "last_execution_job_id = NULL", "executed_at = NULL"])
            assignments.append("updated_at = ?"); parameters.append(_now())
            db.execute(f"UPDATE album_reviews SET {', '.join(assignments)} WHERE id = ?", (*parameters, album_review_id))
    return next(iter(_album_review_payloads(app, [album_review_id])), None)


def _execute_library_import_album(app: Flask, album_review_id: int, *, dry_run: bool) -> dict | None:
    """Register an approved in-library album and optionally write its chosen tags in place."""
    root = Path(app.config["LIBRARY_PATH"]).resolve()
    with _connect(app.config["APP_DB"]) as db:
        album = db.execute(
            "SELECT * FROM album_reviews WHERE id = ? AND root_path = ?", (album_review_id, str(root))
        ).fetchone()
        if album is None:
            return None
        if not dry_run and album["execution_status"] == "complete":
            previous = db.execute(
                "SELECT summary_json FROM adoption_jobs WHERE id = ?", (album["last_execution_job_id"],)
            ).fetchone()
            result = json.loads(previous["summary_json"]) if previous else {
                "album_review_id": album_review_id, "status": "complete"
            }
            return {**result, "already_complete": True}
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES (?, ?, 'running', ?)",
            ("library_import_preview" if dry_run else "library_import_execute", str(root), _now()),
        ).lastrowid
        if album["state"] != "approved":
            _fail_library_execution(db, album_review_id, job_id, "album review is not approved", dry_run)
            db.commit()
            raise LibraryImportExecutionError(
                "Album review must be approved before execution.", code="review_not_approved", job_id=job_id
            )
        rows = db.execute(
            """SELECT inventory.*, tracks.exception_state
                 FROM album_review_tracks AS tracks
                 JOIN library_inventory AS inventory ON inventory.id = tracks.inventory_id
                WHERE tracks.album_review_id = ?
                ORDER BY inventory.relative_path COLLATE NOCASE, inventory.id""",
            (album_review_id,),
        ).fetchall()
        candidate = None
        if album["selected_candidate_id"] is not None:
            candidate = db.execute(
                "SELECT * FROM metadata_candidates WHERE id = ? AND album_review_id = ?",
                (album["selected_candidate_id"], album_review_id),
            ).fetchone()
            if candidate is None:
                _fail_library_execution(db, album_review_id, job_id, "selected candidate is unavailable", dry_run)
                db.commit()
                raise LibraryImportExecutionError(
                    "The selected metadata candidate is no longer available.",
                    code="candidate_unavailable", job_id=job_id,
                )

    # Keep each row's position in the complete album query. Candidate track
    # details use that position even when an earlier track is excluded.
    approved = [
        (position, row) for position, row in enumerate(rows, 1)
        if (row["exception_state"] or album["state"]) == "approved"
    ]
    if not approved:
        _persist_library_execution_failure(app, album_review_id, job_id, "no tracks are approved", dry_run)
        raise LibraryImportExecutionError("No tracks are approved for execution.", code="no_approved_tracks", job_id=job_id)
    try:
        paths = [_validate_inventory_file(root, row) for _, row in approved]
        provider_data = json.loads(candidate["provider_data_json"]) if candidate else None
        plans = _library_import_plans(app, album, approved, paths, provider_data)
        artwork = _artwork_plan(app, provider_data, plans)
        artwork_bytes = None
        if not dry_run and artwork["status"] == "ready":
            artwork_bytes = app.config["ARTWORK_FETCHER"](provider_data)
        result = {
            "id": job_id,
            "album_review_id": album_review_id,
            "status": "preview" if dry_run else "complete",
            "dry_run": dry_run,
            "selection_mode": "candidate" if candidate else "as-is",
            "files": len(plans),
            "registered": sum(plan["action"] in {"register", "keep-both"} for plan in plans),
            "metadata_updates": sum(bool(plan["changes"]) for plan in plans),
            "duplicate_action": album["duplicate_action"],
            "duplicates": sum(plan["duplicate_of"] is not None for plan in plans),
            "skipped_tracks": len(rows) - len(approved),
            "artwork": artwork,
            "items": [{key: plan[key] for key in ("inventory_id", "path", "beets_item_id", "changes", "action", "duplicate_of", "target_path")} for plan in plans],
        }
        if not dry_run:
            _apply_library_import_plans(app, plans)
            if artwork_bytes is not None:
                result["artwork"] = _apply_artwork(app, plans, artwork_bytes, artwork)
            result["items"] = [
                {key: plan[key] for key in ("inventory_id", "path", "beets_item_id", "changes", "action", "duplicate_of", "target_path")} for plan in plans
            ]
        finished = _now()
        result["finished_at"] = finished
        with _connect(app.config["APP_DB"]) as db:
            db.execute(
                "UPDATE adoption_jobs SET status = 'complete', summary_json = ?, finished_at = ? WHERE id = ?",
                (json.dumps(result, sort_keys=True), finished, job_id),
            )
            if not dry_run:
                db.executemany(
                    "UPDATE library_inventory SET beets_item_id = NULL WHERE root_path = ? AND beets_item_id = ? AND id != ?",
                    [
                        (str(root), plan["duplicate_of"], plan["inventory_id"])
                        for plan in plans if plan["action"] == "replace"
                    ],
                )
                db.executemany(
                    "UPDATE library_inventory SET beets_item_id = ? WHERE id = ?",
                    [(plan["beets_item_id"], plan["inventory_id"]) for plan in plans],
                )
                db.execute(
                    """UPDATE album_reviews SET execution_status = 'complete', execution_error = '',
                              last_execution_job_id = ?, executed_at = ?, updated_at = ? WHERE id = ?""",
                    (job_id, finished, finished, album_review_id),
                )
                db.execute(
                    """UPDATE adoption_reviews SET imported_at = ?, updated_at = ?
                         WHERE inventory_id IN (
                           SELECT inventory_id FROM album_review_tracks WHERE album_review_id = ?
                         )""",
                    (finished, finished, album_review_id),
                )
        return result
    except LibraryImportExecutionError as exc:
        _persist_library_execution_failure(app, album_review_id, job_id, str(exc), dry_run)
        exc.job_id = job_id
        raise
    except Exception as exc:
        message = f"library import execution failed: {exc}"
        _persist_library_execution_failure(app, album_review_id, job_id, message, dry_run)
        raise LibraryImportExecutionError(message, code="execution_failed", status=500, job_id=job_id) from exc


def _fail_library_execution(
    db: sqlite3.Connection, album_review_id: int, job_id: int, message: str, dry_run: bool
) -> None:
    finished = _now()
    db.execute(
        "UPDATE adoption_jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
        (message[:1000], finished, job_id),
    )
    if not dry_run:
        db.execute(
            """UPDATE album_reviews SET execution_status = 'failed', execution_error = ?,
                      last_execution_job_id = ?, updated_at = ? WHERE id = ?""",
            (message[:500], job_id, finished, album_review_id),
        )


def _persist_library_execution_failure(
    app: Flask, album_review_id: int, job_id: int, message: str, dry_run: bool
) -> None:
    with _connect(app.config["APP_DB"]) as db:
        _fail_library_execution(db, album_review_id, job_id, message, dry_run)


def _validate_inventory_file(root: Path, row: sqlite3.Row) -> Path:
    path = root / row["relative_path"]
    try:
        resolved = path.resolve(strict=True)
        stat = resolved.stat()
    except OSError as exc:
        raise LibraryImportExecutionError(
            f"Library file is missing: {row['relative_path']}", code="inventory_stale"
        ) from exc
    if resolved == root or root not in resolved.parents or path.is_symlink() or not resolved.is_file():
        raise LibraryImportExecutionError(
            f"Library file is no longer a safe regular file: {row['relative_path']}", code="inventory_stale"
        )
    expected = (row["size_bytes"], row["mtime_ns"], row["device"], row["inode"])
    current = (stat.st_size, stat.st_mtime_ns, stat.st_dev, stat.st_ino)
    if not row["present"] or current != expected:
        raise LibraryImportExecutionError(
            f"Library file changed after inventory preview: {row['relative_path']}", code="inventory_stale"
        )
    return resolved


def _library_import_plans(
    app: Flask, album: sqlite3.Row, rows: list[tuple[int, sqlite3.Row]], paths: list[Path], provider_data: dict | None
) -> list[dict]:
    library = _library(app)
    managed_items = list(library.items())
    details = {}
    if provider_data:
        query = _album_query(app, album)
        provider_data = _effective_candidate(app, provider_data, query)
        details = {
            detail["position"]: detail for detail in _candidate_match(query, provider_data)["track_details"]
            if detail.get("local") is not None
        }
    plans = []
    for (position, row), path in zip(rows, paths):
        item = library.get_item(row["beets_item_id"]) if row["beets_item_id"] else None
        if item is not None and Path(os.fsdecode(item.path)).resolve() != path:
            raise LibraryImportExecutionError(
                f"Tracked item path changed after inventory preview: {row['relative_path']}", code="inventory_stale"
            )
        if item is None:
            item = Item.from_path(path)
        values = _candidate_item_values(provider_data, details.get(position)) if provider_data else {}
        duplicate = None
        recording_id = values.get("mb_trackid")
        if row["beets_item_id"] is None and recording_id:
            matches = [candidate for candidate in managed_items if str(candidate.get("mb_trackid") or "") == recording_id]
            if len(matches) > 1:
                raise LibraryImportExecutionError(
                    f"Duplicate identity is ambiguous for {row['relative_path']}; {len(matches)} managed tracks share recording ID {recording_id}.",
                    code="duplicate_ambiguous",
                )
            duplicate = matches[0] if matches else None
        duplicate_action = album["duplicate_action"] if duplicate is not None else None
        if duplicate is not None and duplicate_action is None:
            raise LibraryImportExecutionError(
                f"Choose merge, replace, keep both, or skip for duplicate {row['relative_path']} before execution.",
                code="duplicate_decision_required",
            )
        target_path = row["relative_path"]
        artwork_path = path
        action = "update" if row["beets_item_id"] is not None else "register"
        comparison_item = item
        if duplicate is not None:
            if duplicate_action in {"merge", "replace"}:
                duplicate_path = _validate_duplicate_item(app, root=Path(app.config["LIBRARY_PATH"]).resolve(), item=duplicate)
                target_path = duplicate_path.relative_to(Path(app.config["LIBRARY_PATH"]).resolve()).as_posix()
                artwork_path = duplicate_path
            if duplicate_action == "skip":
                action = "skip"
                values = {}
            elif duplicate_action == "keep-both":
                action = "keep-both"
            elif duplicate_action == "replace":
                action = "replace"
                target_path = row["relative_path"]
                artwork_path = path
            elif duplicate_action == "merge":
                action = "merge"
                comparison_item = duplicate
        changes = {}
        for field, value in list(values.items()):
            current = _item_genre(comparison_item) if field == "genre" else comparison_item.get(field)
            if field == "genre" and _genre_presentation_key(current) == _genre_presentation_key(value):
                # Preserve the exact existing spelling and avoid both tag and
                # database writes for presentation-only differences.
                del values[field]
                continue
            if current != value:
                changes[field] = {"from": current, "to": value}
        plans.append({
            "inventory_id": row["id"], "path": row["relative_path"], "absolute_path": path,
            "beets_item_id": row["beets_item_id"], "item": item, "values": values, "changes": changes,
            "action": action, "duplicate_item": duplicate,
            "duplicate_of": duplicate.id if duplicate is not None else None, "target_path": target_path,
            "artwork_path": artwork_path,
        })
    return plans


def _validate_duplicate_item(app: Flask, *, root: Path, item: Item) -> Path:
    """Require a unique, current inventory snapshot before touching a managed duplicate."""
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            "SELECT * FROM library_inventory WHERE root_path = ? AND beets_item_id = ? AND present = 1",
            (str(root), item.id),
        ).fetchall()
    if len(rows) != 1:
        raise LibraryImportExecutionError(
            f"Managed duplicate identity {item.id} is not uniquely represented in the current inventory.",
            code="duplicate_ambiguous",
        )
    path = _validate_inventory_file(root, rows[0])
    if Path(os.fsdecode(item.path)).resolve() != path:
        raise LibraryImportExecutionError(
            f"Managed duplicate path changed after inventory preview: {rows[0]['relative_path']}",
            code="inventory_stale",
        )
    return path


def _candidate_item_values(candidate: dict, detail: dict | None) -> dict:
    values = {}
    artist = _normalize_metadata(candidate.get("artist"))
    album = _normalize_metadata(candidate.get("album"))
    if artist:
        values["albumartist"] = artist
        values["artist"] = _normalize_metadata(detail.get("proposed_artist")) if detail else artist
        if not values["artist"]:
            values["artist"] = artist
    if album:
        values["album"] = album
    genre = _candidate_genre(candidate)
    if genre:
        values["genre"] = genre
    date = str(candidate.get("date") or candidate.get("year") or "")
    date_match = re.fullmatch(r"(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?", date)
    if date_match:
        values["year"] = int(date_match.group(1))
        values["month"] = int(date_match.group(2) or 0)
        values["day"] = int(date_match.group(3) or 0)
    if candidate.get("provider_id"):
        values["mb_albumid"] = str(candidate["provider_id"])
    if candidate.get("release_group_id"):
        values["mb_releasegroupid"] = str(candidate["release_group_id"])
    if candidate.get("release_type"):
        values["albumtype"] = str(candidate["release_type"])
    if candidate.get("country"):
        values["country"] = str(candidate["country"])
    media = candidate.get("media") if isinstance(candidate.get("media"), list) else []
    if media and isinstance(media[0], dict) and media[0].get("format"):
        values["media"] = str(media[0]["format"])
    try:
        if candidate.get("track_count") is not None:
            values["tracktotal"] = int(candidate["track_count"])
        if media:
            values["disctotal"] = len(media)
    except (TypeError, ValueError):
        pass
    if detail and detail.get("proposed") is not None:
        values["title"] = detail["proposed"]
        proposed_position = detail.get("proposed_position")
        if proposed_position:
            values["disc"], values["track"] = (int(proposed_position[0]), int(proposed_position[1]))
        if detail.get("recording_id"):
            values["mb_trackid"] = str(detail["recording_id"])
    return values


def _artwork_plan(app: Flask, candidate: dict | None, plans: list[dict]) -> dict:
    destinations = {"sidecar": bool(app.config["ART_SIDECAR"]), "embedded": bool(app.config["ART_EMBED"])}
    base = {"destinations": destinations, "replace_existing": bool(app.config["ART_REPLACE"])}
    if not app.config["FETCH_ART"]:
        return {**base, "status": "disabled", "message": "Artwork fetching is disabled."}
    if not any(destinations.values()):
        return {**base, "status": "disabled", "message": "No artwork destination is enabled."}
    artwork = candidate.get("artwork") if isinstance(candidate, dict) else None
    if not isinstance(artwork, dict) or not artwork.get("available"):
        return {**base, "status": "unavailable", "message": "No candidate artwork is available; existing artwork will be preserved."}
    entity, mbid = artwork.get("entity"), str(artwork.get("mbid") or "")
    if entity not in {"release", "release-group"} or not MUSICBRAINZ_ID_PATTERN.fullmatch(mbid):
        raise LibraryImportExecutionError("Candidate artwork identity is invalid.", code="artwork_identity_invalid", status=400)
    album_folders = sorted({str(plan["artwork_path"].parent) for plan in plans if plan["action"] != "skip"})
    return {
        **base, "status": "ready", "source": "cover-art-archive", "entity": entity, "mbid": mbid,
        "album_folders": album_folders, "message": "Candidate artwork is ready for approved import.",
    }


class _SafeArtworkRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host in TRUSTED_ARTWORK_HOSTS or host.endswith(".archive.org")):
            raise urllib.error.URLError("Cover Art Archive redirected to an untrusted host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_and_normalize_artwork(candidate: dict) -> bytes:
    artwork = candidate["artwork"]
    url = f"https://coverartarchive.org/{artwork['entity']}/{artwork['mbid']}/front"
    request_object = urllib.request.Request(
        url, headers={"Accept": "image/jpeg,image/png,image/webp,image/gif", "User-Agent": "Cratekeep/0.1 (https://github.com/kylejschultz/cratekeep)"}
    )
    try:
        with urllib.request.build_opener(_SafeArtworkRedirect()).open(request_object, timeout=15) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_ARTWORK_BYTES:
                raise ValueError("artwork exceeds 10 MB limit")
            payload = response.read(MAX_ARTWORK_BYTES + 1)
            if len(payload) > MAX_ARTWORK_BYTES:
                raise ValueError("artwork exceeds 10 MB limit")
        with PillowImage.open(BytesIO(payload)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise ValueError(f"unsupported artwork format {image.format or 'unknown'}")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > 40_000_000:
                raise ValueError("artwork dimensions are invalid or too large")
            image.seek(0)
            normalized = image.convert("RGB")
            output = BytesIO()
            normalized.save(output, format="JPEG", quality=90, optimize=False, progressive=False)
            return output.getvalue()
    except (
        OSError, ValueError, TypeError, urllib.error.URLError,
        UnidentifiedImageError, PillowImage.DecompressionBombError,
    ) as exc:
        raise LibraryImportExecutionError(
            f"Artwork retrieval failed before artwork was changed: {exc}", code="artwork_fetch_failed", status=502
        ) from exc


def _apply_artwork(app: Flask, plans: list[dict], jpeg: bytes, summary: dict) -> dict:
    replace = bool(app.config["ART_REPLACE"])
    sidecars = {"written": [], "preserved": []}
    embedded = {"written": [], "preserved": []}
    if app.config["ART_SIDECAR"]:
        for folder in sorted({plan["artwork_path"].parent for plan in plans if plan["action"] != "skip"}, key=str):
            destination = folder / "cover.jpg"
            if destination.exists() and not replace:
                sidecars["preserved"].append(str(destination))
                continue
            temporary = folder / f".cover.{secrets.token_hex(8)}.tmp"
            try:
                temporary.write_bytes(jpeg)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            sidecars["written"].append(str(destination))
    if app.config["ART_EMBED"]:
        for plan in plans:
            if plan["action"] == "skip":
                continue
            media = MediaFile(str(plan["artwork_path"]))
            if media.images and not replace:
                embedded["preserved"].append(plan["target_path"])
                continue
            media.images = [MediaImage(jpeg, desc="Front cover", type=ImageType.front)]
            media.save()
            embedded["written"].append(plan["target_path"])
    return {
        **summary, "status": "applied", "sidecars": sidecars, "embedded": embedded,
        "sha256": hashlib.sha256(jpeg).hexdigest(),
        "message": "Artwork applied; pre-existing artwork was preserved unless replacement was enabled.",
    }


def _apply_library_import_plans(app: Flask, plans: list[dict]) -> None:
    library = _library(app)
    for plan in plans:
        if plan["action"] == "skip":
            continue
        if plan["action"] == "merge":
            item = plan["duplicate_item"]
            for field, value in plan["values"].items():
                _set_item_value(item, field, value)
            if plan["changes"]:
                item.write()
                item.store()
            continue
        item = plan["item"]
        for field, value in plan["values"].items():
            _set_item_value(item, field, value)
        if plan["action"] == "replace":
            replacement = plan["duplicate_item"]
            for field in ("title", "artist", "album", "albumartist", "genre", "year", "month", "day", "track", "disc"):
                current = _item_genre(item) if field == "genre" else item.get(field)
                _set_item_value(replacement, field, current)
            for field, value in plan["values"].items():
                _set_item_value(replacement, field, value)
            replacement.path = os.fsencode(plan["absolute_path"])
            replacement.write()
            replacement.store()
            if Path(os.fsdecode(replacement.path)).resolve() != plan["absolute_path"]:
                raise OSError(f"beets changed the replacement path for {plan['path']}")
            plan["beets_item_id"] = replacement.id
            continue
        if plan["changes"]:
            item.write()
        if plan["beets_item_id"] is None:
            # Library.add only registers the existing path in beets. File
            # copying and moving are importer operations, not Library.add
            # options; the invariant below verifies registration stayed in place.
            library.add(item)
        else:
            item.store()
        if Path(os.fsdecode(item.path)).resolve() != plan["absolute_path"]:
            raise OSError(f"beets changed the file path for {plan['path']}")
        plan["beets_item_id"] = item.id


def _serialize_library_import_review(row: sqlite3.Row, item=None) -> dict:
    candidates = json.loads(row["candidates_json"])
    path = Path(row["relative_path"])
    parts = path.parts
    fallback_artist = parts[0] if len(parts) >= 3 else "Unknown artist"
    fallback_album = parts[1] if len(parts) >= 3 else (parts[0] if len(parts) == 2 else "Unknown album")
    return {
        "id": row["id"],
        "path": row["relative_path"],
        "artist": (item.get("albumartist") or item.get("artist") or fallback_artist) if item else fallback_artist,
        "album": item.get("album") if item and item.get("album") else fallback_album,
        "title": item.get("title") if item and item.get("title") else path.stem,
        "status": "tracked" if row["beets_item_id"] is not None else "needs-review",
        "decision": row["state"],
        "candidate": candidates[0] if candidates else None,
        "candidates": candidates,
        "updated_at": row["updated_at"],
    }


def _group_library_import_review_items(items: list[dict]) -> list[dict]:
    root = {"name": "Library root", "path": "", "folders": {}, "albums": {}}
    for item in items:
        parts = Path(item["path"]).parts[:-1]
        folder = root
        path_parts = []
        for part in parts:
            path_parts.append(part)
            folder = folder["folders"].setdefault(
                part,
                {"name": part, "path": "/".join(path_parts), "folders": {}, "albums": {}},
            )
        album_key = (item["artist"], item["album"])
        album = folder["albums"].setdefault(
            album_key,
            {"artist": item["artist"], "album": item["album"], "songs": []},
        )
        album["songs"].append(item)

    def serialize(folder: dict) -> dict:
        return {
            "name": folder["name"],
            "path": folder["path"],
            "folders": [
                serialize(child)
                for _, child in sorted(folder["folders"].items(), key=lambda entry: entry[0].casefold())
            ],
            "albums": [
                album for _, album in sorted(
                    folder["albums"].items(), key=lambda entry: (entry[0][0].casefold(), entry[0][1].casefold())
                )
            ],
        }

    grouped = [
        serialize(folder)
        for _, folder in sorted(root["folders"].items(), key=lambda entry: entry[0].casefold())
    ]
    if root["albums"]:
        grouped.insert(0, serialize(root))
    return grouped


def _serialize_item(item) -> dict:
    result = {key: item.get(key) for key in ("id", "title", "artist", "album", "albumartist", "genre", "year", "track", "disc")}
    result["genre"] = _item_genre(item)
    path = item.get("path")
    result["path"] = os.fsdecode(path) if path else ""
    result["bytes"] = Path(result["path"]).stat().st_size if result["path"] and Path(result["path"]).is_file() else None
    return result


def _inbox_candidates(app: Flask) -> list[dict]:
    root = Path(app.config["INBOX_PATH"]).resolve()
    candidates = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if entry.name.startswith(".") or (entry.is_file() and entry.suffix.lower() not in AUDIO_EXTENSIONS):
            continue
        files = _audio_files(entry)
        if files:
            candidates.append({"path": entry.name, "files": len(files), "bytes": sum(p.stat().st_size for p in files)})
    return candidates


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0 MB"
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} GB"
    size_mb = value / 1024**2
    return f"{size_mb:.1f} MB" if size_mb >= 0.1 else "<0.1 MB"


def _audio_files(path: Path) -> list[Path]:
    iterator = path.rglob("*") if path.is_dir() else [path]
    return sorted((p.resolve() for p in iterator if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS), key=str)


def _safe_inbox_path(app: Flask, relative: str) -> Path:
    if not relative:
        abort(400, "path is required")
    root = Path(app.config["INBOX_PATH"]).resolve()
    candidate = (root / relative).resolve()
    if candidate == root or root not in candidate.parents or not candidate.exists():
        abort(400, "path must identify an existing selection inside the inbox")
    return candidate


def _request_value(name: str) -> str:
    values = request.get_json(silent=True) or request.form
    return str(values.get(name, ""))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
