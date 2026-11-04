"""History is unbounded, grouped and clearable without removing execution evidence."""
from beets_mvp.activity import Activity
from test_app import make_app
from test_activity import submit, wait


def scan(service, name='Manual', state='complete'):
    job = service.new_external('Scanning Inbox', name, '/inbox?source=' + name)
    if state:
        service.finish(job['id'], state, result={'files': 3}, files_found=3)
    return job


def test_failed_scans_stay_separate_from_active_scan_status(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    failed = scan(service, 'Manual', 'failed'); scan(service, 'slskd'); scan(service, 'Lidarr', None)
    for scope in ('Library', 'Library', 'Manual'):
        job = service.new_external('Matching albums', scope, '/library-import' if scope == 'Library' else '/inbox')
        service.finish(job['id'], 'complete')
    groups = app.test_client().get('/api/activity/history').json['groups']
    active = next(row for row in groups if row['key'] == 'inbox-scans')
    assert active['count'] == active['active'] == 1
    assert active['latest']['scope'] == 'Lidarr'
    failure = next(row for row in groups if row['latest']['id'] == failed['id'])
    assert failure['attention'] == 1 and failure['active'] == 0
    assert any(row['label'] == 'Matching albums' and row['count'] == 2 for row in groups)


def test_more_than_100_runs_survive_restart_with_stable_bounded_pagination(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    def repeat():
        job = service.new_external('Matching albums', 'Library', '/library-import')
        service.finish(job['id'], 'complete')
        return job
    first_repeat = repeat()
    from beets_mvp.activity_history import group_key
    repeat_group = group_key(service.get(first_repeat['id']))
    for index in range(126):
        repeat()
    for index in range(25):
        job = service.new_external('Operation ' + str(index), 'Library', '/library')
        service.finish(job['id'], 'complete')
    restarted = Activity(app)
    first = restarted.history.page()
    assert len(first['groups']) == 20 and first['next_before']
    # New runs belong to the latest page, not to this ongoing historical snapshot.
    new = restarted.new_external('Matching albums', 'Library', '/library-import')
    restarted.finish(new['id'], 'complete')
    second = restarted.history.page(before=first['next_before'], snapshot=first['snapshot'])
    assert len(second['groups']) == 6
    assert not {g['key'] for g in first['groups']} & {g['key'] for g in second['groups']}
    assert next(g['count'] for g in second['groups'] if g['key'] == repeat_group) == 127
    runs, cursor = [], None
    while True:
        page = restarted.history.page(group=repeat_group, before=cursor, snapshot=first['snapshot'])
        assert len(page['jobs']) <= 20
        runs.extend(job['id'] for job in page['jobs'])
        cursor = page['next_before']
        if cursor is None:
            break
    assert len(runs) == len(set(runs)) == 127
    assert restarted.history.page()['clearable'] == 153


def test_clear_all_is_global_persistent_and_preserves_active_and_later_completions(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']; client = app.test_client()
    old = scan(service)
    for index in range(26):
        job = service.new_external('Other ' + str(index), 'Library', '/library')
        service.finish(job['id'], 'complete')
    running = scan(service, 'Lidarr', None)
    assert client.post('/api/activity/history/clear', json={}).json == {'cleared': 27}
    active = client.get('/api/activity/history').json
    assert active['clearable'] == 0 and active['groups'][0]['count'] == 1
    assert service.get(running['id'])['status'] == 'running'
    service.finish(running['id'], 'complete', result={'imported': [{'job_id': 42}]})
    assert client.get('/api/activity/history').json['clearable'] == 1
    assert service.get(old['id'])['result'] == {'files': 3}
    assert service.get(running['id'])['result']['imported'][0]['job_id'] == 42
    assert Activity(app).history.page()['groups'][0]['count'] == 1
    # Another browser sees the same cleared state; individual clear preserves receipts.
    assert app.test_client().post('/api/activity/history/clear', json={'ids': [running['id']]}).json == {'cleared': 1}
    assert client.get('/api/activity/history').json['groups'] == []
    assert service.get(running['id'])['result']['imported'][0]['job_id'] == 42


def test_group_clear_and_old_dismissal_migration_do_not_remove_other_groups(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']; client = app.test_client()
    one = service.new_external('Repeat fixture', 'Library', '/library')
    service.finish(one['id'], 'complete')
    two = service.new_external('Repeat fixture', 'Library', '/library')
    service.finish(two['id'], 'complete')
    from beets_mvp.activity_history import group_key
    key = group_key(service.get(one['id']))
    other = service.new_external('Matching albums', 'Library', '/library-import')
    service.finish(other['id'], 'failed')
    assert client.post('/api/activity/history/clear', json={'ids': [one['id']]}).json == {'cleared': 1}
    assert client.post('/api/activity/history/clear', json={'group': key}).json == {'cleared': 1}
    assert [row['label'] for row in client.get('/api/activity/history').json['groups']] == ['Matching albums']
    for body in (None, {'ids': []}, {'ids': 'all'}, {'group': 123}, {'surprise': True}, {'group': 'x', 'ids': ['y']}):
        assert client.post('/api/activity/history/clear', json=body).status_code == 400
    assert client.get('/api/activity/history?before=nope').status_code == 400
    assert client.get('/api/activity/history?snapshot=-1').status_code == 400


def test_duplicate_submission_still_returns_saved_result_after_clear(tmp_path):
    app = make_app(tmp_path); client = app.test_client(); calls = []
    def operation():
        calls.append(1)
        return {'files': 12}
    app.view_functions['refresh_library_album_queue'] = operation
    steps = [{'path': '/api/library-import/queue/refresh', 'body': {}}]
    job = submit(client, steps)
    assert wait(client, job['id'])['result'] == {'files': 12}
    client.post('/api/activity/history/clear', json={})
    repeated = submit(client, steps)
    assert repeated['id'] == job['id'] and repeated['result'] == {'files': 12}
    assert calls == [1]
    assert client.get('/api/activity/history').json['groups'] == []


def test_polling_and_history_summaries_omit_execution_payloads(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    job = scan(service)
    for item in (service.list()[0], service.history.page()['groups'][0]['latest'], service.history.page(group='inbox-scan-' + job['id'])['jobs'][0]):
        assert item['rows'] == [] and 'result' not in item and 'steps' not in item
    assert service.get(job['id'])['result'] == {'files': 3}


def test_old_schema_migrates_and_unfinished_runs_remain_interrupted(tmp_path):
    from flask import Flask
    import json
    import sqlite3
    app = Flask(__name__); app.config['APP_DB'] = str(tmp_path / 'old.db')
    job = dict(id='old', label='Scanning Inbox', scope='Manual', href='/inbox', status='running', created_at=1, rows=[])
    with sqlite3.connect(app.config['APP_DB']) as db:
        db.execute('CREATE TABLE activity_jobs(id TEXT PRIMARY KEY,request_id TEXT UNIQUE,work_key TEXT,snapshot TEXT)')
        db.execute('INSERT INTO activity_jobs VALUES (?,?,?,?)', ('old', 'old-request', '', json.dumps(job)))
    service = Activity(app)
    group = service.history.page()['groups'][0]
    assert group['attention'] == 1 and group['active'] == 0
    assert service.get('old')['status'] == 'interrupted'


def test_group_identity_distinguishes_actions_but_not_album_ids_or_renamed_inbox(tmp_path):
    from beets_mvp.activity_history import group_key
    base = dict(label='Updating library files', scope='Library', href='/library')
    metadata = {**base, 'steps': [{'path': '/api/library/albums/1/metadata/apply'}]}
    another_album = {**base, 'steps': [{'path': '/api/library/albums/2/metadata/apply'}]}
    artwork = {**base, 'steps': [{'path': '/api/library/albums/1/artwork/apply'}]}
    assert group_key(metadata) == group_key(another_album) != group_key(artwork)
    assert group_key({**metadata, 'scope': 'Renamed'}) == group_key(metadata)


def test_clear_returns_during_running_mutation_and_later_result_stays_visible(tmp_path):
    import threading
    app = make_app(tmp_path); service = app.extensions['activity']; client = app.test_client()
    started, release = threading.Event(), threading.Event()
    def operation():
        started.set()
        assert release.wait(5)
        return {'files': 4}
    app.view_functions['refresh_library_album_queue'] = operation
    scan(service)
    job = submit(client, [{'path': '/api/library-import/queue/refresh', 'body': {}}])
    assert started.wait(2)
    try:
        assert client.post('/api/activity/history/clear', json={}).json == {'cleared': 1}
        assert service.get(job['id'])['status'] == 'running'
    finally:
        release.set()
    assert wait(client, job['id'])['result'] == {'files': 4}
    page = client.get('/api/activity/history').json
    assert page['clearable'] == 1 and page['groups'][0]['latest']['id'] == job['id']
