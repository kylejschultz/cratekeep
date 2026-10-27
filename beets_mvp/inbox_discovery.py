"""Per-Inbox discovery for the supported single-worker server.

Filesystem events are hints. Only a committed inventory scan emits scan_completed;
future notifications can subscribe there instead of observing raw download events.
"""
import atexit
from contextlib import ExitStack
from datetime import datetime, timezone
import os
from pathlib import Path
import threading
import time
import uuid

from blinker import Signal
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DISCOVERY_MODES = ("manual", "instant", "5", "15", "30", "60", "360", "1440")
scan_completed = Signal("Committed Inbox inventory, not an import or notification")


class InboxDiscovery:
    def __init__(self, app, *, clock=time.monotonic, observer_factory=Observer):
        self.app = app
        self.clock = clock
        self.observer_factory = observer_factory
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.lock = threading.RLock()
        self.states = {}
        self.revisions = {}
        self.identity = uuid.uuid4().hex
        self.thread = None

    def changed(self, inbox_id):
        with self.lock:
            self.revisions[inbox_id] = self.revisions.get(inbox_id, 0) + 1

    def status(self, inbox):
        with self.lock:
            state = self.states.get(inbox["id"], {})
            return {"mode": inbox.get("discovery", "5"),
                    "revision": f'{self.identity}:{self.revisions.get(inbox["id"], 0)}',
                    "watcher_error": state.get("watcher_error", "")}

    def hint(self, inbox_id):
        with self.lock:
            state = self.states.get(inbox_id)
            if state and state["mode"] == "instant":
                state["event_due"] = self.clock() + 2
        self.wake.set()

    def _stop_observer(self, state):
        observer = state.pop("observer", None)
        if observer:
            observer.stop()
            observer.join(timeout=2)

    def _watch(self, inbox, state, now):
        if state["mode"] != "instant":
            return
        observer = state.get("observer")
        root = Path(inbox["path"])
        try:
            stat = root.stat()
            root_identity = (stat.st_dev, stat.st_ino)
        except OSError:
            root_identity = None
        if (observer and observer.is_alive() and root.is_dir() and not root.is_symlink()
                and state.get("root_identity") == root_identity):
            # An emitter can die while its dispatcher remains alive.
            if all(emitter.is_alive() for emitter in observer.emitters):
                return
        if observer:
            self._stop_observer(state)
            state["watcher_error"] = "Instant watcher stopped; using five-minute scans."
            # Report a lost/replaced root promptly rather than leaving its last
            # successful scan visible until the safety interval expires.
            state["due"] = min(state["due"], now)
        if now < state.get("watch_retry", 0):
            return
        state["watch_retry"] = now + 30
        service = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                from . import AUDIO_EXTENSIONS
                if event.event_type not in {"created", "modified", "deleted", "moved", "closed"}:
                    return
                if event.is_directory and event.event_type == "modified":
                    return
                paths = (event.src_path, getattr(event, "dest_path", ""))
                if event.is_directory or any(Path(path).suffix.lower() in AUDIO_EXTENSIONS for path in paths):
                    service.hint(inbox["id"])

        observer = self.observer_factory()
        try:
            if not root.is_dir() or root.is_symlink():
                raise OSError("Inbox root unavailable")
            observer.schedule(Handler(), str(root), recursive=True)
            observer.start()
        except Exception:
            observer.stop()
            if observer.is_alive():
                observer.join(timeout=2)
            state["watcher_error"] = "Instant watcher unavailable; using five-minute scans. Check the mount and watch limits."
        else:
            state["observer"] = observer
            state["root_identity"] = root_identity
            state["watcher_error"] = ""
            # Catch changes made while the watcher was disconnected.
            state["due"] = min(state["due"], now)

    def step(self):
        """One bounded scheduling pass; tests drive this with a fake clock."""
        from . import (_connect, _library_audio_stats, _scan_inbox_inventory,
                       _start_inbox_auto_match, MAX_INBOX_AUTO_MATCH_PENDING)
        app = self.app
        # Same outer lock as mutating HTTP requests: no stale root/settings writes.
        with ExitStack() as locks:
            for lock in (app.config["MUTATION_LOCK"], app.config["INBOX_CANDIDATE_LOCK"],
                         app.config["INBOX_EXECUTION_LOCK"]):
                if not lock.acquire(blocking=False):
                    return
                locks.callback(lock.release)
            inboxes = list(app.config["INBOXES"]) if app.config["SETUP_COMPLETE"] else []
            enabled = {row["id"]: row for row in inboxes if row.get("discovery", "5") != "manual"}
            # Only this scheduling thread creates/stops observers. Event handlers
            # take the short state lock, never the mutation lock.
            for inbox_id in list(self.states):
                state = self.states[inbox_id]
                inbox = enabled.get(inbox_id)
                if not inbox or (inbox["path"], inbox.get("discovery", "5")) != (state["path"], state["mode"]):
                    self._stop_observer(state)
                    with self.lock:
                        self.states.pop(inbox_id, None)
            for inbox_id, inbox in enabled.items():
                if self.stopped.is_set():
                    return
                now = self.clock()
                with self.lock:
                    state = self.states.setdefault(inbox_id, {"path": inbox["path"],
                        "mode": inbox.get("discovery", "5"), "due": now, "event_due": None})
                self._watch(inbox, state, now)
                with self.lock:
                    event_due = state["event_due"]
                    if now < state["due"] and (event_due is None or now < event_due):
                        continue
                    # An active event burst must settle even if its safety scan is due.
                    if event_due is not None and now < event_due:
                        continue
                try:
                    # Two stable snapshots protect partially written files from
                    # background discovery/matching. This is not a downloader protocol.
                    root = Path(inbox["path"])
                    if root.is_symlink():
                        raise ValueError("Inbox root must not be a symlink.")
                    snapshot = [(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev)
                                for path, stat in _library_audio_stats(root)]
                    if state.get("snapshot") != snapshot:
                        state["snapshot"] = snapshot
                        state["due"] = now + 2
                        continue
                    with self.lock:
                        state["event_due"] = None
                    _scan_inbox_inventory(app, inbox, trigger="background")
                    if inbox.get("auto_match"):
                        with _connect(app.config["APP_DB"]) as db:
                            ids = [row[0] for row in db.execute(
                                """SELECT id FROM album_reviews WHERE source_kind='inbox' AND source_id=?
                                     AND root_path=? AND state='pending'
                                     AND candidate_status IN ('not-run','stale')
                                     AND execution_status='not-run' ORDER BY id LIMIT ?""",
                                (inbox_id, inbox["path"], MAX_INBOX_AUTO_MATCH_PENDING))]
                        if ids:
                            _start_inbox_auto_match(app, ids)
                except Exception as exc:
                    # A failed root is not an empty root. Persist scoped status,
                    # preserve its last inventory and let other sources progress.
                    with _connect(app.config["APP_DB"]) as db:
                        now_text = datetime.now(timezone.utc).isoformat()
                        db.execute("""INSERT INTO adoption_jobs(kind,root_path,status,error,created_at,finished_at)
                                      VALUES ('inbox_inventory',?,'failed',?,?,?)""",
                                   (inbox["path"], str(exc)[:1000], now_text, now_text))
                    self.changed(inbox_id)
                    app.logger.warning("Inbox background scan failed for source %s (%s)", inbox_id, type(exc).__name__)
                state.pop("snapshot", None)
                state["due"] = self.clock() + (300 if state["mode"] == "instant" else int(state["mode"]) * 60)
                with self.lock:
                    # Keep any hint received while scanning for a follow-up pass.
                    if state["event_due"] is not None and state["event_due"] <= now:
                        state["event_due"] = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        # The reloader parent must not scan or own native observers.
        from werkzeug.serving import is_running_from_reloader
        if os.environ.get("FLASK_RUN_FROM_CLI") == "true" and (self.app.debug or os.environ.get("FLASK_DEBUG") in {"1", "true"}) and not is_running_from_reloader():
            return
        def run():
            try:
                while not self.stopped.is_set():
                    self.wake.clear()
                    try:
                        with self.app.app_context():
                            self.step()
                    except Exception:
                        self.app.logger.exception("Inbox discovery scheduling pass failed")
                    self.wake.wait(1)
            finally:
                for state in self.states.values():
                    self._stop_observer(state)
        self.thread = threading.Thread(target=run, name="cratekeep-inbox-discovery", daemon=True)
        self.thread.start()
        atexit.register(self.stop)

    def stop(self):
        self.stopped.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)
        else:
            for state in self.states.values():
                self._stop_observer(state)
