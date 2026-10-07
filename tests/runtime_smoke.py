#!/usr/bin/env python3
"""Real-browser smoke test for Settings and direct in-place library import."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

from mediafile import Image as MediaImage, ImageType, MediaFile
from playwright.sync_api import Page, sync_playwright

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
    "deluxe": Path("Big Sean") / "Dark Sky Paradise (Deluxe)" / "01 Blessings.wav",
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


def request_json(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def configure(base_url: str, inbox_path: str, library_path: str) -> None:
    body = urllib.parse.urlencode({"inbox_path": inbox_path, "library_path": library_path}).encode()
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
    origin = "inbox" if enabled else "settings#metadata"
    page.goto(f"{base_url}/{origin}", wait_until="networkidle")
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
    inbox_before = request_json(f"{base_url}/api/inbox")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
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
                                                  expected_failure["kind"] == "rematch" and response.status == 503
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
        assert page.locator(".review-folder").count() == 25
        started = time.perf_counter()
        with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                  and "page=1" in response.url):
            page.locator("#library-import-page-input").fill("1")
            page.locator("#library-import-page-input").press("Enter")
        folder_page_timings_ms["page_1"] = round((time.perf_counter() - started) * 1000, 2)
        first_page_name = page.locator(".review-folder-path").first.inner_text()
        page.locator(".review-folder .review-select").first.check()
        started = time.perf_counter()
        page.locator("#folder-page-next").click()
        page.wait_for_function("document.getElementById('library-import-page-input').value === '2'")
        folder_page_timings_ms["page_2"] = round((time.perf_counter() - started) * 1000, 2)
        assert page.locator(".review-folder").count() >= 2
        second_page_name = page.locator(".review-folder-path").first.inner_text()
        page.locator(".review-folder .review-select").first.check()
        page.get_by_text("2 folders selected", exact=False).wait_for()
        started = time.perf_counter()
        with page.expect_response(lambda response: "/api/library-import/folders?" in response.url
                                  and "page=1" in response.url):
            page.locator("#library-import-page-input").fill("1")
            page.locator("#library-import-page-input").press("Enter")
        page.get_by_text(first_page_name, exact=True).wait_for()
        folder_page_timings_ms["direct_jump_page_1"] = round((time.perf_counter() - started) * 1000, 2)
        assert page.locator(".review-folder .review-select").first.is_checked()
        page.screenshot(path=evidence / "pagination-1-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=evidence / "pagination-narrow.png", full_page=True)
        assert page.locator("html").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        ), "narrow folder queue must not create page-level horizontal overflow"
        assert page.locator("#library-import-review-list").evaluate("element => element.scrollWidth <= element.clientWidth")
        page.set_viewport_size({"width": 1440, "height": 1000})
        assert second_page_name

        # Rapid navigation must leave the newest requested page rendered even if
        # the earlier request is aborted or completes after it.
        page.evaluate("() => { loadFolderItems(2); loadFolderItems(5); }")
        page.wait_for_function("document.getElementById('library-import-page-input').value === '5'")
        page.wait_for_timeout(250)
        assert page.locator("#library-import-page-input").input_value() == "5"

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
        safe_status = page.locator('.review-folder[data-folder-path="Queue Safe"] .status-pill')
        sequential_status = page.locator('.review-folder[data-folder-path="Queue Sequential"] .status-pill')
        unselected_status = page.locator('.review-folder[data-folder-path="Queue Unselected"] .status-pill')
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
          && document.querySelector('.review-folder[data-folder-path="Queue Sequential"] .status-pill')?.textContent === 'needs review'
          && document.querySelector('.review-folder[data-folder-path="Queue Error"] .status-pill')?.textContent === 'error'
        """)
        assert page.locator('.review-folder[data-folder-path="Queue Error"] .status-pill').get_attribute("title")
        assert page.locator("#library-import-review-status").inner_text() == review_status_before_scan
        inline_scan_state_proof["settled"] = {
            "safe": safe_status.inner_text(),
            "sequential": sequential_status.inner_text(),
            "error": page.locator('.review-folder[data-folder-path="Queue Error"] .status-pill').inner_text(),
            "unselected": unselected_status.inner_text(),
        }
        page.screenshot(path=evidence / "03-folder-inline-settled.png", full_page=True)
        assert all((sha256(tracks[name]), str(tracks[name].resolve()), tracks[name].stat().st_ino) == before
                   for name, before in queue_before.items()), "folder scan changed media bytes, path, or inode"
        assert page.locator("#library-import-ready").inner_text() == "Import ready (1)"
        assert page.locator("#library-import-review-exceptions").inner_text() == "Review exceptions (3)"
        page.get_by_role("checkbox", name="Select folder Queue Error", exact=True).uncheck()
        page.get_by_text("2 folders selected", exact=False).wait_for()
        assert page.locator("#library-import-review-exceptions").inner_text() == "Review exceptions (2)"
        page.locator("#library-import-review-last").click()
        page.wait_for_function("document.getElementById('library-import-page-input').value === '6'")
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
        page.wait_for_function("document.getElementById('library-import-page-input').value === '5'")
        active_after_bulk = request_json(f"{base_url}/api/library-import/folders")
        active_by_path = {folder["path"]: folder for folder in active_after_bulk["folders"]}
        assert "Queue Safe" not in active_by_path
        assert active_by_path["Queue Sequential"]["album_count"] == 2
        assert active_by_path["Queue Sequential"]["track_count"] == 2
        page.screenshot(path=evidence / "03-folder-bulk-import.png", full_page=True)
        with page.expect_response(lambda response: re.search(r"/api/library-import/albums/\d+$", response.url)) as first_album_info:
            page.locator("#library-import-review-exceptions").click()
        page.locator("#library-import-modal:not([hidden])").wait_for()
        first_payload = first_album_info.value.json()
        first_exception = page.locator("#library-import-modal-title").inner_text()
        page.get_by_role("button", name="Skip", exact=True).click()
        page.wait_for_function("title => document.getElementById('library-import-modal-title').textContent !== title", arg=first_exception)
        second_exception = page.locator("#library-import-modal-title").inner_text()
        assert second_exception != first_exception
        page.locator("#library-import-modal-close").click()
        page.locator("#library-import-modal").wait_for(state="hidden")

        # Reopening starts a fresh run. A failed first import stays on the same
        # album; retry success advances to a freshly fetched second payload.
        with page.expect_response(lambda response: re.search(r"/api/library-import/albums/\d+$", response.url)) as reopened_info:
            page.locator("#library-import-review-exceptions").click()
        page.locator("#library-import-modal:not([hidden])").wait_for()
        reopened_payload = reopened_info.value.json()
        assert reopened_payload["id"] == first_payload["id"]
        first_title = page.locator("#library-import-modal-title").inner_text()
        execute_pattern = re.compile(r".*/api/library-import/albums/\d+/execute$")
        expected_failure["kind"] = "execute"
        page.route(execute_pattern, lambda route: route.fulfill(
            status=502, content_type="application/json",
            body=json.dumps({"error": "Deterministic sequential import failure", "code": "fixture_failure"}),
        ), times=1)
        with page.expect_response(lambda response: response.url.endswith("/execute")) as failed_sequence:
            page.locator("#library-import-in-place").click()
        assert failed_sequence.value.status == 502
        expected_failure["kind"] = None
        assert page.locator("#library-import-modal-title").inner_text() == first_title
        assert page.locator("#library-import-in-place").inner_text() == "Import"

        with page.expect_response(lambda response: re.search(r"/api/library-import/albums/\d+$", response.url)) as next_album_info:
            with page.expect_response(lambda response: response.url.endswith("/execute")) as first_execute:
                page.locator("#library-import-in-place").click()
        next_payload = next_album_info.value.json()
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
        page.wait_for_function("title => document.getElementById('library-import-modal-title').textContent !== title", arg=first_title)
        with page.expect_response(lambda response: response.url.endswith("/execute")) as second_execute:
            page.locator("#library-import-in-place").click()
        second_body = second_execute.value.request.post_data_json
        sequential_proof.append({"album_id": next_payload["id"], "candidate_id": second_body["candidate_id"],
                                 "owned_candidate_ids": [candidate["id"] for candidate in next_payload["candidates"]]})
        assert second_body["candidate_id"] in sequential_proof[-1]["owned_candidate_ids"]
        assert sequential_proof[0]["album_id"] != sequential_proof[1]["album_id"]
        assert not any(
            folder["path"] == "Queue Sequential"
            for folder in request_json(f"{base_url}/api/library-import/folders")["folders"]
        )
        page.locator("#library-import-in-place", has_text="Close").wait_for()
        page.screenshot(path=evidence / "03-sequential-exceptions-complete.png", full_page=True)
        page.locator("#library-import-in-place").click()
        page.locator("#library-import-modal").wait_for(state="hidden")

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
        page.locator("#library-import-modal:not([hidden])").wait_for()
        page.locator("#library-import-open-mbid-search").click()
        page.locator("#library-import-mbid").fill(DELUXE_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as deluxe_rematch:
            page.locator("#library-import-mbid-search").click()
        assert deluxe_rematch.value.status == 200
        deluxe_payload = deluxe_rematch.value.json()
        deluxe_candidate = deluxe_payload["proposed_match"]
        assert deluxe_candidate["album"] == "Dark Sky Paradise (Deluxe)"
        assert deluxe_candidate["release_disambiguation"] == "deluxe, clean"
        assert "album" not in deluxe_candidate["proposed_diff"]
        assert deluxe_candidate["recordings"][0]["title"] == "Blessings feat. Drake"
        facts = page.locator(".candidate-option[open] .candidate-fact")
        assert facts.filter(has_text="Album").locator("dd").first.inner_text() == "Dark Sky Paradise (Deluxe)"
        assert facts.filter(has_text="MusicBrainz release note").locator("dd").inner_text() == "deluxe, clean"
        assert facts.filter(has_text="Album").first.get_attribute("data-changed") == "false"
        with sqlite3.connect(state_path / "app.db") as database:
            deluxe_raw = json.loads(database.execute(
                "SELECT provider_data_json FROM metadata_candidates WHERE id = ?", (deluxe_candidate["id"],),
            ).fetchone()[0])
        assert deluxe_raw["album"] == "Dark Sky Paradise"
        assert deluxe_raw["release_disambiguation"] == "deluxe, clean"
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
            page.locator("#library-import-in-place").click()
        assert deluxe_execute.value.status == 201
        deluxe_result = deluxe_execute.value.json()
        assert deluxe_result["items"][0]["changes"] == preview_changes
        deluxe_tags = MediaFile(str(tracks["deluxe"]))
        assert (deluxe_tags.album, deluxe_tags.title, deluxe_tags.artist) == (
            "Dark Sky Paradise (Deluxe)", "Blessings feat. Drake", "Big Sean",
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
        page.locator("#library-import-in-place", has_text="Close").click()
        page.locator("#library-import-modal").wait_for(state="hidden")

        candidate_review = open_folder_album("Sidecar Artist")
        candidate_review.click(force=True)
        page.locator("#library-import-modal:not([hidden])").wait_for()
        selected_candidate = page.get_by_role("radio", checked=True)
        assert "Sidecar Artist" in page.locator("#library-import-modal-candidates").inner_text()
        assert candidate_ids["sidecar"] > 0
        assert page.locator(".candidate-artwork").count() == 1

        # MBID lookup is a compact nested, metadata-only flow independent of Import.
        picker = page.locator(".candidate-picker")
        assert picker.count() == 1
        assert "top canonical MusicBrainz release-group genre" not in picker.inner_text()
        assert picker.evaluate("element => getComputedStyle(element).borderTopStyle") == "none"
        page.screenshot(path=evidence / "04-mbid-picker-desktop-dark.png")
        search_open = page.locator("#library-import-open-mbid-search")
        search_open.click()
        mbid_modal = page.locator("#library-import-mbid-modal")
        mbid_input = page.locator("#library-import-mbid")
        mbid_modal.wait_for(state="visible")
        assert mbid_input.evaluate("element => document.activeElement === element")
        assert page.locator(".album-modal-dialog").evaluate("element => element.inert")
        page.screenshot(path=evidence / "04-mbid-search-modal-desktop-dark.png")

        # Transient typing and Cancel do not dirty the parent review.
        mbid_input.fill("transient text")
        page.locator("#library-import-mbid-cancel").click()
        assert search_open.evaluate("element => document.activeElement === element")
        dialogs = []
        def record_dialog(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()
        page.on("dialog", record_dialog)
        page.locator("#library-import-modal-close").click()
        page.locator("#library-import-modal").wait_for(state="hidden")
        page.remove_listener("dialog", record_dialog)
        assert dialogs == [], "cancelled MBID typing must not trigger the parent dirty-state prompt"
        candidate_review.click(force=True)
        page.locator("#library-import-modal:not([hidden])").wait_for()
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
        assert page.locator("#library-import-mbid-search").evaluate("element => document.activeElement === element")
        page.keyboard.press("Tab")
        assert mbid_input.evaluate("element => document.activeElement === element")
        page.evaluate("document.getElementById('library-import-mbid-search').setAttribute('aria-busy', 'true')")
        page.locator("#library-import-mbid-cancel").click()
        page.keyboard.press("Escape")
        mbid_modal.click(position={"x": 2, "y": 2})
        assert mbid_modal.is_visible(), "busy MBID search must block Cancel, Escape, and backdrop close"
        page.evaluate("document.getElementById('library-import-mbid-search').setAttribute('aria-busy', 'false')")

        request_network_start = len(network)
        item_rows_before = request_json(f"{base_url}/api/items")
        file_before = {
            "sha256": sha256(tracks["sidecar"]),
            "stat": (tracks["sidecar"].stat().st_size, tracks["sidecar"].stat().st_mtime_ns,
                     tracks["sidecar"].stat().st_dev, tracks["sidecar"].stat().st_ino),
            "path": str(tracks["sidecar"].resolve()),
        }
        mbid_input.fill("not-a-uuid")
        page.locator("#library-import-mbid-search").click()
        assert "canonical MusicBrainz release UUID" in page.locator("#library-import-mbid-status").inner_text()
        assert len(network) == request_network_start, "invalid UUID must remain local"

        rematch_pattern = re.compile(r".*/api/library-import/albums/\d+/rematch$")
        page.route(rematch_pattern, lambda route: route.fulfill(
            status=503, content_type="application/json",
            body=json.dumps({"error": "fixture unavailable", "code": "provider_unavailable", "retryable": True}),
        ), times=1)
        mbid_input.fill(SEARCH_RELEASE_ID)
        expected_failure["kind"] = "rematch"
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as failed_rematch:
            page.locator("#library-import-mbid-search").click()
        assert failed_rematch.value.status == 503
        expected_failure["kind"] = None
        assert mbid_modal.is_visible()
        assert mbid_input.is_enabled() and mbid_input.input_value() == SEARCH_RELEASE_ID
        assert "could not be reached" in page.locator("#library-import-mbid-status").inner_text()

        candidate_options = page.locator("#library-import-modal-candidates [role='radio']")
        initial_option_count = candidate_options.count()
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as new_rematch:
            page.locator("#library-import-mbid-search").click()
        assert new_rematch.value.status == 200
        new_payload = new_rematch.value.json()
        mbid_modal.wait_for(state="hidden")
        assert page.locator("#library-import-modal").is_visible()
        assert "added and selected" in page.locator("#library-import-modal-message").inner_text()
        assert candidate_options.count() == initial_option_count + 1
        selected_candidate = page.get_by_role("radio", checked=True)
        assert selected_candidate.evaluate("element => element.parentElement.open")
        assert new_payload["selected_candidate_id"] == new_payload["proposed_match"]["id"]
        assert sum(candidate["provider_id"] == SEARCH_RELEASE_ID for candidate in new_payload["candidates"]) == 1
        assert page.locator("#library-import-in-place").inner_text() == "Import"

        # Looking up an existing release moves/selects its row without duplication.
        search_open.click(); mbid_input.fill(EXISTING_SIDECAR_RELEASE_ID)
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as existing_rematch:
            page.locator("#library-import-mbid-search").click()
        assert existing_rematch.value.status == 200
        existing_payload = existing_rematch.value.json()
        assert candidate_options.count() == initial_option_count + 1
        assert sum(candidate["provider_id"] == EXISTING_SIDECAR_RELEASE_ID for candidate in existing_payload["candidates"]) == 1
        assert existing_payload["candidates"][0]["provider_id"] == EXISTING_SIDECAR_RELEASE_ID
        assert existing_payload["selected_candidate_id"] == existing_payload["candidates"][0]["id"]
        assert page.get_by_role("radio", checked=True).evaluate("element => element.parentElement.open")

        search_requests = network[request_network_start:]
        assert not any(event["url"].endswith(("/preview", "/execute")) for event in search_requests)
        assert sum(event["url"].endswith("/rematch") for event in search_requests) == 3
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
        page.locator("#library-import-mbid-cancel").click()
        page.set_viewport_size({"width": 1440, "height": 1000})

        # Candidate and duplicate state share one guarded close path.
        page.get_by_role("radio", name=re.compile("As Is")).click()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#library-import-modal").click(position={"x": 2, "y": 2})
        assert page.locator("#library-import-modal").is_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(".album-modal-dialog").focus()
        page.keyboard.press("Escape")
        page.locator("#library-import-modal").wait_for(state="hidden")
        assert candidate_review.evaluate("element => document.activeElement === element")
        candidate_review.click(force=True)
        page.locator("#library-import-modal:not([hidden])").wait_for()
        page.evaluate("document.getElementById('library-import-execution-result').dataset.busy = 'true'")
        page.locator("#library-import-modal").click(position={"x": 2, "y": 2})
        assert page.locator("#library-import-modal").is_visible(), "busy review must not close"
        page.evaluate("document.getElementById('library-import-execution-result').dataset.busy = 'false'")

        # Close the clean modal before exercising persisted destination combinations.
        page.locator("#library-import-modal-close").click()

        def import_candidate(name: str, *, has_art: bool, screenshot: str, mock_failure: bool = False) -> dict:
            page.goto(f"{base_url}/settings#library-import", wait_until="networkidle")
            page.locator("#settings-tab-library-import").click()
            review = open_folder_album(TRACKS[name].parts[0])
            review.click(force=True)
            page.locator("#library-import-modal:not([hidden])").wait_for()
            candidate = page.get_by_role("radio", name=re.compile(re.escape(TRACKS[name].parts[0] + " — "))).first
            if candidate.get_attribute("aria-checked") != "true":
                candidate.click()
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
                rows = page.locator("#track-comparison-rows tr:not(.track-explanation-row)")
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
                page.locator("#track-comparison-close").click()
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
                    page.locator("#library-import-in-place").click()
                assert failed_info.value.status == 502
                page.wait_for_function(
                    "document.getElementById('library-import-execution-result').textContent.includes('retry is available')"
                )
                assert "retry is available" in page.locator("#library-import-execution-result").inner_text()
                assert page.locator("#library-import-in-place").inner_text() == "Import"
                assert page.locator("#library-import-modal").is_visible()
                expected_failure["kind"] = None
            with page.expect_response(lambda response: response.url.endswith("/execute")) as execute_info:
                page.locator("#library-import-in-place").click()
            assert execute_info.value.status in {200, 201}
            payload = execute_info.value.json()
            import_requests = [event for event in network[request_network_start:]
                               if "/api/library-import/albums/" in event["url"]]
            assert not any(event["url"].endswith("/preview") for event in import_requests)
            assert sum(event["url"].endswith("/execute") for event in import_requests) == (2 if mock_failure else 1)
            page.locator("#library-import-in-place", has_text="Close").wait_for()
            assert page.locator("#library-import-modal").is_visible()
            page.locator("#library-import-in-place").click()
            page.locator("#library-import-modal").wait_for(state="hidden")
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
        page.locator("#library-import-modal:not([hidden])").wait_for()
        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/execute")) as response_info:
            page.locator("#library-import-in-place").click()
        assert response_info.value.status in {200, 201}, response_info.value.status
        assert page.locator("#library-import-modal").is_visible()
        page.locator("#library-import-in-place", has_text="Close").wait_for()
        page.screenshot(path=evidence / "11-as-is-direct-success.png", full_page=True)
        assert page.locator("#library-import-in-place").inner_text() == "Close"
        page.locator("#library-import-in-place").click()
        page.locator("#library-import-modal").wait_for(state="hidden")
        browser.close()

    unexpected_console = assert_no_browser_errors(page_errors, console_errors, network)
    inbox_after = request_json(f"{base_url}/api/inbox")
    assert inbox_after == inbox_before and inbox_after, "full-library imports must not alter Inbox retention"
    return {
        "page_errors": page_errors, "console_errors": unexpected_console,
        "expected_failure_console": [message for message in console_errors if message not in unexpected_console],
        "network": network,
        "folder_page_timings_ms": folder_page_timings_ms,
        "inline_scan_state_proof": inline_scan_state_proof,
        "sequential_candidate_proof": sequential_proof,
        "ftintitle_config_proof": ftintitle_config_proof,
        "deluxe_edition_proof": deluxe_proof,
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
    args = parser.parse_args()
    args.fixture_root.mkdir(parents=True, exist_ok=True)
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    tracks = make_fixture(args.fixture_root)
    before = {name: sha256(path) for name, path in tracks.items()}
    host_identity_before = {
        name: {"path": str(path.resolve()), "device": path.stat().st_dev, "inode": path.stat().st_ino}
        for name, path in tracks.items()
    }
    app_library = Path(args.app_library).absolute() if args.app_library else (args.fixture_root / "library").resolve()
    configure(
        args.base_url.rstrip("/"),
        args.app_inbox or str((args.fixture_root / "inbox").resolve()),
        str(app_library),
    )
    browser_result = run_browser(args.base_url.rstrip("/"), args.state_path, args.evidence_dir, tracks)
    after = {name: sha256(path) for name, path in tracks.items()}
    assert before["as_is"] == after["as_is"], "as-is direct import changed source audio bytes"
    assert all(track.exists() for track in tracks.values()), "direct import moved or removed a source file"
    host_identity_after = {
        name: {"path": str(path.resolve()), "device": path.stat().st_dev, "inode": path.stat().st_ino}
        for name, path in tracks.items()
    }
    assert host_identity_after == host_identity_before, "direct import moved or replaced a host fixture file"
    items = request_json(f"{args.base_url.rstrip('/')}/api/items")
    excluded_queue = {"queue_tied", "queue_lower", "queue_error", "queue_unselected"}
    registered_names = set(tracks) - excluded_queue
    assert len(items) == len(registered_names)
    by_path = {Path(item["path"]).name: item for item in items}
    for name in registered_names:
        path = tracks[name]
        expected_app_path = app_library / TRACKS[name]
        reported_path = Path(by_path[path.name]["path"])
        assert reported_path.is_absolute()
        assert reported_path.is_relative_to(app_library)
        assert reported_path == expected_app_path

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
        "fixture_sha256_before": before,
        "fixture_sha256_after": after,
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
    manifest = [f"{sha256(path)}  {path.name}" for path in sorted(args.evidence_dir.iterdir()) if path.is_file()]
    (args.evidence_dir / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
