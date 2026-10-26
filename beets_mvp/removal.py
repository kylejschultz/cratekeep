"""Explicit, previewed whole-album unmanagement and scoped audio deletion."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
import stat
import uuid
from datetime import datetime, timezone

from flask import jsonify, request

from . import (
    AUDIO_EXTENSIONS, ManagedMutationError, _album_items, _connect, _library,
    _now, _require_current_album, _validated_library_root, _managed_path,
    _scope_for_relative_path, _sync_album_reviews,
)


def _file_state(path: Path) -> list[int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Only regular audio files with no shared hard links can be deleted.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _safe_delete_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute() or root not in path.parents or path.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("An audio path is outside the current Library.")
    current = root
    for part in path.relative_to(root).parts:
        if part in {".", ".."}:
            raise ValueError("An audio path is unsafe.")
        current = current / part
        if current.is_symlink():
            raise ValueError("Symlinks cannot be used for file deletion.")
    _file_state(path)
    return path


def _plan(app, album) -> dict:
    root = _validated_library_root(app)
    tracks, blocked = [], []
    owners = {}
    for item in _library(app).items():
        owners.setdefault(str(Path(os.fsdecode(item.path)).resolve()), []).append(item.id)
    for item in _album_items(album):
        raw = os.fsdecode(item.path)
        track = {"id": item.id, "title": item.title, "path": raw,
                 "relative_path": os.path.relpath(raw, root), "file_state": None}
        try:
            path = _safe_delete_path(root, raw)
            if owners.get(str(path)) != [item.id]:
                raise ValueError("An audio path belongs to more than one managed item.")
            track["file_state"] = _file_state(path)
        except (OSError, ValueError) as exc:
            blocked.append(str(exc))
        tracks.append(track)
    return {"root": str(root), "tracks": tracks, "delete_allowed": not blocked and bool(tracks),
            "delete_error": "; ".join(dict.fromkeys(blocked))}


def _unlink_reviewed(root: Path, track: dict) -> None:
    """Walk with directory descriptors: a swapped ancestor cannot redirect unlink."""
    relative = Path(track["path"]).relative_to(root)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
        actual = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or actual != track["file_state"]:
            raise ValueError("The audio file changed after preview.")
        os.unlink(relative.name, dir_fd=descriptor)
    finally:
        os.close(descriptor)


def _reconcile(app, record: dict, removed: list[dict]) -> None:
    """Clear live references, retaining completed reviews and immutable job history."""
    with _connect(app.config["APP_DB"]) as db:
        for track in removed:
            rows = db.execute(
                "SELECT id, root_path, relative_path FROM library_inventory WHERE beets_item_id = ?", (track["id"],)
            ).fetchall()
            for row in rows:
                db.execute("UPDATE library_inventory SET beets_item_id=NULL, present=? WHERE id=?",
                           (int((Path(row["root_path"]) / row["relative_path"]).is_file()), row["id"]))
                db.execute("""UPDATE adoption_reviews SET state='pending', imported_at=NULL,
                              candidates_json='[]', updated_at=? WHERE inventory_id=?""", (_now(), row["id"]))
                # Keep the old review/candidate/job rows for audit; detach only the
                # current inventory relationship so explicit re-adoption gets a new review.
                db.execute("DELETE FROM album_review_tracks WHERE inventory_id=?", (row["id"],))
        # Reserve old completed keys before an explicit inventory scan creates new work.
        db.execute("""UPDATE album_reviews SET artist_key=artist_key || char(31) || 'removed:' || id
                      WHERE execution_status='complete' AND root_path=?
                      AND NOT EXISTS (SELECT 1 FROM album_review_tracks WHERE album_review_id=album_reviews.id)
                      AND instr(artist_key, char(31) || 'removed:')=0""", (record["root"],))
        db.execute("UPDATE managed_album_rematches SET status='invalidated' WHERE album_id=? AND status='pending'",
                   (record["album_id"],))


def _queue_kept_tracks(app, plan: dict) -> None:
    """Publish only the removed, still-unmanaged paths to the adoption queue."""
    root = _validated_library_root(app)
    if str(root) != plan["root"]:
        return
    managed = {str(Path(os.fsdecode(item.path)).resolve()) for item in _library(app).items()}
    kept = []
    for track in plan["tracks"]:
        try:
            path = _managed_path(app, track["path"])
            if str(path) not in managed:
                kept.append((path.relative_to(root).as_posix(), path.stat()))
        except (ManagedMutationError, OSError, ValueError):
            continue
    if not kept:
        return
    now = _now()
    with _connect(app.config["APP_DB"]) as db:
        job_id = db.execute(
            """INSERT INTO adoption_jobs(kind, root_path, status, summary_json, created_at, finished_at)
               VALUES ('removal_inventory', ?, 'complete', ?, ?, ?)""",
            (str(root), json.dumps({"files": len(kept)}), now, now),
        ).lastrowid
        for relative, info in kept:
            db.execute(
                """INSERT INTO library_inventory(root_path, relative_path, size_bytes, mtime_ns, device, inode,
                          beets_item_id, present, first_seen_job_id, last_seen_job_id)
                   VALUES (?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)
                   ON CONFLICT(root_path, relative_path) DO UPDATE SET
                     size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, device=excluded.device,
                     inode=excluded.inode, beets_item_id=NULL, present=1, last_seen_job_id=excluded.last_seen_job_id""",
                (str(root), relative, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, job_id, job_id),
            )
            inventory_id = db.execute("SELECT id FROM library_inventory WHERE root_path=? AND relative_path=?",
                                      (str(root), relative)).fetchone()[0]
            db.execute("""INSERT INTO adoption_reviews(inventory_id, state, candidates_json, updated_at)
                          VALUES (?, 'pending', '[]', ?)
                          ON CONFLICT(inventory_id) DO UPDATE SET state='pending', candidates_json='[]',
                            imported_at=NULL, updated_at=excluded.updated_at""", (inventory_id, now))
    _sync_album_reviews(app, scopes=sorted({_scope_for_relative_path(relative) for relative, _ in kept}))


def _apply(app, album_id: int, values: dict) -> dict:
    with _connect(app.config["APP_DB"]) as db:
        row = db.execute("SELECT * FROM managed_album_removals WHERE id=? AND album_id=?",
                         (values["preview_id"], album_id)).fetchone()
    if row is None:
        raise ManagedMutationError("Removal preview not found. Open a new preview.", code="preview_not_found", status=404)
    if values["expected_fingerprint"] != row["fingerprint"]:
        raise ManagedMutationError("The removal preview changed.", code="stale_fingerprint", status=409)
    if row["status"] != "pending":
        if row["result_json"]:
            result = json.loads(row["result_json"])
            if result["delete_files"] != values["delete_files"]:
                raise ManagedMutationError("This removal already used a different file option.", status=409)
            return result
        raise ManagedMutationError(
            "Removal was interrupted or is running. Refresh the Library and inspect the files before a new removal; this request will not repeat deletion.",
            code="removal_interrupted", status=409)
    if (datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])).total_seconds() > 900:
        raise ManagedMutationError("Removal preview expired. Open a new preview.", code="preview_expired", status=409)
    library = _library(app)
    album = library.get_album(album_id)
    if album is None:
        raise ManagedMutationError("Managed album not found.", code="album_not_found", status=404)
    items = _album_items(album)
    _require_current_album(album, row["fingerprint"], items)
    plan = json.loads(row["plan_json"])
    current = _plan(app, album)
    if current != plan:
        raise ManagedMutationError("Files or ownership changed. Open a new removal preview.", code="stale_fingerprint", status=409)
    if values["delete_files"] and not plan["delete_allowed"]:
        raise ManagedMutationError(plan["delete_error"] or "No audio files to delete.", code="unsafe_deletion", status=409)
    # A durable single-use claim precedes any filesystem mutation. A lost response
    # replays the saved result; a crash never automatically resumes unlinking paths.
    with _connect(app.config["APP_DB"]) as db:
        claimed = db.execute("UPDATE managed_album_removals SET status='running' WHERE id=? AND status='pending'",
                             (row["id"],)).rowcount
    if claimed != 1:
        raise ManagedMutationError("Removal is already running.", code="removal_interrupted", status=409)
    removed, errors = [], []
    progress = {"status": "interrupted", "album_id": album_id, "delete_files": values["delete_files"],
                "removed": [], "remaining": [t["relative_path"] for t in plan["tracks"]], "errors": [],
                "message": "Removal was interrupted or is running. Inspect the Library and files before opening a new removal; this request will not repeat deletion."}
    def save_progress(attempting):
        progress["attempting"] = attempting
        progress["removed"] = [t["relative_path"] for t in removed]
        progress["remaining"] = [t["relative_path"] for t in plan["tracks"] if t not in removed]
        with _connect(app.config["APP_DB"]) as db:
            db.execute("UPDATE managed_album_removals SET result_json=? WHERE id=?",
                       (json.dumps(progress), row["id"]))
    save_progress(None)
    if not values["delete_files"]:
        with library.transaction():
            album.remove(delete=False)
        removed = plan["tracks"]
    else:
        for track, item in zip(plan["tracks"], items):
            file_deleted = False
            try:
                _safe_delete_path(Path(plan["root"]), track["path"])
                save_progress(track["relative_path"])
                # No Beets delete=True: that also prunes folders and can erase artwork.
                _unlink_reviewed(Path(plan["root"]), track)
                file_deleted = True
                with library.transaction():
                    item.remove(delete=False)
                removed.append(track)
                _reconcile(app, {**plan, "album_id": album_id}, [track])
                save_progress(None)
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append({"path": track["relative_path"], "file_deleted": file_deleted, "error": str(exc)})
                break
    _reconcile(app, {**plan, "album_id": album_id}, removed)
    if not values["delete_files"]:
        _queue_kept_tracks(app, plan)
    result = {"status": "partial" if errors else "complete", "album_id": album_id,
              "delete_files": values["delete_files"], "removed": [t["relative_path"] for t in removed],
              "remaining": [t["relative_path"] for t in plan["tracks"] if t not in removed], "errors": errors,
              "message": ("Some tracks remain managed. Refresh and review a new removal preview." if errors else
                          "Album removed. Audio files were deleted; artwork and folders were kept." if values["delete_files"] else
                          "Album removed from Cratekeep. Files and artwork were kept.")}
    with _connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE managed_album_removals SET status=?, result_json=?, inventory_reconciled=1 WHERE id=?",
                   (result["status"], json.dumps(result), row["id"]))
    return result


def register_routes(app) -> None:
    with _connect(app.config["APP_DB"]) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS managed_album_removals (
            id TEXT PRIMARY KEY, album_id INTEGER NOT NULL, fingerprint TEXT NOT NULL,
            plan_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            result_json TEXT, created_at TEXT NOT NULL)""")
        if "inventory_reconciled" not in {row["name"] for row in db.execute("PRAGMA table_info(managed_album_removals)")}:
            db.execute("ALTER TABLE managed_album_removals ADD COLUMN inventory_reconciled INTEGER NOT NULL DEFAULT 0")
        previous = db.execute("SELECT * FROM managed_album_removals WHERE status='complete' AND inventory_reconciled=0").fetchall()
    # Repair successful keep-files removals from earlier candidates on upgrade.
    # Existing managed paths are skipped so later re-adoption is never undone.
    for row in previous:
        result = json.loads(row["result_json"] or "{}")
        plan = json.loads(row["plan_json"])
        if result.get("delete_files") is not False or plan["root"] != str(Path(app.config["LIBRARY_PATH"]).resolve()):
            continue
        try:
            _queue_kept_tracks(app, plan)
        except (ManagedMutationError, OSError, ValueError, sqlite3.Error):
            app.logger.warning("Kept-file adoption reconciliation is pending; Library root/inventory needs review.")
            continue
        with _connect(app.config["APP_DB"]) as db:
            db.execute("UPDATE managed_album_removals SET inventory_reconciled=1 WHERE id=?", (row["id"],))

    @app.post("/api/library/albums/<int:album_id>/removal/preview")
    def preview_album_removal(album_id: int):
        values = request.get_json(silent=True)
        if (not isinstance(values, dict) or set(values) != {"expected_fingerprint"}
                or not isinstance(values["expected_fingerprint"], str)):
            return jsonify(error="expected_fingerprint is required", code="invalid_request"), 400
        album = _library(app).get_album(album_id)
        if album is None:
            return jsonify(error="Managed album not found", code="album_not_found"), 404
        try:
            _require_current_album(album, values["expected_fingerprint"], _album_items(album))
            plan = _plan(app, album)
        except ManagedMutationError as exc:
            return jsonify(error=str(exc), code=exc.code), exc.status
        except (OSError, ValueError) as exc:
            return jsonify(error=str(exc), code="managed_path_invalid"), 409
        preview_id = uuid.uuid4().hex
        with _connect(app.config["APP_DB"]) as db:
            db.execute("INSERT INTO managed_album_removals(id, album_id, fingerprint, plan_json, created_at) VALUES (?,?,?,?,?)",
                       (preview_id, album_id, values["expected_fingerprint"], json.dumps(plan), _now()))
        return jsonify(preview_id=preview_id, fingerprint=values["expected_fingerprint"], **plan), 201

    @app.post("/api/library/albums/<int:album_id>/removal/apply")
    def apply_album_removal(album_id: int):
        values = request.get_json(silent=True)
        if (not isinstance(values, dict) or set(values) != {"preview_id", "expected_fingerprint", "delete_files", "confirmed"}
                or not isinstance(values["preview_id"], str) or not isinstance(values["expected_fingerprint"], str)
                or not isinstance(values["delete_files"], bool) or values["confirmed"] is not True):
            return jsonify(error="A preview, fingerprint, boolean delete_files and confirmed: true are required", code="invalid_request"), 400
        try:
            return jsonify(_apply(app, album_id, values))
        except ManagedMutationError as exc:
            return jsonify(error=str(exc), code=exc.code), exc.status
        except (OSError, ValueError) as exc:
            return jsonify(error=str(exc), code="managed_path_invalid"), 409
