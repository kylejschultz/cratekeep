"""Explicit and persistent album relocation uses generated audio only."""
import os
from pathlib import Path
import pytest
from mediafile import MediaFile
from test_app import make_app, managed_wav_album

@pytest.fixture
def case(tmp_path):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path, titles=('First', 'Second'))
    return app, library, album, paths, app.test_client()


def edit(case, **values):
    app, library, album, paths, client = case
    payload = {'expected_fingerprint': client.get(f'/api/library/albums/{album.id}').json['fingerprint'], 'album': {}, 'tracks': [], **values}
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    preview = client.post(endpoint + 'preview', json=payload)
    assert preview.status_code == 200, preview.json
    saved = client.post(endpoint + 'apply', json={**payload, 'plan_fingerprint': preview.json['plan_fingerprint']})
    return preview.json, saved


def test_manual_move_keeps_bytes_and_companions_and_is_persistent(case):
    app, library, album, paths, client = case
    root = Path(app.config['LIBRARY_PATH'])
    paths[0].with_suffix('.lrc').write_text('[00:01.00]Fixture')
    cover = paths[0].parent / 'cover.jpg'; cover.write_bytes(b'fixture cover')
    album.artpath = os.fsencode(cover); album.store()
    note = paths[0].parent / 'notes.nfo'; note.write_text('Unrelated')
    before = [path.read_bytes() for path in paths]
    # Read-only browser and preview must not create the new folder.
    folders = client.get('/api/library/folders').json
    assert any(row['name'] == 'Artist' for row in folders['entries'])
    opened = client.get(f'/api/library/albums/{album.id}').json
    payload = {'expected_fingerprint': opened['fingerprint'], 'album': {}, 'tracks': [], 'location': {'mode': 'manual', 'directory': 'Chosen/Album'}}
    preview = client.post(f'/api/library/albums/{album.id}/metadata/preview', json=payload)
    assert preview.status_code == 200, preview.json
    assert not (root/'Chosen').exists()
    assert [path.read_bytes() for path in paths] == before
    plan, saved = edit(case, location=payload['location'])
    assert saved.status_code == 200, saved.json
    assert plan['location_change']['to'] == 'Chosen/Album'
    new = [root/row['to'] for row in plan['paths']]
    assert [path.read_bytes() for path in new] == before
    assert new[0].with_suffix('.lrc').read_text() == '[00:01.00]Fixture'
    assert (new[0].parent/'cover.jpg').read_bytes() == b'fixture cover'
    assert note.read_text() == 'Unrelated'
    info = client.get(f'/api/library/albums/{album.id}/location').json
    assert info['directory'] == info['source_directory'] == 'Chosen/Album'
    # Legacy album saves/rematches also honor the pinned album folder.
    opened = client.get(f'/api/library/albums/{album.id}').json
    changed = client.patch(f'/api/library/albums/{album.id}', json={'expected_fingerprint': opened['fingerprint'], 'albumartist': 'New Artist'})
    assert changed.status_code == 200, changed.json
    assert all(Path(item.path.decode()).parent == root/'Chosen/Album' for item in library.get_album(album.id).items())
    plan, saved = edit(case, tracks=[{'id': next(iter(album.items())).id, 'title': 'Renamed'}])
    assert saved.status_code == 200, saved.json
    assert plan['paths'][0]['to'] == 'Chosen/Album/01 Renamed.wav'
    plan, saved = edit(case, location={'mode': 'beets'})
    assert saved.status_code == 200, saved.json
    assert all(row['to'].startswith('New Artist/Album/') for row in plan['paths'])
    assert not library.get_album(album.id).get('cratekeep_directory')


def test_old_folder_can_be_organized_without_changing_metadata(case):
    app, library, album, paths, client = case
    album.albumartist = 'Doechii'; album.store()
    for item in album.items():
        item.albumartist = 'Doechii'; item.write(); item.store()
    info = client.get(f'/api/library/albums/{album.id}/location').json
    assert all(row['to'].startswith('Doechii/Album/') for row in info['beets_paths'])
    before = [path.read_bytes() for path in paths]
    plan, saved = edit(case, location={'mode': 'beets'})
    assert saved.status_code == 200 and not plan['changes'], saved.json
    assert [Path(item.path.decode()).read_bytes() for item in library.get_album(album.id).items()] == before
    assert not any(path.exists() for path in paths)


@pytest.mark.parametrize('destination', ['../escape', '/tmp/escape', 'Artist/../escape', '', 'Artist\\escape'])
def test_outside_or_ambiguous_targets_are_rejected(case, destination):
    app, library, album, paths, client = case
    payload = {'expected_fingerprint': client.get(f'/api/library/albums/{album.id}').json['fingerprint'], 'location': {'mode': 'manual', 'directory': destination}}
    result = client.post(f'/api/library/albums/{album.id}/metadata/preview', json=payload)
    assert result.status_code == 400, result.json
    assert all(path.exists() for path in paths)


def test_symlink_browser_and_target_are_rejected(case):
    app, library, album, paths, client = case
    root = Path(app.config['LIBRARY_PATH']); (root/'Linked').symlink_to(root/'Artist', target_is_directory=True)
    assert not any(row['name'] == 'Linked' for row in client.get('/api/library/folders').json['entries'])
    assert client.get('/api/library/folders?path=Linked').status_code == 400
    payload = {'expected_fingerprint': client.get(f'/api/library/albums/{album.id}').json['fingerprint'], 'location': {'mode': 'manual', 'directory': 'Linked/Other'}}
    assert client.post(f'/api/library/albums/{album.id}/metadata/preview', json=payload).status_code == 400


def test_move_failure_restores_policy_files_and_empty_directories(case, monkeypatch):
    import beets_mvp
    app, library, album, paths, client = case
    before = [path.read_bytes() for path in paths]
    original = beets_mvp._copy_managed_exclusive; calls = []
    def fail_second(source, target):
        calls.append(source)
        if len(calls) == 2:
            raise OSError('fixture move failure')
        return original(source, target)
    monkeypatch.setattr(beets_mvp, '_copy_managed_exclusive', fail_second)
    plan, saved = edit(case, location={'mode': 'manual', 'directory': 'New/Folder'})
    assert saved.status_code == 500 and saved.json['code'] == 'mutation_rolled_back'
    assert not library.get_album(album.id).get('cratekeep_directory')
    assert [path.read_bytes() for path in paths] == before
    assert not (Path(app.config['LIBRARY_PATH'])/'New').exists()


def test_disc_subfolders_are_preserved_and_split_albums_explained(case):
    app, library, album, paths, client = case
    old = paths[0].parent
    for disc, item in enumerate(album.items(), 1):
        path = Path(item.path.decode()); target = old/f'Disc {disc}'/path.name
        target.parent.mkdir(); path.rename(target); item.path = os.fsencode(target); item.disc = disc; item.store()
    plan, saved = edit(case, location={'mode': 'manual', 'directory': 'Pinned/Album'})
    assert saved.status_code == 200, saved.json
    assert all(f'Disc {index}' in row['to'] for index, row in enumerate(plan['paths'], 1))
    # Remove the pin and separate the files into unrelated album folders.
    album = library.get_album(album.id); album['cratekeep_directory'] = ''; album.store()
    for index, item in enumerate(album.items(), 1):
        source = Path(item.path.decode()); target = source.parents[2]/f'Unrelated {index}'/source.name
        target.parent.mkdir(); source.rename(target); item.path = os.fsencode(target); item.store()
    info = client.get(f'/api/library/albums/{album.id}/location').json
    assert 'separate album folders' in info['manual_error']


def test_policy_only_change_is_bound_to_review_and_can_be_saved(case):
    app, library, album, paths, client = case
    plan, saved = edit(case, location={'mode': 'manual', 'directory': 'Artist/Album'})
    assert saved.status_code == 200 and not plan['paths']
    assert plan['location_change'] == {'from': 'Beets naming', 'to': 'Artist/Album'}
    assert library.get_album(album.id).get('cratekeep_directory') == 'Artist/Album'


def test_manual_collision_and_changed_policy_are_rejected_before_moves(case):
    app, library, album, paths, client = case
    root = Path(app.config['LIBRARY_PATH']); target = root/'Occupied'/paths[0].name
    target.parent.mkdir(); target.write_bytes(b'Existing file')
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    opened = client.get(f'/api/library/albums/{album.id}').json
    payload = {'expected_fingerprint': opened['fingerprint'], 'location': {'mode': 'manual', 'directory': 'Occupied'}}
    collision = client.post(endpoint+'preview', json=payload)
    assert collision.status_code == 409 and collision.json['code'] == 'destination_collision'
    assert target.read_bytes() == b'Existing file' and all(path.exists() for path in paths)
    payload['location']['directory'] = 'Unoccupied'
    plan = client.post(endpoint+'preview', json=payload).json
    album = library.get_album(album.id); album['cratekeep_directory'] = 'Artist/Album'; album.store()
    stale = client.post(endpoint+'apply', json={**payload, 'plan_fingerprint': plan['plan_fingerprint']})
    assert stale.status_code == 409 and stale.json['code'] == 'stale_fingerprint'
    assert all(path.exists() for path in paths)
