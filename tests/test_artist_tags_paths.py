"""Generated media only: artist list consistency and metadata-driven paths."""
import os
from pathlib import Path
import pytest
from mediafile import MediaFile
from test_app import make_app, managed_wav_album

SYNC='[00:00.00]Original fixture line'
PLAIN='Original fixture line'

@pytest.fixture
def setup(tmp_path):
    app=make_app(tmp_path)
    library,album,paths=managed_wav_album(app,tmp_path,titles=('First','Second','Third'))
    return app,library,[item.id for item in library.items()],paths

@pytest.mark.parametrize('field,plural',[('artist','artists'),('albumartist','albumartists')])
def test_manual_artist_alias_edit_updates_file_lists_and_repairs_resaved_names(setup,field,plural):
    app,library,ids,paths=setup
    item=library.get_item(ids[0]);item[field]='Old fixture name';item[plural]=['Old fixture name'];item.mb_artistid='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';item.write();item.store()
    album=library.get_album(item.album_id);client=app.test_client()
    if field=='albumartist':album.albumartist='Old fixture name';album.albumartists=['Old fixture name'];album.store()
    opened=client.get(f'/api/library/albums/{album.id}').json
    url=f'/api/library/albums/{album.id}'+(f'/tracks/{item.id}' if field=='artist' else '')
    result=client.patch(url,json={field:'New fixture name','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    paths[0]=Path(library.get_item(item.id).path.decode())
    media=MediaFile(paths[0]);assert getattr(media,field)=='New fixture name' and getattr(media,plural)==['New fixture name']
    assert library.get_item(item.id).get(plural)==['New fixture name']
    assert library.get_item(item.id).mb_artistid=='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    if field=='albumartist':assert library.get_album(album.id).albumartists==['New fixture name']
    # Repair a prior scalar-only edit by re-saving the same display name.
    stale=library.get_item(item.id);stale[plural]=['Old fixture name'];stale.write();stale.store()
    opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(url,json={field:'New fixture name','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200 and getattr(MediaFile(library.get_item(item.id).path),plural)==['New fixture name']


def test_artist_alias_list_rollback_restores_coupled_fields(setup,monkeypatch):
    from beets.library import Item
    app,library,ids,paths=setup;album=library.get_album(library.get_item(ids[0]).album_id)
    for item in album.items():item.albumartists=['Original fixture artist'];item.write();item.store()
    album.albumartists=['Original fixture artist'];album.store()
    original=Item.write;calls=[]
    def fail_second(item,*args,**kwargs):
        calls.append(item.id)
        if len(calls)==2:raise OSError('fixture write failure')
        return original(item,*args,**kwargs)
    monkeypatch.setattr(Item,'write',fail_second)
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'albumartist':'New fixture artist','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==500 and result.json['code']=='mutation_rolled_back'
    assert all(item.albumartists==['Original fixture artist'] for item in library.get_album(album.id).items())
    assert all(MediaFile(path).albumartists==['Original fixture artist'] for path in paths)
    assert library.get_album(album.id).albumartists==['Original fixture artist']


def test_single_artist_alias_in_a_list_preserves_collaborators(setup):
    from beets_mvp import _expanded_artist_edit
    app,library,ids,paths=setup;item=library.get_item(ids[0])
    item.artist='First fixture';item.artists=['First fixture','Guest fixture']
    assert _expanded_artist_edit(item,{'artist':'Renamed fixture'})['artists']==['Renamed fixture','Guest fixture']


def test_artist_edit_moves_audio_lyrics_and_cover_and_rolls_back_on_failure(setup,monkeypatch):
    from beets_mvp import _mutate_managed_items
    app,library,ids,paths=setup
    item=library.get_item(ids[0]);old=paths[0]
    old.with_suffix('.lrc').write_text(SYNC)
    cover=old.parent/'cover.jpg';cover.write_bytes(b'fixture art')
    album=library.get_album(item.album_id);album.artpath=str(cover).encode();album.store()
    before=old.read_bytes()
    def fail():raise OSError('fixture audit failure')
    with pytest.raises(Exception,match='restored'):
        _mutate_managed_items([(item,{'albumartist':'Renamed'})],album=album,
                              album_values={'albumartist':'Renamed'},relocate_app=app,finalize=fail)
    assert old.exists() and old.with_suffix('.lrc').read_text()==SYNC
    assert library.get_item(item.id).path==str(old).encode()
    assert MediaFile(old).albumartist!='Renamed'
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'albumartist':'Renamed','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    new=Path(library.get_item(item.id).path.decode())
    assert new!=old and new.exists() and not old.exists()
    assert new.with_suffix('.lrc').read_text()==SYNC and not old.with_suffix('.lrc').exists()
    assert (new.parent/'cover.jpg').read_bytes()==b'fixture art'
    assert library.get_album(album.id).artpath==str(new.parent/'cover.jpg').encode()
    assert library.get_item(item.id).albumartists==['Renamed']


def test_artist_edit_collision_leaves_existing_audio_and_database_untouched(setup):
    app,library,ids,paths=setup;item=library.get_item(ids[0]);old=paths[0]
    before=old.read_bytes();item.albumartist='Occupied'
    target=Path(app.config['LIBRARY_PATH'])/'Occupied'/'Album'/old.name
    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'occupied fixture')
    client=app.test_client();album=library.get_album(item.album_id);opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'albumartist':'Occupied','expected_fingerprint':opened['fingerprint']})
    assert result.status_code>=400
    assert target.read_bytes()==b'occupied fixture' and old.exists()
    assert library.get_item(item.id).albumartist!='Occupied'
    assert MediaFile(old).albumartist!='Occupied'


def test_track_artist_edit_uses_custom_path_template_and_keeps_album_artist(setup):
    import yaml
    app,library,ids,paths=setup;client=app.test_client();inbox=app.config['INBOXES'][0]
    configured=client.post('/settings',data={
        'inbox_id':[inbox['id']],'inbox_name':[inbox['name']],'inbox_path':[inbox['path']],
        'library_path':app.config['LIBRARY_PATH'],
        'beets_config':yaml.safe_dump({'paths':{'default':'$artist/Custom/$album/$track $title'}}),
    })
    assert configured.status_code==302
    item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}/tracks/{item.id}',json={
        'artist':'New Track Artist','expected_fingerprint':opened['fingerprint'],
    })
    assert result.status_code==200,result.json
    new=Path(library.get_item(item.id).path.decode())
    assert new.relative_to(app.config['LIBRARY_PATH']).parts[:2]==('New Track Artist','Custom')
    assert not paths[0].exists() and MediaFile(new).artist=='New Track Artist'
    assert library.get_album(album.id).albumartist=='Artist'


def test_artist_edit_refuses_ambiguous_lyric_sidecar(setup):
    app,library,ids,paths=setup;client=app.test_client()
    paths[0].with_suffix('.flac').write_bytes(b'another audio owner')
    sidecar=paths[0].with_suffix('.txt');sidecar.write_text(PLAIN)
    item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={
        'albumartist':'Renamed','expected_fingerprint':opened['fingerprint'],
    })
    assert result.status_code>=400 and 'ambiguous' in result.json['error']
    assert all(path.exists() for path in paths) and sidecar.read_text()==PLAIN
    assert library.get_album(album.id).albumartist=='Artist'


@pytest.mark.parametrize('field,value,expected', [
    ('title','Changed Title','01 Changed Title.wav'),
    ('track',7,'07 First.wav'),
])
def test_track_only_edit_renames_audio_and_sidecar_without_changing_identity(setup,field,value,expected):
    app,library,ids,paths=setup
    old=paths[0];old.with_suffix('.lrc').write_text(SYNC)
    item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    response=client.patch(f'/api/library/albums/{album.id}/tracks/{item.id}',json={
        field:value,'expected_fingerprint':opened['fingerprint'],
    })
    assert response.status_code==200,response.json
    stored=library.get_item(item.id);new=Path(os.fsdecode(stored.path))
    assert new.name==expected and new.parent==old.parent and not old.exists()
    assert new.with_suffix('.lrc').read_text()==SYNC and not old.with_suffix('.lrc').exists()
    assert stored.album_id==album.id and getattr(MediaFile(new),field)==value
    assert paths[1].exists() and library.get_item(ids[1]).path==os.fsencode(paths[1])


def test_album_rename_moves_album_folder_artwork_and_preserves_unrelated_files(setup):
    app,library,ids,paths=setup;old=paths[0].parent
    cover=old/'cover.jpg';cover.write_bytes(b'fixture cover')
    note=old/'notes.nfo';note.write_text('unrelated fixture')
    album=library.get_album(library.get_item(ids[0]).album_id);album.artpath=os.fsencode(cover);album.store()
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'album':'Renamed Album','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    new=Path(os.fsdecode(library.get_item(ids[0]).path)).parent
    assert new.name=='Renamed Album' and new.parent==old.parent
    assert all(Path(os.fsdecode(library.get_item(item_id).path)).parent==new for item_id in ids)
    assert (new/'cover.jpg').read_bytes()==b'fixture cover' and not cover.exists()
    assert library.get_album(album.id).artpath==os.fsencode(new/'cover.jpg')
    assert note.read_text()=='unrelated fixture' and not any(path.exists() for path in paths)


def test_move_failure_restores_audio_sidecars_metadata_and_paths(setup,monkeypatch):
    import beets_mvp
    app,library,ids,paths=setup;album=library.get_album(library.get_item(ids[0]).album_id)
    paths[0].with_suffix('.txt').write_text(PLAIN)
    original=beets_mvp._copy_managed_exclusive;calls=[]
    def fail_later(source,target):
        calls.append(source)
        if len(calls)==3:raise OSError('fixture move failure')
        return original(source,target)
    monkeypatch.setattr(beets_mvp,'_copy_managed_exclusive',fail_later)
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'album':'Failed Rename','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==500 and result.json['code']=='mutation_rolled_back'
    assert all(path.exists() and MediaFile(path).album=='Album' for path in paths)
    assert all(library.get_item(item_id).path==os.fsencode(path) for item_id,path in zip(ids,paths))
    assert paths[0].with_suffix('.txt').read_text()==PLAIN
    assert library.get_album(album.id).album=='Album'


def test_metadata_edit_preserves_embedded_lyric_language_and_timing_frames(setup):
    from copy import deepcopy
    from mutagen.id3 import USLT,SYLT
    app,library,ids,paths=setup;old=paths[0]
    media=MediaFile(old)
    media.mgfile.tags.add(USLT(encoding=3,lang='eng',desc='',text=PLAIN))
    media.mgfile.tags.add(USLT(encoding=3,lang='fra',desc='',text='Autre fixture'))
    media.mgfile.tags.add(SYLT(encoding=3,lang='eng',format=2,type=1,desc='',text=[('Fixture',0)]))
    media.save()
    frames={key:{frame.HashKey:deepcopy(frame) for frame in MediaFile(old).mgfile.tags.getall(key)} for key in ('USLT','SYLT')}
    item=library.get_item(ids[0]);item.lyrics='stale catalog text';item.store()
    album=library.get_album(item.album_id);client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}/tracks/{item.id}',json={'title':'Renamed','expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    current=MediaFile(library.get_item(item.id).path)
    assert all({frame.HashKey:frame for frame in current.mgfile.tags.getall(key)}==value for key,value in frames.items())



def test_rollback_does_not_overwrite_a_new_file_at_the_original_path(setup):
    from beets_mvp import _mutate_managed_items, ManagedMutationError
    app,library,ids,paths=setup;item=library.get_item(ids[0]);old=paths[0]
    def collision_after_move():
        old.write_bytes(b'Unrelated file appeared during finalization')
        raise OSError('fixture finalization failure')
    with pytest.raises(ManagedMutationError) as failure:
        _mutate_managed_items([(item,{'title':'Moved fixture'})],relocate_app=app,finalize=collision_after_move)
    assert failure.value.code=='rollback_failed'
    assert old.read_bytes()==b'Unrelated file appeared during finalization'
    kept=Path(os.fsdecode(library.get_item(item.id).path))
    assert kept!=old and kept.is_file() and MediaFile(kept).title=='Moved fixture'


def test_adoption_repairs_stale_artist_lists_even_when_display_names_match(tmp_path):
    from beets.library import Item,Library
    from test_app import write_wav
    app=make_app(tmp_path);path=tmp_path/'library'/'Artist'/'Album'/'01 Song.wav';write_wav(path)
    media=MediaFile(path);media.title='Song';media.artist=media.albumartist='Artist';media.album='Album'
    media.artists=['Old'];media.albumartists=['Old'];media.mb_albumid='fixture-release';media.save()
    library=Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']);item=Item.from_path(path);library.add(item)
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,limit:[{'provider_id':'fixture-release','artist':'Artist','album':'Album'}]
    client=app.test_client();assert client.post('/api/library/inventory/preview').status_code==201
    assert client.post('/api/library-import/candidates',json={}).status_code==201
    review=client.get('/api/library-import/reviews').json['albums'][0]
    base=f"/api/library-import/albums/{review['id']}"
    assert client.patch(base,json={'decision':'approved','candidate_id':review['candidates'][0]['id']}).status_code==200
    preview=client.post(base+'/execute',json={'dry_run':True})
    assert preview.status_code==200,preview.json
    assert preview.json['items'][0]['changes']['albumartists']['to']==['Artist']
    result=client.post(base+'/execute',json={});assert result.status_code==201,result.json
    assert library.get_item(item.id).albumartists==['Artist'] and MediaFile(path).albumartists==['Artist']
    assert library.get_item(item.id).path==os.fsencode(path)


def test_disc_and_year_changes_follow_custom_template(setup):
    import yaml
    app,library,ids,paths=setup;client=app.test_client();inbox=app.config['INBOXES'][0]
    configured=client.post('/settings',data={
        'inbox_id':[inbox['id']],'inbox_name':[inbox['name']],'inbox_path':[inbox['path']],
        'library_path':app.config['LIBRARY_PATH'],
        'beets_config':yaml.safe_dump({'paths':{'default':'$albumartist/$year - $album/Disc $disc/$track $title'}}),
    });assert configured.status_code==302
    item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'year':2025,'expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    result=client.patch(f'/api/library/albums/{album.id}/tracks/{item.id}',json={'disc':2,'expected_fingerprint':result.json['album']['fingerprint']})
    assert result.status_code==200,result.json
    new=Path(os.fsdecode(library.get_item(item.id).path))
    assert new.relative_to(app.config['LIBRARY_PATH']).parts==('Artist','2025 - Album','Disc 02','01 First.wav')
    assert not paths[0].exists() and new.is_file()



def test_track_save_updates_artwork_reference_when_its_album_has_one_track(tmp_path):
    import yaml
    app=make_app(tmp_path);library,album,paths=managed_wav_album(app,tmp_path,titles=('Only',))
    old=paths[0];cover=old.parent/'cover.jpg';cover.write_bytes(b'fixture cover')
    album.artpath=os.fsencode(cover);album.store()
    client=app.test_client();inbox=app.config['INBOXES'][0]
    configured=client.post('/settings',data={
        'inbox_id':[inbox['id']],'inbox_name':[inbox['name']],'inbox_path':[inbox['path']],
        'library_path':app.config['LIBRARY_PATH'],
        'beets_config':yaml.safe_dump({'paths':{'default':'$artist/$album/$track $title'}}),
    });assert configured.status_code==302
    opened=client.get(f'/api/library/albums/{album.id}').json;item=next(iter(album.items()))
    result=client.patch(f'/api/library/albums/{album.id}/tracks/{item.id}',json={
        'artist':'Renamed Track Artist','expected_fingerprint':opened['fingerprint'],
    })
    assert result.status_code==200,result.json
    new=Path(os.fsdecode(library.get_item(item.id).path))
    assert new.relative_to(app.config['LIBRARY_PATH']).parts==('Renamed Track Artist','Album','01 Only.wav')
    assert not old.parent.exists()
    assert library.get_album(album.id).artpath==os.fsencode(new.parent/'cover.jpg')
    assert (new.parent/'cover.jpg').read_bytes()==b'fixture cover'


@pytest.mark.parametrize('field,plural',[('artist','artists'),('albumartist','albumartists')])
def test_explicit_credit_removal_clears_alias_credit_sort_and_ids(setup,field,plural):
    app,library,ids,paths=setup
    kept_id='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';removed_id='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    values={field:'gnash',plural:['gnash','Garrett Nash'],field+'_credit':'gnash, Garrett Nash',
            plural+'_credit':['gnash','Garrett Nash'],field+'_sort':'gnash, gnash',plural+'_sort':['gnash','gnash'],
            'mb_'+field+'id':kept_id,'mb_'+field+'ids':[kept_id,removed_id]}
    item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    targets=list(album.items()) if field=='albumartist' else [item]
    for target in targets:
        for key,value in values.items():target[key]=value
        target.write();target.store()
    if field=='albumartist':
        for key,value in values.items():album[key]=value
        album.store()
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    if field=='albumartist':assert opened['albumartists']==['gnash','Garrett Nash']
    else:assert opened['tracks'][0]['artists']==['gnash','Garrett Nash']
    url=f'/api/library/albums/{album.id}'+(f'/tracks/{item.id}' if field=='artist' else '')
    result=client.patch(url,json={field:'gnash',plural:['gnash'],'expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    for target in targets:
        stored=library.get_item(target.id);media=MediaFile(stored.path)
        for key,expected in {field:'gnash',plural:['gnash'],field+'_credit':'gnash',plural+'_credit':['gnash'],
                             field+'_sort':'gnash',plural+'_sort':['gnash'],
                             'mb_'+field+'id':kept_id,'mb_'+field+'ids':[kept_id]}.items():
            assert stored.get(key)==expected,(key,stored.get(key))
        for key in (field,plural,field+'_credit',plural+'_credit',field+'_sort',plural+'_sort'):
            assert 'Garrett Nash' not in str(getattr(media,key))
    if field=='albumartist':assert library.get_album(album.id).albumartists_credit==['gnash']


def test_explicit_credit_removal_also_updates_unchanged_compound_display(setup):
    from beets_mvp import _expanded_artist_edit
    app,library,ids,paths=setup;item=library.get_item(ids[0]);item.artist='Stage, Legal'
    item.artists=['Stage','Legal'];item.artists_sort=['Stage','Legal']
    values=_expanded_artist_edit(item,{'artist':'Stage, Legal','artists':['Stage']},edit_credits=True)
    assert values['artist']==values['artist_credit']=='Stage'
    assert values['artists_credit']==values['artists_sort']==['Stage']


def test_credit_editor_preserves_commas_inside_names_and_validates_arrays(setup):
    app,library,ids,paths=setup;item=library.get_item(ids[0]);album=library.get_album(item.album_id)
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    url=f'/api/library/albums/{album.id}/tracks/{item.id}'
    for invalid in ['First, Guest',[None],[''],['First','first']]:
        response=client.patch(url,json={'artists':invalid,'expected_fingerprint':opened['fingerprint']})
        assert response.status_code==400,response.json
    response=client.patch(url,json={'artist':'Tyler, The Creator','artists':['Tyler, The Creator','Guest'],
                                   'expected_fingerprint':opened['fingerprint']})
    assert response.status_code==200,response.json
    assert library.get_item(item.id).artists==['Tyler, The Creator','Guest']


def test_flac_raw_album_credit_aliases_are_rewritten_together(setup):
    app,library,ids,paths=setup
    path=paths[0].with_suffix('.flac');path.write_bytes((Path(__file__).parent/'fixtures'/'artist-credit-silence.flac').read_bytes())
    paths[0].unlink()
    media=MediaFile(path);media.title='First';media.album='Album';media.artist=media.albumartist='gnash'
    media.albumartists=['gnash','Garrett Nash'];media.albumartist_credit='gnash, Garrett Nash'
    media.albumartists_credit=['gnash','Garrett Nash'];media.albumartist_sort='gnash, gnash'
    media.albumartists_sort=['gnash','gnash'];media.track=1;media.save()
    item=library.get_item(ids[0]);item.path=os.fsencode(path);item.read();item.store()
    album=library.get_album(item.album_id)
    album.albumartist='gnash';album.albumartists=['gnash','Garrett Nash'];album.store()
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'albumartist':'gnash','albumartists':['gnash'],
                       'expected_fingerprint':opened['fingerprint']})
    assert result.status_code==200,result.json
    tags=MediaFile(library.get_item(item.id).path).mgfile.tags
    for key in ['albumartists','album_artists','albumartist_credit','albumartists_credit','albumartists_sort','albumartistsort']:
        assert tags[key]==['gnash'],(key,tags.get(key))


def test_unknown_new_credit_does_not_inherit_a_removed_artist_id(setup):
    from beets_mvp import _expanded_artist_edit
    app,library,ids,paths=setup;item=library.get_item(ids[0])
    item.artists=['First','Removed'];item.mb_artistids=['first-id','removed-id'];item.mb_artistid='first-id'
    values=_expanded_artist_edit(item,{'artists':['First','New']},edit_credits=True)
    assert values['mb_artistid']=='first-id' and values['mb_artistids']==[]
    assert 'removed-id' not in str(values)


def test_explicit_credit_change_rolls_back_all_related_fields(setup,monkeypatch):
    from beets.library import Item
    app,library,ids,paths=setup;album=library.get_album(library.get_item(ids[0]).album_id)
    values={'albumartist':'Stage','albumartists':['Stage','Legal'],'albumartist_credit':'Stage, Legal',
            'albumartists_credit':['Stage','Legal'],'albumartist_sort':'Stage, Legal','albumartists_sort':['Stage','Legal'],
            'mb_albumartistid':'stage-id','mb_albumartistids':['stage-id','legal-id']}
    for item in album.items():
        for key,value in values.items():item[key]=value
        item.write();item.store()
    for key,value in values.items():album[key]=value
    album.store()
    original=Item.write;calls=[]
    def fail_second(item,*args,**kwargs):
        calls.append(item.id)
        if len(calls)==2:raise OSError('fixture credit write failure')
        return original(item,*args,**kwargs)
    monkeypatch.setattr(Item,'write',fail_second)
    client=app.test_client();opened=client.get(f'/api/library/albums/{album.id}').json
    result=client.patch(f'/api/library/albums/{album.id}',json={'albumartists':['Stage'],'expected_fingerprint':opened['fingerprint']})
    assert result.status_code==500 and result.json['code']=='mutation_rolled_back'
    for item in library.get_album(album.id).items():
        assert all(item.get(key)==value for key,value in values.items())
        media=MediaFile(item.path)
        assert media.albumartists_credit==['Stage','Legal'] and media.albumartists_sort==['Stage','Legal']


def test_display_alias_rename_keeps_collaborators_and_identity_fields(setup):
    from beets_mvp import _expanded_artist_edit
    app,library,ids,paths=setup;item=library.get_item(ids[0]);item.artist='Old'
    item.artists=['Old','Guest'];item.artists_credit=['Old','Guest'];item.mb_artistids=['primary-id','guest-id']
    values=_expanded_artist_edit(item,{'artist':'New'},edit_credits=True)
    assert values['artists']==values['artists_credit']==values['artists_sort']==['New','Guest']
    assert 'mb_artistids' not in values and item.mb_artistids==['primary-id','guest-id']
