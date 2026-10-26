from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import uuid

from beets_mvp import create_app
from beets_mvp.analytics import Beacon, library_counts
from test_app import make_app, ui_bootstrap
from test_toast_settings import settings_values

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def setting(app, key):
    with sqlite3.connect(app.config['APP_DB']) as db:
        row = db.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def test_consent_defaults_off_persists_and_stops_transmission(tmp_path):
    app = make_app(tmp_path); sent = []
    app.config['BEACON_TRANSPORT'] = sent.append
    beacon = app.extensions['beacon']; client = app.test_client()
    assert not beacon.run_once(NOW)
    assert not sent and setting(app, 'analytics_install_id') is None
    values = settings_values(app)
    assert client.post('/settings', data={**values, 'analytics_enabled': 'on', 'settings_tab': 'about'}).location.endswith('/settings#about')
    assert beacon.run_once(NOW)
    install_id = setting(app, 'analytics_install_id'); assert str(uuid.UUID(install_id)) == install_id
    restarted = create_app({'TESTING': True, 'STATE_PATH': app.config['STATE_PATH'], 'BEACON_TRANSPORT': sent.append})
    assert not restarted.extensions['beacon'].run_once(NOW)
    assert restarted.extensions['beacon'].run_once(NOW + timedelta(days=1))
    assert sent[0]['install_id'] == sent[1]['install_id'] == install_id
    assert client.post('/settings', data=values).status_code == 302
    assert not beacon.run_once(NOW + timedelta(days=2))
    boot = ui_bootstrap(client.get('/settings'))
    assert boot['data']['settings']['analytics_enabled'] == ''
    assert 'analytics_install_id' not in boot['data']['settings']
    assert 'analytics_last_attempt' not in boot['data']['settings']


def test_payload_is_allowlisted_aggregates_only_and_missing_library_is_zero(tmp_path):
    app = make_app(tmp_path); sent = []
    app.config.update(BEACON_TRANSPORT=sent.append, BUILD_SHA='a'*40)
    client = app.test_client(); client.post('/settings', data={**settings_values(app), 'analytics_enabled':'on'})
    path = Path(app.config['BEETS_DB']); path.unlink(missing_ok=True)
    assert library_counts(path) == {'artist_count':0, 'album_count':0, 'song_count':0}
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE albums(id INTEGER, albumartist TEXT);
            CREATE TABLE items(id INTEGER, album_id INTEGER, artist TEXT, title TEXT, path TEXT);
            INSERT INTO albums VALUES (1,'Private Artist'),(2,'private artist'),(3,'');
            INSERT INTO items VALUES (1,1,'Private Artist','Secret Song','/private/music'),
                (2,2,'Private Artist','Song Two','/secret/path'),(3,3,'Other Artist','Three','/hidden');''')
    assert app.extensions['beacon'].run_once(NOW)
    payload = sent[0]
    assert set(payload) == {'project','install_id','version','arch','os','timestamp','dev','artist_count','album_count','song_count'}
    assert payload['project'] == 'cratekeep' and payload['version'] == 'a'*40
    assert payload['artist_count'] == 2 and payload['album_count'] == 3 and payload['song_count'] == 3
    assert not any(value in str(payload) for value in ('Private Artist','Secret Song','/private','/secret','/hidden'))


def test_concurrent_workers_and_failed_attempts_are_bounded_across_restarts(tmp_path):
    app = make_app(tmp_path); sent = []
    def failing(payload):
        sent.append(payload)
        raise OSError('private transport failure')
    app.config['BEACON_TRANSPORT'] = failing
    app.test_client().post('/settings', data={**settings_values(app), 'analytics_enabled':'on'})
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert not any(pool.map(lambda _: Beacon(app).run_once(NOW), range(5)))
    assert len(sent) == 1
    assert not Beacon(app).run_once(NOW)
    assert not Beacon(app).run_once(NOW + timedelta(days=1))
    assert len(sent) == 2


def test_worker_starts_only_outside_tests_and_settings_wakes_it(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(Beacon, 'start', lambda self: calls.append(self))
    app = make_app(tmp_path)
    assert not calls
    restarted = create_app({'STATE_PATH': app.config['STATE_PATH']})
    assert calls == [restarted.extensions['beacon']]
    app.test_client().post('/settings', data=settings_values(app))
    assert app.extensions['beacon'].wake.is_set()


def test_transport_has_short_timeout_fixed_endpoint_and_no_redirect_handler(monkeypatch):
    from beets_mvp import analytics
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self, size): assert size == 1024
    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response()
    def opener(handler):
        assert handler().redirect_request(None, None, 302, 'redirect', {}, 'https://other.test') is None
        return Opener()
    monkeypatch.setattr(analytics.urllib.request, 'build_opener', opener)
    analytics.send_ping({'project':'cratekeep'})
    request, timeout = calls[0]
    assert request.full_url == 'https://beacon.kjschultz.com/ping'
    assert request.method == 'POST' and timeout == 3
    assert request.data == b'{"project": "cratekeep"}'
