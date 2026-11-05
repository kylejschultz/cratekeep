"""History is unbounded, grouped and clearable without removing execution evidence."""
import pytest
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


def test_priority_paging_finds_old_running_and_failed_work_without_losing_history(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    running = service.new_external('Old active job', 'Library', '/library')
    failure = service.new_external('Old failure', 'Library', '/library')
    service.finish(failure['id'], 'failed')
    for index in range(45):
        job = service.new_external(f'Finished {index}', 'Library', '/library')
        service.finish(job['id'], 'complete')
    first = service.history.page()
    assert [group['latest']['id'] for group in first['groups'][:2]] == [running['id'], failure['id']]
    seen = [group['key'] for group in first['groups']]
    page = first
    while page['next_before'] is not None:
        page = service.history.page(before=page['next_before'], priority=page['next_priority'], snapshot=first['snapshot'])
        seen.extend(group['key'] for group in page['groups'])
    assert len(seen) == len(set(seen)) == 47
    assert app.test_client().get('/api/activity/history?priority=3').status_code == 400


def test_successful_checks_are_quiet_but_failed_checks_and_replay_receipts_survive(tmp_path):
    app = make_app(tmp_path); service = app.extensions['activity']
    def create(request_id, check):
        return service.create('Updating library files', 'Lidarr', '/inbox?source=lidarr', request_id, '', [],
                              steps=[{'path': '/api/inbox/albums/1/execute', 'body': {'dry_run': check}}], mode='operation')
    check = create('check-receipt', True)
    service.finish(check['id'], 'complete', result={'plan': 'untouched'})
    actual = create('apply-receipt', False)
    service.finish(actual['id'], 'complete', result={'job_id': 42})
    other = create('independent-apply', False)
    service.finish(other['id'], 'complete', result={'job_id': 43})
    failed = create('failed-check', True)
    service.finish(failed['id'], 'partial', result={'error': 'Source changed'})
    groups = service.history.page()['groups']
    assert [row['latest']['id'] for row in groups] == [failed['id'], other['id'], actual['id']]
    assert all(row['count'] == 1 for row in groups)
    assert groups[0]['label'] == 'Checking album import'
    assert service.get(check['id'])['result'] == {'plan': 'untouched'}
    assert service.get(actual['id'])['result'] == {'job_id': 42}
    # Restart reclassification must retain the same visibility and evidence.
    restarted = Activity(app)
    assert len(restarted.history.page()['groups']) == 3
    assert restarted.get(check['id'])['result']['plan'] == 'untouched'


def test_known_legacy_preview_migrates_without_losing_failed_checks_or_unknown_jobs(tmp_path):
    import json
    from beets_mvp.activity_history import group_key
    app = make_app(tmp_path); service = app.extensions['activity']
    for index, status in enumerate(('complete', 'failed')):
        job = service.new_external('Updating library files', 'Library', '/library')
        service.finish(job['id'], status, result={'receipt': index})
        saved = service.get(job['id'])
        saved['steps'] = [{'path': '/api/library/albums/1/metadata/preview', 'body': {}}]
        with service.db() as db:
            db.execute('UPDATE activity_jobs SET snapshot=?,history_group=? WHERE id=?', (json.dumps(saved), group_key(saved), job['id']))
    restarted = Activity(app)
    groups = restarted.history.page()['groups']
    assert len(groups) == 1 and groups[0]['attention'] == 1
    assert groups[0]['label'] == 'Checking metadata changes'
    with restarted.db() as db:
        assert db.execute('SELECT COUNT(*) FROM activity_jobs').fetchone()[0] == 2


def test_completed_outcomes_use_actual_rows_not_a_stale_processed_counter(tmp_path):
    from beets_mvp.activity_history import summary
    job = dict(id='match', status='complete', mode='matching', processed=0, total=3,
               rows=[{'status': 'matched'}, {'status': 'cached'}, {'status': 'no-match'}])
    assert summary(job)['outcome'] == '1 matched · 1 cached · 1 without matches'
    assert summary({**job, 'mode': 'import', 'rows': [{'status': 'imported', 'jobId': 42}]})['outcome'] == '1 imported'
    assert summary({**job, 'mode': 'operation', 'intent': 'delete', 'rows': [{'result': {'removed': ['a', 'b']}}, {'result': {'removed': ['c']}}]})['outcome'] == '3 files removed'
    assert summary({**job, 'mode': 'operation', 'intent': 'metadata', 'rows': []})['outcome'] is None


def test_apply_links_its_quiet_check_but_invalid_references_do_not_start_work(tmp_path):
    from flask import request
    app = make_app(tmp_path); service = app.extensions['activity']; client = app.test_client(); calls = []
    def operation(album_review_id):
        calls.append(request.path)
        return {'plan': 'fixture'}
    # Use the registered route endpoint rather than replacing any URL validation.
    endpoint = app.url_map.bind('').match('/api/inbox/albums/1/execute', method='POST')[0]
    app.view_functions[endpoint] = operation
    steps = [{'path': '/api/inbox/albums/1/execute', 'body': {}}]
    check = service.create('Check', 'Inbox', '/inbox', 'preflight-link', '', [], steps=[{**steps[0], 'body': {'dry_run': True}}], mode='operation')
    service.finish(check['id'], 'complete', result={'plan': 'checked'})
    request_body = {'request_id': 'apply-with-preflight', 'steps': steps, 'check_id': check['id']}
    response = client.post('/api/activity', json=request_body)
    assert response.status_code == 202
    finished = wait(client, response.json['id'])
    assert finished['status'] == 'complete', finished
    assert finished['check_id'] == check['id']
    assert service.get(finished['check_id'])['result']['plan'] == 'checked'
    assert len(calls) == 1
    for value in ('missing', 'f'*32, 123):
        assert client.post('/api/activity', json={**request_body, 'request_id': 'invalid-check-ref', 'check_id': value}).status_code == 400
    assert client.post('/api/activity', json={**request_body, 'steps': [{'path': '/api/inbox/albums/2/execute', 'body': {}}]}).status_code == 400
    assert len(calls) == 1


def test_multi_inbox_bulk_previews_are_quiet_and_linked_to_one_import(tmp_path):
    from flask import request
    from test_app import configure_named_inboxes
    app = make_app(tmp_path)
    configure_named_inboxes(app, [tmp_path/'left', tmp_path/'right'], ['Lidarr', 'Manual'])
    service = app.extensions['activity']; client = app.test_client(); checks = []
    sources = app.config['INBOXES']
    for source in sources:
        job = service.create('Preview', source['name'], '/inbox', source['id']+'-check', '', [],
                             steps=[{'path': f"/api/inbox/{source['id']}/bulk/preview", 'body': {}}], mode='operation')
        service.finish(job['id'], 'complete', result={'ready': 'retained'})
        checks.append(job['id'])
    def execute(inbox_id):
        album_id = request.json['album_ids'][0]
        return {'imported': [{'album_review_id': album_id, 'job_id': album_id}]}
    endpoint = app.url_map.bind('').match(f"/api/inbox/{sources[0]['id']}/bulk/execute", method='POST')[0]
    app.view_functions[endpoint] = execute
    steps = [{'path': f"/api/inbox/{sources[index % 2]['id']}/bulk/execute", 'id': index+1,
              'body': {'album_ids': [index+1]}} for index in range(3)]
    accepted = client.post('/api/activity', json={'request_id': 'mixed-checks-import', 'mode': 'import', 'steps': steps, 'check_ids': checks})
    assert accepted.status_code == 202, accepted.json
    finished = wait(client, accepted.json['id'])
    assert finished['status'] == 'complete' and finished['check_ids'] == checks
    assert finished['scope'] == 'All Inboxes'
    history = service.history.page()['groups']
    assert len(history) == 1 and history[0]['count'] == 1
    assert history[0]['latest']['outcome'] == '3 imported'
    assert all(service.get(check)['result']['ready'] == 'retained' for check in checks)


@pytest.mark.parametrize('delete_files', [False, True])
def test_library_removal_has_one_accurate_entry_with_its_check_and_original_guards(tmp_path, delete_files):
    from test_app import managed_wav_album
    app = make_app(tmp_path); client = app.test_client(); service = app.extensions['activity']
    library, album, paths = managed_wav_album(app, tmp_path)
    before = {path: path.read_bytes() for path in paths}
    fingerprint = client.get(f'/api/library/albums/{album.id}').json['fingerprint']
    check = submit(client, [{'path': f'/api/library/albums/{album.id}/removal/preview', 'body': {'expected_fingerprint': fingerprint}}], request_id='library-removal-check')
    checked = wait(client, check['id'])
    assert checked['is_check'] and checked['status'] == 'complete'
    assert service.history.page()['groups'] == []
    request_body = {'request_id': 'library-removal-apply', 'check_id': check['id'], 'steps': [{
        'path': f'/api/library/albums/{album.id}/removal/apply',
        'body': {'preview_id': checked['result']['preview_id'], 'expected_fingerprint': fingerprint, 'delete_files': delete_files, 'confirmed': True}}]}
    accepted = client.post('/api/activity', json=request_body)
    assert accepted.status_code == 202, accepted.json
    finished = wait(client, accepted.json['id'])
    assert finished['status'] == 'complete' and finished['check_id'] == check['id']
    assert finished['intent'] == ('library-delete' if delete_files else 'unmanage')
    groups = service.history.page()['groups']
    assert len(groups) == 1 and groups[0]['latest']['outcome'] == '2 tracks removed'
    assert not list(library.items())
    if delete_files:
        assert all(not path.exists() for path in paths)
    else:
        assert {path: path.read_bytes() for path in paths} == before
    repeated = client.post('/api/activity', json=request_body).json
    assert repeated['id'] == finished['id'] and repeated['result'] == finished['result']
    assert service.get(check['id'])['result']['preview_id'] == checked['result']['preview_id']


def test_library_removal_checks_upgrade_v1_history_but_preserve_failures_and_receipts(tmp_path):
    import json
    app = make_app(tmp_path); service = app.extensions['activity']
    ids = []
    for state in ['complete', 'failed']:
        job = service.new_external('Updating library files', 'Library', '/library')
        service.finish(job['id'], state, result={'receipt': state})
        saved = service.get(job['id']);saved['steps'] = [{'path': '/api/library/albums/1/removal/preview', 'body': {}}]
        with service.db() as db:
            db.execute('UPDATE activity_jobs SET snapshot=?,history_presentation=1 WHERE id=?', (json.dumps(saved), job['id']))
        ids.append(job['id'])
    restarted = Activity(app); groups = restarted.history.page()['groups']
    assert len(groups) == 1 and groups[0]['latest']['id'] == ids[1]
    assert groups[0]['attention'] == 1 and groups[0]['label'] == 'Checking Library removal'
    assert restarted.get(ids[0])['result'] == {'receipt': 'complete'}
    with restarted.db() as db:
        assert db.execute('SELECT COUNT(*) FROM activity_jobs WHERE history_presentation=2').fetchone()[0] == 2
