import json
import shutil
import sqlite3
import subprocess
import urllib.error
from pathlib import Path

import pytest
from beets.library import Item, Library

from beets_mvp import _format_bytes, _group_library_import_review_items, create_app
from beets_mvp.musicbrainz import ProviderError, search_releases
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

def test_health_and_empty_lists(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    assert client.get("/healthz").json == {"status": "ok"}
    assert client.get("/api/inbox").json == []
    assert client.get("/api/items").json == []
    assert app.config["APP_DB"] == str(tmp_path / "config" / "app.db")
    assert app.config["BEETS_DB"] == str(tmp_path / "config" / "library.db")
    assert app.config["BEETS_CONFIG"] == str(tmp_path / "config" / "config.yaml")

def test_index_renders_app_shell_navigation_and_sections(tmp_path):
    page = make_app(tmp_path).test_client().get("/")

    assert page.status_code == 200
    assert b'id="sidebar"' in page.data
    assert b'aria-label="Primary navigation"' in page.data
    assert b'href="#overview"' in page.data
    assert b'href="#inbox"' in page.data
    assert b'href="#library"' in page.data
    assert b'href="/settings"' in page.data
    assert b'id="overview-title"' in page.data
    assert b'id="inbox-title"' in page.data
    assert b'id="library-title"' in page.data
    assert b'Inbox is empty' in page.data
    assert b'Library is empty' in page.data
    assert b'src="/static/cratekeep-logo.png"' in page.data
    assert b'aria-controls="sidebar"' in page.data
    assert b'id="theme-toggle"' in page.data
    assert b'title="Build SHA"' in page.data
    assert b'https://github.com/kylejschultz/cratekeep' in page.data
    assert b'body.dark-mode' in page.data

def test_index_renders_inbox_data_and_library_edit_form(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        "beets_mvp._inbox_candidates",
        lambda current_app: [{"path": "new-album", "files": 2, "bytes": 1536 * 1024**2}],
    )
    item = {
        "id": 7,
        "title": "Example track",
        "artist": "Example artist",
        "album": "Example album",
        "albumartist": "Example artist",
        "genre": "Rock",
        "year": 2026,
        "track": 1,
        "disc": 1,
        "path": str(tmp_path / "Example track.mp3"),
        "bytes": 2 * 1024**2,
    }
    Path(item["path"]).write_bytes(b"x" * (2 * 1024**2))
    monkeypatch.setattr("beets_mvp._items", lambda current_app: [item])

    page = app.test_client().get("/")

    assert b"new-album" in page.data
    assert b"2 audio files waiting" in page.data
    assert b'<th class="numeric" scope="col">Size</th>' in page.data
    assert page.data.count(b"1.5 GB") == 2
    assert b"Example track" in page.data
    assert b'action="/api/items/7"' in page.data
    assert b'name="title" value="Example track"' in page.data
    assert b"Save changes" in page.data

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
    assert b'id="inbox_path"' in page.data
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
    assert seen["command"][-2:] == ["--move", str(album)]

def test_rejects_path_escape(tmp_path):
    client = make_app(tmp_path).test_client()
    response = client.post("/api/imports/preview", json={"path": "../outside"})
    assert response.status_code == 400

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
    assert b'never changes music files or runs beets import' in page.data


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
    assert b'id="library-import-modal"' in html
    assert b"Adoption" not in html and b"adoption" not in html
    assert b'name="inbox_path"' in general and b'name="navidrome_token"' in general
    assert b'name="fetch_art"' in metadata and b'name="beets_config"' in metadata
    assert b"event.key === 'ArrowRight'" in html
    assert b"window.addEventListener('hashchange'" in html


def test_album_review_modal_renders_compact_accessible_decision_layout(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    modal = html[html.index(b'id="library-import-modal"'):]

    assert b'role="dialog" aria-modal="true"' in modal
    assert b'aria-labelledby="library-import-modal-title"' in modal
    assert b'aria-describedby="library-import-modal-description"' in modal
    assert b'This decision does not change music files.' in modal
    assert b'class="match-summary"' in modal
    assert b'class="album-modal-section alternatives-section"' in modal
    assert b'class="comparison-grid"' in modal
    assert modal.count(b'<caption class="visually-hidden">') == 2
    assert b'<th scope="col">Current</th>' in modal
    assert b'Rematch with MusicBrainz release ID' in modal
    assert b'class="browser-actions review-footer"' in modal
    assert b'class="secondary danger-button" data-review-decision="rejected"' in modal
    assert b'class="approve-button" data-review-decision="approved">Queue import' in modal
    assert b"if (event.key === 'Escape')" in modal
    assert b"event.key !== 'Tab'" in modal
    assert b"reviewReturnFocus.focus()" in modal
    assert b'.comparison-grid { grid-template-columns:1fr;' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_library_inventory_score_bands_and_status_semantics(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    start = html.index("function albumInventoryStatus")
    end = html.index("function renderReviewItems", start)
    helpers = html[start:end]
    probe = """
const result = {
  scores: [0.7499, 0.75, 0.8999, 0.90].map(matchScorePresentation),
  statuses: [
    albumInventoryStatus({decision: 'approved'}, []),
    albumInventoryStatus(null, [{status: 'tracked'}]),
    albumInventoryStatus({decision: 'pending', candidate_status: 'complete', candidates: [{}]}, []),
    albumInventoryStatus({decision: 'pending', candidate_status: 'error', candidates: []}, []),
    albumInventoryStatus({decision: 'pending', candidate_status: 'not-run', candidates: []}, []),
    albumInventoryStatus({decision: 'rejected', candidate_status: 'complete', candidates: [{}]}, []),
    albumInventoryStatus({decision: 'skipped', candidate_status: 'complete', candidates: [{}]}, []),
    albumInventoryStatus({decision: 'approved'}, [{status: 'tracked'}]),
  ],
};
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval", helpers + probe],
        check=True, capture_output=True, text=True,
    )
    result = json.loads(completed.stdout)

    assert [(score["label"], score["band"]) for score in result["scores"]] == [
        ("74.9%", "low"), ("75%", "medium"), ("89.9%", "medium"), ("90%", "high"),
    ]
    assert result["scores"][3]["accessibleLabel"].endswith("initial auto-import indicator threshold")
    assert result["statuses"] == [
        "Queued", "Imported", "Matched", "Needs attention", "Needs attention",
        "Needs attention", "Needs attention", "Imported",
    ]


def test_library_inventory_pills_render_accessible_labels_and_contrast_safe_styles(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)

    assert "% best match" not in html
    assert "score.dataset.band = scorePresentation.band" in html
    assert "score.setAttribute('aria-label', scorePresentation.accessibleLabel)" in html
    assert "status.setAttribute('aria-label', `Status: ${statusValue}. ${statusDescription}`)" in html
    assert "${scorePresentation.accessibleLabel}" in html
    for status in ("Matched", "Queued", "Imported", "Needs attention"):
        assert f"{status}:" in html or f"'{status}':" in html

    palette = {
        "low": ("#fff", "#b42318"),
        "medium": ("#342100", "#fdb022"),
        "high": ("#fff", "#067647"),
    }
    for band, (foreground, background) in palette.items():
        assert f'.score-pill[data-band="{band}"]' in html
        assert f"color:{foreground}; background:{background};" in html
        assert _contrast_ratio(foreground, background) >= 4.5

    status_palettes = [
        ("#163d29", "#d9eee1"), ("#6d1620", "#f9dadd"),
        ("#e9fff0", "#294337"), ("#fff0f1", "#5a3034"),
    ]
    for foreground, background in status_palettes:
        assert f"color:{foreground}; background:{background};" in html
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


def test_library_import_review_groups_synthetic_artist_album_hierarchy(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    paths = [
        root / "Artist One" / "Album A" / "01 First.mp3",
        root / "Artist One" / "Album A" / "02 Second.mp3",
        root / "Artist One" / "Album B" / "01 Other.mp3",
        root / "Artist Two" / "Album C" / "01 Last.mp3",
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

    assert [(group["artist"], len(group["albums"])) for group in payload["groups"]] == [
        ("Artist One", 2), ("Artist Two", 1), ("Bulk Artist", 5),
    ]
    assert [song["title"] for song in payload["groups"][0]["albums"][0]["songs"]] == [
        "01 First", "02 Second",
    ]
    assert sum(len(album["songs"]) for album in payload["groups"][2]["albums"]) == 50
    assert _group_library_import_review_items(payload["items"]) == payload["groups"]


def test_library_import_review_paginates_25_artists_and_keeps_complete_albums(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / "library"
    large_album = root / "000 Large Artist" / "Complete Album"
    for number in range(125):
        path = large_album / f"{number + 1:03d} Track.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    for number in range(26):
        path = root / f"Artist {number:03d}" / "Only Album" / "01 Track.mp3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201

    first = client.get("/api/library-import/reviews?limit=25&offset=0")
    assert first.status_code == 200
    assert first.json["total_artists"] == 27
    assert first.json["artist_count"] == 25
    assert first.json["album_count"] == 25
    assert first.json["has_more"] is True
    assert first.json["next_offset"] == 25
    assert len(first.json["items"]) == 149
    complete = first.json["albums"][0]
    assert complete["album"] == "Complete Album"
    assert len(complete["tracks"]) == 125
    assert len(first.json["groups"][0]["albums"][0]["songs"]) == 125

    last = client.get("/api/library-import/reviews?limit=25&offset=25")
    assert last.status_code == 200
    assert last.json["artist_count"] == 2
    assert last.json["album_count"] == 2
    assert len(last.json["items"]) == 2
    assert last.json["has_more"] is False
    assert last.json["next_offset"] is None
    assert client.get("/api/library-import/reviews?offset=-1").status_code == 400
    assert client.get("/api/library-import/reviews?limit=51").status_code == 400


def test_musicbrainz_candidates_normalize_rank_and_persist_by_album(tmp_path):
    seen = []

    def provider(query, *, limit):
        seen.append((query, limit))
        return [
            {"provider_id": "weaker", "artist": "Other Artist", "album": "Album A", "track_count": 7,
             "tracks": [{"title": f"Other {number}", "position": number} for number in range(1, 8)],
             "retrieval": {"search_score": 100, "source": "musicbrainz-search"}},
            {"provider_id": "best", "artist": "Artist One", "album": "Album A", "track_count": 2, "year": 2020,
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
    assert response.json["selected_candidate_id"] == response.json["proposed_match"]["id"]
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
        "code": "release_not_found",
        "error": "No MusicBrainz release exists for that ID. Check that it is a release ID, not a release-group ID.",
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
    seen = {}

    class Response:
        def __init__(self, payload): self.payload = payload
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def read(self, size):
            seen["read_size"] = size
            return self.payload

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        if "/release/id-1" in request.full_url:
            return Response(b'{"id":"id-1","title":"Album","date":"2024-03-02","release-group":{"id":"group-1"},"artist-credit":[{"name":"Artist"}],"media":[{"position":1,"format":"CD","track-count":1,"tracks":[{"position":1,"number":"1","length":123000,"recording":{"id":"recording-1","title":"Song"}}]}]}')
        return Response(b'{"releases": [{"id": "id-1", "score": 99}]}')

    monkeypatch.setattr("beets_mvp.musicbrainz.urllib.request.urlopen", urlopen)
    result = search_releases({"artist": "Artist", "album": "Album"}, limit=100)
    assert result[0]["provider_id"] == "id-1"
    assert result[0]["release_group_id"] == "group-1"
    assert result[0]["tracks"][0] == {"medium_position": 1, "position": 1, "number": "1", "title": "Song", "recording_id": "recording-1", "length_ms": 123000}
    assert result[0]["retrieval"] == {"search_score": 99, "source": "musicbrainz-search"}
    assert "/release/id-1" in seen["url"]
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
    assert "order-mismatch" in statuses
    assert {"title-mismatch", "duration-mismatch", "order-mismatch", "extra"} <= issues
    assert match["track_count_mismatch"] is True
    assert match["confidence"] < .95
    assert match["recommendation"] in {"strong", "medium", "low", "none"}
    assert {penalty["field"] for penalty in match["penalties"]} == {"artist", "album", "date", "tracks"}


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
    assert response.json["selected_candidate_id"] == candidate["id"]
    assert candidate["provider_id"] == release_id
    assert candidate["artist"] == "Other Performer"
    assert candidate["retrieval"] == {"search_score": None, "source": "release-id"}
    assert candidate["confidence"] == .02
    assert candidate["recommendation"] == "none"


def test_library_import_review_markup_is_collapsible_and_not_a_flat_file_wall(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"document.createElement('details')" in html
    assert b"document.createElement('summary')" in html
    assert b"review-source" in html and b"review-album" in html
    assert b"source.open = sourceRows.length === 1" in html
    assert b"sourceFolderRows(albumGroup.songs)" in html
    assert b"reviewList.replaceChildren(...reviewItems.map" not in html
    assert b"reviews?limit=50" not in html


def test_library_import_review_rows_are_unfilled_but_keep_hierarchy_and_focus_contract(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b".review-list { overflow:hidden;" in html
    assert b".review-source-summary { display:grid; grid-template-columns:minmax(0,1fr) auto auto;" in html
    assert b".review-source-body { display:grid; grid-template-columns:minmax(0,1fr) auto;" in html
    assert b":is(a, button, input, select, summary):focus-visible { outline: 3px solid var(--focus);" in html
    assert b"const source = document.createElement('details');" in html
    assert b"const album = document.createElement('button');" in html


def test_library_import_review_modal_is_album_scoped_and_accessible(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b'id="library-import-modal-proposed"' in html
    assert b'id="library-import-modal-candidates"' in html
    assert b'id="library-import-modal-diff"' in html
    assert b'id="library-import-modal-tracks"' in html
    assert b'id="library-import-rematch-form"' in html
    assert b'aria-modal="true" aria-labelledby="library-import-modal-title" tabindex="-1"' in html
    assert b"if (event.key === 'Escape')" in html
    assert b"input:not(:disabled)" in html
    assert b"album.addEventListener('click', () => openReview(albumReview, album));" in html
    assert b"albumGroup.songs.forEach(item" not in html


def test_inventory_rows_render_source_paths_with_secondary_metadata(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"source.className = 'review-source'" in html
    assert b"path.className = 'review-source-path'" in html
    assert b"metadata.className = 'review-source-metadata'" in html
    assert b"metadata.textContent = `${group.artist} \xe2\x80\x94 ${albumGroup.album}" in html
    assert b"track.textContent = `${item.title || 'Untitled track'} \xe2\x80\x94 ${item.path}`" in html
    assert b"sourceFolderRows(albumGroup.songs)" in html
    assert b"path.textContent = sourceFolder(songs)" in html
    assert b"status.className = 'status-pill'" in html
    assert b"matchScorePresentation(albumReview?.highest_confidence)" in html
    assert b"score.dataset.band = scorePresentation.band" in html
    assert b"of ${reviewTotalArtists} artists" in html
    assert b"Previous artists" in html and b"Next artists" in html


def test_library_import_operations_expose_visible_live_progress(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert html.count(b'class="operation-status"') >= 2
    assert b'aria-live="polite"' in html
    assert b'.operation-status[data-busy="true"]::before' in html
    assert b"Scanning source folders, file names, and stats" in html
    assert b"Requesting bounded MusicBrainz matches" in html
    assert b"Rematching through the configured MusicBrainz provider" in html
    assert b"Queueing this album for import" in html
    assert b"setAttribute('aria-busy', String(busy))" in html


def test_album_candidate_payload_exposes_only_persisted_safe_artwork_and_best_score(tmp_path):
    app = make_app(tmp_path)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, *, limit: [
        {
            "provider_id": "safe", "artist": "Artist", "album": "Album", "track_count": 1,
            "tracks": [{"title": "Song", "position": 1}],
            "artwork_url": "https://images.example.test/cover.jpg",
        },
        {
            "provider_id": "unsafe", "artist": "Artist", "album": "Album", "track_count": 1,
            "tracks": [{"title": "Different", "position": 1}], "image_url": "javascript:alert(1)",
        },
    ]
    path = tmp_path / "library" / "Artist" / "Album" / "01 Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"synthetic")
    client = app.test_client()
    assert client.post("/api/library/inventory/preview").status_code == 201
    assert client.post("/api/library-import/candidates", json={}).status_code == 201

    album = client.get("/api/library-import/reviews").json["albums"][0]
    assert album["candidate_status"] == "complete"
    assert album["highest_confidence"] == max(candidate["confidence"] for candidate in album["candidates"])
    assert album["candidates"][0]["artwork_url"] == "https://images.example.test/cover.jpg"
    assert album["candidates"][1]["artwork_url"] is None

    html = client.get("/settings").data
    assert b"if (candidate.artwork_url)" in html
    assert b"artwork.src = candidate.artwork_url" in html
    assert b"innerHTML" not in html


def test_album_modal_actions_remain_workflow_only(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"const payload = {decision};" in html
    assert b"decision === 'approved' && activeCandidateId !== null" in html
    assert b"method: 'PATCH'" in html
    assert b"data-review-decision=\"skipped\"" in html
    assert b"data-review-decision=\"rejected\"" in html
    assert b"data-review-decision=\"approved\"" in html
    assert b"beet import" not in html
    assert b"/api/imports/" not in html


def test_inventory_preview_session_cache_is_scoped_to_rendered_build(tmp_path):
    app = make_app(tmp_path)
    app.config["BUILD_SHA"] = "build-one"
    first = app.test_client().get("/settings").data
    app.config["BUILD_SHA"] = "build-two"
    second = app.test_client().get("/settings").data

    assert b"const inventoryStorageKey = 'cratekeep-inventory-preview:' + \"build-one\";" in first
    assert b"const inventoryStorageKey = 'cratekeep-inventory-preview:' + \"build-two\";" in second
    assert b"sessionStorage.setItem(inventoryStorageKey, JSON.stringify({" in first
    assert b"preview: latestInventoryPreview" in first
    assert b"reviews: {items: reviewItems, groups: reviewGroups, albums: albumReviews, has_more: reviewHasMore," in first
    assert b"/api/library-import/reviews?limit=${reviewArtistLimit}&offset=${offset}" in first
    assert b'id="library-import-review-previous"' in first
    assert b'id="library-import-review-next"' in first
    assert b"sessionStorage.getItem(inventoryStorageKey)" in first
    assert b"renderInventoryPreview(latestInventoryPreview);" in first
    assert b"if (restoreInventoryPreview()) loadReviewItems();" in first
    assert b"latestInventoryPreview = data;" in first


def test_settings_persists_valid_beets_config_and_managed_values(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    new_library = tmp_path / "new-library"

    response = client.post("/settings", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(new_library),
        "fetch_art": "1",
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
    assert b'name="fetch_art" type="checkbox" value="1" checked' in restarted_page.data


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

    assert b"color-scheme:light" in page.data
    assert b'--font-sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI"' in page.data
    assert b"--page:#e7e9ec" in page.data
    assert b"font:16px/1.5 var(--font-sans)" in page.data
    assert b"width:13.5rem" in page.data
    assert b"border-right:1px solid var(--soft)" in page.data
    assert b"box-shadow:0 1px 2px" in page.data
    assert b"prefers-color-scheme:dark" not in page.data


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

    client.post("/setup", data={
        "inbox_path": str(tmp_path / "inbox"),
        "library_path": str(tmp_path / "library"),
    })
    assert client.get("/setup").headers["Location"].endswith("/settings")
