"""Browser smoke for the React single-cutover UI.

The smoke uses disposable WAV fixtures and metadata-only scans. It never
executes an import or contacts a real metadata provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
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
    write_wav(inbox_track); write_wav(library_track)
    before = {"inbox": sha256(inbox_track), "library": sha256(library_track)}
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

        page.goto(base + "/inbox", wait_until="networkidle")
        assert page.get_by_role("button", name="Match Selected").is_disabled()
        page.get_by_role("checkbox", name="Select folder Inbox Ready").check()
        with page.expect_response(lambda response: response.url.endswith("/scopes/scan")) as inbox_scan:
            page.get_by_role("button", name="Match Selected").click()
        assert inbox_scan.value.ok
        page.get_by_role("button", name="Expand folder Inbox Ready").click()
        inbox_album = page.locator(".album-node").filter(has_text="Inbox Ready")
        inbox_album.wait_for()
        inbox_album.get_by_role("button", name=re.compile("Actions for")).click()
        assert inbox_album.get_by_role("menuitem").all_inner_texts() == ["Scan", "Import", "Review"]
        with page.expect_response(lambda response: response.url.endswith("/scopes/scan")) as inbox_album_scan:
            inbox_album.get_by_role("menuitem", name="Scan").click()
        assert inbox_album_scan.value.ok

        page.goto(base + "/library-import", wait_until="networkidle")
        assert page.locator(".toolbar-actions button").all_inner_texts() == ["Match Selected", "Import", "Review", "Refresh"]
        page.get_by_role("checkbox", name="Select folder Library Ready").check()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")):
            page.get_by_role("button", name="Match Selected").click()
        page.get_by_role("button", name="Expand folder Library Ready").click()
        album = page.locator(".album-node").filter(has_text="Library Ready")
        album.wait_for()
        assert album.locator(".status").inner_text() in {"Matched", "Threshold"}
        assert album.locator(".compact-import").count() == 0
        album.get_by_role("button", name=re.compile("Actions for")).click()
        assert album.get_by_role("menuitem").all_inner_texts() == ["Scan", "Import", "Review"]
        assert album.get_by_role("menuitem", name="Import").is_enabled()
        with page.expect_response(lambda response: response.url.endswith("/api/library-import/folders/scan")) as album_scan:
            album.get_by_role("menuitem", name="Scan").click()
        assert album_scan.value.ok
        album = page.locator(".album-node").filter(has_text="Library Ready")
        album.get_by_role("button", name=re.compile("Actions for")).click()
        album.get_by_role("menuitem", name="Review").click()
        page.locator("[data-review-modal]").wait_for()
        assert page.locator("[data-review-candidates]").is_visible()
        page.get_by_text(re.compile(r"Compare tracks \(\d+\)")).first.click()
        assert page.locator(".track-evidence-row").count() > 0
        assert page.locator(".track-evidence-row [role=cell]").first.is_visible()
        assert page.locator(".change-indicator").first.is_visible()
        page.locator("[data-review-close]").click()
        page.locator("[data-review-modal]").wait_for(state="detached")
        page.screenshot(path=args.evidence_dir / "06-library-import-queue.png", full_page=True)
        browser_version = browser.version
        browser.close()

    after = {"inbox": sha256(inbox_track), "library": sha256(library_track)}
    assert before == after, "metadata-only UI smoke changed fixture bytes"
    assert not page_errors and not console_errors, {"page_errors": page_errors, "console_errors": console_errors}
    result = {
        "source_identity": {"commit_at_run": args.source_commit, "tree_at_run": args.source_tree,
                            "definition": args.source_set_definition},
        "browser_version": browser_version, "routes": route_results,
        "threshold": 90, "inbox_scope_scan": True, "album_scope_scan": True,
        "queue_review_modal": True, "review_track_evidence": True,
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
