import hashlib
import json
from io import BytesIO
import shutil
import sqlite3
import subprocess
import urllib.error
import wave
from pathlib import Path

import pytest
from beets.library import Item, Library
from mediafile import Image as MediaImage, ImageType, MediaFile
from PIL import Image as PillowImage

from beets_mvp import (
    LibraryImportExecutionError, _candidate_diff, _fetch_and_normalize_artwork, _format_bytes,
    _format_genre, _genre_presentation_key, _group_library_import_review_items, _init_db,
    _item_genre, _SafeArtworkRedirect, create_app,
)
from beets_mvp.musicbrainz import ProviderError, _cover_art_evidence, _genre_evidence, search_releases
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


def test_settings_script_initializes_without_stale_direct_import_references(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]

    assert "library-import-queue" not in script
    assert "const inventoryButton = document.getElementById('inventory-preview');" in script
    assert "if (inventoryButton) inventoryButton.addEventListener('click'" in script
    assert "const themeButton = document.getElementById('theme-toggle');" in script
    assert "localStorage.getItem('cratekeep-theme') === 'dark'" in script
    assert "themeButton.addEventListener('click'" in script
    assert script.index("function resetImportPreview()") < script.index("function renderAlbumMatch()")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required for rendered JavaScript syntax validation")
def test_rendered_settings_javascript_has_valid_syntax(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    script = html[html.rindex("<script>") + len("<script>"):html.rindex("</script>")]
    completed = subprocess.run(
        [shutil.which("node"), "--check", "-"], input=script, text=True, capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_album_review_modal_renders_compact_accessible_decision_layout(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    modal = html[html.index(b'id="library-import-modal"'):]

    assert b'role="dialog" aria-modal="true"' in modal
    assert b'aria-labelledby="library-import-modal-title"' in modal
    assert b'aria-describedby="library-import-modal-description"' in modal
    assert b'This decision does not change music files.' in modal
    assert b'class="album-modal-section alternatives-section"' in modal
    assert b'>Choose album metadata<' in modal
    assert b'role="radiogroup" aria-label="Album metadata candidates"' in modal
    assert b"detail.className = 'candidate-option'" in modal
    assert b"const asIs = {...current, id:'as-is', is_as_is:true" in modal
    assert b"label:'Tracks changed'" in modal
    assert b"candidateReleaseYear(value)" in modal
    assert b"oldYear.className = 'metadata-old'" in modal
    assert b"newYear.className = 'metadata-new'" in modal
    assert b"changeButton.className = 'track-change-button'" in modal
    assert b"changeButton.textContent = `View track changes (${fact.changeCount})`" in modal
    assert b"changeButton.setAttribute('aria-label', `View track changes (${fact.changeCount}) for ${value.artist}" in modal
    assert b"changeButton.setAttribute('aria-haspopup', 'dialog')" in modal
    assert b"if (fact.changeCount === 0)" in modal
    assert b"dd.textContent = 'All tracks matched'" in modal
    assert b'.track-change-button { display:inline; width:auto; margin:0; padding:0; border:0; border-radius:0; appearance:none; background:transparent; color:var(--text); font:inherit; font-weight:700;' in html
    assert b'.track-change-button:focus-visible { outline:2px solid var(--modal-accent); outline-offset:3px; }' in html
    assert b"openTrackComparison(fact.tracks" in modal
    assert b'class="browser track-modal"' in modal
    assert b'aria-labelledby="track-comparison-title"' in modal
    assert b'aria-describedby="track-comparison-description"' in modal
    assert b'<th scope="col">Position</th>' in modal
    assert b'<th scope="col">Local title</th>' in modal
    assert b'<th scope="col">MusicBrainz title</th>' in modal
    assert b'<th scope="col">Local duration</th>' in modal
    assert b'<th scope="col">Proposed duration</th>' in modal
    assert b"row.className = 'track-row-unmatched'" in modal
    assert b"cell.classList.add('track-cell-changed')" in modal
    assert b"explanationRow.className = 'track-explanation-row'" in modal
    assert b"const explanations = track.explanations || []" in modal
    assert b"reviewDialog.inert = true" in modal
    assert b"reviewDialog.inert = false" in modal
    assert b"trackComparisonReturnFocus.focus()" in modal
    assert b"event.stopPropagation(); closeTrackComparison()" in modal
    assert b"label:'Release Type/media'" in modal
    assert b"label:'Release Region'" in modal
    assert b"label:'Local tracks found online'" in modal
    assert b"label:'Online tracks present locally'" in modal
    assert b'id="library-import-modal-tracks"' not in modal
    assert b'Rematch with MusicBrainz release ID' in modal
    assert b'class="browser-actions review-footer"' in modal
    assert b'data-review-decision="rejected"' not in modal
    assert b'id="library-import-in-place" class="approve-button">Import' in modal
    assert b'Skip for now' not in modal
    assert b'id="library-import-confirmation" hidden' in modal
    assert b'id="library-import-execution-apply" type="button">Confirm import' in modal
    assert b'Queue import' not in modal
    assert b"/preview" in modal
    assert b"if (event.key === 'Escape')" in modal
    assert b"event.key !== 'Tab'" in modal
    assert b"reviewReturnFocus.focus()" in modal
    assert b'.candidate-option[open]' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_candidate_track_evidence_is_text_only_when_all_tracks_match(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    start = html.index("function renderTrackChangeFact")
    end = html.index("function candidateFacts", start)
    helper = html[start:end]
    probe = """
const created = [];
const modalCalls = [];
globalThis.document = {
  createElement(tagName) {
    const element = {
      tagName,
      attributes: {},
      listeners: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, listener) { this.listeners[name] = listener; },
    };
    created.push(element);
    return element;
  },
};
globalThis.openTrackComparison = (...args) => modalCalls.push(args);
function destination() {
  return {textContent: '', children: [], append(child) { this.children.push(child); }};
}
const matched = destination();
renderTrackChangeFact(matched, {changeCount: 0, tracks: []}, {artist: 'Artist', album: 'Album'});
const changed = destination();
const tracks = [{local: 'Old', proposed: 'New'}];
renderTrackChangeFact(changed, {changeCount: 1, tracks}, {artist: 'Artist', album: 'Album'});
changed.children[0].listeners.click();
console.log(JSON.stringify({
  matched: {textContent: matched.textContent, childCount: matched.children.length},
  changed: {
    textContent: changed.children[0].textContent,
    className: changed.children[0].className,
    hasPopup: changed.children[0].attributes['aria-haspopup'],
    childCount: changed.children.length,
  },
  createdCount: created.length,
  modalCall: {tracks: modalCalls[0][0], title: modalCalls[0][1], returnControlMatches: modalCalls[0][2] === changed.children[0]},
}));
"""
    completed = subprocess.run(["node", "-e", f"{helper}\n{probe}"], check=True, capture_output=True, text=True)

    assert json.loads(completed.stdout) == {
        "matched": {"textContent": "All tracks matched", "childCount": 0},
        "changed": {
            "textContent": "View track changes (1)",
            "className": "track-change-button",
            "hasPopup": "dialog",
            "childCount": 1,
        },
        "createdCount": 1,
        "modalCall": {"tracks": [{"local": "Old", "proposed": "New"}], "title": "Artist — Album", "returnControlMatches": True},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_candidate_evidence_uses_release_year_and_identifies_changed_tracks(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)
    start = html.index("function metadataChanged")
    end = html.index("function candidateFacts", start)
    helpers = html[start:end]
    probe = """
const result = {
  years: [
    candidateReleaseYear({date: '2024-03-17'}),
    candidateReleaseYear({year: '1999', date: '1999-12-31'}),
    candidateReleaseYear({year: 'unknown', date: '2003-08-04'}),
    candidateReleaseYear({date: 'not provided'}),
  ],
  yearEvidence: [
    releaseYearEvidence({year:'2021'}, {date:'2022-03-04'}),
    releaseYearEvidence({date:'2022-01-01'}, {year:'2022'}),
    releaseYearEvidence({year:null}, {year:'2024'}),
  ],
  changes: changedTrackEvidence([
    {local:'Old title', proposed:'New title', current_position:[1,1], proposed_position:[1,2], local_duration:120, proposed_duration:130, status:'title-mismatch'},
    {local:'Part 1: Intro', proposed:'Part 1 — Intro', current_position:[1,2], proposed_position:[1,2], local_duration:90, proposed_duration:90, status:'title-mismatch'},
    {local:"Don't Stop", proposed:'Don’t Stop', current_position:[1,3], proposed_position:[1,3], local_duration:180, proposed_duration:180, status:'matched'},
    {local:'He Said "Go"', proposed:'He Said “Go”', current_position:[1,4], proposed_position:[1,4], local_duration:180, proposed_duration:180, status:'matched'},
    {local:"Don't Stop", proposed:'Doesn’t Stop', current_position:[1,5], proposed_position:[1,5], local_duration:180, proposed_duration:180, status:'title-mismatch'},
    {local:'Same', proposed:'Same', current_position:[1,3], proposed_position:[1,3], local_duration:180, proposed_duration:181, status:'matched'},
    {local:null, proposed:'Bonus', current_position:null, proposed_position:[2,1], local_duration:null, proposed_duration:90, status:'extra'},
  ]).map(track => ({local:track.local, proposed:track.proposed, localPosition:track.localPosition,
    proposedPosition:track.proposedPosition, durationDelta:track.durationDelta, explanations:track.explanations})),
  compact: trackEvidence([
    {local:'Same', proposed:'Same', current_position:[1,1], proposed_position:[1,1], local_duration:180, proposed_duration:null, status:'matched'},
  ])[0].explanations,
};
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(["node", "-e", f"{helpers}\n{probe}"], check=True, capture_output=True, text=True)

    assert json.loads(completed.stdout) == {
        "years": ["2024", "1999", "2003", ""],
        "yearEvidence": [
            {"localYear": "2021", "proposedYear": "2022", "changed": True, "value": "2021 → 2022"},
            {"localYear": "2022", "proposedYear": "2022", "changed": False, "value": "2022"},
            {"localYear": "", "proposedYear": "2024", "changed": True, "value": "Not provided → 2024"},
        ],
        "changes": [
            {"local": "Old title", "proposed": "New title", "localPosition": "1.01", "proposedPosition": "1.02", "durationDelta": 10,
             "explanations": ["Title differs: local “Old title” → MusicBrainz “New title”.",
                              "Position differs: local 1.01 → MusicBrainz 1.02.",
                              "Duration differs: local 2:00 → MusicBrainz 2:10 (10 seconds longer)."]},
            {"local": "Part 1: Intro", "proposed": "Part 1 — Intro", "localPosition": "1.02", "proposedPosition": "1.02", "durationDelta": 0,
             "explanations": ["Title punctuation or formatting differs: local “Part 1: Intro” → MusicBrainz “Part 1 — Intro”."]},
            {"local": "Don't Stop", "proposed": "Doesn’t Stop", "localPosition": "1.05", "proposedPosition": "1.05", "durationDelta": 0,
             "explanations": ["Title differs: local “Don't Stop” → MusicBrainz “Doesn’t Stop”."]},
            {"local": None, "proposed": "Bonus", "localPosition": "", "proposedPosition": "2.01", "durationDelta": None,
             "explanations": ["MusicBrainz track is not present locally.", "Local duration unavailable."]},
        ],
        "compact": [],
    }


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


def test_inbox_route_owns_library_import_review_workspace(tmp_path):
    page = make_app(tmp_path).test_client().get("/inbox")

    assert page.status_code == 200
    assert b"<title>Inbox \xc2\xb7 Cratekeep</title>" in page.data
    assert b"<h1>Inbox</h1>" in page.data
    assert b'href="/inbox" aria-current="page"' in page.data
    assert b'id="library-import-review-list"' in page.data
    assert b'id="library-import-modal"' in page.data
    assert b": \"library-import\");" in page.data


def test_library_route_exposes_summary_search_sort_and_existing_edits(tmp_path):
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
        library.add(item)

    client = app.test_client()
    page = client.get("/library")
    assert page.status_code == 200
    assert b'aria-current="page"' in page.data
    assert b"<span>Tracks</span><strong>3</strong>" in page.data
    assert b"<span>Albums</span><strong>2</strong>" in page.data
    assert b"<span>Artists</span><strong>2</strong>" in page.data
    assert b"Top artists" in page.data and b"Recently added" in page.data
    assert b'name="q" type="search"' in page.data
    assert b'name="sort"' in page.data and b'name="order"' in page.data
    assert b'action="/api/items/' in page.data

    filtered = client.get("/library?q=artist+b&sort=title&order=desc")
    browse = filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"Beta Song" in browse
    assert b"Alpha Song" not in browse
    assert b'option value="title" selected' in filtered.data
    assert b'option value="desc" selected' in filtered.data

    artist_filtered = client.get("/library?q=song&sort=title&order=desc&artist=Artist+A")
    artist_browse = artist_filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"Alpha Song" in artist_browse and b"Beta Song" not in artist_browse
    assert b'name="artist" value="Artist A"' in artist_filtered.data
    assert b'q=song&amp;sort=title&amp;order=desc&amp;artist=Artist+A' in artist_filtered.data

    album_filtered = client.get("/library?sort=album&order=asc&album=Shared")
    album_browse = album_filtered.data.split(b'id="browse-title"', 1)[1]
    assert b"Beta Song" in album_browse and b"Alpha Song" not in album_browse
    assert b'name="album" value="Shared"' in album_filtered.data


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
    assert result[0]["tracks"][0] == {"medium_position": 1, "position": 1, "number": "1", "title": "Song", "recording_id": "recording-1", "length_ms": 123000}
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


def test_library_import_review_markup_is_album_folder_first(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"document.createElement('details')" in html
    assert b"document.createElement('summary')" in html
    assert b"review-folder" in html and b"review-album" in html
    assert b"albumReviews.map(renderAlbum)" in html
    assert b"path.textContent = folderPath === '.' ? 'Library root' : folderPath" in html
    assert b"reviewList.replaceChildren(...reviewItems.map" not in html
    assert b"reviews?limit=50" not in html


def test_library_import_review_rows_are_unfilled_and_keep_focus_contract(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b".review-list { overflow:hidden;" in html
    assert b".review-folder-summary { display:grid; grid-template-columns:minmax(0,1fr) auto auto auto;" in html
    assert b".review-album-row { display:grid; grid-template-columns:auto minmax(0,1fr) auto auto auto auto auto;" in html
    assert b":is(a, button, input, select, summary):focus-visible { outline: 3px solid var(--focus);" in html
    assert b"document.createElement('article')" in html
    assert b"const album = document.createElement('button');" in html
    assert b"album.textContent =" in html
    assert b"album.title = `Review match for ${folderPath || 'library root'}`" in html


def test_library_import_review_modal_is_album_scoped_and_accessible(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b'id="library-import-modal-candidates"' in html
    assert b'id="library-import-modal-proposed"' not in html
    assert b'id="library-import-modal-tracks"' not in html
    assert b'role="radiogroup" aria-label="Album metadata candidates"' in html
    assert b"summary.setAttribute('role', 'radio')" in html
    assert b"label:'Release Type/media'" in html
    assert b"label:'Tracks changed'" in html
    assert b"Not applicable \xe2\x80\x94 As Is" in html
    assert b"Math.round(candidate.confidence" not in html
    assert b"candidates.querySelector('[aria-checked=\"true\"]')?.focus()" in html
    assert b'id="library-import-rematch-form"' in html
    assert b'aria-modal="true" aria-labelledby="library-import-modal-title" tabindex="-1"' in html
    assert b"if (event.key === 'Escape')" in html
    assert b"input:not(:disabled)" in html
    assert b"openReview(albumReview, album);" in html


def test_inventory_rows_render_compact_literal_paths_and_track_pills(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"path.className = 'review-folder-path'" in html
    assert b"path.textContent = folderPath === '.' ? 'Library root' : folderPath" in html
    assert b"path.title = folderPath === '.' ? 'Library root' : folderPath" in html
    assert b"review-source-metadata" not in html
    assert b"count.className = 'review-track-count'" in html
    assert b"count.textContent = `${songs.length} ${songs.length === 1 ? 'track' : 'tracks'}`" in html
    assert b"renderTrackList" not in html
    assert b"const sourcePaths = albumReview.source_paths.length ? albumReview.source_paths : ['.']" in html
    assert b"status.className = 'status-pill'" in html
    assert b"matchScorePresentation(albumReview?.highest_confidence)" in html
    assert b"score.dataset.band = scorePresentation.band" in html
    assert b"Albums ${pageStart}\xe2\x80\x93${pageEnd} of ${reviewTotalAlbums}" in html
    assert b"Previous albums" in html and b"Next albums" in html


def test_album_folder_line_carries_review_status_score_without_track_expansion(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"row.append(select, identity, count, duplicate, parts.status, parts.score, parts.album)" in html
    assert b"const albumCards = albumReviews.map(renderAlbum)" in html
    assert b"renderTrackList" not in html
    assert b"review-track-list" not in html
    assert b"event.stopPropagation(); openReview(albumReview, album)" in html
    assert b"identity.append(path)" in html


def test_album_review_multi_select_keeps_sequential_single_album_flow(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b'id="library-import-select-all" type="checkbox"' in html
    assert b'id="library-import-review-selected" class="secondary" type="button" disabled>Review selected (0)' in html
    assert b"select.setAttribute('aria-label', `Select ${folderPath === '.' ? 'library root' : folderPath} for sequential review`)" in html
    assert b"const selectedAlbumIds = new Set();" in html
    assert b"reviewSelectedButton.textContent = `Review selected (${selectedAlbumIds.size})`" in html
    assert b"reviewSequence = albumReviews.filter(album => selectedAlbumIds.has(album.id)).map(album => album.id)" in html
    assert b"if (nextAlbum && nextButton) openReview(nextAlbum, nextButton)" in html


def test_duplicate_warning_is_preflighted_and_gates_direct_import(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b'id="library-import-duplicate-warning"' in html
    assert b'id="library-import-duplicate-policy"' in html
    assert b"warning.hidden = preflight.status === 'clear'" in html
    assert b"policy.hidden = preflight.status !== 'possible'" in html
    assert b"library-import-queue" not in html
    assert b"if (preflight.status === 'ambiguous')" in html
    assert b"if (preflight.status === 'possible' && !duplicateAction)" in html
    assert b"/execute" in html


def test_album_modal_candidate_details_match_flask_field_contract(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data
    candidate_ui = html[html.index(b"const candidateIconPaths"):html.index(b"function openReview")]

    for label in (
        b"Artist", b"Album", b"Genre", b"Release Year", b"Tracks changed",
        b"Release Type/media", b"Release Region",
        b"Local tracks found online", b"Online tracks present locally",
    ):
        assert label in candidate_ui
    for omitted in (
        b"Duration delta", b"Release ID", b"Release group ID", b"Proposed position",
        b"Current duration", b"Label", b"Barcode", b"matching service",
    ):
        assert omitted not in candidate_ui
    assert b"candidateIconPaths" in candidate_ui
    assert b"candidate-signals" in candidate_ui
    assert b"candidate-fact" in candidate_ui
    assert b"document.createElement('details')" in candidate_ui
    assert b"document.createElement('summary')" in candidate_ui


def test_album_modal_candidate_rows_keep_numeric_score_selected_state_and_artwork(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"const scorePresentation = matchScorePresentation(value.confidence)" in html
    assert b"${selected ? 'Selected \xc2\xb7 ' : ''}${scorePresentation?.label" in html
    assert b"detail.open = selected" in html
    assert b"detail.setAttribute('aria-current', String(selected))" in html
    assert b"if (value.artwork_url)" in html
    assert b"artwork.src = value.artwork_url" in html


def test_library_import_operations_expose_visible_live_progress(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert html.count(b'class="operation-status"') >= 2
    assert b'aria-live="polite"' in html
    assert b'.operation-status[data-busy="true"]::before' in html
    assert b"Scanning source folders, file names, and stats" in html
    assert b"Requesting bounded MusicBrainz matches" in html
    assert b"Rematching through the configured MusicBrainz provider" in html
    assert b"Checking files and preparing an in-place preview" in html
    assert b"Registering files and applying approved metadata in place" in html
    assert b"setAttribute('aria-busy', String(busy))" in html


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
    assert b"if (value.artwork_url)" in html
    assert b"artwork.src = value.artwork_url" in html
    assert b"const asIs = {...current, id:'as-is', is_as_is:true, duplicate_preflight:activeAlbum?.duplicate_preflight};" in html
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
        assert db.execute("SELECT schema_version FROM metadata_candidates").fetchone()[0] == 3

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
    assert upgraded_default == "1"
    assert rows == [
        (1, "approved", 1, "stale", "not-run"),
        (2, "approved", 2, "complete", "complete"),
    ]
    assert versions == [(1,), (1,)]


def test_album_modal_actions_remain_workflow_only(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").data

    assert b"const payload = {decision};" in html
    assert b"const payload = {candidate_id: activeCandidateId === 'as-is' ? null : activeCandidateId}" in html
    assert b"if (duplicateAction) payload.duplicate_action = duplicateAction" in html
    assert b'id="library-import-duplicate-action"' in html
    for action in (b'merge', b'replace', b'keep-both', b'skip'):
        assert b'value="' + action + b'"' in html
    assert b"same MusicBrainz recording ID" in html
    assert b"method: 'PATCH'" in html
    assert b"/api/library-import/albums/${activeAlbum.id}/preview" in html
    assert b"/api/library-import/albums/${activeAlbum.id}/execute" in html
    assert b"data-review-decision=\"skipped\"" not in html
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

    assert b"const inventoryStorageKey = 'cratekeep-inventory-preview:' + \"build-one\";" in first
    assert b"const inventoryStorageKey = 'cratekeep-inventory-preview:' + \"build-two\";" in second
    assert b"sessionStorage.setItem(inventoryStorageKey, JSON.stringify({" in first
    assert b"preview: latestInventoryPreview" in first
    assert b"reviews: {items: reviewItems, groups: reviewGroups, albums: albumReviews, has_more: reviewHasMore," in first
    assert b"/api/library-import/reviews?limit=${reviewAlbumLimit}&offset=${offset}" in first
    assert b'id="library-import-review-previous"' in first
    assert b'id="library-import-review-next"' in first
    assert b"sessionStorage.getItem(inventoryStorageKey)" in first
    assert b"renderInventoryPreview(latestInventoryPreview);" in first
    assert b"if (restoreInventoryPreview()) loadReviewItems();" in first
    assert b"latestInventoryPreview = data;" in first


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is required to evaluate rendered Settings helpers")
def test_musicbrainz_candidates_require_successful_preview_in_current_page_session(tmp_path):
    html = make_app(tmp_path).test_client().get("/settings").get_data(as_text=True)

    assert 'id="candidate-generate" type="button" aria-describedby="candidate-generate-help" disabled' in html
    assert "Run inventory preview in this browser session before finding candidates." in html
    assert html.count("setCandidateAvailability(true);") == 1
    preview_success = html.index("setCandidateAvailability(true);")
    assert html.index("readApiResponse(response, 'Inventory preview failed'", html.index("/api/library/inventory/preview")) < preview_success
    assert preview_success < html.index("await loadReviewItems(0);")

    start = html.index("let inventoryPreviewReady = false;")
    end = html.index("function setOperationBusy", start)
    helper = html[start:end]
    probe = f"""
const attributes = {{}};
const candidateButton = {{disabled: null, setAttribute(name, value) {{ attributes[name] = value; }}}};
const candidateHelp = {{textContent: ''}};
{helper}
const before = {{disabled: candidateButton.disabled, ariaDisabled: attributes['aria-disabled'], help: candidateHelp.textContent}};
setCandidateAvailability(true);
const after = {{disabled: candidateButton.disabled, ariaDisabled: attributes['aria-disabled'], help: candidateHelp.textContent}};
console.log(JSON.stringify({{before, after}}));
"""
    completed = subprocess.run(
        [shutil.which("node"), "--input-type=module", "--eval", probe],
        check=True, capture_output=True, text=True,
    )

    assert json.loads(completed.stdout) == {
        "before": {
            "disabled": True,
            "ariaDisabled": "true",
            "help": "Run inventory preview in this browser session before finding candidates.",
        },
        "after": {
            "disabled": False,
            "ariaDisabled": "false",
            "help": "Inventory preview complete. Candidate lookup uses the current inventory.",
        },
    }


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
