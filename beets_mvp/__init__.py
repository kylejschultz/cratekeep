from __future__ import annotations

import json
import os
import secrets
import sqlite3
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml
from beets import config as beets_config
from beets.library import Library
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".ape", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wv"}
EDITABLE_FIELDS = {"title", "artist", "album", "albumartist", "genre", "year", "track", "disc"}
SETTING_KEYS = ("inbox_path", "library_path", "navidrome_rescan_url", "navidrome_token", "fetch_art")
MAX_BEETS_CONFIG_BYTES = 128 * 1024


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.add_template_filter(_format_bytes, "format_bytes")
    app.config.from_mapping(
        BUILD_SHA=os.getenv("BUILD_SHA", os.getenv("GITHUB_SHA", "unknown")),
        SECRET_KEY=os.getenv("SECRET_KEY", ""),
        STATE_PATH=os.getenv("STATE_PATH", str(Path.cwd() / "data/config")),
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
