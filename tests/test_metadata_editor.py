"""Combined metadata drafts use the existing tag/move rollback boundary."""
from pathlib import Path
import pytest
from mediafile import MediaFile
from test_app import make_app, managed_wav_album

@pytest.fixture
def case(tmp_path):
    app = make_app(tmp_path)
    library, album, paths = managed_wav_album(app, tmp_path, titles=('First', 'Second'))
    client = app.test_client()
    opened = client.get(f'/api/library/albums/{album.id}').json
    draft = {'expected_fingerprint': opened['fingerprint'], 'album': {'albumartist': 'Renamed Artist', 'album': 'Renamed Album'},
             'tracks': [{'id': next(iter(album.items())).id, 'title': 'Renamed Track', 'artist': 'Guest'}]}
    return app, library, album, paths, client, draft


def test_preview_is_read_only_and_matches_combined_save(case):
    app, library, album, paths, client, draft = case
    before = [path.read_bytes() for path in paths]
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    result = client.post(endpoint + 'preview', json=draft)
    assert result.status_code == 200, result.json
    plan = result.json
    assert len(plan['paths']) == 2 and len(plan['changes']) == 2
    assert library.get_album(album.id).album != 'Renamed Album'
    assert [path.read_bytes() for path in paths] == before
    assert all('Renamed Artist/Renamed Album/' in row['to'] for row in plan['paths'])
    saved = client.post(endpoint + 'apply', json={**draft, 'plan_fingerprint': plan['plan_fingerprint']})
    assert saved.status_code == 200, saved.json
    tracks = saved.json['album']['tracks']
    assert {row['relative_path'] for row in tracks} == {row['to'] for row in plan['paths']}
    assert tracks[0]['title'] == 'Renamed Track' and tracks[0]['artist'] == 'Guest'
    assert all(MediaFile(item.path).album == 'Renamed Album' for item in library.get_album(album.id).items())
    assert not any(path.exists() for path in paths)


def test_apply_rejects_modified_draft_and_stale_album(case):
    app, library, album, paths, client, draft = case
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    plan = client.post(endpoint + 'preview', json=draft).json
    changed = {**draft, 'album': {'album': 'Different'}, 'plan_fingerprint': plan['plan_fingerprint']}
    assert client.post(endpoint + 'apply', json=changed).json['code'] == 'metadata_preview_stale'
    item = next(iter(album.items())); item.title = 'Concurrent edit'; item.store()
    result = client.post(endpoint + 'apply', json={**draft, 'plan_fingerprint': plan['plan_fingerprint']})
    assert result.status_code == 409 and result.json['code'] == 'stale_fingerprint'
    assert all(path.exists() for path in paths)


def test_combined_failure_restores_all_album_track_tags_and_paths(case, monkeypatch):
    from beets.library import Item
    app, library, album, paths, client, draft = case
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    plan = client.post(endpoint + 'preview', json=draft).json
    original = Item.write; calls = []
    def fail_second(item, *args, **kwargs):
        calls.append(item.id)
        if len(calls) == 2:
            raise OSError('fixture write failed')
        return original(item, *args, **kwargs)
    monkeypatch.setattr(Item, 'write', fail_second)
    result = client.post(endpoint + 'apply', json={**draft, 'plan_fingerprint': plan['plan_fingerprint']})
    assert result.status_code == 500 and result.json['code'] == 'mutation_rolled_back'
    assert library.get_album(album.id).album != 'Renamed Album'
    assert all(path.exists() for path in paths)
    assert MediaFile(paths[0]).title == 'First'


def test_preview_reports_sidecars_and_checks_changed_companion(case):
    app, library, album, paths, client, draft = case
    sidecar = paths[0].with_suffix('.lrc'); sidecar.write_text('[00:01.00]Fixture')
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    plan = client.post(endpoint + 'preview', json=draft).json
    assert plan['related_files'][0]['kind'] == 'Lyrics sidecar'
    sidecar.write_text('[00:02.00]Different fixture')
    result = client.post(endpoint + 'apply', json={**draft, 'plan_fingerprint': plan['plan_fingerprint']})
    assert result.status_code == 409 and result.json['code'] == 'metadata_preview_stale'
    assert all(path.exists() for path in paths)


def test_preview_collision_and_foreign_track_rejected_before_writes(case):
    app, library, album, paths, client, draft = case
    endpoint = f'/api/library/albums/{album.id}/metadata/'
    bad = {**draft, 'tracks': [{'id': 999999, 'title': 'Invalid'}]}
    assert client.post(endpoint + 'preview', json=bad).status_code == 400
    plan = client.post(endpoint + 'preview', json=draft).json
    target = Path(app.config['LIBRARY_PATH']) / plan['paths'][0]['to']
    target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(b'occupied')
    result = client.post(endpoint + 'apply', json={**draft, 'plan_fingerprint': plan['plan_fingerprint']})
    assert result.status_code == 409 and result.json['code'] == 'destination_collision'
    assert target.read_bytes() == b'occupied' and all(path.exists() for path in paths)
