#!/usr/bin/env python3
"""Real-browser smoke test for Settings and direct in-place library import."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

from mediafile import Image as MediaImage, ImageType, MediaFile
from playwright.sync_api import Page, sync_playwright
from PIL import Image as PillowImage
from beets_mvp import _format_bytes

from runtime_app import ARTWORK_JPEG, EXISTING_ARTWORK_JPEG


TRACKS = {
    "case": Path("+44") / "When Your Heart Stops Beating" / "01 Lycanthrope.wav",
    "as_is": Path("As Is Artist") / "As Is Album" / "01 As Is Song.wav",
    "sidecar": Path("Sidecar Artist") / "Sidecar Album" / "01 Sidecar Song.wav",
    "embed": Path("Embed Artist") / "Embed Album" / "01 Embed Song.wav",
    "preserve": Path("Preserve Artist") / "Preserve Album" / "01 Preserve Song.wav",
    "replace": Path("Replace Artist") / "Replace Album" / "01 Replace Song.wav",
    "missing": Path("Missing Art Artist") / "Missing Art Album" / "01 Missing Art Song.wav",
    "feature_used": Path("2 Chainz") / "B.O.A.T.S. II #METIME" / "01 Used 2.wav",
    "feature_extra": Path("2 Chainz") / "B.O.A.T.S. II #METIME" / "08 Extra feat. Rich Homie Quan.wav",
    "feature_realest": Path("2 Chainz") / "B.O.A.T.S. II #METIME" / "09 U Da Realest.wav",
    "feature_ratchet": Path("2 Chainz") / "B.O.A.T.S. II #METIME" / "10 Mainstream Ratchet.wav",
    "feature_disabled": Path("Disabled Lead") / "Disabled Feature Album" / "01 Disabled Song.wav",
    "deluxe": Path("Big Sean") / "BULLY - DELUXE" / "01 Blessings.wav",
    "queue_safe": Path("Queue Safe") / "Safe Match" / "01 Safe.wav",
    "queue_tied": Path("Queue Tied") / "Tied Match" / "01 Tied.wav",
    "queue_lower": Path("Queue Lower") / "Lower Match" / "01 Lower.wav",
    "queue_error": Path("Queue Error") / "Provider Error" / "01 Error.wav",
    "queue_unselected": Path("Queue Unselected") / "Untouched" / "01 Untouched.wav",
    "queue_sequence_first": Path("Queue Sequential") / "Sequential First" / "01 First.wav",
    "queue_sequence_second": Path("Queue Sequential") / "Sequential Second" / "01 Second.wav",
}
GENRES = {"case": "Alternative Rock", "sidecar": "Rock", "embed": "Jazz", "preserve": "Blues", "replace": "Folk", "missing": "Ambient"}
CASE_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174099"
SEARCH_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174077"
EXISTING_SIDECAR_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174002"
DELUXE_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174010"
LIBRARY_MATCH_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174077"
LIBRARY_MISMATCH_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174011"
LIBRARY_NO_ART_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174012"
RELEASE_GROUP_ID = "123e4567-e89b-42d3-a456-426614174020"


def request_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def configure(base_url: str, inbox_paths: list[str], library_path: str) -> None:
    body = urllib.parse.urlencode([
        *(pair for index, path in enumerate(inbox_paths, 1)
          for pair in (("inbox_id", ""), ("inbox_name", f"Source {index}"), ("inbox_path", path))),
        ("library_path", library_path),
    ]).encode()
    opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
    request = urllib.request.Request(f"{base_url}/setup", data=body)
    with opener.open(request, timeout=10) as response:
        if response.url.rstrip("/") != base_url.rstrip("/"):
            raise AssertionError(f"setup did not redirect to the application: {response.url}")


def write_wav(path: Path, seconds: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * seconds)


def make_fixture(root: Path) -> dict[str, Path]:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    write_wav(root / "inbox" / "Rollback Retained" / "01 Inbox.wav")
    shared_folder = root / "inbox" / "Shared Ordinary"
    for album, title, track_number in (("Shared Album A", "First", 1), ("Shared Album B", "Second", 2)):
        track = shared_folder / f"{track_number:02d} {title}.wav"
        write_wav(track)
        tags = MediaFile(str(track)); tags.title = title; tags.artist = tags.albumartist = "Shared Artist"
        tags.album = album; tags.track = track_number; tags.save()
    (shared_folder / "notes.txt").write_bytes(b"shared ordinary unrelated bytes")
    (shared_folder / "cover.jpg").write_bytes(EXISTING_ARTWORK_JPEG)
    for folder, artist in (("inbox", "First Inbox"), ("inbox-two", "Second Inbox")):
        track = root / folder / "Shared Selection" / f"01 {artist}.wav"
        write_wav(track)
        tags = MediaFile(str(track))
        tags.title = artist
        tags.artist = tags.albumartist = artist
        tags.album = "Shared Selection"
        tags.save()
        (track.parent / "cover.jpg").write_bytes(EXISTING_ARTWORK_JPEG)
    for source_index, folder in enumerate(("inbox", "inbox-two")):
        for index in range(30):
            track = root / folder / f"Z Synthetic {index:02d}" / "01 Synthetic.wav"
            write_wav(track)
            tags = MediaFile(str(track)); tags.title = "Synthetic"
            tags.artist = tags.albumartist = f"Synthetic Source {source_index} Artist {index:02d}"
            tags.album = f"Synthetic Album {index:02d}"; tags.save()
    tracks = {name: root / "library" / relative for name, relative in TRACKS.items()}
    for name, track in tracks.items():
        write_wav(track, 288 if name == "feature_extra" else 1)
        tags = MediaFile(str(track))
        tags.title = re.sub(r"^\d+\s+", "", track.stem)
        tags.artist = track.parent.parent.name
        tags.albumartist = track.parent.parent.name
        tags.album = track.parent.name
        if name in GENRES:
            tags.genre = GENRES[name]
        if name == "case":
            tags.mb_albumid = CASE_RELEASE_ID
        if name in {"preserve", "replace", "missing"}:
            tags.images = [MediaImage(EXISTING_ARTWORK_JPEG, desc="Existing front cover", type=ImageType.front)]
        tags.save()
        if name in {"preserve", "replace", "missing"}:
            (track.parent / "cover.jpg").write_bytes(EXISTING_ARTWORK_JPEG)
        if name.startswith("feature_"):
            tags.track = int(track.stem.split(" ", 1)[0])
            tags.disc = 1
            if name == "feature_extra":
                tags.title = "Extra [Remix]"
                tags.artist = "2 Chainz feat. Rich Homie Quan"
                tags.albumartist = "2 Chainz"
            elif name == "feature_disabled":
                tags.artist = "Disabled Lead feat. Guest"
                tags.albumartist = "Disabled Lead"
            tags.save()
        if name == "deluxe":
            tags.title = "Blessings"
            tags.artist = "Big Sean feat. Drake"
            tags.albumartist = "Big Sean"
            tags.save()
    # Sixteen real top-level scopes plus these 110 empty scopes make exactly
    # 126 active rows, so removing one ready folder exercises a 6 -> 5 clamp.
    for number in range(110):
        (root / "library" / f"ZZ Pagination Folder {number:03d}").mkdir(parents=True)
    return tracks


def install_provider_fixtures(state_path: Path) -> dict[str, int]:
    """Persist deterministic MusicBrainz/CAA evidence after inventory."""
    genre_evidence = {
        "case": {"counts": [{"name": "alternative rock", "count": 4}], "selected": "alternative rock"},
        "sidecar": {"counts": [{"name": "electronic", "count": 4}, {"name": "rock", "count": 1}], "selected": "electronic"},
        "embed": {"counts": [{"name": "jazz", "count": 1}], "selected": None},
        "preserve": {"counts": [{"name": "blues", "count": 3}, {"name": "rock", "count": 3}], "selected": None},
        "replace": {"counts": [], "selected": None},
        "missing": {"counts": [], "selected": None},
    }
    candidate_ids = {}
    with sqlite3.connect(state_path / "app.db", timeout=10) as database:
        for index, name in enumerate(("case", "sidecar", "embed", "preserve", "replace", "missing"), 1):
            relative = TRACKS[name]
            artist, album_name = relative.parts[:2]
            album = database.execute(
                "SELECT id FROM album_reviews WHERE artist = ? AND album = ?", (artist, album_name)
            ).fetchone()
            assert album is not None, f"{name} fixture album was not inventoried"
            release_id = CASE_RELEASE_ID if name == "case" else f"123e4567-e89b-42d3-a456-4266141740{index:02d}"
            recording_id = f"223e4567-e89b-42d3-a456-4266141740{index:02d}"
            evidence = {"source": "musicbrainz-release-group-genres",
                        "rule": "top positive canonical genre has at least 2 votes and strictly exceeds runner-up",
                        **genre_evidence[name]}
            provider_data = {
                "provider_id": release_id,
                "artist": artist, "album": album_name,
                "genre": evidence["selected"], "genre_evidence": evidence,
                "date": "2024-03-02", "year": "2024", "release_type": "Album", "country": "GB",
                "track_count": 1, "media": [{"position": 1, "format": "CD", "track_count": 1}],
                "tracks": [{"title": relative.stem.removeprefix("01 "), "position": 1, "medium_position": 1,
                            "recording_id": recording_id}],
                "retrieval": {"search_score": None, "source": "deterministic-provider-fixture-mock"},
            }
            if name == "case":
                provider_data = {
                    key: provider_data[key]
                    for key in ("provider_id", "artist", "album", "genre", "genre_evidence", "retrieval")
                }
            else:
                provider_data["release_group_id"] = f"323e4567-e89b-42d3-a456-4266141740{index:02d}"
            if name not in {"case", "missing"}:
                provider_data["artwork"] = {
                    "available": True, "source": "cover-art-archive", "entity": "release", "mbid": release_id,
                    "thumbnail_url": f"https://coverartarchive.org/release/{release_id}/front-250",
                }
            proposed_diff = ({"genre": {"from": GENRES[name], "to": "Electronic"}} if name == "sidecar" else {})
            now = datetime.now(timezone.utc).isoformat()
            candidate_ids[name] = database.execute(
                """INSERT INTO metadata_candidates(
                       album_review_id, provider, provider_id, rank, confidence, artist, album,
                       year, proposed_diff_json, provider_data_json, created_at
                   ) VALUES (?, 'musicbrainz', ?, 1, 1.0, ?, ?, ?, ?, ?, ?)""",
                (album[0], release_id, artist, album_name, "2024", json.dumps(proposed_diff, sort_keys=True),
                 json.dumps(provider_data, sort_keys=True), now),
            ).lastrowid
            database.execute(
                "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
                (now, album[0]),
            )
        feature_album = database.execute(
            "SELECT id FROM album_reviews WHERE artist = ? AND album = ?",
            ("2 Chainz", "B.O.A.T.S. II #METIME"),
        ).fetchone()
        assert feature_album is not None, "featured-artist fixture album was not inventoried"
        feature_release_id = "123e4567-e89b-42d3-a456-426614174088"
        feature_data = {
            "provider_id": feature_release_id, "artist": "2 Chainz", "album": "B.O.A.T.S. II #METIME",
            "date": "2013-09-10", "year": "2013", "release_type": "Album", "country": "US",
            "track_count": 4, "media": [{"position": 1, "format": "CD", "track_count": 4}],
            "tracks": [
                {"title": "Used 2", "position": 1, "medium_position": 1, "length_ms": 1000},
                {"title": "Extra [Remix]", "position": 8, "medium_position": 1, "length_ms": 287000,
                 "track_artist": "2 Chainz feat. Rich Homie Quan", "artist_credit_source": "track"},
                {"title": "U Da Realest", "position": 9, "medium_position": 1, "length_ms": 1000},
                {"title": "Mainstream Ratchet", "position": 10, "medium_position": 1, "length_ms": 1000},
            ],
            "retrieval": {"search_score": None, "source": "deterministic-provider-fixture-mock"},
        }
        now = datetime.now(timezone.utc).isoformat()
        candidate_ids["feature"] = database.execute(
            """INSERT INTO metadata_candidates(
                   album_review_id, provider, provider_id, rank, confidence, artist, album,
                   year, proposed_diff_json, provider_data_json, created_at
               ) VALUES (?, 'musicbrainz', ?, 1, 1.0, ?, ?, ?, ?, ?, ?)""",
            (feature_album[0], feature_release_id, "2 Chainz", "B.O.A.T.S. II #METIME", "2013",
             json.dumps({"tracks": {"from": ["Used 2", "Extra [Remix]", "U Da Realest", "Mainstream Ratchet"],
                                            "to": ["Used 2", "Extra feat. Rich Homie Quan [Remix]", "U Da Realest", "Mainstream Ratchet"]}}, sort_keys=True),
             json.dumps(feature_data, sort_keys=True), now),
        ).lastrowid
        database.execute(
            "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
            (now, feature_album[0]),
        )
        disabled_album = database.execute(
            "SELECT id FROM album_reviews WHERE artist = ? AND album = ?",
            ("Disabled Lead", "Disabled Feature Album"),
        ).fetchone()
        assert disabled_album is not None, "disabled featured-artist fixture album was not inventoried"
        disabled_release_id = "123e4567-e89b-42d3-a456-426614174089"
        disabled_data = {
            "provider_id": disabled_release_id, "artist": "Disabled Lead", "album": "Disabled Feature Album",
            "track_count": 1, "media": [{"position": 1, "format": "CD", "track_count": 1}],
            "tracks": [{"title": "Disabled Song", "position": 1, "medium_position": 1,
                        "track_artist": "Disabled Lead feat. Guest", "artist_credit_source": "track"}],
            "retrieval": {"search_score": None, "source": "deterministic-provider-fixture-mock"},
        }
        candidate_ids["feature_disabled"] = database.execute(
            """INSERT INTO metadata_candidates(
                   album_review_id, provider, provider_id, rank, confidence, artist, album,
                   year, proposed_diff_json, provider_data_json, created_at
               ) VALUES (?, 'musicbrainz', ?, 1, 1.0, ?, ?, NULL, '{}', ?, ?)""",
            (disabled_album[0], disabled_release_id, "Disabled Lead", "Disabled Feature Album",
             json.dumps(disabled_data, sort_keys=True), now),
        ).lastrowid
        database.execute(
            "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
            (now, disabled_album[0]),
        )
    return candidate_ids


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_no_browser_errors(page_errors: list[str], console_errors: list[str], network: list[dict]) -> list[str]:
    failed_responses = [event for event in network if event.get("status", 0) >= 400 and not event.get("expected")]
    expected_statuses = {event["status"] for event in network if event.get("expected")}
    if expected_statuses:
        console_errors = [
            message for message in console_errors
            if not any(f"status of {status}" in message for status in expected_statuses)
        ]
    assert not page_errors, f"browser page errors: {page_errors}"
    assert not console_errors, f"browser console errors: {console_errors}"
    assert not failed_responses, f"failed API responses: {failed_responses}"
    return console_errors


def set_artwork_settings(page: Page, base_url: str, *, sidecar: bool, embed: bool, replace: bool) -> None:
    page.goto(f"{base_url}/settings#metadata", wait_until="networkidle")
    page.locator("#settings-tab-metadata").click()
    page.locator('input[name="fetch_art"]').set_checked(True)
    page.locator('input[name="art_sidecar"]').set_checked(sidecar)
    page.locator('input[name="art_embed"]').set_checked(embed)
    page.locator('input[name="art_replace"]').set_checked(replace)
    with page.expect_navigation(wait_until="networkidle"):
        page.get_by_role("button", name="Save settings").click()
    page.goto(f"{base_url}/settings#metadata", wait_until="networkidle")
    page.locator("#settings-tab-metadata").click()
    assert page.locator('input[name="fetch_art"]').is_checked()
    assert page.locator('input[name="art_sidecar"]').is_checked() is sidecar
    assert page.locator('input[name="art_embed"]').is_checked() is embed
    assert page.locator('input[name="art_replace"]').is_checked() is replace


def set_ftintitle_settings(
    page: Page, base_url: str, *, enabled: bool, evidence: Path | None = None,
) -> None:
    page.goto(f"{base_url}/settings#metadata", wait_until="networkidle")
    page.locator("#settings-tab-metadata").click()
    toggle = page.locator('input[name="ftintitle_enabled"]')
    toggle.set_checked(enabled)
    if enabled:
        page.locator('input[name="ftintitle_format"]').fill("feat. {0}")
        page.locator('input[name="ftintitle_keep_in_artist"]').set_checked(False)
        assert page.locator("#ftintitle-options").is_visible()
    else:
        assert page.locator("#ftintitle-options").is_hidden()
    if enabled and evidence:
        page.screenshot(path=evidence / "01-inbox-ftintitle-before-save.png", full_page=True)
    with page.expect_navigation(wait_until="networkidle") as navigation:
        page.get_by_role("button", name="Save settings").click()
    assert navigation.value is not None
    assert navigation.value.status != 405
    page.goto(f"{base_url}/settings#metadata", wait_until="networkidle")
    page.locator("#settings-tab-metadata").click()
    assert page.locator('input[name="ftintitle_enabled"]').is_checked() is enabled
    assert page.locator("#ftintitle-options").is_visible() is enabled


def run_browser(base_url: str, state_path: Path, evidence: Path, tracks: dict[str, Path]) -> dict:
    page_errors: list[str] = []
    console_errors: list[str] = []
    network: list[dict] = []
    folder_page_timings_ms: dict[str, float] = {}
    sequential_proof: list[dict] = []
    inline_scan_state_proof: dict[str, dict[str, str]] = {}
    ftintitle_config_proof: dict[str, object] = {}
    deluxe_proof: dict[str, object] = {}
    managed_library_proof: dict[str, object] = {}
    inbox_review_proof: dict[str, object] = {}
    inbox_page_metrics: dict[str, object] = {}
    viewport_checks: dict[str, object] = {}
    library_modal_contract: dict[str, object] = {}
    accidental = tracks["sidecar"].parent.parent / "Accidental Album" / "01 Wrong.wav"
    write_wav(accidental)
    accidental_tags = MediaFile(str(accidental))
    accidental_tags.title = "Wrong"
    accidental_tags.artist = accidental_tags.albumartist = "Wrong Artist"
    accidental_tags.album = "Accidental Album"
    accidental_tags.save()
    inbox_before = request_json(f"{base_url}/api/inbox")
    with sync_playwright() as playwright:
        executable = os.environ.get("CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(executable_path=executable) if executable else playwright.chromium.launch()
        browser_version = browser.version
        page: Page = browser.new_page(viewport={"width": 1440, "height": 1000})
        expected_failure = {"kind": None}
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on(
            "response",
            lambda response: network.append({"method": response.request.method, "status": response.status, "url": response.url,
                                              "expected": (
                                                  expected_failure["kind"] == "execute" and response.status == 502
                                              ) or (
                                                  expected_failure["kind"] == "inbox-stale" and response.status == 409
                                              ) or (
                                                  expected_failure["kind"] == "rematch" and response.status == 503
                                              ) or (
                                                  expected_failure["kind"] == "artwork" and response.status == 409
                                              )})
            if "/api/" in response.url
            else None,
        )
        page.route("https://coverartarchive.org/**", lambda route: route.fulfill(
            status=200, content_type="image/jpeg", body=ARTWORK_JPEG,
        ))

        def open_folder_album(folder_name: str):
            search = page.locator("#library-import-search")
            with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                      and f"q={folder_name}" in urllib.parse.unquote_plus(response.url)):
                search.fill(folder_name)
            row = page.locator(f'.review-folder[data-folder-path="{folder_name}"]')
            direct = row.locator(".folder-import")
            if direct.count():
                direct.wait_for()
                return direct
            expand = page.get_by_role("button", name=f"Expand {folder_name}")
            expand.wait_for()
            expand.click()
            page.locator(".review-folder-contents:not([hidden]) .review-album").first.wait_for()
            return page.locator(".review-folder-contents:not([hidden]) .review-album").first

        page.goto(f"{base_url}/settings", wait_until="networkidle")
        assert page.evaluate("sessionStorage.length") == 0
        assert not page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        page.screenshot(path=evidence / "01-settings-initial.png", full_page=True)

        set_ftintitle_settings(page, base_url, enabled=True, evidence=evidence)
        page.screenshot(path=evidence / "01-ftintitle-enabled-settings.png", full_page=True)
        config_text = (state_path / "config.yaml").read_text(encoding="utf-8")
        assert "- ftintitle" in config_text
        assert "format: feat. {0}" in config_text
        assert "auto: true" in config_text
        assert "drop: false" in config_text
        ftintitle_config_proof["enabled"] = {
            "plugin": "- ftintitle" in config_text,
            "format": "format: feat. {0}" in config_text,
            "auto": "auto: true" in config_text,
            "drop_false": "drop: false" in config_text,
        }

        page.locator("#theme-toggle").click()
        assert page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        assert page.evaluate("localStorage.getItem('cratekeep-theme')") == "dark"
        page.reload(wait_until="networkidle")
        assert page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        page.screenshot(path=evidence / "02-settings-dark-refresh.png", full_page=True)

        page.locator("#settings-tab-library-import").click()
        page.locator(".review-folder").first.wait_for()

        # A hard refresh followed by ordinary expansion reconciles an album
        # deleted after scan while preserving the unchanged album's decision.
        search = page.locator("#library-import-search")
        with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                  and "q=Sidecar" in urllib.parse.unquote_plus(response.url)):
            search.fill("Sidecar")
        page.get_by_role("checkbox", name="Select folder Sidecar Artist", exact=True).check()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")):
            page.locator("#library-import-scan-selected").click()
        scanned_sidecar = request_json(f"{base_url}/api/library-import/folders/albums?folder=Sidecar%20Artist")
        assert len(scanned_sidecar["albums"]) == 2
        kept_sidecar = next(album for album in scanned_sidecar["albums"] if album["album"] == "Sidecar Album")
        saved_candidate_id = kept_sidecar["selected_candidate_id"]
        page.evaluate("""async ({id, candidateId}) => {
          const response = await fetch(`/api/library-import/albums/${id}`, {
            method:'PATCH', headers:{'Content-Type':'application/json'},
            body:JSON.stringify({decision:'approved', candidate_id:candidateId}),
          });
          if (!response.ok) throw new Error(`save failed: ${response.status}`);
        }""", {"id": kept_sidecar["id"], "candidateId": saved_candidate_id})
        accidental.unlink(); accidental.parent.rmdir()
        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                  and "q=Sidecar" in urllib.parse.unquote_plus(response.url)):
            page.locator("#library-import-search").fill("Sidecar")
        with page.expect_response(lambda response: "/api/library-import/folders/albums?" in response.url) as reconciled_info:
            page.get_by_role("button", name="Expand Sidecar Artist").click()
        reconciled_sidecar = reconciled_info.value.json()
        assert len(reconciled_sidecar["albums"]) == 1
        assert reconciled_sidecar["albums"][0]["album"] == "Sidecar Album"
        assert reconciled_sidecar["albums"][0]["decision"] == "approved"
        assert reconciled_sidecar["albums"][0]["selected_candidate_id"] == saved_candidate_id
        assert reconciled_sidecar["reconciliation"]["removed_files"] == 1
        assert "1 albums · 1 tracks" in page.locator(".review-folder-count").inner_text()
        page.screenshot(path=evidence / "02-deleted-album-reconciled-dark.png", full_page=True)
        page.get_by_role("checkbox", name="Select folder Sidecar Artist", exact=True).uncheck()
        with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                  and "q=" in response.url):
            search.fill("")
        page.locator(".review-folder").first.wait_for()
        assert page.locator(".review-folder").count() == 25
        started = time.perf_counter()
        folder_page_timings_ms["page_1"] = round((time.perf_counter() - started) * 1000, 2)
        page_label = page.locator("#folder-page-total").inner_text()
        assert page_label.startswith("Page 1 of ")
        total_pages = int(page_label.rsplit(" ", 1)[1])
        assert total_pages >= 5
        first_page_name = page.locator(".review-folder-path").first.inner_text()
        page.locator(".review-folder .review-select").first.check()
        started = time.perf_counter()
        page.locator("#folder-page-next").click()
        page.wait_for_function("total => document.getElementById('folder-page-total').textContent === `Page 2 of ${total}`", arg=total_pages)
        folder_page_timings_ms["page_2"] = round((time.perf_counter() - started) * 1000, 2)
        assert page.locator(".review-folder").count() >= 2
        second_page_name = page.locator(".review-folder-path").first.inner_text()
        page.locator(".review-folder .review-select").first.check()
        page.get_by_text("2 folders selected", exact=False).wait_for()
        started = time.perf_counter()
        page.locator("#library-import-review-first").click()
        page.get_by_text(first_page_name, exact=True).wait_for()
        folder_page_timings_ms["direct_jump_page_1"] = round((time.perf_counter() - started) * 1000, 2)
        assert page.locator(".review-folder .review-select").first.is_checked()
        page.screenshot(path=evidence / "pagination-1-desktop.png", full_page=True)
        def pagination_centers() -> list[float]:
            boxes = [pagination.get_by_role("button").nth(index).bounding_box() for index in range(4)]
            label_box = pagination.locator(".page-label").bounding_box()
            assert label_box and all(boxes)
            label_center = label_box["y"] + label_box["height"] / 2
            return [abs((box["y"] + box["height"] / 2) - label_center) for box in boxes]
        pagination = page.locator(".review-pagination")
        assert max(pagination_centers()) <= 1
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=evidence / "pagination-narrow.png", full_page=True)
        assert pagination.get_by_role("button").count() == 4
        assert [pagination.get_by_role("button").nth(index).inner_text() for index in range(4)] == ["«", "‹", "›", "»"]
        assert pagination.evaluate("element => element.scrollWidth <= element.clientWidth")
        assert max(pagination_centers()) <= 1
        viewport_checks["pagination_label_centered_desktop_and_narrow"] = True
        assert page.locator("html").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), "narrow folder queue must not create page-level horizontal overflow"
        assert page.locator("#library-import-review-list").evaluate("element => element.scrollWidth <= element.clientWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        assert second_page_name

        # Rapid navigation must leave the newest requested page rendered even if
        # the earlier request is aborted or completes after it.
        page.evaluate("() => { libraryFolders.load(2); libraryFolders.load(5); }")
        page.wait_for_function("total => document.getElementById('folder-page-total').textContent === `Page 5 of ${total}`", arg=total_pages)
        page.wait_for_timeout(250)
        assert page.locator("#folder-page-total").inner_text() == f"Page 5 of {total_pages}"

        # Select folders across filtered views and exercise scoped scan, safe
        # bulk import confirmation, and exception sequencing.
        page.evaluate("sessionStorage.removeItem(Object.keys(sessionStorage).find(key => key.startsWith('cratekeep-folder-selection:')))" )
        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        queue_before = {name: (sha256(tracks[name]), str(tracks[name].resolve()), tracks[name].stat().st_ino)
                        for name in ("queue_safe", "queue_tied", "queue_lower", "queue_error", "queue_unselected")}
        for folder_name in ("Queue Safe", "Queue Sequential", "Queue Error"):
            page.get_by_role("checkbox", name=f"Select folder {folder_name}", exact=True).check()
        page.get_by_text("3 folders selected", exact=False).wait_for()
        review_status_before_scan = page.locator("#library-import-review-status").inner_text()
        safe_status = page.locator('.review-folder[data-folder-path="Queue Safe"] .review-folder-summary>.status-pill')
        sequential_status = page.locator('.review-folder[data-folder-path="Queue Sequential"] .review-folder-summary>.status-pill')
        unselected_status = page.locator('.review-folder[data-folder-path="Queue Unselected"] .review-folder-summary>.status-pill')
        unselected_status_before = unselected_status.inner_text()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")) as scan_info:
            # Dispatch synchronously so the intentionally brief queued render is
            # observed before the handler's two-animation-frame transition.
            queued = page.evaluate("""() => {
              const status = path => document.querySelector(
                `.review-folder[data-folder-path="${path}"] .status-pill`
              ).innerText.trim();
              document.getElementById('library-import-scan-selected').click();
              return {
                safe: status('Queue Safe'),
                sequential: status('Queue Sequential'),
                unselected: status('Queue Unselected'),
              };
            }""")
            expected_queued = {"safe": "Queued", "sequential": "Queued", "unselected": unselected_status_before}
            assert queued == expected_queued, f"queued status mismatch: expected {expected_queued}, got {queued}"
            inline_scan_state_proof["queued"] = queued
            page.wait_for_function("""() => {
              const selected = ['Queue Safe', 'Queue Sequential'];
              return selected.every(path => document.querySelector(`.review-folder[data-folder-path="${path}"] .status-pill`)?.textContent === 'Scanning…');
            }""")
            assert safe_status.inner_text() == sequential_status.inner_text() == "Scanning…"
            assert unselected_status.inner_text() == unselected_status_before
            inline_scan_state_proof["scanning"] = {
                "safe": safe_status.inner_text(),
                "sequential": sequential_status.inner_text(),
                "unselected": unselected_status.inner_text(),
            }
            assert page.locator("#library-import-review-status").inner_text() == review_status_before_scan
            assert page.locator("#library-import-scan-selected").get_attribute("aria-busy") == "true"
            page.screenshot(path=evidence / "03-folder-inline-scanning.png", full_page=True)
        assert scan_info.value.status == 207
        page.wait_for_function("""() =>
          document.querySelector('.review-folder[data-folder-path="Queue Safe"] .status-pill')?.textContent === 'ready'
          && document.querySelector('.review-folder[data-folder-path="Queue Sequential"] .status-pill')?.textContent === 'Tagged'
          && document.querySelector('.review-folder[data-folder-path="Queue Error"] .status-pill')?.textContent === 'error'
        """)
        assert page.locator('.review-folder[data-folder-path="Queue Error"] .status-pill').get_attribute("title")
        assert "deterministic folder-queue provider error" in page.locator(
            '.review-folder[data-folder-path="Queue Error"] .status-pill'
        ).get_attribute("aria-label")
        assert page.locator('.folder-error-reason').count() == 0
        assert page.locator(
            '.review-folder[data-folder-path="Queue Sequential"] .status-pill'
        ).get_attribute("title") is None
        safe_row = page.locator('.review-folder[data-folder-path="Queue Safe"]')
        sequential_row = page.locator('.review-folder[data-folder-path="Queue Sequential"]')
        assert safe_row.get_by_role("button", name="Import Queue Safe", exact=True).count() == 1
        assert safe_row.locator('.folder-expand').count() == 1
        assert sequential_row.locator(".folder-import").count() == 0
        assert sequential_row.get_by_role("button", name="Expand Queue Sequential", exact=True).count() == 1
        with page.expect_response(lambda response: "/api/library-import/folders/albums" in response.url):
            safe_row.get_by_role("button", name="Expand Queue Safe", exact=True).click()
        safe_album = safe_row.locator(".review-album-row")
        assert safe_album.get_by_label("Match confidence 100%", exact=True).inner_text() == "100%"
        assert safe_album.locator('[data-status="ready"]').inner_text().lower() == "ready"
        assert safe_row.locator(".review-file-list").is_hidden()
        safe_album.get_by_role("button", name=re.compile("Expand files for Safe Match")).click()
        assert safe_row.locator(".review-file-name").inner_text() == "01 Safe.wav"
        page.set_viewport_size({"width": 390, "height": 844})
        tree_overflow = page.locator("html").evaluate("""element => ({
          clientWidth:element.clientWidth,
          scrollWidth:element.scrollWidth,
          offenders:[...document.querySelectorAll('*')].filter(node => {
            const box=node.getBoundingClientRect();
            return box.right > element.clientWidth + 1 || node.scrollWidth > node.clientWidth + 1;
          }).slice(0,12).map(node => ({
            tag:node.tagName, className:node.className, id:node.id,
            clientWidth:node.clientWidth, scrollWidth:node.scrollWidth,
            right:Math.round(node.getBoundingClientRect().right),
          })),
        })""")
        assert tree_overflow["scrollWidth"] <= tree_overflow["clientWidth"], tree_overflow
        page.screenshot(path=evidence / "03-library-row-tree-narrow.png", full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        safe_row.get_by_role("button", name="Expand Queue Safe", exact=True).click()
        safe_row.get_by_role("button", name="Import Queue Safe", exact=True).click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        assert "Import" in page.locator("[data-review-execute]").inner_text()
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="hidden")
        assert page.locator("#library-import-review-status").inner_text() == review_status_before_scan
        inline_scan_state_proof["settled"] = {
            "safe": safe_status.inner_text(),
            "sequential": sequential_status.inner_text(),
            "error": page.locator('.review-folder[data-folder-path="Queue Error"] .status-pill').inner_text(),
            "unselected": unselected_status.inner_text(),
        }
        page.screenshot(path=evidence / "03-folder-inline-settled.png", full_page=True)
        page.locator('.review-folder[data-folder-path="Queue Safe"] .review-folder-summary').screenshot(
            path=evidence / "_row-library.png"
        )
        assert all((sha256(tracks[name]), str(tracks[name].resolve()), tracks[name].stat().st_ino) == before
                   for name, before in queue_before.items()), "folder scan changed media bytes, path, or inode"
        assert page.locator("#library-import-ready").inner_text() == "Import ready (1)"
        assert page.locator("#library-import-review-exceptions").inner_text() == "Review exceptions (3)"
        page.get_by_role("checkbox", name="Select folder Queue Error", exact=True).uncheck()
        page.get_by_text("2 folders selected", exact=False).wait_for()
        page.get_by_text("Review exceptions (2)", exact=True).wait_for()
        assert page.locator("#library-import-review-exceptions").inner_text() == "Review exceptions (2)"
        page.locator("#library-import-review-last").click()
        page.wait_for_function("total => document.getElementById('folder-page-total').textContent === `Page ${total} of ${total}`", arg=total_pages)
        page.locator("#library-import-ready").click()
        page.locator("#library-import-confirm-modal:not([hidden])").wait_for()
        assert "2 folders · 1 ready albums · 1 files/tracks · 2 excluded exceptions" in page.locator("#library-import-confirm-summary").inner_text()
        page.locator("#library-import-confirm-cancel").click()
        assert page.locator("#library-import-ready").evaluate("element => document.activeElement === element")
        page.locator("#library-import-ready").click()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/execute")) as bulk_info:
            page.locator("#library-import-confirm").click()
        assert bulk_info.value.status == 201
        assert len(bulk_info.value.json()["imported"]) == 1
        page.get_by_text("1 albums imported", exact=False).wait_for()
        page.wait_for_function("() => document.getElementById('folder-page-total').textContent.startsWith('Page 5 of ')")
        active_after_bulk = request_json(f"{base_url}/api/library-import/folders")
        active_by_path = {folder["path"]: folder for folder in active_after_bulk["folders"]}
        assert "Queue Safe" not in active_by_path
        assert active_by_path["Queue Sequential"]["album_count"] == 2
        assert active_by_path["Queue Sequential"]["track_count"] == 2
        page.screenshot(path=evidence / "03-folder-bulk-import.png", full_page=True)
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/exceptions")) as first_album_info:
            page.locator("#library-import-review-exceptions").click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        first_payload = first_album_info.value.json()["albums"][0]
        first_exception = page.locator("[data-review-title]").inner_text()
        page.get_by_role("button", name="Skip", exact=True).click()
        page.wait_for_function("title => document.querySelector('[data-review-title]').textContent !== title", arg=first_exception)
        second_exception = page.locator("[data-review-title]").inner_text()
        assert second_exception != first_exception
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="hidden")

        # Reopening starts a fresh run. A failed first import stays on the same
        # album; retry success advances using the already-loaded second payload.
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/exceptions")) as reopened_info:
            page.locator("#library-import-review-exceptions").click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        reopened_albums = reopened_info.value.json()["albums"]
        reopened_payload, next_payload = reopened_albums
        assert reopened_payload["id"] == first_payload["id"]
        first_title = page.locator("[data-review-title]").inner_text()
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
            page.locator("[data-review-candidates] [role='radio']").first.click()
        page.wait_for_function("document.querySelector('[data-review-execute]').disabled === false")
        execute_pattern = re.compile(r".*/api/library-import/albums/\d+/execute$")
        expected_failure["kind"] = "execute"
        page.route(execute_pattern, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body=json.dumps({"error": "Deterministic sequential import failure", "code": "fixture_failure"}),
        ), times=1)
        with page.expect_response(lambda response: response.url.endswith("/execute")) as failed_sequence:
            page.locator("[data-review-execute]").click()
        assert failed_sequence.value.status == 502
        expected_failure["kind"] = None
        assert page.locator("[data-review-title]").inner_text() == first_title
        assert page.locator("[data-review-execute]").inner_text() == "Retry import"

        page.evaluate("""() => {
          const nativeFetch = window.fetch.bind(window);
          window.fetch = async (...args) => {
            if (String(args[0]).endsWith('/execute')) await new Promise(resolve => setTimeout(resolve, 300));
            return nativeFetch(...args);
          };
        }""")
        with page.expect_response(lambda response: response.url.endswith("/execute")) as first_execute:
            page.locator("[data-review-execute]").click()
            assert page.locator("[data-review-execute]").is_disabled()
            assert page.locator("[data-review-execute]").get_attribute("aria-busy") == "true"
            assert page.locator("[data-review-execute] .button-spinner").is_visible()
            page.locator("[data-review-execute]").click(force=True)
        first_body = first_execute.value.request.post_data_json
        sequential_proof.append({"album_id": reopened_payload["id"], "candidate_id": first_body["candidate_id"],
                                 "owned_candidate_ids": [candidate["id"] for candidate in reopened_payload["candidates"]]})
        assert first_body["candidate_id"] in sequential_proof[-1]["owned_candidate_ids"]
        sequential_pending = request_json(
            f"{base_url}/api/library-import/folders/albums?folder=Queue%20Sequential"
        )
        assert len(sequential_pending["albums"]) == 1
        assert sequential_pending["albums"][0]["id"] == next_payload["id"]
        assert len(sequential_pending["albums"][0]["tracks"]) == 1
        page.wait_for_function("title => document.querySelector('[data-review-title]').textContent !== title", arg=first_title)
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
            page.locator("[data-review-candidates] [role='radio']").first.click()
        page.wait_for_function("document.querySelector('[data-review-execute]').disabled === false")
        with page.expect_response(lambda response: response.url.endswith("/execute")) as second_execute:
            page.locator("[data-review-execute]").click()
        second_body = second_execute.value.request.post_data_json
        sequential_proof.append({"album_id": next_payload["id"], "candidate_id": second_body["candidate_id"],
                                 "owned_candidate_ids": [candidate["id"] for candidate in next_payload["candidates"]]})
        assert second_body["candidate_id"] in sequential_proof[-1]["owned_candidate_ids"]
        assert sequential_proof[0]["album_id"] != sequential_proof[1]["album_id"]
        assert not any(
            folder["path"] == "Queue Sequential"
            for folder in request_json(f"{base_url}/api/library-import/folders")["folders"]
        )
        page.locator("[data-review-modal]").wait_for(state="hidden")
        assert "imported successfully" in page.locator("#library-import-success").inner_text()
        assert page.locator("#library-import-search").evaluate("element => document.activeElement === element")
        page.screenshot(path=evidence / "03-sequential-exceptions-complete.png", full_page=True)

        # Populate the existing detailed-review smoke fixtures through the
        # compatibility inventory endpoint without exposing obsolete controls.
        status = page.evaluate("async () => (await fetch('/api/library/inventory/preview', {method:'POST'})).status")
        assert status == 201
        candidate_ids = install_provider_fixtures(state_path)
        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        page.get_by_text("2 folders selected", exact=False).wait_for()
        page.screenshot(path=evidence / "03-selection-restored.png", full_page=True)

        # Exact-release edition evidence projects the confirmed local suffix
        # through review, dry-run, database, and file tags without changing the
        # raw MusicBrainz title or appending the unrelated "clean" note.
        deluxe_review = open_folder_album("Big Sean")
        deluxe_review.click(force=True)
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        page.locator("[data-review-open-mbid]").click()
        page.locator("[data-review-mbid-input]").fill(DELUXE_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as deluxe_rematch:
            page.locator("[data-review-mbid-submit]").click()
        assert deluxe_rematch.value.status == 200
        deluxe_payload = deluxe_rematch.value.json()
        deluxe_candidate = deluxe_payload["proposed_match"]
        assert deluxe_candidate["album"] == "BULLY - DELUXE"
        assert deluxe_candidate["release_disambiguation"] == "deluxe, clean"
        assert "album" not in deluxe_candidate["proposed_diff"]
        assert deluxe_candidate["recordings"][0]["title"] == "Blessings feat. Drake"
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
            page.get_by_role("radio", name=re.compile("Big Sean — BULLY - DELUXE")).click()
        facts = page.locator(".candidate-option[open] .candidate-fact")
        assert facts.filter(has_text="Album").locator("dd").first.inner_text() == "BULLY - DELUXE"
        assert facts.filter(has_text="MusicBrainz release note").locator("dd").inner_text() == "deluxe, clean"
        assert facts.filter(has_text="Album").first.get_attribute("data-changed") == "false"
        with sqlite3.connect(state_path / "app.db") as database:
            deluxe_raw = json.loads(database.execute(
                "SELECT provider_data_json FROM metadata_candidates WHERE id = ?", (deluxe_candidate["id"],),
            ).fetchone()[0])
        assert deluxe_raw["album"] == "BULLY"
        assert deluxe_raw["release_disambiguation"] == "deluxe, clean"
        page.evaluate("document.body.classList.remove('dark-mode')")
        page.screenshot(path=evidence / "04-deluxe-review-desktop-light.png", full_page=True)
        library_modal_contract = page.locator("[data-review-dialog]").evaluate("""dialog => ({
          controls:[...dialog.querySelectorAll('[data-review-find],[data-review-open-mbid],[data-review-cancel],[data-review-skip],[data-review-execute]')].map(node => node.textContent.trim()),
          currentPanels:dialog.querySelectorAll('[data-review-current]').length,
          approveControls:dialog.querySelectorAll('[data-review-approve]').length,
          candidateGroups:dialog.querySelectorAll('[data-review-candidates][role="radiogroup"]').length,
        })""")
        page.evaluate("document.body.classList.add('dark-mode')")
        page.screenshot(path=evidence / "04-deluxe-review-desktop-dark.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=evidence / "04-deluxe-review-narrow-dark.png", full_page=True)
        assert page.locator(".album-modal-dialog").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), "narrow deluxe review must not overflow"
        page.set_viewport_size({"width": 1440, "height": 1000})
        preview_response = page.evaluate("""async ({albumId, candidateId}) => {
          const response = await fetch(`/api/library-import/albums/${albumId}/preview`, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({candidate_id:candidateId}),
          });
          return {status: response.status, body: await response.json()};
        }""", {"albumId": deluxe_payload["id"], "candidateId": deluxe_candidate["id"]})
        assert preview_response["status"] == 200
        preview_changes = preview_response["body"]["items"][0]["changes"]
        assert "album" not in preview_changes
        assert preview_changes["title"] == {"from": "Blessings", "to": "Blessings feat. Drake"}
        assert preview_changes["artist"] == {"from": "Big Sean feat. Drake", "to": "Big Sean"}
        with page.expect_response(lambda response: response.url.endswith("/execute")) as deluxe_execute:
            page.locator("[data-review-execute]").click()
        assert deluxe_execute.value.status == 201
        deluxe_result = deluxe_execute.value.json()
        assert deluxe_result["items"][0]["changes"] == preview_changes
        deluxe_tags = MediaFile(str(tracks["deluxe"]))
        assert (deluxe_tags.album, deluxe_tags.title, deluxe_tags.artist) == (
            "BULLY - DELUXE", "Blessings feat. Drake", "Big Sean",
        )
        deluxe_proof = {
            "review_album": deluxe_candidate["album"],
            "release_note": deluxe_candidate["release_disambiguation"],
            "raw_provider_album": deluxe_raw["album"],
            "raw_provider_release_note": deluxe_raw["release_disambiguation"],
            "preview_changes": preview_changes,
            "execution_changes": deluxe_result["items"][0]["changes"],
            "file": {"album": deluxe_tags.album, "title": deluxe_tags.title, "artist": deluxe_tags.artist},
        }
        page.locator("[data-review-modal]").wait_for(state="hidden")
        assert "BULLY - DELUXE" in page.locator("#library-import-success").inner_text()

        candidate_review = open_folder_album("Sidecar Artist")
        candidate_review.click(force=True)
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        selected_candidate = page.get_by_role("radio", checked=True)
        assert "Sidecar Artist" in page.locator("[data-review-candidates]").inner_text()
        assert candidate_ids["sidecar"] > 0
        assert page.locator(".candidate-artwork").count() == 1

        # MBID lookup is a compact nested, metadata-only flow independent of Import.
        picker = page.locator(".candidate-picker")
        assert picker.count() == 1
        assert "top canonical MusicBrainz release-group genre" not in picker.inner_text()
        assert picker.evaluate("element => getComputedStyle(element).borderTopStyle") == "none"
        page.screenshot(path=evidence / "04-mbid-picker-desktop-dark.png")
        search_open = page.locator("[data-review-open-mbid]")
        search_open.click()
        mbid_modal = page.locator("[data-review-mbid-modal]")
        mbid_input = page.locator("[data-review-mbid-input]")
        mbid_modal.wait_for(state="visible")
        assert mbid_input.evaluate("element => document.activeElement === element")
        assert page.locator(".album-modal-dialog").evaluate("element => element.inert")
        page.screenshot(path=evidence / "04-mbid-search-modal-desktop-dark.png")

        # Transient typing and Cancel do not dirty the parent review.
        mbid_input.fill("transient text")
        mbid_modal.get_by_role("button", name="Cancel", exact=True).click()
        assert search_open.evaluate("element => document.activeElement === element")
        dialogs = []
        def record_dialog(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()
        page.on("dialog", record_dialog)
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="hidden")
        page.remove_listener("dialog", record_dialog)
        assert dialogs == [], "cancelled MBID typing must not trigger the parent dirty-state prompt"
        candidate_review.click(force=True)
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        search_open.click(); mbid_modal.wait_for(state="visible")
        page.keyboard.press("Escape")
        mbid_modal.wait_for(state="hidden")
        assert search_open.evaluate("element => document.activeElement === element")
        search_open.click(); mbid_modal.wait_for(state="visible")
        mbid_modal.click(position={"x": 2, "y": 2})
        mbid_modal.wait_for(state="hidden")

        # Focus wraps within the nested dialog, and active requests block every close path.
        search_open.click(); mbid_modal.wait_for(state="visible")
        page.keyboard.press("Shift+Tab")
        assert mbid_modal.get_by_role("button", name="Close MusicBrainz search").evaluate("element => document.activeElement === element")
        page.keyboard.press("Tab")
        assert mbid_input.evaluate("element => document.activeElement === element")
        page.evaluate("document.querySelector('[data-review-modal]').dataset.busy = 'true'")
        mbid_modal.get_by_role("button", name="Cancel", exact=True).click()
        page.keyboard.press("Escape")
        mbid_modal.click(position={"x": 2, "y": 2})
        assert mbid_modal.is_visible(), "busy MBID search must block Cancel, Escape, and backdrop close"
        page.evaluate("document.querySelector('[data-review-modal]').dataset.busy = 'false'")

        request_network_start = len(network)
        item_rows_before = request_json(f"{base_url}/api/items")
        file_before = {
            "sha256": sha256(tracks["sidecar"]),
            "stat": (tracks["sidecar"].stat().st_size, tracks["sidecar"].stat().st_mtime_ns,
                     tracks["sidecar"].stat().st_dev, tracks["sidecar"].stat().st_ino),
            "path": str(tracks["sidecar"].resolve()),
        }
        mbid_input.fill("not-a-uuid")
        page.locator("[data-review-mbid-submit]").click()
        assert "canonical MusicBrainz release or release-group UUID" in page.locator("[data-review-mbid-status]").inner_text()
        assert len(network) == request_network_start, "invalid UUID must remain local"

        rematch_pattern = re.compile(r".*/api/library-import/albums/\d+/rematch$")
        page.route(rematch_pattern, lambda route: route.fulfill(
            status=503, content_type="application/json",
            body=json.dumps({"error": "fixture unavailable", "code": "provider_unavailable", "retryable": True}),
        ), times=1)
        mbid_input.fill(SEARCH_RELEASE_ID)
        expected_failure["kind"] = "rematch"
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as failed_rematch:
            page.locator("[data-review-mbid-submit]").click()
        assert failed_rematch.value.status == 503
        expected_failure["kind"] = None
        assert mbid_modal.is_visible()
        assert mbid_input.is_enabled() and mbid_input.input_value() == SEARCH_RELEASE_ID
        assert "fixture unavailable" in page.locator("[data-review-mbid-status]").inner_text()

        candidate_options = page.locator("[data-review-candidates] [role='radio']")
        initial_option_count = candidate_options.count()
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as new_rematch:
            page.locator("[data-review-mbid-submit]").click()
        assert new_rematch.value.status == 200
        new_payload = new_rematch.value.json()
        mbid_modal.wait_for(state="hidden")
        assert page.locator("[data-review-modal]").is_visible()
        assert "candidate choices added" in page.locator("[data-review-status]").inner_text()
        assert candidate_options.count() == initial_option_count + 1
        assert page.get_by_role("radio", checked=True).count() == 0
        assert new_payload["selected_candidate_id"] is None
        assert sum(candidate["provider_id"] == SEARCH_RELEASE_ID for candidate in new_payload["candidates"]) == 1
        assert page.locator("[data-review-execute]").inner_text() == "Import to Library"

        # A release-group MBID presents each bounded edition as a choice and
        # never silently turns one edition into the review decision.
        search_open.click(); mbid_input.fill(RELEASE_GROUP_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as group_rematch:
            page.locator("[data-review-mbid-submit]").click()
        assert group_rematch.value.status == 200
        group_payload = group_rematch.value.json()
        group_candidates = [candidate for candidate in group_payload["candidates"]
                            if candidate["release_group_id"] == RELEASE_GROUP_ID]
        assert len(group_candidates) == 2
        assert {candidate["retrieval"]["source"] for candidate in group_candidates} == {"release-group-id"}
        assert group_payload["selected_candidate_id"] is None
        assert page.get_by_role("radio", checked=True).count() == 0
        assert candidate_options.count() == initial_option_count + 3

        # Looking up an existing release moves/selects its row without duplication.
        search_open.click(); mbid_input.fill(EXISTING_SIDECAR_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as existing_rematch:
            page.locator("[data-review-mbid-submit]").click()
        assert existing_rematch.value.status == 200
        existing_payload = existing_rematch.value.json()
        assert candidate_options.count() == initial_option_count + 3
        assert sum(candidate["provider_id"] == EXISTING_SIDECAR_RELEASE_ID for candidate in existing_payload["candidates"]) == 1
        assert existing_payload["candidates"][0]["provider_id"] == EXISTING_SIDECAR_RELEASE_ID
        assert existing_payload["selected_candidate_id"] is None
        assert page.get_by_role("radio", checked=True).count() == 0

        search_requests = network[request_network_start:]
        assert not any(event["url"].endswith(("/preview", "/execute")) for event in search_requests)
        assert sum(event["url"].endswith("/rematch") for event in search_requests) == 4
        file_after = {
            "sha256": sha256(tracks["sidecar"]),
            "stat": (tracks["sidecar"].stat().st_size, tracks["sidecar"].stat().st_mtime_ns,
                     tracks["sidecar"].stat().st_dev, tracks["sidecar"].stat().st_ino),
            "path": str(tracks["sidecar"].resolve()),
        }
        assert file_after == file_before
        assert request_json(f"{base_url}/api/items") == item_rows_before
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=evidence / "05-mbid-picker-narrow-dark.png")
        assert page.locator(".album-modal-dialog").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), "narrow album review must not overflow after MBID search"
        search_open.click(); mbid_modal.wait_for(state="visible")
        page.screenshot(path=evidence / "06-mbid-search-modal-narrow-dark.png")
        assert page.locator(".mbid-modal-dialog").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), "narrow MBID search must not overflow its dialog"
        mbid_modal.get_by_role("button", name="Cancel", exact=True).click()
        page.set_viewport_size({"width": 1440, "height": 1000})

        # A selected candidate is persisted immediately, so closing is clean.
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
            page.get_by_role("radio", name=re.compile("As Is")).click()
        page.locator(".album-modal-dialog").focus(); page.keyboard.press("Escape")
        page.locator("[data-review-modal]").wait_for(state="hidden")
        assert candidate_review.evaluate("element => document.activeElement === element")
        candidate_review.click(force=True)
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        page.evaluate("document.querySelector('[data-review-modal]').dataset.busy = 'true'")
        page.locator("[data-review-modal]").click(position={"x": 2, "y": 2})
        assert page.locator("[data-review-modal]").is_visible(), "busy review must not close"
        page.evaluate("document.querySelector('[data-review-modal]').dataset.busy = 'false'")

        # Close the clean modal before exercising persisted destination combinations.
        page.locator("[data-review-close]").click()

        def import_candidate(name: str, *, has_art: bool, screenshot: str, mock_failure: bool = False) -> dict:
            page.goto(f"{base_url}/settings#library-import", wait_until="networkidle")
            page.locator("#settings-tab-library-import").click()
            review = open_folder_album(TRACKS[name].parts[0])
            review.click(force=True)
            page.locator("[data-review-modal]:not([hidden])").wait_for()
            candidate = page.get_by_role("radio", name=re.compile(re.escape(TRACKS[name].parts[0] + " — "))).first
            if candidate.get_attribute("aria-checked") != "true":
                with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
                    candidate.click()
            page.wait_for_function("document.querySelector('[data-review-execute]').disabled === false")
            if has_art:
                assert candidate.locator("xpath=..").locator(".candidate-artwork").count() == 1
            else:
                missing_text = " ".join(page.locator(".candidate-artwork-missing").all_text_contents())
                assert "No candidate artwork available" in missing_text
            assert page.locator("#library-import-execution-apply").count() == 0
            assert page.locator("#library-import-preview").count() == 0
            assert page.locator(".review-footer .approve-button").count() == 1
            if name == "feature_extra":
                change_button = page.get_by_role("button", name=re.compile(r"View track changes \(1\)"))
                change_button.click()
                rows = page.locator("[data-review-track-rows] tr:not(.track-explanation-row)")
                assert rows.count() == 1
                cells = rows.locator("td")
                assert cells.count() == 5
                row_text = rows.inner_text()
                assert "Extra feat. Rich Homie Quan [Remix]" in row_text and "Extra [Remix]" in row_text
                assert "2 Chainz feat. Rich Homie Quan" in row_text
                assert cells.nth(1).locator(".track-metadata-title").inner_text() == "Extra [Remix]"
                assert cells.nth(1).locator(".track-metadata-artist").inner_text() == "Artist: 2 Chainz feat. Rich Homie Quan"
                assert cells.nth(2).locator(".track-metadata-title").inner_text() == "Extra feat. Rich Homie Quan [Remix]"
                assert cells.nth(2).locator(".track-metadata-artist").inner_text() == "Artist: 2 Chainz"
                assert "Title: Extra [Remix] (changed)" in cells.nth(1).get_attribute("aria-label")
                assert "Artist: 2 Chainz feat. Rich Homie Quan (changed)" in cells.nth(1).get_attribute("aria-label")
                assert "UNMATCHED" not in row_text
                assert all(title not in row_text for title in ("Used 2", "U Da Realest", "Mainstream Ratchet"))
                page.screenshot(path=evidence / screenshot)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=evidence / "04-feature-track-changes-narrow-dark.png")
                assert page.locator(".track-modal-dialog").evaluate(
                    "element => element.scrollWidth <= element.clientWidth"
                ), "narrow track changes must not overflow its dialog"
                assert page.locator(".track-table-wrap").evaluate(
                    "element => element.scrollWidth <= element.clientWidth"
                ), "compact narrow track table should not require horizontal scrolling"
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator("[data-review-track-modal]").get_by_role("button", name="Close", exact=True).click()
            else:
                page.screenshot(path=evidence / screenshot)
            request_network_start = len(network)
            if mock_failure:
                execute_pattern = re.compile(r".*/api/library-import/albums/\d+/execute$")
                expected_failure["kind"] = "execute"
                page.route(execute_pattern, lambda route: route.fulfill(
                    status=502, content_type="application/json",
                    body=json.dumps({"error": "Deterministic mocked artwork failure; retry is available.",
                                     "code": "artwork_fetch_failed"}),
                ), times=1)
                with page.expect_response(lambda response: response.url.endswith("/execute")) as failed_info:
                    page.locator("[data-review-execute]").click()
                assert failed_info.value.status == 502
                page.wait_for_function(
                    "document.querySelector('[data-review-status]').textContent.includes('retry is available')"
                )
                assert "retry is available" in page.locator("[data-review-status]").inner_text()
                assert page.locator("[data-review-execute]").inner_text() == "Retry import"
                assert page.locator("[data-review-modal]").is_visible()
                expected_failure["kind"] = None
            with page.expect_response(lambda response: response.url.endswith("/execute")) as execute_info:
                page.locator("[data-review-execute]").click()
            assert execute_info.value.status in {200, 201}
            payload = execute_info.value.json()
            import_requests = [event for event in network[request_network_start:]
                               if "/api/library-import/albums/" in event["url"]]
            assert not any(event["url"].endswith("/preview") for event in import_requests)
            assert sum(event["url"].endswith("/execute") for event in import_requests) == (2 if mock_failure else 1)
            page.locator("[data-review-modal]").wait_for(state="hidden")
            assert TRACKS[name].parts[0] in page.locator("#library-import-success").inner_text()
            return payload

        set_artwork_settings(page, base_url, sidecar=True, embed=False, replace=False)
        sidecar_result = import_candidate(
            "sidecar", has_art=True, screenshot="05-sidecar-direct-import-desktop-dark.png",
            mock_failure=True,
        )
        assert sidecar_result["artwork"]["status"] == "applied"

        set_artwork_settings(page, base_url, sidecar=False, embed=True, replace=False)
        embed_result = import_candidate("embed", has_art=True, screenshot="06-embed-direct-import.png")
        assert embed_result["artwork"]["status"] == "applied"

        set_artwork_settings(page, base_url, sidecar=True, embed=True, replace=False)
        preserve_result = import_candidate("preserve", has_art=True, screenshot="07-preserve-direct-import.png")
        assert preserve_result["artwork"]["sidecars"]["preserved"]
        assert preserve_result["artwork"]["embedded"]["preserved"]

        set_artwork_settings(page, base_url, sidecar=True, embed=True, replace=True)
        replace_result = import_candidate("replace", has_art=True, screenshot="08-replace-direct-import.png")
        assert replace_result["artwork"]["sidecars"]["written"]
        assert replace_result["artwork"]["embedded"]["written"]
        missing_result = import_candidate("missing", has_art=False, screenshot="09-missing-art-direct-import.png")
        assert missing_result["artwork"]["status"] == "unavailable"

        case_result = import_candidate("case", has_art=False, screenshot="10-case-preserved-direct-import.png")
        assert not case_result["items"][0]["changes"].get("genre")

        feature_result = import_candidate(
            "feature_extra", has_art=False, screenshot="04-feature-track-changes-desktop-dark.png"
        )
        extra_item = next(item for item in feature_result["items"] if item["changes"].get("title"))
        assert extra_item["changes"]["title"] == {
            "from": "Extra [Remix]", "to": "Extra feat. Rich Homie Quan [Remix]",
        }
        assert extra_item["changes"]["artist"] == {"from": "2 Chainz feat. Rich Homie Quan", "to": "2 Chainz"}
        assert "albumartist" not in extra_item["changes"]

        set_ftintitle_settings(page, base_url, enabled=False)
        disabled_config = (state_path / "config.yaml").read_text(encoding="utf-8")
        assert "- ftintitle" not in disabled_config
        ftintitle_config_proof["disabled"] = {"plugin": "- ftintitle" in disabled_config}
        disabled_result = import_candidate(
            "feature_disabled", has_art=False, screenshot="04-ftintitle-disabled-desktop-dark.png"
        )
        disabled_changes = disabled_result["items"][0]["changes"]
        assert "title" not in disabled_changes and "artist" not in disabled_changes

        as_is_review = open_folder_album("As Is Artist")
        as_is_review.click(force=True)
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        if page.locator("[data-review-execute]").is_disabled():
            with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/library-import/albums/" in response.url):
                page.get_by_role("radio", name=re.compile("As Is")).click()
        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/execute")) as response_info:
            page.locator("[data-review-execute]").click()
        assert response_info.value.status in {200, 201}, response_info.value.status
        page.locator("[data-review-modal]").wait_for(state="hidden")
        assert "As Is Artist" in page.locator("#library-import-success").inner_text()
        page.screenshot(path=evidence / "11-as-is-direct-success.png", full_page=True)

        # Managed Library: album-only landing, lazy detail, manual writes,
        # exact-ID preview/apply, mismatch blocking, and responsive modal.
        page.evaluate("localStorage.removeItem('cratekeep-theme')")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.goto(f"{base_url}/library?q=As+Is+Artist&sort=artist&order=asc", wait_until="networkidle")
        assert page.locator("#browse-title").inner_text() == "Browse albums"
        assert page.locator(".album-card").count() == 1
        assert page.locator(".track-row").count() == 0
        assert "As Is Song" not in page.locator(".browse").inner_text()
        with sqlite3.connect(state_path / "app.db") as snapshot_db:
            snapshot_bytes = snapshot_db.execute(
                """SELECT SUM(size_bytes) FROM library_inventory
                    WHERE source_kind='library' AND present=1"""
            ).fetchone()[0]
        expected_library_size = _format_bytes(snapshot_bytes)
        assert page.locator(".summary-grid .card").last.locator("strong").inner_text() == expected_library_size
        assert expected_library_size != "0 MB"
        assert page.locator(".album-card [data-album-cover]").get_by_text("No art").is_visible()
        assert page.locator("body").evaluate("element => element.scrollWidth <= element.clientWidth")
        viewport_checks["desktop_1440x1000_no_horizontal_overflow"] = True
        page.screenshot(path=evidence / "12-library-albums-desktop-light.png", full_page=True)
        page.locator("#theme-toggle").click()
        assert page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        album_card = page.locator(".album-card").first
        album_id = int(album_card.get_attribute("data-album-id"))
        with page.expect_response(lambda response: response.url.endswith(f"/api/library/albums/{album_id}")):
            album_card.click()
        page.locator("#album-modal:not([hidden])").wait_for()
        assert page.locator("#track-list .track-row").count() == 1
        page.screenshot(path=evidence / "13-library-album-modal-desktop-dark.png", full_page=True)
        initial = request_json(f"{base_url}/api/library/albums/{album_id}")
        original_path = next(item["path"] for item in request_json(f"{base_url}/api/items")
                             if item["id"] == initial["tracks"][0]["id"])
        page.locator('#album-form [name="genre"]').fill("Soul")
        page.locator('#album-form [name="year"]').fill("2023")
        with page.expect_response(lambda response: response.request.method == "PATCH"
                                  and response.url.endswith(f"/api/library/albums/{album_id}")) as album_edit_info:
            page.locator('#album-form button[type="submit"]').click()
        album_edit = album_edit_info.value.json()
        assert (album_edit["album"]["genre"], album_edit["album"]["year"]) == ("Soul", 2023)
        page.wait_for_function("document.getElementById('album-status').textContent.includes('updated')")
        page.locator("#track-list .track-row button").click()
        page.locator("#track-form:not([hidden])").wait_for()
        page.locator('#track-form [name="title"]').fill("As Is Song Edited")
        with page.expect_response(lambda response: response.request.method == "PATCH"
                                  and "/tracks/" in response.url) as track_edit_info:
            page.locator('#track-form button[type="submit"]').click()
        track_response = track_edit_info.value
        assert track_response.status == 200, track_response.text()
        track_edit = track_response.json()
        page.wait_for_function("document.getElementById('album-status').textContent.includes('Song metadata')")
        assert track_edit["album"]["tracks"][0]["title"] == "As Is Song Edited"
        page.locator("#open-rematch").click()
        page.locator("#musicbrainz-id").fill(LIBRARY_MATCH_RELEASE_ID)
        before_preview = request_json(f"{base_url}/api/library/albums/{album_id}")
        with page.expect_response(lambda response: response.url.endswith("/rematch/preview")) as preview_info:
            page.locator('#rematch-form button[type="submit"]').click()
        preview = preview_info.value.json()
        assert preview["applicable"] is True
        page.wait_for_function("document.getElementById('rematch-status').textContent.includes('Preview only')")
        assert request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"] == before_preview["fingerprint"]
        page.screenshot(path=evidence / "14-library-rematch-preview-desktop-dark.png", full_page=True)
        with page.expect_response(lambda response: response.url.endswith("/rematch/apply")) as apply_info:
            page.locator("#apply-rematch").click()
        applied = apply_info.value.json()
        assert applied["changes"] == preview["changes"]
        page.wait_for_function("document.getElementById('rematch-status').textContent.includes('applied')")
        assert request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"] == applied["fingerprint"]
        assert next(item["path"] for item in request_json(f"{base_url}/api/items")
                    if item["id"] == initial["tracks"][0]["id"]) == original_path
        page.locator("#rematch-close").click()
        page.locator("#rematch-modal").wait_for(state="hidden")

        # Managed artwork is always preview-first. Exercise upload and exact-MBID
        # refresh through the visible controls, then prove a no-art refresh is a no-op.
        upload_fixture = evidence / ".managed-upload.jpg"
        upload_fixture.write_bytes(EXISTING_ARTWORK_JPEG)
        before_upload = request_json(f"{base_url}/api/library/albums/{album_id}")
        with page.expect_file_chooser() as chooser_info:
            page.locator("#choose-artwork").click()
        with page.expect_response(lambda response: response.url.endswith("/artwork/upload-preview")) as upload_info:
            chooser_info.value.set_files(str(upload_fixture))
        upload_fixture.unlink()
        upload_preview = upload_info.value.json()
        assert upload_info.value.status == 201 and upload_preview["source"] == "upload"
        page.locator("#artwork-preview:not([hidden])").wait_for()
        assert request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"] == before_upload["fingerprint"]
        assert not (tracks["as_is"].parent / "cover.jpg").exists()
        page.screenshot(path=evidence / "17-managed-artwork-upload-preview.png")
        with page.expect_response(lambda response: response.url.endswith("/artwork/apply")) as upload_apply_info:
            page.locator("#apply-artwork").click()
        upload_applied = upload_apply_info.value.json()
        assert upload_applied["sidecars_written"] == 1 and upload_applied["tracks_embedded"] == 1
        assert (tracks["as_is"].parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
        assert (MediaFile(str(tracks["as_is"])).images or [])[0].data == EXISTING_ARTWORK_JPEG
        assert page.locator("#album-artwork-current img").is_visible()
        assert page.locator(".album-card [data-album-cover] img").is_visible()
        assert page.locator(".recent-cover img").first.is_visible()

        before_refresh_art = request_json(f"{base_url}/api/library/albums/{album_id}")
        with page.expect_response(lambda response: response.url.endswith("/artwork/refresh-preview")) as refresh_info:
            page.locator("#refresh-artwork").click()
        refresh_preview = refresh_info.value.json()
        assert refresh_info.value.status == 201 and refresh_preview["release_id"] == LIBRARY_MATCH_RELEASE_ID
        assert (tracks["as_is"].parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
        assert request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"] == before_refresh_art["fingerprint"]
        with page.expect_response(lambda response: response.url.endswith("/artwork/apply")) as refresh_apply_info:
            page.locator("#apply-artwork").click()
        refresh_applied = refresh_apply_info.value.json()
        assert (tracks["as_is"].parent / "cover.jpg").read_bytes() == ARTWORK_JPEG
        assert (MediaFile(str(tracks["as_is"])).images or [])[0].data == ARTWORK_JPEG

        page.locator("#open-rematch").click()
        page.locator("#musicbrainz-id").fill(LIBRARY_NO_ART_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch/preview")):
            page.locator('#rematch-form button[type="submit"]').click()
        with page.expect_response(lambda response: response.url.endswith("/rematch/apply")):
            page.locator("#apply-rematch").click()
        page.locator("#rematch-close").click()
        preserved_art_sha = hashlib.sha256((tracks["as_is"].parent / "cover.jpg").read_bytes()).hexdigest()
        expected_failure["kind"] = "artwork"
        with page.expect_response(lambda response: response.url.endswith("/artwork/refresh-preview")) as no_art_info:
            page.locator("#refresh-artwork").click()
        expected_failure["kind"] = None
        assert no_art_info.value.status == 409
        page.wait_for_function("document.getElementById('album-status').textContent.includes('existing artwork was preserved')")
        assert hashlib.sha256((tracks["as_is"].parent / "cover.jpg").read_bytes()).hexdigest() == preserved_art_sha

        before_mismatch_fingerprint = request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"]
        page.locator("#open-rematch").click()
        page.locator("#musicbrainz-id").fill(LIBRARY_MISMATCH_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch/preview")) as mismatch_info:
            page.locator('#rematch-form button[type="submit"]').click()
        mismatch = mismatch_info.value.json()
        assert mismatch["applicable"] is False
        page.wait_for_function("document.getElementById('rematch-status').textContent.includes('Preview only')")
        assert page.locator("#apply-rematch").is_disabled()
        mismatch_fingerprint = request_json(f"{base_url}/api/library/albums/{album_id}")["fingerprint"]
        assert mismatch_fingerprint == before_mismatch_fingerprint
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("#rematch-modal .modal-panel").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        viewport_checks["mobile_390x844_rematch_no_horizontal_overflow"] = True
        page.screenshot(path=evidence / "15-library-rematch-mismatch-mobile-dark.png", full_page=True)
        page.keyboard.press("Escape")
        assert page.locator("#rematch-modal").is_hidden()
        assert page.locator("#open-rematch").evaluate("element => document.activeElement === element")
        viewport_checks["mobile_escape_restores_nested_modal_focus"] = True
        assert page.locator("#album-modal .modal-panel").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        viewport_checks["mobile_390x844_album_no_horizontal_overflow"] = True
        page.screenshot(path=evidence / "16-library-album-mobile-dark.png", full_page=True)
        page.keyboard.press("Escape")
        assert page.locator("#album-modal").is_hidden()
        managed_library_proof.update({
            "album_id": album_id, "path_before": original_path,
            "path_after": next(item["path"] for item in request_json(f"{base_url}/api/items")
                               if item["id"] == initial["tracks"][0]["id"]),
            "before": initial, "after": applied["album"],
            "album_edit": album_edit["changes"], "track_edit": track_edit["changes"],
            "preview_changes": preview["changes"], "apply_changes": applied["changes"],
            "preview_fingerprint": preview["fingerprint"], "applied_fingerprint": applied["fingerprint"],
            "mismatch_reasons": mismatch["inapplicable_reasons"],
            "mismatch_apply_disabled": True,
            "provider_call_count": 5, "provider_preview_requests": 5,
            "apply_refetched_provider": False,
            "preview_apply_parity": preview["changes"] == applied["changes"],
            "library_bytes": {"expected": expected_library_size, "rendered": expected_library_size},
            "artwork": {
                "upload_preview_sha256": upload_preview["sha256"],
                "upload_apply_sha256": upload_applied["sha256"],
                "refresh_preview_sha256": refresh_preview["sha256"],
                "refresh_apply_sha256": refresh_applied["sha256"],
                "preview_apply_parity": (
                    upload_preview["sha256"] == upload_applied["sha256"]
                    and refresh_preview["sha256"] == refresh_applied["sha256"]
                ),
                "no_art_preserved_sha256": preserved_art_sha,
            },
        })

        # Named Inbox sources: dynamic settings rows/picker, persistence, disambiguated
        # same-name selections, stale-preview retry, success, focus, and scoped refresh.
        fixture_root = tracks["case"].parents[3]
        page.set_viewport_size({"width": 1440, "height": 1000})
        set_artwork_settings(page, base_url, sidecar=True, embed=True, replace=False)
        page.goto(f"{base_url}/settings", wait_until="networkidle")
        rows = page.locator("[data-inbox-row]")
        assert rows.count() == 2
        rows.nth(0).locator('[name="inbox_name"]').fill("Downloads")
        rows.nth(1).get_by_role("button", name="Browse").click()
        assert page.locator("#browser").is_visible()
        page.locator("#browser-choose").click()
        assert rows.nth(1).get_by_role("button", name="Browse").evaluate(
            "element => document.activeElement === element"
        )
        page.locator("#add-inbox").click()
        assert rows.count() == 3
        rows.nth(2).locator('[name="inbox_name"]').fill("Temporary")
        rows.nth(2).locator('[name="inbox_path"]').fill(str(fixture_root / "temporary"))
        rows.nth(2).get_by_role("button", name=re.compile("Remove")).click()
        assert rows.count() == 2
        with page.expect_navigation(wait_until="networkidle"):
            page.get_by_role("button", name="Save settings").click()
        page.goto(f"{base_url}/settings", wait_until="networkidle")
        assert page.locator('[name="inbox_name"]').evaluate_all("elements => elements.map(element => element.value)") == ["Downloads", "Source 2"]
        page.goto(f"{base_url}/inbox", wait_until="networkidle")
        cards = page.locator("[data-inbox-id]")
        assert cards.count() == 2
        assert [card.locator("h2").inner_text() for card in cards.all()] == ["Downloads", "Source 2"]
        assert page.locator("[data-inbox-toggle]").evaluate_all(
            "elements => elements.map(element => element.getAttribute('aria-expanded'))"
        ) == ["false", "false"]
        assert all("Shared Selection" not in card.inner_text() for card in cards.all())
        scope_responses = []
        for index in range(cards.count()):
            source_id = cards.nth(index).get_attribute("data-inbox-id")
            with page.expect_response(lambda response, source_id=source_id: response.url.endswith(f"/api/inbox/{source_id}/scopes")) as scope_info:
                cards.nth(index).locator("[data-inbox-toggle]").click()
            cards.nth(index).locator('[data-scope="Shared Selection"]').wait_for()
            scope_responses.append(scope_info.value)
        assert cards.nth(0).locator("[data-scope]").count() >= 31
        assert cards.nth(1).locator("[data-scope]").count() >= 31
        assert page.locator(".review-pagination").count() == 0
        inbox_page_metrics.update({
            "pagination_removed": True,
            "first_inbox_scope_rows": cards.nth(0).locator("[data-scope]").count(),
            "second_inbox_scope_rows": cards.nth(1).locator("[data-scope]").count(),
            "scope_payload_bytes": [len(response.body()) for response in scope_responses],
        })
        for index in range(cards.count()):
            card = cards.nth(index)
            source_id = card.get_attribute("data-inbox-id")
            card.get_by_role("checkbox", name="Select album folder Shared Selection", exact=True).check()
            with page.expect_response(lambda response, source_id=source_id: response.url.endswith(f"/api/inbox/{source_id}/scopes/scan")):
                card.get_by_role("button", name="Scan selected", exact=True).click()
            card.locator('[data-scope="Shared Selection"] [data-status="needs-review"]').wait_for()
        assert all("Shared Selection" in card.inner_text() for card in cards.all())
        cards.nth(0).locator('[data-scope="Shared Selection"] .review-folder-summary').screenshot(
            path=evidence / "_row-inbox.png"
        )
        with PillowImage.open(evidence / "_row-library.png") as library_row, PillowImage.open(evidence / "_row-inbox.png") as inbox_row:
            comparison = PillowImage.new("RGB", (library_row.width + inbox_row.width, max(library_row.height, inbox_row.height)), "white")
            comparison.paste(library_row.convert("RGB"), (0, 0)); comparison.paste(inbox_row.convert("RGB"), (library_row.width, 0))
            comparison.save(evidence / "17-library-inbox-rows-side-by-side.png")
        page.screenshot(path=evidence / "17-named-inboxes-before-import.png", full_page=True)

        first_scope = cards.nth(0).locator('[data-scope="Shared Selection"]')
        first_scope.get_by_role("button", name="Expand Shared Selection", exact=True).click()
        first_scope.locator(".review-album-row").wait_for()
        assert first_scope.locator(".review-album-row .status-pill").inner_text().lower() == "pending"
        first_scope.get_by_role("button", name=re.compile("Expand files for Shared Selection")).click()
        assert first_scope.locator(".review-file-name").inner_text() == "01 First Inbox.wav"
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("html").evaluate("element => element.scrollWidth <= element.clientWidth")
        page.screenshot(path=evidence / "17-inbox-row-tree-narrow.png", full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        first_scope.locator(".folder-import").click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        inbox_modal_contract = page.locator("[data-review-dialog]").evaluate("""dialog => ({
          controls:[...dialog.querySelectorAll('[data-review-find],[data-review-open-mbid],[data-review-cancel],[data-review-skip],[data-review-execute]')].map(node => node.textContent.trim()),
          currentPanels:dialog.querySelectorAll('[data-review-current]').length,
          approveControls:dialog.querySelectorAll('[data-review-approve]').length,
          candidateGroups:dialog.querySelectorAll('[data-review-candidates][role="radiogroup"]').length,
        })""")
        assert inbox_modal_contract == library_modal_contract
        viewport_checks["library_inbox_modal_contract_identical"] = inbox_modal_contract
        page.locator("[data-review-find]").click()
        page.wait_for_function("document.querySelector('[data-review-find]').getAttribute('aria-busy') === 'true'")
        assert page.locator("[data-review-execute]").get_attribute("aria-busy") == "false"
        assert page.locator("[data-review-execute] .button-spinner").is_hidden()
        page.wait_for_function("document.querySelectorAll('[data-review-candidates] .candidate-option').length > 0")
        candidate = page.locator("[data-review-candidates] .candidate-option").first
        assert all(candidate.filter(has_text=label).count() for label in (
            "Artist", "Album", "Genre", "Release Year", "Release Type/media",
            "Release Region", "Local tracks found online", "Online tracks present locally",
        ))
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/inbox/albums/" in response.url):
            candidate.locator("[role='radio']").click()
        track_button = candidate.get_by_role("button", name=re.compile("View track changes"))
        track_button.click()
        assert page.locator("[data-review-track-modal]").is_visible()
        assert page.locator("[data-review-track-rows] tr:not(.track-explanation-row)").count() == 1
        assert "Duration differs" in page.locator("[data-review-track-rows]").inner_text()
        page.screenshot(path=evidence / "17-inbox-track-changes-desktop-light.png", full_page=True)
        page.keyboard.press("Escape")
        assert track_button.evaluate("element => document.activeElement === element")
        page.screenshot(path=evidence / "17-inbox-review-desktop-light.png", full_page=True)
        page.evaluate("localStorage.setItem('cratekeep-theme','dark');document.body.classList.add('dark-mode')")
        page.screenshot(path=evidence / "17-inbox-review-desktop-dark.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("[data-review-dialog]").evaluate("element => element.scrollWidth <= element.clientWidth")
        page.screenshot(path=evidence / "17-inbox-review-narrow-dark.png", full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_function("document.querySelector('[data-review-execute]').disabled === false")
        first_track = fixture_root / "inbox" / "Shared Selection" / "01 First Inbox.wav"
        with page.expect_response(lambda response: "/api/inbox/albums/" in response.url and response.url.endswith("/execute") and response.status == 201) as executed:
            page.locator("[data-review-execute]").click()
        assert executed.value.status == 201
        assert executed.value.request.post_data_json == {}
        first_result = executed.value.json()
        first_destination = fixture_root / "library" / first_result["destinations"][0]
        assert first_result["destinations"] == ["First Inbox/Shared Selection/01 First Inbox.wav"]
        assert first_destination.is_file() and not first_track.exists()
        assert MediaFile(str(first_destination)).images[0].data == ARTWORK_JPEG
        assert (first_destination.parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
        first_source_cover = first_track.parent / "cover.jpg"
        if first_source_cover.exists():
            assert first_source_cover.read_bytes() == EXISTING_ARTWORK_JPEG
        assert first_result["artwork"]["sha256"] == hashlib.sha256(ARTWORK_JPEG).hexdigest()
        assert first_result["artwork"]["sidecar_sha256"] == hashlib.sha256(EXISTING_ARTWORK_JPEG).hexdigest()
        page.wait_for_function("""() => !document.querySelectorAll('[data-inbox-id]')[0]
          .querySelector('[data-scope="Shared Selection"]')""")
        page.wait_for_function("document.activeElement === document.querySelector('#inbox-success')")
        inbox_review_proof.update({
            "candidate_destination": str(first_destination),
            "candidate_destination_relative": first_result["destinations"][0],
            "source_removed": not first_track.exists(),
            "embedded_art_sha256": hashlib.sha256(MediaFile(str(first_destination)).images[0].data).hexdigest(),
            "sidecar_art_sha256": sha256(first_destination.parent / "cover.jpg"),
            "incoming_sidecar_sha256": hashlib.sha256(EXISTING_ARTWORK_JPEG).hexdigest(),
            "incoming_sidecar_retained": first_source_cover.is_file(),
            "success_focus": True,
        })

        # Production shape: two album reviews share one ordinary non-root
        # folder. Import A from isolated staging and prove every sibling byte
        # plus review B remain intact and reviewable.
        shared_folder = fixture_root / "inbox" / "Shared Ordinary"
        shared_a = shared_folder / "01 First.wav"
        shared_b = shared_folder / "02 Second.wav"
        shared_notes = shared_folder / "notes.txt"
        shared_cover = shared_folder / "cover.jpg"
        shared_before = {str(path): sha256(path) for path in (shared_a, shared_b, shared_notes, shared_cover)}
        cards = page.locator("[data-inbox-id]")
        first_card = cards.nth(0)
        first_card.get_by_role("checkbox", name="Select album folder Shared Ordinary", exact=True).check()
        source_id = first_card.get_attribute("data-inbox-id")
        with page.expect_response(lambda response: response.url.endswith(f"/api/inbox/{source_id}/scopes/scan")):
            first_card.get_by_role("button", name="Scan selected", exact=True).click()
        shared_scope = first_card.locator('[data-scope="Shared Ordinary"]')
        shared_scope.locator('[data-status="needs-review"]').wait_for()
        assert "2 albums · 2 tracks" in shared_scope.inner_text()
        shared_scope.get_by_role("button", name=re.compile("Expand Shared Ordinary")).click()
        page.screenshot(path=evidence / "17-shared-ordinary-before-import.png", full_page=True)
        album_a_row = shared_scope.locator(".review-album-row").filter(has_text="Shared Album A")
        album_a_row.locator(".review-album").click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/inbox/albums/" in response.url):
            page.get_by_role("radio", name=re.compile("As Is")).click()
        with page.expect_response(lambda response: response.url.endswith("/execute") and response.status == 201) as shared_execute:
            page.locator("[data-review-execute]").click()
        shared_result = shared_execute.value.json()
        assert shared_result["execution_selection"] == "staged-album"
        assert shared_result["source_artwork"]["status"] == "ignored-shared-source"
        assert not shared_a.exists()
        shared_after = {str(path): sha256(path) for path in (shared_b, shared_notes, shared_cover)}
        assert shared_after == {path: digest for path, digest in shared_before.items() if path != str(shared_a)}
        remaining_shared = [album for album in request_json(f"{base_url}/api/inbox/reviews")["albums"]
                            if album["album"] == "Shared Album B"]
        assert len(remaining_shared) == 1 and remaining_shared[0]["execution_status"] != "complete"
        page.reload(wait_until="networkidle")
        cards = page.locator("[data-inbox-id]")
        first_card = cards.nth(0)
        with page.expect_response(lambda response: response.url.endswith(f"/api/inbox/{source_id}/scopes")):
            first_card.locator("[data-inbox-toggle]").click()
        shared_scope = first_card.locator('[data-scope="Shared Ordinary"]')
        shared_scope.wait_for()
        assert shared_scope.get_by_role("button", name="Import Shared Ordinary", exact=True).count() == 1
        assert shared_scope.locator(".folder-expand").count() == 1
        page.screenshot(path=evidence / "17-shared-ordinary-after-import.png", full_page=True)
        shared_scope.get_by_role("button", name="Import Shared Ordinary", exact=True).click()
        page.locator("[data-review-modal]:not([hidden])").wait_for()
        assert "Shared Album B" in page.locator("[data-review-title]").inner_text()
        page.screenshot(path=evidence / "17-shared-ordinary-review-b-after-a.png", full_page=True)
        page.locator("[data-review-close]").click()
        inbox_review_proof["shared_non_root_exact_staging"] = {
            "execution_selection": shared_result["execution_selection"],
            "source_contains_other_albums_error": False,
            "sibling_sha256_before": shared_before[str(shared_b)],
            "sibling_sha256_after": shared_after[str(shared_b)],
            "sidecars_unchanged": all(shared_after[str(path)] == shared_before[str(path)] for path in (shared_notes, shared_cover)),
            "review_b_remaining": True,
        }

        # Match-only automation runs only for the explicitly selected scope and
        # never approves, imports, or mutates the sibling Inbox.
        provider_calls_before_auto = request_json(f"{base_url}/__smoke/provider-calls")["calls"].get("Shared Selection", 0)
        library_items_before_auto = request_json(f"{base_url}/api/items")
        page.goto(f"{base_url}/settings", wait_until="networkidle")
        page.locator('[name="inbox_auto_match_id"]').nth(1).check()
        with page.expect_navigation(wait_until="networkidle"):
            page.get_by_role("button", name="Save settings").click()
        page.goto(f"{base_url}/inbox", wait_until="networkidle")
        cards = page.locator("[data-inbox-id]")
        second_source_id = cards.nth(1).get_attribute("data-inbox-id")
        with page.expect_response(lambda response: response.url.endswith(f"/api/inbox/{second_source_id}/scopes")):
            cards.nth(1).locator("[data-inbox-toggle]").click()
        second_card = cards.nth(1)
        second_card.get_by_role("checkbox", name="Select album folder Shared Selection", exact=True).check()
        with page.expect_response(lambda response: response.url.endswith(f"/api/inbox/{second_source_id}/scopes/scan")):
            second_card.get_by_role("button", name="Scan selected", exact=True).click()
        page.wait_for_function("""async sourceId => {
          const data = await (await fetch('/api/inbox/reviews')).json();
          return data.albums.some(album => album.source_id === sourceId && album.candidate_status === 'complete');
        }""", arg=second_source_id, timeout=10000)
        provider_calls_after_auto = request_json(f"{base_url}/__smoke/provider-calls")["calls"].get("Shared Selection", 0)
        assert provider_calls_after_auto == provider_calls_before_auto + 1
        auto_album = next(album for album in request_json(f"{base_url}/api/inbox/reviews")["albums"]
                          if album["source_id"] == second_source_id)
        second_track = fixture_root / "inbox-two" / "Shared Selection" / "01 Second Inbox.wav"
        assert auto_album["decision"] == "pending" and auto_album["execution_status"] == "not-run"
        assert second_track.exists() and request_json(f"{base_url}/api/items") == library_items_before_auto
        page.reload(wait_until="networkidle")
        cards = page.locator("[data-inbox-id]")
        with page.expect_response(lambda response: response.url.endswith(f"/api/inbox/{second_source_id}/scopes")):
            cards.nth(1).locator("[data-inbox-toggle]").click()
        assert request_json(f"{base_url}/__smoke/provider-calls")["calls"].get("Shared Selection", 0) == provider_calls_after_auto
        inbox_review_proof["auto_match"] = {
            "calls_before": provider_calls_before_auto, "calls_after": provider_calls_after_auto,
            "calls_after_reload": request_json(f"{base_url}/__smoke/provider-calls")["calls"].get("Shared Selection", 0),
            "decision_before_explicit_approval": auto_album["decision"],
            "execution_before_explicit_approval": auto_album["execution_status"],
            "source_present_before_approval": second_track.exists(),
            "library_unchanged_before_approval": True,
        }
        second_scope = cards.nth(1).locator('[data-scope="Shared Selection"]')
        second_scope.locator(".folder-import").click()
        page.wait_for_function("document.querySelectorAll('[data-review-candidates] .candidate-option').length > 0")
        with page.expect_response(lambda response: response.request.method == "PATCH" and "/api/inbox/albums/" in response.url):
            page.locator("[data-review-candidates] .candidate-option").first.locator("[role='radio']").click()
        page.wait_for_function("document.querySelector('[data-review-execute]').disabled === false")
        with page.expect_response(lambda response: "/api/inbox/albums/" in response.url and response.url.endswith("/execute") and response.status == 201) as second_executed:
            page.locator("[data-review-execute]").click()
        assert second_executed.value.status == 201
        as_is_result = second_executed.value.json()
        second_destination = fixture_root / "library" / as_is_result["destinations"][0]
        assert as_is_result["destinations"] == ["Second Inbox/Shared Selection/01 Second Inbox.wav"]
        assert second_destination.is_file() and not second_track.exists()
        assert MediaFile(str(second_destination)).images[0].data == ARTWORK_JPEG
        assert (second_destination.parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
        second_source_cover = second_track.parent / "cover.jpg"
        if second_source_cover.exists():
            assert second_source_cover.read_bytes() == EXISTING_ARTWORK_JPEG
        inbox_review_proof["approved_second_inbox"] = {
            "destination": str(second_destination),
            "destination_relative": as_is_result["destinations"][0],
            "source_removed": not second_track.exists(),
            "source_sidecar_retained": second_source_cover.is_file(),
            "source_sidecar_sha256": hashlib.sha256(EXISTING_ARTWORK_JPEG).hexdigest(),
            "destination_sidecar_sha256": sha256(second_destination.parent / "cover.jpg"),
            "embedded_images": len(MediaFile(str(second_destination)).images or []),
        }
        page.wait_for_function("""() => !document.querySelectorAll('[data-inbox-id]')[1]
          .querySelector('[data-scope="Shared Selection"]')""")
        page.set_viewport_size({"width": 390, "height": 844})
        mobile_overflow = page.locator("body").evaluate("""body => ({
          scrollWidth:body.scrollWidth, clientWidth:body.clientWidth,
          elements:[...document.querySelectorAll('*')].filter(node => {
            const box=node.getBoundingClientRect(); return box.right > body.clientWidth + 1;
          }).slice(0,10).map(node => ({tag:node.tagName, className:node.className, right:node.getBoundingClientRect().right}))
        })""")
        assert mobile_overflow["scrollWidth"] <= mobile_overflow["clientWidth"], mobile_overflow
        page.screenshot(path=evidence / "17-named-inboxes-after-import-mobile.png", full_page=True)
        viewport_checks["named_inboxes_picker_persistence_scoped_retry_success"] = True

        # Shared shell/palette/density contract across every primary page.
        primary_pages = {"overview": "/", "inbox": "/inbox", "library": "/library", "settings": "/settings"}
        palette_proof = {}
        page.set_viewport_size({"width": 1440, "height": 1000})
        for mode in ("light", "dark"):
            expected_tokens = None
            for name, path in primary_pages.items():
                page.evaluate("mode => mode === 'dark' ? localStorage.setItem('cratekeep-theme','dark') : localStorage.removeItem('cratekeep-theme')", mode)
                page.goto(f"{base_url}{path}", wait_until="networkidle")
                tokens = page.evaluate("""() => {
                  const body=getComputedStyle(document.body), style=body;
                  return {page:style.getPropertyValue('--page').trim(),accent:style.getPropertyValue('--accent').trim(),
                          surface:style.getPropertyValue('--surface').trim(),fontSize:body.fontSize,
                          sidebar:document.getElementById('sidebar')?.getBoundingClientRect().width};
                }""")
                expected_tokens = expected_tokens or tokens
                assert tokens == expected_tokens, f"shared {mode} tokens drifted on {name}: {tokens}"
                assert tokens["fontSize"] == "14px" and tokens["sidebar"] <= 190
                contrast = page.evaluate("""() => {
                  const luminance = rgb => rgb.slice(0,3).map(value => value / 255)
                    .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
                    .reduce((sum,value,index) => sum + value * [.2126,.7152,.0722][index], 0);
                  const channels = value => (value.match(/[\\d.]+/g) || []).map(Number);
                  const background = element => { let node=element, color; do {
                    color=channels(getComputedStyle(node).backgroundColor); node=node.parentElement;
                  } while(node && color.length === 4 && color[3] === 0); return color;
                  };
                  const ratio = element => { const fg=luminance(channels(getComputedStyle(element).color));
                    const bg=luminance(background(element)); return (Math.max(fg,bg)+.05)/(Math.min(fg,bg)+.05); };
                  const selectors={primary:'.button-primary,.save-button,button[type="submit"]',
                    secondary:'button:not(.button-primary):not(.button-quiet):not(.button-danger):not(.remove-inbox):not(:disabled)',
                    quiet:'.button-quiet',danger:'.button-danger,.remove-inbox'};
                  return Object.fromEntries(Object.entries(selectors).flatMap(([key,selector]) => {
                    const element=document.querySelector(selector); return element ? [[key,ratio(element)]] : [];
                  }));
                }""")
                assert all(ratio >= 4.5 for ratio in contrast.values()), f"{mode} {name} button contrast: {contrast}"
                assert page.locator(".nav-link.button-primary,.album-card.button-primary").count() == 0
                assert page.locator("body").evaluate("element => element.scrollWidth <= element.clientWidth")
                page.screenshot(path=evidence / f"18-{name}-desktop-{mode}.png")
            palette_proof[mode] = expected_tokens
        page.set_viewport_size({"width": 390, "height": 844})
        narrow_shell = None
        for name, path in {"inbox": "/inbox", "library": "/library"}.items():
            page.goto(f"{base_url}{path}", wait_until="networkidle")
            geometry = page.evaluate("""() => {
              const sidebar = document.getElementById('sidebar');
              const nav = sidebar.querySelector('.nav-link');
              const mobile = document.querySelector('.mobile-header');
              return {sidebarWidth:sidebar.getBoundingClientRect().width,
                      sidebarPadding:getComputedStyle(sidebar).padding,
                      navFont:getComputedStyle(nav).font,
                      navPadding:getComputedStyle(nav).padding,
                      mobileHeight:mobile.getBoundingClientRect().height,
                      mobilePadding:getComputedStyle(mobile).padding};
            }""")
            narrow_shell = narrow_shell or geometry
            assert geometry == narrow_shell, f"narrow shared shell drifted on {name}: {geometry}"
        page.set_viewport_size({"width": 390, "height": 844})
        for name, path in primary_pages.items():
            page.evaluate("localStorage.setItem('cratekeep-theme','dark')")
            page.goto(f"{base_url}{path}", wait_until="networkidle")
            assert page.locator("body").evaluate("element => element.scrollWidth <= element.clientWidth")
            if name == "library":
                album_pagination = page.locator("nav.pagination")
                assert album_pagination.locator('[aria-label$=" page"]').count() == 4
                assert all(glyph in album_pagination.inner_text() for glyph in ("«", "‹", "›", "»"))
                assert album_pagination.evaluate("element => element.scrollWidth <= element.clientWidth")
            page.screenshot(path=evidence / f"19-{name}-mobile-dark.png")
        viewport_checks["primary_pages_shared_palette"] = palette_proof
        viewport_checks["primary_pages_desktop_light_dark_and_mobile_no_overflow"] = True
        browser.close()

    unexpected_console = assert_no_browser_errors(page_errors, console_errors, network)
    inbox_after = request_json(f"{base_url}/api/inbox")
    assert any(item["path"] == "Rollback Retained" for item in inbox_before)
    assert any(item["inbox_name"] == "Downloads" and item["path"] == "Rollback Retained" for item in inbox_after)
    assert not any(item["path"] == "Shared Selection" for item in inbox_after)
    assert sum(item["path"].startswith("Z Synthetic") for item in inbox_after) == 60
    return {
        "browser_version": browser_version,
        "page_errors": page_errors, "console_errors": unexpected_console,
        "expected_failure_console": [message for message in console_errors if message not in unexpected_console],
        "network": network,
        "folder_page_timings_ms": folder_page_timings_ms,
        "inline_scan_state_proof": inline_scan_state_proof,
        "sequential_candidate_proof": sequential_proof,
        "ftintitle_config_proof": ftintitle_config_proof,
        "deluxe_edition_proof": deluxe_proof,
        "managed_library_proof": managed_library_proof,
        "inbox_review_proof": inbox_review_proof,
        "inbox_page_metrics": inbox_page_metrics,
        "viewport_checks": viewport_checks,
        "inbox_retention": {"before": inbox_before, "after": inbox_after},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--app-inbox")
    parser.add_argument("--app-library")
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--source-set-definition", required=True)
    args = parser.parse_args()
    args.fixture_root.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    tracks = make_fixture(args.fixture_root)
    before = {name: sha256(path) for name, path in tracks.items()}
    synthetic_before = {str(path.relative_to(args.fixture_root)):sha256(path)
                        for path in args.fixture_root.rglob("Z Synthetic*/*.wav")}
    host_identity_before = {
        name: {"path": str(path.resolve()), "device": path.stat().st_dev, "inode": path.stat().st_ino}
        for name, path in tracks.items()
    }
    app_library = Path(args.app_library).resolve() if args.app_library else (args.fixture_root / "library").resolve()
    configure(
        args.base_url.rstrip("/"),
        [
            args.app_inbox or str((args.fixture_root / "inbox").resolve()),
            str(Path(args.app_inbox).parent / "inbox-two") if args.app_inbox
            else str((args.fixture_root / "inbox-two").resolve()),
        ],
        str(app_library),
    )
    browser_result = run_browser(args.base_url.rstrip("/"), args.state_path, args.evidence_dir, tracks)
    after = {name: sha256(path) for name, path in tracks.items()}
    synthetic_after = {str(path.relative_to(args.fixture_root)):sha256(path)
                       for path in args.fixture_root.rglob("Z Synthetic*/*.wav")}
    assert synthetic_after == synthetic_before and len(synthetic_after) == 60
    assert before["as_is"] != after["as_is"], "managed Library edits did not update the fixture tags"
    assert all(track.exists() for track in tracks.values()), "direct import moved or removed a source file"
    host_identity_after = {
        name: {"path": str(path.resolve()), "device": path.stat().st_dev, "inode": path.stat().st_ino}
        for name, path in tracks.items()
    }
    assert host_identity_after == host_identity_before, "direct import moved or replaced a host fixture file"
    items = request_json(f"{args.base_url.rstrip('/')}/api/items")
    excluded_queue = {"queue_tied", "queue_lower", "queue_error", "queue_unselected"}
    registered_names = set(tracks) - excluded_queue
    assert len(items) == len(registered_names) + 3
    reported_paths = {Path(item["path"]) for item in items}
    by_path = {Path(item["path"]).name: item for item in items}
    for name in registered_names:
        expected_app_path = app_library / TRACKS[name]
        assert expected_app_path.is_absolute()
        assert expected_app_path.is_relative_to(app_library)
        assert expected_app_path in reported_paths
    managed_tags = MediaFile(str(tracks["as_is"]))
    assert (managed_tags.title, managed_tags.album, managed_tags.albumartist,
            managed_tags.genre, managed_tags.year) == (
        "As Is Song Edited", "As Is Album (MBID Edition)", "As Is Artist", "Soul", 2024,
    )
    managed_proof = browser_result["managed_library_proof"]
    managed_track = managed_proof["after"]["tracks"][0]
    managed_proof.update({
        "database_values": {
            key: managed_track[key] for key in ("title", "album", "albumartist", "artist", "genre", "year")
        },
        "file_values": {
            "title": managed_tags.title, "album": managed_tags.album,
            "albumartist": managed_tags.albumartist, "artist": managed_tags.artist,
            "genre": managed_tags.genre, "year": managed_tags.year,
        },
        "db_file_parity": all(managed_track[key] == getattr(managed_tags, key)
                              for key in ("title", "album", "albumartist", "artist", "genre", "year")),
        "path_unchanged": managed_proof["path_before"] == managed_proof["path_after"],
    })

    sidecar_tags = MediaFile(str(tracks["sidecar"]))
    embed_tags = MediaFile(str(tracks["embed"]))
    preserve_tags = MediaFile(str(tracks["preserve"]))
    replace_tags = MediaFile(str(tracks["replace"]))
    missing_tags = MediaFile(str(tracks["missing"]))
    assert (tracks["sidecar"].parent / "cover.jpg").read_bytes() == ARTWORK_JPEG
    assert not sidecar_tags.images
    assert not (tracks["embed"].parent / "cover.jpg").exists()
    assert embed_tags.images[0].data == ARTWORK_JPEG
    assert (tracks["preserve"].parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
    assert preserve_tags.images[0].data == EXISTING_ARTWORK_JPEG
    assert (tracks["replace"].parent / "cover.jpg").read_bytes() == ARTWORK_JPEG
    assert replace_tags.images[0].data == ARTWORK_JPEG
    assert (tracks["missing"].parent / "cover.jpg").read_bytes() == EXISTING_ARTWORK_JPEG
    assert missing_tags.images[0].data == EXISTING_ARTWORK_JPEG
    expected_genres = {
        "case": "Alternative Rock", "sidecar": "Electronic", "embed": "Jazz", "preserve": "Blues", "replace": "Folk", "missing": "Ambient",
    }
    assert {name: by_path[path.name]["genre"] for name, path in tracks.items() if name in GENRES} == expected_genres
    file_genres = {name: MediaFile(str(tracks[name])).genre for name in GENRES}
    assert file_genres == expected_genres
    feature_db = by_path[tracks["feature_extra"].name]
    assert (feature_db["title"], feature_db["artist"], feature_db["albumartist"]) == (
        "Extra feat. Rich Homie Quan [Remix]", "2 Chainz", "2 Chainz",
    )
    feature_tags = MediaFile(str(tracks["feature_extra"]))
    assert (feature_tags.title, feature_tags.artist, feature_tags.albumartist) == (
        "Extra feat. Rich Homie Quan [Remix]", "2 Chainz", "2 Chainz",
    )
    disabled_db = by_path[tracks["feature_disabled"].name]
    disabled_tags = MediaFile(str(tracks["feature_disabled"]))
    assert (disabled_db["title"], disabled_db["artist"], disabled_db["albumartist"]) == (
        "Disabled Song", "Disabled Lead feat. Guest", "Disabled Lead",
    )
    assert (disabled_tags.title, disabled_tags.artist, disabled_tags.albumartist) == (
        "Disabled Song", "Disabled Lead feat. Guest", "Disabled Lead",
    )
    with sqlite3.connect(args.state_path / "app.db") as database:
        raw_feature = json.loads(database.execute(
            "SELECT provider_data_json FROM metadata_candidates WHERE provider_id = ?",
            ("123e4567-e89b-42d3-a456-426614174088",),
        ).fetchone()[0])
    assert raw_feature["tracks"][1]["title"] == "Extra [Remix]"
    assert raw_feature["tracks"][1]["track_artist"] == "2 Chainz feat. Rich Homie Quan"
    result = {
        "source_identity": {
            "commit_at_run": args.source_commit,
            "tree_at_run": args.source_tree,
            "definition": args.source_set_definition,
        },
        "fixture_sha256_before": before,
        "fixture_sha256_after": after,
        "synthetic_match_only": {"files":len(synthetic_after),"unchanged":synthetic_after == synthetic_before,
                                 "aggregate_sha256":hashlib.sha256(json.dumps(synthetic_after,sort_keys=True).encode()).hexdigest()},
        "host_identity_before": host_identity_before,
        "host_identity_after": host_identity_after,
        "expected_app_library": str(app_library),
        "registered_paths": sorted(item["path"] for item in items),
        "provider_mock": {
            "kind": "persisted MusicBrainz/CAA evidence plus config-injected server JPEG and routed browser thumbnail",
            "network_used": False,
        },
        "artwork_sha256": {
            "fetched": hashlib.sha256(ARTWORK_JPEG).hexdigest(),
            "existing": hashlib.sha256(EXISTING_ARTWORK_JPEG).hexdigest(),
        },
        "genre_results": {name: by_path[path.name]["genre"] for name, path in tracks.items() if name in GENRES},
        "file_genre_results": file_genres,
        "feature_credit_result": {
            "raw_candidate": {
                "title": raw_feature["tracks"][1]["title"],
                "artist": raw_feature["tracks"][1]["track_artist"],
            },
            "database": {key: feature_db[key] for key in ("title", "artist", "albumartist")},
            "file": {"title": feature_tags.title, "artist": feature_tags.artist, "albumartist": feature_tags.albumartist},
        },
        "feature_disabled_result": {
            "database": {key: disabled_db[key] for key in ("title", "artist", "albumartist")},
            "file": {"title": disabled_tags.title, "artist": disabled_tags.artist,
                     "albumartist": disabled_tags.albumartist},
        },
        "inbox_config_proof": {
            "command_config_path": str(args.state_path / "config.yaml"),
            "ftintitle_disabled_after_parity_check": "- ftintitle" not in (args.state_path / "config.yaml").read_text(),
        },
        **browser_result,
    }
    results_path = args.evidence_dir / "runtime-results.json"
    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.evidence_dir / "environment-identity.json").write_text(json.dumps({
        "hostname": platform.node(), "platform": platform.platform(),
        "python": sys.version, "browser": browser_result["browser_version"],
        "fixture_root": str(args.fixture_root.resolve()),
        "state_path": str(args.state_path.resolve()),
    }, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{sha256(path)}  {path.name}" for path in sorted(args.evidence_dir.iterdir()) if path.is_file()]
    (args.evidence_dir / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
