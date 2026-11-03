"""Activity owns work lifetime, not the requesting page, and reuses route guards."""
import threading
import time
from test_app import make_app


def wait(client, job_id):
    for _ in range(150):
        job = client.get('/api/activity/' + job_id).json
        if job['status'] not in {'queued', 'running', 'waiting', 'stopping'}:
            return job
        time.sleep(.02)
    raise AssertionError('Activity did not finish')


def submit(client, steps, request_id='fixture-request-1', mode='operation'):
    response = client.post('/api/activity', json={'request_id': request_id, 'steps': steps, 'mode': mode})
    assert response.status_code == 202, response.json
    return response.json


def test_progress_reads_are_responsive_and_duplicate_starts_attach(tmp_path):
    from beets_mvp.activity import report
    app = make_app(tmp_path); started, release = threading.Event(), threading.Event(); calls = []
    def operation():
        calls.append(1); report('Reading tags', 2, 10); started.set(); assert release.wait(5)
        return {'files': 10}
    app.view_functions['refresh_library_album_queue'] = operation
    client = app.test_client()
    steps = [{'path': '/api/library-import/queue/refresh', 'body': {}}]
    first = submit(client, steps)
    assert started.wait(3)
    running = client.get('/api/activity/' + first['id']).json
    assert running['stage_done'] == 2 and running['stage_total'] == 10
    # Service deduplication is independent of the HTTP mutation lock.
    duplicate = app.extensions['activity'].submit({'request_id': 'fixture-request-2', 'steps': steps})
    assert duplicate['id'] == first['id']
    release.set()
    finished = wait(client, first['id'])
    assert finished['status'] == 'complete' and finished['result'] == {'files': 10}
    assert calls == [1]
    assert submit(client, steps)['id'] == first['id']


def test_batch_stop_keeps_current_result_and_does_not_start_next_album(tmp_path):
    app = make_app(tmp_path); started, release = threading.Event(), threading.Event(); calls = []
    from flask import request
    # Use the production route name/validation surface, with a controlled worker seam.
    def execute():
        album_id = request.json['album_ids'][0]; calls.append(album_id); started.set(); assert release.wait(5)
        return {'imported': [{'album_review_id': album_id, 'job_id': album_id}], 'failed': []}
    app.view_functions['execute_ready_library_imports'] = execute
    client = app.test_client()
    steps = [{'path': '/api/library-import/bulk/execute', 'body': {'album_ids': [i]}, 'id': i, 'album': str(i)} for i in (1, 2)]
    job = submit(client, steps, mode='import'); assert started.wait(3)
    assert client.post('/api/activity/' + job['id'] + '/stop', json={}).status_code == 200
    release.set()
    finished = wait(client, job['id'])
    assert finished['status'] == 'stopped' and calls == [1]
    assert [row['status'] for row in finished['rows']] == ['imported', 'not-started']


def test_errors_do_not_claim_success_and_progress_cannot_escape_api_routes(tmp_path):
    app = make_app(tmp_path)
    def failed():
        return {'error': 'Check saved files'}, 500
    app.view_functions['refresh_library_album_queue'] = failed
    client = app.test_client()
    job = submit(client, [{'path': '/api/library-import/queue/refresh', 'body': {}}])
    final = wait(client, job['id'])
    assert final['status'] == 'partial' and final['http_status'] == 500
    for path in ('/settings', 'https://example.com', '/api/activity', '/api/library/../settings'):
        assert client.post('/api/activity', json={'request_id': 'bad-request-1', 'steps': [{'path': path, 'body': {}}]}).status_code == 400


def test_restart_marks_unfinished_work_interrupted_and_preserves_finished_results(tmp_path):
    from beets_mvp.activity import Activity
    app = make_app(tmp_path); service = app.extensions['activity']
    job = service.new_external('Scan', 'Library', '/library-import')
    restarted = Activity(app)
    interrupted = restarted.get(job['id'])
    assert interrupted['status'] == 'interrupted'
    assert 'Check saved results' in interrupted['error']


def test_matching_retries_cooldown_and_resumes_only_unfinished_albums(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    from flask import request
    calls = []
    def execute():
        album_id = request.json['album_id']; calls.append(album_id)
        if calls == [1]:
            return {'album': None, 'errors': [{'error': 'Transient fixture', 'retryable': True}]}, 207
        return {'album': {'candidate_status': 'complete', 'candidates': [1]}, 'errors': []}
    app.view_functions['generate_library_import_candidates'] = execute
    monkeypatch.setattr(app.extensions['activity'], 'wait_retry', lambda event, seconds: None)
    client = app.test_client()
    steps = [{'path': '/api/library-import/candidates', 'body': {'album_id': i}, 'id': i, 'album': str(i)} for i in (1, 2)]
    job = submit(client, steps, mode='matching')
    finished = wait(client, job['id'])
    assert finished['status'] == 'complete' and calls == [1, 1, 2]
    assert all(not row.get('reason') for row in finished['rows'])
    assert client.post('/api/activity/' + job['id'] + '/resume', json={}).status_code == 409


def test_retry_wait_is_visible_and_successful_albums_are_not_repeated_on_resume(tmp_path, monkeypatch):
    app = make_app(tmp_path); service = app.extensions['activity']; client = app.test_client()
    from flask import request
    calls, waits = [], []
    def execute():
        album_id = request.json['album_id']; calls.append(album_id)
        if album_id == 2:
            return {'album': None, 'errors': [{'error': 'Cooldown fixture', 'retryable': True, 'retry_after_seconds': 3}]}, 207
        return {'album': {'candidate_status': 'complete', 'candidates': [1]}, 'errors': []}
    app.view_functions['generate_library_import_candidates'] = execute
    monkeypatch.setattr(service, 'wait_retry', lambda event, seconds: waits.append((seconds, service.list()[0]['stage'])))
    steps = [{'path': '/api/library-import/candidates', 'body': {'album_id': i}, 'id': i, 'album': str(i)} for i in (1, 2, 3)]
    job = submit(client, steps, mode='matching')
    ended = wait(client, job['id'])
    assert [row['status'] for row in ended['rows']] == ['matched', 'needs-retry', 'not-started']
    assert waits == [(10, 'MusicBrainz cooldown'), (20, 'MusicBrainz cooldown')]
    app.view_functions['generate_library_import_candidates'] = lambda: {'album': {'candidate_status': 'complete', 'candidates': []}, 'errors': []}
    resumed = client.post('/api/activity/' + job['id'] + '/resume', json={})
    assert resumed.status_code == 202
    final = wait(client, resumed.json['id'])
    assert [row['id'] for row in final['rows']] == [2, 3]
    assert all(row['status'] == 'no-match' for row in final['rows'])


def test_restart_retains_committed_album_results_and_marks_inflight_uncertain(tmp_path):
    from beets_mvp.activity import Activity
    app = make_app(tmp_path); service = app.extensions['activity']
    rows = [{'id': 1, 'status': 'imported', 'jobId': 9}, {'id': 2, 'status': 'importing'}, {'id': 3, 'status': 'queued'}]
    job = service.create('Import', 'Library', '/library-import', 'restart-fixture', 'key', rows)
    service.update(job['id'], mode='import', status='running', processed=1)
    service.checkpoint(job['id'])
    restored = Activity(app).get(job['id'])
    assert restored['status'] == 'interrupted'
    assert [row['status'] for row in restored['rows']] == ['imported', 'unknown', 'not-started']
    assert restored['rows'][0]['jobId'] == 9


def test_inbox_saved_match_reuse_is_reported_as_cached(tmp_path):
    app = make_app(tmp_path); client = app.test_client()
    app.view_functions['generate_inbox_candidates'] = lambda: {'result': {'already_matched': True, 'errors': []}, 'album': {'candidate_status': 'complete', 'candidates': [1]}}
    job = submit(client, [{'path': '/api/inbox/candidates', 'id': 1, 'body': {'album_id': 1}}], mode='matching')
    assert wait(client, job['id'])['rows'][0]['status'] == 'cached'


def test_refresh_keeps_cumulative_track_count_through_folders_and_finalization(tmp_path, monkeypatch):
    from test_app import tagged_inbox_wav
    app = make_app(tmp_path)
    for name in ('Root.wav', 'Artist/One.wav', 'Artist/Two.wav', 'Other/Three.wav'):
        tagged_inbox_wav(tmp_path / 'library' / name, album='Count fixture')
    service = app.extensions['activity']
    snapshots = []
    original = service.update
    def record(job_id, **values):
        original(job_id, **values)
        snapshots.append(dict(service.jobs[job_id]))
    monkeypatch.setattr(service, 'update', record)
    client = app.test_client()
    # A fresh and unchanged refresh must each count all tracks, not reset per folder.
    for index in range(2):
        snapshots.clear()
        job = submit(client, [{'path': '/api/library-import/queue/refresh', 'body': {}}],
                     request_id=f'cumulative-refresh-{index}')
        result = wait(client, job['id'])
        counts = [row['files_found'] for row in snapshots if 'files_found' in row]
        assert counts == sorted(counts)
        assert counts[0] == 0 and counts[-1] == 4
        assert result['status'] == 'complete' and result['files_found'] == 4
        syncing = [row for row in snapshots if row['stage'] == 'Synchronizing album reviews']
        assert syncing and syncing[-1]['files_found'] == 4


def test_scoped_inbox_discovery_counts_unique_tracks_across_overlapping_folders(tmp_path, monkeypatch):
    from test_app import write_wav
    import beets_mvp
    root = tmp_path / 'inbox'
    for name in ('Root.wav', 'Artist/One.wav', 'Artist/Album/Two.wav'):
        write_wav(root / name)
    counts = []
    monkeypatch.setattr(beets_mvp, 'report_progress', lambda *args, **values: counts.append(values['files_found']))
    files = beets_mvp._inbox_scope_audio_stats(root, ['.', 'Artist', 'Artist/Album'])
    assert len(files) == 3
    assert counts == sorted(counts) and counts[-1] == 3
