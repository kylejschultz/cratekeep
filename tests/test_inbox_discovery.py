"""Initial Inbox discovery is a disk scan, not matching or source cleanup."""
from pathlib import Path

import pytest

from beets_mvp import create_app
from test_app import configure_named_inboxes, make_app, tagged_inbox_wav


def test_open_scan_discovers_audio_without_matching_or_removing_folders(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / 'inbox'
    configure_named_inboxes(app, [root])
    app.config['MUSICBRAINZ_PROVIDER'] = lambda *args, **kwargs: pytest.fail('opening Inbox matched online')
    audio = root / 'Artist' / 'Album' / 'Song.wav'
    tagged_inbox_wav(audio)
    original = audio.read_bytes()
    empty = root / 'Download in progress'
    empty.mkdir()
    client = app.test_client()
    base = f"/api/inbox/{app.config['INBOXES'][0]['id']}"
    assert client.get(base + '/queue').json['total'] == 0
    response = client.post(base + '/queue/refresh', json={})
    assert response.status_code == 200, response.json
    assert response.json['inventory']['files'] == 1
    assert client.get(base + '/queue').json['total'] == 1
    assert empty.is_dir()
    assert audio.read_bytes() == original
    assert response.json['cleanup']['removed'] == []


def test_open_scan_surfaces_unavailable_folder_and_respects_import_lock(tmp_path):
    app = make_app(tmp_path)
    root = tmp_path / 'inbox'
    configure_named_inboxes(app, [root])
    client = app.test_client()
    url = f"/api/inbox/{app.config['INBOXES'][0]['id']}/queue/refresh"
    with app.config['INBOX_EXECUTION_LOCK']:
        response = client.post(url, json={})
        assert response.status_code == 409
        assert response.json['code'] == 'inbox_busy'
    root.rmdir()
    response = client.post(url, json={})
    assert response.status_code == 409
    assert 'directory' in response.json['error'].lower()


def test_startup_permission_error_explains_container_user_and_config_folder(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    original_open = Path.open

    def inaccessible(path, *args, **kwargs):
        if path == state / 'secret.key':
            raise PermissionError('fixture AppData ownership mismatch')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', inaccessible)
    with pytest.raises(RuntimeError, match='container user') as error:
        create_app({'TESTING': True, 'STATE_PATH': str(state), 'SECRET_KEY': ''})
    assert str(state) in str(error.value)
    assert 'UID:GID' in str(error.value)
    assert '--user=99:100' in str(error.value)
