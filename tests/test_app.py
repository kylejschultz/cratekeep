import sqlite3
from pathlib import Path

from beets.library import Item, Library

from beets_mvp import _format_bytes, _group_library_import_review_items, create_app

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
    payload = client.get("/api/library-import/reviews?limit=100").json

    assert [(group["artist"], len(group["albums"])) for group in payload["groups"]] == [
        ("Artist One", 2), ("Artist Two", 1), ("Bulk Artist", 5),
    ]
    assert [song["title"] for song in payload["groups"][0]["albums"][0]["songs"]] == [
        "01 First", "02 Second",
    ]
    assert sum(len(album["songs"]) for album in payload["groups"][2]["albums"]) == 50
    assert _group_library_import_review_items(payload["items"]) == payload["groups"]


def test_library_import_review_markup_is_collapsible_and_not_a_flat_file_wall(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"document.createElement('details')" in html
    assert b"document.createElement('summary')" in html
    assert b"review-artist" in html and b"review-album" in html
    assert b"groups.length === 1 && artistCount <= 12" in html
    assert b"reviewList.replaceChildren(...reviewItems.map" not in html
    assert b"reviews?limit=50" not in html


def test_library_import_review_rows_are_unfilled_but_keep_hierarchy_and_focus_contract(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b".review-artist,.review-album { border:1px solid var(--border); border-radius:.4rem; background:transparent; }" in html
    assert b".review-album { margin:.5rem .75rem .75rem; }" in html
    assert b".review-item { display:grid;" in html
    assert b"color:var(--text); border:1px solid var(--border); border-radius:.3rem; background:transparent; text-align:left;" in html
    assert b".review-item:hover { color:var(--text); border-color:var(--accent); background:transparent; }" in html
    assert b":is(a, button, input, select, summary):focus-visible { outline: 3px solid var(--focus);" in html
    assert b"const artist = document.createElement('details');" in html
    assert b"const album = document.createElement('details');" in html


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
    assert b"reviews: {items: reviewItems, groups: reviewGroups, has_more: reviewHasMore}" in first
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
