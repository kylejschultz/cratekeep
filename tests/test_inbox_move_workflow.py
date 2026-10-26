"""Copy lifecycle and compatibility with persisted import recovery records."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

import pytest
from beets.library import Library

from beets_mvp import create_app, LibraryImportExecutionError
from test_app import (make_app, tagged_inbox_wav, exact_candidate, jpeg_bytes,
                      removal_preview, removal_apply, approve_inbox_candidate,
                      complete_mock_inbox_move)


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', f"{Path(os.sys.executable).parent}:{os.environ.get('PATH','')}")
    return make_app(tmp_path)


def import_pair(app, tmp_path):
    folder = tmp_path / 'inbox/Artist/Album'
    sources = [folder / '01 First.wav', folder / '02 Second.wav']
    for number, (path, title) in enumerate(zip(sources, ['First', 'Second']), 1):
        tagged_inbox_wav(path, title=title, track=number)
    app.config['MUSICBRAINZ_PROVIDER'] = lambda query, *, limit: [exact_candidate(query)]
    client = app.test_client()
    review = client.get('/api/inbox/reviews').json['albums'][0]
    matched = client.post('/api/inbox/candidates', json={'album_id':review['id']}).json['album']
    base = f"/api/inbox/albums/{review['id']}"
    assert client.patch(base, json={'decision':'approved','candidate_id':matched['candidates'][0]['id']}).status_code == 200
    imported = client.post(base + '/execute', json={})
    assert imported.status_code == 201, imported.json
    return sources, review, imported.json


def test_copy_remove_keep_files_preserves_both_locations(app, tmp_path):
    sources, review, imported=import_pair(app,tmp_path)
    client=app.test_client();base=f"/api/inbox/{app.config['INBOXES'][0]['id']}"
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    before={path:path.read_bytes() for path in sources}
    targets=[Path(row['path']) for row in imported['items']]
    hashes={path:path.read_bytes() for path in targets}
    assert imported['copy_import'] and all(path.is_file() for path in sources+targets)
    album_id=next(iter(library.albums())).id
    assert removal_apply(client,album_id,removal_preview(client,album_id)).status_code==200
    assert not list(library.items())
    assert all(path.read_bytes()==data for path,data in {**before,**hashes}.items())
    # Keeping Library files is not authorization to delete either copy. The
    # Inbox receipt remains visible but refuses undo and source purge.
    assert client.post(base+'/scopes/refresh',json={}).status_code==200
    row=client.get(base+'/queue?status=imported').json['albums'][0]
    assert not row['imported']['undoable']
    assert client.post(base+'/actions/preview',json={'kind':'delete-imported','job_ids':[imported['id']]}).status_code==409
    assert all(path.read_bytes()==data for path,data in before.items())


def test_move_keeps_sidecars_hidden_and_unrelated_audio(app, tmp_path):
    folder = tmp_path/'inbox/Artist/Album';folder.mkdir(parents=True)
    cover = folder/'cover.jpg';cover.write_bytes(jpeg_bytes((11,22,33)))
    hidden = folder/'.keep';hidden.write_bytes(b'keep')
    sources, review, imported = import_pair(app,tmp_path)
    cover_bytes, hidden_bytes = cover.read_bytes(), hidden.read_bytes()
    other = tmp_path/'inbox/Other/Song.wav';tagged_inbox_wav(other,album='Other')
    original = other.read_bytes()
    client = app.test_client();inbox = app.config['INBOXES'][0]
    result = client.post(f"/api/inbox/{inbox['id']}/scopes/refresh",json={})
    assert result.status_code==200
    assert all(path.exists() for path in sources)
    assert cover.read_bytes()==cover_bytes and hidden.read_bytes()==hidden_bytes
    assert other.read_bytes()==original and folder.is_dir()


def test_copy_receipts_and_interrupted_action_survive_restart(app,tmp_path):
    sources,review,imported=import_pair(app,tmp_path)
    before={path:path.read_bytes() for path in sources}
    client=app.test_client();base=f"/api/inbox/{app.config['INBOXES'][0]['id']}"
    preview=client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[imported['id']]}).json
    with sqlite3.connect(app.config['APP_DB']) as db:
        manifest=db.execute('SELECT manifest_json FROM inbox_copy_imports WHERE job_id=?',(imported['id'],)).fetchone()[0]
        db.execute("UPDATE inbox_file_actions SET status='running' WHERE id=?",(preview['preview_id'],))
    restarted=create_app({'TESTING':True,'STATE_PATH':app.config['STATE_PATH'],'BROWSE_ROOTS':[str(tmp_path)]})
    client=restarted.test_client()
    assert client.post(base+'/scopes/refresh',json={}).status_code==200
    assert client.get(base+'/queue?status=imported').json['total']==1
    assert client.get(base+'/imports').json['incomplete_actions'][0]['status']=='running'
    assert client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[imported['id']]}).status_code==409
    assert all(path.read_bytes()==data for path,data in before.items())
    with sqlite3.connect(app.config['APP_DB']) as db:
        assert db.execute('SELECT manifest_json FROM inbox_copy_imports WHERE job_id=?',(imported['id'],)).fetchone()[0]==manifest


def test_copy_candidate_pending_artwork_recovers_without_consuming_original(app,tmp_path,monkeypatch):
    import beets_mvp
    app.config.update(FETCH_ART=True,ART_SIDECAR=True,ART_EMBED=False)
    source = tmp_path/'inbox/Album/01 Song.wav';tagged_inbox_wav(source)
    before = source.read_bytes();art=jpeg_bytes((40,50,60))
    client = app.test_client()
    review = approve_inbox_candidate(client,app,artwork={'available':True,'entity':'release','mbid':'123e4567-e89b-42d3-a456-426614174000'})
    app.config['ARTWORK_FETCHER'] = lambda candidate: art
    def run(current_app, selection, *, pretend=False):
        if not pretend: complete_mock_inbox_move(current_app,source,selection=selection)
        return subprocess.CompletedProcess(['beet'],0,'fixture','')
    monkeypatch.setattr(beets_mvp,'_run_beets_import',run)
    apply_sidecar = beets_mvp._apply_inbox_destination_sidecars
    def fail(*args,**kwargs):
        error=LibraryImportExecutionError('fixture failure',code='artwork_destination_failed')
        error.diagnostics={'rollback_errors':[],'rolled_back':[]}
        raise error
    monkeypatch.setattr(beets_mvp,'_apply_inbox_destination_sidecars',fail)
    base=f"/api/inbox/albums/{review['id']}"
    failed=client.post(base+'/execute',json={})
    assert failed.json['code']=='artwork_finalize_failed'
    source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(before)
    with sqlite3.connect(app.config['APP_DB']) as db:
        job=db.execute('SELECT last_execution_job_id FROM album_reviews WHERE id=?',(review['id'],)).fetchone()[0]
        summary=json.loads(db.execute('SELECT summary_json FROM adoption_jobs WHERE id=?',(job,)).fetchone()[0])
        summary['copy_import']=True
        db.executemany('INSERT OR REPLACE INTO app_settings(key,value) VALUES (?,?)',[('art_sidecar','1'),('fetch_art','1')])
        db.execute('UPDATE adoption_jobs SET summary_json=? WHERE id=?',(json.dumps(summary),job))
    restarted=create_app({'TESTING':True,'STATE_PATH':app.config['STATE_PATH'],'BROWSE_ROOTS':[str(tmp_path)]})
    monkeypatch.setattr(beets_mvp,'_apply_inbox_destination_sidecars',apply_sidecar)
    monkeypatch.setattr(beets_mvp,'_run_beets_import',lambda *args,**kwargs: pytest.fail('Recovery must not import audio again'))
    result=restarted.test_client().post(base+'/execute',json={})
    assert result.status_code==201,result.json
    assert source.read_bytes()==before
    target=tmp_path/'library/Artist/Album/01 Song.wav'
    assert target.is_file() and (target.parent/'cover.jpg').read_bytes()==art
    assert len(list(Library(restarted.config['BEETS_DB'],directory=restarted.config['LIBRARY_PATH']).items()))==1


def test_copy_candidate_duplicate_recovery_never_consumes_retained_sources(tmp_path,monkeypatch):
    from test_app import duplicate_inbox_fixture
    app,client,library,album,managed,sources,base=duplicate_inbox_fixture(tmp_path,'keep-existing',monkeypatch)
    before=sources[0].read_bytes();managed_bytes=managed.read_bytes()
    imported=client.post(base+'/execute',json={})
    assert imported.status_code==201,imported.json
    # Reconstruct a process interrupted after destination verification, before
    # completion. The saved copy result has no source-cleanup operations.
    with sqlite3.connect(app.config['APP_DB']) as db:
        db.execute("UPDATE album_reviews SET execution_status='cleanup-pending' WHERE id=?",(imported.json['album_review_id'],))
    restarted=create_app({'TESTING':True,'STATE_PATH':app.config['STATE_PATH'],'BROWSE_ROOTS':[str(tmp_path)]})
    monkeypatch.setattr('beets_mvp._run_beets_import',lambda *args,**kwargs: pytest.fail('Recovery must not import audio again'))
    recovered=restarted.test_client().post(base+'/execute',json={})
    assert recovered.status_code==201,recovered.json
    assert sources[0].read_bytes()==before and managed.read_bytes()==managed_bytes
    assert len(list(library.items()))==1
