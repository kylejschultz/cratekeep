"""Release-specific titles survive provider normalization and actual Inbox import."""
from beets.library import Library
from beets_mvp.musicbrainz import _normalize_release
from test_app import tagged_inbox_wav
from test_inbox_copies import app, imports_base


def test_live_demo_remix_titles_are_written_without_changing_originals(app, tmp_path):
    titles = ['A Place for My Head (live at Docklands Arena, London)',
              'Step Up (1999 demo)', 'Buy Myself (Marilyn Manson remix)']
    recording_titles = ['A Place for My Head', 'Step Up', 'Buy Myself Remix']
    sources = []
    for index, title in enumerate(titles, 1):
        path = tmp_path / f'inbox/Album/{index}.wav'
        tagged_inbox_wav(path, title=title.upper(), track=index)
        sources.append(path)
    originals = [path.read_bytes() for path in sources]
    client = app.test_client(); base = imports_base(app)
    client.post(base + '/scopes/refresh', json={})
    album = client.get(base + '/queue').json['albums'][0]
    def provider(query, *, limit):
        tracks = [{'position': index, 'title': title, 'length': 1000,
                   'recording': {'id': f'recording-{index}', 'title': recording_titles[index-1]}}
                  for index, title in enumerate(titles, 1)]
        return [_normalize_release({'id': 'live-release', 'title': 'Album',
                  'artist-credit': [{'name': 'Artist'}],
                  'media': [{'position': 1, 'tracks': tracks}]}, exact=True)]
    app.config['MUSICBRAINZ_PROVIDER'] = provider
    response = client.post('/api/inbox/candidates', json={'album_id': album['id']})
    assert response.status_code == 201
    assert [row['title'] for row in response.json['album']['candidates'][0]['recordings']] == titles
    result = client.post(base + '/bulk/execute', json={'album_ids': [album['id']], 'scopes': ['Album'], 'mode': 'selected', 'confirmed': True})
    assert result.status_code == 201 and len(result.json['imported']) == 1, result.json
    assert {item.title for item in Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH']).items()} == set(titles)
    assert [path.read_bytes() for path in sources] == originals
