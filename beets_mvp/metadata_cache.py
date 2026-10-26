"""Bounded, disposable cache of successful public metadata responses."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

MAX_ENTRIES = 512
MAX_BYTES = 32 * 1024 * 1024


class MetadataCache:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=0.5)
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS responses (
                key TEXT PRIMARY KEY, document TEXT NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, bytes INTEGER NOT NULL)""")
            return db
        except sqlite3.Error:
            db.close()
            raise

    def get(self, url: str) -> dict | None:
        now = time.time()
        try:
            with closing(self._connect()) as db, db:
                row = db.execute("SELECT document FROM responses WHERE key=? AND created<=? AND expires>?",
                                 (hashlib.sha256(url.encode()).hexdigest(), now, now)).fetchone()
            result = json.loads(row[0]) if row else None
            return result if isinstance(result, dict) else None
        except (OSError, sqlite3.Error, ValueError):
            return None  # A broken/disposable cache must not break metadata access.

    def put(self, url: str, document: dict, ttl: int) -> None:
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        size = len(payload.encode())
        if size > MAX_BYTES:
            return
        now = time.time()
        try:
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM responses WHERE expires<=? OR created>?", (now, now))
                db.execute("INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?)",
                           (hashlib.sha256(url.encode()).hexdigest(), payload, now, now + ttl, size))
                count, total = db.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM responses").fetchone()
                while count > MAX_ENTRIES or total > MAX_BYTES:
                    oldest = db.execute("SELECT key, bytes FROM responses ORDER BY created, key LIMIT 1").fetchone()
                    db.execute("DELETE FROM responses WHERE key=?", (oldest[0],))
                    count -= 1
                    total -= oldest[1]
        except (OSError, sqlite3.Error):
            pass
