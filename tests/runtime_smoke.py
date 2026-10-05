#!/usr/bin/env python3
"""Real-browser smoke test for Settings and direct in-place library import."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
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
}
GENRES = {"case": "Alternative Rock", "sidecar": "Rock", "embed": "Jazz", "preserve": "Blues", "replace": "Folk", "missing": "Ambient"}
CASE_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174099"


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
            tags.save()
    for number in range(50):
        pagination = root / "library" / f"ZZ Pagination Artist {number // 4:02d}" / f"Album {number:02d}" / "01 Track.wav"
        write_wav(pagination)
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
                {"title": "Extra", "position": 8, "medium_position": 1, "length_ms": 287000},
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
             json.dumps({"tracks": {"from": ["Used 2", "Extra feat. Rich Homie Quan", "U Da Realest", "Mainstream Ratchet"],
                                            "to": ["Used 2", "Extra", "U Da Realest", "Mainstream Ratchet"]}}, sort_keys=True),
             json.dumps(feature_data, sort_keys=True), now),
        ).lastrowid
        database.execute(
            "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
            (now, feature_album[0]),
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
    if any(event.get("expected") and event.get("status") == 502 for event in network):
        console_errors = [message for message in console_errors if "status of 502" not in message]
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


def run_browser(base_url: str, state_path: Path, evidence: Path) -> dict:
    page_errors: list[str] = []
    console_errors: list[str] = []
    network: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page: Page = browser.new_page(viewport={"width": 1440, "height": 1000})
        expected_execute_failure = {"pending": False}
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on(
            "response",
            lambda response: network.append({"method": response.request.method, "status": response.status, "url": response.url,
                                              "expected": response.status == 502 and (
                                                  expected_execute_failure["pending"]
                                              )})
            if "/api/" in response.url
            else None,
        )
        page.route("https://coverartarchive.org/**", lambda route: route.fulfill(
            status=200, content_type="image/jpeg", body=ARTWORK_JPEG,
        ))

        page.goto(f"{base_url}/settings", wait_until="networkidle")
        assert page.evaluate("sessionStorage.length") == 0
        assert not page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        page.screenshot(path=evidence / "01-settings-initial.png", full_page=True)

        page.locator("#theme-toggle").click()
        assert page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        assert page.evaluate("localStorage.getItem('cratekeep-theme')") == "dark"
        page.reload(wait_until="networkidle")
        assert page.locator("body").evaluate("element => element.classList.contains('dark-mode')")
        page.screenshot(path=evidence / "02-settings-dark-refresh.png", full_page=True)

        page.locator("#settings-tab-library-import").click()
        assert page.locator("#candidate-generate").is_disabled()
        with page.expect_response(lambda response: response.url.endswith("/api/library/inventory/preview")) as response_info:
            page.locator("#inventory-preview").click()
        assert response_info.value.status == 201
        page.locator("#inventory-status").get_by_text("complete", exact=False).wait_for()
        assert "Files: 61" in page.locator("#inventory-summary").inner_text()
        assert not page.locator("#candidate-generate").is_disabled()

        page.locator("#library-import-review-page").get_by_text("Albums 1–25 of 58", exact=True).wait_for()
        seen_album_labels = []
        for page_number, expected_count in enumerate((25, 25, 8), 1):
            assert page.locator(".review-album-row").count() == expected_count
            seen_album_labels.extend(page.locator(".review-album").evaluate_all(
                "elements => elements.map(element => element.getAttribute('aria-label'))"
            ))
            review_box = page.locator("#library-import-review-list").evaluate(
                "element => ({clientHeight: element.clientHeight, scrollHeight: element.scrollHeight})"
            )
            assert review_box["clientHeight"] == review_box["scrollHeight"], "review list must not create an inner scroll"
            page.screenshot(path=evidence / f"pagination-{page_number}-desktop.png", full_page=True)
            if page_number < 3:
                page.locator("#library-import-review-next").click()
                page.locator("#library-import-review-page").get_by_text(
                    f"Albums {page_number * 25 + 1}–{min((page_number + 1) * 25, 58)} of 58", exact=True
                ).wait_for()
        assert len(seen_album_labels) == len(set(seen_album_labels)) == 58
        assert page.locator("#library-import-review-next").is_disabled()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator(".review-album-row").count() == 8
        page.screenshot(path=evidence / "pagination-3-narrow.png", full_page=True)
        page.locator("#library-import-review-previous").click()
        page.locator("#library-import-review-page").get_by_text("Albums 26–50 of 58", exact=True).wait_for()
        page.locator("#library-import-review-previous").click()
        page.locator("#library-import-review-page").get_by_text("Albums 1–25 of 58", exact=True).wait_for()
        assert page.locator("#library-import-review-previous").is_disabled()
        page.set_viewport_size({"width": 1440, "height": 1000})

        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        assert "Files: 61" in page.locator("#inventory-summary").inner_text()
        assert page.locator("#candidate-generate").is_disabled(), "restored state must not unlock candidate lookup"
        page.screenshot(path=evidence / "03-inventory-restored.png", full_page=True)

        with page.expect_response(lambda response: response.url.endswith("/api/library/inventory/preview")) as response_info:
            page.locator("#inventory-preview").click()
        assert response_info.value.status == 201
        assert not page.locator("#candidate-generate").is_disabled()
        candidate_ids = install_provider_fixtures(state_path)
        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        candidate_review = page.get_by_role("button", name=re.compile("Sidecar Artist"))
        candidate_review.wait_for()
        candidate_review.click()
        page.locator("#library-import-modal:not([hidden])").wait_for()
        selected_candidate = page.get_by_role("radio", checked=True)
        assert "Sidecar Artist — Sidecar Album" in selected_candidate.get_attribute("aria-label")
        assert candidate_ids["sidecar"] > 0
        assert page.locator(".candidate-artwork").count() == 1

        # Candidate, duplicate, rematch, and preview state share one guarded close path.
        page.get_by_role("radio", name=re.compile("As Is")).click()
        page.once("dialog", lambda dialog: dialog.dismiss())
        page.locator("#library-import-modal").click(position={"x": 2, "y": 2})
        assert page.locator("#library-import-modal").is_visible()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(".album-modal-dialog").focus()
        page.keyboard.press("Escape")
        page.locator("#library-import-modal").wait_for(state="hidden")
        assert candidate_review.evaluate("element => document.activeElement === element")
        candidate_review.click()
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
            review = page.get_by_role("button", name=re.compile(re.escape(TRACKS[name].parts[0])))
            review.wait_for(); review.click()
            page.locator("#library-import-modal:not([hidden])").wait_for()
            candidate = page.get_by_role("radio", name=re.compile(re.escape(TRACKS[name].parts[0] + " — "))).first
            if candidate.get_attribute("aria-checked") != "true":
                candidate.click()
            if has_art:
                assert page.locator(".candidate-artwork").count() == 1
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
                row_text = rows.inner_text()
                assert "Extra feat. Rich Homie Quan" in row_text and "Extra" in row_text
                assert "UNMATCHED" not in row_text
                assert all(title not in row_text for title in ("Used 2", "U Da Realest", "Mainstream Ratchet"))
                page.screenshot(path=evidence / screenshot)
                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=evidence / "04-feature-track-changes-narrow-dark.png")
                assert page.locator(".track-modal-dialog").evaluate(
                    "element => element.scrollWidth <= element.clientWidth"
                ), "narrow track changes must not overflow its dialog"
                page.set_viewport_size({"width": 1440, "height": 1000})
                page.locator("#track-comparison-close").click()
            else:
                page.screenshot(path=evidence / screenshot)
            request_network_start = len(network)
            if mock_failure:
                execute_pattern = re.compile(r".*/api/library-import/albums/\d+/execute$")
                expected_execute_failure["pending"] = True
                page.route(execute_pattern, lambda route: route.fulfill(
                    status=502, content_type="application/json",
                    body=json.dumps({"error": "Deterministic mocked artwork failure; retry is available.",
                                     "code": "artwork_fetch_failed"}),
                ), times=1)
                with page.expect_response(lambda response: response.url.endswith("/execute")) as failed_info:
                    page.locator("#library-import-in-place").click()
                assert failed_info.value.status == 502
                assert "retry is available" in page.locator("#library-import-execution-result").inner_text()
                assert page.locator("#library-import-in-place").inner_text() == "Import"
                assert page.locator("#library-import-modal").is_visible()
                expected_execute_failure["pending"] = False
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
        assert extra_item["changes"]["title"] == {"from": "Extra feat. Rich Homie Quan", "to": "Extra"}

        as_is_review = page.get_by_role("button", name=re.compile("As Is Artist"))
        as_is_review.wait_for()
        as_is_review.click()
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
        assert page.locator(".review-album").count() == 25
        browser.close()

    unexpected_console = assert_no_browser_errors(page_errors, console_errors, network)
    return {
        "page_errors": page_errors, "console_errors": unexpected_console,
        "expected_failure_console": [message for message in console_errors if message not in unexpected_console],
        "network": network,
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
    browser_result = run_browser(args.base_url.rstrip("/"), args.state_path, args.evidence_dir)
    after = {name: sha256(path) for name, path in tracks.items()}
    assert before["as_is"] == after["as_is"], "as-is direct import changed source audio bytes"
    assert all(track.exists() for track in tracks.values()), "direct import moved or removed a source file"
    host_identity_after = {
        name: {"path": str(path.resolve()), "device": path.stat().st_dev, "inode": path.stat().st_ino}
        for name, path in tracks.items()
    }
    assert host_identity_after == host_identity_before, "direct import moved or replaced a host fixture file"
    items = request_json(f"{args.base_url.rstrip('/')}/api/items")
    assert len(items) == len(tracks)
    by_path = {Path(item["path"]).name: item for item in items}
    for name, path in tracks.items():
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
        **browser_result,
    }
    results_path = args.evidence_dir / "runtime-results.json"
    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{sha256(path)}  {path.name}" for path in sorted(args.evidence_dir.iterdir()) if path.is_file()]
    (args.evidence_dir / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
