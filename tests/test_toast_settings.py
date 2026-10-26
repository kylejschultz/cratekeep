from pathlib import Path
import pytest
from test_app import make_app, ui_bootstrap
from beets_mvp import create_app


def settings_values(app):
    inbox = app.config['INBOXES'][0]
    return {'inbox_id':[inbox['id']], 'inbox_name':[inbox['name']], 'inbox_path':[inbox['path']],
            'library_path': app.config['LIBRARY_PATH'], 'beets_config':Path(app.config['BEETS_CONFIG']).read_text()}


def test_toast_duration_default_persists_and_reloads_on_every_page(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    assert ui_bootstrap(client.get('/'))['toastDurationSeconds'] == 5
    values = settings_values(app)
    assert client.post('/settings',data={**values,'toast_duration_seconds':'23'}).status_code == 302
    for route in ('/','/inbox','/library-import','/library','/settings'):
        assert ui_bootstrap(client.get(route))['toastDurationSeconds'] == 23
    assert client.post('/settings',data=values).status_code == 302
    restarted = create_app({'TESTING':True,'STATE_PATH':str(tmp_path/'config')})
    assert ui_bootstrap(restarted.test_client().get('/'))['toastDurationSeconds'] == 23


@pytest.mark.parametrize('duration',['0','301','1.5','nope'])
def test_invalid_toast_duration_does_not_change_settings(tmp_path,duration):
    app = make_app(tmp_path); client = app.test_client()
    result = client.post('/settings',data={**settings_values(app),'toast_duration_seconds':duration})
    assert result.status_code == 200
    assert b'Toast duration must be a whole number' in result.data
    assert ui_bootstrap(client.get('/'))['toastDurationSeconds'] == 5


def test_settings_without_navidrome_controls_preserves_legacy_config(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    values = settings_values(app)
    assert client.post('/settings', data={**values, 'navidrome_rescan_url': 'https://player.example.test/scan',
                                         'navidrome_token': 'fixture-only-token'}).status_code == 302
    assert client.post('/settings', data=values).status_code == 302
    assert app.config['NAVIDROME_RESCAN_URL'] == 'https://player.example.test/scan'
    assert app.config['NAVIDROME_TOKEN'] == 'fixture-only-token'
