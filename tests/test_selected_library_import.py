"""Explicit selection adopts saved matches safely without the ready-only score gate."""
import sqlite3
from pathlib import Path

import pytest
from beets.library import Library, Item
from beets_mvp import create_app
from beets_mvp.musicbrainz import ProviderError
from test_app import make_app, tagged_wav, exact_candidate


def fixture(tmp_path, *, tied=False, partial=False):
    app = make_app(tmp_path)
    path = tmp_path/'library'/'Artist'/'Album'/'01 Song.wav'
    tagged_wav(path, artist='Artist', album='Album', title='Song')
    if partial:
        tagged_wav(path.with_name('02 Missing.wav'), artist='Artist', album='Album', title='Missing')
    def provider(query, *, limit):
        result = exact_candidate(query)
        result['release_group_id'] = 'fixture-album-group'
        result['artist'] = query['artist'] + ' Ensemble'  # A score penalty is not an import prohibition.
        if partial:
            result['tracks'] = result['tracks'][:1]
        return [result, {**result, 'provider_id':'another-release'}] if tied else [result]
    app.config['MUSICBRAINZ_PROVIDER'] = provider
    client = app.test_client()
    client.post('/api/library/inventory/preview')
    assert client.post('/api/library-import/candidates',json={}).status_code == 201
    album = client.get('/api/library-import/reviews').json['albums'][0]
    body = {'folders':['Artist'], 'album_ids':[album['id']], 'mode':'selected'}
    return app, client, album, body, path


@pytest.mark.parametrize('tied',[False,True])
def test_selected_import_approves_saved_suggestion_without_score_or_tie_gate(tmp_path,tied):
    app, client, album, body, path = fixture(tmp_path,tied=tied)
    with sqlite3.connect(app.config['APP_DB']) as db:
        db.execute('UPDATE album_reviews SET selected_candidate_id=NULL')
    app.config['MATCH_THRESHOLD'] = 100
    assert 0 < album['candidates'][0]['confidence'] < 1
    app.config['MUSICBRAINZ_PROVIDER'] = lambda *args, **kwargs: pytest.fail('Import contacted provider')
    preview = client.post('/api/library-import/bulk/preview',json=body).json
    assert preview['importable_album_count'] == 1
    assert client.post('/api/library-import/bulk/execute',json=body).status_code == 400
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True})
    assert result.status_code == 201 and len(result.json['imported']) == 1
    items = list(Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH']).items())
    assert len(items) == 1 and Path(items[0].path.decode()) == path
    assert path.exists()


def test_selected_import_preserves_as_is_and_track_exclusions(tmp_path):
    app, client, album, body, path = fixture(tmp_path,partial=True)
    original = path.read_bytes()
    client.patch(f"/api/library-import/albums/{album['id']}",json={
        'decision':'approved','candidate_id':None,
        'track_exceptions':{str(album['tracks'][1]['id']):'skipped'}})
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True})
    assert len(result.json['imported']) == 1
    assert path.read_bytes() == original and path.with_name('02 Missing.wav').exists()
    assert len(list(Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH']).items())) == 1


def test_selected_import_keeps_unpaired_or_excluded_albums_and_files(tmp_path):
    app, client, album, body, path = fixture(tmp_path,partial=True)
    before = path.read_bytes()
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert not result['imported'] and 'pairing' in result['skipped'][0]['reason']
    client.patch(f"/api/library-import/albums/{album['id']}",json={'decision':'rejected'})
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert 'excluded' in result['skipped'][0]['reason']
    assert path.read_bytes() == before and not list(Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH']).items())


@pytest.mark.parametrize('action',['keep-existing','keep-both'])
def test_selected_duplicates_preserve_both_file_paths_and_existing_bytes(tmp_path,action):
    app, client, album, body, incoming = fixture(tmp_path)
    managed_path = tmp_path/'library'/'Managed'/'01 Song.wav'
    tagged_wav(managed_path,artist='Artist',album='Existing',title='Song')
    lib = Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH'])
    lib.add(Item(path=str(managed_path),title='Song',artist='Artist',album='Existing',mb_trackid='recording-release-1-1',mb_releasegroupid='fixture-album-group'))
    before = {p:p.read_bytes() for p in (incoming,managed_path)}
    blocked = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert not blocked['imported'] and 'duplicates' in blocked['skipped'][0]['reason']
    client.patch(f"/api/library-import/albums/{album['id']}",json={'duplicate_action':action})
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert len(result['imported']) == 1 and not result['failed']
    assert managed_path.read_bytes() == before[managed_path]
    assert incoming.exists()
    if action == 'keep-existing':
        assert incoming.read_bytes() == before[incoming]
    assert len(list(lib.items())) == (1 if action == 'keep-existing' else 2)


def test_import_revalidates_disk_and_scope_without_provider_lookup(tmp_path):
    app, client, album, body, path = fixture(tmp_path)
    assert client.post('/api/library-import/bulk/preview',json={**body,'album_ids':[]}).status_code == 400
    assert client.post('/api/library-import/bulk/preview',json={**body,'folders':['Other']}).status_code == 400
    path.write_bytes(path.read_bytes()+b'changed')
    changed = path.read_bytes()
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert not result['imported'] and result['failed']
    assert path.read_bytes() == changed and not list(Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH']).items())


def test_failed_optional_refresh_and_restart_retain_usable_saved_match(tmp_path):
    app, client, album, body, path = fixture(tmp_path)
    def unavailable(*args,**kwargs):
        raise ProviderError('503',retryable=True)
    app.config['MUSICBRAINZ_PROVIDER'] = unavailable
    assert client.post('/api/library-import/candidates',json={'album_id':album['id']}).status_code == 207
    saved = client.get('/api/library-import/reviews').json['albums'][0]
    assert saved['candidate_status'] == 'complete' and not saved['candidate_error']
    restarted = create_app({'TESTING':True,'STATE_PATH':str(tmp_path/'config'),'MUSICBRAINZ_PROVIDER':unavailable})
    result = restarted.test_client().post('/api/library-import/bulk/execute',json={**body,'confirmed':True})
    assert len(result.json['imported']) == 1 and path.exists()


def test_perfect_filter_uses_exact_score_and_filters_before_paging(tmp_path):
    app = make_app(tmp_path)
    for index in range(26):
        tagged_wav(tmp_path/'library'/'Artist'/f'Album {index:02d}'/'01 Song.wav',artist='Artist',album=f'Album {index:02d}',title='Song')
    tagged_wav(tmp_path/'library'/'Artist'/'Near'/'01 Song.wav',artist='Artist',album='Near',title='Song')
    def provider(query, *, limit):
        result = exact_candidate(query,provider_id=query['album'])
        if query['album'] == 'Near':
            result['artist'] = 'Different Artist'
        return [result]
    app.config['MUSICBRAINZ_PROVIDER'] = provider
    client = app.test_client()
    client.post('/api/library/inventory/preview')
    for album in client.get('/api/library-import/queue').json['albums'] + client.get('/api/library-import/queue?page=2').json['albums']:
        assert client.post('/api/library-import/candidates',json={'album_id':album['id']}).status_code == 201
    app.config['MUSICBRAINZ_PROVIDER'] = lambda *a,**kw: pytest.fail('Queue filter contacted provider')
    first = client.get('/api/library-import/queue?status=perfect').json
    second = client.get('/api/library-import/queue?status=perfect&page=2').json
    assert first['total'] == 26 and first['page_count'] == 2
    assert len(first['albums']) == 25 and len(second['albums']) == 1
    assert not {a['id'] for a in first['albums']} & {a['id'] for a in second['albums']}
    assert client.get('/api/library-import/queue?status=perfect&q=Near').json['total'] == 0
    assert client.get('/api/library-import/queue?status=perfect&page=999').json['page'] == 2
    assert client.get('/api/library-import/queue?status=duplicates').json['total'] == 0


def test_newly_managed_duplicate_is_seen_by_filter_and_between_selected_albums(tmp_path):
    app, client, album, body, path = fixture(tmp_path)
    managed_path = tmp_path/'library'/'Managed.wav'
    tagged_wav(managed_path,artist='Artist',album='Existing',title='Song')
    lib = Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH'])
    lib.add(Item(path=str(managed_path),mb_trackid='recording-release-1-1',mb_releasegroupid='fixture-album-group'))
    assert client.get('/api/library-import/queue?status=duplicates').json['total'] == 1
    assert client.get('/api/library-import/queue?status=perfect').json['total'] == 0


def test_failed_first_match_does_not_invent_saved_evidence(tmp_path):
    app = make_app(tmp_path)
    tagged_wav(tmp_path/'library'/'Song.wav',artist='Artist',album='Album',title='Song')
    client = app.test_client(); client.post('/api/library/inventory/preview')
    def failed(*args,**kwargs):
        raise ProviderError('503',retryable=True)
    app.config['MUSICBRAINZ_PROVIDER'] = failed
    assert client.post('/api/library-import/candidates',json={}).status_code == 207
    assert client.get('/api/library-import/reviews').json['albums'][0]['candidate_status'] == 'error'


def test_selected_batch_detects_duplicates_created_by_earlier_album(tmp_path):
    app, client, album, body, path = fixture(tmp_path)
    second = path.parent.parent/'Second'/'01 Song.wav'
    tagged_wav(second,artist='Artist',album='Second',title='Song')
    client.post('/api/library/inventory/preview')
    client.post('/api/library-import/candidates',json={})
    albums = client.get('/api/library-import/queue').json['albums']
    before = second.read_bytes()
    result = client.post('/api/library-import/bulk/execute',json={**body,'album_ids':[row['id'] for row in albums],'confirmed':True}).json
    assert len(result['imported']) == 1 and len(result['skipped']) == 1
    assert 'duplicates' in result['skipped'][0]['reason']
    assert path.exists() and second.read_bytes() == before
    assert len(list(Library(app.config['BEETS_DB'],directory=app.config['LIBRARY_PATH']).items())) == 1


def test_changed_source_does_not_retain_complete_status_after_failed_refresh(tmp_path):
    app, client, album, body, path = fixture(tmp_path)
    path.write_bytes(path.read_bytes()+b'changed')
    def unavailable(*args,**kwargs):
        raise ProviderError('503',retryable=True)
    app.config['MUSICBRAINZ_PROVIDER'] = unavailable
    assert client.post('/api/library-import/candidates',json={'album_id':album['id']}).status_code == 207
    current = client.get('/api/library-import/queue').json['albums'][0]
    assert current['candidate_status'] != 'complete'
    result = client.post('/api/library-import/bulk/execute',json={**body,'confirmed':True}).json
    assert not result['imported'] and result['skipped']


def test_compatible_evidence_survives_new_presentation_schema(tmp_path,monkeypatch):
    app, client, album, body, path = fixture(tmp_path)
    monkeypatch.setattr('beets_mvp.CANDIDATE_SCHEMA_VERSION',6)
    restarted = create_app({'TESTING':True,'STATE_PATH':str(tmp_path/'config')})
    current = restarted.test_client().get('/api/library-import/queue').json['albums'][0]
    assert current['candidate_status'] == 'complete'
    assert current['candidates'][0]['id'] == album['candidates'][0]['id']
