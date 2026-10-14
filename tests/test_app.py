import hashlib
import json
import copy
import os
import re
from io import BytesIO
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.error
import wave
from pathlib import Path

import pytest
import yaml
from beets.dbcore import types as beets_types
from beets.library import Item, Library
from mediafile import Image as MediaImage, ImageType, MediaFile
from PIL import Image as PillowImage

from beets_mvp import (
    LibraryImportExecutionError, _album_bulk_readiness, _candidate_diff, _fetch_and_normalize_artwork, _format_bytes,
    _format_genre, _genre_presentation_key, _group_library_import_review_items, _init_db,
    _effective_candidate, _item_genre, _set_item_value, _SafeArtworkRedirect, create_app,
)
from beets_mvp.musicbrainz import (
    ProviderError, _artist_credit_text, _cover_art_evidence, _genre_evidence,
    _normalize_release, search_releases,
)
from beets_mvp.matching import score_release

def make_app(tmp_path: Path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    response = app.test_client().post("/setup", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(tmp_path / "library"),
    })
    assert response.status_code == 302
    return app


def write_wav(path: Path, seconds: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * seconds)


def jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    PillowImage.new("RGB", (8, 8), color).save(output, format="JPEG", quality=90)
    return output.getvalue()

def test_health_and_empty_lists(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    assert client.get("/healthz").json == {"status": "ok"}
    assert client.get("/api/inbox").json == []
    assert client.get("/api/items").json == []
    assert app.config["APP_DB"] == str(tmp_path / "config" / "app.db")
    assert app.config["BEETS_DB"] == str(tmp_path / "config" / "library.db")
    assert app.config["BEETS_CONFIG"] == str(tmp_path / "config" / "config.yaml")


def test_container_smoke_forwards_every_required_runtime_option():
    repo_root = Path(__file__).resolve().parent.parent
    runtime = (repo_root / "tests/runtime_smoke.py").read_text(encoding="utf-8")
    wrapper = (repo_root / "tests/container_smoke.sh").read_text(encoding="utf-8")
    required = set(re.findall(
        r'parser\.add_argument\(\s*["\'](?P<option>--[a-z0-9-]+)["\'][^)]*required=True',
        runtime,
    ))
    invocation = wrapper.split('"$python_bin" tests/runtime_smoke.py', 1)[1]

    assert required == {
        "--fixture-root", "--state-path", "--evidence-dir",
        "--source-commit", "--source-tree", "--source-set-definition",
    }
    assert required <= set(re.findall(r"--[a-z0-9-]+", invocation))
    assert '--source-commit "$source_commit"' in invocation
    assert '--source-tree "$source_tree"' in invocation
    assert '--source-set-definition "$source_set_definition"' in invocation
    assert "rev-parse --verify 'HEAD^{tree}'" in wrapper


def test_index_renders_app_shell_navigation_and_sections(tmp_path):
    page = make_app(tmp_path).test_client().get("/")

    assert page.status_code == 200
    assert b'id="sidebar"' in page.data
    assert b'aria-label="Primary navigation"' in page.data
    assert b'href="/"' in page.data
    assert b'href="/inbox"' in page.data
    assert b'href="/library"' in page.data
    assert b'href="/settings"' in page.data
    assert b'id="overview-title"' in page.data
    assert b'id="inbox-title"' in page.data
    assert b'id="recent-title"' in page.data
    assert b'No managed albums yet' in page.data
    assert b'src="/static/cratekeep-logo.png"' in page.data
    assert b'id="theme-toggle"' in page.data
    assert b'app-shell.css' in page.data

def test_index_is_bounded_status_and_recent_albums_without_edit_forms(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr("beets_mvp._inbox_review_summaries", lambda current_app: [{
        "id":"source", "name":"Downloads", "path":str(tmp_path / "inbox"),
        "albums":1, "items":1, "bytes":1024, "matching":0, "errors":0,
        "ready":0, "scan_error":"", "scanned_at":"2026-10-10",
    }])
    monkeypatch.setattr("beets_mvp._library_page_data", lambda *args, **kwargs: {
        "recent_albums":[{"id":7,"album":"Example album","albumartist":"Example artist",
                          "year":2026,"track_count":2,"artwork_url":None}],
        "summary":{"tracks":2,"albums":1,"artists":1,"bytes":1024},
    })

    page = app.test_client().get("/")

    assert b"Downloads" in page.data and b"Example album" in page.data
    assert b"Open Inbox" in page.data and b"Open Library" in page.data
    assert b'action="/api/items/' not in page.data
    assert b"Save changes" not in page.data


def test_overview_uses_persisted_inbox_totals_when_source_becomes_unavailable(tmp_path):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Once Readable" / "01 Song.wav")
    client = app.test_client()
    assert len(client.get("/api/inbox/reviews").json["albums"]) == 1
    (tmp_path / "inbox").rename(tmp_path / "inbox-unavailable")
    page = client.get("/").get_data(as_text=True)
    assert "Once Readable" not in page
    assert "1 file" in page
    assert "one or more source totals are unavailable" not in page

def test_format_bytes_handles_megabytes_zero_and_unknown():
    assert _format_bytes(5 * 1024**2) == "5.0 MB"
    assert _format_bytes(4096) == "<0.1 MB"
    assert _format_bytes(0) == "0 MB"
    assert _format_bytes(None) == "—"

def test_default_state_path_uses_config_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STATE_PATH", raising=False)
    monkeypatch.delenv("BEETS_CONFIG", raising=False)

    app = create_app({"TESTING": True})

    assert app.config["STATE_PATH"] == str(tmp_path / "data" / "config")
    assert app.config["APP_DB"] == str(tmp_path / "data" / "config" / "app.db")
    assert app.config["BEETS_DB"] == str(tmp_path / "data" / "config" / "library.db")
    assert app.config["BEETS_CONFIG"] == str(tmp_path / "data" / "config" / "config.yaml")

def test_secret_key_is_generated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    state_path = tmp_path / "config"
    app = create_app({"TESTING": True, "STATE_PATH": str(state_path)})
    key = app.config["SECRET_KEY"]

    assert len(key) >= 64
    assert (state_path / "secret.key").read_text().strip() == key

    restarted = create_app({"TESTING": True, "STATE_PATH": str(state_path)})
    assert restarted.config["SECRET_KEY"] == key

def test_explicit_secret_key_overrides_generated_key(tmp_path):
    app = create_app({"TESTING": True, "STATE_PATH": str(tmp_path / "config"), "SECRET_KEY": "explicit"})

    assert app.config["SECRET_KEY"] == "explicit"

def test_first_run_requires_and_persists_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("INBOX_PATH", "/ignored/inbox")
    monkeypatch.setenv("LIBRARY_PATH", "/ignored/library")
    monkeypatch.setenv("NAVIDROME_RESCAN_URL", "https://ignored.invalid/scan")
    state_path = tmp_path / "config"
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(state_path), "BROWSE_ROOTS": [str(tmp_path)]})
    client = app.test_client()

    assert client.get("/").headers["Location"].endswith("/setup")
    unavailable = client.get("/api/inbox")
    assert unavailable.status_code == 503
    assert unavailable.json["setup"] == "/setup"

    inbox = tmp_path / "chosen-inbox"
    library = tmp_path / "chosen-library"
    saved = client.post("/setup", data={
        "inbox_path": str(inbox),
        "library_path": str(library),
        "navidrome_rescan_url": "https://music.example.test/scan",
        "navidrome_token": "secret-token",
    })
    assert saved.status_code == 302
    assert saved.headers["Location"] == "/"
    assert inbox.is_dir()
    assert library.is_dir()

    restarted = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(state_path), "BROWSE_ROOTS": [str(tmp_path)]})
    assert restarted.config["INBOX_PATH"] == str(inbox)
    assert restarted.config["LIBRARY_PATH"] == str(library)
    assert restarted.config["NAVIDROME_RESCAN_URL"] == "https://music.example.test/scan"
    assert restarted.config["NAVIDROME_TOKEN"] == "secret-token"
    assert restarted.test_client().get("/").status_code == 200

def test_browse_is_limited_to_configured_mount_roots(tmp_path):
    mounted = tmp_path / "mounted"
    (mounted / "inbox").mkdir(parents=True)
    (mounted / "library").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(mounted)],
    })
    client = app.test_client()
    root = client.get("/api/browse")
    assert root.status_code == 200
    assert {entry["name"] for entry in root.json["entries"]} == {"inbox", "library"}
    assert client.get(f"/api/browse?path={outside}").status_code == 400

def test_browse_exposes_and_navigates_each_mounted_root(tmp_path):
    first = tmp_path / "first-mount"
    second = tmp_path / "userMedia"
    (first / "inbox").mkdir(parents=True)
    (second / "music" / "albums").mkdir(parents=True)
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(first), str(second)],
    })
    client = app.test_client()

    root = client.get("/api/browse")
    assert root.json["roots"] == [
        {"name": "first-mount", "path": str(first)},
        {"name": "userMedia", "path": str(second)},
    ]
    other_root = client.get("/api/browse", query_string={"path": str(second)})
    assert other_root.status_code == 200
    assert other_root.json["path"] == str(second)
    assert other_root.json["parent"] is None
    assert other_root.json["entries"] == [{"name": "music", "path": str(second / "music")}]

def test_setup_uses_compact_logo_typeable_paths_and_modal_browser(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    client = app.test_client()

    page = client.get("/setup")
    assert b'src="/static/cratekeep-logo.png"' in page.data
    assert b'.logo { display: block; width: 112px;' in page.data
    assert b'id="inbox-list"' in page.data
    assert b'id="inbox-path-1"' in page.data
    assert b'readonly' not in page.data
    assert b'role="dialog" aria-modal="true"' in page.data
    assert b'id="browser-breadcrumbs"' in page.data
    assert b'id="browser-search"' in page.data
    assert b'id="browser-path-form"' in page.data
    assert b'id="browser-root"' not in page.data
    assert b'id="browser-root-button"' not in page.data
    assert page.data.index(b'id="browser-breadcrumbs"') < page.data.index(b'id="browser-up"')
    assert page.data.index(b'id="browser-up"') < page.data.index(b'id="browser-path-form"')
    assert b"event.key === 'Escape'" in page.data
    assert b"fetch('/api/browse?path='" in page.data
    logo = client.get("/static/cratekeep-logo.png")
    assert logo.status_code == 200
    assert logo.mimetype == "image/png"

def test_settings_update_paths_and_preserve_or_clear_token(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(tmp_path / "library"),
        "navidrome_token": "saved-token",
    })

    new_inbox = tmp_path / "new-inbox"
    client.post("/settings", data={
        "inbox_path": str(new_inbox),
        "library_path": str(tmp_path / "library"),
        "navidrome_token": "",
    })
    assert app.config["INBOX_PATH"] == str(new_inbox)
    assert app.config["NAVIDROME_TOKEN"] == "saved-token"
    assert f'directory: "{tmp_path / "library"}"' in Path(app.config["BEETS_CONFIG"]).read_text()

    client.post("/settings", data={
        "inbox_path": str(new_inbox),
        "library_path": str(tmp_path / "library"),
        "navidrome_token": "",
        "clear_navidrome_token": "1",
    })
    assert app.config["NAVIDROME_TOKEN"] == ""

def test_review_then_import(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    album = tmp_path / "inbox" / "album"
    album.mkdir()
    (album / "song.mp3").write_bytes(b"not-real-audio")
    client = app.test_client()
    config_path = Path(app.config["BEETS_CONFIG"])
    assert client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat. {0}",
        "beets_config": config_path.read_text(),
    }).status_code == 302

    preview = client.post("/api/imports/preview", json={"path": "album"})
    assert preview.status_code == 201
    assert preview.json["files"] == ["album/song.mp3"]

    class Result:
        returncode = 0
        stdout = "Imported\n"
        stderr = ""

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return Result()

    monkeypatch.setattr("beets_mvp.subprocess.run", fake_run)
    executed = client.post(f"/api/imports/{preview.json['id']}/execute")
    assert executed.status_code == 200
    assert executed.json["status"] == "complete"
    assert seen["command"][:3] == ["beet", "-c", str(config_path)]
    assert seen["command"][-2:] == ["--move", str(album)]
    assert "ftintitle" in yaml.safe_load(config_path.read_text())["plugins"]

def test_rejects_path_escape(tmp_path):
    client = make_app(tmp_path).test_client()
    response = client.post("/api/imports/preview", json={"path": "../outside"})
    assert response.status_code == 400


def test_named_inboxes_setup_edit_validation_and_legacy_migration(tmp_path):
    state = tmp_path / "config"
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(state),
                      "BROWSE_ROOTS": [str(tmp_path)]})
    client = app.test_client()
    first, second, library = tmp_path / "first", tmp_path / "second", tmp_path / "library"
    response = client.post("/setup", data={
        "inbox_id": ["", ""], "inbox_name": ["Downloads", "Recorder"],
        "inbox_path": [str(first), str(second)], "library_path": str(library),
    })
    assert response.status_code == 302
    assert [row["name"] for row in app.config["INBOXES"]] == ["Downloads", "Recorder"]
    assert all(len(row["id"]) == 32 for row in app.config["INBOXES"])
    original_ids = [row["id"] for row in app.config["INBOXES"]]

    for submitted_ids, expected_error in (
        ([original_ids[0], original_ids[0]], b"may appear only once"),
        ([original_ids[0], "client-chosen-id"], b"managed by Cratekeep"),
    ):
        tampered = client.post("/settings", data={
            "inbox_id": submitted_ids, "inbox_name": ["Changed one", "Changed two"],
            "inbox_path": [str(tmp_path / "changed-one"), str(tmp_path / "changed-two")],
            "library_path": str(library), "beets_config": Path(app.config["BEETS_CONFIG"]).read_text(),
        })
        assert tampered.status_code == 200 and expected_error in tampered.data
        assert [row["id"] for row in app.config["INBOXES"]] == original_ids
        assert [row["name"] for row in app.config["INBOXES"]] == ["Downloads", "Recorder"]
        assert not (tmp_path / "changed-one").exists() and not (tmp_path / "changed-two").exists()

    invalid = client.post("/settings", data={
        "inbox_id": original_ids, "inbox_name": ["Same", "same"],
        "inbox_path": [str(first), str(first)], "library_path": str(library),
        "beets_config": Path(app.config["BEETS_CONFIG"]).read_text(),
    })
    assert invalid.status_code == 200
    assert b"Inbox names must be unique" in invalid.data and b"Inbox paths must be unique" in invalid.data
    assert [row["name"] for row in app.config["INBOXES"]] == ["Downloads", "Recorder"]
    outside = client.post("/settings", data={
        "inbox_id": original_ids, "inbox_name": ["Downloads", "Recorder"],
        "inbox_path": [str(first), "/outside-configured-browse-root"],
        "library_path": str(library), "beets_config": Path(app.config["BEETS_CONFIG"]).read_text(),
    })
    assert outside.status_code == 200 and b"must be inside a mounted directory" in outside.data
    assert [row["path"] for row in app.config["INBOXES"]] == [str(first), str(second)]

    saved = client.post("/settings", data={
        "inbox_id": [original_ids[1], ""], "inbox_name": ["Field", "New source"],
        "inbox_path": [str(tmp_path / "second-edited"), str(tmp_path / "third")], "library_path": str(library),
        "beets_config": Path(app.config["BEETS_CONFIG"]).read_text(),
    })
    assert saved.status_code == 302
    assert app.config["INBOXES"][0]["id"] == original_ids[1]
    assert app.config["INBOXES"][1]["id"] not in original_ids

    restarted = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(state),
                            "BROWSE_ROOTS": [str(tmp_path)]})
    assert restarted.config["INBOXES"] == app.config["INBOXES"]

    legacy_state = tmp_path / "legacy-config"
    legacy_state.mkdir()
    legacy_db = legacy_state / "app.db"
    _init_db(str(legacy_db))
    with sqlite3.connect(legacy_db) as db:
        db.executemany("INSERT INTO app_settings(key,value) VALUES (?,?)", [
            ("inbox_path", str(first)), ("library_path", str(library)),
        ])
        review_job_id = db.execute(
            "INSERT INTO import_jobs(source,files_json,status,created_at) VALUES (?,?,?,?)",
            ("album", json.dumps(["album/song.mp3"]), "review", "now"),
        ).lastrowid
        settled_job_id = db.execute(
            "INSERT INTO import_jobs(source,files_json,status,created_at) VALUES (?,?,?,?)",
            ("old", "[]", "complete", "before"),
        ).lastrowid
    legacy = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(legacy_state),
                         "BROWSE_ROOTS": [str(tmp_path)]})
    assert [(row["name"], row["path"]) for row in legacy.config["INBOXES"]] == [("Inbox", str(first))]
    with sqlite3.connect(legacy.config["APP_DB"]) as db:
        migrated = json.loads(db.execute("SELECT value FROM app_settings WHERE key='inboxes_json'").fetchone()[0])[0]
        assert migrated["path"] == str(first)
        review = db.execute("SELECT inbox_id,inbox_root FROM import_jobs WHERE id=?", (review_job_id,)).fetchone()
        settled = db.execute("SELECT inbox_id,inbox_root FROM import_jobs WHERE id=?", (settled_job_id,)).fetchone()
        assert review == (migrated["id"], str(first.resolve()))
        assert settled == (None, None)
    legacy_client = legacy.test_client()
    changed_root = tmp_path / "legacy-changed"
    assert legacy_client.post("/settings", data={
        "inbox_id": [migrated["id"]], "inbox_name": ["Inbox"], "inbox_path": [str(changed_root)],
        "library_path": str(library), "beets_config": Path(legacy.config["BEETS_CONFIG"]).read_text(),
    }).status_code == 302
    rebound = legacy_client.post(f"/api/imports/{review_job_id}/execute")
    assert rebound.status_code == 409 and b"changed after preview" in rebound.data


def test_global_auto_match_migrates_by_stable_id_and_new_inboxes_default_disabled(tmp_path):
    state = tmp_path / "config"
    state.mkdir()
    database = state / "app.db"
    _init_db(str(database))
    first, second, library = tmp_path / "first", tmp_path / "second", tmp_path / "library"
    rows = [
        {"id": "a" * 32, "name": "First", "path": str(first)},
        {"id": "b" * 32, "name": "Second", "path": str(second)},
    ]
    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO app_settings(key,value) VALUES (?,?)", [
            ("inboxes_json", json.dumps(rows)), ("inbox_path", str(first)),
            ("library_path", str(library)), ("inbox_auto_match", "1"),
        ])
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(state),
                      "BROWSE_ROOTS": [str(tmp_path)]})
    assert [(row["id"], row["auto_match"]) for row in app.config["INBOXES"]] == [
        ("a" * 32, True), ("b" * 32, True),
    ]
    third = tmp_path / "third"
    response = app.test_client().post("/settings", data={
        "inbox_id": ["b" * 32, "a" * 32, ""],
        "inbox_name": ["Second", "First", "Third"],
        "inbox_path": [str(second), str(first), str(third)],
        "inbox_auto_match_id": ["a" * 32],
        "library_path": str(library),
        "beets_config": Path(app.config["BEETS_CONFIG"]).read_text(),
    })
    assert response.status_code == 302
    assert [(row["name"], row["auto_match"]) for row in app.config["INBOXES"]] == [
        ("Second", False), ("First", True), ("Third", False),
    ]
def test_named_inbox_api_disambiguates_and_binds_preview_jobs(tmp_path):
    app = create_app({"TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(tmp_path / "config"),
                      "BROWSE_ROOTS": [str(tmp_path)]})
    roots = [tmp_path / "one", tmp_path / "two"]
    for root in roots:
        (root / "Shared").mkdir(parents=True)
        (root / "Shared" / "song.mp3").write_bytes(root.name.encode())
    library = tmp_path / "library"
    client = app.test_client()
    assert client.post("/setup", data={
        "inbox_id": ["", ""], "inbox_name": ["One", "Two"],
        "inbox_path": [str(path) for path in roots], "library_path": str(library),
    }).status_code == 302
    inboxes = app.config["INBOXES"]
    candidates = client.get("/api/inbox").json
    assert [(item["inbox_name"], item["path"]) for item in candidates] == [("One", "Shared"), ("Two", "Shared")]
    assert client.post("/api/imports/preview", json={"path": "Shared"}).status_code == 400
    assert client.post("/api/imports/preview", json={"inbox_id": "missing", "path": "Shared"}).status_code == 400
    preview = client.post("/api/imports/preview", json={"inbox_id": inboxes[1]["id"], "path": "Shared"})
    assert preview.status_code == 201 and preview.json["inbox_name"] == "Two"

    # Editing the selected ID's path makes the exact preview stale rather than redirecting it.
    config_text = Path(app.config["BEETS_CONFIG"]).read_text()
    assert client.post("/settings", data={
        "inbox_id": [inboxes[0]["id"], inboxes[1]["id"]], "inbox_name": ["One", "Two"],
        "inbox_path": [str(roots[0]), str(tmp_path / "changed")], "library_path": str(library),
        "beets_config": config_text,
    }).status_code == 302
    stale = client.post(f"/api/imports/{preview.json['id']}/execute")
    assert stale.status_code == 409 and b"changed after preview" in stale.data
    assert client.post("/settings", data={
        "inbox_id": [inboxes[0]["id"]], "inbox_name": ["One"],
        "inbox_path": [str(roots[0])], "library_path": str(library),
        "beets_config": config_text,
    }).status_code == 302
    removed = client.post(f"/api/imports/{preview.json['id']}/execute")
    assert removed.status_code == 409 and b"no longer configured" in removed.data


def test_named_inbox_symlink_escape_and_unbound_legacy_job_rejection(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "inbox"
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    link = root / "linked.mp3"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    client = app.test_client()
    assert client.post("/api/imports/preview", json={"path": "linked.mp3"}).status_code == 400

    with sqlite3.connect(app.config["APP_DB"]) as db:
        unbound_job_id = db.execute(
            "INSERT INTO import_jobs(source,files_json,status,created_at) VALUES (?,?,?,?)",
            ("legacy", json.dumps(["legacy/song.mp3"]), "review", "now"),
        ).lastrowid
    unbound = client.post(f"/api/imports/{unbound_job_id}/execute")
    assert unbound.status_code == 409 and b"not bound to an inbox root" in unbound.data

def test_normal_settings_renders_in_app_page_and_beets_editor(tmp_path):
    page = make_app(tmp_path).test_client().get("/settings")

    assert page.status_code == 200
    assert b'--font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI"' in page.data
    assert b'--page: #e7e9ec;' in page.data
    assert b'id="sidebar"' in page.data
    assert b'class="content"' in page.data
    assert b'aria-label="Primary navigation"' in page.data
    assert b'aria-current="page"' in page.data
    assert b'<span class="nav-label">Settings</span>' in page.data
    assert b'id="theme-toggle"' in page.data
    assert b'title="Build SHA"' in page.data
    assert b'id="open-sidebar"' in page.data
    assert b'<h1>Settings</h1>' in page.data
    assert b'Set up Cratekeep' not in page.data
    assert b'id="beets_config"' in page.data
    assert b'core import safety settings' in page.data
    assert b'name="fetch_art"' in page.data
    assert b'name="fetch_art" type="checkbox" value="1" checked' not in page.data
    assert b'id="inventory-preview"' in page.data
    assert b'never imports or changes media' in page.data


def test_settings_groups_existing_controls_in_accessible_tabs(tmp_path):
    page = make_app(tmp_path).test_client().get("/settings")
    html = page.data

    for name, label in (("general", "General"), ("metadata", "Metadata"), ("library-import", "Library import")):
        assert f'id="settings-tab-{name}"'.encode() in html
        assert b'role="tab" aria-selected=' in html
        assert f'aria-controls="settings-panel-{name}"'.encode() in html
        assert f'>{label}</button>'.encode() in html
        assert f'id="settings-panel-{name}" role="tabpanel" aria-labelledby="settings-tab-{name}"'.encode() in html

    general = html[html.index(b'id="settings-panel-general"'):html.index(b'id="settings-panel-metadata"')]
    metadata = html[html.index(b'id="settings-panel-metadata"'):html.index(b'id="settings-panel-library-import"')]
    library_import = html[html.index(b'id="settings-panel-library-import"'):html.index(b'<button type="submit">')]
    assert b'id="storage-title"' in general and b'id="navidrome-title"' in general
    assert b'id="artwork-title"' in metadata and b'id="beets_config"' in metadata
    assert b'id="inventory-preview"' in library_import and b'id="inventory-status"' in library_import
    assert b'id="library-import-review-list"' in library_import
    assert b'data-review-modal' in html
    assert b"Adoption" not in html and b"adoption" not in html
    assert b'name="inbox_path"' in general and b'name="navidrome_token"' in general
    assert b'name="fetch_art"' in metadata and b'name="beets_config"' in metadata
    assert b"event.key === 'ArrowRight'" in html
    assert b"window.addEventListener('hashchange'" in html


def test_settings_script_initializes_without_stale_direct_import_references(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]

    assert "library-import-queue" not in script
    assert "CratekeepReviewWorkflow.create" in script
    assert "CratekeepFolderWorkflow.create" in script
    assert "const themeButton = document.getElementById('theme-toggle');" in script
    assert "localStorage.getItem('cratekeep-theme') === 'dark'" in script
    assert "themeButton.addEventListener('click'" in script
    assert "function renderAlbumMatch" not in script and "function openReview" not in script


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for rendered JavaScript syntax validation")
def test_rendered_settings_javascript_has_valid_syntax(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    completed = subprocess.run(
        [shutil.which("node"), "--check", "-"], input=script, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for rendered JavaScript syntax validation")
def test_rendered_inbox_javascript_has_valid_syntax(tmp_path):
    html = make_app(tmp_path).test_client().get("/inbox").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    completed = subprocess.run(
        [shutil.which("node"), "--check", "-"], input=script, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate shared review output")
def test_library_and_inbox_use_one_structured_review_presentation(tmp_path):
    client = make_app(tmp_path).test_client()
    settings = client.get("/settings").get_data(as_text=True)
    inbox = client.get("/inbox").get_data(as_text=True)
    assert 'src="/static/review-presentation.js"' in settings
    assert 'src="/static/review-presentation.js"' in inbox
    assert 'src="/static/review-workflow.js"' in settings
    assert 'src="/static/review-workflow.js"' in inbox
    assert "CratekeepReviewWorkflow.create" in settings
    assert "CratekeepReviewWorkflow.create" in inbox
    module = Path("beets_mvp/static/review-presentation.js").read_text()
    fixture = {
        "current_metadata":{"artist":"Local Artist","album":"Local Album","genre":"Rock","year":"2001"},
        "tracks":[{"id":1}],
    }
    candidate = {
        "artist":"Online Artist","album":"Online Album","genre":"Jazz","year":"2002",
        "provider_id":"release-id","release_type":"Album","country":"GB",
        "media":[{"format":"CD"}],
        "track_details":[{"status":"matched","local":"Old title","proposed":"New title",
                           "local_artist":"Local Artist","proposed_artist":"Online Artist",
                           "current_position":[1,1],"proposed_position":[1,1],
                           "local_duration":60,"proposed_duration":70}],
    }
    probe = f"""
globalThis.window = globalThis;
{module}
const facts = window.CratekeepReview.describeCandidate({json.dumps(fixture)}, {json.dumps(candidate)});
process.stdout.write(JSON.stringify(facts.map(f => ({{label:f.label,value:f.value,changed:f.changed,tracks:(f.tracks||[]).map(t => ({{titleChanged:t.titleChanged,durationChanged:t.durationChanged,explanations:t.explanations}}))}}))));
"""
    completed = subprocess.run([shutil.which("node"), "-"], input=probe, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    facts = {fact["label"]: fact for fact in json.loads(completed.stdout)}
    assert set(facts) >= {"Artist", "Album", "Genre", "Release Year", "Tracks changed",
                          "Release Type/media", "Release Region", "Local tracks found online",
                          "Online tracks present locally"}
    assert facts["Artist"]["changed"] and facts["Release Year"]["changed"]
    changed_track = facts["Tracks changed"]["tracks"][0]
    assert changed_track["titleChanged"] and changed_track["durationChanged"]
    assert any("Title differs" in item for item in changed_track["explanations"])


def test_album_review_modal_uses_shared_presentation_and_accessible_shell(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    modal = html[html.index('data-review-modal'):]
    controller = Path("beets_mvp/static/review-workflow.js").read_text()
    shared_js = Path("beets_mvp/static/review-presentation.js").read_text()
    shared_css = Path("beets_mvp/static/review-presentation.css").read_text()

    for contract in (
        'role="dialog" aria-modal="true"', 'aria-labelledby="shared-library-review-title"',
        'role="radiogroup"', 'data-review-track-modal', 'data-review-mbid-modal',
        'class="approve-button button-primary"',
    ):
        assert contract in modal
    for contract in (
        "function renderCandidates", "function renderTrackRows", "className = 'candidate-option'",
        "className='candidate-fact'", "className='track-change-button'", "aria-haspopup','dialog'",
        "className='track-row-unmatched'", "classList.add('track-field-changed')",
        "className='track-explanation-row'", "label:'Release Type/media'",
        "label:'Release Region'", "label:'Local tracks found online'", "label:'Online tracks present locally'",
    ):
        assert contract in shared_js
    for selector in (
        '.candidate-option[open]', '.candidate-fact[data-changed="true"]', '.track-change-button:focus-visible',
        '.track-comparison-table', '.track-field-changed', '.track-explanation-row td',
    ):
        assert selector in shared_css
    assert "presentation.renderCandidates" in controller
    assert "presentation.renderTrackRows" in controller
    assert "dialog.inert = true" in controller and "nestedReturnFocus?.focus()" in controller
    assert "if (event.key === 'Escape')" in controller and "event.key !== 'Tab'" in controller
    assert "focusTarget?.focus()" in controller
    assert 'class="button-spinner" aria-hidden="true" hidden' in modal
    assert "library-import-queue" not in modal and "Confirm import" not in modal


def test_review_presentation_has_one_renderer_and_one_common_stylesheet():
    settings = Path("beets_mvp/templates/settings.html").read_text()
    inbox = Path("beets_mvp/templates/inbox.html").read_text()
    shared_js = Path("beets_mvp/static/review-presentation.js").read_text()
    shared_css = Path("beets_mvp/static/review-presentation.css").read_text()

    review_controller = Path("beets_mvp/static/review-workflow.js").read_text()
    partial = Path("beets_mvp/templates/_review_workflow.html").read_text()
    assert settings.count("{% include '_review_workflow.html' %}") == 1
    assert inbox.count("{% include '_review_workflow.html' %}") == 1
    assert settings.count("CratekeepReviewWorkflow.create") == 1
    assert inbox.count("CratekeepReviewWorkflow.create") == 1
    assert review_controller.count("presentation.renderCandidates") == 1
    assert review_controller.count("presentation.renderTrackRows") == 1
    assert partial.count("data-review-modal") == 1
    assert shared_js.count("function renderCandidates") == 1
    assert shared_js.count("function renderTrackRows") == 1
    for dead_definition in (
        "candidateIconPaths", "function candidateFacts", "function candidateIcon",
        "function trackEvidence", "function appendTrackCell", "function appendTrackMetadataCell",
        "function matchScorePresentation",
    ):
        assert dead_definition not in settings
        assert dead_definition not in inbox
    for duplicate in (
        "function openReview", "function renderAlbumMatch", "function openTrackComparison",
        "mbidSearchReturnFocus", "reviewOpeningState", "reviewTransitionToken",
    ):
        assert duplicate not in settings and duplicate not in inbox
    assert "innerHTML" not in settings and "innerHTML" not in inbox

    inline_styles = "\n".join(re.findall(r"<style>(.*?)</style>", settings + inbox, re.S))
    definitive_selectors = (
        ".candidate-option", ".candidate-option-summary", ".candidate-confidence",
        ".candidate-signals", ".candidate-fact", ".candidate-artwork",
        ".track-change-button", ".track-comparison-table", ".track-field-changed",
        ".track-explanation-row", ".score-pill[data-band", ".confidence-band[data-band",
    )
    for selector in definitive_selectors:
        assert selector in shared_css
        assert selector not in inline_styles


def test_library_and_inbox_share_corrected_modal_controls_and_selection_save_path(tmp_path):
    client = make_app(tmp_path).test_client()
    settings = client.get("/settings").get_data(as_text=True)
    inbox = client.get("/inbox").get_data(as_text=True)
    partial = Path("beets_mvp/templates/_review_workflow.html").read_text()
    controller = Path("beets_mvp/static/review-workflow.js").read_text()

    for page in (settings, inbox):
        assert ">Find matches<" in page
        assert ">Search by MBID<" in page
        assert ">Import to Library<" in page
        assert "Current metadata" not in page
        assert "Approve selection" not in page
        assert "Exact MusicBrainz release ID" not in page
    assert "Selecting metadata is explicit; matching alone never writes or moves files." in inbox
    assert "Approval is always explicit; matching alone never writes or moves files." not in inbox
    assert "MusicBrainz release or release-group ID" in partial
    assert "data-review-current" not in partial and "data-review-approve" not in partial
    assert "async function selectDecision" in controller
    assert "decision: 'approved', candidate_id: selectedValue(id)" in controller
    assert "adapter.save(album" in controller
    assert "As Is keeps current tags. Select this option to use current metadata." in Path(
        "beets_mvp/static/review-presentation.js"
    ).read_text()
    assert "Explicit approval required." not in Path("beets_mvp/static/review-presentation.js").read_text()


def test_inbox_duplicate_choice_after_selection_persists_before_execute(tmp_path, monkeypatch):
    controller = Path("beets_mvp/static/review-workflow.js").read_text()
    handler = controller[controller.index("async function persistDuplicateAction"):controller.index("function render()")]
    assert "setBusy(true, 'Saving duplicate decision…', duplicateAction)" in handler
    assert "decision: 'approved', candidate_id: selectedValue(selected), duplicate_action: next" in handler
    assert "duplicateAction.value = previous; render(); setStatus(error.message, true)" in handler
    assert "duplicateAction.addEventListener('change', persistDuplicateAction)" in controller

    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Incoming" / "01 Song.wav"
    tagged_inbox_wav(source)
    managed_path = tmp_path / "library" / "Managed" / "Old Album" / "01 Song.wav"
    tagged_inbox_wav(managed_path, artist="Managed", album="Old Album")
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    managed = Item.from_path(managed_path); managed.mb_trackid = "recording-1"; library.add(managed)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": query["artist"], "album": query["album"],
        "tracks": [{"title": "Song", "track_artist": "Artist", "position": 1,
                    "medium_position": 1, "length_ms": 1000, "recording_id": "recording-1"}],
    }]
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    matched = client.post("/api/inbox/candidates", json={"album_id": album["id"]}).json["album"]
    candidate_id = matched["candidates"][0]["id"]

    selected = client.patch(f"/api/inbox/albums/{album['id']}", json={
        "decision": "approved", "candidate_id": candidate_id,
    })
    assert selected.status_code == 200 and selected.json["duplicate_action"] is None
    saved = client.patch(f"/api/inbox/albums/{album['id']}", json={
        "decision": "approved", "candidate_id": candidate_id, "duplicate_action": "keep-both",
    })
    assert saved.status_code == 200 and saved.json["duplicate_action"] == "keep-both"

    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")

    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    preview = client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run": True})
    executed = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert preview.status_code == 200 and preview.json["status"] == "preview"
    assert executed.status_code == 201 and executed.json["status"] == "complete"


def test_shared_rows_button_gaps_and_pagination_alignment_contract(tmp_path):
    client = make_app(tmp_path).test_client()
    settings = client.get("/settings").get_data(as_text=True)
    inbox = client.get("/inbox").get_data(as_text=True)
    css = Path("beets_mvp/static/app-shell.css").read_text()

    controller = Path("beets_mvp/static/folder-workflow.js").read_text()
    for class_name in ("review-folder", "review-folder-summary", "review-folder-identity",
                       "review-folder-path", "review-folder-count", "status-pill"):
        assert class_name in controller
    assert "rowClass:'review-folder'" not in settings + inbox
    assert "statusClass:'status-pill'" not in settings + inbox
    assert ".review-toolbar-actions,.review-footer-group,.browser-actions,.workflow-actions { gap:.5rem; }" in css
    assert ".pagination .page-label,.review-pagination .page-label { display:flex; align-self:stretch; align-items:center;" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate shared folder helpers")
def test_shared_folder_status_and_progressive_loading_contract(tmp_path):
    module = Path("beets_mvp/static/folder-workflow.js").read_text()
    probe = """
globalThis.window = globalThis;
%s
process.stdout.write(JSON.stringify([
  window.CratekeepFolderWorkflow.statusLabel('needs-review'),
  window.CratekeepFolderWorkflow.statusLabel('Needs attention'),
  window.CratekeepFolderWorkflow.statusLabel('error'),
]));
""" % module
    completed = subprocess.run([shutil.which("node"), "-"], input=probe, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == ["Tagged", "Tagged", "error"]

    settings = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    assert "progressivePages:true" in settings
    assert "void hydratePages(result, context, query, filter)" in module
    assert module.index("render();", module.index("async function load(")) < module.index(
        "void hydratePages(result, context, query, filter)", module.index("async function load(")
    ) < module.index("await refreshSummary();", module.index("async function load("))
    assert "if (seen.has(item.path)) throw new Error" in module
    assert "if (seen.size !== result.total) throw new Error" in module
    assert "pageCache.get(targetPage)" in module
    assert "preserveSelection = true" in module
    assert ">Needs review<" not in settings and ">Tagged<" in settings


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate shared review helpers")
def test_shared_track_evidence_identifies_only_material_changes():
    module = Path("beets_mvp/static/review-presentation.js").read_text()
    tracks = [
        {"local":"Old title","proposed":"New title","current_position":[1,1],"proposed_position":[1,2],"local_duration":120,"proposed_duration":130,"status":"title-mismatch"},
        {"local":"Part 1: Intro","proposed":"Part 1 — Intro","current_position":[1,2],"proposed_position":[1,2],"local_duration":90,"proposed_duration":90,"status":"title-mismatch"},
        {"local":"Don't Stop","proposed":"Don’t Stop","current_position":[1,3],"proposed_position":[1,3],"local_duration":180,"proposed_duration":180,"status":"matched"},
        {"local":"Artist only","proposed":"Artist only","local_artist":"Lead","proposed_artist":"Lead feat. Guest","current_position":[1,4],"proposed_position":[1,4],"local_duration":180,"proposed_duration":180,"status":"matched"},
        {"local":None,"proposed":"Bonus","current_position":None,"proposed_position":[2,1],"local_duration":None,"proposed_duration":90,"status":"extra"},
    ]
    probe = f"""
globalThis.window = globalThis;
{module}
const result = window.CratekeepReview.trackEvidence({json.dumps(tracks)});
process.stdout.write(JSON.stringify({{changed:result.filter(t => t.titleChanged || t.artistChanged || t.positionChanged || t.durationChanged || t.unpaired), score:window.CratekeepReview.score(.901)}}));
"""
    completed = subprocess.run([shutil.which("node"), "-"], input=probe, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert len(result["changed"]) == 4
    assert result["score"] == {"band": "high", "label": "90.1%", "accessibleLabel": "90.1% match confidence; meets the initial auto-import indicator threshold"}
    assert result["changed"][0]["explanations"] == [
        "Title differs: local “Old title” → MusicBrainz “New title”.",
        "Position differs: local 1.01 → MusicBrainz 1.02.",
        "Duration differs: local 2:00 → MusicBrainz 2:10 (10 seconds longer).",
    ]
    assert result["changed"][-1]["explanations"] == [
        "MusicBrainz track is not present locally.", "Local duration unavailable.",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_library_inventory_score_bands_and_status_semantics(tmp_path):
    module = Path("beets_mvp/static/review-presentation.js").read_text()
    probe = """
const result = {
  scores: [0.7499, 0.75, 0.8999, 0.90].map(window.CratekeepReview.score),
};
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval",
         "globalThis.window=globalThis;\n" + module + "\n" + probe],
        check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)

    assert [(score["label"], score["band"]) for score in result["scores"]] == [
        ("74.9%", "low"), ("75%", "medium"), ("89.9%", "medium"), ("90%", "high"),
    ]
    assert result["scores"][3]["accessibleLabel"].endswith("initial auto-import indicator threshold")


def test_library_inventory_pills_render_accessible_labels_and_contrast_safe_styles(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    shell_css = Path("beets_mvp/static/app-shell.css").read_text()
    shared_css = Path("beets_mvp/static/review-presentation.css").read_text()
    shared_js = Path("beets_mvp/static/review-presentation.js").read_text()

    assert "confidence.dataset.band" in shared_js
    assert "presentation.accessibleLabel" in shared_js

    palette = {
        "low": ("#fff", "#b42318"),
        "medium": ("#342100", "#fdb022"),
        "high": ("#fff", "#067647"),
    }
    for band, (foreground, background) in palette.items():
        assert f'.score-pill[data-band="{band}"]' in shared_css
        assert f"color:{foreground};background:{background}" in shared_css
        assert _contrast_ratio(foreground, background) >= 4.5

    status_palettes = [
        ("#163d29", "#d9eee1"), ("#704600", "#fff0c2"),
        ("#8a1c28", "#f8e3e5"), ("#e9fff0", "#294337"),
        ("#ffe6aa", "#533f1d"), ("#ffd9dc", "#55252b"),
    ]
    for foreground, background in status_palettes:
        assert f"color:{foreground}; background:{background};" in shell_css
        assert _contrast_ratio(foreground, background) >= 4.5


def _contrast_ratio(first: str, second: str) -> float:
    def luminance(value: str) -> float:
        digits = value.removeprefix("#")
        if len(digits) == 3:
            digits = "".join(character * 2 for character in digits)
        channels = [int(digits[offset:offset + 2], 16) / 255 for offset in (0, 2, 4)]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                  for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_library_inventory_preview_is_bounded_non_mutating_and_persisted(tmp_path):
    app = make_app(tmp_path)
    library_root = tmp_path / "library"
    files = []
    for number in range(55):
        path = library_root / f"album-{number:02d}" / f"track-{number:02d}.mp3"
        path.parent.mkdir()
        path.write_bytes(f"synthetic-{number}".encode())
        files.append(path)
    (library_root / "album-00" / "notes.txt").write_text("ignored")
    original = {path: path.read_bytes() for path in files}

    beets_library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    tracked = Item(title="Canonical title", path=str(files[0]))
    beets_library.add(tracked)

    client = app.test_client()
    preview = client.post("/api/library/inventory/preview")

    assert preview.status_code == 201
    assert preview.json["status"] == "complete"
    assert preview.json["files"] == 55
    assert preview.json["tracked"] == 1
    assert preview.json["untracked"] == 54
    assert preview.json["missing"] == 0
    assert len(preview.json["sample"]) == 50
    assert preview.json["sample_truncated"] is True
    assert all(path.read_bytes() == original[path] for path in files)

    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM adoption_jobs").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM library_inventory").fetchone()[0] == 55
        assert db.execute("SELECT COUNT(*) FROM adoption_reviews").fetchone()[0] == 55
        row = db.execute(
            "SELECT beets_item_id, size_bytes, mtime_ns, inode, present FROM library_inventory WHERE relative_path = ?",
            (files[0].relative_to(library_root).as_posix(),),
        ).fetchone()
        assert row[0] == tracked.id
        assert row[1] == files[0].stat().st_size
        assert row[2] == files[0].stat().st_mtime_ns
        assert row[3] == files[0].stat().st_ino
        assert row[4] == 1

    files[-1].unlink()
    second = client.post("/api/library/inventory/preview")
    assert second.status_code == 201
    assert second.json["files"] == 54
    assert second.json["missing"] == 1
    assert client.get("/api/library/inventory").json == second.json


def test_inventory_requires_setup_and_ignores_library_symlinks(tmp_path):
    unconfigured = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "unconfigured"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    assert unconfigured.test_client().post("/api/library/inventory/preview").status_code == 503

    app = make_app(tmp_path)
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    link = tmp_path / "library" / "outside-link.mp3"
    link.symlink_to(outside)

    preview = app.test_client().post("/api/library/inventory/preview")
    assert preview.status_code == 201
    assert preview.json["files"] == 0
    assert outside.read_bytes() == b"outside"


def test_library_import_reviews_are_bounded_deterministic_and_decisions_only_update_workflow(tmp_path):
    app = make_app(tmp_path)
    library_root = tmp_path / "library"
    tracked_path = library_root / "a-tracked.mp3"
    review_path = library_root / "b-review.mp3"
    tracked_path.write_bytes(b"tracked-audio")
    review_path.write_bytes(b"review-audio")
    original = {path: path.read_bytes() for path in (tracked_path, review_path)}

    beets_library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    tracked = Item(
        title="Already tracked", artist="Track Artist", albumartist="Album Artist",
        album="Tracked Album", path=str(tracked_path),
    )
    beets_library.add(tracked)

    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201

    bounded = client.get("/api/library-import/reviews?limit=1")
    assert bounded.status_code == 200
    assert bounded.json["limit"] == 1
    assert bounded.json["has_more"] is True
    assert bounded.json["items"] == [{
        "id": bounded.json["items"][0]["id"],
        "path": "a-tracked.mp3",
        "artist": "Album Artist",
        "album": "Tracked Album",
        "title": "Already tracked",
        "status": "tracked",
        "decision": "pending",
        "candidate": {"kind": "beets-item", "beets_item_id": tracked.id},
        "candidates": [{"kind": "beets-item", "beets_item_id": tracked.id}],
        "updated_at": bounded.json["items"][0]["updated_at"],
    }]

    reviews = client.get("/api/library-import/reviews").json["items"]
    needs_review = next(item for item in reviews if item["path"] == "b-review.mp3")
    assert needs_review["status"] == "needs-review"
    assert needs_review["candidate"] == {"kind": "needs-review", "match": None}

    updated = client.patch(
        f'/api/library-import/reviews/{needs_review["id"]}', json={"decision": "approved"}
    )
    assert updated.status_code == 200
    assert updated.json["decision"] == "approved"
    assert client.patch(
        f'/api/library-import/reviews/{needs_review["id"]}', json={"decision": "import-now"}
    ).status_code == 400
    assert client.get("/api/library-import/reviews?limit=0").status_code == 400

    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT state FROM adoption_reviews WHERE inventory_id = ?", (needs_review["id"],)
        ).fetchone()[0] == "approved"
    assert all(path.read_bytes() == contents for path, contents in original.items())


def test_library_import_review_groups_preserve_source_folder_hierarchy(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    paths = [
        root / "Artist One" / "Album A" / "01 First.mp3",
        root / "Artist One" / "Album A" / "02 Second.mp3",
        root / "Artist One" / "Album B" / "01 Other.mp3",
        root / "Artist Two" / "Album C" / "01 Last.mp3",
        root / "Compilations" / "Deep" / "Various" / "01 Mixed.mp3",
        root / "Loose.mp3",
    ]
    paths.extend(
        root / "Bulk Artist" / f"Album {number // 10}" / f"{number:02d} Song.mp3"
        for number in range(50)
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    payload = client.get("/api/library-import/reviews?limit=50").json

    assert [group["path"] for group in payload["groups"]] == [
        "", "Artist One", "Artist Two", "Bulk Artist", "Compilations",
    ]
    artist_one = payload["groups"][1]
    assert [folder["path"] for folder in artist_one["folders"]] == ["Artist One/Album A", "Artist One/Album B"]
    assert [song["title"] for song in artist_one["folders"][0]["albums"][0]["songs"]] == [
        "01 First", "02 Second",
    ]
    bulk = payload["groups"][3]
    assert sum(len(album["songs"]) for folder in bulk["folders"] for album in folder["albums"]) == 50
    compilations = payload["groups"][4]
    assert compilations["folders"][0]["folders"][0]["path"] == "Compilations/Deep/Various"
    assert payload["groups"][0]["albums"][0]["songs"][0]["path"] == "Loose.mp3"
    assert "Artist One/Album A" in {
        path for album in payload["albums"] for path in album["source_paths"]
    }
    assert _group_library_import_review_items(payload["items"]) == payload["groups"]


def test_library_import_review_paginates_25_albums_without_gaps_or_duplicates(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    expected_paths = []
    for number in range(53):
        artist = f"Artist {number // 4:03d}"
        album = f"Album {number:03d}"
        path = root / artist / album / "01 Track.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
        expected_paths.append(path.relative_to(root).as_posix())

    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201

    pages = [client.get(f"/api/library-import/reviews?limit=25&offset={offset}") for offset in (0, 25, 50)]
    assert all(page.status_code == 200 for page in pages)
    assert [page.json["album_count"] for page in pages] == [25, 25, 3]
    assert [page.json["total_albums"] for page in pages] == [53, 53, 53]
    assert [page.json["has_more"] for page in pages] == [True, True, False]
    assert [page.json["next_offset"] for page in pages] == [25, 50, None]
    actual_paths = [item["path"] for page in pages for item in page.json["items"]]
    assert actual_paths == expected_paths
    assert len(actual_paths) == len(set(actual_paths)) == 53
    assert all(len(page.json["albums"]) <= 25 for page in pages)
    queried_albums = []
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: queried_albums.append(query["album"]) or []
    generated = client.post("/api/library-import/candidates", json={"limit": 25})
    assert generated.status_code == 201
    assert generated.json["albums"] == 25
    assert queried_albums == [f"Album {number:03d}" for number in range(25)]
    assert client.get("/api/library-import/reviews?offset=-1").status_code == 400
    assert client.get("/api/library-import/reviews?limit=51").status_code == 400


def test_genre_presentation_helpers_are_conservative_and_deterministic():
    assert _genre_presentation_key("  Alternative\t Rock ") == _genre_presentation_key("alternative rock")
    assert _genre_presentation_key("post-punk") != _genre_presentation_key("post punk")
    assert _genre_presentation_key("Ｒ＆Ｂ") != _genre_presentation_key("R&B")
    assert [_format_genre(value) for value in ("alternative rock", "post-punk", "r&b")] == [
        "Alternative Rock", "Post-Punk", "R&B",
    ]


@pytest.mark.parametrize(
    "current_genre, provider_genre, expected_genre, expected_change",
    [
        ("Alternative Rock", "alternative rock", "Alternative Rock", None),
        ("", "alternative rock", "Alternative Rock", {"from": None, "to": "Alternative Rock"}),
        ("Rock", "post-punk", "Post-Punk", {"from": "Rock", "to": "Post-Punk"}),
    ],
)
def test_reliable_genre_preview_and_write_preserve_equivalent_or_apply_display_form(
    tmp_path, current_genre, provider_genre, expected_genre, expected_change,
):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "+44" / "When Your Heart Stops Beating" / "01 Lycanthrope.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Lycanthrope"
    media.artist = media.albumartist = "+44"
    media.album = "When Your Heart Stops Beating"
    media.genre = current_genre
    media.mb_albumid = "release-1"
    media.save()
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    item = Item.from_path(path)
    library.add(item)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = {
        "provider_id": "release-1", "artist": "+44", "album": "When Your Heart Stops Beating",
        "genre": provider_genre,
        "genre_evidence": {
            "source": "musicbrainz-release-group-genres", "selected": provider_genre,
            "counts": [{"name": provider_genre, "count": 3}],
        },
    }
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [candidate]
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert album["candidates"][0]["genre"] == expected_genre
    candidate_change = album["candidates"][0]["proposed_diff"].get("genre")
    assert (candidate_change or {}).get("to") == (expected_change or {}).get("to")
    assert bool(candidate_change) is bool(expected_change)
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
    })
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    preview_change = preview.json["items"][0]["changes"].get("genre")
    assert (preview_change or {}).get("to") == (expected_change or {}).get("to")
    assert bool(preview_change) is bool(expected_change)
    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    stored = library.get_item(item.id)
    assert _item_genre(stored) == expected_genre
    assert MediaFile(str(path)).genre == expected_genre
    if expected_change is None:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_genre_write_skips_undeclared_flex_alias_and_keeps_scalar(monkeypatch):
    monkeypatch.delitem(Item._fields, "genres", raising=False)
    item = Item()

    _set_item_value(item, "genre", "Alternative Rock")

    assert item.genre == "Alternative Rock"
    assert item.get("genres") is None


def test_genre_write_mirrors_declared_list_field(tmp_path, monkeypatch):
    monkeypatch.setitem(Item._fields, "genres", beets_types.DelimitedString("; "))
    library = Library(str(tmp_path / "declared-genres.db"), directory=str(tmp_path))
    item = Item(title="Song", path=str(tmp_path / "song.wav"))

    _set_item_value(item, "genre", "Alternative Rock")
    library.add(item)
    item.store()

    stored = library.get_item(item.id)
    assert stored.genre == "Alternative Rock"
    assert stored.genres == ["Alternative Rock"]


@pytest.mark.parametrize("evidence", [
    {"source": "musicbrainz-release-group-genres", "selected": None,
     "counts": [{"name": "alternative rock", "count": 1}]},
    {"source": "musicbrainz-release-group-genres", "selected": None,
     "counts": [{"name": "rock", "count": 3}, {"name": "pop", "count": 3}]},
    None,
])
def test_weak_tied_or_missing_genre_evidence_preserves_existing_spelling(evidence):
    candidate = {"genre": None, "genre_evidence": evidence}
    assert _candidate_diff({"artist": "A", "album": "B", "genre": "aLtErNaTiVe  ROCK", "tracks": []}, candidate).get("genre") is None


def test_album_review_source_path_preserves_literal_filesystem_spelling(tmp_path):
    app = make_app(tmp_path)
    folder = "+44 - When Your Heart Stops Beating"
    path = tmp_path / "library" / folder / "01 - Lycanthrope.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")

    client = app.test_client()
    client.post("/api/library/inventory/preview")
    payload = client.get("/api/library-import/reviews").json

    assert payload["albums"][0]["source_paths"] == [folder]
    assert payload["items"][0]["path"] == f"{folder}/01 - Lycanthrope.mp3"


def test_musicbrainz_candidates_normalize_rank_and_persist_by_album(tmp_path):
    seen = []

    def provider(query, *, limit):
        seen.append((query, limit))
        return [
            {"provider_id": "weaker", "artist": "Other Artist", "album": "Album A", "track_count": 7,
             "tracks": [{"title": f"Other {number}", "position": number} for number in range(1, 8)],
             "retrieval": {"search_score": 100, "source": "musicbrainz-search"}},
            {"provider_id": "best", "artist": "Artist One", "album": "Album A", "track_count": 2, "year": 2020,
             "release_type": "Album", "country": "GB", "media": [{"format": "CD"}],
             "tracks": [{"title": "First Song", "position": 1}, {"title": "Second Song", "position": 2}],
             "retrieval": {"search_score": 1, "source": "musicbrainz-search"}},
        ]

    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    root = tmp_path / "library" / "  Artist One  " / "Album A"
    root.mkdir(parents=True)
    (root / "01 - First Song.mp3").write_bytes(b"first")
    (root / "02 Second Song.mp3").write_bytes(b"second")
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201

    generated = client.post("/api/library-import/candidates", json={"limit": 25})

    assert generated.status_code == 201
    assert generated.json["albums"] == 1
    assert generated.json["candidates"] == 2
    assert [track["title"] for track in seen[0][0]["tracks"]] == ["First Song", "Second Song"]
    assert seen[0][1] == 5
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert [candidate["provider_id"] for candidate in album["candidates"]] == ["best", "weaker"]
    assert album["candidates"][0]["confidence"] > .9
    assert album["candidates"][0]["retrieval"]["search_score"] == 1
    assert album["candidates"][0]["release_type"] == "Album"
    assert album["candidates"][0]["country"] == "GB"
    assert album["candidates"][0]["media"] == [{"format": "CD"}]
    assert {key: album["candidates"][1]["proposed_diff"][key] for key in ("artist", "track_count")} == {
        "artist": {"from": "Artist One", "to": "Other Artist"},
        "track_count": {"from": 2, "to": 7},
    }
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM metadata_candidates").fetchone()[0] == 2
        assert db.execute("SELECT status FROM adoption_jobs WHERE kind = 'musicbrainz_candidates'").fetchone()[0] == "complete"


def test_candidate_refresh_replaces_results_and_provider_errors_are_persisted(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"song")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [
        {"provider_id": "old", "artist": query["artist"], "album": query["album"], "score": 100}
    ]
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    def rate_limited(query, *, limit):
        raise ProviderError("MusicBrainz rate limit reached; try again later", retryable=True)

    app.config["MUSICBRAINZ_PROVIDER"] = rate_limited
    failed = client.post("/api/library-import/candidates", json={})
    assert failed.status_code == 207
    assert failed.json["errors"][0]["retryable"] is True
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert album["candidate_status"] == "error"
    assert "rate limit" in album["candidate_error"]
    assert album["candidates"][0]["provider_id"] == "old"


@pytest.mark.parametrize("provider_genre, expected_genre", [(None, None), ("jazz", {"from": "Rock", "to": "Jazz"})])
def test_candidate_genre_is_explicit_and_only_proposed_when_real(tmp_path, provider_genre, expected_genre):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item(title="Song", artist="Artist", albumartist="Artist", album="Album", genre="Rock", path=str(path)))
    candidate = {
        "provider_id": "release-1", "artist": "Artist", "album": "Album", "track_count": 1,
        "tracks": [{"title": "Song", "position": 1}],
    }
    if provider_genre:
        candidate["genre"] = provider_genre
        candidate["genre_evidence"] = {
            "source": "musicbrainz-release-group-genres", "selected": provider_genre,
            "counts": [{"name": provider_genre, "count": 3}],
        }
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [candidate]
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = client.get("/api/library-import/reviews").json["albums"][0]

    assert album["current_metadata"]["genre"] == "Rock"
    assert album["candidates"][0]["genre"] == (_format_genre(provider_genre) if provider_genre else None)
    assert album["candidates"][0]["proposed_diff"].get("genre") == expected_genre
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
    })
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    genre_changes = [item["changes"].get("genre") for item in preview.json["items"] if item["changes"].get("genre")]
    assert genre_changes == ([expected_genre] if expected_genre else [])


@pytest.mark.parametrize("genres, selected", [
    ([{"name": "rock", "count": 2}, {"name": "pop", "count": 1}], "rock"),
    ([{"name": "rock", "count": 1}], None),
    ([{"name": "rock", "count": 3}, {"name": "pop", "count": 3}], None),
    ([{"name": "noise", "count": 0}], None),
    ([], None),
])
def test_musicbrainz_genre_rule_requires_decisive_positive_release_group_votes(genres, selected):
    result = _genre_evidence(genres)
    assert result["genre"] == selected
    assert result["genre_evidence"]["selected"] == selected
    assert result["genre_evidence"]["source"] == "musicbrainz-release-group-genres"


@pytest.mark.parametrize("sidecar,embed,replace,existing,available", [
    (True, False, False, False, True),
    (False, True, False, False, True),
    (True, True, False, False, True),
    (True, True, False, True, True),
    (True, True, True, True, True),
    (True, True, True, True, False),
])
def test_direct_import_artwork_destinations_preserve_replace_and_missing_noop(
    tmp_path, sidecar, embed, replace, existing, available,
):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=sidecar, ART_EMBED=embed, ART_REPLACE=replace)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    old_art, new_art = jpeg_bytes((120, 10, 10)), jpeg_bytes((10, 120, 10))
    cover = path.parent / "cover.jpg"
    if existing:
        cover.write_bytes(old_art)
        media = MediaFile(str(path)); media.images = [MediaImage(old_art, type=ImageType.front)]; media.save()
    candidate = {
        "provider_id": "123e4567-e89b-42d3-a456-426614174000", "artist": "Artist", "album": "Album",
        "track_count": 1, "tracks": [{"title": "Song", "position": 1}],
    }
    if available:
        candidate["artwork"] = {
            "available": True, "source": "cover-art-archive", "entity": "release",
            "mbid": "123e4567-e89b-42d3-a456-426614174000",
            "thumbnail_url": "https://coverartarchive.org/release/123e4567-e89b-42d3-a456-426614174000/front-250",
        }
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [candidate]
    app.config["ARTWORK_FETCHER"] = lambda provider_data: new_art
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    chosen = album["candidates"][0]
    assert (chosen["artwork_url"] is not None) is available
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": chosen["id"],
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json["artwork"]["status"] == ("ready" if available else "unavailable")
    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    images = MediaFile(str(path)).images or []
    if not available:
        assert cover.read_bytes() == old_art and images[0].data == old_art
        assert executed.json["artwork"]["status"] == "unavailable"
    elif existing and not replace:
        assert cover.read_bytes() == old_art and images[0].data == old_art
        assert executed.json["artwork"]["sidecars"]["preserved"]
        assert executed.json["artwork"]["embedded"]["preserved"]
    else:
        assert (cover.exists() and cover.read_bytes() == new_art) is sidecar
        assert (bool(images) and images[0].data == new_art) is embed
    assert path.exists()
    state_before_retry = (
        cover.read_bytes() if cover.exists() else None,
        [image.data for image in (MediaFile(str(path)).images or [])],
    )
    repeated = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert repeated.status_code == 200 and repeated.json["already_complete"] is True
    assert state_before_retry == (
        cover.read_bytes() if cover.exists() else None,
        [image.data for image in (MediaFile(str(path)).images or [])],
    )
    with sqlite3.connect(app.config["APP_DB"]) as database:
        summary = json.loads(database.execute(
            "SELECT summary_json FROM adoption_jobs WHERE id = ?", (executed.json["id"],)
        ).fetchone()[0])
    assert summary["artwork"] == executed.json["artwork"]


def test_artwork_download_failure_precedes_mutation_and_retry_succeeds(tmp_path):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=True)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    original = path.read_bytes()
    candidate = {
        "provider_id": "123e4567-e89b-42d3-a456-426614174000", "artist": "Changed", "album": "Changed",
        "track_count": 1, "tracks": [{"title": "Changed", "position": 1}],
        "artwork": {"available": True, "source": "cover-art-archive", "entity": "release",
                    "mbid": "123e4567-e89b-42d3-a456-426614174000"},
    }
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [candidate]
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = client.get("/api/library-import/reviews").json["albums"][0]
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
    })
    app.config["ARTWORK_FETCHER"] = lambda candidate: (_ for _ in ()).throw(
        LibraryImportExecutionError("mock CAA failure", code="artwork_fetch_failed", status=502)
    )
    failed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert failed.status_code == 502 and failed.json["code"] == "artwork_fetch_failed"
    assert path.read_bytes() == original
    assert not (path.parent / "cover.jpg").exists()
    assert list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()) == []

    replacement = jpeg_bytes((20, 40, 60))
    app.config["ARTWORK_FETCHER"] = lambda candidate: replacement
    retried = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert retried.status_code == 201
    assert (path.parent / "cover.jpg").read_bytes() == replacement
    assert MediaFile(str(path)).images[0].data == replacement


def test_caa_exact_release_precedes_release_group_and_missing_falls_back(monkeypatch):
    calls = []
    responses = {
        "https://coverartarchive.org/release/release-id/": ProviderError("missing", code="release_not_found"),
        "https://coverartarchive.org/release-group/group-id/": {
            "images": [{"front": True, "thumbnails": {"250": "https://coverartarchive.org/release-group/group-id/front-250"}}]
        },
    }
    def fake_request(url, timeout):
        calls.append(url)
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr("beets_mvp.musicbrainz._request_json", fake_request)
    evidence = _cover_art_evidence("release-id", "group-id", 10)
    assert calls == list(responses)
    assert evidence["entity"] == "release-group" and evidence["mbid"] == "group-id"


@pytest.mark.parametrize("failure, expected_status", [
    (ProviderError("missing", code="artwork_not_found", upstream_status=404), "missing"),
    (ProviderError("bad HTML", code="artwork_invalid_response", retryable=True), "lookup-error"),
    (ProviderError("temporary", code="artwork_http_error", retryable=True, upstream_status=503), "lookup-error"),
])
def test_caa_absence_and_failures_are_bounded_optional_evidence(monkeypatch, failure, expected_status):
    monkeypatch.setattr("beets_mvp.musicbrainz._request_json", lambda url, timeout: (_ for _ in ()).throw(failure))

    evidence = _cover_art_evidence("release-id", "group-id", 10)

    assert evidence["available"] is False
    assert evidence["status"] == expected_status
    assert [attempt["entity"] for attempt in evidence["attempts"]] == ["release", "release-group"]
    assert all(len(attempt["mbid"]) <= len("release-id") for attempt in evidence["attempts"])


def test_caa_front_types_fallback_and_shared_group_cache(monkeypatch):
    calls = []

    def fake_request(url, timeout):
        calls.append(url)
        if "/release/" in url:
            raise ProviderError("missing", code="artwork_not_found", upstream_status=404)
        return {"images": [{
            "front": False, "types": ["Front"],
            "thumbnails": {"small": "https://ia800.example.archive.org/items/example/cover.jpg"},
        }]}

    monkeypatch.setattr("beets_mvp.musicbrainz._request_json", fake_request)
    cache = {}
    first = _cover_art_evidence("release-1", "shared-group", 10, cache=cache)
    second = _cover_art_evidence("release-2", "shared-group", 10, cache=cache)

    assert first["entity"] == second["entity"] == "release-group"
    assert first["thumbnail_url"].startswith("https://ia800.example.archive.org/")
    assert calls.count("https://coverartarchive.org/release-group/shared-group/") == 1


def test_caa_cache_reuses_empty_documents_and_errors(monkeypatch):
    calls = []

    def unexpected_request(url, timeout):
        calls.append(url)
        raise AssertionError("cached CAA evidence must not be fetched again")

    monkeypatch.setattr("beets_mvp.musicbrainz._request_json", unexpected_request)
    cached_error = ProviderError("temporary", code="artwork_http_error", retryable=True, upstream_status=503)
    cache = {("release", "empty-release"): {}, ("release-group", "error-group"): cached_error}

    evidence = _cover_art_evidence("empty-release", "error-group", 10, cache=cache)

    assert calls == []
    assert evidence["available"] is False
    assert evidence["status"] == "lookup-error"
    assert evidence["attempts"] == [
        {"entity": "release", "mbid": "empty-release", "status": "missing"},
        {"entity": "release-group", "mbid": "error-group", "status": "lookup-error",
         "code": "artwork_http_error", "upstream_status": 503, "retryable": True},
    ]


def test_caa_invalid_html_response_does_not_discard_valid_musicbrainz_candidate(monkeypatch):
    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size): return self.payload

    def urlopen(request, timeout):
        if "coverartarchive.org" in request.full_url:
            return Response(b"<!DOCTYPE html><title>temporary proxy error</title>")
        if "musicbrainz.org/ws/2/release-group/" in request.full_url:
            return Response(b'{"genres":[]}')
        return Response(
            b'{"id":"release-id","title":"Album","release-group":{"id":"group-id"},'
            b'"artist-credit":[{"name":"Artist"}],"media":[]}'
        )

    monkeypatch.setattr("beets_mvp.musicbrainz.urllib.request.urlopen", urlopen)
    candidate = search_releases({"musicbrainz_id": "release-id"}, limit=1)[0]

    assert candidate["provider_id"] == "release-id"
    assert candidate["artwork"]["available"] is False
    assert candidate["artwork"]["status"] == "lookup-error"
    assert {attempt["code"] for attempt in candidate["artwork"]["attempts"]} == {"artwork_invalid_response"}


def test_artwork_fetch_rejects_untrusted_redirect_and_normalizes_actual_image(monkeypatch):
    handler = _SafeArtworkRedirect()
    with pytest.raises(urllib.error.URLError, match="untrusted"):
        handler.redirect_request(None, None, 302, "Found", {}, "https://example.test/cover.jpg")

    png = BytesIO(); PillowImage.new("RGBA", (4, 4), (10, 20, 30, 120)).save(png, format="PNG")
    seen = {}
    class Response:
        headers = {"Content-Length": str(len(png.getvalue()))}
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size): seen["read_size"] = size; return png.getvalue()
    class Opener:
        def open(self, request, timeout): seen.update(url=request.full_url, timeout=timeout); return Response()
    monkeypatch.setattr("beets_mvp.urllib.request.build_opener", lambda *handlers: Opener())
    normalized = _fetch_and_normalize_artwork({"artwork": {
        "entity": "release", "mbid": "123e4567-e89b-42d3-a456-426614174000",
    }})
    assert seen["url"] == "https://coverartarchive.org/release/123e4567-e89b-42d3-a456-426614174000/front"
    assert seen["timeout"] == 15 and seen["read_size"] == 10 * 1024 * 1024 + 1
    with PillowImage.open(BytesIO(normalized)) as image:
        assert image.format == "JPEG" and image.mode == "RGB" and image.size == (4, 4)


def test_album_decision_candidate_and_track_exception_persist_across_group_sync(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [
        {"provider_id": "release-1", "artist": query["artist"], "album": query["album"], "score": 100}
    ]
    root = tmp_path / "library" / "Artist" / "Album"
    root.mkdir(parents=True)
    for name in ("01 First.mp3", "02 Second.mp3"):
        (root / name).write_bytes(b"song")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = client.get("/api/library-import/reviews").json["albums"][0]
    exception_id = album["tracks"][1]["id"]

    updated = client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved",
        "candidate_id": album["candidates"][0]["id"],
        "track_exceptions": {str(exception_id): "rejected"},
    })

    assert updated.status_code == 200
    assert updated.json["decision"] == "approved"
    assert updated.json["selected_candidate_id"] == album["candidates"][0]["id"]
    assert [track["effective_decision"] for track in updated.json["tracks"]] == ["approved", "rejected"]
    regrouped = client.get("/api/library-import/reviews").json["albums"][0]
    assert next(track for track in regrouped["tracks"] if track["id"] == exception_id)["exception"] == "rejected"
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "track_exceptions": {"999999": "approved"},
    }).status_code == 400
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "duplicate_action": "overwrite",
    }).status_code == 400


def test_album_as_is_selection_uses_null_candidate_without_changing_musicbrainz_results(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": query["artist"], "album": "Canonical Album",
        "tracks": query["tracks"],
    }]
    path = tmp_path / "library" / "Artist" / "Incoming Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate_ids = [candidate["id"] for candidate in album["candidates"]]

    response = client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": None,
    })

    assert response.status_code == 200
    assert response.json["selected_candidate_id"] is None
    assert response.json["selection_mode"] == "as-is"
    assert response.json["current_metadata"] == {
        "artist": "Artist", "album": "Incoming Album", "year": None, "date": None,
        "genre": None, "track_count": 1, "disc_count": 1, "artwork_url": None,
    }
    assert [candidate["id"] for candidate in response.json["candidates"]] == candidate_ids
    assert response.json["proposed_match"]["provider_id"] == "release-1"


def test_match_screen_preview_then_apply_imports_in_place_without_queue_step(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    original = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]

    preview = client.post(
        f'/api/library-import/albums/{album["id"]}/preview', json={"candidate_id": None}
    )

    assert preview.status_code == 200
    assert preview.json["status"] == "preview"
    assert preview.json["selection_mode"] == "as-is"
    assert preview.json["registered"] == 1
    assert list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()) == []
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime

    applied = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})

    assert applied.status_code == 201
    assert client.get("/api/library-import/reviews").json["albums"] == []
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    assert Path(item.path.decode()).resolve() == path.resolve()
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_mtime
    repeated = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert repeated.status_code == 200
    assert repeated.json["already_complete"] is True
    repeated_direct = client.post(
        f'/api/library-import/albums/{album["id"]}/preview', json={"candidate_id": None}
    )
    assert repeated_direct.status_code == 200
    assert repeated_direct.json["already_complete"] is True
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT state, execution_status FROM album_reviews WHERE id = ?", (album["id"],)
        ).fetchone() == ("approved", "complete")
        assert db.execute(
            "SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_preview'"
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_execute'"
        ).fetchone()[0] == 1


def test_match_screen_execute_persists_selection_and_imports_in_one_request(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]

    response = client.post(
        f'/api/library-import/albums/{album["id"]}/execute', json={"candidate_id": None}
    )

    assert response.status_code == 201
    assert response.json["status"] == "complete"
    assert response.json["selection_mode"] == "as-is"
    assert client.get("/api/library-import/reviews").json["albums"] == []
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    assert Path(item.path.decode()).resolve() == path.resolve()
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT state, selected_candidate_id, execution_status FROM album_reviews WHERE id = ?",
            (album["id"],),
        ).fetchone() == ("approved", None, "complete")
        assert db.execute(
            "SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_preview'"
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_execute'"
        ).fetchone()[0] == 1

    repeated = client.post(
        f'/api/library-import/albums/{album["id"]}/execute', json={"candidate_id": None}
    )
    assert repeated.status_code == 200
    assert repeated.json["already_complete"] is True
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT execution_status FROM album_reviews WHERE id = ?", (album["id"],)
        ).fetchone() == ("complete",)
        assert db.execute(
            "SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_execute'"
        ).fetchone()[0] == 1


def test_match_screen_preview_failure_stays_pending_and_audits_error(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    path.write_bytes(path.read_bytes() + b"changed")

    response = client.post(
        f'/api/library-import/albums/{album["id"]}/preview', json={"candidate_id": None}
    )

    assert response.status_code == 409
    assert response.json["code"] == "inventory_stale"
    assert [item["id"] for item in client.get("/api/library-import/reviews").json["albums"]] == [album["id"]]
    assert list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()) == []
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT state, execution_status FROM album_reviews WHERE id = ?", (album["id"],)
        ).fetchone()
        job = db.execute(
            "SELECT status, error FROM adoption_jobs WHERE kind = 'library_import_preview'"
        ).fetchone()
        assert review == ("approved", "not-run")
        assert job[0] == "failed"
        assert "Library file" in job[1]


def test_approved_as_is_execution_registers_in_place_and_honors_track_exceptions(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library" / "Artist" / "Album"
    approved_path = root / "01 Approved.wav"
    rejected_path = root / "02 Rejected.wav"
    write_wav(approved_path)
    write_wav(rejected_path)
    original = approved_path.read_bytes()
    original_mtime = approved_path.stat().st_mtime_ns
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    rejected_id = album["tracks"][1]["id"]
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": None,
        "track_exceptions": {str(rejected_id): "rejected"},
    }).status_code == 200

    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json["selection_mode"] == "as-is"
    assert preview.json["files"] == 1
    assert preview.json["registered"] == 1
    assert preview.json["metadata_updates"] == 0
    assert preview.json["skipped_tracks"] == 1
    assert approved_path.read_bytes() == original
    assert approved_path.stat().st_mtime_ns == original_mtime

    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    items = list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items())
    assert len(items) == 1
    assert Path(items[0].path.decode()).resolve() == approved_path.resolve()
    assert approved_path.read_bytes() == original
    assert approved_path.stat().st_mtime_ns == original_mtime
    assert rejected_path.exists()
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT execution_status FROM album_reviews WHERE id = ?", (album["id"],)).fetchone()[0] == "complete"
        assert db.execute("SELECT status FROM adoption_jobs WHERE kind = 'library_import_execute'").fetchone()[0] == "complete"


def test_approved_candidate_execution_preserves_query_position_and_file_paths(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174000",
        "release_group_id": "group-1", "artist": "Canonical Artist", "album": "Canonical Album",
        "date": "2024-03-02", "year": "2024", "release_type": "Album", "country": "GB",
        "track_count": 2, "media": [{"position": 1, "format": "CD"}],
        "tracks": [
            {"title": "First Song", "position": 1, "medium_position": 1,
             "recording_id": "recording-1"},
            {"title": "Second Song", "position": 2, "medium_position": 1,
             "recording_id": "recording-2"},
        ],
    }]
    root = tmp_path / "library" / "Local Artist" / "Local Album"
    rejected_path = root / "01 First Song.wav"
    path = root / "02 Second Song.wav"
    write_wav(rejected_path)
    write_wav(path)
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate_id = album["candidates"][0]["id"]
    rejected_id = album["tracks"][0]["id"]
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": candidate_id,
        "track_exceptions": {str(rejected_id): "rejected"},
    })
    before_preview = path.read_bytes()
    rejected_before = rejected_path.read_bytes()

    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json["metadata_updates"] == 1
    assert preview.json["files"] == 1
    assert preview.json["items"][0]["path"] == "Local Artist/Local Album/02 Second Song.wav"
    assert preview.json["items"][0]["changes"]["track"]["to"] == 2
    assert preview.json["items"][0]["changes"]["mb_trackid"]["to"] == "recording-2"
    assert path.read_bytes() == before_preview
    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    assert path.exists()
    assert rejected_path.read_bytes() == rejected_before
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    assert Path(item.path.decode()).resolve() == path.resolve()
    assert {field: item.get(field) for field in ("title", "artist", "album", "albumartist", "year", "month", "day")} == {
        "title": "Second Song", "artist": "Canonical Artist", "album": "Canonical Album",
        "albumartist": "Canonical Artist", "year": 2024, "month": 3, "day": 2,
    }
    assert item.get("mb_albumid") == "123e4567-e89b-42d3-a456-426614174000"
    assert item.get("track") == 2
    assert item.get("mb_trackid") == "recording-2"


def test_two_chainz_feature_credit_is_reviewed_planned_and_written_to_db_and_file(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174088",
        "artist": "2 Chainz", "album": "B.O.A.T.S. II #METIME", "track_count": 1,
        "media": [{"position": 1, "format": "CD", "track_count": 1}],
        "tracks": [{
            "title": "Extra", "position": 8, "medium_position": 1, "length_ms": 287000,
            "recording_id": "recording-extra", "track_artist": "2 Chainz feat. Rich Homie Quan",
            "artist_credit_source": "track",
        }],
    }]
    path = tmp_path / "library" / "2 Chainz" / "B.O.A.T.S. II #METIME" / "08 Extra feat. Rich Homie Quan.wav"
    write_wav(path, seconds=288)
    media = MediaFile(str(path))
    media.title = "Extra feat. Rich Homie Quan"
    media.artist = "2 Chainz"
    media.albumartist = "2 Chainz"
    media.album = "B.O.A.T.S. II #METIME"
    media.track = 8
    media.disc = 1
    media.save()
    client = app.test_client()

    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate = album["candidates"][0]
    detail = candidate["track_details"][0]
    assert {key: detail[key] for key in ("local", "proposed", "local_artist", "proposed_artist", "artist_credit_source")} == {
        "local": "Extra feat. Rich Homie Quan", "proposed": "Extra", "local_artist": "2 Chainz",
        "proposed_artist": "2 Chainz feat. Rich Homie Quan", "artist_credit_source": "track",
    }
    assert detail["status"] == "matched"
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": candidate["id"],
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    changes = preview.json["items"][0]["changes"]
    assert changes["title"] == {"from": "Extra feat. Rich Homie Quan", "to": "Extra"}
    assert changes["artist"] == {"from": "2 Chainz", "to": "2 Chainz feat. Rich Homie Quan"}
    assert "albumartist" not in changes

    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    assert (item.title, item.artist, item.albumartist) == (
        "Extra", "2 Chainz feat. Rich Homie Quan", "2 Chainz",
    )
    file_tags = MediaFile(str(path))
    assert (file_tags.title, file_tags.artist, file_tags.albumartist) == (
        "Extra", "2 Chainz feat. Rich Homie Quan", "2 Chainz",
    )


@pytest.mark.parametrize("keep_in_artist", [False, True])
def test_enabled_ftintitle_projection_equals_preview_database_and_file_tags(tmp_path, keep_in_artist):
    app = make_app(tmp_path)
    client = app.test_client()
    config_path = Path(app.config["BEETS_CONFIG"])
    settings = {
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat. {0}",
        "beets_config": config_path.read_text(),
    }
    if keep_in_artist:
        settings["ftintitle_keep_in_artist"] = "1"
    assert client.post("/settings", data=settings).status_code == 302
    configured = yaml.safe_load(config_path.read_text())
    assert "ftintitle" in configured["plugins"]
    assert configured["ftintitle"]["keep_in_artist"] is keep_in_artist

    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174099",
        "artist": "Lead", "album": "Featured Album", "track_count": 1,
        "media": [{"position": 1, "format": "CD", "track_count": 1}],
        "tracks": [{
            "title": "Song", "position": 1, "medium_position": 1, "length_ms": 1000,
            "recording_id": "recording-featured", "track_artist": "Lead feat. Guest",
            "artist_credit_source": "track",
        }],
    }]
    path = tmp_path / "library" / "Lead" / "Featured Album" / "01 Song.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Song"; media.artist = "Lead feat. Guest"; media.albumartist = "Lead"
    media.album = "Featured Album"; media.track = 1; media.disc = 1; media.save()

    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate = album["candidates"][0]
    detail = candidate["track_details"][0]
    expected_artist = "Lead feat. Guest" if keep_in_artist else "Lead"
    assert (detail["proposed"], detail["proposed_artist"]) == ("Song feat. Guest", expected_artist)
    assert candidate["recordings"][0]["title"] == "Song feat. Guest"
    assert candidate["recordings"][0]["track_artist"] == expected_artist
    assert candidate["proposed_diff"]["tracks"]["to"] == ["Song feat. Guest"]

    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": candidate["id"],
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    expected_changes = {"title": {"from": "Song", "to": "Song feat. Guest"}}
    if not keep_in_artist:
        expected_changes["artist"] = {"from": "Lead feat. Guest", "to": "Lead"}
    assert {key: preview.json["items"][0]["changes"][key] for key in expected_changes} == expected_changes

    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    for field in ("path", "changes", "action", "duplicate_of", "target_path"):
        assert executed.json["items"][0][field] == preview.json["items"][0][field]
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    tags = MediaFile(str(path))
    assert (item.title, item.artist, item.albumartist) == ("Song feat. Guest", expected_artist, "Lead")
    assert (tags.title, tags.artist, tags.albumartist) == ("Song feat. Guest", expected_artist, "Lead")


def test_confirmed_release_edition_matches_review_preview_database_file_and_rematch(tmp_path):
    app = make_app(tmp_path)
    app.config["FTINTITLE"] = {
        **app.config["FTINTITLE"], "enabled": True, "format": "feat. {0}", "keep_in_artist": False,
    }

    def provider(query, *, limit):
        return [{
            "provider_id": query.get("musicbrainz_id", "123e4567-e89b-42d3-a456-426614174010"),
            "artist": "Big Sean", "album": "BULLY",
            "release_disambiguation": "deluxe, clean", "track_count": 1,
            "tracks": [{
                "title": "Blessings", "track_artist": "Big Sean feat. Drake",
                "artist_credit_source": "track", "position": 1, "medium_position": 1,
                "length_ms": 1000, "recording_id": "recording-blessings",
            }],
        }]

    app.config["MUSICBRAINZ_PROVIDER"] = provider
    path = tmp_path / "library" / "Big Sean" / "BULLY - DELUXE" / "01 Blessings.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Blessings"; media.artist = "Big Sean feat. Drake"; media.albumartist = "Big Sean"
    media.album = "BULLY - DELUXE"; media.track = media.disc = 1; media.save()
    client = app.test_client()

    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate = album["candidates"][0]
    assert candidate["album"] == "BULLY - DELUXE"
    assert candidate["release_disambiguation"] == "deluxe, clean"
    assert "album" not in candidate["proposed_diff"]
    assert candidate["recordings"][0]["title"] == "Blessings feat. Drake"
    assert candidate["track_details"][0]["proposed"] == "Blessings feat. Drake"
    with sqlite3.connect(app.config["APP_DB"]) as database:
        stored = database.execute(
            "SELECT album, proposed_diff_json, provider_data_json FROM metadata_candidates WHERE id = ?",
            (candidate["id"],),
        ).fetchone()
    raw = json.loads(stored[2])
    assert stored[0] == raw["album"] == "BULLY"
    assert "album" not in json.loads(stored[1])
    assert raw["release_disambiguation"] == "deluxe, clean"

    release_id = "123e4567-e89b-42d3-a456-426614174011"
    rematched = client.post(
        f'/api/library-import/albums/{album["id"]}/rematch', json={"musicbrainz_id": release_id},
    )
    assert rematched.status_code == 200
    candidate = rematched.json["proposed_match"]
    assert candidate["provider_id"] == release_id
    assert candidate["album"] == "BULLY - DELUXE"
    assert candidate["release_disambiguation"] == "deluxe, clean"
    assert "album" not in candidate["proposed_diff"]

    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": candidate["id"],
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    changes = preview.json["items"][0]["changes"]
    assert "album" not in changes
    assert changes["title"] == {"from": "Blessings", "to": "Blessings feat. Drake"}
    assert changes["artist"] == {"from": "Big Sean feat. Drake", "to": "Big Sean"}

    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    assert executed.json["items"][0]["changes"] == changes
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    tags = MediaFile(str(path))
    assert (item.album, item.title, item.artist) == (
        "BULLY - DELUXE", "Blessings feat. Drake", "Big Sean",
    )
    assert (tags.album, tags.title, tags.artist) == (
        "BULLY - DELUXE", "Blessings feat. Drake", "Big Sean",
    )


def test_ftintitle_custom_marker_and_bracket_projection_reaches_final_tags(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    config_path = Path(app.config["BEETS_CONFIG"])
    document = yaml.safe_load(config_path.read_text())
    document["ftintitle"] = {"custom_words": ["alongside"], "bracket_keywords": ["remix"]}
    assert client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat. {0}",
        "beets_config": yaml.safe_dump(document, sort_keys=False),
    }).status_code == 302
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174097",
        "artist": "Lead", "album": "Custom Feature Album", "track_count": 1,
        "tracks": [{
            "title": "Song [Remix]", "position": 1, "medium_position": 1,
            "recording_id": "recording-custom-feature", "track_artist": "Lead alongside Guest",
            "artist_credit_source": "track",
        }],
    }]
    path = tmp_path / "library" / "Lead" / "Custom Feature Album" / "01 Song Remix.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Song [Remix]"; media.artist = "Lead alongside Guest"; media.albumartist = "Lead"
    media.album = "Custom Feature Album"; media.save()

    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    candidate = album["candidates"][0]
    detail = candidate["track_details"][0]
    assert (detail["proposed"], detail["proposed_artist"]) == ("Song feat. Guest [Remix]", "Lead")
    with sqlite3.connect(app.config["APP_DB"]) as database:
        raw = json.loads(database.execute(
            "SELECT provider_data_json FROM metadata_candidates WHERE id = ?", (candidate["id"],)
        ).fetchone()[0])
    assert raw["tracks"][0]["title"] == "Song [Remix]"
    assert raw["tracks"][0]["track_artist"] == "Lead alongside Guest"

    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": candidate["id"],
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    changes = preview.json["items"][0]["changes"]
    assert changes["title"] == {"from": "Song [Remix]", "to": "Song feat. Guest [Remix]"}
    assert changes["artist"] == {"from": "Lead alongside Guest", "to": "Lead"}
    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    assert executed.json["items"][0]["changes"] == changes
    item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    tags = MediaFile(str(path))
    assert (item.title, item.artist, item.albumartist) == ("Song feat. Guest [Remix]", "Lead", "Lead")
    assert (tags.title, tags.artist, tags.albumartist) == ("Song feat. Guest [Remix]", "Lead", "Lead")


def test_enabled_ftintitle_does_not_transform_as_is_import(tmp_path):
    app = make_app(tmp_path)
    app.config["FTINTITLE"] = {**app.config["FTINTITLE"], "enabled": True}
    path = tmp_path / "library" / "Lead" / "As Is Album" / "01 Song.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Song"; media.artist = "Lead feat. Guest"; media.albumartist = "Lead"
    media.album = "As Is Album"; media.save()
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": None,
    }).status_code == 200
    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    assert preview.status_code == 200
    assert preview.json["selection_mode"] == "as-is"
    assert preview.json["items"][0]["changes"] == {}
    assert client.post(f'/api/library-import/albums/{album["id"]}/execute', json={}).status_code == 201
    tags = MediaFile(str(path))
    assert (tags.title, tags.artist, tags.albumartist) == ("Song", "Lead feat. Guest", "Lead")


@pytest.mark.parametrize("duplicate_action", [None, "skip", "keep-both", "merge", "replace"])
def test_duplicate_decision_is_persisted_previewed_and_enforced(tmp_path, duplicate_action):
    app = make_app(tmp_path)
    managed_path = tmp_path / "library" / "Managed" / "Old Album" / "01 Song.wav"
    incoming_path = tmp_path / "library" / "Incoming Artist" / "Incoming Album" / "01 Song.wav"
    write_wav(managed_path)
    write_wav(incoming_path)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    managed = Item(
        title="Song", artist="Managed", albumartist="Managed", album="Old Album",
        mb_trackid="recording-1", path=str(managed_path),
    )
    library.add(managed)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": query["artist"], "album": query["album"], "track_count": 1,
        "tracks": [{"title": "Song", "position": 1, "medium_position": 1, "recording_id": "recording-1"}],
    }]
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = next(
        album for album in client.get("/api/library-import/reviews").json["albums"]
        if album["album"] == "Incoming Album"
    )
    assert album["duplicate_preflight"]["status"] == "possible"
    assert album["duplicate_preflight"]["count"] == 1
    assert album["duplicate_preflight"]["warnings"] == [{
        "incoming_path": "Incoming Artist/Incoming Album/01 Song.wav",
        "recording_id": "recording-1",
        "managed_paths": ["Managed/Old Album/01 Song.wav"],
        "ambiguous": False,
    }]
    assert album["candidates"][0]["duplicate_preflight"] == album["duplicate_preflight"]
    patch = {"decision": "approved", "candidate_id": album["candidates"][0]["id"]}
    if duplicate_action:
        patch["duplicate_action"] = duplicate_action
    saved = client.patch(f'/api/library-import/albums/{album["id"]}', json=patch)
    assert saved.status_code == 200
    assert saved.json["duplicate_action"] == duplicate_action

    preview = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={"dry_run": True})
    if duplicate_action is None:
        assert preview.status_code == 409
        assert preview.json["code"] == "duplicate_decision_required"
        return
    assert preview.status_code == 200
    assert preview.json["duplicate_action"] == duplicate_action
    assert preview.json["duplicates"] == 1
    assert preview.json["items"][0]["action"] == duplicate_action

    incoming_before = incoming_path.read_bytes()
    executed = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert executed.status_code == 201
    items = list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items())
    if duplicate_action == "keep-both":
        assert len(items) == 2
    else:
        assert len(items) == 1
    if duplicate_action == "replace":
        assert Path(items[0].path.decode()).resolve() == incoming_path.resolve()
        assert managed_path.exists()
    elif duplicate_action == "keep-both":
        assert {Path(item.path.decode()).resolve() for item in items} == {managed_path.resolve(), incoming_path.resolve()}
    else:
        assert Path(items[0].path.decode()).resolve() == managed_path.resolve()
    if duplicate_action in {"skip", "merge"}:
        assert incoming_path.read_bytes() == incoming_before
    with sqlite3.connect(app.config["APP_DB"]) as db:
        identities = dict(db.execute("SELECT relative_path, beets_item_id FROM library_inventory"))
    incoming_relative = "Incoming Artist/Incoming Album/01 Song.wav"
    managed_relative = "Managed/Old Album/01 Song.wav"
    if duplicate_action in {"skip", "merge"}:
        assert identities[incoming_relative] is None
    elif duplicate_action == "keep-both":
        assert identities[incoming_relative] != identities[managed_relative]
    else:
        assert identities[incoming_relative] == managed.id
        assert identities[managed_relative] is None


@pytest.mark.parametrize("duplicate_action, expected_target", [("merge", "managed"), ("replace", "incoming")])
def test_duplicate_artwork_targets_effective_managed_file(tmp_path, duplicate_action, expected_target):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=True)
    artwork = jpeg_bytes((22, 44, 66))
    app.config["ARTWORK_FETCHER"] = lambda candidate: artwork
    managed_path = tmp_path / "library" / "Managed" / "Old Album" / "01 Song.wav"
    incoming_path = tmp_path / "library" / "Incoming" / "New Album" / "01 Song.wav"
    write_wav(managed_path)
    write_wav(incoming_path)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item(title="Song", artist="Managed", album="Old Album", mb_trackid="recording-1",
                     path=str(managed_path)))
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174000", "artist": query["artist"],
        "album": query["album"], "track_count": 1,
        "tracks": [{"title": "Song", "position": 1, "recording_id": "recording-1"}],
        "artwork": {"available": True, "source": "cover-art-archive", "entity": "release",
                    "mbid": "123e4567-e89b-42d3-a456-426614174000"},
    }]
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = next(item for item in client.get("/api/library-import/reviews").json["albums"] if item["artist"] == "Incoming")
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
        "duplicate_action": duplicate_action,
    })
    result = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert result.status_code == 201
    target = managed_path if expected_target == "managed" else incoming_path
    untouched = incoming_path if expected_target == "managed" else managed_path
    assert (target.parent / "cover.jpg").read_bytes() == artwork
    assert MediaFile(str(target)).images[0].data == artwork
    assert not (untouched.parent / "cover.jpg").exists()
    assert not MediaFile(str(untouched)).images
    assert result.json["artwork"]["embedded"]["written"] == [
        target.relative_to(tmp_path / "library").as_posix()
    ]


def test_duplicate_merge_rejects_ambiguous_managed_recording_identity(tmp_path):
    app = make_app(tmp_path)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    for number in (1, 2):
        path = tmp_path / "library" / f"Managed {number}" / "Album" / "01 Song.wav"
        write_wav(path)
        library.add(Item(title="Song", artist=f"Managed {number}", album="Album",
                         mb_trackid="recording-1", path=str(path)))
    incoming = tmp_path / "library" / "Incoming" / "Album" / "01 Song.wav"
    write_wav(incoming)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": query["artist"], "album": query["album"], "track_count": 1,
        "tracks": [{"title": "Song", "position": 1, "recording_id": "recording-1"}],
    }]
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = next(album for album in client.get("/api/library-import/reviews").json["albums"] if album["artist"] == "Incoming")
    assert album["duplicate_preflight"]["status"] == "ambiguous"
    assert album["duplicate_preflight"]["warnings"][0]["ambiguous"] is True
    assert len(album["duplicate_preflight"]["warnings"][0]["managed_paths"]) == 2
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"], "duplicate_action": "merge",
    })

    response = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})

    assert response.status_code == 409
    assert response.json["code"] == "duplicate_ambiguous"
    assert len(list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items())) == 2


@pytest.mark.parametrize("change", ["changed", "missing"])
def test_library_execution_rejects_files_changed_since_inventory(tmp_path, change):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album = client.get("/api/library-import/reviews").json["albums"][0]
    client.patch(f'/api/library-import/albums/{album["id"]}', json={"decision": "approved", "candidate_id": None})
    if change == "changed":
        path.write_bytes(path.read_bytes() + b"changed")
    else:
        path.unlink()

    response = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    assert response.status_code == 409
    assert response.json["code"] == "inventory_stale"
    assert list(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()) == []
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT execution_status, execution_error, last_execution_job_id FROM album_reviews WHERE id = ?",
            (album["id"],),
        ).fetchone()
        assert review[0] == "failed"
        assert "Library file" in review[1]
        assert db.execute("SELECT status FROM adoption_jobs WHERE id = ?", (review[2],)).fetchone()[0] == "failed"


def test_library_execution_rerun_is_idempotent(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album = client.get("/api/library-import/reviews").json["albums"][0]
    client.patch(f'/api/library-import/albums/{album["id"]}', json={"decision": "approved", "candidate_id": None})
    first = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})
    item_id = first.json["items"][0]["beets_item_id"]
    pending = client.get("/api/library-import/reviews").json
    assert pending["items"] == []
    assert pending["albums"] == []
    assert pending["total_albums"] == 0
    second = client.post(f'/api/library-import/albums/{album["id"]}/execute', json={})

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json["already_complete"] is True
    assert second.json["items"][0]["beets_item_id"] == item_id
    assert [item.id for item in Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()] == [item_id]
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM adoption_jobs WHERE kind = 'library_import_execute'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM library_inventory").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM adoption_reviews WHERE imported_at IS NOT NULL").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM album_reviews WHERE execution_status = 'complete'").fetchone()[0] == 1
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.get("/api/library-import/reviews").json["albums"] == []


def test_inbox_route_owns_named_inbox_workspace_not_library_import_queue(tmp_path):
    client = make_app(tmp_path).test_client()
    page = client.get("/inbox")

    assert page.status_code == 200
    assert b"<title>Inbox \xc2\xb7 Cratekeep</title>" in page.data
    assert b"<h1>Named inboxes</h1>" in page.data
    assert b'href="/inbox" aria-current="page"' in page.data
    assert b'id="inbox-grid"' in page.data and b'data-review-modal' in page.data
    assert b'id="library-import-review-list"' not in page.data
    settings = client.get("/settings")
    assert b'id="library-import-review-list"' in settings.data


def test_library_route_is_album_first_searchable_and_lazy(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    tracks = [
        Item(title="Beta Song", artist="Artist B", albumartist="Artist B", album="Shared", genre="Rock", year=2024,
             path=str(root / "b.mp3")),
        Item(title="Alpha Song", artist="Artist A", albumartist="Artist A", album="First", genre="Jazz", year=2023,
             path=str(root / "a.mp3")),
        Item(title="Other Song", artist="Artist A", albumartist="Artist A", album="First", genre="Jazz", year=2023,
             path=str(root / "other.mp3")),
    ]
    for item in tracks:
        Path(item.path.decode()).write_bytes(b"audio")
    first_album = library.add_album([tracks[1], tracks[2]])
    shared_album = library.add_album([tracks[0]])
    stale = Item(
        title="0 Missing Song", artist="Artist A", albumartist="Artist A", album="First",
        genre="Jazz", year=2023, path=str(root / "missing.mp3"), album_id=first_album.id,
    )
    library.add(stale)
    with sqlite3.connect(app.config["BEETS_DB"]) as database:
        for item in [*tracks, stale]:
            database.execute("UPDATE items SET path = ? WHERE id = ?",
                             (os.fsencode(Path(item.path.decode()).name), item.id))
    cover = root / "cover.jpg"
    cover.write_bytes(jpeg_bytes((20, 40, 80)))
    original_read_bytes = Path.read_bytes
    import beets_mvp
    original_normalize_artwork = beets_mvp._normalize_artwork_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: (
        pytest.fail("Library landing read full artwork bytes") if path == cover
        else original_read_bytes(path)
    ))
    monkeypatch.setattr("beets_mvp._normalize_artwork_bytes", lambda payload: pytest.fail(
        "Library landing decoded artwork"
    ))
    original_discovery = beets_mvp._managed_album_artwork_source
    discoveries = []

    def counted_discovery(current_app, items):
        discoveries.append(tuple(item.id for item in items))
        return original_discovery(current_app, items)

    monkeypatch.setattr(beets_mvp, "_managed_album_artwork_source", counted_discovery)

    client = app.test_client()
    page = client.get("/library")
    assert page.status_code == 200
    assert b'aria-current="page"' in page.data
    assert b"<span>Tracks</span><strong>4</strong>" in page.data
    assert b"<span>Albums</span><strong>2</strong>" in page.data
    assert b"<span>Artists</span><strong>2</strong>" in page.data
    assert b"<span>Library size</span><strong>\xe2\x80\x94</strong>" in page.data
    assert len(discoveries) == 2
    assert b"Top artists" in page.data and b"Recently added albums" in page.data
    assert b'name="q" type="search"' in page.data
    assert b'name="sort"' in page.data and b'name="order"' in page.data
    browse = page.data.split(b'id="browse-title"', 1)[1]
    assert b"Browse albums" in page.data
    assert b"Alpha Song" not in browse and b"Beta Song" not in browse
    assert b'action="/api/items/' not in page.data
    assert f'data-album-id="{first_album.id}"'.encode() in page.data
    assert f'data-album-id="{shared_album.id}"'.encode() in page.data
    assert b'data-album-cover' in page.data and b'/artwork' in page.data
    assert b'id="choose-artwork"' in page.data and b'id="refresh-artwork"' in page.data

    filtered = client.get("/library?q=artist+b&sort=album&order=desc")
    browse = filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"Shared" in browse and b'data-album-title>First<' not in browse
    assert b'option value="album" selected' in filtered.data
    assert b'option value="desc" selected' in filtered.data

    artist_filtered = client.get("/library?q=song&sort=album&order=desc&artist=Artist+A")
    artist_browse = artist_filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"First" in artist_browse and b"Shared" not in artist_browse
    assert b'name="artist" value="Artist A"' in artist_filtered.data

    album_filtered = client.get("/library?sort=album&order=asc&album=Shared")
    album_browse = album_filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"Shared" in album_browse and b'data-album-title>First<' not in album_browse
    assert b'name="album" value="Shared"' in album_filtered.data

    detail = client.get(f"/api/library/albums/{first_album.id}")
    assert detail.status_code == 200
    stale_payload = next(track for track in detail.json["tracks"] if track["id"] == stale.id)
    assert stale_payload["bytes"] is None
    assert all(
        track["bytes"] == len(b"audio")
        for track in detail.json["tracks"] if track["id"] != stale.id
    )
    monkeypatch.setattr(Path, "read_bytes", original_read_bytes)
    monkeypatch.setattr(beets_mvp, "_normalize_artwork_bytes", original_normalize_artwork)
    assert client.get(f"/api/library/albums/{first_album.id}/artwork").status_code == 200
    valid_before = (root / "a.mp3").read_bytes()
    blocked = client.patch(f"/api/library/albums/{first_album.id}/tracks/{stale.id}", json={
        "title": "Must not write", "expected_fingerprint": detail.json["fingerprint"],
    })
    assert blocked.status_code == 409 and blocked.json["code"] == "managed_path_invalid"
    assert library.get_item(stale.id).title == "0 Missing Song"
    assert (root / "a.mp3").read_bytes() == valid_before


def test_large_library_landing_file_work_is_bounded_to_visible_album_page(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    for number in range(80):
        path = root / f"Artist {number:03d}" / f"Album {number:03d}" / "01 Song.wav"
        write_wav(path)
        library.add_album([Item(
            title="Song", artist=f"Artist {number:03d}", albumartist=f"Artist {number:03d}",
            album=f"Album {number:03d}", path=str(path),
        )])
    original_stat = Path.stat
    audio_stats = []
    def counted_stat(path, *args, **kwargs):
        if path.suffix == ".wav":
            audio_stats.append(path)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "stat", counted_stat)

    page = app.test_client().get("/library")

    assert page.status_code == 200
    assert b"Showing 1\xe2\x80\x9325 of 80 matching albums" in page.data
    assert len(set(audio_stats)) <= 30
    assert len(audio_stats) <= 150


def managed_wav_album(app, tmp_path, *, artist="Artist", album_name="Album", titles=("First", "Second")):
    items = []
    for number, title in enumerate(titles, 1):
        path = tmp_path / "library" / artist / album_name / f"{number:02d} {title}.wav"
        write_wav(path)
        media = MediaFile(str(path))
        media.title, media.artist, media.albumartist, media.album = title, artist, artist, album_name
        media.genre, media.year, media.track, media.disc = "Rock", 2020, number, 1
        media.save()
        item = Item.from_path(path)
        item.albumartist = artist
        items.append(item)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    return library, library.add_album(items), paths_for_items(items)


def paths_for_items(items):
    return [Path(item.path.decode()) for item in items]


def test_managed_album_and_track_edits_preserve_identity_paths_and_tags(tmp_path):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path)
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    original_tracks = copy.deepcopy(opened["tracks"])
    original_paths = [path.resolve() for path in paths]

    edited = client.patch(f"/api/library/albums/{album.id}", json={
        "album": "New Album", "albumartist": "New Album Artist", "genre": "Jazz", "year": 2024,
        "expected_fingerprint": opened["fingerprint"],
    })
    assert edited.status_code == 200
    assert edited.json["album"]["fingerprint"] != opened["fingerprint"]
    for before, after in zip(original_tracks, edited.json["album"]["tracks"]):
        assert (after["album"], after["albumartist"], after["genre"], after["year"]) == (
            "New Album", "New Album Artist", "Jazz", 2024,
        )
        assert (after["title"], after["artist"], after["track"], after["disc"]) == (
            before["title"], before["artist"], before["track"], before["disc"],
        )
    assert [Path(item.path.decode()).resolve() for item in library.get_album(album.id).items()] == original_paths
    for path in paths:
        media = MediaFile(str(path))
        assert (media.album, media.albumartist, media.genre, media.year) == (
            "New Album", "New Album Artist", "Jazz", 2024,
        )

    second_id = edited.json["album"]["tracks"][1]["id"]
    track_edit = client.patch(f"/api/library/albums/{album.id}/tracks/{second_id}", json={
        "title": "Changed Second", "artist": "Guest", "album": "New Album",
        "albumartist": "New Album Artist", "genre": "Jazz", "year": 2024, "track": 2, "disc": 1,
        "expected_fingerprint": edited.json["album"]["fingerprint"],
    })
    assert track_edit.status_code == 200
    assert [track["title"] for track in track_edit.json["album"]["tracks"]] == ["First", "Changed Second"]
    assert MediaFile(str(paths[0])).title == "First"
    assert (MediaFile(str(paths[1])).title, MediaFile(str(paths[1])).artist) == ("Changed Second", "Guest")
    assert [Path(item.path.decode()).resolve() for item in library.get_album(album.id).items()] == original_paths

    assert client.patch(f"/api/library/albums/{album.id}", json={
        "genre": "Soul", "expected_fingerprint": opened["fingerprint"],
    }).status_code == 409
    assert client.patch(f"/api/library/albums/{album.id}", json={
        "title": "Nope", "expected_fingerprint": track_edit.json["album"]["fingerprint"],
    }).status_code == 400
    assert client.patch(f"/api/library/albums/{album.id}", json={
        "year": "not-an-int", "expected_fingerprint": track_edit.json["album"]["fingerprint"],
    }).status_code == 400
    assert client.patch(f"/api/library/albums/{album.id}", json={
        "year": 2024.5, "expected_fingerprint": track_edit.json["album"]["fingerprint"],
    }).status_code == 400
    assert client.patch(f"/api/library/albums/{album.id}/tracks/999999", json={
        "title": "Nope", "expected_fingerprint": track_edit.json["album"]["fingerprint"],
    }).status_code == 404


def test_managed_album_edit_rolls_back_first_file_and_database_on_second_write_failure(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path)
    opened = app.test_client().get(f"/api/library/albums/{album.id}").json
    original = [(MediaFile(str(path)).album, path.read_bytes()) for path in paths]
    original_write = Item.write
    calls = 0

    def fail_second_write(item, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected second-file failure")
        return original_write(item, *args, **kwargs)

    monkeypatch.setattr(Item, "write", fail_second_write)
    response = app.test_client().patch(f"/api/library/albums/{album.id}", json={
        "album": "Should Roll Back", "expected_fingerprint": opened["fingerprint"],
    })
    assert response.status_code == 500
    assert response.json["code"] == "mutation_rolled_back"
    refreshed = library.get_album(album.id)
    assert [item.album for item in refreshed.items()] == ["Album", "Album"]
    assert [MediaFile(str(path)).album for path in paths] == [value[0] for value in original]
    assert [Path(item.path.decode()).resolve() for item in refreshed.items()] == [path.resolve() for path in paths]


def test_managed_album_exact_mbid_preview_apply_is_stored_single_use_and_non_mutating(tmp_path):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(
        app, tmp_path, album_name="BULLY - DELUXE", titles=("First Song", "Second Song")
    )
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    provider_calls = []

    def provider(query, *, limit):
        provider_calls.append((query["musicbrainz_id"], limit))
        return [{
            "provider_id": release_id, "release_group_id": "group-1", "artist": "Artist",
            "album": "BULLY", "date": "2024-03-01", "year": "2024",
            "release_disambiguation": "Deluxe", "country": "US", "release_type": "Album",
            "track_count": 2, "media": [{"position": 1, "format": "CD"}],
            "tracks": [
                {"title": "First Song Remastered", "track_artist": "Artist", "position": 1,
                 "medium_position": 1, "recording_id": "recording-1"},
                {"title": "Second Song", "track_artist": "Artist", "position": 2,
                 "medium_position": 1, "recording_id": "recording-2"},
            ],
            "retrieval": {"source": "release-id", "search_score": None},
        }]

    app.config["MUSICBRAINZ_PROVIDER"] = provider
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    before_db = [(item.title, item.mb_trackid, Path(item.path.decode()).resolve()) for item in album.items()]
    before_files = [path.read_bytes() for path in paths]
    preview = client.post(f"/api/library/albums/{album.id}/rematch/preview", json={
        "musicbrainz_id": release_id, "expected_fingerprint": opened["fingerprint"],
    })
    assert preview.status_code == 201
    assert preview.json["applicable"] is True
    assert preview.json["candidate"]["album"] == "BULLY - DELUXE"
    assert preview.json["candidate"]["raw_album"] == "BULLY"
    assert preview.json["changes"][0]["changes"]["title"]["to"] == "First Song Remastered"
    assert [(item.title, item.mb_trackid, Path(item.path.decode()).resolve()) for item in album.items()] == before_db
    assert [path.read_bytes() for path in paths] == before_files
    with sqlite3.connect(app.config["APP_DB"]) as db:
        evidence, status = db.execute(
            "SELECT evidence_json, status FROM managed_album_rematches WHERE id = ?",
            (preview.json["preview_id"],),
        ).fetchone()
    assert json.loads(evidence)["album"] == "BULLY"
    assert status == "pending"

    applied = client.post(f"/api/library/albums/{album.id}/rematch/apply", json={
        "preview_id": preview.json["preview_id"], "expected_fingerprint": preview.json["fingerprint"],
    })
    assert applied.status_code == 200
    assert applied.json["changes"] == preview.json["changes"]
    assert provider_calls == [(release_id, 1)]
    assert [item.title for item in library.get_album(album.id).items()] == ["First Song Remastered", "Second Song"]
    assert [item.mb_trackid for item in library.get_album(album.id).items()] == ["recording-1", "recording-2"]
    assert [MediaFile(str(path)).title for path in paths] == ["First Song Remastered", "Second Song"]
    assert [Path(item.path.decode()).resolve() for item in library.get_album(album.id).items()] == [path.resolve() for path in paths]
    assert client.post(f"/api/library/albums/{album.id}/rematch/apply", json={
        "preview_id": preview.json["preview_id"], "expected_fingerprint": preview.json["fingerprint"],
    }).status_code == 409


def test_managed_artwork_upload_and_exact_refresh_preview_apply_preserve_metadata_paths(tmp_path):
    app = make_app(tmp_path)
    app.config.update(ART_SIDECAR=True, ART_EMBED=True)
    library, album, paths = managed_wav_album(app, tmp_path)
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    album.mb_albumid = release_id
    album.store()
    for item in album.items():
        item.mb_albumid = release_id
        item.store()
    provider_calls = []
    refreshed_jpeg = jpeg_bytes((10, 120, 80))
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: provider_calls.append(
        (query["musicbrainz_id"], limit)
    ) or [{
        "provider_id": release_id, "artist": "Artist", "album": "Album", "tracks": [],
        "artwork": {"available": True, "source": "cover-art-archive",
                    "entity": "release", "mbid": release_id},
    }]
    app.config["ARTWORK_FETCHER"] = lambda candidate: refreshed_jpeg
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    original_paths = [path.resolve() for path in paths]
    original_metadata = [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths]

    uploaded = client.post(f"/api/library/albums/{album.id}/artwork/upload-preview", data={
        "expected_fingerprint": opened["fingerprint"],
        "artwork": (BytesIO(jpeg_bytes((160, 20, 40))), "cover.png"),
    })
    assert uploaded.status_code == 201
    assert client.get(uploaded.json["preview_url"]).status_code == 200
    replacement_preview = client.post(f"/api/library/albums/{album.id}/artwork/upload-preview", data={
        "expected_fingerprint": opened["fingerprint"],
        "artwork": (BytesIO(jpeg_bytes((160, 20, 40))), "cover.png"),
    })
    assert replacement_preview.status_code == 201
    assert client.get(uploaded.json["preview_url"]).status_code == 404
    assert client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": uploaded.json["preview_id"], "expected_fingerprint": opened["fingerprint"],
    }).status_code == 409
    uploaded = replacement_preview
    assert not (paths[0].parent / "cover.jpg").exists()
    assert all(not (MediaFile(str(path)).images or []) for path in paths)
    applied = client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": uploaded.json["preview_id"], "expected_fingerprint": opened["fingerprint"],
    })
    assert applied.status_code == 200
    assert applied.json["sidecars_written"] == 1 and applied.json["tracks_embedded"] == 2
    uploaded_bytes = (paths[0].parent / "cover.jpg").read_bytes()
    assert all((MediaFile(str(path)).images or [])[0].data == uploaded_bytes for path in paths)
    assert client.get(f"/api/library/albums/{album.id}/artwork").status_code == 200

    before_refresh = client.get(f"/api/library/albums/{album.id}").json
    refresh = client.post(f"/api/library/albums/{album.id}/artwork/refresh-preview", json={
        "expected_fingerprint": before_refresh["fingerprint"],
    })
    assert refresh.status_code == 201
    assert refresh.json["release_id"] == release_id and provider_calls == [(release_id, 1)]
    assert (paths[0].parent / "cover.jpg").read_bytes() == uploaded_bytes
    sidecar = paths[0].parent / "cover.jpg"
    externally_changed = uploaded_bytes + b"external artwork change"
    sidecar.write_bytes(externally_changed)
    refreshed = client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": refresh.json["preview_id"],
        "expected_fingerprint": before_refresh["fingerprint"],
    })
    assert refreshed.status_code == 409 and refreshed.json["code"] == "stale_artwork"
    assert sidecar.read_bytes() == externally_changed
    refresh = client.post(f"/api/library/albums/{album.id}/artwork/refresh-preview", json={
        "expected_fingerprint": before_refresh["fingerprint"],
    })
    assert refresh.status_code == 201 and provider_calls == [(release_id, 1), (release_id, 1)]
    refreshed = client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": refresh.json["preview_id"],
        "expected_fingerprint": before_refresh["fingerprint"],
    })
    assert refreshed.status_code == 200
    assert sidecar.read_bytes() == refreshed_jpeg
    assert all((MediaFile(str(path)).images or [])[0].data == refreshed_jpeg for path in paths)
    assert [Path(item.path.decode()).resolve() for item in library.get_album(album.id).items()] == original_paths
    assert [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths] == original_metadata


def test_managed_artwork_failure_rolls_back_and_rejects_sidecar_symlink(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app.config.update(ART_SIDECAR=True, ART_EMBED=True)
    _, album, paths = managed_wav_album(app, tmp_path)
    original_jpeg = jpeg_bytes((20, 40, 80))
    sidecar = paths[0].parent / "cover.jpg"
    sidecar.write_bytes(original_jpeg)
    for path in paths:
        media = MediaFile(str(path))
        media.images = [MediaImage(original_jpeg, desc="Original", type=ImageType.front)]
        media.save()
    original_paths = [path.resolve() for path in paths]
    original_file_bytes = [path.read_bytes() for path in paths]
    original_metadata = [
        (media.title, media.artist, media.album, media.albumartist, media.genre,
         media.year, media.track, media.disc)
        for media in (MediaFile(str(path)) for path in paths)
    ]
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    preview = client.post(f"/api/library/albums/{album.id}/artwork/upload-preview", data={
        "expected_fingerprint": opened["fingerprint"],
        "artwork": (BytesIO(jpeg_bytes((180, 140, 20))), "replacement.jpg"),
    }).json
    original_save = MediaFile.save
    calls = 0

    def fail_second_save(media, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected artwork write failure")
        return original_save(media, *args, **kwargs)

    monkeypatch.setattr(MediaFile, "save", fail_second_save)
    failed = client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": preview["preview_id"], "expected_fingerprint": opened["fingerprint"],
    })
    assert failed.status_code == 500 and failed.json["code"] == "artwork_mutation_rolled_back"
    assert sidecar.read_bytes() == original_jpeg
    assert [path.read_bytes() for path in paths] == original_file_bytes
    assert all((MediaFile(str(path)).images or [])[0].data == original_jpeg for path in paths)
    assert [
        (media.title, media.artist, media.album, media.albumartist, media.genre,
         media.year, media.track, media.disc)
        for media in (MediaFile(str(path)) for path in paths)
    ] == original_metadata
    assert [path.resolve() for path in paths] == original_paths
    assert not list(paths[0].parent.glob(".*.cratekeep-art-*.bak"))
    assert client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": preview["preview_id"], "expected_fingerprint": opened["fingerprint"],
    }).status_code == 409

    monkeypatch.setattr(MediaFile, "save", original_save)
    outside = tmp_path / "outside-cover.jpg"
    outside.write_bytes(b"outside must remain untouched")
    sidecar.unlink()
    sidecar.symlink_to(outside)
    current = client.get(f"/api/library/albums/{album.id}").json
    unsafe_preview = client.post(f"/api/library/albums/{album.id}/artwork/upload-preview", data={
        "expected_fingerprint": current["fingerprint"],
        "artwork": (BytesIO(jpeg_bytes((1, 2, 3))), "unsafe.jpg"),
    }).json
    rejected = client.post(f"/api/library/albums/{album.id}/artwork/apply", json={
        "preview_id": unsafe_preview["preview_id"],
        "expected_fingerprint": current["fingerprint"],
    })
    assert rejected.status_code == 409 and rejected.json["code"] == "managed_path_invalid"
    assert outside.read_bytes() == b"outside must remain untouched"


def test_managed_album_rematch_rolls_back_on_second_tag_failure(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path)
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": release_id, "artist": "Artist", "album": "Changed Album", "year": "2025",
        "track_count": 2, "tracks": [
            {"title": "First Updated", "track_artist": "Artist", "position": 1,
             "medium_position": 1, "recording_id": "new-1"},
            {"title": "Second Updated", "track_artist": "Artist", "position": 2,
             "medium_position": 1, "recording_id": "new-2"},
        ], "retrieval": {"source": "release-id"},
    }]
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    preview = client.post(f"/api/library/albums/{album.id}/rematch/preview", json={
        "musicbrainz_id": release_id, "expected_fingerprint": opened["fingerprint"],
    }).json
    assert preview["applicable"] is True
    original_write = Item.write
    calls = 0

    def fail_second_write(item, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected rematch tag failure")
        return original_write(item, *args, **kwargs)

    monkeypatch.setattr(Item, "write", fail_second_write)
    failed = client.post(f"/api/library/albums/{album.id}/rematch/apply", json={
        "preview_id": preview["preview_id"], "expected_fingerprint": preview["fingerprint"],
    })
    assert failed.status_code == 500 and failed.json["code"] == "mutation_rolled_back"
    assert [(item.title, item.album, item.mb_trackid) for item in library.get_album(album.id).items()] == [
        ("First", "Album", ""), ("Second", "Album", ""),
    ]
    assert [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths] == [
        ("First", "Album"), ("Second", "Album"),
    ]
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT status FROM managed_album_rematches WHERE id = ?", (preview["preview_id"],)
        ).fetchone()[0] == "pending"


def test_managed_album_rematch_rolls_back_when_audit_claim_is_lost(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path)
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": release_id, "artist": "Artist", "album": "Audited Album", "year": "2026",
        "track_count": 2, "tracks": [
            {"title": "Audited First", "track_artist": "Artist", "position": 1,
             "medium_position": 1, "recording_id": "audit-1"},
            {"title": "Audited Second", "track_artist": "Artist", "position": 2,
             "medium_position": 1, "recording_id": "audit-2"},
        ], "retrieval": {"source": "release-id"},
    }]
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    preview = client.post(f"/api/library/albums/{album.id}/rematch/preview", json={
        "musicbrainz_id": release_id, "expected_fingerprint": opened["fingerprint"],
    }).json
    original_db = [(item.title, item.album, item.mb_trackid, Path(item.path.decode()).resolve())
                   for item in library.get_album(album.id).items()]
    original_files = [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths]

    import beets_mvp
    original_finalize = beets_mvp._finalize_managed_rematch

    def lose_audit_claim(target_app, preview_id, result):
        with sqlite3.connect(target_app.config["APP_DB"]) as db:
            db.execute(
                "UPDATE managed_album_rematches SET status = 'pending' WHERE id = ? AND status = 'applying'",
                (preview_id,),
            )
        original_finalize(target_app, preview_id, result)

    monkeypatch.setattr(beets_mvp, "_finalize_managed_rematch", lose_audit_claim)
    failed = client.post(f"/api/library/albums/{album.id}/rematch/apply", json={
        "preview_id": preview["preview_id"], "expected_fingerprint": preview["fingerprint"],
    })
    assert failed.status_code == 500 and failed.json["code"] == "preview_audit_failed"
    assert [(item.title, item.album, item.mb_trackid, Path(item.path.decode()).resolve())
            for item in library.get_album(album.id).items()] == original_db
    assert [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths] == original_files
    with sqlite3.connect(app.config["APP_DB"]) as db:
        row = db.execute(
            "SELECT status, result_json, applied_at FROM managed_album_rematches WHERE id = ?",
            (preview["preview_id"],),
        ).fetchone()
    assert row == ("pending", None, None)


def test_managed_album_mismatched_mbid_preview_cannot_apply(tmp_path):
    app = make_app(tmp_path)
    _, album, paths = managed_wav_album(app, tmp_path)
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": release_id, "artist": "Artist", "album": "Album", "track_count": 1,
        "tracks": [{"title": "First", "track_artist": "Artist", "position": 1,
                    "medium_position": 1, "recording_id": "recording-1"}],
        "retrieval": {"source": "release-id"},
    }]
    client = app.test_client()
    opened = client.get(f"/api/library/albums/{album.id}").json
    before = [path.read_bytes() for path in paths]
    preview = client.post(f"/api/library/albums/{album.id}/rematch/preview", json={
        "musicbrainz_id": release_id, "expected_fingerprint": opened["fingerprint"],
    })
    assert preview.status_code == 201
    assert preview.json["applicable"] is False
    assert preview.json["inapplicable_reasons"]
    apply = client.post(f"/api/library/albums/{album.id}/rematch/apply", json={
        "preview_id": preview.json["preview_id"], "expected_fingerprint": preview.json["fingerprint"],
    })
    assert apply.status_code == 409 and apply.json["code"] == "preview_not_applicable"
    assert [path.read_bytes() for path in paths] == before


def test_library_album_pagination_clamps_and_has_no_gaps(tmp_path):
    app = make_app(tmp_path)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    ids = []
    for number in range(26):
        path = tmp_path / "library" / f"Artist {number % 2}" / f"Album {number:02d}" / "track.wav"
        write_wav(path)
        ids.append(library.add_album([Item(
            title=f"Hidden Song {number:02d}", artist=f"Artist {number % 2}",
            albumartist=f"Artist {number % 2}", album=f"Album {number:02d}", year=2000 + number,
            track=1, disc=1, path=str(path),
        )]).id)
    client = app.test_client()
    first = client.get("/library?sort=album&order=asc")
    second = client.get("/library?sort=album&order=asc&page=2")
    clamped = client.get("/library?sort=album&order=asc&page=999")
    first_ids = re.findall(rb'data-album-id="(\d+)"', first.data)
    second_ids = re.findall(rb'data-album-id="(\d+)"', second.data)
    # Recently-added controls repeat five IDs outside Browse; isolate its section.
    first_browse = first.data.split(b'id="browse-title"', 1)[1]
    second_browse = second.data.split(b'id="browse-title"', 1)[1]
    first_ids = re.findall(rb'class="album-card open-album" type="button" data-album-id="(\d+)"', first_browse)
    second_ids = re.findall(rb'class="album-card open-album" type="button" data-album-id="(\d+)"', second_browse)
    assert len(first_ids) == 25 and len(second_ids) == 1
    assert len(set(first_ids + second_ids)) == 26
    assert b"Page 2 of 2" in clamped.data
    assert b"Hidden Song" not in first_browse


def test_library_import_promotes_wholly_ungrouped_tracks_to_stable_beets_album(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Imported Artist" / "Imported Album" / "01 Song.wav"
    tagged_wav(path, artist="Imported Artist", album="Imported Album", title="Song")
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    review = client.get("/api/library-import/reviews").json["albums"][0]
    assert client.post(
        f'/api/library-import/albums/{review["id"]}/preview', json={"candidate_id": None}
    ).status_code == 200
    executed = client.post(f'/api/library-import/albums/{review["id"]}/execute', json={})
    assert executed.status_code == 201
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    albums = list(library.albums())
    assert len(albums) == 1
    assert [item.id for item in albums[0].items()] == [executed.json["items"][0]["beets_item_id"]]
    assert f'data-album-id="{albums[0].id}"'.encode() in client.get("/library").data


def test_library_promotes_pre_feature_complete_review_without_touching_files(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = Path(app.config["LIBRARY_PATH"])
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    paths, items = [], []
    for number, title in enumerate(("First", "Second"), 1):
        path = root / "Legacy Artist" / "Legacy Album" / f"{number:02d} {title}.wav"
        tagged_wav(path, artist="Legacy Artist", album="Legacy Album", title=title)
        media = MediaFile(str(path)); media.track = number; media.save()
        item = Item.from_path(path)
        library.add(item)
        paths.append(path)
        items.append(item)
    assert all(item.album_id is None for item in items)
    before_bytes = [path.read_bytes() for path in paths]
    before_tags = [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths]
    before_paths = [path.resolve() for path in paths]
    now = "2026-10-08T00:00:00+00:00"
    with sqlite3.connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('fixture', ?, 'complete', ?)",
            (str(root.resolve()), now),
        ).lastrowid
        review_id = db.execute(
            """INSERT INTO album_reviews(root_path, artist_key, album_key, artist, album, state,
                      execution_status, updated_at)
               VALUES (?, 'legacy artist', 'legacy album', 'Legacy Artist', 'Legacy Album',
                       'approved', 'complete', ?)""",
            (str(root.resolve()), now),
        ).lastrowid
        for path, item in zip(paths, items):
            stat = path.stat()
            inventory_id = db.execute(
                """INSERT INTO library_inventory(root_path, relative_path, size_bytes, mtime_ns, device, inode,
                          beets_item_id, present, first_seen_job_id, last_seen_job_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (str(root.resolve()), str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns,
                 stat.st_dev, stat.st_ino, item.id, job_id, job_id),
            ).lastrowid
            db.execute("INSERT INTO album_review_tracks(album_review_id, inventory_id) VALUES (?, ?)",
                       (review_id, inventory_id))

    def forbid_tag_write(*args, **kwargs):
        raise AssertionError("legacy promotion must not write file tags")

    monkeypatch.setattr(Item, "write", forbid_tag_write)
    client = app.test_client()
    first = client.get("/library")
    assert first.status_code == 200
    albums = list(library.albums())
    assert len(albums) == 1
    assert sorted(item.id for item in albums[0].items()) == sorted(item.id for item in items)
    assert first.data.count(b'class="album-card open-album"') == 1
    detail = client.get(f"/api/library/albums/{albums[0].id}")
    assert detail.status_code == 200
    assert [track["title"] for track in detail.json["tracks"]] == ["First", "Second"]
    assert client.get("/library").status_code == 200
    assert len(list(library.albums())) == 1
    assert [path.read_bytes() for path in paths] == before_bytes
    assert [(MediaFile(str(path)).title, MediaFile(str(path)).album) for path in paths] == before_tags
    assert [Path(item.path.decode()).resolve() for item in library.items()] == before_paths


def test_legacy_fallback_keeps_same_title_editions_in_separate_directories(tmp_path):
    app = make_app(tmp_path)
    root = Path(app.config["LIBRARY_PATH"])
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    fixtures = (
        ("Edition A", "release-a", "One", 1),
        ("Edition A", "release-a", "Two", 2),
        ("Edition B", "release-b", "One", 1),
    )
    original_paths = []
    for directory, release_id, title, track in fixtures:
        path = root / "Artist" / directory / f"{track:02d} {title}.wav"
        tagged_wav(path, artist="Artist", album="Shared Title", title=title)
        media = MediaFile(str(path)); media.track = track; media.mb_albumid = release_id; media.save()
        library.add(Item.from_path(path))
        original_paths.append(path.resolve())

    page = app.test_client().get("/library")
    albums = list(library.albums())
    assert page.status_code == 200
    assert len(albums) == 2
    assert sorted(len(list(album.items())) for album in albums) == [1, 2]
    assert page.data.count(b'class="album-card open-album"') == 2
    assert sorted(Path(item.path.decode()).resolve() for item in library.items()) == sorted(original_paths)
    assert app.test_client().get("/library").data.count(b'class="album-card open-album"') == 2
    assert len(list(library.albums())) == 2


def test_album_modal_payload_includes_proposal_reasons_diff_and_track_details(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174000",
        "artist": "Artist", "album": "Renamed Album", "year": "2024", "score": 96,
        "tracks": ["First", "Changed title"], "track_count": 2,
    }]
    root = tmp_path / "library" / "Artist" / "Album"
    root.mkdir(parents=True)
    (root / "01 First.mp3").write_bytes(b"first")
    (root / "02 Second.mp3").write_bytes(b"second")
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    album = client.get("/api/library-import/reviews").json["albums"][0]

    assert album["proposed_match"]["provider_id"] == "123e4567-e89b-42d3-a456-426614174000"
    assert album["proposed_match"]["proposed_diff"]["album"] == {"from": "Album", "to": "Renamed Album"}
    assert all("search score" not in reason.lower() for reason in album["proposed_match"]["confidence_reasons"])
    assert album["proposed_match"]["recommendation"] in {"strong", "medium", "low", "none"}
    assert [track["status"] for track in album["proposed_match"]["track_details"]] == ["matched", "unmatched"]
    assert [track["title"] for track in album["tracks"]] == ["First", "Second"]


def test_track_title_quote_variants_do_not_create_candidate_changes():
    query = {"artist": "Artist", "album": "Album", "tracks": [
        {"title": "Don't Stop"}, {"title": 'He Said "Go"'},
    ]}
    typographic = {"artist": "Artist", "album": "Album", "tracks": [
        {"title": "Don’t Stop"}, {"title": "He Said “Go”"},
    ]}

    assert "tracks" not in _candidate_diff(query, typographic)
    assert [detail["status"] for detail in score_release(query, typographic)["track_details"]] == ["matched", "matched"]

    meaningful = {**typographic, "tracks": [{"title": "Doesn’t Stop"}, {"title": "He Said “Go”"}]}
    assert _candidate_diff(query, meaningful)["tracks"]["to"][0] == "Doesn’t Stop"


def test_album_track_comparison_carries_current_track_duration(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item(title="Song", artist="Artist", album="Album", path=str(path), length=187.25))
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": "Artist", "album": "Album",
        "tracks": [{"title": "Song", "position": 1, "duration": 190}],
    }]
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    album = client.get("/api/library-import/reviews").json["albums"][0]

    assert album["tracks"][0]["duration"] == pytest.approx(187.25)
    assert album["candidates"][0]["track_details"][0]["local_duration"] == pytest.approx(187.25)
    assert album["candidates"][0]["track_details"][0]["proposed_duration"] == 190


def test_album_track_comparison_measures_duration_from_audio_file(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    path.parent.mkdir(parents=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 16000)
    original_bytes = path.read_bytes()
    original_mtime = path.stat().st_mtime_ns
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": "Artist", "album": "Album",
        "tracks": [{"title": "Song", "position": 1, "duration": 7}],
    }]
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    album = client.get("/api/library-import/reviews").json["albums"][0]

    assert album["tracks"][0]["duration"] == pytest.approx(2.0)
    assert album["candidates"][0]["track_details"][0]["local_duration"] == pytest.approx(2.0)
    assert album["candidates"][0]["track_details"][0]["proposed_duration"] == 7
    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime_ns == original_mtime


def test_musicbrainz_id_override_is_strict_and_uses_injected_provider(tmp_path):
    calls = []
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: calls.append((query, limit)) or [{
        "provider_id": query["musicbrainz_id"], "artist": query["artist"], "album": query["album"],
        "score": 100, "tracks": query["tracks"],
    }]
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album_id = client.get("/api/library-import/reviews").json["albums"][0]["id"]

    for body in ({}, {"musicbrainz_id": "not-a-uuid"}, {
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000", "extra": True,
    }, {"musicbrainz_id": " 123e4567-e89b-42d3-a456-426614174000"}):
        assert client.post(f"/api/library-import/albums/{album_id}/rematch", json=body).status_code == 400
    assert calls == []

    response = client.post(f"/api/library-import/albums/{album_id}/rematch", json={
        "musicbrainz_id": "123E4567-E89B-42D3-A456-426614174000",
    })

    assert response.status_code == 200
    assert response.json["selected_candidate_id"] is None
    assert response.json["decision"] == "pending"
    assert response.json["proposed_match"]["confidence"] == 1.0
    assert response.json["proposed_match"]["recommendation"] == "strong"
    assert response.json["proposed_match"]["selected_by_mbid"] is True
    assert calls[0][0]["musicbrainz_id"] == "123e4567-e89b-42d3-a456-426614174000"
    assert calls[0][1] == 1

    # Canonical UUID text is not limited to the older RFC version/variant bit patterns.
    response = client.post(f"/api/library-import/albums/{album_id}/rematch", json={
        "musicbrainz_id": "AAAAAAAA-AAAA-8AAA-7AAA-AAAAAAAAAAAA",
    })
    assert response.status_code == 200
    assert calls[1][0]["musicbrainz_id"] == "aaaaaaaa-aaaa-8aaa-7aaa-aaaaaaaaaaaa"


def test_release_group_mbid_returns_all_bounded_editions_without_selecting_one(tmp_path):
    group_id = "123e4567-e89b-42d3-a456-426614174100"
    release_ids = ["123e4567-e89b-42d3-a456-426614174101", "123e4567-e89b-42d3-a456-426614174102"]
    calls = []
    app = make_app(tmp_path)
    def provider(query, *, limit):
        calls.append((dict(query), limit))
        if "musicbrainz_id" in query:
            return []
        return [{
            "provider_id": release_id, "release_group_id": group_id,
            "artist": "Artist", "album": "Album", "year": str(2020 + index),
            "tracks": query["tracks"],
        } for index, release_id in enumerate(release_ids, 1)]
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album_id = client.get("/api/library-import/reviews").json["albums"][0]["id"]

    response = client.post(
        f"/api/library-import/albums/{album_id}/rematch", json={"musicbrainz_id": group_id},
    )

    assert response.status_code == 200
    assert response.json["decision"] == "pending"
    assert response.json["selected_candidate_id"] is None
    assert [candidate["provider_id"] for candidate in response.json["candidates"][:2]] == release_ids
    assert all(candidate["retrieval"]["source"] == "release-group-id" for candidate in response.json["candidates"][:2])
    assert calls[0][0]["musicbrainz_id"] == group_id and calls[0][1] == 1
    assert calls[1][0]["musicbrainz_release_group_id"] == group_id and calls[1][1] == 10


def test_rematch_distinguishes_invalid_id_missing_release_and_provider_failure(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Alec Benjamin" / "(Un)Commentary" / "01 Dopamine Addict.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album_id = client.get("/api/library-import/reviews").json["albums"][0]["id"]
    endpoint = f"/api/library-import/albums/{album_id}/rematch"

    invalid = client.post(endpoint, json={"musicbrainz_id": "not-a-uuid"})
    assert invalid.status_code == 400
    assert invalid.json["code"] == "invalid_musicbrainz_id"

    missing_album = client.post("/api/library-import/albums/999999/rematch", json={
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000",
    })
    assert missing_album.status_code == 404
    assert missing_album.json["code"] == "album_review_not_found"

    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: []
    missing = client.post(endpoint, json={
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000",
    })
    assert missing.status_code == 404
    assert missing.json == {
        "code": "musicbrainz_id_not_found",
        "error": "No MusicBrainz release or release group exists for that ID.",
        "retryable": False,
    }

    def failed_provider(query, *, limit):
        raise ProviderError(
            "MusicBrainz returned HTTP 500",
            retryable=True,
            code="provider_http_error",
            upstream_status=500,
        )

    app.config["MUSICBRAINZ_PROVIDER"] = failed_provider
    failed = client.post(endpoint, json={
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000",
    })
    assert failed.status_code == 503
    assert failed.json == {
        "code": "provider_http_error",
        "error": "MusicBrainz returned HTTP 500",
        "retryable": True,
        "upstream_status": 500,
    }


def test_rematch_rejects_provider_returning_a_different_release(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "123e4567-e89b-42d3-a456-426614174001",
    }]
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album_id = client.get("/api/library-import/reviews").json["albums"][0]["id"]

    response = client.post(f"/api/library-import/albums/{album_id}/rematch", json={
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000",
    })

    assert response.status_code == 502
    assert response.json["code"] == "provider_invalid_response"


def test_album_review_actions_and_rematch_are_metadata_only(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": query["musicbrainz_id"], "artist": query["artist"], "album": query["album"], "score": 100,
    }]
    path = tmp_path / "library" / "Artist" / "Album" / "Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"do-not-touch")
    original = path.read_bytes()
    monkeypatch.setattr("beets_mvp.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not execute")))
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert client.post(f'/api/library-import/albums/{album["id"]}/rematch', json={
        "musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000",
    }).status_code == 200
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={"decision": "approved"}).status_code == 200
    assert path.exists() and path.read_bytes() == original


def test_musicbrainz_http_seam_bounds_limit_and_maps_rate_limit(monkeypatch):
    seen = {"urls": []}

    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size):
            seen["read_size"] = size
            return self.payload

    def urlopen(request, timeout):
        seen["urls"].append(request.full_url)
        seen["timeout"] = timeout
        if "musicbrainz.org/ws/2/release-group/group-1" in request.full_url:
            return Response(b'{"genres":[{"name":"Rock","count":4},{"name":"Pop","count":2}]}')
        if "coverartarchive.org/release/id-1" in request.full_url:
            return Response(b'{"images":[{"front":true,"thumbnails":{"250":"https://coverartarchive.org/release/id-1/front-250"}}]}')
        if "/release/id-1" in request.full_url:
            return Response(b'{"id":"id-1","title":"Album","date":"2024-03-02","country":"GB","release-group":{"id":"group-1","primary-type":"Album"},"artist-credit":[{"name":"Artist"}],"media":[{"position":1,"format":"CD","track-count":1,"tracks":[{"position":1,"number":"1","length":123000,"recording":{"id":"recording-1","title":"Song"}}]}]}')
        return Response(b'{"releases": [{"id": "id-1", "score": 99}]}')

    monkeypatch.setattr("beets_mvp.musicbrainz.time.sleep", lambda delay: None)
    monkeypatch.setattr("beets_mvp.musicbrainz.urllib.request.urlopen", urlopen)
    result = search_releases({"artist": "Artist", "album": "Album"}, limit=100)
    assert result[0]["provider_id"] == "id-1"
    assert result[0]["release_group_id"] == "group-1"
    assert result[0]["release_type"] == "Album"
    assert result[0]["country"] == "GB"
    assert result[0]["tracks"][0] == {"medium_position": 1, "position": 1, "number": "1", "title": "Song", "recording_id": "recording-1", "length_ms": 123000,
                                        "track_artist": "Artist", "artist_credit_source": "release"}
    assert result[0]["retrieval"] == {"search_score": 99, "source": "musicbrainz-search"}
    assert result[0]["genre"] == "Rock"
    assert result[0]["genre_evidence"]["counts"] == [{"name": "Rock", "count": 4}, {"name": "Pop", "count": 2}]
    assert result[0]["artwork"]["entity"] == "release"
    assert any("/release/id-1" in url for url in seen["urls"])
    assert seen == {**seen, "timeout": 10, "read_size": 512 * 1024}

    def limited(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 429, "limited", {}, None)

    monkeypatch.setattr("beets_mvp.musicbrainz.urllib.request.urlopen", limited)
    try:
        search_releases({"artist": "Artist", "album": "Album"})
    except ProviderError as exc:
        assert exc.retryable is True
        assert "rate limit" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_musicbrainz_track_artist_credit_precedence_joinphrases_and_fallbacks():
    release = _normalize_release({
        "id": "release", "title": "Album", "artist-credit": [{"name": "Release Artist"}],
        "media": [{"tracks": [
            {"title": "Track credit", "artist-credit": [
                {"name": "Lead", "joinphrase": " feat. "},
                {"artist": {"name": "Guest"}, "joinphrase": " & "}, {"name": "Other"},
            ], "recording": {"title": "Track credit", "artist-credit": [{"name": "Ignored"}]}},
            {"title": "Recording credit", "artist-credit": [], "recording": {
                "title": "Recording credit", "artist-credit": [{"name": "Recorder", "joinphrase": " with "}, {"name": "Guest"}],
            }},
            {"title": "Malformed", "artist-credit": [{"joinphrase": " feat. "}, None],
             "recording": {"title": "Malformed", "artist-credit": "not-a-list"}},
            {"title": "Release fallback", "recording": {"title": "Release fallback"}},
        ]}],
    }, exact=True)

    assert [(track["track_artist"], track["artist_credit_source"]) for track in release["tracks"]] == [
        ("Lead feat. Guest & Other", "track"),
        ("Recorder with Guest", "recording"),
        ("Release Artist", "release"),
        ("Release Artist", "release"),
    ]
    assert _artist_credit_text([{"artist": {"name": "Fallback Name"}}]) == "Fallback Name"
    assert _artist_credit_text([None, "bad", {"name": ""}]) == ""
    assert _artist_credit_text([{"name": "Partial", "joinphrase": " feat. "}, None]) == ""


@pytest.mark.parametrize("exact", [False, True])
def test_musicbrainz_release_parser_preserves_whitespace_normalized_disambiguation(exact):
    release = _normalize_release({
        "id": "release", "title": "Dark Sky Paradise",
        "disambiguation": "  deluxe,   clean  ",
        "artist-credit": [{"name": "Big Sean"}], "media": [],
    }, exact=exact)

    assert release["album"] == "Dark Sky Paradise"
    assert release["release_disambiguation"] == "deluxe, clean"


def test_musicbrainz_http_404_is_a_release_not_found_error(monkeypatch):
    def missing(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr("beets_mvp.musicbrainz.urllib.request.urlopen", missing)
    try:
        search_releases({"musicbrainz_id": "123e4567-e89b-42d3-a456-426614174000"}, limit=1)
    except ProviderError as exc:
        assert exc.code == "release_not_found"
        assert exc.upstream_status == 404
        assert exc.retryable is False
        assert "release-group ID" in str(exc)
    else:
        raise AssertionError("expected ProviderError")


def test_matching_assigns_tracks_reports_explicit_mismatches_and_bounds_count_confidence():
    query = {"artist": "Artist", "album": "Album", "year": 2024, "tracks": [
        {"title": "First", "duration": 120, "disc": 1, "track": 1},
        {"title": "Second", "duration": 180, "disc": 1, "track": 2},
    ]}
    candidate = {"provider_id": "release", "artist": "Artist", "album": "Album", "year": 2024, "tracks": [
        {"title": "Second", "length_ms": 181000, "medium_position": 1, "position": 1, "recording_id": "r2"},
        {"title": "First renamed", "length_ms": 150000, "medium_position": 1, "position": 2, "recording_id": "r1"},
        {"title": "Bonus", "length_ms": 90000, "medium_position": 1, "position": 3, "recording_id": "r3"},
    ], "retrieval": {"search_score": 100, "source": "musicbrainz-search"}}

    match = score_release(query, candidate)

    statuses = {detail["status"] for detail in match["track_details"]}
    issues = {issue for detail in match["track_details"] for issue in detail["issues"]}
    paired = next(detail for detail in match["track_details"] if detail["local"] == "First")
    assert paired["local_duration"] == 120
    assert paired["proposed_duration"] == 150
    assert paired["current_position"] == [1, 1]
    assert paired["proposed_position"] == [1, 2]
    assert "order-mismatch" in statuses
    assert {"title-mismatch", "duration-mismatch", "order-mismatch", "extra"} <= issues
    assert match["track_count_mismatch"] is True
    assert match["confidence"] < .95
    assert match["recommendation"] in {"strong", "medium", "low", "none"}
    assert {penalty["field"] for penalty in match["penalties"]} == {"artist", "album", "date", "tracks"}


@pytest.mark.parametrize("local_title", [
    "Extra feat. Rich Homie Quan",
    "EXTRA FT RICH HOMIE QUAN",
    "Extra (featuring Rich Homie Quan)",
    "Extra [Feat Rich Homie Quan]",
    "Extra — ft. Rich Homie Quan",
])
def test_matching_pairs_trailing_feature_credits_without_hiding_title_changes(local_title):
    query = {"artist": "2 Chainz", "album": "B.O.A.T.S. II #METIME", "tracks": [{
        "title": local_title, "duration": 288, "disc": 1, "track": 8,
    }]}
    candidate = {"artist": "2 Chainz", "album": "B.O.A.T.S. II #METIME", "tracks": [{
        "title": "Extra", "length_ms": 287000, "medium_position": 1, "position": 8,
    }]}

    result = score_release(query, candidate)
    detail = result["track_details"][0]

    assert detail["local"] == local_title
    assert detail["proposed"] == "Extra"
    assert detail["status"] in {"matched", "title-mismatch"}
    assert not {"unmatched", "missing", "extra"}.intersection(detail["issues"])
    assert "unmatched_tracks" not in result["hard_mismatches"]
    assert result["matched_track_count"] == 1
    assert _candidate_diff(query, candidate)["tracks"] == {
        "from": [local_title], "to": ["Extra"],
    }


@pytest.mark.parametrize("local_title, local_artist, expected, source", [
    ("Extra feat. Rich Homie Quan", "2 Chainz", "2 Chainz feat. Rich Homie Quan", "local-title-fallback"),
    ("Extra ft. Rich Homie Quan", "2 Chainz feat. Rich Homie Quan", "2 Chainz feat. Rich Homie Quan", "local-title-fallback"),
    ("Extra featuring Rich Homie Quan", "2 Chainz & Rich Homie Quan", "2 Chainz & Rich Homie Quan", "local-title-fallback"),
    ("Extra", "2 Chainz", "2 Chainz", "release"),
    ("Extra feat. 2 Chainz", "2 Chainz", "2 Chainz", "local-title-fallback"),
    ("Extra feat. X", "2 Chainz", "2 Chainz feat. X", "local-title-fallback"),
    ("Extra feat. Rich Homie Quan (Live)", "2 Chainz", "2 Chainz", "release"),
    ("Extra feat. Rich Homie Quan [Remix]", "2 Chainz", "2 Chainz", "release"),
    ("Extra (feat. Rich Homie Quan]", "2 Chainz", "2 Chainz", "release"),
])
def test_track_artist_conservative_local_feature_fallback(local_title, local_artist, expected, source):
    result = score_release(
        {"artist": "2 Chainz", "album": "Album", "tracks": [{"title": local_title, "artist": local_artist}]},
        {"artist": "2 Chainz", "album": "Album", "tracks": [{
            "title": "Extra", "track_artist": "2 Chainz", "artist_credit_source": "release",
        }]},
    )
    detail = result["track_details"][0]
    assert detail["proposed_artist"] == expected
    assert detail["artist_credit_source"] == source


def test_explicit_musicbrainz_track_credit_overrides_local_feature_fallback():
    detail = score_release(
        {"artist": "2 Chainz", "album": "Album", "tracks": [{
            "title": "Extra feat. Wrong Guest", "artist": "2 Chainz feat. Wrong Guest",
        }]},
        {"artist": "2 Chainz", "album": "Album", "tracks": [{
            "title": "Extra", "track_artist": "2 Chainz feat. Rich Homie Quan",
            "artist_credit_source": "recording",
        }]},
    )["track_details"][0]
    assert detail["proposed_artist"] == "2 Chainz feat. Rich Homie Quan"
    assert detail["artist_credit_source"] == "recording"


@pytest.mark.parametrize("local_title, canonical_title", [
    ("I Do It feat. Lil Wayne", "I Do It"),
    ("Netflix ft. Fergie", "Netflix"),
    ("Beautiful Pain (featuring Ma$e)", "Beautiful Pain"),
])
def test_nearby_feature_credit_tracks_pair_and_keep_raw_titles(local_title, canonical_title):
    result = score_release(
        {"artist": "2 Chainz", "album": "Album", "tracks": [{"title": local_title}]},
        {"artist": "2 Chainz", "album": "Album", "tracks": [{"title": canonical_title}]},
    )

    assert result["track_details"][0]["status"] in {"matched", "title-mismatch"}
    assert result["track_details"][0]["local"] == local_title
    assert result["track_details"][0]["proposed"] == canonical_title
    assert "unmatched_tracks" not in result["hard_mismatches"]


@pytest.mark.parametrize("left, right", [
    ("Featuring Artist", "Artist"),
    ("Feat. of Strength", "Strength"),
    ("The Featuring", "The"),
    ("Song feat. Artist (Live)", "Song"),
    ("A Feature Presentation", "A"),
    ("Song (feat. Artist]", "Song"),
    ("Song [ft. Artist)", "Song"),
])
def test_feature_words_outside_a_trailing_credit_do_not_match(left, right):
    result = score_release(
        {"artist": "Artist", "album": "Album", "tracks": [{"title": left}]},
        {"artist": "Artist", "album": "Album", "tracks": [{"title": right}]},
    )

    detail = result["track_details"][0]
    assert detail["distance"] > 0
    assert "title-mismatch" in detail["issues"]
    assert result["confidence"] < 1


def test_exact_release_id_selects_but_does_not_override_unrelated_evidence():
    result = score_release({"artist": "Wrong", "album": "Wrong", "tracks": [{"title": "One"}]}, {
        "provider_id": "release", "artist": "Canonical", "album": "Canonical", "tracks": [],
    }, exact_mbid=True)

    assert result["confidence"] == .02
    assert result["distance"] == .98
    assert result["recommendation"] == "none"
    assert result["selected_by_mbid"] is True
    assert {"artist", "album", "zero_matched_tracks", "track_count", "unmatched_tracks"} <= set(result["hard_mismatches"])
    assert result["reasons"][0] == "Selected by exact MusicBrainz release ID; confidence remains evidence-based"


def test_exact_release_id_with_exact_metadata_and_tracks_is_strong():
    query = {"artist": "Artist", "album": "Album", "year": 2024, "tracks": [
        {"title": "One", "duration": 120, "disc": 1, "track": 1},
        {"title": "Two", "duration": 180, "disc": 1, "track": 2},
    ]}
    candidate = {"provider_id": "release", "artist": "Artist", "album": "Album", "year": 2024, "tracks": [
        {"title": "One", "length_ms": 120000, "medium_position": 1, "position": 1},
        {"title": "Two", "length_ms": 180000, "medium_position": 1, "position": 2},
    ], "retrieval": {"source": "release-id", "search_score": None}}

    result = score_release(query, candidate, exact_mbid=True)

    assert result["confidence"] == 1.0
    assert result["recommendation"] == "strong"
    assert result["matched_track_count"] == 2
    assert result["hard_mismatches"] == []


def test_exact_release_id_partial_track_count_mismatch_is_penalized_and_visible():
    result = score_release({"artist": "Artist", "album": "Album", "tracks": [
        {"title": "One"}, {"title": "Two"},
    ]}, {"provider_id": "release", "artist": "Artist", "album": "Album", "tracks": [
        {"title": "One"},
    ]}, exact_mbid=True)

    assert result["confidence"] < 1.0
    assert result["track_count_mismatch"] is True
    assert result["matched_track_count"] == 1
    assert result["unmatched_track_count"] == 1
    assert {"track_count", "unmatched_tracks"} <= set(result["hard_mismatches"])
    assert any(reason.startswith("Track count differs") for reason in result["reasons"])
    assert any(reason.startswith("Unmatched tracks:") for reason in result["reasons"])


def test_rematch_wrong_release_id_stays_selected_with_low_evidence_confidence(tmp_path):
    app = make_app(tmp_path)
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": release_id, "artist": "Other Performer", "album": "Foreign Recording",
        "tracks": [], "retrieval": {"source": "release-id", "search_score": 100},
    }]
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    album_id = client.get("/api/library-import/reviews").json["albums"][0]["id"]

    response = client.post(f"/api/library-import/albums/{album_id}/rematch", json={"musicbrainz_id": release_id})

    assert response.status_code == 200
    candidate = response.json["proposed_match"]
    assert response.json["selected_candidate_id"] is None
    assert response.json["decision"] == "pending"
    assert candidate["provider_id"] == release_id
    assert candidate["artist"] == "Other Performer"
    assert candidate["retrieval"] == {"search_score": None, "source": "release-id"}
    assert candidate["confidence"] == .02
    assert candidate["recommendation"] == "none"


def test_library_import_review_markup_is_album_folder_first(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    shared = Path("beets_mvp/static/review-presentation.js").read_bytes()
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()

    assert b"document.createElement('details')" in shared
    assert b"document.createElement('summary')" in shared
    assert b"review-folder" in html and b"review-album" in html
    assert b"CratekeepFolderWorkflow.create" in html
    assert b"adapter.albums(item.path)" in folders
    assert b"review.open(album, open)" in folders
    assert b"reviews?limit=50" not in html


def test_library_import_review_rows_are_unfilled_and_keep_focus_contract(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    shell_css = Path("beets_mvp/static/app-shell.css").read_bytes()

    assert b".review-list { overflow:hidden;" in html
    assert b".review-folder-summary { display:grid; grid-template-columns:auto auto minmax(0,1fr) auto auto;" in shell_css
    assert b".review-album-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto auto;" in shell_css
    assert b":is(a, button, input, select, summary):focus-visible { outline: 3px solid var(--focus);" in html
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()
    assert b"const article = make('article'" in folders
    assert b"const open = make('button', '\xe2\x86\x97'" in folders
    assert b"open.setAttribute('aria-label', `Review ${album.album} by ${album.artist}`)" in folders


def test_library_import_review_modal_is_album_scoped_and_accessible(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    shared = Path("beets_mvp/static/review-presentation.js").read_bytes()

    assert b'data-review-candidates' in html
    assert b'id="library-import-modal-proposed"' not in html
    assert b'id="library-import-modal-tracks"' not in html
    assert b'role="radiogroup" aria-label="Album metadata candidates"' in html
    assert b"summary.setAttribute('role','radio')" in shared
    assert b"label:'Release Type/media'" in shared
    assert b"label:'Tracks changed'" in shared
    assert b"Not applicable \xe2\x80\x94 As Is" in shared
    assert b"Math.round(candidate.confidence" not in html
    assert b"container.querySelector('[aria-checked=\"true\"]')?.focus()" in shared
    assert b'data-review-open-mbid' in html
    assert b'data-review-mbid-modal' in html
    assert b'id="library-import-rematch-form"' not in html
    assert b'.override-form' not in html
    assert b'aria-modal="true" aria-labelledby="shared-library-review-title"' in html
    controller = Path("beets_mvp/static/review-workflow.js").read_bytes()
    assert b"if (event.key === 'Escape')" in controller
    assert b"input:not(:disabled)" in controller
    assert b"async function open(value, trigger)" in controller


def test_inventory_rows_render_compact_literal_paths_and_track_pills(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()

    assert b"item.name || item.path" in folders
    assert b"item.path === '.' ? 'Root-level loose audio'" in folders
    assert b"review-source-metadata" not in html
    assert b"'review-track-count'" in folders
    assert b"renderTrackList" not in html
    assert b"adapter.statusClass || 'status-pill'" in folders
    assert b"Page ${page} of ${pages}" in folders
    assert b'aria-label="Previous page"' in html and b'aria-label="Next page"' in html


def test_album_folder_line_carries_review_status_score_without_track_expansion(tmp_path):
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()

    assert b"row.append(identity, count, state, open)" in folders
    assert b"review.open(album, open)" in folders
    assert b"renderTrackList" not in folders


def test_album_review_multi_select_keeps_sequential_single_album_flow(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b'id="folder-select-page" type="checkbox"' in html
    assert b'id="library-import-review-exceptions"' in html
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()
    controller = Path("beets_mvp/static/review-workflow.js").read_bytes()
    assert b"const selected = new Set()" in folders
    assert b"async function exceptions()" in folders
    assert b"review.openSequence" in folders
    assert b"await fetch(`/api/library-import/albums/${nextId}`)" not in html
    assert b"queue.shift()" in controller
    assert b"listRequest?.abort()" in folders
    assert b"sequence !== listSequence" in folders


def test_folder_queue_page_two_bounds_readiness_and_groups_inventory_once(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = Path(app.config["LIBRARY_PATH"])
    folder_count = 1050
    for number in range(folder_count):
        (root / f"Folder {number:04d}").mkdir()
    root_text = str(root.resolve())
    now = "2026-10-06T00:00:00+00:00"
    with sqlite3.connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('fixture', ?, 'complete', ?)",
            (root_text, now),
        ).lastrowid
        for number in range(folder_count):
            album_id = number + 1
            inventory_id = db.execute(
                """INSERT INTO library_inventory(root_path, relative_path, size_bytes, mtime_ns, device, inode,
                          present, first_seen_job_id, last_seen_job_id) VALUES (?, ?, 1, 1, 1, ?, 1, ?, ?)""",
                (root_text, f"Folder {number:04d}/01 Track.wav", album_id, job_id, job_id),
            ).lastrowid
            db.execute(
                """INSERT INTO album_reviews(id, root_path, artist_key, album_key, artist, album, state,
                          candidate_status, candidate_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'pending',
                          'complete', '', ?)""",
                (album_id, root_text, f"artist-{number}", f"album-{number}", f"Artist {number}",
                 f"Album {number}", now),
            )
            db.execute(
                "INSERT INTO album_review_tracks(album_review_id, inventory_id) VALUES (?, ?)",
                (album_id, inventory_id),
            )
            candidate_id = db.execute(
                """INSERT INTO metadata_candidates(album_review_id, provider, provider_id, rank, confidence,
                          artist, album, proposed_diff_json, provider_data_json, created_at)
                   VALUES (?, 'fixture', ?, 1, 1.0, ?, ?, '{}', '{}', ?)""",
                (album_id, f"candidate-{number}", f"Artist {number}", f"Album {number}", now),
            ).lastrowid
            db.execute("UPDATE album_reviews SET selected_candidate_id=? WHERE id=?", (candidate_id, album_id))

    payload_calls = []
    scope_calls = 0
    import beets_mvp
    original_scope = beets_mvp._scope_for_relative_path

    def count_scope(relative_path):
        nonlocal scope_calls
        scope_calls += 1
        return original_scope(relative_path)

    def readiness_payloads(_app, album_ids=None):
        payload_calls.append(list(album_ids or []))
        return [{
            "id": album_id, "execution_status": "not-run", "candidate_status": "complete",
            "candidate_error": "", "decision": "pending", "selected_candidate_id": album_id,
            "duplicate_preflight": {"status": "clear"}, "tracks": [{"id": album_id, "exception": None}],
            "candidates": [{"id": album_id, "confidence": 1.0, "track_count_mismatch": False,
                            "unmatched_track_count": 0, "hard_mismatches": [], "matched_track_count": 1,
                            "track_details": [{"status": "matched", "local": "Track", "proposed": "Track"}]}],
        } for album_id in (album_ids or [])]

    monkeypatch.setattr(beets_mvp, "_scope_for_relative_path", count_scope)
    monkeypatch.setattr(beets_mvp, "_album_review_payloads", readiness_payloads)
    result = app.test_client().get("/api/library-import/folders?page=2").json

    assert result["total"] == folder_count and result["page_count"] == 42
    assert [folder["path"] for folder in result["folders"]] == [f"Folder {number:04d}" for number in range(25, 50)]
    assert payload_calls == [list(range(26, 51))]
    assert scope_calls == folder_count


def test_folder_queue_search_and_every_status_remain_truthful(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = Path(app.config["LIBRARY_PATH"])
    for folder in ("Error", "Imported", "Needs Review", "Not Scanned", "Ready"):
        (root / folder).mkdir()
    root_text = str(root.resolve())
    now = "2026-10-06T00:00:00+00:00"
    states = [
        ("Error", "Needle Error", "failed", "error", "pending", .5),
        ("Imported", "Needle Imported", "complete", "complete", "approved", 1.0),
        ("Needs Review", "Needle Review", "not-run", "complete", "pending", .998),
        ("Ready", "Needle Ready", "not-run", "complete", "pending", 1.0),
    ]
    with sqlite3.connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            "INSERT INTO adoption_jobs(kind, root_path, status, created_at) VALUES ('fixture', ?, 'complete', ?)",
            (root_text, now),
        ).lastrowid
        for album_id, (folder, artist, execution, candidate_status, state, confidence) in enumerate(states, 1):
            inventory_id = db.execute(
                """INSERT INTO library_inventory(root_path, relative_path, size_bytes, mtime_ns, device, inode,
                          present, first_seen_job_id, last_seen_job_id) VALUES (?, ?, 1, 1, 1, ?, 1, ?, ?)""",
                (root_text, f"{folder}/01 Track.wav", album_id, job_id, job_id),
            ).lastrowid
            db.execute(
                """INSERT INTO album_reviews(id, root_path, artist_key, album_key, artist, album, state,
                          candidate_status, candidate_error, execution_status, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (album_id, root_text, artist.casefold(), folder.casefold(), artist, folder, state,
                 candidate_status, "fixture error" if candidate_status == "error" else "", execution, now),
            )
            db.execute("INSERT INTO album_review_tracks(album_review_id, inventory_id) VALUES (?, ?)",
                       (album_id, inventory_id))
            candidate_id = db.execute(
                """INSERT INTO metadata_candidates(album_review_id, provider, provider_id, rank, confidence,
                          artist, album, proposed_diff_json, provider_data_json, created_at)
                   VALUES (?, 'fixture', ?, 1, ?, ?, ?, '{}', '{}', ?)""",
                (album_id, f"candidate-{album_id}", confidence, artist, folder, now),
            ).lastrowid
            db.execute("UPDATE album_reviews SET selected_candidate_id=? WHERE id=?", (candidate_id, album_id))

    import beets_mvp
    monkeypatch.setattr(beets_mvp, "_album_review_payloads", lambda _app, album_ids=None: [{
        "id": album_id, "execution_status": "not-run", "candidate_status": "complete", "candidate_error": "",
        "decision": "pending", "selected_candidate_id": album_id, "duplicate_preflight": {"status": "clear"},
        "tracks": [{"id": album_id, "exception": None}],
        "candidates": [{"id": album_id, "confidence": 1.0, "track_count_mismatch": False,
                        "unmatched_track_count": 0, "hard_mismatches": [], "matched_track_count": 1,
                        "track_details": [{"status": "matched", "local": "Track", "proposed": "Track"}]}],
    } for album_id in (album_ids or [])])
    client = app.test_client()

    assert client.get("/api/library-import/folders?q=Needle+Ready").json["folders"][0]["path"] == "Ready"
    expected = {"not-scanned": "Not Scanned", "ready": "Ready", "needs-review": "Needs Review",
                "error": "Error", "imported": "Imported"}
    for status, folder in expected.items():
        result = client.get("/api/library-import/folders", query_string={"status": status}).json
        assert [record["path"] for record in result["folders"]] == [folder]


def test_duplicate_warning_is_preflighted_and_gates_direct_import(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    controller = Path("beets_mvp/static/review-workflow.js").read_bytes()

    assert b'data-review-duplicate' in html
    assert b'data-review-duplicate-action' in html
    assert b"warning.hidden = duplicate.status === 'clear'" in controller
    assert b"library-import-queue" not in html
    assert b"if (duplicate.status === 'ambiguous')" in controller
    assert b"if (duplicate.status === 'possible' && !duplicateAction.value)" in controller
    assert b"/execute" in html


def test_album_modal_candidate_details_match_flask_field_contract(tmp_path):
    candidate_ui = Path("beets_mvp/static/review-presentation.js").read_bytes()

    for label in (
        b"Artist", b"Album", b"Genre", b"Release Year", b"Tracks changed",
        b"Release Type/media", b"Release Region",
        b"Local tracks found online", b"Online tracks present locally",
    ):
        assert label in candidate_ui
    for omitted in (
        b"Duration delta", b"Release ID", b"Release group ID", b"Proposed position",
        b"Current duration", b"Barcode", b"matching service",
    ):
        assert omitted not in candidate_ui
    assert b"const iconPaths" in candidate_ui
    assert b"candidate-signals" in candidate_ui
    assert b"candidate-fact" in candidate_ui
    assert b"document.createElement('details')" in candidate_ui
    assert b"document.createElement('summary')" in candidate_ui


def test_album_modal_candidate_rows_keep_numeric_score_selected_state_and_artwork(tmp_path):
    shared = Path("beets_mvp/static/review-presentation.js").read_bytes()

    assert b"const presentation = score(value.confidence)" in shared
    assert b"details.open = selected" in shared
    assert b"details.setAttribute('aria-current', String(selected))" in shared
    assert b"if(value.artwork_url)" in shared
    assert b"artwork.src=value.artwork_url" in shared


def test_library_import_operations_expose_visible_live_progress(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    controller = Path("beets_mvp/static/review-workflow.js").read_bytes()
    folders = Path("beets_mvp/static/folder-workflow.js").read_bytes()

    assert html.count(b'operation-status') >= 2
    assert b'aria-live="polite"' in html
    assert b'.operation-status[data-busy="true"]::before' in html
    assert b"Scanning" in folders
    assert b"Finding bounded MusicBrainz candidates" in controller
    assert b"Searching MusicBrainz by MBID" in controller
    assert b"Validating files, registering them, and applying approved metadata in place" in html
    assert b"setAttribute('aria-busy', String(value))" in controller


def test_album_candidate_payload_exposes_only_persisted_safe_artwork_and_best_score(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [
        {
            "provider_id": "safe", "artist": "Artist", "album": "Album", "track_count": 1,
            "tracks": [{"title": "Song", "position": 1}],
            "artwork": {"available": True, "source": "cover-art-archive", "entity": "release",
                        "mbid": "123e4567-e89b-42d3-a456-426614174000",
                        "thumbnail_url": "https://coverartarchive.org/release/123e4567-e89b-42d3-a456-426614174000/front-250"},
        },
        {
            "provider_id": "unsafe", "artist": "Artist", "album": "Album", "track_count": 1,
            "tracks": [{"title": "Different", "position": 1}], "image_url": "javascript:alert(1)",
        },
    ]
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    incoming_artwork = "data:image/png;base64,aW5jb21pbmc="
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item(
        title="Song", artist="Artist", album="Album", path=str(path), artwork_url=incoming_artwork,
        albumtype="album", media="Digital Media", country="GB",
    ))
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert album["candidate_status"] == "complete"
    assert album["highest_confidence"] == max(candidate["confidence"] for candidate in album["candidates"])
    assert album["candidates"][0]["artwork_url"].startswith("https://coverartarchive.org/release/")
    assert album["candidates"][1]["artwork_url"] is None
    assert album["current_metadata"]["artwork_url"] == incoming_artwork
    assert {key: album["current_metadata"][key] for key in ("release_type", "media", "country")} == {
        "release_type": "album", "media": "Digital Media", "country": "GB",
    }

    html = client.get("/settings").data
    shared = Path("beets_mvp/static/review-presentation.js").read_bytes()
    assert b"if(value.artwork_url)" in shared
    assert b"artwork.src=value.artwork_url" in shared
    assert b"asIs = {...current, id:asIsId, is_as_is:true, duplicate_preflight:album.duplicate_preflight};" in shared
    assert b"artwork_url:null" not in html
    assert b"innerHTML" not in html


def test_legacy_candidates_are_marked_stale_then_refreshed_without_losing_decision(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    provider = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": query["artist"], "album": query["album"],
        "track_count": 1, "artwork": {"available": False, "source": "cover-art-archive", "status": "missing"},
    }]
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    client = app.test_client()
    client.post("/api/library/inventory/preview")
    client.post("/api/library-import/candidates", json={})
    album = client.get("/api/library-import/reviews").json["albums"][0]
    client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
    })
    with sqlite3.connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE metadata_candidates SET schema_version = 1")

    restarted = create_app({
        "TESTING": True, "SECRET_KEY": "test", "STATE_PATH": app.config["STATE_PATH"],
        "BROWSE_ROOTS": [str(tmp_path)], "MUSICBRAINZ_PROVIDER": provider,
    })
    restarted_client = restarted.test_client()
    stale = restarted_client.get("/api/library-import/reviews").json["albums"][0]
    assert stale["decision"] == "approved"
    assert stale["selected_candidate_id"] is not None
    assert stale["candidate_status"] == "stale"
    assert "refresh" in stale["candidate_error"].lower()

    assert restarted_client.post("/api/library-import/candidates", json={"limit": 1}).status_code == 201
    refreshed = restarted_client.get("/api/library-import/reviews").json["albums"][0]
    assert refreshed["decision"] == "approved"
    assert refreshed["selected_candidate_id"] == refreshed["candidates"][0]["id"]
    assert refreshed["candidate_status"] == "complete"
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT schema_version FROM metadata_candidates").fetchone()[0] == 5

    restarted.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "different-release", "artist": query["artist"], "album": query["album"],
    }]
    selected_id = refreshed["selected_candidate_id"]
    assert restarted_client.post("/api/library-import/candidates", json={"limit": 1}).status_code == 207
    preserved = restarted_client.get("/api/library-import/reviews").json["albums"][0]
    assert preserved["decision"] == "approved"
    assert preserved["selected_candidate_id"] == selected_id
    assert preserved["candidates"][0]["provider_id"] == "release-1"
    assert preserved["candidate_status"] == "stale"


def test_schema_v4_restart_does_not_reopen_or_rewrite_completed_import(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    write_wav(path)
    media = MediaFile(str(path))
    media.title = "Song"
    media.artist = media.albumartist = "Artist"
    media.album = "Album"
    media.save()
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        "provider_id": "release-1", "artist": "Artist", "album": "Album", "track_count": 1,
        "tracks": [{"title": "Song", "position": 1, "recording_id": "recording-1",
                    "track_artist": "Artist", "artist_credit_source": "track"}],
    }]
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201
    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert client.patch(f'/api/library-import/albums/{album["id"]}', json={
        "decision": "approved", "candidate_id": album["candidates"][0]["id"],
    }).status_code == 200
    assert client.post(f'/api/library-import/albums/{album["id"]}/execute', json={}).status_code == 201
    before_bytes = path.read_bytes()
    before_tags = MediaFile(str(path))
    before_file_metadata = (before_tags.title, before_tags.artist, before_tags.albumartist)
    before_item = next(iter(Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).items()))
    before_db_metadata = (before_item.title, before_item.artist, before_item.albumartist, before_item.mb_trackid)
    with sqlite3.connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE metadata_candidates SET schema_version = 3 WHERE album_review_id = ?", (album["id"],))

    def provider_must_not_run(query, *, limit):
        raise AssertionError("startup must not refresh completed candidate evidence")

    restarted = create_app({
        "TESTING": True, "SECRET_KEY": "test", "STATE_PATH": app.config["STATE_PATH"],
        "BROWSE_ROOTS": [str(tmp_path)], "MUSICBRAINZ_PROVIDER": provider_must_not_run,
    })
    restarted_client = restarted.test_client()
    pending = restarted_client.get("/api/library-import/reviews").json
    assert pending["albums"] == []
    assert pending["items"] == []
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT candidate_status, execution_status FROM album_reviews WHERE id = ?", (album["id"],)
        ).fetchone()
        schema_version = db.execute(
            "SELECT schema_version FROM metadata_candidates WHERE album_review_id = ?", (album["id"],)
        ).fetchone()[0]
    assert review == ("complete", "complete")
    assert schema_version == 3
    assert path.read_bytes() == before_bytes
    after_tags = MediaFile(str(path))
    assert (after_tags.title, after_tags.artist, after_tags.albumartist) == before_file_metadata
    after_item = next(iter(Library(restarted.config["BEETS_DB"], directory=restarted.config["LIBRARY_PATH"]).items()))
    assert (after_item.title, after_item.artist, after_item.albumartist, after_item.mb_trackid) == before_db_metadata


def test_candidate_schema_defaults_distinguish_fresh_and_upgraded_databases(tmp_path):
    fresh_path = tmp_path / "fresh.db"
    _init_db(str(fresh_path))
    with sqlite3.connect(fresh_path) as db:
        fresh_default = next(row[4] for row in db.execute("PRAGMA table_info(metadata_candidates)")
                             if row[1] == "schema_version")
    assert fresh_default == "2"

    upgraded_path = tmp_path / "upgraded.db"
    with sqlite3.connect(upgraded_path) as db:
        db.execute("""CREATE TABLE album_reviews (
            id INTEGER PRIMARY KEY, root_path TEXT NOT NULL, artist_key TEXT NOT NULL,
            album_key TEXT NOT NULL, artist TEXT NOT NULL, album TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', selected_candidate_id INTEGER,
            candidate_status TEXT NOT NULL DEFAULT 'not-run', candidate_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL, execution_status TEXT NOT NULL DEFAULT 'not-run'
        )""")
        db.execute("""CREATE TABLE metadata_candidates (
            id INTEGER PRIMARY KEY, album_review_id INTEGER NOT NULL, provider TEXT NOT NULL,
            provider_id TEXT NOT NULL, rank INTEGER NOT NULL, confidence REAL NOT NULL,
            artist TEXT NOT NULL, album TEXT NOT NULL, year TEXT, proposed_diff_json TEXT NOT NULL,
            provider_data_json TEXT NOT NULL, created_at TEXT NOT NULL
        )""")
        for album_id, execution_status in ((1, "not-run"), (2, "complete")):
            db.execute(
                """INSERT INTO album_reviews(
                       id, root_path, artist_key, album_key, artist, album, state,
                       selected_candidate_id, candidate_status, updated_at, execution_status
                   ) VALUES (?, '/library', ?, ?, 'Artist', 'Album', 'approved', ?, 'complete', 'now', ?)""",
                (album_id, f"artist-{album_id}", f"album-{album_id}", album_id, execution_status),
            )
            db.execute(
                """INSERT INTO metadata_candidates(
                       id, album_review_id, provider, provider_id, rank, confidence, artist, album,
                       proposed_diff_json, provider_data_json, created_at
                   ) VALUES (?, ?, 'musicbrainz', ?, 1, 1.0, 'Artist', 'Album', '{}', '{}', 'now')""",
                (album_id, album_id, f"release-{album_id}"),
            )

    _init_db(str(upgraded_path))
    with sqlite3.connect(upgraded_path) as db:
        upgraded_default = next(row[4] for row in db.execute("PRAGMA table_info(metadata_candidates)")
                                if row[1] == "schema_version")
        rows = db.execute(
            "SELECT id, state, selected_candidate_id, candidate_status, execution_status FROM album_reviews ORDER BY id"
        ).fetchall()
        versions = db.execute("SELECT schema_version FROM metadata_candidates ORDER BY id").fetchall()
        source_columns = {row[1]: row[4] for row in db.execute("PRAGMA table_info(album_reviews)")}
    assert upgraded_default == "1"
    assert rows == [
        (1, "approved", 1, "stale", "not-run"),
        (2, "approved", 2, "complete", "complete"),
    ]
    assert versions == [(1,), (1,)]
    assert source_columns["source_kind"] == "'library'" and source_columns["source_id"] == "''"


def test_album_modal_actions_remain_workflow_only(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    controller = Path("beets_mvp/static/review-workflow.js").read_bytes()

    assert b"adapter.save(album, {decision: 'skipped'})" in controller
    assert b"candidate_id: selectedValue(selected)" in controller
    assert b"duplicate_action: duplicateAction.value || undefined" in controller
    assert b'data-review-duplicate-action' in html
    for action in (b'merge', b'replace', b'keep-both', b'skip'):
        assert b'value="' + action + b'"' in html
    assert b"same MusicBrainz recording ID" in html
    assert b"method:'PATCH'" in html
    assert b"/api/library-import/albums/${activeAlbum.id}/preview" not in html
    assert b"/api/library-import/albums/${album.id}/execute" in html
    assert b"data-review-skip" in html
    assert b"Skip for now" not in html
    assert b"data-review-decision=\"rejected\"" not in html
    assert b"data-review-decision=\"approved\"" not in html
    assert b"beet import" not in html
    assert b"/api/imports/" not in html


def test_inventory_preview_session_cache_is_scoped_to_rendered_build(tmp_path):
    app = make_app(tmp_path)
    app.config["BUILD_SHA"] = "build-one"
    first = app.test_client().get("/settings").data
    app.config["BUILD_SHA"] = "build-two"
    second = app.test_client().get("/settings").data

    assert b"storageKey:'cratekeep-folder-selection:' + \"build-one\"" in first
    assert b"storageKey:'cratekeep-folder-selection:' + \"build-two\"" in second
    module = Path("beets_mvp/static/folder-workflow.js").read_bytes()
    assert b"sessionStorage.setItem(storageKey" in module
    assert b"sessionStorage.getItem(storageKey)" in module


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_musicbrainz_candidates_require_successful_preview_in_current_page_session(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)

    assert 'id="library-import-scan-selected" type="button" data-folder-scan disabled' in html
    assert "libraryApi('/api/library-import/folders/scan'" in html
    assert "body:JSON.stringify({folders})" in html
    assert "CratekeepFolderWorkflow.create" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_shared_response_reader_preserves_json_errors_and_explains_html(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    start = html.index("async function readApiResponse")
    end = html.index("const browser =", start)
    helper = html[start:end]
    probe = f"""
{helper}
const headers = value => ({{get(name) {{ return name === 'content-type' ? value : null; }}}});
const structured = await readApiResponse({{
  ok:false, status:409, headers:headers('application/json'), async text() {{ return '{{"error":"inventory is stale"}}'; }}
}}, 'Unable to preview').catch(error => error.message);
const htmlError = await readApiResponse({{
  ok:false, status:502, headers:headers('text/html'), async text() {{ return '<!DOCTYPE html><title>Bad Gateway</title>'; }}
}}, 'Candidate generation failed').catch(error => error.message);
console.log(JSON.stringify({{structured, htmlError}}));
"""
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval", probe],
        check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)
    assert result["structured"] == "inventory is stale"
    assert result["htmlError"] == (
        "Candidate generation failed (HTTP 502). The server returned HTML; "
        "check the proxy route and application logs."
    )
    assert "response.json()" not in html


def test_settings_persists_valid_beets_config_and_managed_values(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    new_library = tmp_path / "new-library"

    response = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(new_library),
        "fetch_art": "1",
        "art_sidecar": "1",
        "art_embed": "1",
        "art_replace": "1",
        "beets_config": "directory: /ignored\nlibrary: /ignored.db\nimport:\n  move: false\n  quiet: true\nplugins: [lastgenre]\npaths:\n  default: $albumartist/$album\n",
    })

    assert response.status_code == 302
    config = Path(app.config["BEETS_CONFIG"]).read_text()
    assert f'directory: "{new_library}"' in config
    assert f'library: "{tmp_path / "config" / "library.db"}"' in config
    assert "move: true" in config
    assert "quiet: true" in config
    assert "- lastgenre" in config
    assert "- fetchart" in config
    assert "default: $albumartist/$album" in config

    restarted = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    restarted_page = restarted.test_client().get("/settings")
    assert restarted.config["FETCH_ART"] is True
    assert restarted.config["ART_SIDECAR"] is True
    assert restarted.config["ART_EMBED"] is True
    assert restarted.config["ART_REPLACE"] is True
    assert b'name="fetch_art" type="checkbox" value="1" checked' in restarted_page.data
    assert b'name="art_sidecar" type="checkbox" value="1" checked' in restarted_page.data
    assert b'name="art_embed" type="checkbox" value="1" checked' in restarted_page.data
    assert b'name="art_replace" type="checkbox" value="1" checked' in restarted_page.data


@pytest.mark.parametrize("plugins", ["ftintitle lastgenre", ["ftintitle", "lastgenre"]])
def test_ftintitle_existing_plugin_forms_hydrate_and_preserve_advanced_config(tmp_path, plugins):
    app = make_app(tmp_path)
    config_path = Path(app.config["BEETS_CONFIG"])
    document = yaml.safe_load(config_path.read_text())
    document.update({
        "plugins": plugins,
        "ftintitle": {
            "format": "(feat. {0})", "keep_in_artist": True, "auto": False, "drop": True,
            "custom_words": ["guest"], "bracket_keywords": ["feat"], "future_option": "preserve-me",
        },
        "lastgenre": {"canonical": True},
    })
    config_path.write_text(yaml.safe_dump(document, sort_keys=False))
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": "test", "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    page = restarted.test_client().get("/settings").get_data(as_text=True)
    assert 'name="ftintitle_enabled" type="checkbox" value="1" checked' in page
    assert 'value="(feat. {0})"' in page
    assert 'name="ftintitle_keep_in_artist" type="checkbox" value="1" checked' in page

    response = restarted.test_client().post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "[feat. {}]",
        "ftintitle_keep_in_artist": "1", "beets_config": config_path.read_text(),
    })
    assert response.status_code == 302
    saved = yaml.safe_load(config_path.read_text())
    assert saved["plugins"] == ["lastgenre", "ftintitle"]
    assert saved["lastgenre"] == {"canonical": True}
    assert saved["ftintitle"] == {
        "format": "[feat. {}]", "keep_in_artist": True, "preserve_album_artist": True,
        "auto": True, "drop": False, "custom_words": ["guest"], "bracket_keywords": ["feat"],
        "future_option": "preserve-me",
    }


def test_ftintitle_disable_and_invalid_format_are_safe(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    config_path = Path(app.config["BEETS_CONFIG"])
    source = config_path.read_text() + "ftintitle:\n  custom_words: [guest]\n"
    invalid = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat.", "beets_config": source,
    })
    assert invalid.status_code == 200
    assert b"FtInTitle format must include the guest" in invalid.data
    assert b'value="feat."' in invalid.data
    assert "custom_words: [guest]" in invalid.get_data(as_text=True)

    invalid_list = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat. {0}",
        "beets_config": config_path.read_text() + "ftintitle:\n  custom_words: guest\n",
    })
    assert invalid_list.status_code == 200
    assert b"ftintitle custom_words setting must be a list" in invalid_list.data

    enabled = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_enabled": "1", "ftintitle_format": "feat. {0}", "beets_config": source,
    })
    assert enabled.status_code == 302
    saved = yaml.safe_load(config_path.read_text())
    assert "ftintitle" in saved["plugins"]
    assert saved["ftintitle"]["preserve_album_artist"] is True
    assert saved["ftintitle"]["auto"] is True
    assert saved["ftintitle"]["drop"] is False

    disabled = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"), "library_path": str(tmp_path / "library"),
        "ftintitle_format": "feat. {0}", "beets_config": config_path.read_text(),
    })
    assert disabled.status_code == 302
    saved = yaml.safe_load(config_path.read_text())
    assert "ftintitle" not in saved["plugins"]
    assert saved["ftintitle"]["custom_words"] == ["guest"]


def test_ftintitle_projection_uses_upstream_parsing_and_formatting(tmp_path):
    app = make_app(tmp_path)
    candidate = {"artist": "Lead", "tracks": [
        {"title": "Song (Live)", "track_artist": "Lead feat. Guest"},
        {"title": "Song [Remix]", "track_artist": "Lead feat. Guest"},
        {"title": "Custom [Remix]", "track_artist": "Lead alongside Guest"},
        {"title": "Existing feat. Guest", "track_artist": "Lead feat. Guest"},
        {"title": "Bracket", "track_artist": "Lead [feat. Guest"},
        {"title": "No feature", "track_artist": "Lead presents Guest"},
    ]}
    app.config["FTINTITLE"] = {
        **app.config["FTINTITLE"], "enabled": True, "format": "feat. {0}", "keep_in_artist": False,
        "custom_words": ["alongside"], "bracket_keywords": ["live", "remix"],
    }
    projected = _effective_candidate(app, candidate)
    assert candidate["tracks"][0] == {"title": "Song (Live)", "track_artist": "Lead feat. Guest"}
    assert projected["tracks"][0] == {"title": "Song feat. Guest (Live)", "track_artist": "Lead"}
    assert projected["tracks"][1] == {"title": "Song feat. Guest [Remix]", "track_artist": "Lead"}
    assert projected["tracks"][2] == {"title": "Custom feat. Guest [Remix]", "track_artist": "Lead"}
    assert projected["tracks"][3] == {"title": "Existing feat. Guest", "track_artist": "Lead"}
    assert projected["tracks"][4] == candidate["tracks"][4]
    assert projected["tracks"][5] == candidate["tracks"][5]


@pytest.mark.parametrize(("local_album", "canonical_album", "release_note", "expected", "changed"), [
    ("Dark Sky Paradise (Deluxe)", "Dark Sky Paradise", "deluxe", "Dark Sky Paradise (Deluxe)", False),
    ("Dark Sky Paradise [DELUXE]", "dark sky paradise", " deluxe,   clean ", "Dark Sky Paradise [DELUXE]", False),
    ("Dark Sky Paradise", "Dark Sky Paradise", "deluxe", "Dark Sky Paradise", False),
    ("Dark Sky Paradise (Remastered)", "Dark Sky Paradise", "deluxe", "Dark Sky Paradise", True),
    ("Dark Sky Paradise (Deluxe)", "Dark Sky Paradise (Deluxe)", "deluxe", "Dark Sky Paradise (Deluxe)", False),
    ("BULLY - DELUXE", "BULLY", "deluxe", "BULLY - DELUXE", False),
    ("BULLY – Deluxe", "bully", "DELUXE", "BULLY – Deluxe", False),
    ("BULLY — Deluxe", "BULLY", "deluxe, clean", "BULLY — Deluxe", False),
    ("BULLY: Deluxe", "BULLY", "deluxe", "BULLY: Deluxe", False),
    ("BULLY - CLEAN", "BULLY", "deluxe", "BULLY", True),
    ("BULLY-DELUXE", "BULLY", "deluxe", "BULLY", True),
])
def test_reviewed_library_album_edition_projection_is_conservative(
    tmp_path, local_album, canonical_album, release_note, expected, changed,
):
    app = make_app(tmp_path)
    candidate = {"album": canonical_album, "release_disambiguation": release_note, "tracks": []}

    projected = _effective_candidate(app, candidate, {"album": local_album})

    assert projected["album"] == expected
    assert candidate == {"album": canonical_album, "release_disambiguation": release_note, "tracks": []}
    diff = _candidate_diff({"artist": "", "album": local_album, "tracks": []}, projected)
    assert diff.get("album") == ({"from": local_album, "to": expected} if changed else None)


def test_ftintitle_preserve_album_artist_candidate_semantics(tmp_path):
    app = make_app(tmp_path)
    candidate = {
        "artist": "Lead feat. Guest",
        "tracks": [{"title": "Song", "track_artist": "Lead feat. Guest"}],
    }
    app.config["FTINTITLE"] = {**app.config["FTINTITLE"], "enabled": True, "preserve_album_artist": True}
    assert _effective_candidate(app, candidate) == candidate
    app.config["FTINTITLE"]["preserve_album_artist"] = False
    projected = _effective_candidate(app, candidate)
    assert projected["artist"] == "Lead feat. Guest"
    assert projected["tracks"] == [{"title": "Song feat. Guest", "track_artist": "Lead"}]


def test_projection_recheck_never_weakens_strict_readiness():
    album = {
        "execution_status": "not-run", "candidate_status": "complete", "candidate_error": "",
        "decision": "pending", "selected_candidate_id": 7, "duplicate_preflight": {"status": "clear"},
        "tracks": [{"id": 1, "exception": None}],
        "candidates": [{
            "id": 7, "confidence": .99, "track_count_mismatch": False, "unmatched_track_count": 0,
            "hard_mismatches": [], "matched_track_count": 1,
            "track_details": [{"status": "matched", "local": "Song", "proposed": "Song"}],
        }],
    }
    assert _album_bulk_readiness(album) == (False, "selected candidate is not an exact 100% match")


def test_existing_fetch_art_setting_migrates_to_sidecar_only_defaults(tmp_path):
    app = make_app(tmp_path)
    with sqlite3.connect(app.config["APP_DB"]) as database:
        database.execute("DELETE FROM app_settings WHERE key IN ('art_sidecar', 'art_embed', 'art_replace')")
        database.execute("UPDATE app_settings SET value = '1' WHERE key = 'fetch_art'")
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": "test", "STATE_PATH": app.config["STATE_PATH"],
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    assert restarted.config["FETCH_ART"] is True
    assert restarted.config["ART_SIDECAR"] is True
    assert restarted.config["ART_EMBED"] is False
    assert restarted.config["ART_REPLACE"] is False


def test_invalid_beets_config_is_rejected_without_persisting(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    config_path = Path(app.config["BEETS_CONFIG"])
    original = config_path.read_text()

    response = client.post("/settings", data={
        "inbox_path": str(tmp_path / "changed-inbox"),
        "library_path": str(tmp_path / "library"),
        "beets_config": "import: [not, a, mapping]",
    })

    assert response.status_code == 200
    assert b'The beets import section must be a YAML mapping.' in response.data
    assert config_path.read_text() == original
    assert app.config["INBOX_PATH"] == str(tmp_path / "inbox")


def test_dashboard_uses_compact_light_admin_visual_contract(tmp_path):
    page = make_app(tmp_path).test_client().get("/")
    stylesheet = (Path(__file__).parents[1] / "beets_mvp/static/app-shell.css").read_bytes()

    assert b"color-scheme: light" in stylesheet
    assert b"--page:#f1f4f3" in stylesheet
    assert b'app-shell.css' in page.data
    assert b'font:16px/1.5 ui-sans-serif' in page.data
    assert b"width:13.5rem" in page.data
    assert b"summary-grid" in page.data
    assert b"prefers-color-scheme:dark" not in stylesheet


def test_setup_and_settings_routes_have_distinct_lifecycle_pages(tmp_path):
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test",
        "STATE_PATH": str(tmp_path / "config"),
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    client = app.test_client()

    assert client.get("/settings").headers["Location"].endswith("/setup")
    setup_page = client.get("/setup")
    assert b'class="setup-mode"' in setup_page.data
    assert b'Set up Cratekeep' in setup_page.data
    assert b'<form method="post" action="/setup">' in setup_page.data

    setup_response = client.post("/setup", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(tmp_path / "library"),
    })
    assert setup_response.status_code == 302
    assert setup_response.headers["Location"].endswith("/")
    assert client.get("/setup").headers["Location"].endswith("/settings")

    settings_page = client.get("/settings")
    assert settings_page.status_code == 200
    assert b'<form method="post" action="/settings">' in settings_page.data

    inbox_page = client.get("/inbox")
    assert inbox_page.status_code == 200
    assert b'<form method="post" action="/settings">' not in inbox_page.data
    assert b'id="inbox-grid"' in inbox_page.data
    assert client.post("/inbox").status_code == 405


def tagged_wav(path: Path, *, artist: str, album: str, title: str = "Track") -> None:
    write_wav(path)
    media = MediaFile(str(path))
    media.title = title
    media.artist = media.albumartist = artist
    media.album = album
    media.track = media.disc = 1
    media.save()


def exact_candidate(query: dict, provider_id: str = "release-1") -> dict:
    return {
        "provider_id": provider_id, "artist": query["artist"], "album": query["album"],
        "tracks": [{
            "title": track["title"], "track_artist": track.get("artist"),
            "position": index, "medium_position": 1,
            "length_ms": round(float(track.get("duration") or 1) * 1000),
            "recording_id": f"recording-{provider_id}-{index}",
        } for index, track in enumerate(query["tracks"], 1)],
    }


def test_folder_queue_is_literal_paginated_searchable_filterable_and_path_safe(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    for number in range(27):
        (root / f"Literal Folder {number:02d}").mkdir()
    tagged_wav(root / "Loose.wav", artist="Loose Artist", album="Loose Album")
    client = app.test_client()

    first = client.get("/api/library-import/folders").json
    second = client.get("/api/library-import/folders?page=2").json
    assert first["limit"] == 25 and len(first["folders"]) == 25
    assert second["page"] == second["page_count"] == 2 and len(second["folders"]) == 3
    assert first["folders"][0]["path"] == "."
    assert first["folders"][1]["name"] == "Literal Folder 00"
    searched = client.get("/api/library-import/folders?q=Folder+26").json
    assert [folder["path"] for folder in searched["folders"]] == ["Literal Folder 26"]
    assert client.get("/api/library-import/folders?status=not-scanned").json["total"] == 28
    for unsafe in ("../outside", "/tmp", "Literal Folder 00/child"):
        assert client.get("/api/library-import/folders/albums", query_string={"folder": unsafe}).status_code == 400


def test_folder_expansion_and_selected_scan_are_scoped_and_non_mutating(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    selected = root / "Selected Literal" / "Album A" / "01 Song.wav"
    sibling = root / "Sibling Literal" / "Album B" / "01 Other.wav"
    tagged_wav(selected, artist="Shared Metadata Artist", album="Shared Album", title="Song")
    tagged_wav(sibling, artist="Shared Metadata Artist", album="Shared Album", title="Other")
    beets_library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    beets_library.add(Item(title="Song", artist="Shared Metadata Artist", albumartist="Shared Metadata Artist",
                           album="Shared Album", path=str(selected)))
    beets_library.add(Item(title="Other", artist="Shared Metadata Artist", albumartist="Shared Metadata Artist",
                           album="Shared Album", path=str(sibling)))
    before = {path: (path.read_bytes(), path.stat().st_ino) for path in (selected, sibling)}
    queries = []
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: queries.append(query["album"]) or [exact_candidate(query)]
    client = app.test_client()

    assert client.post("/api/library-import/folders/scan", json={"folders": ["Sibling Literal"]}).status_code == 201
    with sqlite3.connect(app.config["APP_DB"]) as db:
        sibling_seen_job = db.execute(
            "SELECT last_seen_job_id FROM library_inventory WHERE relative_path LIKE 'Sibling Literal/%'"
        ).fetchone()[0]
    queries.clear()
    scanned = client.post("/api/library-import/folders/scan", json={"folders": ["Selected Literal"]})
    assert scanned.status_code == 201
    assert queries == ["Shared Album"]
    sibling_expanded = client.get("/api/library-import/folders/albums", query_string={"folder": "Sibling Literal"}).json
    assert [track["path"] for album in sibling_expanded["albums"] for track in album["tracks"]] == [
        "Sibling Literal/Album B/01 Other.wav"
    ]
    expanded = client.get("/api/library-import/folders/albums", query_string={"folder": "Selected Literal"}).json
    assert [track["path"] for album in expanded["albums"] for track in album["tracks"]] == [
        "Selected Literal/Album A/01 Song.wav"
    ]
    assert all(path.read_bytes() == contents and path.stat().st_ino == inode
               for path, (contents, inode) in before.items())
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute(
            "SELECT last_seen_job_id FROM library_inventory WHERE relative_path LIKE 'Sibling Literal/%'"
        ).fetchone()[0] == sibling_seen_job


def test_folder_scan_upgrades_legacy_album_key_without_losing_review_identity(tmp_path):
    app = make_app(tmp_path)
    track = tmp_path / "library" / "Selected" / "Album" / "01 Song.wav"
    tagged_wav(track, artist="Legacy Artist", album="Legacy Album", title="Song")
    Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).add(
        Item(title="Song", artist="Legacy Artist", albumartist="Legacy Artist",
             album="Legacy Album", path=str(track))
    )
    provider_queries = []
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: provider_queries.append(query) or [exact_candidate(query)]
    client = app.test_client()

    assert client.post("/api/library-import/folders/scan", json={"folders": ["Selected"]}).status_code == 201
    with sqlite3.connect(app.config["APP_DB"]) as db:
        album_id, candidate_id = db.execute(
            "SELECT id, selected_candidate_id FROM album_reviews WHERE artist = 'Legacy Artist'"
        ).fetchone()
        assert candidate_id is not None
        db.execute(
            "UPDATE album_reviews SET artist_key = ?, state = 'approved' WHERE id = ?",
            ("legacy artist", album_id),
        )

    rescanned = client.post("/api/library-import/folders/scan", json={"folders": ["Selected"]})
    assert rescanned.status_code == 201
    assert rescanned.json["folders"][0]["changed"] is False
    assert len(provider_queries) == 1
    expanded = client.get(
        "/api/library-import/folders/albums", query_string={"folder": "Selected"}
    ).json["albums"]
    assert len(expanded) == 1
    assert expanded[0]["id"] == album_id
    assert expanded[0]["decision"] == "approved"
    assert expanded[0]["selected_candidate_id"] == candidate_id

    sibling = tmp_path / "library" / "Sibling" / "Album" / "01 Other.wav"
    tagged_wav(sibling, artist="Legacy Artist", album="Legacy Album", title="Other")
    Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"]).add(
        Item(title="Other", artist="Legacy Artist", albumartist="Legacy Artist",
             album="Legacy Album", path=str(sibling))
    )
    assert client.post("/api/library-import/folders/scan", json={"folders": ["Sibling"]}).status_code == 201
    with sqlite3.connect(app.config["APP_DB"]) as db:
        sibling_album_id = db.execute(
            "SELECT id FROM album_reviews WHERE artist_key LIKE 'sibling%'"
        ).fetchone()[0]
        sibling_inventory_id = db.execute(
            "SELECT id FROM library_inventory WHERE relative_path LIKE 'Sibling/%'"
        ).fetchone()[0]
        db.execute("DELETE FROM metadata_candidates WHERE album_review_id=?", (sibling_album_id,))
        db.execute("DELETE FROM album_review_tracks WHERE album_review_id=?", (sibling_album_id,))
        db.execute("DELETE FROM album_reviews WHERE id=?", (sibling_album_id,))
        db.execute(
            "INSERT INTO album_review_tracks(album_review_id, inventory_id) VALUES (?, ?)",
            (album_id, sibling_inventory_id),
        )
        db.execute("UPDATE album_reviews SET artist_key='legacy artist' WHERE id=?", (album_id,))

    media = MediaFile(str(track))
    media.title = "Song changed"
    media.save()
    assert client.post("/api/library-import/folders/scan", json={"folders": ["Selected"]}).status_code == 201
    selected_tracks = client.get(
        "/api/library-import/folders/albums", query_string={"folder": "Selected"}
    ).json["albums"][0]["tracks"]
    sibling_album = client.get(
        "/api/library-import/folders/albums", query_string={"folder": "Sibling"}
    ).json["albums"][0]
    assert [item["path"] for item in selected_tracks] == ["Selected/Album/01 Song.wav"]
    assert [item["path"] for item in sibling_album["tracks"]] == ["Sibling/Album/01 Other.wav"]
    assert sibling_album["id"] != album_id
    assert sibling_album["decision"] == "pending"


def test_folder_expansion_reconciles_deleted_album_and_preserves_unchanged_review(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library" / "Artist Folder"
    kept = root / "Album A" / "01 Kept.wav"
    removed = root / "Album B" / "01 Removed.wav"
    tagged_wav(kept, artist="Artist", album="Album A", title="Kept")
    tagged_wav(removed, artist="Artist", album="Album B", title="Removed")
    provider_queries = []
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: provider_queries.append(query["album"]) or [exact_candidate(query)]
    client = app.test_client()

    assert client.post("/api/library-import/folders/scan", json={"folders": ["Artist Folder"]}).status_code == 201
    albums = client.get("/api/library-import/folders/albums", query_string={"folder": "Artist Folder"}).json["albums"]
    album_a = next(album for album in albums if album["album"] == "Album A")
    saved = client.patch(f'/api/library-import/albums/{album_a["id"]}', json={
        "decision": "approved", "candidate_id": album_a["selected_candidate_id"],
    }).json
    provider_queries.clear()
    removed.unlink()
    removed.parent.rmdir()

    # The first bounded scope expansion after a hard page load reconciles disk.
    assert client.get("/api/library-import/folders").json["folders"][0]["album_count"] == 2
    refreshed = client.get("/api/library-import/folders/albums", query_string={"folder": "Artist Folder"})
    assert refreshed.status_code == 200
    assert [(album["album"], album["decision"], album["selected_candidate_id"])
            for album in refreshed.json["albums"]] == [("Album A", "approved", saved["selected_candidate_id"])]
    assert refreshed.json["reconciliation"]["removed_files"] == 1
    assert provider_queries == []
    assert client.get("/api/library-import/folders").json["folders"][0]["album_count"] == 1

    media = MediaFile(str(kept))
    media.title = "Kept (changed)"
    media.save()
    changed = client.post("/api/library-import/folders/scan", json={"folders": ["Artist Folder"]})
    assert changed.status_code == 201
    assert changed.json["folders"][0]["changed_files"] == 1
    assert provider_queries == ["Album A"]
    changed_album = client.get(
        "/api/library-import/folders/albums", query_string={"folder": "Artist Folder"}
    ).json["albums"][0]
    assert changed_album["decision"] == "approved"
    assert changed_album["selected_candidate_id"] is not None


def test_selected_scan_reports_partial_album_failure_without_losing_success(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    tagged_wav(root / "Selected" / "Good" / "01 Good.wav", artist="Artist", album="Good", title="Good")
    tagged_wav(root / "Selected" / "Bad" / "01 Bad.wav", artist="Artist", album="Bad", title="Bad")

    def provider(query, *, limit):
        if query["album"] == "Bad":
            raise ProviderError("deterministic provider failure", code="fixture_failure")
        return [exact_candidate(query)]

    app.config["MUSICBRAINZ_PROVIDER"] = provider
    response = app.test_client().post("/api/library-import/folders/scan", json={"folders": ["Selected"]})
    assert response.status_code == 207
    assert {album["status"] for album in response.json["albums"]} == {"ready", "error"}
    assert any(error.get("album_review_id") for error in response.json["errors"])
    summary = app.test_client().post("/api/library-import/bulk/preview", json={"folders": ["Selected"]}).json
    assert summary["ready_album_count"] == summary["exception_count"] == 1


def test_library_import_active_queue_removes_completed_albums_but_retains_history_and_inbox(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    tagged_wav(root / "Partial" / "First" / "01 First.wav", artist="Artist", album="First", title="First")
    tagged_wav(root / "Partial" / "Second" / "01 Second.wav", artist="Artist", album="Second", title="Second")
    inbox_track = tmp_path / "inbox" / "Rollback Album" / "01 Inbox.mp3"
    inbox_track.parent.mkdir(parents=True)
    inbox_track.write_bytes(b"synthetic inbox fixture")
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query, query["album"])]
    client = app.test_client()
    inbox_before = client.get("/api/inbox").json

    assert client.post("/api/library-import/folders/scan", json={"folders": ["Partial"]}).status_code == 201
    albums = client.get("/api/library-import/folders/albums", query_string={"folder": "Partial"}).json["albums"]
    first, second = sorted(albums, key=lambda album: album["album"])
    imported = client.post(
        f'/api/library-import/albums/{first["id"]}/execute',
        json={"candidate_id": first["selected_candidate_id"]},
    )
    assert imported.status_code == 201

    active = client.get("/api/library-import/folders").json
    assert [(folder["path"], folder["album_count"], folder["track_count"])
            for folder in active["folders"]] == [("Partial", 1, 1)]
    expanded = client.get("/api/library-import/folders/albums", query_string={"folder": "Partial"}).json
    assert [album["id"] for album in expanded["albums"]] == [second["id"]]

    completed = client.post(
        f'/api/library-import/albums/{second["id"]}/execute',
        json={"candidate_id": second["selected_candidate_id"]},
    )
    assert completed.status_code == 201
    assert client.get("/api/library-import/folders").json["folders"] == []
    archived = client.get("/api/library-import/folders?status=imported").json["folders"]
    assert [(folder["path"], folder["album_count"], folder["track_count"])
            for folder in archived] == [("Partial", 2, 2)]
    assert client.get("/api/inbox").json == inbox_before
    assert inbox_track.read_bytes() == b"synthetic inbox fixture"
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM album_reviews WHERE execution_status='complete'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM adoption_reviews WHERE imported_at IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM adoption_jobs WHERE kind='library_import_execute'").fetchone()[0] == 2


def test_bulk_import_removes_completed_last_page_and_clamps_to_remaining_active_queue(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    for number in range(25):
        (root / f"Active {number:02d}").mkdir()
    tagged_wav(root / "Z Ready" / "Album" / "01 Track.wav", artist="Artist", album="Album")
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query)]
    client = app.test_client()

    assert client.post("/api/library-import/folders/scan", json={"folders": ["Z Ready"]}).status_code == 201
    last_page = client.get("/api/library-import/folders?page=2").json
    assert last_page["page"] == 2 and [folder["path"] for folder in last_page["folders"]] == ["Z Ready"]
    result = client.post(
        "/api/library-import/bulk/execute", json={"folders": ["Z Ready"], "confirmed": True}
    )
    assert result.status_code == 201 and len(result.json["imported"]) == 1

    clamped = client.get("/api/library-import/folders?page=2").json
    assert clamped["page"] == clamped["page_count"] == 1
    assert clamped["total"] == 25 and all(folder["path"] != "Z Ready" for folder in clamped["folders"])
    assert client.get("/api/library-import/folders?status=imported").json["folders"][0]["path"] == "Z Ready"


def test_selected_scan_progress_is_bounded_to_visible_selected_folder_rows(tmp_path):
    scan_handler = Path("beets_mvp/static/folder-workflow.js").read_text()

    assert "const sequence = ++scanSequence, scopes = [...selected]" in scan_handler
    assert "setPhase(phases, scopes, 'Queued')" in scan_handler
    assert "setPhase(phases, scopes, {status:'Scanning', label:'Scanning…'})" in scan_handler
    assert "busy(scanButton, true)" in scan_handler
    assert "await load(page, {announce:false, refresh:true})" in scan_handler
    assert "No files were changed" not in scan_handler
    assert "state.setAttribute('aria-live', 'polite')" in scan_handler
    assert "phases.get(item.path)" in scan_handler


def test_bulk_readiness_predicate_rejects_every_unsafe_condition():
    base = {
        "id": 1, "execution_status": "not-run", "candidate_status": "complete", "candidate_error": "",
        "decision": "pending", "selected_candidate_id": 10, "duplicate_preflight": {"status": "clear"},
        "tracks": [{"id": 1, "exception": None}],
        "candidates": [{"id": 10, "confidence": 1.0, "track_count_mismatch": False,
                        "unmatched_track_count": 0, "hard_mismatches": [], "matched_track_count": 1,
                        "track_details": [{"status": "matched", "local": "Track", "proposed": "Track"}]}],
    }
    assert _album_bulk_readiness(base)[0]
    changes = [
        lambda album: album["candidates"].append({**album["candidates"][0], "id": 11}),
        lambda album: album["candidates"][0].update(confidence=.999),
        lambda album: album["candidates"][0].update(unmatched_track_count=1),
        lambda album: album.update(candidate_status="stale"),
        lambda album: album["duplicate_preflight"].update(status="possible"),
        lambda album: album.update(candidate_status="error"),
        lambda album: album.update(execution_status="complete"),
    ]
    for change in changes:
        album = copy.deepcopy(base); change(album)
        assert not _album_bulk_readiness(album)[0]


def test_bulk_import_requires_confirmation_revalidates_and_isolates_failures(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    for album in ("First", "Second"):
        tagged_wav(root / "Selected" / album / f"01 {album}.wav", artist="Artist", album=album, title=album)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query, query["album"])]
    client = app.test_client()
    assert client.post("/api/library-import/folders/scan", json={"folders": ["Selected"]}).status_code == 201
    assert client.post("/api/library-import/bulk/execute", json={"folders": ["Selected"]}).status_code == 400

    import beets_mvp
    original = beets_mvp._execute_library_import_album
    failed_once = False
    def isolated(app_value, album_id, *, dry_run):
        nonlocal failed_once
        if not dry_run and not failed_once:
            failed_once = True
            raise LibraryImportExecutionError("fixture execution failure", code="fixture_failure", status=500)
        return original(app_value, album_id, dry_run=dry_run)
    monkeypatch.setattr(beets_mvp, "_execute_library_import_album", isolated)

    result = client.post("/api/library-import/bulk/execute", json={"folders": ["Selected"], "confirmed": True})
    assert result.status_code == 207
    assert len(result.json["failed"]) == len(result.json["imported"]) == 1
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM album_reviews WHERE execution_status='complete'").fetchone()[0] == 1


def test_folder_queue_ui_has_persistent_selection_lazy_expansion_and_accessible_confirmation(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    for control in ("library-import-search", "library-import-filter", "folder-select-page",
                    "library-import-scan-selected", "library-import-ready", "library-import-review-exceptions"):
        assert f'id="{control}"' in html
    assert 'id="library-import-page-input"' not in html
    module = Path("beets_mvp/static/folder-workflow.js").read_text()
    assert "sessionStorage.setItem(storageKey" in module
    assert "/api/library-import/folders/albums?folder=" in html
    assert 'aria-modal="true" aria-labelledby="library-import-confirm-title"' in html
    assert "confirmModal?.addEventListener('mousedown'" in module
    assert "review.openSequence" in module
    assert "/api/library-import/albums/${nextId}" not in html
    assert "data-review-skip" in html
    assert "@media (max-width: 520px)" in html


def configure_named_inboxes(app, roots: list[Path], names: list[str] | None = None, **extra):
    names = names or [f"Source {index}" for index in range(1, len(roots) + 1)]
    existing = app.config["INBOXES"]
    data = {
        "inbox_id": [existing[index]["id"] if index < len(existing) else "" for index in range(len(roots))],
        "inbox_name": names,
        "inbox_path": [str(root) for root in roots],
        "library_path": app.config["LIBRARY_PATH"],
        **extra,
    }
    response = app.test_client().post("/settings", data=data)
    assert response.status_code == 302


def tagged_inbox_wav(path: Path, *, artist="Artist", album="Album", title="Song", track=1):
    write_wav(path)
    media = MediaFile(str(path))
    media.artist = media.albumartist = artist
    media.album = album
    media.title = title
    media.track = track
    media.save()


def approve_inbox_candidate(client, app, *, artwork: dict | None = None):
    candidate = {
        **exact_candidate({
            "artist": "Artist", "album": "Album",
            "tracks": [{"title": "Song", "artist": "Artist", "duration": 1}],
        }),
        "date": "2024-03-02", "year": "2024", "genre": "Rock",
        "release_type": "Album", "country": "US",
        "media": [{"position": 1, "format": "CD", "track_count": 1}],
    }
    if artwork is not None:
        candidate["artwork"] = artwork
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [candidate]
    album = client.get("/api/inbox/reviews").json["albums"][0]
    matched = client.post("/api/inbox/candidates", json={"album_id": album["id"]}).json["album"]
    chosen = matched["candidates"][0]
    approved = client.patch(
        f"/api/inbox/albums/{album['id']}",
        json={"decision": "approved", "candidate_id": chosen["id"]},
    )
    assert approved.status_code == 200
    return approved.json


def complete_mock_inbox_move(app, source: Path, *, add_unrelated: bool = False):
    destination = Path(app.config["LIBRARY_PATH"]) / "Artist" / "Album" / "01 Song.wav"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(source, destination)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item.from_path(destination))
    if add_unrelated:
        unrelated = Path(app.config["LIBRARY_PATH"]) / "Concurrent" / "Other" / "01 Other.wav"
        tagged_inbox_wav(unrelated, artist="Concurrent", album="Other", title="Other")
        library.add(Item.from_path(unrelated))
    return destination


def test_inbox_review_identity_is_scoped_by_stable_named_source(tmp_path):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "inbox-two"]
    roots[1].mkdir()
    for root in roots:
        tagged_inbox_wav(root / "Same" / "01 Song.wav")
    configure_named_inboxes(app, roots, ["One", "Two"])
    client = app.test_client()

    albums = client.get("/api/inbox/reviews").json["albums"]
    assert len(albums) == 2
    assert len({album["id"] for album in albums}) == 2
    assert len({album["source_id"] for album in albums}) == 2
    first_id = albums[0]["id"]
    first_source_id = app.config["INBOXES"][0]["id"]
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query)]
    matched = client.post("/api/inbox/candidates", json={"album_id": first_id}).json["album"]
    client.patch(
        f"/api/inbox/albums/{first_id}",
        json={"decision":"approved", "candidate_id":matched["candidates"][0]["id"]},
    )

    configure_named_inboxes(app, roots, ["Renamed", "Two"])
    renamed = client.get("/api/inbox/reviews").json["albums"]
    assert next(album for album in renamed if album["source_id"] == first_source_id)["id"] == first_id

    replacement = tmp_path / "replacement"
    replacement.mkdir()
    configure_named_inboxes(app, [replacement, roots[1]], ["Renamed", "Two"])
    changed = client.get("/api/inbox/reviews").json["albums"]
    assert all(album["id"] != first_id for album in changed)
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT present FROM library_inventory WHERE source_id=?", (first_source_id,)).fetchone()[0] == 0
        stale = db.execute(
            "SELECT state, candidate_status, selected_candidate_id FROM album_reviews WHERE id=?", (first_id,)
        ).fetchone()
        assert stale == ("pending", "stale", None)
        assert db.execute("SELECT COUNT(*) FROM metadata_candidates WHERE album_review_id=?", (first_id,)).fetchone()[0] == 0


def test_inbox_stat_identical_scan_avoids_tag_and_provider_reads(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Stable" / "01 Song.wav")
    client = app.test_client()
    first = client.get("/api/inbox/reviews").json["albums"][0]
    monkeypatch.setattr("beets_mvp._inbox_tag_snapshot", lambda path: pytest.fail("stat-identical tag reread"))
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: pytest.fail("ordinary reload provider call")

    second = client.get("/api/inbox/reviews").json["albums"][0]
    assert second["id"] == first["id"] and second["candidate_status"] == "not-run"


def test_inbox_landing_is_collapsed_and_never_scans_until_named_section_expands(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Deferred" / "01 Song.wav")
    monkeypatch.setattr("beets_mvp._scan_inbox_inventory", lambda *args: pytest.fail("landing scanned inbox"))

    page = app.test_client().get("/inbox")

    assert page.status_code == 200
    assert b'data-inbox-toggle' in page.data and b'aria-expanded="false"' in page.data
    assert b'class="album-list" data-album-list hidden' in page.data
    assert b"Never scanned" in page.data
    assert b"0 items" in page.data and b"built-in method items" not in page.data
    assert b"/scopes`" in page.data
    assert b"data-inbox-page" not in page.data


def test_inbox_expansion_is_one_lightweight_unpaginated_320_scope_response(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    for index in range(320):
        (tmp_path / "inbox" / f"Album {index:03d}").mkdir(parents=True)
    monkeypatch.setattr("beets_mvp._scan_inbox_inventory", lambda *args, **kwargs: pytest.fail("expansion scanned audio"))
    monkeypatch.setattr("beets_mvp._album_review_payloads", lambda *args, **kwargs: pytest.fail("expansion serialized albums"))
    inbox_id = app.config["INBOXES"][0]["id"]

    response = app.test_client().get(f"/api/inbox/{inbox_id}/scopes")

    assert response.status_code == 200
    assert response.json["total"] == len(response.json["scopes"]) == 320
    assert response.json["scopes"][0]["name"] == "Album 000"
    assert all("albums" not in scope for scope in response.json["scopes"])
    assert len(response.data) < 60_000


def test_inbox_selected_scope_scan_is_stable_id_isolated_and_keeps_loose_audio_distinct(tmp_path):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "inbox-two", tmp_path / "inbox-three"]
    roots[1].mkdir(); roots[2].mkdir()
    for root, artist in zip(roots, ("One", "Two", "Three")):
        tagged_inbox_wav(root / "Shared" / "01 Song.wav", artist=artist, album="Shared")
        tagged_inbox_wav(root / "Sibling" / "01 Song.wav", artist=artist, album="Sibling")
    tagged_inbox_wav(roots[0] / "Loose.wav", artist="Loose", album="Loose")
    configure_named_inboxes(app, roots, ["One", "Two", "Three"])
    first = app.config["INBOXES"][0]
    client = app.test_client()

    listing = client.get(f"/api/inbox/{first['id']}/scopes").json
    assert [scope["path"] for scope in listing["scopes"]] == [".", "Shared", "Sibling"]
    scanned = client.post(f"/api/inbox/{first['id']}/scopes/scan", json={"scopes": ["Shared"]})
    assert scanned.status_code == 201
    assert scanned.json["inventory"]["scopes"] == ["Shared"]

    with sqlite3.connect(app.config["APP_DB"]) as db:
        rows = db.execute(
            "SELECT source_id,relative_path,present FROM library_inventory WHERE source_kind='inbox'"
        ).fetchall()
    assert rows == [(first["id"], "Shared/01 Song.wav", 1)]
    assert (roots[0] / "Sibling" / "01 Song.wav").exists()
    assert (roots[0] / "Loose.wav").exists()
    assert all((root / "Shared" / "01 Song.wav").exists() for root in roots[1:])


def test_inbox_scoped_scan_stales_candidate_when_selected_track_is_removed(tmp_path):
    app = make_app(tmp_path)
    first = tmp_path / "inbox" / "Album" / "01 First.wav"
    second = tmp_path / "inbox" / "Album" / "02 Second.wav"
    tagged_inbox_wav(first, title="First", track=1)
    tagged_inbox_wav(second, title="Second", track=2)
    inbox = app.config["INBOXES"][0]
    client = app.test_client()
    assert client.post(f"/api/inbox/{inbox['id']}/scopes/scan", json={"scopes":["Album"]}).status_code == 201
    album = client.get("/api/inbox/reviews").json["albums"][0]
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query)]
    assert client.post("/api/inbox/candidates", json={"album_id":album["id"]}).json["album"]["candidate_status"] == "complete"

    second.unlink()
    rescanned = client.post(f"/api/inbox/{inbox['id']}/scopes/scan", json={"scopes":["Album"]})
    current = client.get(f"/api/inbox/albums/{album['id']}").json

    assert rescanned.status_code == 201 and rescanned.json["inventory"]["changed"] == 1
    assert current["candidate_status"] == "stale"
    assert len(current["tracks"]) == 1


def test_inbox_bulk_endpoints_reject_cross_source_and_require_confirmation(tmp_path):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "other"]
    roots[1].mkdir()
    for root in roots:
        tagged_inbox_wav(root / "Same" / "01 Song.wav")
    configure_named_inboxes(app, roots, ["First", "Second"])
    first, second = app.config["INBOXES"]
    client = app.test_client()
    assert client.post(f"/api/inbox/{first['id']}/scopes/scan", json={"scopes":["Same"]}).status_code == 201

    preview = client.post(f"/api/inbox/{second['id']}/bulk/preview", json={"scopes":["Same"]})
    execute = client.post(f"/api/inbox/{first['id']}/bulk/execute", json={"scopes":["Same"]})

    assert preview.status_code == 200 and preview.json["available_album_count"] == 0
    assert execute.status_code == 400 and execute.json["code"] == "confirmation_required"


def test_inbox_bulk_execute_revalidates_and_runs_only_ready_selected_scope(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Ready" / "01 Song.wav")
    tagged_inbox_wav(tmp_path / "inbox" / "Unselected" / "01 Other.wav", album="Other")
    inbox = app.config["INBOXES"][0]
    inbox["auto_match"] = True
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [exact_candidate(query)]
    client = app.test_client()
    scanned = client.post(f"/api/inbox/{inbox['id']}/scopes/scan", json={"scopes":["Ready"]})
    ready_id = scanned.json["ready_albums"][0]["id"]
    calls = []
    monkeypatch.setattr("beets_mvp._execute_inbox_album", lambda current_app, album_id, *, dry_run: (
        calls.append((album_id, dry_run)) or {"files":1, "status":"complete"}
    ))

    result = client.post(
        f"/api/inbox/{inbox['id']}/bulk/execute",
        json={"scopes":["Ready"], "confirmed":True},
    )

    assert result.status_code == 201
    assert calls == [(ready_id, True), (ready_id, False)]
    assert [item["album_review_id"] for item in result.json["imported"]] == [ready_id]
    assert (tmp_path / "inbox" / "Unselected" / "01 Other.wav").exists()


def test_inbox_ui_uses_per_source_bulk_folder_workflow_without_pagination(tmp_path):
    html = make_app(tmp_path).test_client().get("/inbox").get_data(as_text=True)
    for label in ("Select all", "Scan selected", "Import ready (0)", "Review exceptions (0)"):
        assert label in html
    assert "data-folder-select-all" in html and "data-folder-list" in html
    assert "/scopes/scan`" in html and "/bulk/preview`" in html and "/bulk/execute`" in html
    assert 'src="/static/folder-workflow.js"' in html
    assert "CratekeepFolderWorkflow.create" in html
    assert "data-folder-confirm" in html and "data-folder-confirm-dialog" in html
    assert "window.confirm" not in html
    assert "data-inbox-page" not in html and "review-pagination" not in html
    assert "execute:async album=>" in html and "body:JSON.stringify({dry_run:true})" in html and "body:'{}'" in html


def test_library_and_inbox_share_batched_folder_workflow_primitives(tmp_path):
    client = make_app(tmp_path).test_client()
    inbox = client.get("/inbox").get_data(as_text=True)
    settings = client.get("/settings").get_data(as_text=True)
    module = Path("beets_mvp/static/folder-workflow.js").read_text()
    assert "document.createDocumentFragment()" in module
    for page in (inbox, settings):
        assert 'src="/static/folder-workflow.js"' in page
        assert "CratekeepFolderWorkflow.create" in page
        for duplicate in ("async function scanSelected", "async function reviewExceptions", "function refreshFolderSelectionSummary"):
            assert duplicate not in page
    assert module.count("function create(") == 1
    assert "async function scan()" in module and "async function exceptions()" in module
    assert "adapter.selectLabel ? adapter.selectLabel(item) : `Select ${noun} ${item.name}`" in module
    assert "adapter.selectAllLabel || `Select all ${noun}s`" in module
    assert "selectAllLabel:'Select all folders on this page'" in settings
    assert "item.path==='.'?'Select loose files folder':`Select album folder ${item.name}`" in inbox
    assert "adapter.rowClass || 'review-folder'" in module
    assert "adapter.summaryClass || 'review-folder-summary'" in module
    assert "adapter.checkboxClass || 'review-select'" in module
    assert "adapter.countClass || 'review-folder-count'" in module
    assert "libraryFolders.load(2); libraryFolders.load(5);" in Path("tests/runtime_smoke.py").read_text()
    assert "if (failed) one(root, 'execute').querySelector('.button-label').textContent" in Path("beets_mvp/static/review-workflow.js").read_text()
    assert "focusFallback:()=>document.getElementById('library-import-search')" in settings


def test_shell_remains_responsive_while_scoped_inbox_scan_is_held(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    started, release = threading.Event(), threading.Event()
    def held_scan(current_app, inbox):
        started.set()
        assert release.wait(5)
        return {"files": 0, "bytes": 0, "changed": 0}
    monkeypatch.setattr("beets_mvp._scan_inbox_inventory", held_scan)
    inbox_id = app.config["INBOXES"][0]["id"]
    responses = []
    worker = threading.Thread(target=lambda: responses.append(
        app.test_client().post("/api/inbox/scan", json={"inbox_id": inbox_id})
    ))
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    shell = app.test_client().get("/inbox")
    elapsed = time.monotonic() - began
    release.set()
    worker.join(5)
    assert shell.status_code == 200 and elapsed < .5
    assert not worker.is_alive() and responses[0].status_code == 200


def test_overview_never_scans_or_calls_provider_while_inbox_scan_is_held(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    started, release = threading.Event(), threading.Event()
    def held_scan(current_app, inbox):
        started.set()
        assert release.wait(5)
        return {"files": 0, "bytes": 0, "changed": 0}
    monkeypatch.setattr("beets_mvp._scan_inbox_inventory", held_scan)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda *args, **kwargs: pytest.fail("overview called provider")
    inbox_id = app.config["INBOXES"][0]["id"]
    worker = threading.Thread(target=lambda: app.test_client().post(
        "/api/inbox/scan", json={"inbox_id": inbox_id, "page": 1, "refresh": True},
    ))
    worker.start()
    assert started.wait(2)
    began = time.monotonic()
    overview = app.test_client().get("/")
    elapsed = time.monotonic() - began
    release.set(); worker.join(5)
    assert overview.status_code == 200 and elapsed < .5
    assert b'id="sidebar"' in overview.data


def test_large_inbox_scan_serializes_only_requested_25_album_page(tmp_path):
    app = make_app(tmp_path)
    for index in range(60):
        tagged_inbox_wav(
            tmp_path / "inbox" / f"Album {index:02d}" / "01 Song.wav",
            artist=f"Artist {index:02d}", album=f"Album {index:02d}",
        )
    client = app.test_client(); inbox_id = app.config["INBOXES"][0]["id"]
    first = client.post("/api/inbox/scan", json={"inbox_id":inbox_id,"page":1,"refresh":True})
    second = client.post("/api/inbox/scan", json={"inbox_id":inbox_id,"page":2,"refresh":False})
    assert first.status_code == second.status_code == 200
    first_group, second_group = first.json["inboxes"][0], second.json["inboxes"][0]
    assert (first_group["total_albums"], first_group["page_count"], len(first_group["albums"])) == (60, 3, 25)
    assert (second_group["total_albums"], second_group["page"], len(second_group["albums"])) == (60, 2, 25)
    assert {album["id"] for album in first_group["albums"]}.isdisjoint(
        album["id"] for album in second_group["albums"]
    )
    assert len(first.data) < 250_000 and len(second.data) < 250_000


def test_per_inbox_auto_match_isolated_match_only_and_reload_idempotent(tmp_path):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "inbox-two"]
    roots[1].mkdir()
    for root, artist in zip(roots, ("Enabled Artist", "Disabled Artist")):
        tagged_inbox_wav(root / "Shared Folder" / "01 Song.wav", artist=artist, album="Shared Album")
    configure_named_inboxes(app, roots, ["Enabled", "Disabled"])
    enabled, disabled = app.config["INBOXES"]
    configure_named_inboxes(
        app, roots, ["Enabled", "Disabled"], inbox_auto_match_id=enabled["id"],
    )
    assert [row["auto_match"] for row in app.config["INBOXES"]] == [True, False]
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for root in roots for path in root.rglob("*.wav")
    }
    calls, active, maximum = [], 0, 0
    lock = threading.Lock()
    def provider(query, *, limit):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            calls.append(query["artist"])
            time.sleep(.02)
            return [exact_candidate(query, f"release-{query['artist']}")]
        finally:
            with lock:
                active -= 1
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    client = app.test_client()

    albums = client.get("/api/inbox/reviews").json["albums"]
    deadline = time.monotonic() + 5
    while app.config["INBOX_MATCH_RUNNING"] and time.monotonic() < deadline:
        time.sleep(.01)
    refreshed = client.get("/api/inbox/reviews").json["albums"]
    by_source = {album["source_id"]: album for album in refreshed}
    assert len({album["id"] for album in albums}) == 2
    assert by_source[enabled["id"]]["candidate_status"] == "complete"
    assert by_source[disabled["id"]]["candidate_status"] == "not-run"
    assert calls == ["Enabled Artist"] and maximum == 1

    manual = client.post("/api/inbox/candidates", json={"album_id": by_source[disabled["id"]]["id"]})
    assert manual.status_code == 201 and calls == ["Enabled Artist", "Disabled Artist"]
    client.get("/api/inbox/reviews")
    assert calls == ["Enabled Artist", "Disabled Artist"]
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM metadata_candidates").fetchone()[0] == 2
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}
    assert list((tmp_path / "library").iterdir()) == []


def test_large_multi_inbox_auto_match_batches_isolates_retries_and_never_mutates(tmp_path):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "inbox-two", tmp_path / "inbox-three"]
    roots[1].mkdir(); roots[2].mkdir()
    counts = [30, 15, 15]
    for source_index, (root, count) in enumerate(zip(roots, counts)):
        for index in range(count):
            # Folder names intentionally collide across roots; source identity must not.
            tagged_inbox_wav(
                root / f"Shared {index:02d}" / "01 Song.wav",
                artist=f"S{source_index}-Artist-{index:02d}", album=f"Album {index:02d}",
            )
    configure_named_inboxes(app, roots, ["Enabled A", "Enabled B", "Disabled"])
    enabled_a, enabled_b, disabled = app.config["INBOXES"]
    configure_named_inboxes(
        app, roots, ["Enabled A", "Enabled B", "Disabled"],
        inbox_auto_match_id=[enabled_a["id"], enabled_b["id"]],
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
              for root in roots for path in root.rglob("*") if path.is_file()}
    first_started, release_first = threading.Event(), threading.Event()
    calls, active, maximum, retry_errors = [], 0, 0, False
    lock = threading.Lock()
    def provider(query, *, limit):
        nonlocal active, maximum
        key = query["artist"]
        with lock:
            active += 1; maximum = max(maximum, active); calls.append(key)
            call_number = len(calls)
        try:
            if call_number == 1:
                first_started.set(); assert release_first.wait(5)
            index = int(key.rsplit("-", 1)[1])
            if index % 11 == 7:
                return []
            if index % 13 == 5 and not retry_errors:
                raise ProviderError("deterministic retry case")
            return [exact_candidate(query, f"release-{key}")]
        finally:
            with lock: active -= 1
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    client = app.test_client()
    scanned = client.get("/api/inbox/reviews")
    assert scanned.status_code == 200 and first_started.wait(2)
    second_page = client.post("/api/inbox/scan", json={
        "inbox_id":enabled_a["id"], "page":2, "refresh":False,
    }).json["inboxes"][0]
    assert sum(group["total_albums"] for group in scanned.json["inboxes"]) == 60
    albums = scanned.json["albums"] + second_page["albums"]
    assert len(albums) == 60 and len({album["id"] for album in albums}) == 60
    disabled_album = next(album for album in albums if album["source_id"] == disabled["id"])
    manual_responses = []
    manual = threading.Thread(target=lambda: manual_responses.append(
        app.test_client().post("/api/inbox/candidates", json={"album_id":disabled_album["id"]})
    ))
    manual.start(); release_first.set(); manual.join(10)
    assert not manual.is_alive() and manual_responses[0].status_code == 201
    deadline = time.monotonic() + 15
    while app.config["INBOX_MATCH_RUNNING"] and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert app.config["INBOX_MATCH_RUNNING"] is False and maximum == 1
    automatic_calls = [key for key in calls if not key.startswith("S2-")]
    assert len(automatic_calls) == 45 and len(set(automatic_calls)) == 45
    assert [key for key in calls if key.startswith("S2-")] == [disabled_album["artist"]]

    reloaded = client.get("/api/inbox/reviews").json["albums"]
    reloaded += client.post("/api/inbox/scan", json={
        "inbox_id":enabled_a["id"], "page":2, "refresh":False,
    }).json["inboxes"][0]["albums"]
    assert len(calls) == 46
    by_source = {source_id: [album for album in reloaded if album["source_id"] == source_id]
                 for source_id in (enabled_a["id"], enabled_b["id"], disabled["id"])}
    assert len(by_source[enabled_a["id"]]) == 30 and len(by_source[enabled_b["id"]]) == 15
    assert all(album["candidate_status"] == "not-run" for album in by_source[disabled["id"]]
               if album["id"] != disabled_album["id"])
    failures = [album for album in reloaded if album["candidate_status"] == "error"]
    failed_call_counts = {album["artist"]: calls.count(album["artist"]) for album in failures}
    assert failures and set(failed_call_counts.values()) == {1}
    retry_errors = True
    retried = client.post("/api/inbox/candidates", json={"album_id":failures[0]["id"]})
    assert retried.status_code == 201 and calls.count(failures[0]["artist"]) == 2
    assert all(calls.count(album["artist"]) == 1 for album in failures[1:])

    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT COUNT(*) FROM import_jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM adoption_jobs WHERE kind NOT IN ('musicbrainz_candidates','inbox_inventory')").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM album_reviews WHERE state!='pending'").fetchone()[0] == 0
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before}
    assert list((tmp_path / "library").iterdir()) == []


def test_inbox_manual_match_uses_shared_candidate_and_never_auto_approves(tmp_path):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Manual" / "01 Song.wav")
    calls = []
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: calls.append(query) or [exact_candidate(query)]
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]

    matched = client.post("/api/inbox/candidates", json={"album_id": album["id"]})
    assert matched.status_code == 201 and len(calls) == 1
    result = matched.json["album"]
    assert result["candidates"][0]["provider_id"] == "release-1"
    assert result["decision"] == "pending"
    assert result["execution_status"] == "not-run"


def test_legacy_execute_cannot_bypass_a_root_level_reviewed_album(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Root Song.wav"
    tagged_inbox_wav(source, album="Root Album")
    client = app.test_client()
    assert len(client.get("/api/inbox/reviews").json["albums"]) == 1
    preview = client.post("/api/imports/preview", json={"path":"Root Song.wav"})
    assert preview.status_code == 201
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, **kwargs: pytest.fail("must stay reviewed"))

    response = client.post(f"/api/imports/{preview.json['id']}/execute")
    assert response.status_code == 409
    assert b"requires an approved review" in response.data


def test_inbox_auto_match_setting_defaults_off_and_only_schedules_lookup(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Auto" / "01 Song.wav")
    scheduled = []
    monkeypatch.setattr("beets_mvp._start_inbox_auto_match", lambda current_app, ids: scheduled.extend(ids))
    client = app.test_client()
    assert app.config["INBOX_AUTO_MATCH"] is False
    client.get("/api/inbox/reviews")
    assert scheduled == []
    inbox = app.config["INBOXES"][0]
    response = client.post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":[inbox["name"]], "inbox_path":[inbox["path"]],
        "library_path":app.config["LIBRARY_PATH"], "inbox_auto_match_id":inbox["id"],
    })
    assert response.status_code == 302 and app.config["INBOX_AUTO_MATCH"] is True
    client.get("/api/inbox/reviews")
    assert len(scheduled) == 1
    assert (tmp_path / "inbox" / "Auto" / "01 Song.wav").exists()
    assert list((tmp_path / "library").iterdir()) == []


def test_inbox_auto_match_worker_drains_new_work_and_skips_manual_completion(tmp_path):
    import beets_mvp
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "A" / "01 Song.wav", album="Album A")
    tagged_inbox_wav(tmp_path / "inbox" / "B" / "01 Song.wav", album="Album B")
    albums = app.test_client().get("/api/inbox/reviews").json["albums"]
    by_name = {album["album"]: album for album in albums}
    started, release = threading.Event(), threading.Event()
    calls = []
    def provider(query, *, limit):
        calls.append(query["album"])
        if query["album"] == "Album A":
            started.set()
            assert release.wait(5)
        return [exact_candidate(query, f"release-{query['album']}")]
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    first, second = by_name["Album A"], by_name["Album B"]
    beets_mvp._start_inbox_auto_match(app, [first["id"]])
    assert started.wait(5)
    beets_mvp._start_inbox_auto_match(app, [second["id"]])
    # A manual lookup completing B while it is queued must make the worker's
    # final status recheck skip a second provider request.
    beets_mvp._generate_musicbrainz_candidates(
        app, 1, album_ids=[second["id"]], root=app.config["INBOXES"][0]["path"],
        source_kind="inbox", source_id=second["source_id"],
    )
    release.set()
    deadline = time.monotonic() + 5
    while app.config["INBOX_MATCH_RUNNING"] and time.monotonic() < deadline:
        time.sleep(.01)
    assert app.config["INBOX_MATCH_RUNNING"] is False
    assert calls.count("Album A") == calls.count("Album B") == 1
    refreshed = app.test_client().get("/api/inbox/reviews").json["albums"]
    assert all(album["candidate_status"] == "complete" for album in refreshed)


def test_inbox_manual_match_racing_auto_match_does_not_repeat_provider(tmp_path):
    import beets_mvp
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Race" / "01 Song.wav", album="Race Album")
    album = app.test_client().get("/api/inbox/reviews").json["albums"][0]
    provider_started, provider_release, manual_started = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def provider(query, *, limit):
        calls.append(query["album"])
        provider_started.set()
        assert provider_release.wait(5)
        return [exact_candidate(query)]
    app.config["MUSICBRAINZ_PROVIDER"] = provider
    beets_mvp._start_inbox_auto_match(app, [album["id"]])
    assert provider_started.wait(5)
    responses = []
    def manual_match():
        manual_started.set()
        responses.append(app.test_client().post("/api/inbox/candidates", json={"album_id":album["id"]}))
    thread = threading.Thread(target=manual_match)
    thread.start()
    assert manual_started.wait(5)
    provider_release.set()
    thread.join(5)
    assert not thread.is_alive() and responses[0].status_code == 201
    assert responses[0].json["result"]["already_matched"] is True
    assert calls == ["Race Album"]


def test_settings_rejects_library_inbox_overlap_and_symlink_root(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    inbox = app.config["INBOXES"][0]
    nested = Path(app.config["LIBRARY_PATH"]) / "incoming"
    response = client.post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":["Inbox"], "inbox_path":[str(nested)],
        "library_path":app.config["LIBRARY_PATH"],
    })
    assert response.status_code == 200
    assert b"must not be equal or contain one another" in response.data
    target = tmp_path / "real-inbox"; target.mkdir()
    alias = tmp_path / "alias-inbox"; alias.symlink_to(target, target_is_directory=True)
    response = client.post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":["Inbox"], "inbox_path":[str(alias)],
        "library_path":app.config["LIBRARY_PATH"],
    })
    assert response.status_code == 200
    assert b"must not be a symlink or filesystem alias" in response.data

    real_library = tmp_path / "real-library"; real_library.mkdir()
    alias_library = tmp_path / "alias-library"; alias_library.symlink_to(real_library, target_is_directory=True)
    response = client.post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":["Inbox"], "inbox_path":[inbox["path"]],
        "library_path":str(alias_library),
    })
    assert response.status_code == 200
    assert b"Library path must not be a symlink or filesystem alias" in response.data


def test_generated_beets_paths_are_live_compatible_and_custom_paths_are_preserved(tmp_path):
    app = make_app(tmp_path)
    config_path = Path(app.config["BEETS_CONFIG"])
    generated = yaml.safe_load(config_path.read_text())
    assert generated["paths"] == {
        "default": "$albumartist/$album/$track $title",
        "singleton": "Non-Album/$artist/$title",
        "comp": "Compilations/$album/$track $title",
    }
    assert "%aunique" not in config_path.read_text() and "year" not in generated["paths"]["default"]

    custom = {
        "default": "$albumartist/Custom/$album/$track $title",
        "singleton": "Singles/$artist/$title",
        "comp": "Various/$album/$track $title",
        "albumtype:soundtrack": "Soundtracks/$album/$track $title",
    }
    inbox = app.config["INBOXES"][0]
    response = app.test_client().post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":[inbox["name"]], "inbox_path":[inbox["path"]],
        "library_path":app.config["LIBRARY_PATH"],
        "beets_config":yaml.safe_dump({"paths": custom}, sort_keys=False),
    })
    assert response.status_code == 302
    assert yaml.safe_load(config_path.read_text())["paths"] == custom


def test_settings_render_failure_does_not_stale_existing_inbox_review(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Stable" / "01 Song.wav"
    tagged_inbox_wav(source)
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    original_config = Path(app.config["BEETS_CONFIG"]).read_bytes()
    replacement = tmp_path / "replacement"; replacement.mkdir()
    import beets_mvp
    original_stale = beets_mvp._stale_changed_inbox_roots

    def fail_after_staging(database, current, inboxes):
        original_stale(database, current, inboxes)
        raise OSError("fixture settings database failure")

    monkeypatch.setattr("beets_mvp._stale_changed_inbox_roots", fail_after_staging)
    inbox = app.config["INBOXES"][0]
    with pytest.raises(OSError, match="fixture settings database failure"):
        client.post("/settings", data={
            "inbox_id":[inbox["id"]], "inbox_name":[inbox["name"]], "inbox_path":[str(replacement)],
            "library_path":app.config["LIBRARY_PATH"],
        })
    assert Path(app.config["BEETS_CONFIG"]).read_bytes() == original_config
    monkeypatch.setattr("beets_mvp._stale_changed_inbox_roots", original_stale)
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT state, candidate_status FROM album_reviews WHERE id=?", (album["id"],)).fetchone() == (
            "pending", "not-run"
        )
        assert db.execute("SELECT present FROM library_inventory WHERE root_path=?", (str(tmp_path / "inbox"),)).fetchone()[0] == 1


def test_settings_apply_failure_rolls_back_config_database_and_runtime(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / "inbox" / "Stable" / "01 Song.wav")
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    inbox = app.config["INBOXES"][0]
    original_config = Path(app.config["BEETS_CONFIG"]).read_bytes()
    original_apply = beets_mvp._apply_settings
    replacement = tmp_path / "replacement"; replacement.mkdir()

    def fail_new_settings(current_app, settings):
        original_apply(current_app, settings)
        if str(replacement) in settings.get("inboxes_json", ""):
            raise OSError("fixture Beets apply failure")

    monkeypatch.setattr("beets_mvp._apply_settings", fail_new_settings)
    with pytest.raises(OSError, match="fixture Beets apply failure"):
        client.post("/settings", data={
            "inbox_id":[inbox["id"]], "inbox_name":[inbox["name"]], "inbox_path":[str(replacement)],
            "library_path":app.config["LIBRARY_PATH"],
        })
    assert Path(app.config["BEETS_CONFIG"]).read_bytes() == original_config
    assert app.config["INBOXES"][0]["path"] == inbox["path"]
    with sqlite3.connect(app.config["APP_DB"]) as db:
        assert db.execute("SELECT state,candidate_status FROM album_reviews WHERE id=?", (album["id"],)).fetchone() == (
            "pending", "not-run"
        )
        assert db.execute("SELECT present FROM library_inventory WHERE root_path=?", (inbox["path"],)).fetchone()[0] == 1


def test_reviewed_as_is_moves_with_beets_default_album_path_and_is_idempotent(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Move Me" / "01 Song.wav"
    tagged_inbox_wav(source)
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    approved = client.patch(
        f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None}
    )
    assert approved.status_code == 200 and approved.json["selection_mode"] == "as-is"
    preview = client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run":True})
    assert preview.status_code == 200
    assert preview.json["destinations"] == ["Artist/Album/01 Song.wav"]
    executed = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert executed.status_code == 201
    destination = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    assert destination.exists() and not source.exists()
    assert executed.json["items"][0]["path"] == str(destination)
    retried = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert retried.status_code == 200 and retried.json["already_complete"] is True


def test_reviewed_as_is_real_beets_move_honors_custom_paths_mapping(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    inbox = app.config["INBOXES"][0]
    custom = {"default":"$albumartist/Custom/$album/$track $title",
              "singleton":"Singles/$artist/$title", "comp":"Various/$album/$track $title"}
    response = app.test_client().post("/settings", data={
        "inbox_id":[inbox["id"]], "inbox_name":[inbox["name"]], "inbox_path":[inbox["path"]],
        "library_path":app.config["LIBRARY_PATH"], "beets_config":yaml.safe_dump({"paths":custom}),
    })
    assert response.status_code == 302
    source = tmp_path / "inbox" / "Custom Move" / "01 Song.wav"
    tagged_inbox_wav(source)
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    client.patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})
    preview = client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run":True})
    assert preview.status_code == 200
    assert preview.json["destinations"] == ["Artist/Custom/Album/01 Song.wav"]
    executed = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    destination = tmp_path / "library" / "Artist" / "Custom" / "Album" / "01 Song.wav"
    assert executed.status_code == 201 and destination.exists() and not source.exists()
    assert executed.json["items"][0]["path"] == str(destination)


def test_inbox_execution_blocks_track_exceptions_and_destination_collision(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Blocked" / "01 Song.wav"
    tagged_inbox_wav(source)
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    track_id = album["tracks"][0]["id"]
    client.patch(f"/api/inbox/albums/{album['id']}", json={
        "decision":"approved", "candidate_id":None, "track_exceptions":{str(track_id):"skipped"},
    })
    blocked = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert blocked.status_code == 409 and blocked.json["code"] == "partial_album_not_supported"
    client.patch(f"/api/inbox/albums/{album['id']}", json={
        "decision":"approved", "candidate_id":None, "track_exceptions":{str(track_id):None},
    })
    collision = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    collision.parent.mkdir(parents=True); collision.write_bytes(b"unrelated")
    blocked = client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run":True})
    assert blocked.status_code == 409 and blocked.json["code"] == "destination_collision"
    assert source.exists() and collision.read_bytes() == b"unrelated"


def test_inbox_execution_revalidates_after_preflight_and_audits_stale_source(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Changed" / "01 Song.wav"
    tagged_inbox_wav(source)
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    client.patch(
        f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None}
    )
    calls = []
    def changing_preflight(current_app, selection, *, pretend=False):
        calls.append(pretend)
        assert pretend is True
        current = source.stat()
        os.utime(source, ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
        return subprocess.CompletedProcess(["beet"], 0, "preflight", "")
    monkeypatch.setattr("beets_mvp._run_beets_import", changing_preflight)

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 409 and response.json["code"] == "inventory_stale"
    assert calls == [True] and source.exists()
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT execution_status, execution_error FROM album_reviews WHERE id=?", (album["id"],)
        ).fetchone()
        job = db.execute(
            "SELECT status, error FROM adoption_jobs WHERE kind='inbox_import_execute' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert review[0] == job[0] == "failed"
    assert "changed after inventory preview" in review[1]
    assert "changed after inventory preview" in job[1]


def test_inbox_shared_non_root_folder_imports_exact_review_and_preserves_sibling(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Mixed"
    tagged_inbox_wav(source / "01 First.wav", album="First", title="First")
    tagged_inbox_wav(source / "01 Second.wav", album="Second", title="Second")
    sidecar = source / "notes.txt"
    sidecar.write_bytes(b"unrelated sidecar bytes")
    cover = source / "cover.jpg"
    cover.write_bytes(jpeg_bytes((19, 41, 73)))
    first_path, second_path = source / "01 First.wav", source / "01 Second.wav"
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (first_path, second_path, sidecar, cover)}
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    calls = []
    real_run = beets_mvp._run_beets_import
    def record_run(current_app, selection, *, pretend=False):
        selection = Path(selection)
        calls.append((selection, pretend, sorted(path.name for path in selection.rglob("*") if path.is_file())))
        return real_run(current_app, selection, pretend=pretend)
    monkeypatch.setattr("beets_mvp._run_beets_import", record_run)
    client = app.test_client()
    albums = client.get("/api/inbox/reviews").json["albums"]
    assert {album["album"] for album in albums} == {"First", "Second"}
    first = next(album for album in albums if album["album"] == "First")
    second = next(album for album in albums if album["album"] == "Second")
    client.patch(
        f"/api/inbox/albums/{first['id']}", json={"decision":"approved", "candidate_id":None}
    )

    response = client.post(f"/api/inbox/albums/{first['id']}/execute", json={})

    assert response.status_code == 201
    assert response.json["execution_selection"] == "staged-album"
    assert response.json["source_artwork"]["status"] == "ignored-shared-source"
    assert len(calls) == 2 and all(selection != source for selection, _, _ in calls)
    assert all(files == ["01 First.wav"] for _, _, files in calls)
    assert not first_path.exists()
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (second_path, sidecar, cover)} == {
        path: before[path] for path in (second_path, sidecar, cover)
    }
    remaining = client.get("/api/inbox/reviews").json["albums"]
    sibling = next(album for album in remaining if album["id"] == second["id"])
    assert sibling["execution_status"] != "complete"
    assert sibling["tracks"][0]["path"] == "Mixed/01 Second.wav"


def test_inbox_immediate_sibling_folders_import_independently_with_real_beets(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    sources = {
        "First": tmp_path / "inbox" / "First Folder" / "01 First.wav",
        "Second": tmp_path / "inbox" / "Second Folder" / "01 Second.wav",
    }
    for index, (album_name, path) in enumerate(sources.items(), 1):
        tagged_inbox_wav(path, album=album_name, title=album_name, track=index)
    original_second = sources["Second"].read_bytes()
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    albums = {album["album"]: album for album in client.get("/api/inbox/reviews").json["albums"]}

    for album_name in ("First", "Second"):
        album = albums[album_name]
        assert client.patch(
            f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None}
        ).status_code == 200
        response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
        assert response.status_code == 201
        assert response.json["execution_selection"] == "exclusive-scope"
        if album_name == "First":
            assert sources["Second"].read_bytes() == original_second
            remaining = client.get("/api/inbox/reviews").json["albums"]
            sibling = next(item for item in remaining if item["id"] == albums["Second"]["id"])
            assert sibling["execution_status"] != "complete"
    assert not any(path.exists() for path in sources.values())


def test_inbox_root_level_singles_use_exact_path_and_leave_sibling_importable(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    sources = {
        "First": tmp_path / "inbox" / "First.wav",
        "Second": tmp_path / "inbox" / "Second.wav",
    }
    for album_name, path in sources.items():
        tagged_inbox_wav(path, album=album_name, title=album_name)
    original_second = sources["Second"].read_bytes()
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    calls = []
    real_run = beets_mvp._run_beets_import
    def record_run(current_app, selection, *, pretend=False):
        command, _ = beets_mvp._beets_import_command(current_app, Path(selection), pretend=pretend)
        calls.append((Path(selection), pretend, command))
        return real_run(current_app, selection, pretend=pretend)
    monkeypatch.setattr("beets_mvp._run_beets_import", record_run)
    client = app.test_client()
    albums = {album["album"]: album for album in client.get("/api/inbox/reviews").json["albums"]}

    first = albums["First"]
    client.patch(f"/api/inbox/albums/{first['id']}", json={"decision":"approved", "candidate_id":None})
    preview = client.post(f"/api/inbox/albums/{first['id']}/execute", json={"dry_run":True})
    executed = client.post(f"/api/inbox/albums/{first['id']}/execute", json={})
    assert preview.status_code == 200 and executed.status_code == 201
    assert preview.json["execution_selection"] == executed.json["execution_selection"] == "exact-file"
    assert [path for path, _, _ in calls] == [sources["First"], sources["First"], sources["First"]]
    assert all(command[-1] == str(sources["First"]) for _, _, command in calls)
    assert all(str(tmp_path / "inbox") not in command for _, _, command in calls)
    assert sources["Second"].read_bytes() == original_second
    assert not sources["First"].exists()

    second = albums["Second"]
    client.patch(f"/api/inbox/albums/{second['id']}", json={"decision":"approved", "candidate_id":None})
    imported = client.post(f"/api/inbox/albums/{second['id']}/execute", json={})
    assert imported.status_code == 201 and not sources["Second"].exists()


def test_exact_selection_isolated_by_stable_id_across_named_inbox_roots(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    roots = [tmp_path / "inbox", tmp_path / "inbox-two"]
    roots[1].mkdir()
    first = roots[0] / "Shared.wav"
    second = roots[1] / "Shared.wav"
    tagged_inbox_wav(first, artist="First Artist", album="First Album", title="Shared")
    tagged_inbox_wav(second, artist="Second Artist", album="Second Album", title="Shared")
    second_bytes = second.read_bytes()
    configure_named_inboxes(app, roots, ["One", "Two"])
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    albums = client.get("/api/inbox/reviews").json["albums"]
    one = next(album for album in albums if album["source_id"] == app.config["INBOXES"][0]["id"])
    two = next(album for album in albums if album["source_id"] == app.config["INBOXES"][1]["id"])
    client.patch(f"/api/inbox/albums/{one['id']}", json={"decision":"approved", "candidate_id":None})

    imported = client.post(f"/api/inbox/albums/{one['id']}/execute", json={})

    assert imported.status_code == 201 and not first.exists()
    assert second.read_bytes() == second_bytes
    refreshed = client.get(f"/api/inbox/albums/{two['id']}")
    assert refreshed.status_code == 200
    assert refreshed.json["source_id"] == app.config["INBOXES"][1]["id"]


def test_reviewed_legacy_root_file_keeps_album_id_and_decision_during_scope_upgrade(tmp_path):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Reviewed.wav"
    tagged_inbox_wav(source, album="Reviewed")
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    approved = client.patch(
        f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None}
    )
    assert approved.status_code == 200
    with sqlite3.connect(app.config["APP_DB"]) as db:
        db.execute(
            "UPDATE album_reviews SET artist_key=? WHERE id=?",
            (f"reviewed.wav\x1fartist", album["id"]),
        )

    rescanned = client.post("/api/inbox/scan", json={
        "inbox_id": album["source_id"], "refresh": True,
    })

    assert rescanned.status_code == 200
    current = client.get(f"/api/inbox/albums/{album['id']}")
    assert current.status_code == 200
    assert current.json["decision"] == "approved"
    assert current.json["selection_mode"] == "as-is"


def test_root_level_multitrack_album_uses_exact_staging_and_preserves_unrelated_audio(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    inbox = tmp_path / "inbox"
    selected = [inbox / "01 First.wav", inbox / "02 Second.wav"]
    tagged_inbox_wav(selected[0], album="Pair", title="First", track=1)
    tagged_inbox_wav(selected[1], album="Pair", title="Second", track=2)
    unrelated = inbox / "Other.wav"
    tagged_inbox_wav(unrelated, album="Other", title="Other")
    unrelated_bytes = unrelated.read_bytes()
    custom = {"default":"$albumartist/Custom/$album/$track $title",
              "singleton":"Singles/$artist/$title", "comp":"Various/$album/$track $title"}
    configured = app.config["INBOXES"][0]
    assert app.test_client().post("/settings", data={
        "inbox_id":[configured["id"]], "inbox_name":[configured["name"]],
        "inbox_path":[configured["path"]], "library_path":app.config["LIBRARY_PATH"],
        "beets_config":yaml.safe_dump({"paths":custom}),
    }).status_code == 302
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    calls = []
    real_run = beets_mvp._run_beets_import
    def record_run(current_app, selection, *, pretend=False):
        selection = Path(selection)
        command, _ = beets_mvp._beets_import_command(current_app, selection, pretend=pretend)
        calls.append({
            "selection": selection, "pretend": pretend,
            "files": sorted(path.name for path in selection.rglob("*") if path.is_file()),
            "argv": command,
        })
        return real_run(current_app, selection, pretend=pretend)
    monkeypatch.setattr("beets_mvp._run_beets_import", record_run)
    client = app.test_client()
    albums = {album["album"]: album for album in client.get("/api/inbox/reviews").json["albums"]}
    pair = albums["Pair"]
    client.patch(f"/api/inbox/albums/{pair['id']}", json={"decision":"approved", "candidate_id":None})

    response = client.post(f"/api/inbox/albums/{pair['id']}/execute", json={})

    assert response.status_code == 201 and response.json["execution_selection"] == "staged-album"
    assert len(calls) == 2 and all(call["selection"] != inbox for call in calls)
    assert all(call["files"] == ["01 First.wav", "02 Second.wav"] for call in calls)
    assert all(call["argv"][-1] == str(call["selection"]) for call in calls)
    assert all(str(inbox) not in call["argv"] for call in calls)
    assert unrelated.read_bytes() == unrelated_bytes
    assert not any(path.exists() for path in selected)
    destinations = [tmp_path / "library" / relative for relative in response.json["destinations"]]
    assert [path.relative_to(tmp_path / "library").as_posix() for path in destinations] == [
        "Artist/Custom/Pair/01 First.wav", "Artist/Custom/Pair/02 Second.wav",
    ]
    assert all(path.exists() for path in destinations)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    imported = list(library.albums())
    assert len(imported) == 1 and len(list(imported[0].items())) == 2


@pytest.mark.parametrize("failure_at", ["preflight", "execution"])
def test_root_level_exact_staging_failure_restores_selected_and_unrelated_sources(
    tmp_path, monkeypatch, failure_at,
):
    app = make_app(tmp_path)
    inbox = tmp_path / "inbox"
    selected = [inbox / "01 First.wav", inbox / "02 Second.wav"]
    tagged_inbox_wav(selected[0], album="Pair", title="First", track=1)
    tagged_inbox_wav(selected[1], album="Pair", title="Second", track=2)
    unrelated = inbox / "Other.wav"
    tagged_inbox_wav(unrelated, album="Other", title="Other")
    before = {path: path.read_bytes() for path in [*selected, unrelated]}
    client = app.test_client()
    pair = next(album for album in client.get("/api/inbox/reviews").json["albums"] if album["album"] == "Pair")
    client.patch(f"/api/inbox/albums/{pair['id']}", json={"decision":"approved", "candidate_id":None})
    def fail(current_app, selection, *, pretend=False):
        return subprocess.CompletedProcess(
            ["beet"], 9 if (pretend is (failure_at == "preflight")) else 0, "", "fixture failure",
        )
    monkeypatch.setattr("beets_mvp._run_beets_import", fail)

    response = client.post(f"/api/inbox/albums/{pair['id']}/execute", json={})

    assert response.status_code == 502
    assert response.json["code"] == ("beets_preflight_failed" if failure_at == "preflight" else "beets_import_failed")
    assert before == {path: path.read_bytes() for path in before}
    assert not list((tmp_path / "library").rglob("*.wav"))


def test_root_level_exact_staging_detects_source_change_and_restores_before_beets(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    inbox = tmp_path / "inbox"
    selected = [inbox / "01 First.wav", inbox / "02 Second.wav"]
    tagged_inbox_wav(selected[0], album="Pair", title="First", track=1)
    tagged_inbox_wav(selected[1], album="Pair", title="Second", track=2)
    unrelated = inbox / "Other.wav"
    tagged_inbox_wav(unrelated, album="Other", title="Other")
    before = {path: path.read_bytes() for path in [*selected, unrelated]}
    client = app.test_client()
    pair = next(album for album in client.get("/api/inbox/reviews").json["albums"] if album["album"] == "Pair")
    client.patch(f"/api/inbox/albums/{pair['id']}", json={"decision":"approved", "candidate_id":None})
    original_stage = beets_mvp._stage_exact_inbox_album
    calls = []
    stage_calls = 0
    def change_after_stage(directory, paths):
        nonlocal stage_calls
        stage_calls += 1
        selection = original_stage(directory, paths)
        if stage_calls == 2:
            current = paths[0].stat()
            os.utime(paths[0], ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000))
        return selection
    def preflight_only(current_app, selection, *, pretend=False):
        calls.append(pretend)
        return subprocess.CompletedProcess(["beet"], 0, "fixture preflight", "")
    monkeypatch.setattr("beets_mvp._stage_exact_inbox_album", change_after_stage)
    monkeypatch.setattr("beets_mvp._run_beets_import", preflight_only)

    response = client.post(f"/api/inbox/albums/{pair['id']}/execute", json={})

    assert response.status_code == 502 and response.json["code"] == "beets_import_failed"
    assert calls == [True]
    assert before == {path: path.read_bytes() for path in before}


def test_root_level_shared_sidecar_is_left_untouched_and_not_attached(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    inbox = tmp_path / "inbox"
    first, second = inbox / "First.wav", inbox / "Second.wav"
    tagged_inbox_wav(first, album="First", title="First")
    tagged_inbox_wav(second, album="Second", title="Second")
    cover = inbox / "cover.jpg"
    artwork = jpeg_bytes((37, 73, 109))
    cover.write_bytes(artwork)
    second_bytes = second.read_bytes()
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    client = app.test_client()
    album = next(item for item in client.get("/api/inbox/reviews").json["albums"] if item["album"] == "First")
    client.patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})

    assert response.status_code == 201
    assert response.json["source_artwork"]["status"] == "ignored-shared-source"
    assert cover.read_bytes() == artwork and second.read_bytes() == second_bytes
    destination = Path(response.json["items"][0]["path"])
    assert not (destination.parent / "cover.jpg").exists()


def test_inbox_beets_failure_restores_candidate_tags_and_audits_retry(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Failure" / "01 Song.wav"
    tagged_inbox_wav(source)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [{
        **exact_candidate(query), "artist":"Changed Artist", "album":"Changed Album",
        "tracks":[{"title":"Changed Song", "artist":"Changed Artist", "position":"1",
                   "recording_id":"recording-changed"}],
    }]
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    matched = client.post("/api/inbox/candidates", json={"album_id":album["id"]}).json["album"]
    candidate_id = matched["candidates"][0]["id"]
    client.patch(f"/api/inbox/albums/{album['id']}", json={
        "decision":"approved", "candidate_id":candidate_id,
    })
    calls = []
    def failed_import(current_app, selection, *, pretend=False):
        calls.append(pretend)
        return subprocess.CompletedProcess(["beet"], 0 if pretend else 9, "", "fixture beets failure")
    monkeypatch.setattr("beets_mvp._run_beets_import", failed_import)

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 502 and response.json["code"] == "beets_import_failed"
    assert calls == [True, False] and source.exists()
    restored = MediaFile(str(source))
    assert (restored.artist, restored.album, restored.title) == ("Artist", "Album", "Song")
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT execution_status, execution_error, last_execution_job_id FROM album_reviews WHERE id=?",
            (album["id"],),
        ).fetchone()
        assert review[0] == "failed" and "fixture beets failure" in review[1] and review[2]


def test_inbox_beets_zero_without_exact_move_fails_closed_and_remains_retryable(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Incomplete" / "01 Song.wav"
    tagged_inbox_wav(source)
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    client.patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})
    calls = []
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, pretend=False, **kwargs: (
        calls.append(pretend) or subprocess.CompletedProcess(["beet"], 0, "fixture success", "")
    ))

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 502 and response.json["code"] == "beets_incomplete"
    assert calls == [True, False] and source.exists()
    assert not (tmp_path / "library" / "Artist" / "Album" / "01 Song.wav").exists()
    with sqlite3.connect(app.config["APP_DB"]) as db:
        review = db.execute(
            "SELECT execution_status, executed_at FROM album_reviews WHERE id=?", (album["id"],)
        ).fetchone()
        inventory = db.execute(
            "SELECT present FROM library_inventory WHERE source_kind='inbox' AND relative_path='Incomplete/01 Song.wav'"
        ).fetchone()
        summary = json.loads(db.execute(
            "SELECT summary_json FROM adoption_jobs WHERE kind='inbox_import_execute' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
    assert review == ("failed", None) and inventory[0] == 1
    assert summary["status"] == "failed" and summary["verification"]["expected"] == 1
    assert summary["verification"]["verified"] == 0


def test_inbox_exact_destination_identity_ignores_unrelated_concurrent_beets_ids(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Exact" / "01 Song.wav"
    tagged_inbox_wav(source)
    client = app.test_client()
    album = client.get("/api/inbox/reviews").json["albums"][0]
    client.patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})

    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source, add_unrelated=True)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")

    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 201
    assert [item["path"] for item in response.json["items"]] == [
        str(tmp_path / "library" / "Artist" / "Album" / "01 Song.wav")
    ]
    assert all("Concurrent" not in item["path"] for item in response.json["items"])


def test_inbox_execute_rejects_stale_or_old_schema_candidate_evidence(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Stale Candidate" / "01 Song.wav"
    tagged_inbox_wav(source)
    client = app.test_client()
    album = approve_inbox_candidate(client, app)
    with sqlite3.connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE album_reviews SET candidate_status='stale', candidate_error='refresh required' WHERE id=?", (album["id"],))
        db.execute("UPDATE metadata_candidates SET schema_version=1 WHERE id=?", (album["selected_candidate_id"],))
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, **kwargs: pytest.fail("Beets must not run"))
    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run": True})
    assert response.status_code == 409 and response.json["code"] == "candidate_stale"
    assert source.exists()


def test_inbox_exact_verifier_rejects_two_aliases_of_one_file_and_item(tmp_path):
    import beets_mvp
    app = make_app(tmp_path)
    destination = tmp_path / "library" / "Real" / "Album" / "01 Song.wav"
    tagged_inbox_wav(destination)
    alias = tmp_path / "library" / "Alias"; alias.symlink_to(destination.parent.parent, target_is_directory=True)
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])
    library.add(Item.from_path(destination))
    with pytest.raises(LibraryImportExecutionError) as failure:
        beets_mvp._verify_inbox_move(
            app, [tmp_path / "missing-one.wav", tmp_path / "missing-two.wav"],
            ["Real/Album/01 Song.wav", "Alias/Album/01 Song.wav"],
        )
    assert failure.value.code == "beets_incomplete"
    assert "duplicates another expected destination" in str(failure.value)


def test_inbox_execution_lock_rejects_overlapping_request_without_running_beets(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Locked" / "01 Song.wav"
    tagged_inbox_wav(source)
    album = app.test_client().get("/api/inbox/reviews").json["albums"][0]
    app.test_client().patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, **kwargs: pytest.fail("second request must not run"))
    lock = app.config["INBOX_EXECUTION_LOCK"]
    lock.acquire()
    try:
        response = app.test_client().post(f"/api/inbox/albums/{album['id']}/execute", json={})
    finally:
        lock.release()
    assert response.status_code == 409 and response.json["code"] == "execution_in_progress"


def test_replaced_library_root_symlink_fails_closed_after_restart(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    source = tmp_path / "inbox" / "Safe" / "01 Song.wav"
    tagged_inbox_wav(source)
    album = app.test_client().get("/api/inbox/reviews").json["albums"][0]
    app.test_client().patch(f"/api/inbox/albums/{album['id']}", json={"decision":"approved", "candidate_id":None})
    library = tmp_path / "library"; library.rmdir()
    target = tmp_path / "redirect-target"; target.mkdir()
    library.symlink_to(target, target_is_directory=True)
    restarted = create_app({
        "TESTING": True, "SECRET_KEY": "test", "STATE_PATH": app.config["STATE_PATH"],
        "BROWSE_ROOTS": [str(tmp_path)],
    })
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, **kwargs: pytest.fail("Beets must not run"))
    assert restarted.config["LIBRARY_ROOT_ERROR"]
    preview = restarted.test_client().post("/api/library/inventory/preview")
    assert preview.status_code == 400 and b"must not be a symlink" in preview.data
    executed = restarted.test_client().post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert executed.status_code == 409 and executed.json["code"] == "library_root_unsafe"
    assert not list(target.iterdir()) and source.exists()


@pytest.mark.parametrize("sidecar,embed,replace,existing,available", [
    (True, False, False, False, True),
    (False, True, False, False, True),
    (True, True, False, False, True),
    (True, True, False, True, True),
    (True, True, True, True, True),
    (True, True, True, True, False),
])
def test_reviewed_inbox_artwork_is_finalized_only_at_verified_destination(
    tmp_path, monkeypatch, sidecar, embed, replace, existing, available,
):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=sidecar, ART_EMBED=embed, ART_REPLACE=replace)
    source = tmp_path / "inbox" / "Artwork" / "01 Song.wav"
    tagged_inbox_wav(source)
    old_art, new_art = jpeg_bytes((120, 10, 10)), jpeg_bytes((10, 120, 10))
    source_cover = source.parent / "cover.jpg"
    final_cover = tmp_path / "library" / "Artist" / "Album" / "cover.jpg"
    if existing:
        source_cover.write_bytes(old_art)
        media = MediaFile(str(source)); media.images = [MediaImage(old_art, type=ImageType.front)]; media.save()
        final_cover.parent.mkdir(parents=True, exist_ok=True)
        final_cover.write_bytes(old_art)
    artwork = ({
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
        "thumbnail_url": "https://coverartarchive.org/release/123e4567-e89b-42d3-a456-426614174000/front-250",
    } if available else None)
    app.config["ARTWORK_FETCHER"] = lambda provider_data: new_art
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork=artwork)
    fetches = []
    app.config["ARTWORK_FETCHER"] = lambda provider_data: fetches.append(provider_data["provider_id"]) or new_art

    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")

    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 201
    destination = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    images = MediaFile(str(destination)).images or []
    assert fetches == (["release-1"] if available else [])
    if not available:
        assert response.json["artwork"]["status"] == "unavailable"
        assert final_cover.read_bytes() == old_art and images[0].data == old_art
    elif existing and not replace:
        assert final_cover.read_bytes() == old_art and images[0].data == old_art
        assert response.json["artwork"]["sidecars"]["preserved"]
        assert response.json["artwork"]["embedded"]["preserved"]
    else:
        assert (final_cover.exists() and final_cover.read_bytes() == new_art) is sidecar
        assert (bool(images) and images[0].data == new_art) is embed
    if source_cover.exists():
        assert source_cover.read_bytes() == old_art
    assert not (source.parent / ".cover.jpg").exists()
    if available:
        assert response.json["artwork"]["sha256"] == hashlib.sha256(new_art).hexdigest()
        assert response.json["artwork"]["album_folders"] == [str(destination.parent)]


@pytest.mark.parametrize("embed,replace,available,existing_embed,expected_sidecar,expected_embed", [
    (False, False, True, False, "incoming", None),
    (True, False, True, False, "incoming", "candidate"),
    (True, True, True, True, "candidate", "candidate"),
    (True, True, False, True, "incoming", "incoming"),
])
def test_reviewed_inbox_carries_incoming_sidecar_without_precreated_destination(
    tmp_path, monkeypatch, embed, replace, available, existing_embed, expected_sidecar, expected_embed,
):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=embed, ART_REPLACE=replace)
    source = tmp_path / "inbox" / "Incoming Artwork" / "01 Song.wav"
    tagged_inbox_wav(source)
    incoming, candidate = jpeg_bytes((91, 23, 47)), jpeg_bytes((14, 151, 72))
    source_cover = source.parent / "cover.jpg"
    source_cover.write_bytes(incoming)
    if existing_embed:
        media = MediaFile(str(source))
        media.images = [MediaImage(incoming, type=ImageType.front)]
        media.save()
    artwork = ({
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
    } if available else None)
    app.config["ARTWORK_FETCHER"] = lambda provider_data: candidate
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork=artwork)

    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")

    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 201
    destination = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    final_cover = destination.parent / "cover.jpg"
    assert final_cover.read_bytes() == (incoming if expected_sidecar == "incoming" else candidate)
    images = MediaFile(str(destination)).images or []
    if expected_embed is None:
        assert images == []
    else:
        assert images[0].data == (incoming if expected_embed == "incoming" else candidate)
    assert source_cover.read_bytes() == incoming
    assert response.json["artwork"]["source_sidecar_sha256"] == hashlib.sha256(incoming).hexdigest()
    assert response.json["artwork"]["sidecar_sha256"] == hashlib.sha256(final_cover.read_bytes()).hexdigest()


@pytest.mark.parametrize("unsafe", ["symlink", "oversized", "unreadable"])
def test_reviewed_inbox_rejects_unsafe_incoming_sidecar_before_audio_mutation(
    tmp_path, monkeypatch, unsafe,
):
    import beets_mvp
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=False)
    source = tmp_path / "inbox" / "Unsafe Artwork" / "01 Song.wav"
    tagged_inbox_wav(source)
    original_audio = source.read_bytes()
    cover = source.parent / "cover.jpg"
    if unsafe == "symlink":
        outside = tmp_path / "outside-cover.jpg"
        outside.write_bytes(jpeg_bytes((1, 2, 3)))
        cover.symlink_to(outside)
    elif unsafe == "oversized":
        with cover.open("wb") as stream:
            stream.truncate(beets_mvp.MAX_ARTWORK_BYTES + 1)
    else:
        cover.write_bytes(jpeg_bytes((1, 2, 3)))
        original_open = os.open
        monkeypatch.setattr(os, "open", lambda path, flags, *args, **kwargs: (
            (_ for _ in ()).throw(PermissionError("fixture unreadable"))
            if Path(path) == cover else original_open(path, flags, *args, **kwargs)
        ))
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork={
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
    })
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, pretend=False, **kwargs: (
        subprocess.CompletedProcess(["beet"], 0, "preflight", "")
        if pretend else pytest.fail("moving Beets import must not run")
    ))

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 409 and response.json["code"] == "source_artwork_unsafe"
    assert source.read_bytes() == original_audio and os.path.lexists(cover)
    assert not (tmp_path / "library" / "Artist" / "Album" / "01 Song.wav").exists()


def test_reviewed_inbox_beets_failure_restores_embedded_art_and_preserves_source_sidecar(
    tmp_path, monkeypatch,
):
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=True)
    source = tmp_path / "inbox" / "Artwork Failure" / "01 Song.wav"
    tagged_inbox_wav(source)
    old_art, new_art = jpeg_bytes((120, 10, 10)), jpeg_bytes((10, 120, 10))
    cover = source.parent / "cover.jpg"; cover.write_bytes(old_art)
    media = MediaFile(str(source)); media.images = [MediaImage(old_art, type=ImageType.front)]; media.save()
    artwork = {
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
    }
    app.config["ARTWORK_FETCHER"] = lambda provider_data: new_art
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork=artwork)
    monkeypatch.setattr("beets_mvp._run_beets_import", lambda *args, pretend=False, **kwargs:
        subprocess.CompletedProcess(["beet"], 0 if pretend else 9, "", "fixture failure"))

    response = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert response.status_code == 502 and response.json["code"] == "beets_import_failed"
    assert source.exists() and cover.read_bytes() == old_art
    assert MediaFile(str(source)).images[0].data == old_art
    assert not (tmp_path / "library" / "Artist" / "Album" / "cover.jpg").exists()


def test_reviewed_inbox_destination_sidecar_failure_is_audited_and_retryable(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=True)
    source = tmp_path / "inbox" / "Artwork Retry" / "01 Song.wav"
    tagged_inbox_wav(source)
    art = jpeg_bytes((10, 120, 10))
    app.config["ARTWORK_FETCHER"] = lambda provider_data: art
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork={
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
    })
    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")
    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    original = beets_mvp._apply_inbox_destination_sidecars
    calls = 0
    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            error = LibraryImportExecutionError("fixture sidecar commit failure", code="artwork_destination_failed", status=502)
            error.diagnostics = {"staged": ["cover.jpg"], "rolled_back": ["cover.jpg"], "rollback_errors": []}
            raise error
        return original(*args, **kwargs)
    monkeypatch.setattr("beets_mvp._apply_inbox_destination_sidecars", fail_once)

    failed = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert failed.status_code == 502 and failed.json["code"] == "artwork_finalize_failed"
    destination = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    assert destination.exists() and not source.exists() and not (destination.parent / "cover.jpg").exists()
    with sqlite3.connect(app.config["APP_DB"]) as db:
        state = db.execute("SELECT execution_status,last_execution_job_id FROM album_reviews WHERE id=?", (album["id"],)).fetchone()
        summary = json.loads(db.execute("SELECT summary_json FROM adoption_jobs WHERE id=?", (state[1],)).fetchone()[0])
    assert state[0] == "artwork-pending" and summary["status"] == "partial"
    assert summary["artwork"]["failure"]["rolled_back"] == ["cover.jpg"]
    assert summary["artwork"]["retryable"] is True

    # Ordinary page/API reconciliation must retain the destination-only
    # recovery manifest even though Beets already removed the source audio.
    refreshed = client.get("/api/inbox/reviews")
    assert refreshed.status_code == 200
    assert next(item for item in refreshed.json["albums"] if item["id"] == album["id"])["execution_status"] == "artwork-pending"
    scanned = client.post("/api/inbox/scan", json={"inbox_id": album["source_id"]})
    assert scanned.status_code == 200
    assert any(item["id"] == album["id"] for item in scanned.json["inboxes"][0]["albums"])
    assert client.post(f"/api/inbox/albums/{album['id']}/execute", json={"dry_run":True}).status_code == 200
    retried = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert retried.status_code == 201 and retried.json["retry_of_job_id"] == state[1]
    assert "failure" not in retried.json["artwork"]
    assert retried.json["artwork"]["recovered_from"]["rolled_back"] == ["cover.jpg"]
    assert (destination.parent / "cover.jpg").read_bytes() == art
    assert MediaFile(str(destination)).images[0].data == art


def test_incoming_sidecar_finalize_failure_retries_preserved_bytes(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    app.config.update(FETCH_ART=True, ART_SIDECAR=True, ART_EMBED=True, ART_REPLACE=False)
    source = tmp_path / "inbox" / "Incoming Retry" / "01 Song.wav"
    tagged_inbox_wav(source)
    incoming, candidate = jpeg_bytes((73, 24, 99)), jpeg_bytes((12, 177, 43))
    source_cover = source.parent / "cover.jpg"
    source_cover.write_bytes(incoming)
    app.config["ARTWORK_FETCHER"] = lambda provider_data: candidate
    client = app.test_client()
    album = approve_inbox_candidate(client, app, artwork={
        "available": True, "source": "cover-art-archive", "entity": "release",
        "mbid": "123e4567-e89b-42d3-a456-426614174000",
    })

    def run(current_app, selection, *, pretend=False):
        if not pretend:
            complete_mock_inbox_move(current_app, source)
        return subprocess.CompletedProcess(["beet"], 0, "fixture success", "")

    monkeypatch.setattr("beets_mvp._run_beets_import", run)
    original = beets_mvp._apply_inbox_destination_sidecars
    calls = 0
    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LibraryImportExecutionError("fixture finalization failure", code="artwork_destination_failed")
        return original(*args, **kwargs)
    monkeypatch.setattr("beets_mvp._apply_inbox_destination_sidecars", fail_once)

    failed = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert failed.status_code == 502 and failed.json["code"] == "artwork_finalize_failed"
    destination = tmp_path / "library" / "Artist" / "Album" / "01 Song.wav"
    assert destination.exists() and source_cover.read_bytes() == incoming
    assert MediaFile(str(destination)).images[0].data == candidate
    assert not (destination.parent / "cover.jpg").exists()
    retried = client.post(f"/api/inbox/albums/{album['id']}/execute", json={})
    assert retried.status_code == 201
    assert (destination.parent / "cover.jpg").read_bytes() == incoming
    assert retried.json["artwork"]["sidecar_sha256"] == hashlib.sha256(incoming).hexdigest()


def test_destination_sidecars_roll_back_first_folder_when_second_commit_fails(tmp_path, monkeypatch):
    import beets_mvp
    app = make_app(tmp_path)
    app.config.update(ART_SIDECAR=True, ART_REPLACE=True)
    destinations = ["Artist/First/01 Song.wav", "Artist/Second/01 Song.wav"]
    for relative in destinations:
        tagged_inbox_wav(tmp_path / "library" / relative)
    original_replace = Path.replace
    commits = 0
    def fail_second_cover(source, target):
        nonlocal commits
        if source.name.startswith(".cover.") and source.suffix == ".tmp" and Path(target).name == "cover.jpg":
            commits += 1
            if commits == 2:
                raise OSError("fixture second-folder failure")
        return original_replace(source, target)
    monkeypatch.setattr(Path, "replace", fail_second_cover)
    with pytest.raises(LibraryImportExecutionError) as failure:
        beets_mvp._apply_inbox_destination_sidecars(app, destinations, jpeg_bytes((1, 2, 3)))
    assert failure.value.code == "artwork_destination_failed"
    assert len(failure.value.diagnostics["staged"]) == 2
    assert all(not (tmp_path / "library" / "Artist" / folder / "cover.jpg").exists()
               for folder in ("First", "Second"))


def test_inbox_modal_contains_library_review_evidence_and_supported_duplicate_policy(tmp_path):
    client = make_app(tmp_path).test_client()
    inbox = client.get("/inbox").get_data(as_text=True)
    settings = client.get("/settings").get_data(as_text=True)
    assert 'src="/static/review-presentation.js"' in inbox
    assert 'src="/static/review-presentation.js"' in settings
    assert 'href="/static/review-presentation.css"' in inbox
    assert 'href="/static/review-presentation.css"' in settings
    for page in (inbox, settings):
        assert 'data-review-candidates' in page
        assert 'data-review-track-modal' in page
        assert 'src="/static/review-workflow.js"' in page
        assert "CratekeepReviewWorkflow.create" in page
    partial = Path("beets_mvp/templates/_review_workflow.html").read_text()
    controller = Path("beets_mvp/static/review-workflow.js").read_text()
    assert "presentation.renderCandidates" in controller
    assert "presentation.renderTrackRows" in controller
    assert "UUID = /^[0-9a-f]" in controller
    assert "function candidateOption" not in inbox
    assert '<option value="keep-both">Keep both' in inbox
    for unsupported in ('value="merge"', 'value="replace"', 'value="skip"'):
        assert unsupported not in inbox
    for shell_control in ("collapse-sidebar", "open-sidebar", "sidebar-scrim", "theme-toggle"):
        assert f'id="{shell_control}"' in inbox
    assert "GitHub" in inbox and "Build SHA" in inbox
    assert 'id="inbox-success"' in inbox and "success.focus()" in inbox
    assert "await workflow.load(1)" in inbox
    assert "details.length-localMissing" not in inbox
    shared_module = (Path(__file__).parents[1] / "beets_mvp/static/review-presentation.js").read_text()
    shared_css = (Path(__file__).parents[1] / "beets_mvp/static/review-presentation.css").read_text()
    for band in ("high", "medium", "low"):
        assert f'.confidence-band[data-band="{band}"]' in shared_css
    assert "const releaseYear" in shared_module
    assert "setBusy(true, 'Finding bounded MusicBrainz candidates" in controller
    assert "executing:'Running Beets no-write preflight" in inbox
    assert "executeButton.addEventListener('click'" in controller
    for shared_class in (
        "candidate-option", "candidate-confidence", "candidate-signals", "candidate-fact",
        "track-comparison-table", "track-change-button", "track-cell-changed", "track-explanation-row",
    ):
        assert shared_class in shared_module or shared_class in shared_css


def test_semantic_button_tokens_meet_normal_contrast_and_pagination_is_four_control(tmp_path):
    css = (Path(__file__).parents[1] / "beets_mvp/static/app-shell.css").read_text()
    light_block = css[css.index(":root {"):css.index("body.dark-mode")]
    dark_block = css[css.index("body.dark-mode"):css.index("button,.button-secondary")]
    def luminance(value):
        raw = value.lstrip("#")
        if len(raw) == 3: raw = "".join(character * 2 for character in raw)
        channels = [int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4)]
        channels = [channel / 12.92 if channel <= .04045 else ((channel + .055) / 1.055) ** 2.4 for channel in channels]
        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2]
    def contrast(first, second):
        light, dark = sorted((luminance(first), luminance(second)), reverse=True)
        return (light + .05) / (dark + .05)
    for mode, block in (("light", light_block), ("dark", dark_block)):
        colors = dict(re.findall(r"(--button-[\w-]+):(#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3}))", block))
        for prefix in ("primary", "secondary", "danger", "disabled"):
            ratio = contrast(colors[f"--button-{prefix}-text"], colors[f"--button-{prefix}-bg"])
            assert ratio >= 4.5, f"{mode} {prefix} contrast is only {ratio:.2f}:1"
    library = make_app(tmp_path).test_client().get("/library").get_data(as_text=True)
    for label, glyph in (("First", "«"), ("Previous", "‹"), ("Next", "›"), ("Last", "»")):
        assert f'aria-label="{label} page"' in library and glyph in library
    settings = make_app(tmp_path / "other").test_client().get("/settings").get_data(as_text=True)
    assert 'library-import-page-input' not in settings
    assert 'Page 1 of 1' in settings
