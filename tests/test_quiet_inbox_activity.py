"""Routine scans retain one status; arrivals and errors retain real run evidence."""
import json
from beets_mvp.activity import Activity
from test_app import make_app, tagged_inbox_wav
from test_activity import submit, wait


def finish_scan(service, *, added=0, scope='Manual', status='complete'):
    job = service.new_external('Scanning Inbox', scope, '/inbox?source=' + scope)
    service.finish(job['id'], status, result={'added': added, 'files': 30}, files_found=30)
    return job


def test_routine_background_scans_replace_history_in_storage_and_after_restart(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    for index in range(150):
        finish_scan(service, scope=('Manual', 'slskd', 'Lidarr')[index % 3])
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM activity_jobs').fetchone()[0] == 1
    groups = Activity(app).history.page()['groups']
    assert len(groups) == 1 and groups[0]['key'] == 'inbox-scans'
    assert groups[0]['count'] == 1 and groups[0]['latest']['is_inbox_status']
    assert groups[0]['latest']['stage'] == 'Success'
    assert 'files_found' not in groups[0]['latest']
    service.history.clear({})
    assert service.history.page()['groups'] == []
    finish_scan(service)
    assert service.history.page()['groups'][0]['count'] == 1


def test_arrivals_and_failures_are_standalone_and_survive_routine_checks(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    arrival = finish_scan(service, added=3, scope='Lidarr')
    failed = finish_scan(service, status='failed', scope='slskd')
    another = finish_scan(service, added=2)
    for _ in range(30):
        finish_scan(service)
    groups = service.history.page()['groups']
    assert len(groups) == 4
    arrivals = [row for row in groups if row['label'] == 'Inbox arrivals']
    assert sorted(row['latest']['scan_added'] for row in arrivals) == [2, 3]
    assert all(row['count'] == 1 for row in arrivals)
    assert next(row for row in groups if row['latest']['id'] == failed['id'])['attention'] == 1
    assert service.get(arrival['id'])['result']['added'] == 3
    assert service.get(another['id'])['result']['added'] == 2


def test_committed_new_tracks_are_reported_once_and_manual_receipts_stay_replayable(tmp_path):
    app = make_app(tmp_path); client = app.test_client(); service = app.extensions['activity']
    inbox = app.config['INBOXES'][0]
    tagged_inbox_wav(tmp_path / 'inbox' / 'Album' / 'One.wav')
    steps = [{'path': f"/api/inbox/{inbox['id']}/queue/refresh", 'body': {}}]
    first = submit(client, steps, request_id='first-inbox-refresh')
    discovered = wait(client, first['id'])
    assert discovered['scan_added'] == 1
    assert discovered['result']['inventory']['added'] == 1
    second = submit(client, steps, request_id='second-inbox-refresh')
    unchanged = wait(client, second['id'])
    assert unchanged['scan_added'] == 0
    assert unchanged['result']['inventory']['files'] == 1
    groups = service.history.page()['groups']
    assert len([row for row in groups if row['label'] == 'Inbox arrivals']) == 1
    assert next(row for row in groups if row['key'] == 'inbox-scans')['count'] == 1
    repeated = submit(client, steps, request_id='second-inbox-refresh')
    assert repeated['id'] == second['id'] and repeated['result'] == unchanged['result']
    assert service.get(first['id'])['result'] == discovered['result']


def test_upgrade_compacts_known_old_successes_preserves_unknown_and_errors(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    # Seed the previous release's saved snapshots without invoking new compaction.
    for index, (state, result, cleared) in enumerate([
        ('complete', {'added': 0}, 0), ('complete', {'inventory': {'added': 0}}, 0),
        ('complete', {'added': 2}, 0), ('failed', {}, 0), ('complete', {'files': 5}, 0),
        ('complete', {'added': 0}, 1),
    ]):
        job = dict(id=f'old-{index}', label='Scanning Inbox', scope='Manual', href='/inbox', status=state, rows=[], created_at=index+1, finished_at=index+2, result=result)
        with service.db() as db:
            db.execute('INSERT INTO activity_jobs(id,request_id,work_key,snapshot,history_group,history_status,history_cleared) VALUES (?,?,?,?,?,?,?)', (job['id'], job['id'], '', json.dumps(job), 'inbox-scans', state, cleared))
    restarted = Activity(app)
    with restarted.db() as db:
        assert db.execute('SELECT COUNT(*) FROM activity_jobs').fetchone()[0] == 4
    groups = restarted.history.page()['groups']
    assert len(groups) == 4
    assert any(row['label'] == 'Inbox arrivals' for row in groups)
    assert restarted.get('old-3')['status'] == 'failed'
    assert restarted.get('old-4')['result'] == {'files': 5}
    assert restarted.get('inbox-scan-status')['finished_at'] == 4
    restarted.history.clear({})
    assert Activity(app).history.page()['groups'] == []


def test_latest_success_timestamp_does_not_move_backwards(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    finish_scan(service)
    newest = service.get('inbox-scan-status')['finished_at']
    job = service.new_external('Scanning Inbox', 'Lidarr', '/inbox')
    older = {**job, 'status': 'complete', 'result': {'added': 0}, 'finished_at': newest - 100}
    with service.db() as db:
        service.history.compact_scan(db, older)
    assert service.get('inbox-scan-status')['finished_at'] == newest


def test_restart_keeps_interrupted_scan_separate_from_success_status(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    finish_scan(service)
    unfinished = service.new_external('Scanning Inbox', 'Lidarr', '/inbox?source=Lidarr')
    service.checkpoint(unfinished['id'])
    restarted = Activity(app)
    groups = restarted.history.page()['groups']
    assert len(groups) == 2
    interrupted = next(row for row in groups if row['latest']['id'] == unfinished['id'])
    assert interrupted['attention'] == 1 and interrupted['latest']['status'] == 'interrupted'
    assert interrupted['key'] != 'inbox-scans'
    assert next(row for row in groups if row['key'] == 'inbox-scans')['latest']['stage'] == 'Success'


def test_history_read_keeps_its_snapshot_during_automatic_compaction(tmp_path, monkeypatch):
    import sqlite3

    app = make_app(tmp_path)
    service = app.extensions['activity']
    finish_scan(service)
    old_timestamp = service.history.page()['groups'][0]['timestamp']
    compacted = False

    class InterleavedRead(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            nonlocal compacted
            if not compacted and sql.startswith('SELECT snapshot FROM activity_jobs WHERE rowid='):
                # The history query already picked a rowid. Commit a replacement
                # on another connection before resolving that exact rowid.
                compacted = True
                finish_scan(service)
            return super().execute(sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(service, 'db', lambda: sqlite3.connect(app.config['APP_DB'], factory=InterleavedRead))
        page = service.history.page()

    assert compacted
    assert page['groups'][0]['timestamp'] == old_timestamp
    assert service.history.page()['groups'][0]['timestamp'] > old_timestamp
    with service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM activity_jobs').fetchone()[0] == 1
