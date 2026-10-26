"""Flat album queue stays paginated, source-scoped and provider-free on load."""
import sqlite3

from test_app import make_app, tagged_inbox_wav


def test_album_queue_paging_filters_and_source_isolation(tmp_path):
    app = make_app(tmp_path)
    calls = []
    app.config['MUSICBRAINZ_PROVIDER'] = lambda query, **kwargs: calls.append(query) or []
    client = app.test_client()
    inbox_id = app.config['INBOXES'][0]['id']
    base = f'/api/inbox/{inbox_id}'
    # Loading inventory is read-only, including when no scan has happened yet.
    tagged_inbox_wav(tmp_path/'inbox/Artist/Album/Song.wav', album='Élan')
    assert client.get(base+'/queue').json['total'] == 0
    for index in range(25):
        tagged_inbox_wav(tmp_path/f'inbox/Artist/Album {index:02d}/Song.wav', album=f'Album {index:02d}')
    refreshed = client.post(base+'/scopes/refresh', json={})
    assert refreshed.status_code == 200, refreshed.json
    first = client.get(base+'/queue').json
    second = client.get(base+'/queue?page=2').json
    assert first['total'] == 26 and len(first['albums']) == 25
    assert second['page'] == 2 and len(second['albums']) == 1
    assert not {a['id'] for a in first['albums']} & {a['id'] for a in second['albums']}
    assert client.get(base+'/queue?q=ÉLAN').json['total'] == 1
    assert client.get(base+'/queue?page=999').json['page'] == 2
    album_id = first['albums'][0]['id']
    assert client.patch(f'/api/inbox/albums/{album_id}', json={'decision':'approved', 'candidate_id':None}).status_code == 200
    assert client.get(base+'/queue?status=unreviewed').json['total'] == 25
    assert client.get('/api/inbox/missing/queue').status_code == 404
    assert client.get('/api/library-import/queue').json['total'] == 0
    assert not calls
    for suffix in ['?page=0','?page=oops','?status=other']:
        assert client.get(base+'/queue'+suffix).status_code == 400


def test_library_refresh_discovers_files_without_matching_and_reconciles_deletions(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    path = tmp_path/'library/Artist/Album/Song.wav'
    tagged_inbox_wav(path)
    before = path.read_bytes()
    def unexpected_provider(*args, **kwargs):
        raise AssertionError('Refresh or navigation started provider matching')
    app.config['MUSICBRAINZ_PROVIDER'] = unexpected_provider
    refreshed = client.post('/api/library-import/queue/refresh', json={})
    assert refreshed.status_code == 200, refreshed.json
    queue = client.get('/api/library-import/queue').json
    assert queue['total'] == 1 and len(queue['albums'][0]['tracks']) == 1
    assert queue['albums'][0]['candidate_status'] == 'stale'
    assert path.read_bytes() == before
    assert client.post('/api/library-import/queue/refresh', json={'match':True}).status_code == 400
    # Complete reviews stay hidden; removal's existing reconciliation reopens
    # kept files, and the flat listing must expose the resulting group.
    with sqlite3.connect(app.config['APP_DB']) as db:
        db.execute("UPDATE album_reviews SET execution_status='complete'")
    assert client.get('/api/library-import/queue').json['total'] == 0
    path.unlink()
    path.parent.rmdir()
    path.parent.parent.rmdir()
    assert client.post('/api/library-import/queue/refresh', json={}).status_code == 200
    assert client.get('/api/library-import/queue').json['total'] == 0


def test_refresh_removes_entire_missing_folder_from_flat_queue(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    path = tmp_path/'library/Gone/Song.wav'
    tagged_inbox_wav(path)
    assert client.post('/api/library-import/queue/refresh', json={}).status_code == 200
    assert client.get('/api/library-import/queue').json['total'] == 1
    path.unlink(); path.parent.rmdir()
    assert client.post('/api/library-import/queue/refresh', json={}).status_code == 200
    assert client.get('/api/library-import/queue').json['total'] == 0
    # A missing root is an actionable JSON failure, not a server traceback.
    path.parent.parent.rmdir()
    unavailable = client.get('/api/library-import/queue')
    assert unavailable.status_code == 409 and unavailable.json['error']


def test_ui_assets_follow_the_server_build_on_every_page(tmp_path):
    import re
    from pathlib import Path
    app=make_app(tmp_path);client=app.test_client()
    routes=['/','/inbox','/library-import','/library','/settings']
    previous={}
    for build in ['first-candidate','next-candidate']:
        app.config['BUILD_SHA']=build
        for route in routes:
            page=client.get(route)
            assert page.status_code==200
            urls=re.findall(r'(?:src|href)="([^"]+ui/cratekeep\.(?:js|css)[^"]*)"',page.get_data(as_text=True))
            assert len(urls)==2
            for url in urls:
                assert url.endswith('?v='+build)
                response=client.get(url)
                asset=Path(url.split('?')[0]).name
                assert response.status_code==200
                assert response.data==(Path(app.static_folder)/'ui'/asset).read_bytes()
            if route in previous:assert urls!=previous[route]
            previous[route]=urls
