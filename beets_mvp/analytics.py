"""Opt-in, install-level Beacon telemetry. Never runs on the request thread."""
import json
import platform
import sqlite3
import threading
import urllib.request
import uuid
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

BEACON_URL = "https://beacon.kjschultz.com/ping"


def send_ping(payload):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    request = urllib.request.Request(
        BEACON_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "Cratekeep"},
    )
    with urllib.request.build_opener(NoRedirect).open(request, timeout=3) as response:
        response.read(1024)


def library_counts(path):
    counts = {"artist_count": 0, "album_count": 0, "song_count": 0}
    if not Path(path).is_file():
        return counts
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.create_function("CASEFOLD", 1, lambda value: str(value or "").casefold())
        row = db.execute("""SELECT
            (SELECT COUNT(*) FROM items), (SELECT COUNT(*) FROM albums),
            (SELECT COUNT(DISTINCT CASEFOLD(COALESCE(NULLIF(albumartist, ''),
                (SELECT NULLIF(artist, '') FROM items WHERE album_id=albums.id ORDER BY id LIMIT 1),
                'Unknown artist'))) FROM albums)""").fetchone()
    return dict(zip(("song_count", "album_count", "artist_count"), row))


class Beacon:
    def __init__(self, app):
        self.app = app
        self.wake = threading.Event()

    def run_once(self, now=None):
        """Claim an attempt transactionally before I/O; failures wait until tomorrow."""
        try:
            now = now or datetime.now(timezone.utc)
            today = now.astimezone(timezone.utc).date().isoformat()
            with closing(sqlite3.connect(self.app.config["APP_DB"], timeout=1)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                settings = dict(db.execute("SELECT key, value FROM app_settings WHERE key IN "
                                           "('analytics_enabled','analytics_install_id','analytics_last_attempt')"))
                if settings.get("analytics_enabled") != "1" or settings.get("analytics_last_attempt") == today:
                    return False
                install_id = settings.get("analytics_install_id") or str(uuid.uuid4())
                db.executemany("INSERT INTO app_settings(key,value) VALUES (?,?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               [("analytics_install_id", install_id), ("analytics_last_attempt", today)])
            machine = platform.machine().lower()
            build = self.app.config["BUILD_SHA"]
            payload = {
                "project": "cratekeep", "install_id": install_id,
                "version": build if build != "unknown" else "development",
                "arch": {"aarch64": "arm64", "x86_64": "amd64"}.get(machine, machine),
                "os": platform.system(), "timestamp": now.isoformat(),
                "dev": build == "unknown" or self.app.config.get("BEACON_DEV", False),
                **library_counts(self.app.config["BEETS_DB"]),
            }
            # Recheck consent after preparing counts, before any network request.
            with closing(sqlite3.connect(self.app.config["APP_DB"], timeout=1)) as db, db:
                enabled = db.execute("SELECT value FROM app_settings WHERE key='analytics_enabled'").fetchone()
            if not enabled or enabled[0] != "1":
                return False
            self.app.config.get("BEACON_TRANSPORT", send_ping)(payload)
            return True
        except Exception:
            # No provider errors/payloads in logs and no effect on the music workflow.
            return False

    def start(self):
        def worker():
            while True:
                self.run_once()
                self.wake.wait(3600)
                self.wake.clear()
        threading.Thread(target=worker, name="cratekeep-beacon", daemon=True).start()
