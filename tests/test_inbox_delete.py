"""Inbox-only deletion uses generated files and never imports or mutates Library."""
import json
from pathlib import Path

import pytest
from beets.library import Library
from beets_mvp import _connect
from test_app import tagged_inbox_wav, exact_candidate
from test_inbox_copies import app, copied, imports_base, action


def queued(app,tmp_path,*,folder='Bad download',count=2):
    sources=[tmp_path/'inbox'/folder/f'{index}.wav' for index in range(1,count+1)]
    for index,path in enumerate(sources,1):
        tagged_inbox_wav(path,title=f'Song {index}',track=index)
    client=app.test_client();base=imports_base(app)
    assert client.post(base+'/scopes/refresh',json={}).status_code==200
    album=next(row for row in client.get(base+'/queue').json['albums'] if any(t['path']==sources[0].relative_to(tmp_path/'inbox').as_posix() for t in row['tracks']))
    return client,base,album,sources


def test_delete_unmatched_album_and_companions_without_import(app,tmp_path):
    client,base,album,sources=queued(app,tmp_path)
    for relative in ['album.nfo','folder.jpg','artwork/back.jpg']:
        path=sources[0].parent/relative;path.parent.mkdir(exist_ok=True);path.write_bytes(b'junk')
    sibling=tmp_path/'inbox/Other/keep.txt';sibling.parent.mkdir();sibling.write_bytes(b'keep')
    preview,done,values=action(client,base,{'kind':'delete-albums','album_ids':[album['id']]})
    assert preview['count']==5 and preview['albums']==[album['album']]
    assert done['remaining']==[] and not sources[0].parent.exists()
    assert sibling.read_bytes()==b'keep' and client.get(base+'/queue').json['total']==0
    assert not list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items())
    with _connect(app.config['APP_DB']) as db:
        assert db.execute("SELECT count(*) FROM adoption_jobs WHERE kind LIKE 'inbox_import%'").fetchone()[0]==0
    assert client.post(base+'/actions/apply',json=values).json==done


def test_delete_one_track_preserves_other_files_and_invalidates_match(app,tmp_path):
    client,base,album,sources=queued(app,tmp_path)
    companion=sources[0].parent/'album.nfo';companion.write_bytes(b'keep')
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,limit:[exact_candidate(query,'123e4567-e89b-42d3-a456-426614174000')]
    response=client.post('/api/inbox/candidates',json={'album_id':album['id']})
    assert response.status_code==201,response.json
    before=sources[1].read_bytes()
    preview,_,_=action(client,base,{'kind':'delete-track','album_id':album['id'],'track_id':album['tracks'][0]['id']})
    assert preview['count']==1 and not sources[0].exists()
    assert sources[1].read_bytes()==before and companion.read_bytes()==b'keep'
    remaining=client.get(base+'/queue').json['albums'][0]
    assert len(remaining['tracks'])==1 and remaining['candidate_status']=='stale'
    blocked=client.post(base+'/bulk/preview',json={'mode':'selected','album_ids':[remaining['id']],'scopes':['Bad download']})
    assert not blocked.json['importable_albums']


@pytest.mark.parametrize('selection',[
    {'kind':'delete-albums'}, {'kind':'delete-albums','album_ids':[]},
    {'kind':'delete-albums','album_ids':[True]}, {'kind':'delete-albums','album_ids':[999]},
    {'kind':'delete-track','album_id':1,'track_id':999},
    {'kind':'delete-track','album_id':1,'track_id':'../outside.wav'},
    {'kind':'delete-albums','album_ids':[1],'path':'../outside'},
])
def test_delete_requires_current_explicit_owned_ids(app,tmp_path,selection):
    client,base,_,sources=queued(app,tmp_path)
    assert client.post(base+'/actions/preview',json=selection).status_code==409
    assert all(path.is_file() for path in sources)


@pytest.mark.parametrize('state',['complete','failed','artwork-pending','cleanup-pending','duplicate-executing'])
def test_delete_refuses_imported_or_recovery_state(app,tmp_path,state):
    client,base,album,sources=queued(app,tmp_path)
    with _connect(app.config['APP_DB']) as db:
        db.execute('UPDATE album_reviews SET execution_status=? WHERE id=?',(state,album['id']))
    assert client.post(base+'/actions/preview',json={'kind':'delete-albums','album_ids':[album['id']]}).status_code==409
    assert all(path.is_file() for path in sources)


def test_delete_cannot_bypass_import_receipt(app,tmp_path):
    source,before,result,album=copied(app,tmp_path)
    with _connect(app.config['APP_DB']) as db:
        db.execute("UPDATE album_reviews SET execution_status='not-run' WHERE id=?",(album['id'],))
    response=app.test_client().post(imports_base(app)+'/actions/preview',json={'kind':'delete-albums','album_ids':[album['id']]})
    assert response.status_code==409 and source.read_bytes()==before
    assert Path(result['items'][0]['path']).is_file()


@pytest.mark.parametrize('change',['source','companion','symlink','hardlink','ownership','import'])
def test_delete_revalidates_preview_before_any_unlink(app,tmp_path,change):
    client,base,album,sources=queued(app,tmp_path)
    companion=sources[0].parent/'album.nfo';companion.write_bytes(b'info')
    preview=client.post(base+'/actions/preview',json={'kind':'delete-albums','album_ids':[album['id']]}).json
    if change=='source': sources[0].write_bytes(b'changed')
    elif change=='companion': companion.write_bytes(b'changed')
    elif change=='symlink':
        companion.unlink();companion.symlink_to(sources[1])
    elif change=='hardlink':
        import os
        os.link(companion,companion.with_suffix('.extra'))
    else:
        with _connect(app.config['APP_DB']) as db:
            if change=='ownership': db.execute("UPDATE album_reviews SET source_id='other' WHERE id=?",(album['id'],))
            else: db.execute("INSERT INTO adoption_jobs(kind,root_path,status,created_at) VALUES ('inbox_import_execute',?,'running','2026-10-27T00:00:00+00:00')",(str(tmp_path/'inbox'),))
    result=client.post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
    assert result.status_code==409,result.json
    assert all(path.exists() for path in sources) and companion.exists()


def test_delete_partial_companion_failure_leaves_audio_for_safe_retry(app,tmp_path,monkeypatch):
    from beets_mvp import removal
    client,base,album,sources=queued(app,tmp_path)
    companion=sources[0].parent/'album.nfo';companion.write_bytes(b'info')
    unlink=removal._unlink_reviewed
    monkeypatch.setattr(removal,'_unlink_reviewed',lambda *args: (_ for _ in ()).throw(PermissionError('locked')))
    preview=client.post(base+'/actions/preview',json={'kind':'delete-albums','album_ids':[album['id']]}).json
    values={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True}
    result=client.post(base+'/actions/apply',json=values)
    assert result.status_code==207 and len(result.json['remaining'])==3
    assert all(path.is_file() for path in sources)
    assert client.get(base+'/actions/'+preview['preview_id']).json['result']==result.json
    assert client.post(base+'/actions/apply',json=values).status_code==409
    monkeypatch.setattr(removal,'_unlink_reviewed',unlink)
    current=client.get(base+'/queue').json['albums'][0]
    action(client,base,{'kind':'delete-albums','album_ids':[current['id']]})
    assert not sources[0].parent.exists()


def test_delete_root_track_leaves_companions_and_other_albums(app,tmp_path):
    client,base,album,sources=queued(app,tmp_path,folder='',count=1)
    companion=tmp_path/'inbox/keep.nfo';companion.write_bytes(b'keep')
    action(client,base,{'kind':'delete-albums','album_ids':[album['id']]})
    assert not sources[0].exists() and companion.read_bytes()==b'keep'


def test_import_cannot_start_while_deletion_is_running(app,tmp_path):
    client,base,album,sources=queued(app,tmp_path)
    with _connect(app.config['APP_DB']) as db:
        db.execute("INSERT INTO inbox_file_actions(id,source_id,kind,fingerprint,plan_json,status,created_at) VALUES ('busy',?,'delete-albums','hash','{}','running','2026-10-27T00:00:00+00:00')",(app.config['INBOXES'][0]['id'],))
    response=client.post(f"/api/inbox/albums/{album['id']}/execute",json={'dry_run':True})
    assert response.status_code==409,response.json
    assert all(path.is_file() for path in sources)


def test_delete_one_album_keeps_unselected_album_and_late_arrivals(app,tmp_path,monkeypatch):
    from beets_mvp import removal
    client,base,one,sources=queued(app,tmp_path,folder='Delete',count=1)
    _,_,other,kept=queued(app,tmp_path,folder='Keep',count=1)
    before=kept[0].read_bytes();late=sources[0].parent/'late.nfo'
    unlink=removal._unlink_reviewed
    def arriving(root,row):
        unlink(root,row);late.write_bytes(b'new')
    monkeypatch.setattr(removal,'_unlink_reviewed',arriving)
    action(client,base,{'kind':'delete-albums','album_ids':[one['id']]})
    assert not sources[0].exists() and late.read_bytes()==b'new' and kept[0].read_bytes()==before
    assert client.get(base+'/queue').json['albums'][0]['id']==other['id']


def test_delete_refuses_track_from_another_album(app,tmp_path):
    client,base,one,sources=queued(app,tmp_path,folder='One',count=1)
    _,_,two,others=queued(app,tmp_path,folder='Two',count=1)
    response=client.post(base+'/actions/preview',json={'kind':'delete-track','album_id':one['id'],'track_id':two['tracks'][0]['id']})
    assert response.status_code==409 and all(path.is_file() for path in sources+others)
