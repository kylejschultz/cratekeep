"""Queued matching shares safe reuse and machine-readable cooldowns on both sources."""
import sqlite3

import pytest
from beets_mvp.musicbrainz import ProviderError
from test_app import make_app, tagged_wav, exact_candidate


def fixture(tmp_path, source):
    app=make_app(tmp_path);client=app.test_client()
    path=tmp_path/source/'Artist'/'Album'/'01 Song.wav'
    tagged_wav(path,artist='Artist',album='Album',title='Song')
    if source=='inbox':
        album=client.get('/api/inbox/reviews').json['albums'][0]
        endpoint='/api/inbox/candidates'
    else:
        client.post('/api/library/inventory/preview')
        album=client.get('/api/library-import/queue').json['albums'][0]
        endpoint='/api/library-import/candidates'
    body={'album_id':album['id'],'reuse_current':True}
    return app,client,path,endpoint,body


@pytest.mark.parametrize('source',['inbox','library'])
def test_queue_reuses_saved_matches_and_refresh_is_explicit(tmp_path,source):
    app,client,path,endpoint,body=fixture(tmp_path,source)
    calls=[]
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,*,limit: calls.append(query) or [exact_candidate(query)]
    before=path.read_bytes()
    first=client.post(endpoint,json=body)
    assert first.status_code==201
    ids=[row['id'] for row in first.json['album']['candidates']]
    reused=client.post(endpoint,json=body)
    result=reused.json.get('result',reused.json)
    assert result['already_matched'] is True and len(calls)==1
    assert [row['id'] for row in reused.json['album']['candidates']]==ids
    assert reused.json['album']['decision']=='pending'
    assert client.post(endpoint,json={**body,'reuse_current':False}).status_code==201
    assert len(calls)==2 and path.read_bytes()==before


@pytest.mark.parametrize('source',['inbox','library'])
def test_changed_source_cannot_reuse_old_match(tmp_path,source):
    app,client,path,endpoint,body=fixture(tmp_path,source)
    calls=[]
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,*,limit: calls.append(query) or [exact_candidate(query)]
    client.post(endpoint,json=body)
    path.write_bytes(path.read_bytes()+b'changed')
    assert client.post(endpoint,json=body).status_code==201
    assert len(calls)==2
    assert client.post(endpoint,json={**body,'reuse_current':'yes'}).status_code==400


@pytest.mark.parametrize('source',['inbox','library'])
def test_cooldown_error_is_retryable_and_does_not_discard_success(tmp_path,source):
    app,client,path,endpoint,body=fixture(tmp_path,source)
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,*,limit:[exact_candidate(query)]
    original=client.post(endpoint,json=body).json['album']
    def unavailable(*args,**kwargs):
        raise ProviderError('MusicBrainz returned HTTP 503',retryable=True,code='provider_cooldown',upstream_status=503,retry_after_seconds=120)
    app.config['MUSICBRAINZ_PROVIDER']=unavailable
    response=client.post(endpoint,json={**body,'reuse_current':False})
    assert response.status_code==207
    error=response.json.get('result',response.json)['errors'][0]
    assert error['retryable'] is True and error['retry_after_seconds']==120
    assert error['upstream_status']==503 and error['code']=='provider_cooldown'
    assert response.json['album']['candidate_status']=='complete'
    assert response.json['album']['candidates']==original['candidates']
    reused=client.post(endpoint,json=body).json
    assert reused.get('result',reused)['already_matched'] is True


@pytest.mark.parametrize('source',['inbox','library'])
def test_empty_results_are_reused_but_incompatible_evidence_is_not(tmp_path,source):
    app,client,path,endpoint,body=fixture(tmp_path,source)
    calls=[]
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,*,limit: calls.append(query) or []
    client.post(endpoint,json=body);client.post(endpoint,json=body)
    assert len(calls)==1
    app.config['MUSICBRAINZ_PROVIDER']=lambda query,*,limit: calls.append(query) or [exact_candidate(query)]
    client.post(endpoint,json={**body,'reuse_current':False})
    with sqlite3.connect(app.config['APP_DB']) as db:
        db.execute('UPDATE metadata_candidates SET schema_version=1')
    client.post(endpoint,json=body)
    assert len(calls)==3
