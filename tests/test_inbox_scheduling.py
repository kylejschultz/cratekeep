"""Discovery is independent per source, settles downloads and never imports."""
import json
import threading

import pytest
from werkzeug.datastructures import MultiDict

from beets_mvp import _connect, _load_settings, create_app
from beets_mvp.inbox_discovery import InboxDiscovery, scan_completed
from test_app import configure_named_inboxes, make_app, tagged_inbox_wav


class Clock:
    value = 0
    def __call__(self):
        return self.value
    def advance(self, seconds=3):
        self.value += seconds


class Observer:
    def __init__(self):
        self.running = False
        self.emitters = []
    def schedule(self, handler, path, recursive):
        self.handler = handler
        self.path = path
        assert recursive
    def start(self):
        self.running = True
    def is_alive(self):
        return self.running
    def stop(self):
        self.running = False
    def join(self, timeout):
        pass


def setup(tmp_path, modes=("5", "15", "manual")):
    app = make_app(tmp_path)
    roots = [tmp_path / f"source-{i}" for i in range(len(modes))]
    for root in roots:
        root.mkdir()
    configure_named_inboxes(app, roots)
    rows = app.config["INBOXES"]
    response = app.test_client().post('/settings', data=MultiDict(
        [(key, row[key.removeprefix('inbox_')]) for row in rows for key in ('inbox_id', 'inbox_name', 'inbox_path')]
        + [('inbox_discovery', mode) for mode in modes]
        + [('library_path', app.config['LIBRARY_PATH'])]))
    assert response.status_code == 302
    clock = Clock()
    service = InboxDiscovery(app, clock=clock, observer_factory=Observer)
    app.extensions['inbox_discovery'] = service
    app.config['MUSICBRAINZ_PROVIDER'] = lambda *args, **kwargs: pytest.fail('unexpected matching')
    return app, service, clock, roots


def settle(service, clock):
    service.step()
    clock.advance()
    service.step()


def count(app, inbox):
    return app.test_client().get(f"/api/inbox/{inbox['id']}/queue").json['total']


def test_independent_schedules_manual_mode_and_post_commit_event(tmp_path):
    app, service, clock, roots = setup(tmp_path)
    inboxes = app.config['INBOXES']
    events = []
    def completed(sender, **event):
        # A subscriber can already read the committed inventory/reviews.
        assert count(sender, next(row for row in inboxes if row['id'] == event['inbox_id'])) >= 1
        events.append(event)
    scan_completed.connect(completed, sender=app)
    for index, root in enumerate(roots):
        tagged_inbox_wav(root / 'First' / 'Song.wav', album=f'First {index}')
    original = (roots[0] / 'First/Song.wav').read_bytes()
    settle(service, clock)
    assert [count(app, inbox) for inbox in inboxes] == [1, 1, 0]
    assert [event['added'] for event in events] == [1, 1]
    assert all(event['trigger'] == 'background' and event['scan_id'] for event in events)
    assert all(len(event['added_inventory_ids']) == 1 and not event['added_ids_truncated'] for event in events)
    with _connect(app.config['APP_DB']) as db:
        saved = json.loads(db.execute('SELECT summary_json FROM adoption_jobs WHERE id=?', (events[0]['scan_id'],)).fetchone()[0])
        assert saved['added_inventory_ids'] == events[0]['added_inventory_ids']
    for index, root in enumerate(roots):
        tagged_inbox_wav(root / 'Next' / 'Song.wav', album=f'Next {index}')
    clock.advance(300)
    settle(service, clock)
    assert [count(app, inbox) for inbox in inboxes] == [2, 1, 0]
    clock.advance(600)
    settle(service, clock)
    assert [count(app, inbox) for inbox in inboxes] == [2, 2, 0]
    assert (roots[0] / 'First/Song.wav').read_bytes() == original
    assert not list((tmp_path / 'library').iterdir())
    assert not app.config['INBOX_MATCH_RUNNING']
    manual = inboxes[2]
    assert app.test_client().post(f"/api/inbox/{manual['id']}/queue/refresh", json={}).status_code == 200
    assert count(app, manual) == 2
    scan_completed.disconnect(completed, sender=app)


def test_instant_coalesces_events_settles_writes_and_reconciles_missed_events(tmp_path):
    from watchdog.events import FileCreatedEvent, FileModifiedEvent, DirMovedEvent, FileOpenedEvent
    app, service, clock, roots = setup(tmp_path, ('instant', 'manual'))
    settle(service, clock)
    inbox = app.config['INBOXES'][0]
    state = service.states[inbox['id']]
    handler = state['observer'].handler
    initial = service.status(inbox)['revision']
    target = roots[0] / 'New/Sub/Track.wav'
    tagged_inbox_wav(target)
    handler.on_any_event(DirMovedEvent(str(roots[0] / 'tmp'), str(target.parent)))
    for _ in range(20):
        handler.on_any_event(FileCreatedEvent(str(target)))
    service.step()
    assert count(app, inbox) == 0
    clock.advance()
    service.step()  # First stat sample.
    tagged_inbox_wav(target, album='Changed while downloading')
    handler.on_any_event(FileModifiedEvent(str(target)))
    clock.advance()
    service.step()  # A changed snapshot must settle again.
    assert count(app, inbox) == 0
    clock.advance()
    service.step()
    assert count(app, inbox) == 1
    assert initial != service.status(inbox)['revision']
    with _connect(app.config['APP_DB']) as db:
        completed = db.execute("SELECT count(*) FROM adoption_jobs WHERE kind='inbox_inventory' AND status='complete'").fetchone()[0]
    assert completed == 2  # startup plus one coalesced arrival
    handler.on_any_event(FileOpenedEvent(str(target)))
    assert state['event_due'] is None
    tagged_inbox_wav(roots[0] / 'Missed/Song.wav', album='Missed event')
    clock.advance(300)
    settle(service, clock)
    assert count(app, inbox) == 2
    service.stop()
    assert "observer" not in state


def test_unavailable_source_preserves_inventory_and_recovers_without_starvation(tmp_path):
    app, service, clock, roots = setup(tmp_path, ('5', '5'))
    for root in roots:
        tagged_inbox_wav(root / 'Album/Song.wav')
    settle(service, clock)
    first, second = app.config['INBOXES']
    roots[0].rename(tmp_path / 'temporarily-offline')
    tagged_inbox_wav(roots[1] / 'Another/Song.wav', album='Another')
    clock.advance(300)
    settle(service, clock)
    client = app.test_client()
    assert client.get(f"/api/inbox/{first['id']}/discovery").json['error']
    assert count(app, first) == 1 and count(app, second) == 2
    assert not roots[0].exists()
    # Restart must not silently manufacture an empty Inbox.
    restarted = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'STATE_PATH': app.config['STATE_PATH']})
    assert not roots[0].exists()
    assert count(restarted, first) == 1
    (tmp_path / 'temporarily-offline').rename(roots[0])
    clock.advance(300)
    settle(service, clock)
    assert not client.get(f"/api/inbox/{first['id']}/discovery").json['error']


@pytest.mark.parametrize('lock_name', ['MUTATION_LOCK', 'INBOX_EXECUTION_LOCK', 'INBOX_CANDIDATE_LOCK'])
def test_busy_scan_defers_without_starting_work(tmp_path, lock_name):
    app, service, clock, roots = setup(tmp_path, ('5',))
    tagged_inbox_wav(roots[0] / 'Album/Song.wav')
    acquired, release = threading.Event(), threading.Event()
    def hold():
        with app.config[lock_name]:
            acquired.set()
            release.wait(5)
    worker = threading.Thread(target=hold)
    worker.start()
    assert acquired.wait(2)
    try:
        settle(service, clock)
        assert count(app, app.config['INBOXES'][0]) == 0
    finally:
        release.set()
        worker.join(2)
    settle(service, clock)
    assert count(app, app.config['INBOXES'][0]) == 1


def test_modes_persist_follow_ids_and_invalid_input_is_atomic(tmp_path):
    app, service, clock, roots = setup(tmp_path, ('instant', 'manual'))
    current = app.config['INBOXES']
    client = app.test_client()
    def submit(modes=(), rows=None):
        return client.post('/settings', data=MultiDict(
            [(key, row[key.removeprefix('inbox_')]) for row in (rows or current) for key in ('inbox_id', 'inbox_name', 'inbox_path')]
            + [('inbox_discovery', mode) for mode in modes]
            + [('library_path', app.config['LIBRARY_PATH'])]))
    assert submit(('7', 'manual')).status_code == 200
    assert [row['discovery'] for row in app.config['INBOXES']] == ['instant', 'manual']
    assert submit(rows=[{**current[1], 'name': 'Renamed'}, current[0]]).status_code == 302
    assert [row['discovery'] for row in app.config['INBOXES']] == ['manual', 'instant']
    reloaded = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'STATE_PATH': app.config['STATE_PATH']})
    assert [(row['id'], row['discovery']) for row in reloaded.config['INBOXES']] == [(current[1]['id'], 'manual'), (current[0]['id'], 'instant')]
    assert service.thread is None


def test_legacy_modes_migrate_to_five_minutes_without_identity_changes(tmp_path):
    app = make_app(tmp_path)
    before = app.config['INBOXES'][0]
    with _connect(app.config['APP_DB']) as db:
        db.execute("UPDATE app_settings SET value=? WHERE key='inboxes_json'",
                   (json.dumps([{key: value for key, value in before.items() if key != 'discovery'}]),))
    settings = _load_settings(app.config['APP_DB'])
    assert json.loads(settings['inboxes_json']) == [{**before, 'discovery': '5'}]
    with _connect(app.config['APP_DB']) as db:
        assert json.loads(db.execute("SELECT value FROM app_settings WHERE key='inboxes_json'").fetchone()[0])[0]['discovery'] == '5'


def test_watcher_failure_falls_back_and_retries(tmp_path):
    class BrokenObserver(Observer):
        def start(self):
            raise OSError('watch limit')
    app, service, clock, roots = setup(tmp_path, ('instant',))
    service.observer_factory = BrokenObserver
    tagged_inbox_wav(roots[0] / 'Album/Song.wav')
    settle(service, clock)
    inbox = app.config['INBOXES'][0]
    assert 'five-minute' in service.status(inbox)['watcher_error']
    assert count(app, inbox) == 1
    service.observer_factory = Observer
    clock.advance(30)
    service.step()
    assert not service.status(inbox)['watcher_error']
    service.stop()


def test_matching_is_opt_in_for_both_modes_and_does_not_repeat_completed_work(tmp_path, monkeypatch):
    import beets_mvp
    app, service, clock, roots = setup(tmp_path, ('instant', '5', '5'))
    rows = app.config['INBOXES']
    rows[0]['auto_match'] = rows[1]['auto_match'] = True
    for index, root in enumerate(roots):
        tagged_inbox_wav(root / 'Album/Song.wav', album=f'Album {index}')
    scheduled = []
    monkeypatch.setattr(beets_mvp, '_start_inbox_auto_match', lambda app, ids: scheduled.extend(ids))
    settle(service, clock)
    assert len(scheduled) == 2
    with _connect(app.config['APP_DB']) as db:
        assert {row[0] for row in db.execute('SELECT source_id FROM album_reviews WHERE id IN (?,?)', scheduled)} == {rows[0]['id'], rows[1]['id']}
        db.execute("UPDATE album_reviews SET candidate_status='complete'")
    scheduled.clear()
    clock.advance(300)
    settle(service, clock)
    assert scheduled == []
    service.stop()


def test_mode_change_stops_watcher_and_does_not_reset_on_rename(tmp_path):
    app, service, clock, roots = setup(tmp_path, ('instant',))
    settle(service, clock)
    inbox = app.config['INBOXES'][0]
    state = service.states[inbox['id']]
    observer = state['observer']
    due = state['due']
    inbox['name'] = 'Renamed'
    service.step()
    assert state['due'] == due and observer.running
    inbox['discovery'] = 'manual'
    service.step()
    assert not observer.running and not service.states
    service.stop()


def test_root_replacement_restarts_observer_and_first_run_has_no_scan(tmp_path):
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'STATE_PATH': str(tmp_path / 'unconfigured')})
    service = app.extensions['inbox_discovery']
    service.step()
    assert not service.states
    app, service, clock, roots = setup(tmp_path, ('instant',))
    settle(service, clock)
    state = next(iter(service.states.values()))
    observer = state['observer']
    roots[0].rename(tmp_path / 'old-root')
    roots[0].mkdir()
    clock.advance(30)
    service.step()
    assert not observer.running and state['observer'] is not observer
    service.stop()


def test_start_is_idempotent_and_reloader_parent_does_not_own_a_loop(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    service = app.extensions['inbox_discovery']
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    monkeypatch.setenv('FLASK_DEBUG', '1')
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    service.start()
    assert service.thread is None
    monkeypatch.setenv('WERKZEUG_RUN_MAIN', 'true')
    service.start()
    thread = service.thread
    service.start()
    assert service.thread is thread
    service.stop()
    assert not thread.is_alive()
