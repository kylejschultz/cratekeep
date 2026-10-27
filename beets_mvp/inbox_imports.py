"""Retained-source import receipts and explicit previewed Inbox actions."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from datetime import datetime, timezone

from flask import jsonify, request


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS inbox_copy_imports (
        job_id INTEGER PRIMARY KEY, album_review_id INTEGER NOT NULL,
        source_id TEXT NOT NULL, source_root TEXT NOT NULL, library_root TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'imported', manifest_json TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS inbox_copy_source ON inbox_copy_imports(source_id,status)")
    db.execute("""CREATE TABLE IF NOT EXISTS inbox_file_actions (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL, kind TEXT NOT NULL,
        fingerprint TEXT NOT NULL, plan_json TEXT NOT NULL, status TEXT NOT NULL,
        result_json TEXT, created_at TEXT NOT NULL)""")


def _path(root, relative, *, artwork=False, ancillary=False):
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or not relative.parts or any(part in {'.', '..'} for part in relative.parts):
        raise ValueError('Unsafe file path; refresh before continuing.')
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Configured root is no longer a real directory.')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Symlinked files or directories cannot be changed.')
    if artwork:
        if relative.name != 'cover.jpg':
            raise ValueError('Unrecognized artwork path.')
    elif not ancillary:
        from . import AUDIO_EXTENSIONS
        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError('Only reviewed audio files can be changed.')
    return path


def identity(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('A file is not regular or has shared hard links.')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        after = os.fstat(handle.fileno())
    fields = lambda value: [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]
    if fields(info) != fields(before) or fields(before) != fields(after):
        raise ValueError('File changed while it was being verified.')
    return {'stat': fields(info), 'sha256': digest}


def _unchanged(root, record, *, full=True, artwork=False):
    try:
        path = _path(root, record['path'], artwork=artwork)
        if full:
            return identity(path) == record['identity']
        info = path.lstat()
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and [
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        ] == record['identity']['stat']
    except (OSError, ValueError):
        return False


def source_manifest(app, album_id):
    from . import _connect
    with _connect(app.config['APP_DB']) as db:
        album = db.execute('SELECT root_path FROM album_reviews WHERE id=?', (album_id,)).fetchone()
        rows = db.execute('''SELECT inventory.id,inventory.relative_path FROM album_review_tracks AS tracks
            JOIN library_inventory AS inventory ON inventory.id=tracks.inventory_id
            WHERE tracks.album_review_id=? ORDER BY inventory.relative_path COLLATE NOCASE''', (album_id,)).fetchall()
    return [{'inventory_id': row['id'], 'path': row['relative_path'],
             'identity': identity(_path(album['root_path'], row['relative_path']))} for row in rows]


def _item_values(item):
    return {field: item.get(field) for field in ('album_id','artist','albumartist','album','title','track','disc','genre','year','mb_albumid','mb_trackid')}


def record_completion(app, album, job_id, result, db):
    if not result.get('copy_import'):
        return
    if db.execute('SELECT 1 FROM inbox_copy_imports WHERE job_id=?', (job_id,)).fetchone():
        return
    from . import _library, _validated_library_root, _now
    root = _validated_library_root(app)
    library = _library(app)
    owned = set(result.get('created_item_ids', []))
    destinations = []
    for row in result['items']:
        item = library.get_item(row['beets_item_id'])
        if item is None:
            raise ValueError('Verified imported item disappeared before recording completion.')
        relative = Path(row['path']).relative_to(root).as_posix()
        destinations.append({'item_id': item.id, 'path': relative, 'identity': identity(_path(root, relative)),
                             'values': _item_values(item), 'owned': item.id in owned})
    artifacts = []
    for raw in result.get('new_sidecar_paths', []):
        relative = Path(raw).relative_to(root).as_posix()
        path = _path(root, relative, artwork=True)
        if path.is_file():
            artifacts.append({'path': relative, 'identity': identity(path)})
    manifest = {'sources': result['original_sources'], 'destinations': destinations, 'artifacts': artifacts,
                'undoable': result.get('duplicate_action') != 'replace',
                'artist': album['artist'], 'album': album['album']}
    db.execute('''INSERT INTO inbox_copy_imports(job_id,album_review_id,source_id,source_root,library_root,manifest_json,created_at)
        VALUES (?,?,?,?,?,?,?)''', (job_id, album['id'], album['source_id'], album['root_path'], str(root), json.dumps(manifest), _now()))


def _receipts(app, inbox):
    from . import _connect
    root = str(Path(inbox['path']).resolve())
    with _connect(app.config['APP_DB']) as db:
        return [dict(row) for row in db.execute('''SELECT * FROM inbox_copy_imports
            WHERE source_id=? AND source_root=? AND status='imported' ORDER BY job_id DESC''', (inbox['id'], root))]


def retained_ids(app, inbox):
    result = set()
    for receipt in _receipts(app, inbox):
        for source in json.loads(receipt['manifest_json'])['sources']:
            if not source.get('deleted') and _unchanged(receipt['source_root'], source, full=False):
                result.add(source['inventory_id'])
    return result


def _undo_reason(app, receipt, manifest, *, full=False):
    from . import _library, _validated_library_root
    if not manifest['undoable']:
        return 'Replacement imports cannot be undone.'
    if str(_validated_library_root(app)) != receipt['library_root']:
        return 'The Library root changed.'
    if not all(not row.get('deleted') and _unchanged(receipt['source_root'], row, full=full) for row in manifest['sources']):
        return 'Original Inbox files were deleted or changed.'
    library = _library(app)
    from . import _connect
    owned = {row['item_id'] for row in manifest['destinations'] if row['owned'] and not row.get('removed')}
    with _connect(app.config['APP_DB']) as db:
        others = db.execute("SELECT manifest_json FROM inbox_copy_imports WHERE job_id!=? AND status IN ('imported','purged')", (receipt['job_id'],)).fetchall()
    if any(row['item_id'] in owned and not row.get('removed') for other in others
           for row in json.loads(other['manifest_json'])['destinations']):
        return 'A later import uses these Library copies; undo that import first.'
    owners = None
    for row in manifest['destinations']:
        if not row['owned'] or row.get('removed'):
            continue
        item = library.get_item(row['item_id'])
        if (item is None or _item_values(item) != row['values']
                or str(Path(os.fsdecode(item.path)).resolve()) != str(Path(receipt['library_root']) / row['path'])
                or not _unchanged(receipt['library_root'], row, full=full)):
            return 'An imported Library file or its metadata changed; inspect it before undoing.'
        # Queue display is provisional. Full ownership verification belongs to
        # the confirmed-action preview/apply path, not every track on page load.
        if full:
            if owners is None:
                owners = {}
                for other in library.items():
                    owners.setdefault(Path(os.fsdecode(other.path)).resolve(), []).append(other.id)
            if owners.get(Path(os.fsdecode(item.path)).resolve()) != [item.id]:
                return 'An imported Library path has shared ownership.'
    return ''


def _roots(app, inbox):
    from . import _validated_library_root
    configured = Path(inbox['path'])
    if configured.is_symlink() or not configured.is_dir():
        raise ValueError('The Inbox root changed or is unavailable.')
    root, library = configured.resolve(), _validated_library_root(app)
    if root == library or root in library.parents or library in root.parents:
        raise ValueError('Inbox and Library roots overlap.')
    return root, library



def _unimported_plan(app, inbox, values, source_root, library_root):
    from . import _connect, _scope_for_relative_path, _inbox_album_folder
    kind = values['kind']
    track_only = kind == 'delete-track'
    expected = {'kind','album_id','track_id'} if track_only else {'kind','album_ids'}
    if set(values) != expected:
        raise ValueError('Select explicit Inbox albums or one track; paths are not accepted.')
    ids = [values['album_id']] if track_only else values['album_ids']
    if (not isinstance(ids,list) or not ids or len(ids)>1000
            or any(type(value) is not int or value<=0 for value in ids)
            or (track_only and (type(values['track_id']) is not int or values['track_id']<=0))):
        raise ValueError('Select current Inbox albums and tracks.')
    writes, folders, versions, names = [], [], {}, []
    retained_paths = {source['path'] for receipt in _receipts(app,inbox)
                      for source in json.loads(receipt['manifest_json'])['sources'] if not source.get('deleted')}
    with _connect(app.config['APP_DB']) as db:
        _check_import_idle(db, source_root)
        for album_id in sorted(set(ids)):
            album = db.execute("SELECT * FROM album_reviews WHERE id=? AND source_kind='inbox' AND source_id=? AND root_path=?",
                               (album_id,inbox['id'],str(source_root))).fetchone()
            if album is None or album['execution_status'] != 'not-run':
                raise ValueError('Only unimported albums can be deleted here; finish import recovery or use Remove imported.')
            rows = db.execute("""SELECT inventory.* FROM album_review_tracks tracks
                JOIN library_inventory inventory ON inventory.id=tracks.inventory_id
                WHERE tracks.album_review_id=? ORDER BY inventory.relative_path COLLATE NOCASE""",(album_id,)).fetchall()
            if not rows or any(not row['present'] or row['beets_item_id'] is not None
                               or row['source_kind']!='inbox' or row['source_id']!=inbox['id']
                               or row['root_path']!=str(source_root) or row['relative_path'] in retained_paths for row in rows):
                raise ValueError('Album ownership changed; refresh Inbox before deleting.')
            selected = [row for row in rows if not track_only or row['id']==values['track_id']]
            if not selected:
                raise ValueError('That track does not belong to this Inbox album.')
            if any(db.execute('''SELECT 1 FROM album_review_tracks tracks JOIN album_reviews albums ON albums.id=tracks.album_review_id
                       WHERE tracks.inventory_id=? AND albums.id!=? AND albums.execution_status!='complete' LIMIT 1''',
                       (row['id'],album_id)).fetchone() for row in selected):
                raise ValueError('An Inbox file has shared review ownership; refresh and inspect it before deleting.')
            versions[str(album_id)] = {'execution_status':album['execution_status'],
                                      'last_execution_job_id':album['last_execution_job_id'],
                                      'members':[row['id'] for row in rows]}
            names.append(album['album'])
            for row in selected:
                relative=row['relative_path']
                writes.append({'root':str(source_root),'path':relative,'location':'Inbox',
                               'identity':identity(_path(source_root,relative)), 'album_id':album_id, 'inventory_id':row['id']})
                folder=_inbox_album_folder(relative)
                if folder is not None:
                    folders.append((Path(folder),album_id))
    if not track_only:
        companions=_folder_companions(source_root,folders,{row['path'] for row in writes})
        for companion in companions:
            companion.pop('job_id')
        # Keep audio/queue rows available if an accompanying-file removal fails.
        writes=companions+writes
    if len({row['path'] for row in writes})!=len(writes):
        raise ValueError('The selection contains shared file ownership.')
    return {'kind':kind,'source_id':inbox['id'],'source_root':str(source_root),'library_root':str(library_root),
            'request':values,'job_ids':[],'versions':{},'album_versions':versions,'albums':names,
            'files':writes,'library_guards':[],
            'refresh_scopes':sorted({_scope_for_relative_path(row['path']) for row in writes})}


def _check_import_idle(db, root):
    if db.execute("SELECT 1 FROM adoption_jobs WHERE root_path=? AND status='running' AND kind IN ('inbox_import_execute','inbox_import_preview')",(str(root),)).fetchone():
        raise ValueError('An Inbox import is running or interrupted; inspect it before deleting.')


def _plan(app, inbox, values):
    from . import _connect, _library, _scope_for_relative_path, _inbox_album_folder
    source_root, library_root = _roots(app, inbox)
    with _connect(app.config['APP_DB']) as db:
        if db.execute("SELECT 1 FROM inbox_file_actions WHERE source_id=? AND status='running'", (inbox['id'],)).fetchone():
            raise ValueError('An earlier Inbox action was interrupted; inspect its recorded result before further deletion.')
    kind = values.get('kind')
    if kind in {'delete-albums','delete-track'}:
        return _unimported_plan(app,inbox,values,source_root,library_root)
    if kind not in {'undo','delete-imported'}:
        raise ValueError('Unknown Inbox action.')
    if set(values) - {'kind','job_ids','scopes','excluded_album_ids'}:
        raise ValueError('Unexpected Inbox action fields.')
    receipts = _receipts(app, inbox)
    by_id = {row['job_id']: row for row in receipts}
    writes, versions, guards = [], {}, []
    receipt_ids = []
    refresh_scopes = set()
    folders = []
    if kind == 'delete-imported' and 'job_ids' not in values:
        if set(values) != {'kind'}:
            raise ValueError('Delete imported does not accept a selection.')
        receipt_ids = list(by_id)
    elif 'job_ids' in values:
        ids = values['job_ids']
        if not isinstance(ids, list) or not ids or any(type(value) is not int or value not in by_id for value in ids):
            raise ValueError('Select current imported jobs from this Inbox.')
        receipt_ids = list(dict.fromkeys(ids))
        if set(values) != {'kind','job_ids'}:
            raise ValueError('Imported jobs cannot be combined with folder selection.')
    else:
        raise ValueError('Choose an imported job to undo.')
    library = _library(app)
    for job_id in receipt_ids:
        receipt = by_id[job_id]
        manifest = json.loads(receipt['manifest_json'])
        versions[str(job_id)] = receipt['manifest_json']
        refresh_scopes.update(_scope_for_relative_path(row['path']) for row in manifest['sources'] if not row.get('deleted'))
        if kind == 'undo':
            reason = _undo_reason(app, receipt, manifest, full=True)
            if reason:
                raise ValueError(reason)
            owned_paths = set()
            for index, row in enumerate(manifest['destinations']):
                if row['owned'] and not row.get('removed'):
                    owned_paths.add(Path(row['path']))
                    writes.append({'root': receipt['library_root'], **row, 'job_id': job_id,
                                   'destination_index': index, 'location':'Library'})
            # Preserve artwork if any other managed or unmanaged audio shares it.
            from . import AUDIO_EXTENSIONS
            for index, row in enumerate(manifest['artifacts']):
                if row.get('removed') or not _unchanged(receipt['library_root'], row, full=True, artwork=True):
                    continue
                folder = Path(receipt['library_root']) / Path(row['path']).parent
                others = [path for path in folder.iterdir() if path.suffix.lower() in AUDIO_EXTENSIONS
                          and path.relative_to(receipt['library_root']) not in owned_paths]
                if not others:
                    writes.append({'root':receipt['library_root'], **row, 'job_id':job_id,
                                   'artifact_index':index, 'location':'Library artwork', 'artifact':True})
        else:
            # Match the queue's physical album grouping, including CD/Disc folders.
            folders.extend((Path(folder), job_id) for folder in sorted({
                _inbox_album_folder(source['path']) for source in manifest['sources']
            } - {None}))
            if receipt['library_root'] != str(library_root):
                raise ValueError('The Library root changed; inspect this import before deleting originals.')
            if kind == 'delete-imported':
                for destination in manifest['destinations']:
                    item = library.get_item(destination['item_id'])
                    if (item is None or Path(os.fsdecode(item.path)).resolve() != library_root / destination['path']
                            or not _path(library_root, destination['path']).is_file()):
                        raise ValueError('A Library copy is missing or unmanaged; keep its Inbox original and inspect the import.')
                    guards.append({'job_id':job_id,'item_id':item.id,'path':destination['path'], 'identity':identity(_path(library_root, destination['path']))})
            for index, source in enumerate(manifest['sources']):
                if source.get('deleted'):
                    continue
                if not _unchanged(receipt['source_root'], source, full=True):
                    raise ValueError('An imported Inbox original changed or is missing; refresh and review it instead.')
                writes.append({'root': receipt['source_root'], **source, 'job_id':job_id,
                               'source_index':index, 'location':'Inbox'})
    if kind == 'delete-imported':
        companions = _folder_companions(source_root, folders, {row['path'] for row in writes})
        writes.extend(companions)
        refresh_scopes.update(_scope_for_relative_path(row['path']) for row in companions)
    if not writes and not (kind == 'undo' and receipt_ids):
        raise ValueError('No eligible files are selected.')
    paths = [(row['root'],row['path']) for row in writes]
    if len(set(paths)) != len(paths):
        raise ValueError('The selection contains shared file ownership.')
    return {'kind':kind, 'source_id':inbox['id'], 'source_root':str(source_root), 'library_root':str(library_root),
            'request':values, 'job_ids':receipt_ids, 'versions':versions, 'files':writes, 'library_guards':guards,
            'refresh_scopes':sorted(refresh_scopes)}


def _folder_companions(root, folders, sources):
    """Snapshot complete album folders, without expanding into shared audio."""
    from . import AUDIO_EXTENSIONS
    def walk_error(error):
        raise error

    companions = []
    covered = set()
    for folder, job_id in sorted(folders, key=lambda row: (len(row[0].parts), str(row[0]), row[1])):
        # Files stored directly in Inbox never authorize clearing the Inbox root.
        if folder == Path('.') or any(parent == folder or parent in folder.parents for parent in covered):
            continue
        directory = _path(root, folder.as_posix(), ancillary=True)
        if not directory.exists():  # A completed portion of a partial action may already be pruned.
            continue
        files = []
        safe = True
        for current, directories, names in os.walk(directory, followlinks=False, onerror=walk_error):
            directories.sort()
            if any((Path(current) / name).is_symlink() for name in directories):
                raise ValueError('Symlinked album folders cannot be cleared.')
            for name in sorted(names):
                relative = (Path(current) / name).relative_to(root).as_posix()
                path = _path(root, relative, ancillary=True)
                if path.suffix.lower() in AUDIO_EXTENSIONS:
                    if relative not in sources:
                        safe = False
                        break
                else:
                    files.append({'root':str(root), 'path':relative, 'identity':identity(path),
                                  'job_id':job_id, 'location':'Inbox accompanying file', 'ancillary':True})
            if not safe:
                break
        if safe:
            companions.extend(files)
            covered.add(folder)
    return companions


def _digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def _apply(app, inbox, values):
    from . import _connect, _library, _now, _scan_inbox_inventory, _cleanup_inbox_directories
    from .removal import _unlink_reviewed, _reconcile
    if set(values) != {'preview_id','expected_fingerprint','confirmed'} or values['confirmed'] is not True:
        raise ValueError('A confirmed preview and expected fingerprint are required.')
    with _connect(app.config['APP_DB']) as db:
        row = db.execute('SELECT * FROM inbox_file_actions WHERE id=? AND source_id=?', (values['preview_id'],inbox['id'])).fetchone()
    if row is None or row['fingerprint'] != values['expected_fingerprint']:
        raise ValueError('Action preview is missing or changed.')
    if row['status'] == 'complete':
        return json.loads(row['result_json'])
    if row['status'] != 'pending':
        raise ValueError('The earlier action was interrupted or partial; inspect its result before opening a fresh preview.')
    if (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds() > 900:
        raise ValueError('Action preview expired; open a fresh preview.')
    saved = json.loads(row['plan_json'])
    current = _plan(app, inbox, saved['request'])
    if _digest(current) != row['fingerprint']:
        raise ValueError('Files, membership or import receipts changed; open a fresh preview.')
    result = {'status':'running', 'removed':[], 'errors':[], 'kind':saved['kind']}
    with _connect(app.config['APP_DB']) as db:
        # Serialize the claim across workers too: two different previews must
        # not undo a Library copy and purge its original simultaneously.
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM inbox_file_actions WHERE status='running'").fetchone():
            raise ValueError('Another Inbox file action is running; wait for its result before continuing.')
        if current.get('album_versions'):
            _check_import_idle(db, current['source_root'])
            for album_id, version in current['album_versions'].items():
                album=db.execute('SELECT execution_status,last_execution_job_id FROM album_reviews WHERE id=?',(int(album_id),)).fetchone()
                members=[row['inventory_id'] for row in db.execute('''SELECT tracks.inventory_id FROM album_review_tracks tracks
                    JOIN library_inventory inventory ON inventory.id=tracks.inventory_id
                    WHERE tracks.album_review_id=? ORDER BY inventory.relative_path COLLATE NOCASE''',(int(album_id),))]
                if (album is None or album['execution_status']!=version['execution_status']
                        or album['last_execution_job_id']!=version['last_execution_job_id'] or members!=version['members']):
                    raise ValueError('Album changed while waiting; open a fresh deletion preview.')
        for job_id, version in current['versions'].items():
            receipt = db.execute("SELECT manifest_json,status FROM inbox_copy_imports WHERE job_id=?", (int(job_id),)).fetchone()
            if receipt is None or receipt['status'] != 'imported' or receipt['manifest_json'] != version:
                raise ValueError('An import changed while waiting for this action; open a fresh preview.')
        if db.execute("UPDATE inbox_file_actions SET status='running',result_json=? WHERE id=? AND status='pending'",
                      (json.dumps(result),row['id'])).rowcount != 1:
            raise ValueError('This action is already running.')
    library = _library(app)
    for target in current['files']:
        file_deleted = False
        try:
            path = _path(target['root'], target['path'], artwork=target.get('artifact',False), ancillary=target.get('ancillary',False))
            if identity(path) != target['identity']:
                raise ValueError('File changed at the deletion boundary.')
            if 'item_id' in target:
                item = library.get_item(target['item_id'])
                if item is None or _item_values(item) != target['values']:
                    raise ValueError('Managed item changed at the deletion boundary.')
            # _plan fully hashed the copies. Recheck their no-follow stat
            # fingerprints here without rereading every album for every unlink.
            for guard in current.get('library_guards', []):
                if not target.get('ancillary') and guard['job_id'] != target['job_id']:
                    continue
                managed = library.get_item(guard['item_id'])
                if (managed is None or Path(os.fsdecode(managed.path)).resolve() != Path(current['library_root']) / guard['path']
                        or not _unchanged(current['library_root'], guard, full=False)):
                    raise ValueError('A Library destination changed; Inbox originals were kept.')
            _unlink_reviewed(Path(target['root']), {'path':str(path), 'file_state':target['identity']['stat']})
            file_deleted = True
            if 'item_id' in target:
                album_id = item.album_id
                item.remove(delete=False)
                _reconcile(app, {'root':target['root'], 'album_id':album_id}, [{'id':item.id}])
            if 'job_id' in target and not target.get('ancillary'):
                with _connect(app.config['APP_DB']) as db:
                    receipt = db.execute('SELECT manifest_json FROM inbox_copy_imports WHERE job_id=?', (target['job_id'],)).fetchone()
                    manifest = json.loads(receipt['manifest_json'])
                    if 'source_index' in target:
                        manifest['sources'][target['source_index']]['deleted'] = True
                    elif 'destination_index' in target:
                        manifest['destinations'][target['destination_index']]['removed'] = True
                    else:
                        manifest['artifacts'][target['artifact_index']]['removed'] = True
                    db.execute('UPDATE inbox_copy_imports SET manifest_json=? WHERE job_id=?', (json.dumps(manifest),target['job_id']))
            result['removed'].append({'path':target['path'], 'location':target['location']})
        except Exception as exc:
            result['errors'].append({'path':target['path'], 'error':str(exc), 'file_deleted':file_deleted})
            break
        finally:
            with _connect(app.config['APP_DB']) as db:
                db.execute('UPDATE inbox_file_actions SET result_json=? WHERE id=?', (json.dumps(result),row['id']))
    if not result['errors']:
        with _connect(app.config['APP_DB']) as db:
            for job_id in current['job_ids']:
                db.execute('UPDATE inbox_copy_imports SET status=? WHERE job_id=?',
                           ('undone' if current['kind']=='undo' else 'purged',job_id))
                if current['kind'] == 'undo':
                    receipt = db.execute('SELECT album_review_id,manifest_json FROM inbox_copy_imports WHERE job_id=?', (job_id,)).fetchone()
                    db.execute("UPDATE album_reviews SET execution_status='not-run', execution_error='', last_execution_job_id=NULL, executed_at=NULL WHERE id=?", (receipt['album_review_id'],))
                    db.executemany("UPDATE adoption_reviews SET imported_at=NULL WHERE inventory_id=?",
                                   [(source['inventory_id'],) for source in json.loads(receipt['manifest_json'])['sources']])
    removed={(entry['location'],entry['path']) for entry in result['removed']}
    result['remaining']=[{'path':entry['path'],'location':entry['location']} for entry in current['files']
                         if (entry['location'],entry['path']) not in removed]
    result['status'] = 'partial' if result['errors'] else 'complete'
    try:
        _scan_inbox_inventory(app, inbox, scopes=current['refresh_scopes'])
        result['directory_cleanup'] = _cleanup_inbox_directories(app,inbox,scopes=current['refresh_scopes'])
    except (OSError,ValueError) as exc:
        result['errors'].append({'error':f'Files processed, but Inbox refresh failed: {exc}'})
        result['status'] = 'partial'
    result['message'] = ('Action partially completed; inspect the listed errors.' if result['errors'] else
                         'Import undone; originals are ready in Inbox.' if current['kind']=='undo' else
                         'Selected Inbox files permanently deleted without importing. Library files were kept.' if current['kind'] in {'delete-albums','delete-track'} else
                         'Selected Inbox originals deleted; Library files were kept.')
    with _connect(app.config['APP_DB']) as db:
        db.execute('UPDATE inbox_file_actions SET status=?,result_json=? WHERE id=?', (result['status'],json.dumps(result),row['id']))
    return result


def register_routes(app):
    from . import _configured_inbox, _connect, _now

    @app.get('/api/inbox/<inbox_id>/imports')
    def copied_imports(inbox_id):
        inbox = _configured_inbox(app,inbox_id)
        if inbox is None:
            return jsonify(error='Unknown Inbox.'),404
        try:
            _roots(app,inbox)
            page = max(1,int(request.args.get('page',1)))
            receipts = _receipts(app,inbox)
            page_count = max(1,(len(receipts)+24)//25); page = min(page,page_count)
            rows = []
            for receipt in receipts[(page-1)*25:page*25]:
                manifest = json.loads(receipt['manifest_json'])
                reason = _undo_reason(app,receipt,manifest)
                rows.append({'job_id':receipt['job_id'],'artist':manifest['artist'],'album':manifest['album'],
                             'sources':[row['path'] for row in manifest['sources'] if not row.get('deleted')],
                             'destinations':[row['path'] for row in manifest['destinations']],
                             'undoable':not reason,'undo_reason':reason,'created_at':receipt['created_at']})
            with _connect(app.config['APP_DB']) as db:
                incomplete = []
                for action in db.execute("SELECT id,status,result_json,plan_json FROM inbox_file_actions WHERE source_id=? AND status IN ('running','partial') ORDER BY created_at DESC LIMIT 10", (inbox_id,)):
                    result = json.loads(action['result_json'] or '{}')
                    incomplete.append({'id':action['id'], 'status':action['status'],
                                       'removed':result.get('removed',[]), 'errors':result.get('errors',[]),
                                       'planned_files':[{'path':row['path'],'location':row['location']} for row in json.loads(action['plan_json'])['files']]})
            return jsonify(imports=rows,page=page,page_count=page_count,total=len(receipts),incomplete_actions=incomplete)
        except (ValueError,OSError) as exc:
            return jsonify(error=str(exc)),409

    @app.get('/api/inbox/<inbox_id>/actions/<preview_id>')
    def inbox_action_result(inbox_id, preview_id):
        if _configured_inbox(app,inbox_id) is None:
            return jsonify(error='Unknown Inbox.'),404
        with _connect(app.config['APP_DB']) as db:
            row = db.execute('SELECT status,result_json FROM inbox_file_actions WHERE id=? AND source_id=?', (preview_id,inbox_id)).fetchone()
        if row is None:
            return jsonify(error='Unknown Inbox action.'),404
        response = jsonify(status=row['status'],result=json.loads(row['result_json'] or '{}'))
        response.headers['Cache-Control'] = 'no-store'
        return response

    def action(inbox_id, apply):
        inbox = _configured_inbox(app,inbox_id)
        if inbox is None:
            return jsonify(error='Unknown Inbox.'),404
        locks=[]
        for lock in (app.config['INBOX_CANDIDATE_LOCK'], app.config['INBOX_EXECUTION_LOCK']):
            if not lock.acquire(blocking=False):
                for held in reversed(locks): held.release()
                return jsonify(error='Inbox is busy; retry after matching/import finishes.'),409
            locks.append(lock)
        try:
            values=request.get_json(silent=True)
            if not isinstance(values,dict): raise ValueError('Expected a JSON object.')
            if apply:
                result=_apply(app,inbox,values)
                return jsonify(result),200 if result['status']=='complete' else 207
            plan=_plan(app,inbox,values)
            token=str(uuid.uuid4()); fingerprint=_digest(plan)
            with _connect(app.config['APP_DB']) as db:
                db.execute('INSERT INTO inbox_file_actions(id,source_id,kind,fingerprint,plan_json,status,created_at) VALUES (?,?,?,?,?,?,?)',
                           (token,inbox_id,plan['kind'],fingerprint,json.dumps(plan),'pending',_now()))
            return jsonify(preview_id=token,fingerprint=fingerprint,kind=plan['kind'],albums=plan.get('albums',[]),files=[
                {'path':row['path'],'location':row['location']} for row in plan['files']],count=len(plan['files']))
        except (ValueError,OSError) as exc:
            return jsonify(error=str(exc),code='inbox_action_blocked'),409
        finally:
            for lock in reversed(locks): lock.release()

    app.add_url_rule('/api/inbox/<inbox_id>/actions/preview','preview_inbox_action',lambda inbox_id:action(inbox_id,False),methods=['POST'])
    app.add_url_rule('/api/inbox/<inbox_id>/actions/apply','apply_inbox_action',lambda inbox_id:action(inbox_id,True),methods=['POST'])


def import_state(app, album_id):
    from . import _connect
    with _connect(app.config['APP_DB']) as db:
        row = db.execute("SELECT * FROM inbox_copy_imports WHERE album_review_id=? AND status='imported' ORDER BY job_id DESC LIMIT 1", (album_id,)).fetchone()
    if row is None:
        return None
    receipt = dict(row)
    manifest = json.loads(receipt['manifest_json'])
    try:
        reason = _undo_reason(app, receipt, manifest)
    except (OSError, ValueError):
        reason = 'An import root or file is unavailable; inspect it before undoing.'
    return {'job_id': receipt['job_id'], 'undoable': not reason, 'undo_reason': reason,
            'created_at': receipt['created_at'], 'library_album_id': next((row['values'].get('album_id') for row in manifest['destinations']), None),
            'originals': [row['path'] for row in manifest['sources'] if not row.get('deleted')]}
