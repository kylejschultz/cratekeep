"""Real-copy lifecycle tests use only generated audio and isolated databases."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import pytest
from beets.library import Library
from mediafile import MediaFile

from test_app import make_app, tagged_inbox_wav, exact_candidate, jpeg_bytes, configure_named_inboxes


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', f"{Path(os.sys.executable).parent}:{os.environ.get('PATH','')}")
    return make_app(tmp_path)


def copied(app, tmp_path, *, folder='Incoming', title='Song', artist='Original', album='Original Album', mbid='123e4567-e89b-42d3-a456-426614174000', duplicate_action=None):
    source = tmp_path / 'inbox' / folder / f'{title}.wav'
    tagged_inbox_wav(source, artist=artist, album=album, title=title)
    before = source.read_bytes()
    client = app.test_client()
    review = next(row for row in client.get('/api/inbox/reviews').json['albums'] if any(track['path'] == source.relative_to(tmp_path/'inbox').as_posix() for track in row['tracks']))
    def provider(query, *, limit):
        candidate={**exact_candidate(query,mbid), 'artist':'Canonical', 'album':'Canonical Album'}
        for track in candidate['tracks']:
            track['recording_id']=f"recording-{mbid}-{track['title']}"
        return [candidate]
    app.config['MUSICBRAINZ_PROVIDER'] = provider
    matched = client.post('/api/inbox/candidates', json={'album_id':review['id']}).json['album']
    candidate = matched['candidates'][0]
    base=f"/api/inbox/albums/{review['id']}"
    assert client.patch(base,json={'decision':'approved','candidate_id':candidate['id'], **({'duplicate_action':duplicate_action} if duplicate_action else {})}).status_code == 200
    result=client.post(base+'/execute',json={})
    assert result.status_code == 201,result.json
    return source,before,result.json,review


def imports_base(app):
    return f"/api/inbox/{app.config['INBOXES'][0]['id']}"


def action(client,base,request):
    preview=client.post(base+'/actions/preview',json=request)
    assert preview.status_code == 200,preview.json
    values={'preview_id':preview.json['preview_id'],'expected_fingerprint':preview.json['fingerprint'],'confirmed':True}
    result=client.post(base+'/actions/apply',json=values)
    assert result.status_code == 200,result.json
    return preview.json,result.json,values


def test_real_copy_is_independent_and_stays_imported_across_refresh_restart(app,tmp_path):
    source,before,result,review=copied(app,tmp_path)
    assert source.read_bytes() == before
    assert MediaFile(source).artist == 'Original'
    target=Path(result['items'][0]['path'])
    assert target.is_file() and MediaFile(target).artist == 'Canonical'
    assert target.stat().st_ino != source.stat().st_ino
    # Discover the copied files as the adoption queue does on page load.
    assert app.test_client().post('/api/library-import/queue/refresh', json={}).status_code == 200
    assert app.test_client().get('/api/library-import/queue').json['total'] == 0
    assert source.read_bytes() == before
    client=app.test_client();base=imports_base(app)
    assert not client.get('/api/inbox/reviews').json['albums']
    assert client.post(base+'/scopes/refresh',json={}).status_code == 200
    assert client.get(base+'/queue?status=imported').json['total']==1
    row=client.get(base+'/imports').json['imports'][0]
    assert row['undoable'] and row['sources']==[source.relative_to(tmp_path/'inbox').as_posix()]
    from beets_mvp import create_app
    restarted=create_app({'TESTING':True,'SECRET_KEY':'test','STATE_PATH':str(tmp_path/'config'),'BROWSE_ROOTS':[str(tmp_path)]})
    assert restarted.test_client().get(base+'/imports').json['total']==1
    assert not restarted.test_client().get('/api/inbox/reviews').json['albums']
    assert source.read_bytes()==before


def test_undo_removes_only_job_copy_and_allows_reimport_with_replay(app,tmp_path):
    source,before,result,review=copied(app,tmp_path)
    client=app.test_client();base=imports_base(app)
    preview,undone,values=action(client,base,{'kind':'undo','job_ids':[result['id']]})
    assert preview['count']==1 and undone['status']=='complete'
    assert source.read_bytes()==before
    assert not Path(result['items'][0]['path']).exists()
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    assert not list(library.items())
    assert client.get(base+'/imports').json['total']==0
    pending=client.get('/api/inbox/reviews').json['albums']
    assert len(pending)==1 and pending[0]['id']==review['id'] and pending[0]['decision']=='approved'
    assert pending[0]['candidate_status']=='complete'
    assert client.get(base+'/queue?status=matched').json['total']==1
    assert not list(library.albums())
    assert client.post(base+'/actions/apply',json=values).json == undone
    # Reimport uses the same unchanged source, not an archived completed review.
    source2,before2,result2,_=copied(app,tmp_path)
    assert source2.read_bytes()==before2 and result2['id']!=result['id']


def test_delete_imported_all_needs_no_selection_and_keeps_library(app,tmp_path):
    first,before1,result1,_=copied(app,tmp_path,folder='First',title='First')
    second,before2,result2,_=copied(app,tmp_path,folder='Second',title='Second',mbid='123e4567-e89b-42d3-a456-426614174001')
    client=app.test_client();base=imports_base(app)
    targets=[Path(result['items'][0]['path']) for result in (result1,result2)]
    hashes=[path.read_bytes() for path in targets]
    preview,done,_=action(client,base,{'kind':'delete-imported'})
    assert preview['count']==2 and all(row['location']=='Inbox' for row in preview['files'])
    assert not first.exists() and not second.exists()
    assert [path.read_bytes() for path in targets]==hashes
    assert len(list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items()))==2
    assert client.get(base+'/imports').json['total']==0
    assert not (tmp_path/'inbox/First').exists() and (tmp_path/'inbox').is_dir()


def test_removal_never_accepts_unimported_selection(app,tmp_path):
    source=tmp_path/'inbox/Artist/First/Song.wav'
    tagged_inbox_wav(source,album='First');before=source.read_bytes()
    client=app.test_client();base=imports_base(app)
    client.get('/api/inbox/reviews')
    blocked=client.post(base+'/actions/preview',json={'kind':'delete-selected','scopes':['Artist']})
    assert blocked.status_code==409 and source.read_bytes()==before


@pytest.mark.parametrize('change',['source','target','metadata'])
def test_changed_files_or_db_block_undo_without_deleting_anything(app,tmp_path,change):
    source,before,result,_=copied(app,tmp_path)
    target=Path(result['items'][0]['path'])
    if change=='source': source.write_bytes(source.read_bytes()+b'new download')
    elif change=='target': target.write_bytes(target.read_bytes()+b'edited copy')
    else:
        library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
        item=library.get_item(result['items'][0]['beets_item_id']);item.title='Changed';item.store()
    client=app.test_client();base=imports_base(app)
    blocked=client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[result['id']]})
    assert blocked.status_code==409
    assert target.is_file() and source.is_file()
    if change=='source':
        assert client.get('/api/inbox/reviews').json['albums'], 'Changed source must return to Pending'


def test_preview_rejects_file_changes_and_cross_source_job_ids(app,tmp_path):
    source,before,result,_=copied(app,tmp_path)
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported'}).json
    source.write_bytes(before+b'changed')
    blocked=client.post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
    assert blocked.status_code==409 and source.exists()
    other=tmp_path/'other';other.mkdir()
    configure_named_inboxes(app,[tmp_path/'inbox',other],['First','Second'])
    other_base=f"/api/inbox/{app.config['INBOXES'][1]['id']}"
    assert client.post(other_base+'/actions/preview',json={'kind':'undo','job_ids':[result['id']]}).status_code==409


def test_undoing_addition_preserves_other_tracks_in_same_album(app,tmp_path):
    source1,before1,result1,_=copied(app,tmp_path,folder='First',title='First')
    source2,before2,result2,_=copied(app,tmp_path,folder='Second',title='Second')
    first_target=Path(result1['items'][0]['path']);original=first_target.read_bytes()
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    assert len(list(library.albums()))==1
    action(app.test_client(),imports_base(app),{'kind':'undo','job_ids':[result2['id']]})
    assert first_target.read_bytes()==original
    assert len(list(library.items()))==1
    assert source1.read_bytes()==before1 and source2.read_bytes()==before2


@pytest.mark.parametrize('policy',['keep-existing','replace'])
def test_duplicate_copy_receipts_preserve_originals_and_existing_ownership(tmp_path,monkeypatch,policy):
    from test_app import duplicate_inbox_fixture
    app,client,library,album,managed,sources,base=duplicate_inbox_fixture(tmp_path,policy,monkeypatch,mixed=True)
    originals={path:path.read_bytes() for path in sources}
    existing_id=next(iter(album.items())).id
    result=client.post(base+'/execute',json={})
    assert result.status_code==201,result.json
    assert all(path.read_bytes()==data for path,data in originals.items())
    imports=imports_base(app);row=client.get(imports+'/imports').json['imports'][0]
    assert row['undoable'] is (policy=='keep-existing')
    if policy=='replace':
        assert client.post(imports+'/actions/preview',json={'kind':'undo','job_ids':[result.json['id']]}).status_code==409
    else:
        managed_bytes=managed.read_bytes()
        action(client,imports,{'kind':'undo','job_ids':[result.json['id']]})
        assert managed.read_bytes()==managed_bytes
        assert [item.id for item in library.items()]==[existing_id]


def test_selected_import_purge_is_job_scoped_and_ends_undo(app,tmp_path):
    first,_,one,_=copied(app,tmp_path,folder='First',title='First')
    second,before,two,_=copied(app,tmp_path,folder='Second',title='Second')
    client=app.test_client();base=imports_base(app)
    action(client,base,{'kind':'delete-imported','job_ids':[one['id']]})
    assert not first.exists() and second.read_bytes()==before
    assert Path(one['items'][0]['path']).is_file()
    assert client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[one['id']]}).status_code==409
    assert [row['job_id'] for row in client.get(base+'/imports').json['imports']]==[two['id']]


def test_partial_delete_journal_retries_only_remaining_originals(app,tmp_path,monkeypatch):
    from beets_mvp import removal
    first,_,one,_=copied(app,tmp_path,folder='First',title='First')
    second,_,two,_=copied(app,tmp_path,folder='Second',title='Second')
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported'}).json
    unlink=removal._unlink_reviewed;calls=[]
    def fail_second(root,row):
        calls.append(row['path'])
        if len(calls)==2: raise PermissionError('second deletion blocked')
        return unlink(root,row)
    monkeypatch.setattr(removal,'_unlink_reviewed',fail_second)
    values={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True}
    result=client.post(base+'/actions/apply',json=values)
    assert result.status_code==207 and len(result.json['removed'])==1
    assert sum(path.exists() for path in (first,second))==1
    assert client.post(base+'/actions/apply',json=values).status_code==409
    monkeypatch.setattr(removal,'_unlink_reviewed',unlink)
    preview,done,_=action(client,base,{'kind':'delete-imported'})
    assert preview['count']==1 and not first.exists() and not second.exists()
    assert all(Path(result['items'][0]['path']).exists() for result in (one,two))


@pytest.mark.parametrize('changed',['symlink','hardlink','root','shared-item','interrupted'])
def test_unsafe_or_interrupted_actions_never_delete(app,tmp_path,changed):
    from beets.library import Item
    source,before,result,_=copied(app,tmp_path)
    target=Path(result['items'][0]['path']);target_bytes=target.read_bytes()
    client=app.test_client();base=imports_base(app)
    if changed=='symlink':
        source.unlink();source.symlink_to(target)
    elif changed=='hardlink': os.link(source,source.with_suffix('.extra'))
    elif changed=='root':
        other=tmp_path/'other-library';other.mkdir();app.config['LIBRARY_PATH']=str(other)
    elif changed=='shared-item':
        Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).add(Item.from_path(target))
    else:
        preview=client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[result['id']]}).json
        with sqlite3.connect(app.config['APP_DB']) as db:
            db.execute("UPDATE inbox_file_actions SET status='running' WHERE id=?",(preview['preview_id'],))
    response=client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[result['id']]})
    assert response.status_code==409,response.json
    assert target.read_bytes()==target_bytes and source.exists()
    if changed=='interrupted':
        audit=client.get(base+'/imports').json['incomplete_actions'][0]
        assert audit['status']=='running' and audit['planned_files'][0]['location']=='Library'


@pytest.mark.parametrize('shared',[False,True])
def test_undo_only_removes_unchanged_unshared_owned_artwork(app,tmp_path,shared):
    app.config.update(FETCH_ART=True,ART_SIDECAR=True,ART_EMBED=False)
    # An incoming cover is copied to Library without altering the original.
    folder=tmp_path/'inbox/Incoming';folder.mkdir()
    cover=folder/'cover.jpg';art=jpeg_bytes((60,70,80));cover.write_bytes(art)
    source,before,result,_=copied(app,tmp_path)
    target=Path(result['items'][0]['path']);owned_cover=target.parent/'cover.jpg'
    assert owned_cover.read_bytes()==art
    if shared:
        tagged_inbox_wav(target.parent/'Other.wav',title='Other')
    action(app.test_client(),imports_base(app),{'kind':'undo','job_ids':[result['id']]})
    assert owned_cover.exists() is shared
    assert cover.read_bytes()==art and source.read_bytes()==before


def test_later_kept_import_protects_the_only_library_copy(app,tmp_path):
    _,_,first,_=copied(app,tmp_path,folder='First')
    _,_,second,_=copied(app,tmp_path,folder='Second',duplicate_action='keep-existing')
    client=app.test_client();base=imports_base(app)
    target=Path(first['items'][0]['path']);before=target.read_bytes()
    assert client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[first['id']]}).status_code==409
    action(client,base,{'kind':'delete-imported','job_ids':[second['id']]})
    # Purging later originals must not allow the first import to delete the
    # only Library file now used by that non-undoable import.
    assert client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[first['id']]}).status_code==409
    assert target.read_bytes()==before


def test_purge_preview_cannot_outlive_a_library_file_change(app,tmp_path):
    source,before,result,_=copied(app,tmp_path)
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported','job_ids':[result['id']]}).json
    target=Path(result['items'][0]['path']);target.write_bytes(target.read_bytes()+b'changed')
    response=client.post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
    assert response.status_code==409 and source.read_bytes()==before


@pytest.mark.parametrize('mode',['best','explicit','as-is'])
def test_selected_inbox_import_uses_saved_choice_without_threshold_review(app,tmp_path,mode):
    source=tmp_path/'inbox/Incoming/Song.wav';tagged_inbox_wav(source,artist='Original',album='Local',title='Song')
    before=source.read_bytes();client=app.test_client();base=imports_base(app)
    client.post(base+'/scopes/refresh',json={})
    album=client.get(base+'/queue').json['albums'][0]
    assert client.get(base+'/queue?status=pending').json['total']==1
    def provider(query,*,limit):
        return [{**exact_candidate(query, f'release-{index}'), 'artist':artist,'album':title}
                for index,(artist,title) in enumerate([('Distant','Other'),('Original','Local')])]
    app.config['MUSICBRAINZ_PROVIDER']=provider
    matched=client.post('/api/inbox/candidates',json={'album_id':album['id']}).json['album']
    assert client.get(base+'/queue?status=matched').json['total']==1
    if mode=='explicit':
        choice=min(matched['candidates'],key=lambda row:row['confidence'])
        expected='Other'
        assert client.patch(f"/api/inbox/albums/{album['id']}",json={'decision':'approved','candidate_id':choice['id']}).status_code==200
    elif mode=='as-is':
        expected='Local'
        assert client.patch(f"/api/inbox/albums/{album['id']}",json={'decision':'approved','candidate_id':None}).status_code==200
    else: expected='Local'
    body={'scopes':['Incoming'],'album_ids':[album['id']],'mode':'selected'}
    assert client.post(base+'/bulk/preview',json=body).json['importable_albums'][0]['id']==album['id']
    result=client.post(base+'/bulk/execute',json={**body,'confirmed':True})
    assert result.status_code==201 and len(result.json['imported'])==1,result.json
    receipt=client.get(base+'/queue?status=imported').json['albums'][0]['imported']
    assert result.json['imported'][0]['job_id']==receipt['job_id']
    assert source.read_bytes()==before
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    assert list(library.items())[0].album==expected
    assert client.get(base+'/queue?status=imported').json['total']==1
    assert client.get(base+'/queue?status=matched').json['total']==0
    assert client.post(base+'/bulk/preview',json={'scopes':['Incoming'],'mode':'selected'}).status_code==400


def test_new_track_in_retained_folder_does_not_rematch_or_reimport_original(app,tmp_path):
    first,before,result,review=copied(app,tmp_path,folder='Album',title='First')
    second=first.parent/'Second.wav';tagged_inbox_wav(second,artist='Original',album='Original Album',title='Second',track=2)
    client=app.test_client();base=imports_base(app)
    assert client.post(base+'/scopes/refresh',json={}).status_code==200
    albums=client.get(base+'/queue').json['albums']
    imported=next(row for row in albums if row.get('imported'))
    pending=next(row for row in albums if not row.get('imported'))
    assert imported['id']==review['id'] and len(imported['tracks'])==1
    assert len(pending['tracks'])==1 and pending['tracks'][0]['path'].endswith('Second.wav')
    assert first.read_bytes()==before and len(list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items()))==1


def test_two_workers_cannot_undo_and_purge_the_same_import(app,tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from beets_mvp import create_app, inbox_imports
    source,before,result,_=copied(app,tmp_path)
    target=Path(result['items'][0]['path'])
    base=imports_base(app);client=app.test_client()
    previews=[client.post(base+'/actions/preview',json={'kind':kind,'job_ids':[result['id']]}).json for kind in ['undo','delete-imported']]
    workers=[create_app({'TESTING':True,'STATE_PATH':app.config['STATE_PATH']}) for _ in previews]
    barrier=threading.Barrier(2);digest=inbox_imports._digest
    def both_plans_ready(plan):
        value=digest(plan);barrier.wait(timeout=5);return value
    monkeypatch.setattr(inbox_imports,'_digest',both_plans_ready)
    def apply(index):
        preview=previews[index]
        response=workers[index].test_client().post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
        return response.status_code,response.json
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(apply,range(2)))
    assert sorted(status for status,_ in results)==[200,409],results
    assert source.exists() != target.exists(),results
    if source.exists():assert source.read_bytes()==before


def test_imported_queue_does_not_rescan_the_library_per_track(app,tmp_path,monkeypatch):
    from beets.library import Item
    source_paths=[]
    for number in range(1,9):
        path=tmp_path/f'inbox/Album/{number:02d} Song {number}.wav'
        tagged_inbox_wav(path,title=f'Song {number}',track=number)
        source_paths.append(path)
    before={path:path.read_bytes() for path in source_paths}
    client=app.test_client();base=imports_base(app)
    review=client.get('/api/inbox/reviews').json['albums'][0]
    endpoint=f"/api/inbox/albums/{review['id']}"
    assert client.patch(endpoint,json={'decision':'approved','candidate_id':None}).status_code==200
    imported=client.post(endpoint+'/execute',json={})
    assert imported.status_code==201,imported.json
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    for index in range(200):
        library.add(Item(path=str(tmp_path/f'library/Unrelated/{index}.wav'),artist='Other',album='Other',title=str(index)))
    scans=[];items=Library.items
    def counted(library,*args,**kwargs):
        scans.append(1)
        return items(library,*args,**kwargs)
    monkeypatch.setattr(Library,'items',counted)
    response=client.get(base+'/queue')
    assert response.status_code==200 and response.json['albums'][0]['imported']
    assert len(scans)<=1, f'Queue performed {len(scans)} full Library item queries for eight imported tracks'
    assert all(path.read_bytes()==data for path,data in before.items())


def test_full_undo_checks_shared_paths_even_when_queue_uses_lightweight_status(app,tmp_path):
    from beets.library import Item
    source,before,result,_=copied(app,tmp_path)
    target=Path(result['items'][0]['path']);target_bytes=target.read_bytes()
    alias=tmp_path/'library/Alias/Song.wav';alias.parent.mkdir(parents=True);alias.symlink_to(target)
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    library.add(Item(path=str(alias),title='Shared alias'))
    client=app.test_client();base=imports_base(app)
    assert client.get(base+'/queue').status_code==200
    response=client.post(base+'/actions/preview',json={'kind':'undo','job_ids':[result['id']]})
    assert response.status_code==409 and 'shared ownership' in response.json['error']
    assert target.read_bytes()==target_bytes and source.read_bytes()==before


def test_remove_imported_hashes_each_library_file_once_at_apply(app,tmp_path,monkeypatch):
    from beets_mvp import inbox_imports
    imports=[copied(app,tmp_path,folder=f'Album{index}',title=f'Song{index}',mbid=f'123e4567-e89b-42d3-a456-42661417400{index}') for index in range(4)]
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported'}).json
    targets=[Path(result['items'][0]['path']) for _,_,result,_ in imports]
    before={path:path.read_bytes() for path in targets}
    hashes=[];identity=inbox_imports.identity
    def counted(path):
        if Path(app.config['LIBRARY_PATH']) in Path(path).parents:hashes.append(path)
        return identity(path)
    monkeypatch.setattr(inbox_imports,'identity',counted)
    response=client.post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
    assert response.status_code==200,response.json
    assert len(hashes)==len(targets),f'{len(hashes)} Library file hashes for {len(targets)} removals'
    assert all(not path.exists() for path,_,_,_ in imports)
    assert all(path.read_bytes()==data for path,data in before.items())


def test_remove_imported_stops_if_library_changes_between_deletions(app,tmp_path,monkeypatch):
    from beets_mvp import removal
    imports=[copied(app,tmp_path,folder=f'Album{index}',title=f'Song{index}',mbid=f'123e4567-e89b-42d3-a456-42661417400{index}') for index in range(2)]
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported'}).json
    last_source=tmp_path/'inbox'/preview['files'][-1]['path']
    last_target=next(Path(result['items'][0]['path']) for source,_,result,_ in imports if source==last_source)
    before=last_target.read_bytes()
    unlink=removal._unlink_reviewed;deleted=[]
    def change_after_first(root,row):
        unlink(root,row);deleted.append(row['path'])
        if len(deleted)==1:
            state=last_target.stat();last_target.write_bytes(before[:-1]+bytes([before[-1]^1]))
            os.utime(last_target,ns=(state.st_atime_ns,state.st_mtime_ns))
    monkeypatch.setattr(removal,'_unlink_reviewed',change_after_first)
    result=client.post(base+'/actions/apply',json={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True})
    assert result.status_code==207 and len(deleted)==1
    assert last_source.exists() and last_target.read_bytes()!=before and last_target.stat().st_size==len(before)
    assert 'Library destination changed' in result.json['errors'][0]['error']


def test_action_status_is_read_only_source_scoped_and_survives_response_loss(app,tmp_path):
    source,before,result,_=copied(app,tmp_path)
    client=app.test_client();base=imports_base(app)
    preview=client.post(base+'/actions/preview',json={'kind':'delete-imported'}).json
    status_url=base+'/actions/'+preview['preview_id']
    pending=client.get(status_url)
    assert pending.json['status']=='pending' and pending.headers['Cache-Control']=='no-store'
    assert source.read_bytes()==before
    values={'preview_id':preview['preview_id'],'expected_fingerprint':preview['fingerprint'],'confirmed':True}
    finished=client.post(base+'/actions/apply',json=values).json
    assert client.get(status_url).json=={'status':'complete','result':finished}
    assert client.get(base+'/actions/missing').status_code==404
    other=tmp_path/'other';other.mkdir()
    configure_named_inboxes(app,[tmp_path/'inbox',other],['First','Second'])
    other_base=f"/api/inbox/{app.config['INBOXES'][1]['id']}"
    assert client.get(other_base+'/actions/'+preview['preview_id']).status_code==404
    assert not source.exists() and Path(result['items'][0]['path']).is_file()


def test_remove_imported_refreshes_only_the_affected_source_scope(app,tmp_path,monkeypatch):
    import beets_mvp
    source,_,result,_=copied(app,tmp_path,folder='Affected')
    untouched=tmp_path/'inbox/Other/Song.wav';tagged_inbox_wav(untouched);before=untouched.read_bytes()
    scans=[];scan=beets_mvp._scan_inbox_inventory
    def scoped_scan(app,inbox,*,scopes=None):
        scans.append(scopes)
        return scan(app,inbox,scopes=scopes)
    monkeypatch.setattr(beets_mvp,'_scan_inbox_inventory',scoped_scan)
    action(app.test_client(),imports_base(app),{'kind':'delete-imported','job_ids':[result['id']]})
    assert scans==[['Affected']]
    assert not source.exists() and untouched.read_bytes()==before
