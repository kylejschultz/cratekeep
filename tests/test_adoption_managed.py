"""Managed files are not new adoption work, including stale pre-fix groups."""
import sqlite3

from beets.library import Item, Library

from test_app import make_app, tagged_wav


def managed_fixture(tmp_path):
    app = make_app(tmp_path)
    path = tmp_path / 'library' / 'Artist' / 'Album' / '01 Managed.wav'
    tagged_wav(path, artist='Artist', album='Album', title='Managed')
    library = Library(app.config['BEETS_DB'], directory=app.config['LIBRARY_PATH'])
    item = Item.from_path(str(path))
    library.add_album([item])
    return app, library, item, path


def refresh(client):
    result = client.post('/api/library-import/queue/refresh', json={})
    assert result.status_code == 200, result.json
    return result.json


def test_managed_library_files_do_not_enter_adoption_queue(tmp_path):
    app, library, item, path = managed_fixture(tmp_path)
    before = path.read_bytes()
    client = app.test_client()
    refresh(client)
    refresh(client)
    assert client.get('/api/library-import/queue').json['total'] == 0
    assert path.read_bytes() == before
    assert library.get_item(item.id) is not None


def test_unchanged_refresh_repairs_stale_managed_album_group(tmp_path):
    app, library, item, path = managed_fixture(tmp_path)
    client = app.test_client()
    refresh(client)
    with sqlite3.connect(app.config['APP_DB']) as db:
        inventory_id = db.execute('SELECT id FROM library_inventory WHERE beets_item_id=?', (item.id,)).fetchone()[0]
        album_id = db.execute('''INSERT INTO album_reviews(root_path,artist_key,album_key,artist,album,updated_at)
            VALUES (?,?,?,?,?,?)''', (app.config['LIBRARY_PATH'], 'old-group', 'album', 'Artist', 'Album', 'before')).lastrowid
        db.execute('INSERT INTO album_review_tracks(album_review_id,inventory_id) VALUES (?,?)', (album_id,inventory_id))
    assert client.get('/api/library-import/queue').json['total'] == 0
    before = path.read_bytes()
    result = refresh(client)
    assert all(not folder['changed'] for folder in result['folders'])
    assert client.get('/api/library-import/queue').json['total'] == 0
    with sqlite3.connect(app.config['APP_DB']) as db:
        assert db.execute('SELECT COUNT(*) FROM album_review_tracks WHERE album_review_id=?', (album_id,)).fetchone()[0] == 0
    assert path.read_bytes() == before
    assert library.get_item(item.id) is not None


def test_reconciliation_retains_unmanaged_tracks_and_invalidates_old_match(tmp_path):
    app, library, item, managed = managed_fixture(tmp_path)
    pending = managed.with_name('02 Pending.wav')
    tagged_wav(pending, artist='Artist', album='Album', title='Pending')
    client = app.test_client()
    refresh(client)
    album = client.get('/api/library-import/queue').json['albums'][0]
    assert len(album['tracks']) == 1
    with sqlite3.connect(app.config['APP_DB']) as db:
        inventory_id = db.execute('SELECT id FROM library_inventory WHERE beets_item_id=?', (item.id,)).fetchone()[0]
        db.execute('INSERT INTO album_review_tracks(album_review_id,inventory_id) VALUES (?,?)', (album['id'],inventory_id))
        db.execute("UPDATE album_reviews SET candidate_status='complete' WHERE id=?", (album['id'],))
    before = {path: path.read_bytes() for path in (managed,pending)}
    result = refresh(client)
    assert all(not folder['changed'] for folder in result['folders'])
    queue = client.get('/api/library-import/queue').json
    assert queue['total'] == 1
    assert [track['title'] for track in queue['albums'][0]['tracks']] == ['Pending']
    assert queue['albums'][0]['candidate_status'] == 'stale'
    assert all(path.read_bytes() == content for path,content in before.items())
    assert len(list(library.items())) == 1
