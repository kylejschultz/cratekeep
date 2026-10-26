"""Album-aware recording identity shared across queues and execution."""
import shutil
import subprocess
from pathlib import Path

import pytest
from beets.library import Library, Item
from beets_mvp.duplicates import duplicate_matches
from test_app import make_app, tagged_wav, exact_candidate


def item(**values):
    return {'id':1,'album_id':10,'mb_trackid':'recording','mb_releasegroupid':'group','mb_albumid':'release',
            'album':'Album','albumartist':'Artist','disc':1,'track':1,**values}


def candidate(**values):
    return {'release_group_id':'group','provider_id':'release','album':'Album','artist':'Artist',
            'tracks':[{'recording_id':'recording'}],**values}


@pytest.mark.parametrize('managed,incoming,expected',[
    (item(),candidate(),1),
    (item(),candidate(provider_id='regional-edition'),1),
    (item(),candidate(release_group_id='compilation',album='Compilation'),0),
    (item(mb_releasegroupid=''),candidate(release_group_id='',provider_id='other'),1),
    (item(mb_releasegroupid='',mb_albumid=''),candidate(release_group_id='',provider_id='',album=' ALBUM ',artist='artist'),1),
    (item(mb_releasegroupid='',mb_albumid=''),candidate(release_group_id='',provider_id='',artist='Other'),0),
    (item(mb_releasegroupid='',mb_albumid='',album=''),candidate(release_group_id='',provider_id=''),0),
])
def test_context_comparison(managed,incoming,expected):
    assert len(duplicate_matches([managed],'recording',incoming)) == expected


def test_other_albums_do_not_make_same_album_identity_ambiguous():
    matches = duplicate_matches([item(),item(id=2,mb_releasegroupid='other-group')],'recording',candidate())
    assert [row['id'] for row in matches] == [1]


def test_repeated_recording_preserves_distinct_disc_track_occurrences():
    incoming = candidate(tracks=[{'recording_id':'recording'},{'recording_id':'recording'}])
    items = [item(),item(id=2,disc=2,track=1)]
    assert [row['id'] for row in duplicate_matches(items,'recording',incoming,{'proposed_position':[2,1]})] == [2]
    assert duplicate_matches([item()],'recording',incoming,{'proposed_position':[2,1]}) == []
    assert len(duplicate_matches(items,'recording',incoming)) == 2  # No guessed target.


@pytest.mark.parametrize('source',['inbox','library'])
@pytest.mark.parametrize('same_group',[False,True])
@pytest.mark.parametrize('as_is',[False,True])
def test_both_import_paths_check_album_context_and_preserve_existing_audio(tmp_path,monkeypatch,source,same_group,as_is):
    app=make_app(tmp_path); client=app.test_client()
    managed=tmp_path/'library'/'Artist'/'Original'/'01 Song.wav'
    tagged_wav(managed,artist='Artist',album='Original',title='Song')
    lib=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    track=Item.from_path(managed);track.mb_trackid='recording';track.mb_releasegroupid='original-group'
    track.mb_albumid='original-release';track.write();lib.add_album([track])
    original=managed.read_bytes()
    incoming=tmp_path/source/'Artist'/'Compilation'/'01 Song.wav'
    tagged_wav(incoming,artist='Artist',album='Compilation',title='Song')
    if as_is:
        local=Item.from_path(incoming);local.mb_trackid='recording'
        local.mb_releasegroupid='original-group' if same_group else 'compilation-group'
        local.mb_albumid='incoming-release';local.write()
    incoming_before=incoming.read_bytes()
    def provider(query,*,limit):
        value=exact_candidate(query,'123e4567-e89b-42d3-a456-426614174000')
        value['release_group_id']='original-group' if same_group else 'compilation-group'
        value['tracks'][0]['recording_id']='recording'
        return [value]
    app.config['MUSICBRAINZ_PROVIDER']=provider
    if source=='inbox':
        album=client.get('/api/inbox/reviews').json['albums'][0]
        assert client.post('/api/inbox/candidates',json={'album_id':album['id']}).status_code==201
        base=f"/api/inbox/albums/{album['id']}"
        def fake_beets(current,selection,*,pretend=False):
            if not pretend:
                target=tmp_path/'library'/'Artist'/'Compilation'/'01 Song.wav'
                target.parent.mkdir(parents=True,exist_ok=True);shutil.move(next(Path(selection).rglob(incoming.name)),target)
                lib.add(Item.from_path(target))
            return subprocess.CompletedProcess(['beet'],0,'fixture success','')
        # Import before patching so the lazy duplicate module cannot retain a
        # fixture closure after monkeypatch restores the application function.
        import beets_mvp.inbox_duplicates as duplicate_execution
        monkeypatch.setattr('beets_mvp._run_beets_import',fake_beets)
        monkeypatch.setattr(duplicate_execution,'_run_beets_import',fake_beets)
    else:
        client.post('/api/library/inventory/preview')
        client.post('/api/library-import/candidates',json={})
        album=next(row for row in client.get('/api/library-import/reviews').json['albums'] if row['album']=='Compilation')
        base=f"/api/library-import/albums/{album['id']}"
    review=client.get(base).json
    assert review['duplicate_preflight']['status']==('possible' if same_group else 'clear')
    selection={'decision':'approved','candidate_id':None if as_is else review['candidates'][0]['id']}
    if same_group:selection['duplicate_action']='keep-existing'
    assert client.patch(base,json=selection).status_code==200
    preview=client.post(base+'/execute',json={'dry_run':True})
    assert preview.status_code==200,preview.json
    assert managed.read_bytes()==original and incoming.read_bytes()==incoming_before
    result=client.post(base+'/execute',json={})
    assert result.status_code==201,result.json
    assert managed.read_bytes()==original
    assert len(list(lib.items()))==(1 if same_group else 2)
    if source=='library':
        assert incoming.exists()
        if same_group:assert incoming.read_bytes()==incoming_before
