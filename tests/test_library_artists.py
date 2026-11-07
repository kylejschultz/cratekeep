"""Artist browsing is independently paginated and drills into exact album artists."""
import os
from pathlib import Path
from urllib.parse import urlencode

import pytest
from beets.library import Library, Item
from mediafile import MediaFile

from beets_mvp.musicbrainz import _normalize_release
from test_app import make_app, tagged_inbox_wav, ui_bootstrap


def add_record(app, tmp_path, artist, title, genre='Rock', track_artist=None):
    library = Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH'])
    return library.add_album([Item(path=str(tmp_path/'library'/artist/title/'song.wav'), album=title,
                                   albumartist=artist, artist=track_artist or artist, genre=genre, title='Song')])


def test_artist_groups_paginate_independently_and_do_not_load_album_payloads(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    for index in range(26):
        add_record(app, tmp_path, f'Artist {index:02d}', f'Record {index:02d}')
    collaboration = add_record(app, tmp_path, 'Artist 00 with Artist 25', 'Second record', genre='Jazz')
    collaboration.albumartists = ['Artist 00', 'Artist 25']
    collaboration.store()

    def no_album_details(*args, **kwargs):
        raise AssertionError('Artist browse loaded full album details')
    monkeypatch.setattr('beets_mvp._managed_album_summary', no_album_details)
    client = app.test_client()
    first = ui_bootstrap(client.get('/library?view=artists'))['data']
    second = ui_bootstrap(client.get('/library?view=artists&page=2'))['data']
    assert first['view'] == 'artists' and first['matching_artists'] == 26
    assert len(first['artists']) == 25 and len(second['artists']) == 1
    assert second['artists'][0]['album_count'] == 2
    assert not set(a['artist'] for a in first['artists']) & set(a['artist'] for a in second['artists'])
    artist = first['artists'][0]
    assert artist['album_count'] == 2 and set(artist['genres'].split(', ')) == {'Rock','Jazz'}
    assert not first['albums'] and not first['recent_albums']
    assert ui_bootstrap(client.get('/library?view=artists&page=999'))['data']['page'] == 2


def test_artist_unicode_grouping_search_and_exact_drilldown(tmp_path):
    app = make_app(tmp_path)
    add_record(app, tmp_path, 'Élan & Co', 'First')
    collaboration = add_record(app, tmp_path, 'éLAN & CO with Tyler, The Creator', 'Second', genre='Jazz, Fusion')
    collaboration.albumartists = ['éLAN & CO', 'Tyler, The Creator']
    collaboration.store()
    add_record(app, tmp_path, 'Other', 'Élan title')
    client = app.test_client()
    data = ui_bootstrap(client.get('/library?'+urlencode({'view':'artists','q':'ÉLAN'})))['data']
    assert len(data['artists']) == 1 and data['artists'][0]['album_count'] == 2
    assert 'Jazz, Fusion' in data['artists'][0]['genres']
    albums = ui_bootstrap(client.get('/library?'+urlencode({'artist':data['artists'][0]['artist']})))['data']
    assert albums['matching_albums'] == 2
    assert {album['album'] for album in albums['albums']} == {'First','Second'}
    punctuation = ui_bootstrap(client.get('/library?' + urlencode({'artist': 'Tyler, The Creator'})))['data']
    assert [album['id'] for album in punctuation['albums']] == [collaboration.id]


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

def test_collaborations_share_one_album_identity_across_primary_artists(tmp_path):
    app = make_app(tmp_path)
    throne = add_record(app, tmp_path, 'JAY-Z & Kanye West', 'Watch the Throne',
                        track_artist='Track Guest')
    collision = add_record(app, tmp_path, 'Linkin Park and JAY-Z', 'Collision Course', genre='Metal')
    throne.albumartists = ['JAY-Z', 'Kanye West', 'jay-z']
    throne.store()
    # A legacy album can have structured Album Artists only on its tracks.
    item = next(iter(collision.items()))
    item.albumartists = ['Linkin Park', 'JAY-Z']
    item.store()
    legacy = add_record(app, tmp_path, 'Simon & Garfunkel', 'Legacy record')

    client = app.test_client()

    def browse(**params):
        return ui_bootstrap(client.get('/library?' + urlencode(params)))['data']

    artists = browse(view='artists')
    assert {a['artist']: a['album_count'] for a in artists['artists']} == {
        'JAY-Z': 2, 'Kanye West': 1, 'Linkin Park': 1, 'Simon & Garfunkel': 1,
    }
    assert artists['summary']['albums'] == artists['summary']['tracks'] == 3
    assert artists['summary']['artists'] == 4
    assert artists['top_artists'][0] == {'artist': 'JAY-Z', 'albums': 2, 'tracks': 2}
    assert browse(view='artists', q='kanye')['matching_artists'] == 1
    assert browse(view='artists', q='Track Guest')['matching_artists'] == 0
    assert {a['id'] for a in browse(artist='jay-z')['albums']} == {throne.id, collision.id}
    assert [a['id'] for a in browse(artist='Kanye West')['albums']] == [throne.id]
    assert [a['id'] for a in browse(artist='Linkin Park')['albums']] == [collision.id]
    assert [a['id'] for a in browse(q='Kanye West')['albums']] == [throne.id]
    assert [a['id'] for a in browse(q='Track Guest')['albums']] == [throne.id]
    assert [a['id'] for a in browse(artist='Simon & Garfunkel')['albums']] == [legacy.id]
    assert browse(artist='Garfunkel')['matching_albums'] == 0
    assert {a['id'] for a in browse()['albums']} == {throne.id, collision.id, legacy.id}


@pytest.mark.parametrize(("flow", "keep_existing"), [
    ("inbox", False), ("adopt", False), ("rematch", False), ("inbox", True), ("adopt", True),
])
def test_reviewed_album_credits_reach_tags_and_artist_browse(tmp_path, monkeypatch, flow, keep_existing):
    monkeypatch.setenv("PATH", f"{Path(os.sys.executable).parent}:{os.environ.get('PATH', '')}")
    app = make_app(tmp_path)
    client = app.test_client()
    album_title = "Collision Course"
    display = "Linkin Park and JAY-Z"
    names = ["Linkin Park", "JAY-Z"]
    artist_ids = ["123e4567-e89b-42d3-a456-426614174001", "123e4567-e89b-42d3-a456-426614174002"]
    release_id = "123e4567-e89b-42d3-a456-426614174000"
    candidate = _normalize_release({
        "id": release_id, "title": album_title, "track-count": 1,
        "artist-credit": [
            {"name": names[0], "joinphrase": " and ", "artist": {"id": artist_ids[0]}},
            {"artist": {"name": names[1], "id": artist_ids[1]}},
        ],
        "media": [{"position": 1, "tracks": [{
            "position": 1, "title": "Song", "length": 1000,
            "recording": {"id": "123e4567-e89b-42d3-a456-426614174003"},
            "artist-credit": [{"name": "Track Guest"}],
        }]}],
    }, exact=True)
    app.config["MUSICBRAINZ_PROVIDER"] = lambda query, limit: [candidate]
    path = tmp_path / ("inbox" if flow == "inbox" else "library") / album_title / "01 Song.wav"
    tagged_inbox_wav(path, artist=display, album=album_title)
    media = MediaFile(path)
    media.artist = "Track Guest"
    media.albumartists = names if keep_existing else [display]
    if keep_existing:
        media.mb_albumartistids = artist_ids
    media.disc = 1
    media.save()
    before = path.read_bytes()
    library = Library(app.config["BEETS_DB"], directory=app.config["LIBRARY_PATH"])

    if flow == "rematch":
        album = library.add_album([Item.from_path(path)])
        base = f"/api/library/albums/{album.id}"
        opened = client.get(base).json
        preview = client.post(base + "/rematch/preview", json={
            "musicbrainz_id": release_id, "expected_fingerprint": opened["fingerprint"],
        })
        assert preview.status_code == 201, preview.json
        assert preview.json["changes"][0]["changes"]["albumartists"]["to"] == names
        assert path.read_bytes() == before
        result = client.post(base + "/rematch/apply", json={
            "preview_id": preview.json["preview_id"], "expected_fingerprint": opened["fingerprint"],
        })
        assert result.status_code == 200, result.json
        assert result.json["changes"] == preview.json["changes"]
    else:
        prefix = "/api/inbox" if flow == "inbox" else "/api/library-import"
        if flow == "adopt":
            assert client.post("/api/library/inventory/preview").status_code == 201
        review = client.get(prefix + "/reviews").json["albums"][0]
        candidate_id = None
        if not keep_existing:
            matched = client.post(prefix + "/candidates", json={"album_id": review["id"]})
            assert matched.status_code == 201, matched.json
            review = client.get(prefix + "/reviews").json["albums"][0]
            proposed = review["candidates"][0]
            assert proposed["proposed_diff"]["album_artist_credits"]["to"] == names
            candidate_id = proposed["id"]
        base = f"{prefix}/albums/{review['id']}"
        assert client.patch(base, json={"decision": "approved", "candidate_id": candidate_id}).status_code == 200
        preview = client.post(base + "/execute", json={"dry_run": True})
        assert preview.status_code == 200, preview.json
        if flow == "adopt" and not keep_existing:
            assert preview.json["items"][0]["changes"]["albumartists"]["to"] == names
        assert path.read_bytes() == before
        result = client.post(base + "/execute", json={})
        assert result.status_code == 201, result.json
        if flow == "adopt":
            assert result.json["items"][0]["changes"] == preview.json["items"][0]["changes"]
        if flow == "inbox" or keep_existing:
            assert path.read_bytes() == before

    album, = library.albums()
    item, = album.items()
    tags = MediaFile(item.path)
    assert tags.albumartist == item.albumartist == album.albumartist == display
    assert tags.albumartists == item.albumartists == album.albumartists == names
    assert tags.mb_albumartistids == item.mb_albumartistids == album.mb_albumartistids == artist_ids
    assert tags.artist == item.artist == "Track Guest"
    if flow == "adopt":
        assert item.path == os.fsencode(path)

    managed_path = Path(os.fsdecode(item.path))
    managed_bytes = managed_path.read_bytes()
    for artist in names:
        filtered = ui_bootstrap(client.get("/library?" + urlencode({"artist": artist})))["data"]
        assert [record["id"] for record in filtered["albums"]] == [album.id]
    assert managed_path.read_bytes() == managed_bytes
    assert library.get_album(album.id).albumartists == names
