"""Artist browsing is independently paginated and drills into exact album artists."""
from urllib.parse import urlencode
from beets.library import Library, Item
from test_app import make_app, ui_bootstrap


def add_record(app, tmp_path, artist, title, genre='Rock', track_artist=None):
    library = Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH'])
    return library.add_album([Item(path=str(tmp_path/'library'/artist/title/'song.wav'), album=title,
                                   albumartist=artist, artist=track_artist or artist, genre=genre, title='Song')])


def test_artist_groups_paginate_independently_and_do_not_load_album_payloads(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    for index in range(26):
        add_record(app, tmp_path, f'Artist {index:02d}', f'Record {index:02d}')
    add_record(app, tmp_path, 'Artist 00', 'Second record', genre='Jazz')
    def no_album_details(*args, **kwargs):
        raise AssertionError('Artist browse loaded full album details')
    monkeypatch.setattr('beets_mvp._managed_album_summary', no_album_details)
    client = app.test_client()
    first = ui_bootstrap(client.get('/library?view=artists'))['data']
    second = ui_bootstrap(client.get('/library?view=artists&page=2'))['data']
    assert first['view'] == 'artists' and first['matching_artists'] == 26
    assert len(first['artists']) == 25 and len(second['artists']) == 1
    assert not set(a['artist'] for a in first['artists']) & set(a['artist'] for a in second['artists'])
    artist = first['artists'][0]
    assert artist['album_count'] == 2 and set(artist['genres'].split(', ')) == {'Rock','Jazz'}
    assert not first['albums'] and not first['recent_albums']
    assert ui_bootstrap(client.get('/library?view=artists&page=999'))['data']['page'] == 2


def test_artist_unicode_grouping_search_and_exact_drilldown(tmp_path):
    app = make_app(tmp_path)
    add_record(app, tmp_path, 'Élan & Co', 'First')
    add_record(app, tmp_path, 'éLAN & CO', 'Second', genre='Jazz, Fusion')
    add_record(app, tmp_path, 'Other', 'Élan title')
    client = app.test_client()
    data = ui_bootstrap(client.get('/library?'+urlencode({'view':'artists','q':'ÉLAN'})))['data']
    assert len(data['artists']) == 1 and data['artists'][0]['album_count'] == 2
    assert 'Jazz, Fusion' in data['artists'][0]['genres']
    albums = ui_bootstrap(client.get('/library?'+urlencode({'artist':data['artists'][0]['artist']})))['data']
    assert albums['matching_albums'] == 2
    assert {album['album'] for album in albums['albums']} == {'First','Second'}


def test_artist_empty_fallback_and_literal_search(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    assert ui_bootstrap(client.get('/library?view=artists'))['data']['artists'] == []
    add_record(app,tmp_path,'','Fallback album',track_artist='Fallback')
    add_record(app,tmp_path,'100% Club','Another')
    data = ui_bootstrap(client.get('/library?'+urlencode({'view':'artists','q':'%'})))['data']
    assert len(data['artists']) == 1 and data['artists'][0]['artist'] == '100% Club'
    fallback = ui_bootstrap(client.get('/library?artist=Fallback'))['data']
    assert fallback['matching_albums'] == 1


def test_overview_recent_albums_are_bounded_to_twelve_in_recent_order(tmp_path):
    app = make_app(tmp_path)
    for index in range(15):
        add_record(app, tmp_path, 'Artist', f'Record {index:02d}')
    albums = ui_bootstrap(app.test_client().get('/'))['data']['recent_albums']
    assert len(albums) == 12
    assert [row['album'] for row in albums] == [f'Record {index:02d}' for index in range(14, 2, -1)]


def test_library_defaults_to_newest_added_and_respects_explicit_sorts(tmp_path):
    app = make_app(tmp_path)
    for index in range(3):
        record = add_record(app, tmp_path, 'Artist', f'Record {index}')
        record.added = 100 + index
        record.store()
    client = app.test_client()
    newest = ui_bootstrap(client.get('/library'))['data']
    assert (newest['sort'], newest['order']) == ('recent', 'desc')
    assert [row['album'] for row in newest['albums']] == ['Record 2', 'Record 1', 'Record 0']
    explicit = ui_bootstrap(client.get('/library?sort=album&order=asc'))['data']
    assert [row['album'] for row in explicit['albums']] == ['Record 0', 'Record 1', 'Record 2']
    assert ui_bootstrap(client.get('/library?view=artists'))['data']['order'] == 'asc'
