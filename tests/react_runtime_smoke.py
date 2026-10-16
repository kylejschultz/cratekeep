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
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright


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


def wait_for_library_fixtures(base: str) -> None:
    """Wait until the container's bind mount exposes every library fixture."""
    deadline = time.monotonic() + 20
    latest = None
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/api/library-import/folders?page=2", timeout=5) as response:
            latest = json.load(response)
        paths = {folder["path"] for folder in latest.get("folders", [])}
        if latest.get("total") == 27 and paths == {"Bulk Ready", "Library Ready"}:
            return
        time.sleep(0.1)
    raise AssertionError({"library fixtures did not become ready": latest})


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
    for index in range(25):
        (args.fixture_root / "inbox" / f"A Scope {index:02d}").mkdir(parents=True)
        (args.fixture_root / "library" / f"A Scope {index:02d}").mkdir(parents=True)
    before = {"inbox": sha256(inbox_track), "library": sha256(library_track),
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
            assert page.get_by_role("navigation", name="Primary navigation").get_by_role("link").count() == 5
            desktop_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
            desktop_fill = page.locator(".app-content").evaluate("node => node.getBoundingClientRect().height >= innerHeight")
            page.set_viewport_size({"width": 390, "height": 844})
            mobile_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
            mobile_fill = page.locator(".app-content").evaluate("node => node.getBoundingClientRect().height >= innerHeight")
            page.screenshot(path=args.evidence_dir / f"{index:02d}-{path.strip('/') or 'overview'}-mobile.png", full_page=True)
            page.set_viewport_size({"width": 1440, "height": 1000})
            route_results[path] = {"heading": heading, "desktop_no_overflow": desktop_ok, "mobile_no_overflow": mobile_ok,
                                   "desktop_theme_fill": desktop_fill, "mobile_theme_fill": mobile_fill}
            assert desktop_ok and mobile_ok, f"horizontal overflow on {path}"
            assert desktop_fill and mobile_fill, f"viewport theme does not fill {path}"

        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("button", name="Help: Inboxes").wait_for()
        page.get_by_role("tab", name="Matching").click()
        page.get_by_label("Matched threshold (%)").fill("50")
        page.get_by_role("tab", name="Metadata").click()
        page.get_by_label("Fetch Cover Art Archive artwork").check()
        page.get_by_label("Save cover.jpg sidecars").check()
        with page.expect_navigation(wait_until="networkidle"):
            page.get_by_role("button", name="Save settings").click()
        page.goto(base + "/settings", wait_until="networkidle")
        page.get_by_role("tab", name="Metadata").click()
        assert page.get_by_label("Fetch Cover Art Archive artwork").is_checked()
        assert page.get_by_label("Save cover.jpg sidecars").is_checked()
        page.get_by_role("tab", name="Matching").click()
        assert page.get_by_label("Matched threshold (%)").input_value() == "50"
        assert page.get_by_label(re.compile("Automatically find matches for")).count() == 1

        page.goto(base + "/inbox", wait_until="networkidle")
        assert page.get_by_role("button", name="Match Selected").is_disabled()
        assert page.get_by_text("Page 1 of 2").is_visible()
        page.locator(".folder-node input[type=checkbox]").first.check()
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Page 2 of 2").wait_for()
        assert "1 selected" in page.locator(".queue-toolbar small").inner_text()
        page.get_by_role("button", name="Previous").click()
        page.get_by_text("Page 1 of 2").wait_for()
        page.locator(".folder-node input[type=checkbox]").first.uncheck()
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Page 2 of 2").wait_for()
        page.get_by_role("checkbox", name="Select folder Inbox Ready").check()
        with page.expect_response(lambda response: response.url.endswith("/scopes/scan")) as inbox_scan:
            page.get_by_role("button", name="Match Selected").click()
        assert inbox_scan.value.ok
        page.get_by_role("button", name="Expand folder Inbox Ready").click()
        inbox_album = page.locator(".album-node").filter(has_text="Inbox Ready")
        inbox_album.wait_for()
        inbox_album.get_by_role("button", name=re.compile("Actions for")).click()
        assert inbox_album.get_by_role("menuitem").all_inner_texts() == ["Match", "Import", "Review"]
        with page.expect_response(lambda response: response.url.endswith("/scopes/scan")) as inbox_album_scan:
            inbox_album.get_by_role("menuitem", name="Match").click()
        assert inbox_album_scan.value.ok

        page.goto(base + "/library-import", wait_until="networkidle")
        assert page.locator(".toolbar-actions button").all_inner_texts() == ["Match Selected", "Import", "Review", "Refresh"]
        assert page.get_by_text("Page 1 of 2").is_visible()
        page.locator(".folder-node input[type=checkbox]").first.check()
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Page 2 of 2").wait_for()
        assert "1 selected" in page.locator(".queue-toolbar small").inner_text()
        page.get_by_role("button", name="Previous").click()
        page.get_by_text("Page 1 of 2").wait_for()
        page.locator(".folder-node input[type=checkbox]").first.uncheck()
        page.get_by_role("button", name="Next").click()
        page.get_by_text("Page 2 of 2").wait_for()
        page.get_by_role("checkbox", name="Select folder Library Ready").check()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")):
            page.get_by_role("button", name="Match Selected").click()
        page.get_by_role("button", name="Expand folder Library Ready").click()
        album = page.locator(".album-node").filter(has_text="Library Ready")
        album.wait_for()
        assert album.locator(".status").inner_text() in {"Matched", "Threshold"}
        assert album.locator(".compact-import").count() == 0
        album.get_by_role("button", name=re.compile("Actions for")).click()
        assert album.get_by_role("menuitem").all_inner_texts() == ["Match", "Import", "Review"]
        assert album.get_by_role("menuitem", name="Import").is_enabled()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")) as album_scan:
            album.get_by_role("menuitem", name="Match").click()
        assert album_scan.value.ok
        album = page.locator(".album-node").filter(has_text="Library Ready")
        album.get_by_role("button", name=re.compile("Actions for")).click()
        album.get_by_role("menuitem", name="Review").click()
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
        assert page.get_by_text("123e4567-e89b-42d3-a456-426614174002").is_visible()
        assert page.locator(".candidate-fact").count() >= 8
        page.get_by_text(re.compile(r"Compare tracks \(\d+\)")).first.click()
        assert page.locator(".track-evidence-row").count() > 0
        assert page.locator(".track-evidence-row [role=cell]").first.is_visible()
        assert page.locator(".change-indicator").first.is_visible()
        assert page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        page.screenshot(path=args.evidence_dir / "06-review-modal-desktop.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=args.evidence_dir / "07-review-modal-narrow.png", full_page=True)
        narrow_modal_ok = page.locator("html").evaluate("node => node.scrollWidth <= node.clientWidth")
        if not narrow_modal_ok:
            offenders = page.locator("body").evaluate("""node => [...node.querySelectorAll('*')]
              .filter(item => item.getBoundingClientRect().right > innerWidth + 1)
              .map(item => ({tag: item.tagName, className: item.className, right: item.getBoundingClientRect().right, scrollWidth: item.scrollWidth}))
              .slice(0, 12)""")
            raise AssertionError({"narrow_modal_overflow": offenders})
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="detached")
        wait_for_library_fixtures(base)
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders?page=2")):
            page.get_by_role("button", name="Refresh").click()
        page.get_by_role("checkbox", name="Select folder Library Ready").uncheck()
        page.get_by_role("checkbox", name="Select folder Bulk Ready").check()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")):
            page.get_by_role("button", name="Match Selected").click()
        page.once("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/execute")) as bulk_import:
            with page.expect_response(lambda response: response.url.endswith("/api/library-import/bulk/preview")) as bulk_preview:
                page.locator(".toolbar-actions").get_by_role("button", name="Import", exact=True).click()
        preview_payload = bulk_preview.value.json()
        assert preview_payload.get("ready_album_count") == 1, preview_payload
        bulk_payload = bulk_import.value.json()
        assert len(bulk_payload.get("imported", [])) == 1, bulk_payload
        page.get_by_text(re.compile(r"1 albums? imported")).wait_for()

        page.goto(base + "/library", wait_until="networkidle")
        managed_album = page.locator(".album-card").filter(has_text="Bulk Ready")
        managed_album.wait_for()
        managed_album.click()
        page.get_by_role("dialog").wait_for()
        page.locator(".album-detail-art img").wait_for()
        assert page.get_by_role("button", name="Edit").is_visible()
        assert page.get_by_label("Upload replacement").count() == 0
        page.get_by_role("button", name="Edit").click()
        assert page.get_by_label("Upload replacement").is_visible()
        page.get_by_role("button", name="Cancel changes").click()
        assert page.get_by_label("Upload replacement").count() == 0
        assert page.get_by_role("heading", name="MusicBrainz rematch").is_visible()
        page.screenshot(path=args.evidence_dir / "08-managed-album-read-only.png", full_page=True)
        browser_version = browser.version
        browser.close()

    after = {"inbox": sha256(inbox_track), "library": sha256(library_track),
             "library_bulk": sha256(library_bulk_track)}
    assert before["inbox"] == after["inbox"], "Library adoption changed the Inbox fixture"
    assert not page_errors and not console_errors, {"page_errors": page_errors, "console_errors": console_errors}
    result = {
        "source_identity": {"commit_at_run": args.source_commit, "tree_at_run": args.source_tree,
                            "definition": args.source_set_definition},
        "browser_version": browser_version, "routes": route_results,
        "threshold": 50, "inbox_scope_match": True, "album_scope_match": True,
        "inbox_and_library_pagination_selection": True,
        "queue_review_modal": True, "review_mbid_search": True, "review_rich_candidate": True,
        "review_track_evidence": True, "confirmed_bulk_import": True,
        "settings_persistence": True, "managed_album_read_only_edit": True,
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
