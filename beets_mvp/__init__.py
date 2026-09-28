from __future__ import annotations

import json
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
from beets.library import Library
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

from .musicbrainz import ProviderError, search_releases
from .matching import score_release

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".ape", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wv"}
EDITABLE_FIELDS = {"title", "artist", "album", "albumartist", "genre", "year", "track", "disc"}
SETTING_KEYS = ("inbox_path", "library_path", "navidrome_rescan_url", "navidrome_token", "fetch_art")
MAX_BEETS_CONFIG_BYTES = 128 * 1024
MAX_INVENTORY_SAMPLE = 50
MAX_LIBRARY_IMPORT_ARTISTS = 50
MAX_CANDIDATE_ALBUMS = 25
MAX_CANDIDATES_PER_ALBUM = 5
LIBRARY_IMPORT_DECISIONS = {"approved", "rejected", "skipped"}
MUSICBRAINZ_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.add_template_filter(_format_bytes, "format_bytes")
    app.config.from_mapping(
        BUILD_SHA=os.getenv("BUILD_SHA", os.getenv("GITHUB_SHA", "unknown")),
        SECRET_KEY=os.getenv("SECRET_KEY", ""),
        STATE_PATH=os.getenv("STATE_PATH", str(Path.cwd() / "data/config")),
        MUSICBRAINZ_PROVIDER=search_releases,
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
        if not 1 <= limit <= MAX_LIBRARY_IMPORT_ARTISTS:
            return jsonify(error=f"limit must be between 1 and {MAX_LIBRARY_IMPORT_ARTISTS}"), 400
        if offset < 0:
            return jsonify(error="offset must be zero or greater"), 400
        album_ids, artist_count, has_more, total_artists = _library_import_review_artist_page(app, limit, offset)
        items = _library_import_review_items(app, album_ids)
        return jsonify(items=items, groups=_group_library_import_review_items(items),
                       albums=_album_review_payloads(app, album_ids), limit=limit, offset=offset,
                       artist_count=artist_count, total_artists=total_artists,
                       album_count=len(album_ids), has_more=has_more,
                       next_offset=(offset + artist_count) if has_more else None)

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
            item[field] = value
        item.store()
        try:
            item.write()
        except Exception as exc:
            abort(500, f"database updated but tag write failed: {exc}")
        if request.is_json:
            return jsonify(_serialize_item(item))
        flash("Metadata and file tags updated.")
        return redirect(url_for("index"))

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


def _build_beets_config(app: Flask, settings: dict[str, str], content: str, fetch_art: bool) -> str:
    if len(content.encode("utf-8")) > MAX_BEETS_CONFIG_BYTES:
        raise ValueError("Beets configuration must be smaller than 128 KB.")
    try:
        document = yaml.safe_load(content) if content.strip() else {}
    except yaml.YAMLError as exc:
        detail = str(exc).splitlines()[0]
        raise ValueError(f"Beets configuration is not valid YAML: {detail}") from exc
    if not isinstance(document, dict):
        raise ValueError("Beets configuration must be a YAML mapping.")

    import_config = document.get("import", {})
    if import_config is None:
        import_config = {}
    if not isinstance(import_config, dict):
        raise ValueError("The beets import section must be a YAML mapping.")
    import_config.update(move=True, write=True, autotag=False, resume=False)

    plugins = document.get("plugins", [])
    if isinstance(plugins, str):
        plugins = plugins.split()
    if plugins is None:
        plugins = []
    if not isinstance(plugins, list) or any(not isinstance(plugin, str) for plugin in plugins):
        raise ValueError("The beets plugins setting must be a list or space-separated string.")
    plugins = list(dict.fromkeys(plugin for plugin in plugins if plugin != "fetchart"))
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
            created_at TEXT NOT NULL,
            UNIQUE(album_review_id, provider, provider_id)
        )""")


def _load_settings(path: str) -> dict[str, str]:
    with _connect(path) as db:
        rows = db.execute("SELECT key, value FROM app_settings").fetchall()
    return {row["key"]: row["value"] for row in rows if row["key"] in SETTING_KEYS}


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
    )
    app.config["SETUP_COMPLETE"] = bool(app.config["INBOX_PATH"] and app.config["LIBRARY_PATH"])
    if app.config["SETUP_COMPLETE"]:
        for key in ("INBOX_PATH", "LIBRARY_PATH"):
            Path(app.config[key]).mkdir(parents=True, exist_ok=True)
        _write_beets_config(app)
        beets_config.set_file(app.config["BEETS_CONFIG"])
        beets_config.read(user=False, defaults=True)


def _settings_response(app: Flask, first_run: bool):
    current = _load_settings(app.config["APP_DB"])
    config_path = Path(app.config["BEETS_CONFIG"])
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
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
        }
        submitted_config = request.form.get("beets_config", config_text)
        rendered_config = None
        if not first_run:
            try:
                rendered_config = _build_beets_config(app, values, submitted_config, values["fetch_art"] == "1")
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

    defaults = _default_paths()
    return render_template(
        "settings.html",
        first_run=first_run,
        settings=current,
        default_inbox=defaults["inbox_path"],
        default_library=defaults["library_path"],
        has_token=bool(current.get("navidrome_token")),
        beets_config=config_text,
        build_sha=app.config["BUILD_SHA"],
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


def _library_import_review_artist_page(app: Flask, limit: int, offset: int) -> tuple[list[int], int, bool, int]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        artists = db.execute(
            """SELECT albums.artist_key FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                GROUP BY albums.artist_key
                ORDER BY albums.artist_key
                LIMIT ? OFFSET ?""",
            (root, limit + 1, offset),
        ).fetchall()
        total = db.execute(
            """SELECT COUNT(DISTINCT albums.artist_key) FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)""",
            (root,),
        ).fetchone()[0]
        page_artist_keys = [row["artist_key"] for row in artists[:limit]]
        if not page_artist_keys:
            return [], 0, False, total
        placeholders = ", ".join("?" for _ in page_artist_keys)
        albums = db.execute(
            f"""SELECT albums.id FROM album_reviews AS albums
                 WHERE albums.root_path = ? AND albums.artist_key IN ({placeholders})
                   AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                 ORDER BY albums.artist_key, albums.album_key, albums.id""",
            (root, *page_artist_keys),
        ).fetchall()
    return [row["id"] for row in albums], len(page_artist_keys), len(artists) > limit, total


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


def _metadata_key(value: object) -> str:
    return _normalize_metadata(value).casefold()


def _normalize_track_title(value: object) -> str:
    title = _normalize_metadata(value)
    return re.sub(r"^\s*(?:\d+[.-]?\s*[-–—.]?\s*)", "", title) or title


def _all_library_import_review_items(app: Flask) -> list[dict]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            """SELECT inventory.id, inventory.relative_path, inventory.beets_item_id,
                      reviews.state, reviews.candidates_json, reviews.updated_at
                 FROM library_inventory AS inventory
                 JOIN adoption_reviews AS reviews ON reviews.inventory_id = inventory.id
                WHERE inventory.root_path = ? AND inventory.present = 1
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
    groups: dict[tuple[str, str], dict] = {}
    for item in _all_library_import_review_items(app):
        artist = _normalize_metadata(item["artist"]) or "Unknown artist"
        album = _normalize_metadata(item["album"]) or "Unknown album"
        group = groups.setdefault((_metadata_key(artist), _metadata_key(album)), {"artist": artist, "album": album, "items": []})
        group["items"].append(item)
    now = _now()
    with _connect(app.config["APP_DB"]) as db:
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
            "DELETE FROM album_review_tracks WHERE album_review_id IN (SELECT id FROM album_reviews WHERE root_path = ?)",
            (root,),
        )
        for (artist_key, album_key), group in groups.items():
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


def _candidate_album_rows(app: Flask, limit: int) -> list[sqlite3.Row]:
    root = str(Path(app.config["LIBRARY_PATH"]).resolve())
    with _connect(app.config["APP_DB"]) as db:
        return db.execute(
            """SELECT albums.id, albums.artist, albums.album
                 FROM album_reviews AS albums
                WHERE albums.root_path = ?
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                ORDER BY albums.artist_key, albums.album_key, albums.id LIMIT ?""",
            (root, limit),
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
    for row in rows:
        item = library.get_item(row["beets_item_id"]) if row["beets_item_id"] else None
        title = item.get("title") if item and item.get("title") else Path(row["relative_path"]).stem
        track = {"title": _normalize_track_title(title), "path": row["relative_path"]}
        if item:
            for source, target in (("length", "duration"), ("disc", "disc"), ("track", "track"),
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
        tracks.append(track)
    query = {"artist": _normalize_metadata(album["artist"]), "album": _normalize_metadata(album["album"]), "tracks": tracks}
    if years:
        query["year"] = years[0]
        query["date"] = dates[0]
    if release_ids and len(set(release_ids)) == 1:
        query["release_mbid"] = release_ids[0]
    return query


def _candidate_match(query: dict, candidate: dict, *, exact_mbid: bool = False) -> dict:
    exact = exact_mbid or bool(query.get("release_mbid") and
                               str(candidate.get("provider_id", "")).lower() == query["release_mbid"])
    retrieval = candidate.get("retrieval") if isinstance(candidate.get("retrieval"), dict) else {}
    exact = exact or retrieval.get("source") == "release-id"
    return score_release(query, candidate, exact_mbid=exact)


def _candidate_confidence(query: dict, candidate: dict, *, exact_mbid: bool = False) -> float:
    return _candidate_match(query, candidate, exact_mbid=exact_mbid)["confidence"]


def _candidate_diff(query: dict, candidate: dict) -> dict:
    proposed = {}
    for key in ("artist", "album"):
        value = _normalize_metadata(candidate.get(key))
        if value and _metadata_key(value) != _metadata_key(query[key]):
            proposed[key] = {"from": query[key], "to": value}
    count = candidate.get("track_count")
    if isinstance(count, int) and count != len(query["tracks"]):
        proposed["track_count"] = {"from": len(query["tracks"]), "to": count}
    candidate_tracks = candidate.get("tracks")
    if isinstance(candidate_tracks, list):
        normalized_tracks = [_normalize_track_title(track.get("title") if isinstance(track, dict) else track)
                             for track in candidate_tracks]
        local_tracks = [_normalize_track_title(track.get("title") if isinstance(track, dict) else track)
                        for track in query["tracks"]]
        if [_metadata_key(track) for track in normalized_tracks] != [_metadata_key(track) for track in local_tracks]:
            proposed["tracks"] = {"from": local_tracks, "to": normalized_tracks}
    return proposed


def _candidate_reasons(query: dict, candidate: dict) -> list[str]:
    return _candidate_match(query, candidate)["reasons"]


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
            1, _candidate_confidence(query, candidate, exact_mbid=True), _normalize_metadata(candidate.get("artist")),
            _normalize_metadata(candidate.get("album")), str(candidate.get("year")) if candidate.get("year") else None,
            json.dumps(_candidate_diff(query, candidate), sort_keys=True), json.dumps(candidate, sort_keys=True), now,
        )
        if existing:
            candidate_id = existing["id"]
            db.execute(
                """UPDATE metadata_candidates SET rank = ?, confidence = ?, artist = ?, album = ?, year = ?,
                          proposed_diff_json = ?, provider_data_json = ?, created_at = ? WHERE id = ?""",
                (*values, candidate_id),
            )
        else:
            candidate_id = db.execute(
                """INSERT INTO metadata_candidates(
                       album_review_id, provider, provider_id, rank, confidence, artist, album, year,
                       proposed_diff_json, provider_data_json, created_at
                   ) VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (album_review_id, musicbrainz_id, *values),
            ).lastrowid
        db.execute(
            """UPDATE album_reviews SET selected_candidate_id = ?, candidate_status = 'complete',
                      candidate_error = '', updated_at = ? WHERE id = ?""",
            (candidate_id, now, album_review_id),
        )
    return next(iter(_album_review_payloads(app, [album_review_id])), None)


def _generate_musicbrainz_candidates(app: Flask, limit: int) -> dict:
    provider = app.config["MUSICBRAINZ_PROVIDER"]
    albums = _candidate_album_rows(app, limit)
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
            candidates.sort(key=lambda candidate: (-_candidate_confidence(query, candidate), str(candidate["provider_id"])))
            with _connect(app.config["APP_DB"]) as db:
                db.execute("UPDATE album_reviews SET selected_candidate_id = NULL WHERE id = ?", (album["id"],))
                db.execute("DELETE FROM metadata_candidates WHERE album_review_id = ?", (album["id"],))
                for rank, candidate in enumerate(candidates, 1):
                    db.execute(
                        """INSERT INTO metadata_candidates(
                               album_review_id, provider, provider_id, rank, confidence, artist, album,
                               year, proposed_diff_json, provider_data_json, created_at
                           ) VALUES (?, 'musicbrainz', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (album["id"], str(candidate["provider_id"]), rank, _candidate_confidence(query, candidate),
                         _normalize_metadata(candidate.get("artist")), _normalize_metadata(candidate.get("album")),
                         str(candidate.get("year")) if candidate.get("year") else None,
                         json.dumps(_candidate_diff(query, candidate), sort_keys=True),
                         json.dumps(candidate, sort_keys=True), _now()),
                    )
                db.execute(
                    "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
                    (_now(), album["id"]),
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


def _persisted_artwork_source(provider_data: dict) -> str | None:
    """Expose only an already-persisted, browser-renderable artwork source."""
    values = [provider_data.get(key) for key in ("artwork_url", "image_url", "cover_url")]
    cover_art = provider_data.get("cover_art")
    if isinstance(cover_art, dict):
        values.extend(cover_art.get(key) for key in ("url", "image", "data"))
    for value in values:
        if not isinstance(value, str):
            continue
        source = value.strip()
        lowered = source.lower()
        if lowered.startswith(("https://", "http://")) or lowered.startswith(
            ("data:image/png;", "data:image/jpeg;", "data:image/gif;", "data:image/webp;", "data:image/avif;")
        ):
            return source
    return None


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
                  AND EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id = albums.id)
                  {album_filter}
                ORDER BY albums.artist_key, albums.album_key, albums.id""", parameters
        ).fetchall()
        payloads = []
        for row in rows:
            query = _album_query(app, row)
            tracks = db.execute(
                """SELECT inventory.id, inventory.relative_path, tracks.exception_state
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
                match = _candidate_match(query, provider_data)
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
                    "artist": candidate["artist"], "album": candidate["album"], "year": candidate["year"],
                    "date": provider_data.get("date"), "release_group_id": provider_data.get("release_group_id"),
                    "artwork_url": _persisted_artwork_source(provider_data),
                    "media": provider_data.get("media", []), "recordings": provider_data.get("tracks", []),
                    "retrieval": provider_data.get("retrieval", {}),
                    "proposed_diff": json.loads(candidate["proposed_diff_json"]),
                    "track_details": match["track_details"],
                })
            selected_id = row["selected_candidate_id"]
            proposed_match = next((candidate for candidate in serialized_candidates if candidate["id"] == selected_id), None)
            if proposed_match is None and serialized_candidates:
                proposed_match = serialized_candidates[0]
            highest_confidence = max((candidate["confidence"] for candidate in serialized_candidates), default=None)
            payloads.append({
                "id": row["id"], "artist": row["artist"], "album": row["album"], "decision": row["state"],
                "selected_candidate_id": selected_id, "proposed_match": proposed_match,
                "highest_confidence": highest_confidence,
                "candidate_status": row["candidate_status"],
                "candidate_error": row["candidate_error"],
                "tracks": [{"id": track["id"], "path": track["relative_path"],
                            "title": query["tracks"][index]["title"],
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
    allowed = {"decision", "candidate_id", "track_exceptions"}
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unsupported fields: {', '.join(sorted(unknown))}")
    decision = values.get("decision")
    if decision is not None and decision not in LIBRARY_IMPORT_DECISIONS:
        raise ValueError("decision must be approved, rejected, or skipped")
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
        if assignments or exceptions:
            assignments.append("updated_at = ?"); parameters.append(_now())
            db.execute(f"UPDATE album_reviews SET {', '.join(assignments)} WHERE id = ?", (*parameters, album_review_id))
    return next(iter(_album_review_payloads(app, [album_review_id])), None)


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
