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

from mediafile import MediaFile
from playwright.sync_api import Page, sync_playwright


AS_IS_TRACK = Path("As Is Artist") / "As Is Album" / "01 As Is Song.wav"
CANDIDATE_TRACK = Path("Candidate Local Artist") / "Candidate Local Album" / "01 Local Song.wav"
CANDIDATE_RELEASE_ID = "123e4567-e89b-42d3-a456-426614174000"
CANDIDATE_RECORDING_ID = "223e4567-e89b-42d3-a456-426614174001"


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


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)


def make_fixture(root: Path) -> dict[str, Path]:
    (root / "inbox").mkdir(parents=True, exist_ok=True)
    tracks = {"as_is": root / "library" / AS_IS_TRACK, "candidate": root / "library" / CANDIDATE_TRACK}
    for track in tracks.values():
        write_wav(track)
    return tracks


def install_provider_fixture(state_path: Path) -> int:
    """Persist one deterministic mocked provider result for browser validation."""
    provider_data = {
        "provider_id": CANDIDATE_RELEASE_ID,
        "release_group_id": "323e4567-e89b-42d3-a456-426614174002",
        "artist": "Canonical Artist",
        "album": "Canonical Album",
        "genre": "Electronic",
        "date": "2024-03-02",
        "year": "2024",
        "release_type": "Album",
        "country": "GB",
        "track_count": 1,
        "media": [{"position": 1, "format": "CD", "track_count": 1}],
        "tracks": [{
            "title": "Canonical Song", "position": 1, "medium_position": 1,
            "recording_id": CANDIDATE_RECORDING_ID,
        }],
        "retrieval": {"search_score": None, "source": "deterministic-provider-fixture-mock"},
    }
    proposed_diff = {
        "artist": {"from": "Candidate Local Artist", "to": "Canonical Artist"},
        "album": {"from": "Candidate Local Album", "to": "Canonical Album"},
        "genre": {"from": None, "to": "Electronic"},
        "tracks": {"from": ["Local Song"], "to": ["Canonical Song"]},
    }
    with sqlite3.connect(state_path / "app.db", timeout=10) as database:
        album = database.execute(
            "SELECT id FROM album_reviews WHERE artist = ? AND album = ?",
            ("Candidate Local Artist", "Candidate Local Album"),
        ).fetchone()
        assert album is not None, "candidate fixture album was not inventoried"
        now = datetime.now(timezone.utc).isoformat()
        candidate_id = database.execute(
            """INSERT INTO metadata_candidates(
                   album_review_id, provider, provider_id, rank, confidence, artist, album,
                   year, proposed_diff_json, provider_data_json, created_at
               ) VALUES (?, 'musicbrainz', ?, 1, 1.0, ?, ?, ?, ?, ?, ?)""",
            (album[0], CANDIDATE_RELEASE_ID, "Canonical Artist", "Canonical Album", "2024",
             json.dumps(proposed_diff, sort_keys=True), json.dumps(provider_data, sort_keys=True), now),
        ).lastrowid
        database.execute(
            "UPDATE album_reviews SET candidate_status = 'complete', candidate_error = '', updated_at = ? WHERE id = ?",
            (now, album[0]),
        )
    return candidate_id


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def assert_no_browser_errors(page_errors: list[str], console_errors: list[str], network: list[dict]) -> None:
    failed_responses = [event for event in network if event.get("status", 0) >= 400]
    assert not page_errors, f"browser page errors: {page_errors}"
    assert not console_errors, f"browser console errors: {console_errors}"
    assert not failed_responses, f"failed API responses: {failed_responses}"


def run_browser(base_url: str, state_path: Path, evidence: Path) -> dict:
    page_errors: list[str] = []
    console_errors: list[str] = []
    network: list[dict] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page: Page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on(
            "response",
            lambda response: network.append({"method": response.request.method, "status": response.status, "url": response.url})
            if "/api/" in response.url
            else None,
        )

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
        assert "Files: 2" in page.locator("#inventory-summary").inner_text()
        assert not page.locator("#candidate-generate").is_disabled()

        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        assert "Files: 2" in page.locator("#inventory-summary").inner_text()
        assert page.locator("#candidate-generate").is_disabled(), "restored state must not unlock candidate lookup"
        page.screenshot(path=evidence / "03-inventory-restored.png", full_page=True)

        with page.expect_response(lambda response: response.url.endswith("/api/library/inventory/preview")) as response_info:
            page.locator("#inventory-preview").click()
        assert response_info.value.status == 201
        assert not page.locator("#candidate-generate").is_disabled()
        candidate_id = install_provider_fixture(state_path)
        page.reload(wait_until="networkidle")
        page.locator("#settings-tab-library-import").click()
        candidate_review = page.get_by_role("button", name=re.compile("Candidate Local Artist"))
        candidate_review.wait_for()
        candidate_review.click()
        page.locator("#library-import-modal:not([hidden])").wait_for()
        selected_candidate = page.get_by_role("radio", checked=True)
        assert "Canonical Artist — Canonical Album" in selected_candidate.get_attribute("aria-label")
        assert candidate_id > 0

        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/preview")) as response_info:
            page.locator("#library-import-in-place").click()
        assert response_info.value.status == 200, response_info.value.status
        page.locator("#library-import-confirmation:not([hidden])").wait_for()
        assert "1 registrations" in page.locator("#library-import-execution-result").inner_text()
        assert "1 metadata updates" in page.locator("#library-import-execution-result").inner_text()
        page.screenshot(path=evidence / "04-selected-candidate-preview.png", full_page=True)

        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/execute")) as response_info:
            page.locator("#library-import-execution-apply").click()
        assert response_info.value.status in {200, 201}, response_info.value.status
        page.locator("#library-import-modal").wait_for(state="hidden")

        as_is_review = page.get_by_role("button", name=re.compile("As Is Artist"))
        as_is_review.wait_for()
        as_is_review.click()
        page.locator("#library-import-modal:not([hidden])").wait_for()
        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/preview")) as response_info:
            page.locator("#library-import-in-place").click()
        assert response_info.value.status == 200, response_info.value.status
        page.locator("#library-import-confirmation:not([hidden])").wait_for()
        page.screenshot(path=evidence / "05-as-is-preview.png", full_page=True)
        with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.url.endswith("/execute")) as response_info:
            page.locator("#library-import-execution-apply").click()
        assert response_info.value.status in {200, 201}, response_info.value.status
        page.locator("#library-import-modal").wait_for(state="hidden")
        assert page.locator(".review-album").count() == 0
        browser.close()

    assert_no_browser_errors(page_errors, console_errors, network)
    return {"page_errors": page_errors, "console_errors": console_errors, "network": network}


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
    configure(
        args.base_url.rstrip("/"),
        args.app_inbox or str((args.fixture_root / "inbox").resolve()),
        args.app_library or str((args.fixture_root / "library").resolve()),
    )
    browser_result = run_browser(args.base_url.rstrip("/"), args.state_path, args.evidence_dir)
    after = {name: sha256(path) for name, path in tracks.items()}
    assert before["as_is"] == after["as_is"], "as-is direct import changed source audio bytes"
    assert all(track.exists() for track in tracks.values()), "direct import moved or removed a source file"
    items = request_json(f"{args.base_url.rstrip('/')}/api/items")
    assert len(items) == 2
    by_path = {Path(item["path"]).name: item for item in items}
    candidate_item = by_path[CANDIDATE_TRACK.name]
    assert Path(candidate_item["path"]).as_posix().endswith(CANDIDATE_TRACK.as_posix())
    assert {field: candidate_item[field] for field in ("title", "artist", "album", "genre", "year", "track")} == {
        "title": "Canonical Song", "artist": "Canonical Artist", "album": "Canonical Album",
        "genre": "Electronic", "year": 2024, "track": 1,
    }
    tags = MediaFile(str(tracks["candidate"]))
    assert {field: getattr(tags, field) for field in ("title", "artist", "album", "year", "track")} == {
        "title": "Canonical Song", "artist": "Canonical Artist", "album": "Canonical Album",
        "year": 2024, "track": 1,
    }
    assert tags.mb_albumid == CANDIDATE_RELEASE_ID
    assert tags.mb_trackid == CANDIDATE_RECORDING_ID
    result = {
        "fixture_sha256_before": before,
        "fixture_sha256_after": after,
        "registered_paths": sorted(item["path"] for item in items),
        "provider_mock": {
            "kind": "persisted deterministic MusicBrainz candidate fixture",
            "release_id": CANDIDATE_RELEASE_ID,
            "network_used": False,
        },
        "selected_candidate_tags": {
            "title": tags.title, "artist": tags.artist, "album": tags.album,
            "year": tags.year, "track": tags.track, "mb_albumid": tags.mb_albumid, "mb_trackid": tags.mb_trackid,
        },
        "selected_candidate_database_genre": candidate_item["genre"],
        **browser_result,
    }
    results_path = args.evidence_dir / "runtime-results.json"
    results_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{sha256(path)}  {path.name}" for path in sorted(args.evidence_dir.iterdir()) if path.is_file()]
    (args.evidence_dir / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
