"""Reviewed duplicate reconciliation: verify destinations before consuming sources."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from mediafile import MediaFile

from beets.library import Item

from .duplicates import duplicate_matches

from . import (
    LibraryImportExecutionError, _album_query, _attach_inbox_release_items, _candidate_item_values,
    _candidate_match, _complete_inbox_execution, _connect, _effective_candidate, _existing_release_album,
    _inbox_destination_preflight, _library, _managed_path, _now, _run_beets_import,
    _set_item_value, _validate_inventory_file, _validated_library_root, _verify_inbox_move,
)


def _identity(path: Path) -> dict:
    info = path.lstat()
    if path.is_symlink() or not path.is_file():
        raise OSError("Reviewed audio is no longer a regular file")
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"stat": [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns], "sha256": digest}


def _save(app, album_id, job_id, result, state):
    with _connect(app.config["APP_DB"]) as db:
        db.execute("UPDATE adoption_jobs SET summary_json=? WHERE id=?", (json.dumps(result), job_id))
        db.execute("UPDATE album_reviews SET execution_status=?, last_execution_job_id=? WHERE id=?",
                   (state, job_id, album_id))


def finish_cleanup(app, album, *, dry_run):
    with _connect(app.config["APP_DB"]) as db:
        row = db.execute("SELECT summary_json FROM adoption_jobs WHERE id=?", (album["last_execution_job_id"],)).fetchone()
    result = json.loads(row["summary_json"])
    job_id = result["id"]
    try:
        library = _library(app)
        for record in result["verified_destinations"]:
            item = library.get_item(record["beets_item_id"])
            if item is None or str(_managed_path(app, item.path)) != record["path"] or _identity(Path(record["path"])) != record["identity"]:
                raise OSError("A verified managed destination changed; incoming files were retained for inspection.")
        for record in result["cleanup"]:
            if record.get("cleaned"):
                continue
            root = Path(record["root"])
            path = Path(record["path"])
            if path.resolve() != path or root not in path.parents or path.is_symlink():
                raise OSError("An Inbox cleanup path became unsafe.")
            if path.exists() and _identity(path) != record["identity"]:
                raise OSError("A source file changed after destination verification; cleanup stopped.")
        if dry_run:
            return {**result, "status": "preview", "dry_run": True}
        for record in result["cleanup"]:
            if record.get("cleaned"):
                continue
            path = Path(record["path"])
            if path.exists():
                if _identity(path) != record["identity"]:
                    raise OSError("An incoming file changed during cleanup.")
                from .removal import _unlink_reviewed
                info = path.lstat()
                if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns] != record["identity"]["stat"]:
                    raise OSError("An incoming file changed at the cleanup boundary.")
                _unlink_reviewed(Path(record["root"]), {"path": str(path), "file_state":
                    [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]})
            record["cleaned"] = True
            _save(app, album["id"], job_id, result, "cleanup-pending")
    except (OSError, ValueError) as exc:
        raise LibraryImportExecutionError(
            f"Library destinations were verified, but Inbox cleanup is pending: {exc} Retry only resumes cleanup.",
            code="inbox_cleanup_failed", job_id=job_id,
        ) from exc
    result["message"] = ("Earlier copy-import recovery completed; retained Inbox originals were kept."
                         if result.get("copy_import") else
                         "Import reconciled and incoming audio removed; existing cover sidecars were preserved.")
    complete = _complete_inbox_execution(app, album["id"], job_id, result)
    shutil.rmtree(Path(app.config["STATE_PATH"]) / "duplicate-imports" / str(job_id), ignore_errors=True)
    return complete


def execute(app, album, rows, paths, provider, job_id, *, dry_run):
    """Keep existing bytes or replace a reviewed recording, adding distinct tracks via Beets."""
    library = _library(app)
    root = _validated_library_root(app)
    query = _album_query(app, album)
    provider = _effective_candidate(app, provider, query) if provider else None
    details = {d["position"]: d for d in _candidate_match(query, provider)["track_details"] if d.get("local") is not None} if provider else {}
    by_recording = {}
    managed_paths = {}
    for item in library.items():
        by_recording.setdefault(str(item.get("mb_trackid") or ""), []).append(item)
        managed_paths.setdefault(str(Path(os.fsdecode(item.path)).resolve()), []).append(item.id)
    duplicates, new = [], []
    seen = set()
    for position, (row, path) in enumerate(zip(rows, paths), 1):
        values = _candidate_item_values(provider, details.get(position)) if provider else {}
        recording = str(values.get("mb_trackid") or query["tracks"][position - 1].get("recording_id") or "")
        local = query["tracks"][position - 1]
        detail = details.get(position) or ({"proposed_position": [local.get("disc") or 1, local["track"]]} if local.get("track") else None)
        matches = duplicate_matches(by_recording.get(recording, []), recording, provider or query, detail)
        if len(matches) > 1 or (matches and matches[0].id in seen):
            raise LibraryImportExecutionError("Duplicate recording identity is ambiguous.", code="duplicate_ambiguous", job_id=job_id)
        entry = {"row": row, "path": path, "values": values, "position": position, "source_identity": _identity(path)}
        if not matches:
            new.append(entry)
            continue
        item = matches[0]
        seen.add(item.id)
        target = _managed_path(app, item.path)
        if managed_paths[str(target)] != [item.id] or target.stat().st_nlink != 1:
            raise LibraryImportExecutionError("A duplicate target has shared ownership.", code="duplicate_ambiguous", job_id=job_id)
        entry.update(item=item, target=target, target_identity=_identity(target), original_values=dict(item))
        if album["duplicate_action"] == "replace":
            destination = target.with_suffix(path.suffix.lower())
            if destination != target and destination.exists():
                raise LibraryImportExecutionError("Replacement destination already exists.", code="destination_collision", job_id=job_id)
            entry["destination"] = destination
        duplicates.append(entry)
    new_destinations = _inbox_destination_preflight(app, [e["row"] for e in new], [e["path"] for e in new], provider,
                                                   positions=[e["position"] for e in new]) if new else []
    targets = [str(root / path) for path in new_destinations] + [str(e.get("destination", e["target"])) for e in duplicates]
    if len(set(p.casefold() for p in targets)) != len(targets):
        raise LibraryImportExecutionError("Reviewed tracks resolve to conflicting destinations.", code="destination_collision", job_id=job_id)
    existing_album_id = _existing_release_album(app, provider) if new else None
    result = {"id": job_id, "album_review_id": album["id"], "status": "preview", "files": len(paths),
              "duplicate_action": album["duplicate_action"], "kept": len(duplicates) if album["duplicate_action"] == "keep-existing" else 0,
              "replaced": len(duplicates) if album["duplicate_action"] == "replace" else 0,
              "added": len(new), "items": [], "cleanup": [], "verified_destinations": [],
              "planned_destinations": targets, "source_paths": [str(p) for p in paths],
              "existing_item_ids": [e["item"].id for e in duplicates],
              "artwork": {"status": "preserved", "message": "Duplicate reconciliation preserves artwork; use Library artwork editing for changes."}}
    if dry_run:
        with _connect(app.config["APP_DB"]) as db:
            db.execute("UPDATE adoption_jobs SET status='complete', summary_json=?, finished_at=? WHERE id=?",
                       (json.dumps(result), _now(), job_id))
        return result
    workspace = Path(app.config["STATE_PATH"]) / "duplicate-imports" / str(job_id)
    workspace.mkdir(parents=True)
    staging = workspace / "new"
    staging.mkdir()
    from .inbox_imports import source_manifest
    result.update(copy_import=True, original_sources=source_manifest(app, album["id"]), new_sidecar_paths=[])
    result["status"] = "executing"
    _save(app, album["id"], job_id, result, "duplicate-executing")
    verified_new = []
    try:
        # Originals remain intact throughout preparation and Beets execution.
        for index, entry in enumerate([*new, *duplicates]):
            path = _validate_inventory_file(Path(album["root_path"]), entry["row"])
            if _identity(path) != entry["source_identity"]:
                raise OSError("Incoming audio changed after preview")
            staged = (staging if entry in new else workspace) / f"{index:04d}{path.suffix}"
            shutil.copy2(path, staged)
            if _identity(path) != entry["source_identity"] or _identity(staged)["sha256"] != entry["source_identity"]["sha256"]:
                raise OSError("Incoming audio changed while preparing its working copy")
            if "target" in entry and album["duplicate_action"] == "replace" and not app.config["ART_REPLACE"]:
                images = MediaFile(entry["target"]).images
                if images:
                    media = MediaFile(staged)
                    media.images = images
                    media.save()
            incoming = Item.from_path(staged)
            for field, value in entry["values"].items():
                _set_item_value(incoming, field, value)
            if entry["values"]:
                incoming.write()
            entry["staged"] = staged
            if "target" in entry:
                if _identity(entry["target"]) != entry["target_identity"]:
                    raise OSError("Managed duplicate changed before execution")
                backup = workspace / f"backup-{entry['item'].id}{entry['target'].suffix}"
                shutil.copy2(entry["target"], backup)
                entry["backup"] = backup
        if new:
            process = _run_beets_import(app, staging)
            if process.returncode:
                raise OSError(f"Beets could not import the nonduplicate tracks ({process.returncode})")
            verified_new = _verify_inbox_move(app, [e["staged"] for e in new], new_destinations)
        with library.transaction():
            for entry in duplicates:
                item = entry["item"]
                if _identity(entry["target"]) != entry["target_identity"]:
                    raise OSError("Managed duplicate changed during execution")
                if album["duplicate_action"] == "replace":
                    destination = entry["destination"]
                    # STATE_PATH and Library may be different mounts on Unraid.
                    # Copy beside the destination, then atomically replace there.
                    with tempfile.NamedTemporaryFile(prefix=".cratekeep-replace-", dir=destination.parent, delete=False) as handle:
                        temporary = Path(handle.name)
                    try:
                        shutil.copy2(entry["staged"], temporary)
                        prepared_identity = _identity(temporary)
                        os.replace(temporary, destination)
                        entry["written_identity"] = prepared_identity
                    finally:
                        temporary.unlink(missing_ok=True)
                    incoming = Item.from_path(destination)
                    values = dict(incoming)
                    values.update(id=item.id, album_id=item.album_id, added=item.added)
                    item.update(values)
                    item.store()
        for entry in duplicates:
            item = library.get_item(entry["item"].id)
            target = _managed_path(app, item.path)
            result["items"].append({"beets_item_id": item.id, "path": str(target)})
            if target != entry["target"]:
                result["cleanup"].append({"root": str(root), "path": str(entry["target"]), "identity": entry["target_identity"]})
        result["items"].extend(verified_new)
        if existing_album_id is not None:
            _attach_inbox_release_items(app, existing_album_id, verified_new, provider)
        for record in result["items"]:
            path = Path(record["path"])
            result["verified_destinations"].append({**record, "identity": _identity(path)})
        result["created_item_ids"] = [row["beets_item_id"] for row in verified_new]
        result["status"] = "cleanup-pending"
        _save(app, album["id"], job_id, result, "cleanup-pending")
    except Exception as exc:
        # Source files are untouched. Restore any replaced bytes; retain the
        # durable workspace if a process dies or restoration cannot complete.
        try:
            with library.transaction():
                for entry in duplicates:
                    if entry.get("written_identity"):
                        if _identity(entry["destination"]) != entry["written_identity"]:
                            raise LibraryImportExecutionError("Replacement changed during recovery; backups retained for inspection.", code="duplicate_execution_interrupted", job_id=job_id) from exc
                        shutil.copy2(entry["backup"], entry["target"])
                        if entry["destination"] != entry["target"]:
                            entry["destination"].unlink()
                        current = library.get_item(entry["item"].id)
                        current.update(entry["original_values"])
                        current.store()
        except Exception as rollback_error:
            raise LibraryImportExecutionError(
                f"Duplicate reconciliation and rollback were interrupted; Inbox originals and recovery backups were retained: {rollback_error}",
                code="duplicate_execution_interrupted", status=502, job_id=job_id,
            ) from exc
        raise LibraryImportExecutionError(
            f"Duplicate reconciliation stopped; original Inbox files and recovery backups were retained: {exc}. Inspect partial library state before retrying.",
            code="duplicate_execution_interrupted", status=502, job_id=job_id,
        ) from exc
    with _connect(app.config["APP_DB"]) as db:
        current = db.execute("SELECT * FROM album_reviews WHERE id=?", (album["id"],)).fetchone()
    return finish_cleanup(app, current, dry_run=False)
