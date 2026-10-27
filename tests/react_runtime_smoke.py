"""Browser smoke for the React single-cutover UI.

The smoke uses disposable WAV fixtures, a deterministic provider, and one
confirmed Library adoption. It never contacts a real metadata provider or
touches non-fixture media.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from mediafile import MediaFile
from playwright.sync_api import sync_playwright

from runtime_app import EXISTING_ARTWORK_JPEG


def write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)


def post_form(url: str, values: dict) -> None:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(values, doseq=True).encode())
    urllib.request.urlopen(request, timeout=20).read()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def match_selected(page, endpoint, evidence_dir, *, cooldown=False, expected_matches=1):
    page.get_by_role("button", name="Match Selected", exact=True).click()
    confirmation=page.get_by_role("dialog",name="Match selected albums",exact=True)
    confirmation.wait_for()
    assert confirmation.evaluate("node => node.matches(':modal')")
    with page.expect_response(lambda response: response.url.endswith(endpoint)) as matched:
        confirmation.get_by_role("button",name="Start matching",exact=True).click()
    if cooldown:
        page.get_by_text("Fixture MusicBrainz cooldown",exact=True).wait_for()
        page.screenshot(path=evidence_dir/"06-matching-cooldown.png",full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        page.screenshot(path=evidence_dir/"06-matching-cooldown-mobile.png",full_page=True)
        page.set_viewport_size({"width":1440,"height":1000})
    results=page.get_by_role("dialog",name="Matching results",exact=True)
    results.wait_for(timeout=60000)
    matched_rows=results.get_by_text("Matched",exact=True)
    assert matched_rows.count()==expected_matches
    assert all(row.is_visible() for row in matched_rows.all())
    page.screenshot(path=evidence_dir/("06-inbox-matching-results.png" if cooldown else "06-library-matching-results.png"),full_page=True)
    results.get_by_role("button",name="Close results",exact=True).click()
    return matched.value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--app-inbox", required=True)
    parser.add_argument("--app-library", required=True)
    parser.add_argument("--state-path", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--source-set-definition", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    inbox_track = args.fixture_root / "inbox" / "Inbox Ready" / "01 Song.wav"
    library_track = args.fixture_root / "library" / "Library Ready" / "01 Song.wav"
    library_bulk_track = args.fixture_root / "library" / "Bulk Ready" / "01 Song.wav"
    write_wav(inbox_track); write_wav(library_track); write_wav(library_bulk_track)
    inbox_untagged = inbox_track.parent / "02 Untagged.wav"
    write_wav(inbox_untagged)
    tagged = MediaFile(str(inbox_track))
    tagged.artist = tagged.albumartist = "Fixture Artist"
    tagged.album = "Inbox Ready"
    tagged.title = "Song"
    tagged.save()
    for index in range(25):
        for source in ('inbox', 'library'):
            path = args.fixture_root / source / f"A Scope {index:02d}" / "Song.wav"
            write_wav(path)
            tags = MediaFile(str(path)); tags.artist = tags.albumartist = "Paging Artist"
            tags.album = f"A Scope {index:02d}"; tags.title = "Song"; tags.save()
    before = {"inbox_untagged": sha256(inbox_untagged), "inbox": sha256(inbox_track), "library": sha256(library_track),
              "library_bulk": sha256(library_bulk_track)}
    post_form(f"{base}/setup", {"inbox_path": args.app_inbox, "library_path": args.app_library, "match_threshold": "90"})

    page_errors: list[str] = []
    console_errors: list[str] = []
    route_results = {}
    with sync_playwright() as playwright:
        executable = os.environ.get("CHROMIUM_EXECUTABLE")
        browser = playwright.chromium.launch(executable_path=executable) if executable else playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        for index, (path, heading) in enumerate((("/", "Library overview"), ("/inbox", "Incoming music"),
                                                  ("/library-import", "Adoption queue"), ("/library", "Managed collection"),
                                                  ("/settings", "Cratekeep settings")), 1):
            page.goto(base + path, wait_until="networkidle")
            page.get_by_role("heading", name=heading).wait_for()
            asset_build = page.evaluate("window.__CRATEKEEP_BOOTSTRAP__.buildSha")
            assert page.locator('script[type="module"]').get_attribute('src') == f"/static/ui/cratekeep.js?v={asset_build}"
            assert page.locator('link[rel="stylesheet"]').get_attribute('href') == f"/static/ui/cratekeep.css?v={asset_build}"
            assert page.get_by_role("navigation", name="Primary navigation").get_by_role("link").all_inner_texts() == ["Overview", "Inbox", "Library", "Settings"]
            assert page.evaluate("document.documentElement.scrollHeight <= innerHeight"), f"document scrolls on {path}"
            desktop_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
            desktop_fill = page.locator(".app-content").evaluate("node => document.body.getBoundingClientRect().height >= innerHeight")
            page.set_viewport_size({"width": 390, "height": 844})
            page.wait_for_function("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            mobile_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
            mobile_fill = page.locator(".app-content").evaluate("node => document.body.getBoundingClientRect().height >= innerHeight")
            page.screenshot(path=args.evidence_dir / f"{index:02d}-{path.strip('/') or 'overview'}-mobile.png", full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.wait_for_function("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
            route_results[path] = {"heading": heading, "desktop_no_overflow": desktop_ok, "mobile_no_overflow": mobile_ok,
                                   "desktop_theme_fill": desktop_fill, "mobile_theme_fill": mobile_fill}
            assert desktop_ok and mobile_ok, f"horizontal overflow on {path}"
            assert desktop_fill and mobile_fill, f"viewport theme does not fill {path}"
            if path in ("/inbox", "/library-import", "/library"):
                divider = page.get_by_role("separator", name="Resize album details")
                page.wait_for_function("Number(document.querySelector('.inspector-divider').getAttribute('aria-valuemax')) >= 430")
                divider.focus()
                divider.press("Home")
                divider.press("ArrowLeft")
                assert divider.get_attribute("aria-valuenow") == "430"
                divider.press("Home")

        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("button", name="Help: Inboxes").wait_for()
        page.get_by_role("table", name="Configured Inboxes").wait_for()
        page.screenshot(path=args.evidence_dir / "05-settings-desktop.png", full_page=True)
        assert page.get_by_role("heading", name="Navidrome").count() == 0
        page.get_by_role("button", name="Help: Inboxes").click()
        assert "does not delete files" in page.get_by_role("note").inner_text()
        page.get_by_role("button", name="Help: Inboxes").press("Escape")
        assert page.get_by_role("note").count() == 0
        assert page.get_by_label("Name for Inbox", exact=True).count() == 0
        page.get_by_role("button", name="Edit Inbox", exact=True).click()
        assert page.get_by_role("button", name="Save settings").is_disabled()
        page.get_by_label("Name for Inbox", exact=True).fill("Temporary edit")
        page.get_by_role("button", name="Cancel", exact=True).click()
        page.get_by_role("button", name="Edit Inbox", exact=True).click()
        page.get_by_role("button", name="Done", exact=True).click()
        assert page.get_by_role("button", name="Save settings").is_enabled()
        assert page.get_by_label("Toast duration (seconds)").input_value() == "5"
        page.get_by_label("Toast duration (seconds)").fill("3")
        page.get_by_role("tab", name="Metadata").click()
        assert page.get_by_label("Matched threshold (%)").bounding_box()["width"] <= 150
        artwork_box = page.get_by_role("heading", name="Artwork", exact=False).locator("..").bounding_box()
        feature_box = page.get_by_role("heading", name="Feature in Title", exact=False).locator("..").bounding_box()
        assert abs(feature_box["x"] - artwork_box["x"]) < 1
        assert 12 <= feature_box["y"] - artwork_box["y"] - artwork_box["height"] <= 20
        page.screenshot(path=args.evidence_dir / "05-settings-metadata-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=args.evidence_dir / "05-settings-metadata-mobile.png", full_page=True)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.get_by_label("Matched threshold (%)").fill("50")
        page.get_by_role("tab", name="General").click()
        summary_row = page.get_by_role("button", name="Edit Inbox", exact=True).locator("xpath=ancestor::tr")
        assert summary_row.get_by_role("combobox").count() == 0
        assert summary_row.get_by_role("checkbox").count() == 0
        page.get_by_role("button", name="Edit Inbox", exact=True).click()
        match_inbox = page.get_by_role("checkbox", name="Automatically find matches for Inbox", exact=True)
        assert match_inbox.locator("xpath=ancestor::tr").get_by_role("combobox", name="Discovery for Inbox", exact=True).count() == 1
        match_inbox.check()
        assert match_inbox.is_checked()
        match_inbox.uncheck()
        assert not match_inbox.is_checked()
        page.get_by_role("button", name="Done", exact=True).click()
        page.get_by_role("tab", name="Metadata").click()
        page.get_by_label("Fetch Cover Art Archive artwork").check()
        page.get_by_label("Save cover.jpg sidecars").check()
        with page.expect_navigation(wait_until="networkidle"):
            page.get_by_role("button", name="Save settings").click()
        page.goto(base + "/inbox", wait_until="networkidle")
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.mouse.move(0, 0)
        refresh_toast = page.get_by_role("region", name="Notifications").get_by_role("status").first
        refresh_toast.wait_for()
        refresh_toast.wait_for(state="hidden", timeout=6000)
        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("tab", name="General").click()
        assert page.get_by_label("Toast duration (seconds)").input_value() == "3"
        page.get_by_label("Toast duration (seconds)").fill("5")
        with page.expect_navigation(wait_until="networkidle"):
            page.get_by_role("button", name="Save settings").click()
        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("tab", name="Metadata").click()
        assert page.get_by_label("Fetch Cover Art Archive artwork").is_checked()
        assert page.get_by_label("Save cover.jpg sidecars").is_checked()
        page.get_by_role("tab", name="Metadata").click()
        assert page.get_by_label("Matched threshold (%)").input_value() == "50"
        page.get_by_role("tab", name="General").click()
        assert page.get_by_text("Auto-match: Off", exact=True).count() == 1
        assert page.get_by_role("table", name="Configured Inboxes").get_by_role("checkbox").count() == 0

        page.goto(base + "/inbox", wait_until="networkidle")
        assert page.get_by_role("button", name="Match Selected").count() == 0
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.get_by_text(re.compile("Page 1 of 2")).wait_for()
        # The queue scrolls inside the viewport, independently of the inspector.
        queue = page.locator(".desk-albums")
        assert queue.evaluate("node => node.scrollHeight > node.clientHeight")
        queue.evaluate("node => node.scrollTop = 150")
        assert queue.evaluate("node => node.scrollTop > 0")
        assert page.locator(".app-content").evaluate("node => node.scrollTop") == 0
        assert page.evaluate("window.scrollY") == 0
        queue.evaluate("node => node.scrollTop = 0")
        page.get_by_role("checkbox", name="Select album A Scope 00", exact=True).check()
        page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_text(re.compile("Page 2 of 2")).wait_for()
        assert page.get_by_text("1 selected", exact=True).count() == 0
        page.get_by_role("button", name="Previous", exact=True).click()
        page.get_by_text(re.compile("Page 1 of 2")).wait_for()
        assert not page.get_by_role("checkbox", name="Select album A Scope 00", exact=True).is_checked()
        page.get_by_role("checkbox", name="Select album A Scope 00", exact=True).uncheck()
        page.get_by_role("searchbox", name="Find an album").fill("Inbox Ready")
        page.get_by_role("checkbox", name="Select album Inbox Ready", exact=True).check()
        inbox_scan=match_selected(page,"/api/inbox/candidates",args.evidence_dir,cooldown=True)
        assert inbox_scan.ok
        assert set(inbox_scan.request.post_data_json) == {"album_id","reuse_current"}
        toast = page.get_by_role("region", name="Notifications").get_by_role("status").filter(has_text="Matching finished")
        toast.wait_for()
        assert page.locator(".toast-region").evaluate("node => getComputedStyle(node).position") == "fixed"
        bounds = toast.bounding_box()
        assert bounds and bounds["x"] > 900 and bounds["y"] > 750
        page.screenshot(path=args.evidence_dir / "06a-success-toast-desktop.png", full_page=True)
        page.set_viewport_size({"width":390, "height":844})
        assert page.locator(".toast-region").evaluate("node => node.getBoundingClientRect().right <= innerWidth")
        toast.get_by_role("button", name="Dismiss notification").click()
        page.set_viewport_size({"width":1440,"height":1000})
        inbox_album = page.locator(".desk-album").filter(has_text="Inbox Ready")
        assert inbox_album.locator(".desk-track-count").inner_text() == "2"
        inbox_album.get_by_role("button", name="Review Inbox Ready", exact=True).click()
        assert page.get_by_role("complementary", name="Album inspector").is_visible()
        assert page.locator("[data-review-modal]").count() == 1
        page.get_by_role("searchbox", name="Find an album").fill("")

        # Previously inventoried empty scopes are hidden. Returning audio must
        # become visible through the actual Refresh button without matching.
        returned = args.fixture_root / "inbox" / "Returned Refresh" / "Album"
        returned.mkdir(parents=True)
        inbox_base = base + "/api/inbox/" + page.request.get(base + "/api/inbox/reviews").json()["inboxes"][0]["id"]
        hidden_scan = page.request.post(inbox_base + "/scopes/scan", data={"scopes": ["Returned Refresh"]})
        assert hidden_scan.ok
        for index in range(25):
            (args.fixture_root / "inbox" / f"Empty Refresh {index:02d}").mkdir()
        returned_paths = [returned / "01 First.wav", returned / "02 Untagged.wav"]
        for path in returned_paths:
            write_wav(path)
        tagged_return = MediaFile(str(returned_paths[0]))
        tagged_return.artist = tagged_return.albumartist = "Fixture Artist"
        tagged_return.album = "Returned Album"
        tagged_return.title = "First"
        tagged_return.save()
        returned_before = [sha256(path) for path in returned_paths]
        with page.expect_response(lambda response: response.url.endswith("/scopes/refresh")) as disk_refresh:
            page.get_by_role("button", name="Refresh", exact=True).click()
        assert disk_refresh.value.ok, disk_refresh.value.json()
        assert disk_refresh.value.request.method == "POST"
        assert len(disk_refresh.value.json()["cleanup"]["removed"]) >= 25
        page.get_by_role("searchbox", name="Find an album").fill("Returned Album")
        returned_album = page.locator(".desk-album").filter(has_text="Returned Album")
        returned_album.wait_for()
        assert returned_album.count() == 1 and returned_album.locator(".desk-track-count").inner_text() == "2"
        assert returned_album.locator(".desk-decision").inner_text() == "Pending"
        assert returned_before == [sha256(path) for path in returned_paths]
        assert not (args.fixture_root / "inbox" / "Empty Refresh 00").exists()
        page.screenshot(path=args.evidence_dir / "06c-refreshed-returned-inbox.png", full_page=True)

        page.goto(base + "/library-import", wait_until="networkidle")
        page.get_by_role("button", name="Refresh", exact=True).click()
        page.get_by_text(re.compile("Page 1 of 2")).wait_for()
        page.get_by_role("checkbox", name="Select album A Scope 00", exact=True).check()
        page.get_by_role("button", name="Next", exact=True).click()
        page.get_by_text(re.compile("Page 2 of 2")).wait_for()
        assert page.get_by_text("1 selected", exact=True).count() == 0
        page.get_by_role("button", name="Previous", exact=True).click()
        page.get_by_text(re.compile("Page 1 of 2")).wait_for()
        page.get_by_role("checkbox", name="Select album A Scope 00", exact=True).uncheck()
        page.get_by_role("searchbox", name="Find an album").fill("Library Ready")
        page.get_by_role("checkbox", name="Select album Library Ready", exact=True).check()
        library_scan=match_selected(page,"/api/library-import/candidates",args.evidence_dir)
        assert library_scan.ok
        album = page.locator(".desk-album").filter(has_text="Library Ready")
        album.get_by_role("button", name="Review Library Ready", exact=True).click()
        page.screenshot(path=args.evidence_dir / "06-desk-inspector-light.png", full_page=True)
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.screenshot(path=args.evidence_dir / "06-desk-inspector-dark.png", full_page=True)
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.locator("[data-review-modal]").wait_for()
        assert page.locator("[data-review-candidates]").is_visible()
        assert page.get_by_role("heading", name="Current metadata").count() == 0
        assert page.get_by_role("button", name="Match", exact=True).is_visible()
        page.get_by_role("button", name="Search MBID").click()
        page.get_by_label("MusicBrainz release UUID").fill("not-a-uuid")
        page.get_by_role("button", name="Search exact release").click()
        assert "canonical MusicBrainz release UUID" in page.get_by_role("alert").inner_text()
        page.get_by_label("MusicBrainz release UUID").fill("123e4567-e89b-42d3-a456-426614174002")
        with page.expect_response(lambda response: response.url.endswith("/rematch")) as mbid_match:
            page.get_by_role("button", name="Search exact release").click()
        assert mbid_match.value.ok
        mbid_payload = mbid_match.value.json()
        assert any(candidate.get("artwork_url") for candidate in mbid_payload.get("candidates", [])), mbid_payload
        candidate_choice = page.locator("[data-review-candidates] input[type=radio]").first
        if not candidate_choice.is_checked():
            with page.expect_response(lambda response: "/api/library-import/albums/" in response.url and response.request.method == "PATCH") as candidate_selection:
                candidate_choice.click()
            assert candidate_selection.value.ok, candidate_selection.value.json()
        page.locator(".candidate-artwork").first.wait_for()
        page.get_by_text("Release metadata and ID", exact=True).first.click()
        assert page.get_by_text("123e4567-e89b-42d3-a456-426614174002").is_visible()
        assert page.locator(".candidate-fact").count() >= 8
        changed_icon = page.locator(".candidate-fact .field-icon.changed").first
        assert changed_icon.is_visible()
        assert changed_icon.evaluate("node => getComputedStyle(node).backgroundColor") not in ("transparent", "rgba(0, 0, 0, 0)")
        page.get_by_text(re.compile(r"Compare tracks \(\d+\)")).first.click()
        assert page.locator(".track-evidence-row").count() > 0
        table = page.get_by_role("table", name="Current and proposed track metadata")
        assert table.get_by_role("columnheader", name="Current title").count() == 1
        assert table.get_by_role("columnheader", name="Proposed title").count() == 1
        footer = page.locator("[data-review-dialog]>footer")
        footer_before = footer.bounding_box()
        page.locator(".embedded-review .dialog-body").evaluate("node => node.scrollTop = node.scrollHeight")
        assert footer.bounding_box() == footer_before
        for button in footer.get_by_role("button").all():
            bounds = button.bounding_box()
            assert bounds and 0 <= bounds["y"] and bounds["y"] + bounds["height"] <= page.viewport_size["height"]
        assert page.locator(".track-evidence-row").get_by_role("cell").first.is_visible()
        assert page.locator(".track-evidence-row").get_by_role("img", name=re.compile(r"Metadata (changed|unchanged)")).first.is_visible()
        assert page.locator(".candidate-fact").get_by_role("img", name=re.compile(r"Artist (changed|unchanged)")).is_visible()
        assert page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        coverage = page.locator(".review-track-coverage")
        assert "On disk / MusicBrainz" in coverage.inner_text()
        assert "Track matches" in coverage.inner_text() and " / " in coverage.inner_text()
        assert "Release tracks present" not in coverage.inner_text()
        page.screenshot(path=args.evidence_dir / "06-review-modal-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_function("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.screenshot(path=args.evidence_dir / "07-review-modal-narrow.png", full_page=True)
        narrow_modal_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        if not narrow_modal_ok:
            offenders = page.locator("body").evaluate("""node => [...node.querySelectorAll('*')]
              .filter(item => item.getBoundingClientRect().right > innerWidth + 1)
              .map(item => ({tag: item.tagName, className: item.className, right: item.getBoundingClientRect().right, scrollWidth: item.scrollWidth}))
              .slice(0, 12)""")
            raise AssertionError({"narrow_modal_overflow": offenders})
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.wait_for_function("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="detached")
        page.get_by_role("button", name="Clear selection", exact=True).click()
        page.get_by_role("searchbox", name="Find an album").fill("Bulk Ready")
        page.get_by_role("checkbox", name="Select album Bulk Ready", exact=True).check()
        match_selected(page,"/api/library-import/candidates",args.evidence_dir)
        page.get_by_role("button", name="Perfect match", exact=True).click()
        page.get_by_role("checkbox", name="Select album Bulk Ready", exact=True).wait_for()
        page.get_by_role("button", name="Duplicates", exact=True).click()
        page.get_by_text("No albums match this view", exact=True).wait_for()
        page.get_by_role("button", name="All albums", exact=True).click()
        bulk_checkbox = page.get_by_role("checkbox", name="Select album Bulk Ready", exact=True)
        bulk_checkbox.wait_for()
        assert not bulk_checkbox.is_checked()
        assert page.get_by_text("1 selected", exact=True).count() == 0
        bulk_checkbox.check()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/preview")) as bulk_preview:
            page.locator(".toolbar-actions").get_by_role("button", name="Import selected", exact=True).click()
        confirmation = page.get_by_role("dialog", name="Import selected albums", exact=True)
        confirmation.wait_for()
        assert confirmation.evaluate("node => node.matches(':modal')")
        page.screenshot(path=args.evidence_dir / "08-bulk-confirmation.png", full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        assert page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        page.screenshot(path=args.evidence_dir / "08-bulk-confirmation-mobile.png", full_page=True)
        page.set_viewport_size({"width":1440,"height":1000})
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/execute")) as bulk_import:
            confirmation.get_by_role("button", name="Start import", exact=True).click()
        results = page.get_by_role("dialog", name="Import results", exact=True)
        results.wait_for()
        assert results.get_by_text("Imported", exact=True).is_visible()
        page.screenshot(path=args.evidence_dir / "08-bulk-results.png", full_page=True)
        results.get_by_role("button", name="Close results", exact=True).click()
        results.wait_for(state="detached")
        preview_payload = bulk_preview.value.json()
        assert preview_payload.get("importable_album_count") == 1, preview_payload
        bulk_payload = bulk_import.value.json()
        assert len(bulk_payload.get("imported", [])) == 1, bulk_payload
        page.get_by_text(re.compile(r"1 albums? imported")).wait_for()

        page.goto(base + "/", wait_until="networkidle")
        assert page.locator(".metric-grid article").filter(has_text="Library size").count() == 1
        assert page.locator(".metric-grid article").filter(has_text="Inbox size").count() == 0
        assert page.get_by_role("region", name="Inbox status").get_by_role("heading", name="Inbox size").is_visible()
        assert page.locator(".inbox-source-card").count() == 1
        page.screenshot(path=args.evidence_dir / "08-overview-cards.png", full_page=True)
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=args.evidence_dir / "08-overview-cards-dark-mobile.png", full_page=True)
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.set_viewport_size({"width":1440,"height":1000})
        page.goto(base + "/library", wait_until="networkidle")
        page.get_by_role("link", name="Artists", exact=True).click()
        page.get_by_role("heading", name="Browse artists").wait_for()
        artist_row = page.locator(".artist-row").filter(has_text="Unknown artist")
        assert artist_row.count() == 1
        page.screenshot(path=args.evidence_dir / "08-artists.png", full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=args.evidence_dir / "08-artists-mobile.png", full_page=True)
        artist_row.click()
        page.get_by_role("heading", name="Browse albums").wait_for()
        assert page.get_by_role("link", name="All artists", exact=True).is_visible()
        page.set_viewport_size({"width":1440,"height":1000})
        managed_album = page.locator(".album-card").filter(has_text="Bulk Ready")
        managed_album.wait_for()
        page.screenshot(path=args.evidence_dir / "08-collection-ledger.png", full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=args.evidence_dir / "08-collection-ledger-mobile.png", full_page=True)
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.screenshot(path=args.evidence_dir / "08-collection-ledger-dark-mobile.png", full_page=True)
        managed_album.click()
        page.get_by_role("region", name="Bulk Ready", exact=True).wait_for()
        assert page.get_by_role("dialog").count() == 0
        assert not page.locator(".collection-ledger").is_visible()
        assert page.get_by_role("button", name="Back to albums", exact=True).is_visible()
        assert page.locator(".library-record[aria-pressed=true]").count() == 1
        page.screenshot(path=args.evidence_dir / "08-managed-album-dark-mobile.png", full_page=True)
        page.get_by_role("button", name="Close album", exact=True).click()
        page.get_by_role("button", name="Dark mode", exact=True).click()
        page.set_viewport_size({"width":1440,"height":1000})
        managed_album.click()
        page.get_by_role("region", name="Bulk Ready", exact=True).wait_for()
        assert page.get_by_role("dialog").count() == 0
        assert page.locator(".collection-ledger").is_visible()
        assert page.locator(".library-record[aria-pressed=true]").count() == 1
        page.locator(".album-detail-art img").wait_for()
        assert page.get_by_text("MusicBrainz release MBID", exact=True).is_visible()
        assert page.get_by_role("button", name="Edit metadata").is_visible()
        assert page.get_by_label("Upload replacement").is_visible()
        with page.expect_response(lambda response: response.url.endswith("/artwork/upload-preview")) as direct_upload:
            page.get_by_label("Upload replacement").set_input_files({"name":"direct-cover.jpg", "mimeType":"image/jpeg", "buffer":EXISTING_ARTWORK_JPEG})
        assert direct_upload.value.status == 201, direct_upload.value.json()
        assert page.get_by_label("Genre", exact=True).count() == 0
        page.get_by_role("button", name="Cancel", exact=True).click()
        page.get_by_role("button", name="Edit metadata").click()
        assert page.get_by_label("Upload replacement").is_visible()
        assert page.get_by_role("table", name="Tracks on Bulk Ready").is_visible()
        page.get_by_role("button", name=re.compile("^Edit track ")).first.click()
        assert page.get_by_label("Title", exact=True).input_value()
        inline_row = page.locator(".record-tracks tr.is-editing")
        assert inline_row.locator("input").count() == 4
        assert page.locator(".track-editor").count() == 0
        assert page.get_by_label("Title", exact=True).evaluate("el => el.form.id") == "track-form"
        page.screenshot(path=args.evidence_dir / "08-track-editor-desktop.png", full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
        inline_row.scroll_into_view_if_needed()
        assert inline_row.evaluate("el => [...el.querySelectorAll('input')].every(input => { const r = input.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })")
        page.screenshot(path=args.evidence_dir / "08-track-editor-mobile.png", full_page=True)
        original_title = page.get_by_label("Title", exact=True).input_value()
        page.get_by_label("Title", exact=True).fill("Discard this title")
        page.get_by_role("button", name="Cancel", exact=True).click()
        page.set_viewport_size({"width":1440,"height":1000})
        page.get_by_role("button", name=re.compile("^Edit track ")).first.click()
        assert page.get_by_label("Title", exact=True).input_value() == original_title
        page.get_by_label("Title", exact=True).fill("Edited fixture title")
        page.get_by_label("Artist", exact=True).fill("Fixture Guest")
        page.get_by_label("Track", exact=True).fill("3")
        page.get_by_label("Disc", exact=True).fill("2")
        with page.expect_response(lambda response: "/tracks/" in response.url and response.request.method == "PATCH") as saved_track:
            page.get_by_role("button", name="Save track", exact=True).click()
        assert saved_track.value.status == 200, saved_track.value.json()
        page.reload(wait_until="networkidle")
        page.locator(".library-record").filter(has_text="Bulk Ready").click()
        page.get_by_role("table", name="Tracks on Bulk Ready").get_by_text("Edited fixture title", exact=True).wait_for()
        assert MediaFile(str(library_bulk_track)).title == "Edited fixture title"
        page.get_by_role("button", name="Edit metadata", exact=True).click()
        with page.expect_response(lambda response: response.url.endswith("/artwork/upload-preview")) as upload:
            page.get_by_label("Upload replacement").set_input_files({
                "name": "replacement.jpg", "mimeType": "image/jpeg", "buffer": EXISTING_ARTWORK_JPEG,
            })
        assert upload.value.status == 201, upload.value.json()
        artwork_preview = upload.value.json()
        assert isinstance(artwork_preview.get("fingerprint"), str) and artwork_preview["fingerprint"]
        page.get_by_role("button", name="Apply artwork", exact=True).wait_for()
        page.get_by_role("button", name="Cancel", exact=True).click()
        assert page.get_by_role("button", name="Apply artwork", exact=True).count() == 0
        with page.expect_response(lambda response: response.url.endswith("/artwork/upload-preview")) as upload:
            page.get_by_label("Upload replacement").set_input_files({
                "name": "replacement-again.jpg", "mimeType": "image/jpeg", "buffer": EXISTING_ARTWORK_JPEG,
            })
        assert upload.value.status == 201, upload.value.json()
        artwork_preview = upload.value.json()
        with page.expect_response(lambda response: response.url.endswith("/artwork/apply")) as applied:
            page.get_by_role("button", name="Apply artwork", exact=True).click()
        assert applied.value.request.post_data_json == {
            "preview_id": artwork_preview["preview_id"], "expected_fingerprint": artwork_preview["fingerprint"],
        }
        assert applied.value.status == 200, applied.value.json()
        assert applied.value.json()["sha256"] == artwork_preview["sha256"]
        assert applied.value.json()["sidecars_written"] == 1
        assert page.get_by_role("alert").count() == 0
        page.get_by_role("button", name="Cancel changes").click()
        assert page.get_by_label("Upload replacement").is_visible()
        assert page.get_by_role("heading", name="MusicBrainz rematch").is_visible()
        page.screenshot(path=args.evidence_dir / "08-managed-album-read-only.png", full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        page.wait_for_function("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=args.evidence_dir / "08-managed-album-mobile.png", full_page=True)
        page.set_viewport_size({"width":1440,"height":1000})
        # Exercise both removal choices through the real built UI using only fixtures.
        kept_hash = sha256(library_bulk_track)
        page.get_by_role("button", name="Remove from library", exact=True).click()
        page.get_by_role("region", name="Remove album confirmation").wait_for()
        assert not page.get_by_label("Also delete files").is_checked()
        page.get_by_role("button", name="Cancel removal").click()
        assert sha256(library_bulk_track) == kept_hash
        page.get_by_role("button", name="Remove from library", exact=True).click()
        with page.expect_response(lambda response: response.url.endswith("/removal/apply")) as removed:
            page.get_by_role("button", name="Remove from Cratekeep; keep files", exact=True).click()
        assert removed.value.status == 200 and removed.value.json()["status"] == "complete"
        assert not removed.value.request.post_data_json["delete_files"]
        assert sha256(library_bulk_track) == kept_hash
        page.get_by_role("button", name="Refresh Library", exact=True).click()
        page.wait_for_load_state("networkidle")
        assert page.locator(".album-card").filter(has_text="Bulk Ready").count() == 0
        page.goto(base + "/library-import", wait_until="networkidle")
        # Removal reconciliation exposes kept files without a global disk scan.
        page.get_by_role("searchbox", name="Find an album").fill("Bulk Ready")
        page.locator(".desk-album").filter(has_text="Bulk Ready").wait_for()
        reviews = page.request.get(base + "/api/library-import/queue?q=Bulk+Ready").json()["albums"]
        review = next(row for row in reviews if row["album"] == "Bulk Ready")
        adopted = page.request.post(base + f"/api/library-import/albums/{review['id']}/execute", data={"candidate_id": None})
        assert adopted.status == 201, adopted.json()
        page.goto(base + "/library", wait_until="networkidle")
        page.locator(".album-card").filter(has_text="Bulk Ready").click()
        page.get_by_role("button", name="Remove from library", exact=True).click()
        page.get_by_label("Also delete files").check()
        with page.expect_response(lambda response: response.url.endswith("/removal/apply")) as removed:
            page.get_by_role("button", name="Delete listed files and remove album", exact=True).click()
        assert removed.value.status == 200 and removed.value.json()["status"] == "complete"
        assert removed.value.request.post_data_json["delete_files"]
        assert not library_bulk_track.exists()
        assert (library_bulk_track.parent / "cover.jpg").exists()
        page.get_by_text("Album removed. Audio files were deleted; artwork and folders were kept.", exact=True).wait_for()
        page.screenshot(path=args.evidence_dir / "09-album-removal.png", full_page=True)
        # Copy imports retain original bytes; exercise Undo and explicit source purge in the UI.
        copy_folder = args.fixture_root / "inbox" / "Copy Lifecycle"
        copy_sources = [copy_folder / "01 First.wav", copy_folder / "02 Second.wav"]
        for number, path in enumerate(copy_sources, 1):
            write_wav(path)
            tags=MediaFile(str(path))
            tags.artist=tags.albumartist="Copy Artist"
            tags.album="Copy Lifecycle"; tags.title=path.stem[3:]; tags.track=number; tags.save()
        originals={path:sha256(path) for path in copy_sources}
        page.goto(base+"/inbox",wait_until="networkidle")
        with page.expect_response(lambda response: response.url.endswith("/scopes/refresh")) as copied_refresh:
            page.get_by_role("button", name="Refresh", exact=True).click()
        assert copied_refresh.value.ok
        page.get_by_role("searchbox",name="Find an album").fill("Copy Lifecycle")
        copy_row=page.locator(".desk-album").filter(has_text="Copy Lifecycle")
        copy_row.get_by_role("button",name="Review Copy Lifecycle",exact=True).click()
        assert page.get_by_role("button",name="Review import",exact=True).count()==0
        assert page.get_by_role("button",name="Review selected",exact=True).count()==0
        with page.expect_response(lambda response: "/api/inbox/albums/" in response.url and response.request.method=="PATCH") as saved_choice:
            page.get_by_label("Select As Is",exact=True).click()
        assert saved_choice.value.ok
        page.wait_for_function("[...document.querySelectorAll('.as-is input')].some(node=>node.checked)")
        page.get_by_role("checkbox",name="Select album Copy Lifecycle",exact=True).check()
        page.get_by_role("button",name="Import selected",exact=True).click()
        page.get_by_role("button",name="Start import",exact=True).click()
        page.get_by_role("heading",name="Import results",exact=True).wait_for()
        page.get_by_role("button",name="Close results",exact=True).click()
        copy_row.locator(".desk-decision").filter(has_text="Imported").wait_for()
        assert all(sha256(path)==originals[path] for path in copy_sources)
        imported=page.request.get(inbox_base+"/queue?status=imported&q=Copy+Lifecycle").json()["albums"][0]
        # Read only receipt paths through its existing scoped inspection API.
        receipt=next(row for row in page.request.get(inbox_base+"/imports").json()["imports"] if row['job_id']==imported['imported']['job_id'])
        targets=[args.fixture_root/"library"/relative for relative in receipt['destinations']]
        assert all(path.is_file() for path in targets)
        copy_row.get_by_role("button",name="Review Copy Lifecycle",exact=True).click()
        page.get_by_role("button",name="Undo import",exact=True).click()
        page.get_by_role("button",name="Remove Library copies and undo",exact=True).click()
        copy_row.locator(".desk-decision").filter(has_text="Matched").wait_for()
        assert all(sha256(path)==originals[path] for path in copy_sources)
        assert not any(path.exists() for path in targets)
        copy_row.get_by_role("checkbox").check()
        page.get_by_role("button",name="Import selected",exact=True).click()
        page.get_by_role("button",name="Start import",exact=True).click()
        page.get_by_role("heading",name="Import results",exact=True).wait_for()
        saved={path:sha256(path) for path in targets}
        # Download-client sidecars must leave with the cleared album folder.
        (copy_folder/"album.nfo").write_text("download metadata")
        (copy_folder/"folder.jpg").write_bytes(b"disposable cover")
        (copy_folder/"artwork").mkdir()
        (copy_folder/"artwork"/"large.jpg").write_bytes(b"disposable artwork")
        page.screenshot(path=args.evidence_dir/"10a-copy-import-results.png",full_page=True)
        page.get_by_role("button",name="Clear imported from Inbox",exact=True).click()
        page.get_by_role("button",name="Permanently remove Inbox originals",exact=True).wait_for(state="visible")
        assert all(sha256(path)==originals[path] for path in copy_sources)
        assert all(sha256(path)==saved[path] for path in targets)
        page.get_by_role("button",name="Permanently remove Inbox originals",exact=True).click()
        copy_row.wait_for(state="hidden")
        assert not any(path.exists() for path in copy_sources) and not copy_folder.exists()
        assert all(sha256(path)==saved[path] for path in targets)
        page.screenshot(path=args.evidence_dir/"10b-imported-originals-removed.png",full_page=True)
        # Unimported deletion: inspector cancellation, exact track, then page batch.
        delete_folders=[args.fixture_root/"inbox"/name for name in ("Delete Download","Delete Other")]
        delete_sources=[]
        for folder in delete_folders:
            for index in range(2 if folder.name=="Delete Download" else 1):
                path=folder/f"{index+1}.wav";write_wav(path)
                media=MediaFile(path);media.album=folder.name;media.artist="Delete fixture"
                media.title="Bad song" if index==0 else "Keep song";media.track=index+1;media.save()
                delete_sources.append(path)
            (folder/"album.nfo").write_bytes(b"disposable metadata")
            (folder/"artwork").mkdir();(folder/"artwork"/"back.jpg").write_bytes(b"disposable artwork")
        expected={path:sha256(path) for path in delete_sources}
        page.get_by_role("searchbox",name="Find an album").fill("Delete")
        page.get_by_role("button",name="Refresh",exact=True).click()
        page.get_by_role("button",name="Review Delete Download",exact=True).click()
        inspector=page.get_by_role("region",name="Delete Download",exact=True)
        inspector.get_by_role("button",name="Delete from Inbox",exact=True).click()
        deletion=page.get_by_role("dialog",name="Delete from Inbox",exact=True)
        deletion.get_by_role("button",name="Delete permanently",exact=True).wait_for()
        assert "without importing" in deletion.inner_text()
        deletion.get_by_role("button",name="Cancel",exact=True).click()
        assert all(sha256(path)==expected[path] for path in delete_sources)
        inspector.get_by_text("Inbox files (2)",exact=True).click()
        page.screenshot(path=args.evidence_dir/"11-inbox-files-desktop.png",full_page=True)
        page.set_viewport_size({"width":390,"height":844})
        inspector.get_by_role("button",name="Delete track Bad song",exact=True).click()
        deletion=page.get_by_role("dialog",name="Delete track from Inbox",exact=True)
        deletion.locator("li").filter(has_text="Delete Download/1.wav").wait_for()
        assert "Delete Download/1.wav" in deletion.inner_text()
        page.screenshot(path=args.evidence_dir/"11a-delete-track-mobile.png",full_page=True)
        deletion.get_by_role("button",name="Delete permanently",exact=True).click()
        page.wait_for_function("!document.querySelector('dialog[open]')")
        page.get_by_role("button",name="Review Delete Download",exact=True).wait_for()
        assert not delete_sources[0].exists() and sha256(delete_sources[1])==expected[delete_sources[1]]
        assert (delete_folders[0]/"album.nfo").is_file()
        page.get_by_role("checkbox",name="Select this page",exact=True).check()
        page.locator('[aria-label="Selected album actions"]').get_by_role("button",name="Delete from Inbox",exact=True).click()
        deletion=page.get_by_role("dialog",name="Delete from Inbox",exact=True)
        deletion.locator("li").first.wait_for()
        assert "Delete Download" in deletion.inner_text() and "Delete Other" in deletion.inner_text()
        assert "2 folders · 6 files" in deletion.inner_text()
        assert not deletion.get_by_text("Delete Download/2.wav",exact=True).is_visible()
        page.screenshot(path=args.evidence_dir/"11b-delete-batch-mobile.png",full_page=True)
        deletion.get_by_text("Show files",exact=True).click()
        assert deletion.get_by_text("Delete Download/2.wav",exact=True).is_visible()
        deletion.get_by_text("Show files",exact=True).click()
        assert page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        deletion.get_by_role("button",name="Delete permanently",exact=True).click()
        page.get_by_text("No albums match this view",exact=True).wait_for()
        assert not any(folder.exists() for folder in delete_folders)
        assert all(sha256(path)==saved[path] for path in targets)
        assert page.get_by_text(re.compile("Inbox updates waiting")).count()==0
        # Symbol title changes must not block the actual selected bulk path.
        symbol_sources=[]
        for album_index, album_title in enumerate(("Symbol Pair One","Symbol Pair Two"),1):
            for index,title in enumerate(("Opening","Human Heart","Rainbow"),1):
                path=args.fixture_root/"inbox"/album_title/f"{index}.wav";write_wav(path)
                media=MediaFile(path);media.album=album_title;media.artist=f"Symbol fixture {album_index}"
                media.title=title;media.track=index;media.disc=1;media.save();symbol_sources.append(path)
        symbol_before={path:sha256(path) for path in symbol_sources}
        page.set_viewport_size({"width":1440,"height":1000})
        page.get_by_role("searchbox",name="Find an album").fill("Symbol Pair")
        page.get_by_role("button",name="Refresh",exact=True).click()
        page.get_by_role("button",name="Review Symbol Pair One",exact=True).wait_for()
        page.get_by_role("checkbox",name="Select this page",exact=True).check()
        match_selected(page,"/api/inbox/candidates",args.evidence_dir,expected_matches=2)
        page.get_by_role("button",name="Review Symbol Pair One",exact=True).click()
        coverage=page.locator(".review-track-coverage")
        assert "3 / 3" in coverage.inner_text() and "2 / 3" not in coverage.inner_text()
        page.get_by_text("Compare tracks (3)",exact=True).click()
        assert page.get_by_text("Match needs review",exact=True).count()==0
        assert page.get_by_role("table",name="Current and proposed track metadata").get_by_text("🌈",exact=True).is_visible()
        page.screenshot(path=args.evidence_dir/"12-symbol-title-review.png",full_page=True)
        page.locator("[data-review-close]").click()
        page.get_by_role("button",name="Import selected",exact=True).click()
        page.get_by_role("button",name="Start import",exact=True).click()
        page.get_by_text("2 of 2 albums imported.",exact=True).wait_for()
        page.screenshot(path=args.evidence_dir/"12a-symbol-bulk-results.png",full_page=True)
        assert all(sha256(path)==symbol_before[path] for path in symbol_sources)
        receipts=page.request.get(inbox_base+"/imports").json()["imports"]
        symbol_receipts=[row for row in receipts if row['album'].startswith("Symbol Pair")]
        assert len(symbol_receipts)==2
        for receipt in symbol_receipts:
            assert {MediaFile(args.fixture_root/"library"/relative).title for relative in receipt['destinations']}=={"Opening","♡","🌈"}
        # Metadata warnings remain advisory for an explicitly started import.
        page.get_by_role("button",name="Close results",exact=True).click()
        warning_sources=[]
        for index,title in enumerate(("First","Second","Third"),1):
            path=args.fixture_root/"inbox"/"Warning Import"/f"{index}.wav";write_wav(path)
            media=MediaFile(path);media.album="Warning Import";media.artist="Warning fixture"
            media.title=title;media.track=index;media.save();warning_sources.append(path)
        warning_before={path:sha256(path) for path in warning_sources}
        page.get_by_role("searchbox",name="Find an album").fill("Warning Import")
        page.get_by_role("button",name="Refresh",exact=True).click()
        page.get_by_role("button",name="Review Warning Import",exact=True).wait_for()
        page.get_by_role("checkbox",name="Select this page",exact=True).check()
        match_selected(page,"/api/inbox/candidates",args.evidence_dir)
        page.get_by_role("button",name="Import selected",exact=True).click()
        confirmation=page.get_by_role("dialog",name="Import selected albums",exact=True)
        confirmation.get_by_text(re.compile("Warning: Track counts differ")).wait_for()
        assert confirmation.get_by_text("Blocked",exact=True).count()==0
        assert confirmation.get_by_role("button",name="Start import",exact=True).is_enabled()
        page.screenshot(path=args.evidence_dir/"13-metadata-warning-confirmation.png",full_page=True)
        confirmation.get_by_role("button",name="Start import",exact=True).click()
        page.get_by_text("1 of 1 albums imported.",exact=True).wait_for()
        assert all(sha256(path)==warning_before[path] for path in warning_sources)
        receipt=next(row for row in page.request.get(inbox_base+"/imports").json()["imports"] if row['album']=="Warning Import Chosen")
        assert {MediaFile(args.fixture_root/"library"/relative).title for relative in receipt['destinations']}=={"First","Second","Third"}
        browser_version = browser.version
        browser.close()

    after = {"inbox_untagged": sha256(inbox_untagged), "inbox": sha256(inbox_track), "library": sha256(library_track),
             "library_bulk": None if not library_bulk_track.exists() else sha256(library_bulk_track)}
    assert before["inbox_untagged"] == after["inbox_untagged"], "Inbox grouping changed an untagged fixture"
    assert before["inbox"] == after["inbox"], "Library adoption changed the Inbox fixture"
    assert not page_errors and not console_errors, {"page_errors": page_errors, "console_errors": console_errors}
    result = {
        "source_identity": {"commit_at_run": args.source_commit, "tree_at_run": args.source_tree,
                            "definition": args.source_set_definition},
        "browser_version": browser_version, "routes": route_results,
        "threshold": 50, "inbox_scope_match": True, "refresh_rediscovers_hidden_sources_prunes_only_empty": True, "inbox_tagged_untagged_one_album": True, "album_scope_match": True,
        "inbox_and_library_pagination_selection": True,
        "bottom_right_toasts_desktop_mobile_dismiss": True, "queue_review_modal": True, "review_mbid_search": True, "review_rich_candidate": True,
        "review_track_evidence": True, "confirmed_bulk_import": True,
        "settings_persistence": True, "managed_album_read_only_edit": True,
        "managed_artwork_upload_preview_cancel_apply": True,
        "overview_first_collection_ledger_desktop_mobile_dark": True,
        "compact_album_track_editor_desktop_mobile": True,
        "library_inspector_without_modal_overview_library_size_source_cards": True,
        "album_removal_keep_files_readopt_delete": True,
        "copy_import_retains_originals_undo_reimport_remove_originals": True, "simplified_pending_matched_imported_queue": True, "unimported_album_track_batch_deletion": True, "symbol_title_bulk_import": True, "metadata_warnings_do_not_block_import": True,
        "fixture_sha256_before": before, "fixture_sha256_after": after,
        "page_errors": page_errors, "console_errors": console_errors,
    }
    (args.evidence_dir / "runtime-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    (args.evidence_dir / "environment-identity.json").write_text(json.dumps({
        "hostname": platform.node(), "platform": platform.platform(), "python": sys.version,
        "browser": browser_version, "fixture_root": str(args.fixture_root.resolve()),
        "state_path": str(args.state_path.resolve()),
    }, indent=2) + "\n", encoding="utf-8")
    manifest = [f"{sha256(path)}  {path.name}" for path in sorted(args.evidence_dir.iterdir()) if path.is_file()]
    (args.evidence_dir / "SHA256SUMS").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
