from pathlib import Path
import os
import sys
import pytest
from test_app import make_app, configure_named_inboxes, tagged_inbox_wav
from test_activity import submit, wait


def sources(tmp_path, albums=2):
    app = make_app(tmp_path)
    roots = [tmp_path / 'left', tmp_path / 'right']
    configure_named_inboxes(app, roots, ['Lidarr', 'Manual'])
    app.config['MUSICBRAINZ_PROVIDER'] = lambda *args, **kwargs: pytest.fail('queue read contacted provider')
    client = app.test_client()
    for root, inbox in zip(roots, app.config['INBOXES']):
        for index in range(albums):
            tagged_inbox_wav(root / f'Album-{index:02}' / 'song.wav', album=f'Album {index:02}')
        assert client.post(f"/api/inbox/{inbox['id']}/queue/refresh", json={}).status_code == 200
    return app, client, roots


def test_all_queue_has_global_pages_and_distinct_source_identity(tmp_path):
    app, client, roots = sources(tmp_path, 16)
    first = client.get('/api/inbox/all/queue').json
    second = client.get('/api/inbox/all/queue?page=2').json
    assert (first['total'], len(first['albums']), len(second['albums'])) == (32, 25, 7)
    albums = first['albums'] + second['albums']
    assert len({album['id'] for album in albums}) == 32
    assert [album['album'] for album in albums] == sorted(album['album'] for album in albums)
    assert {album['inbox_name'] for album in albums} == {'Lidarr', 'Manual'}
    assert len(client.get('/api/inbox/all/queue?q=Album+03').json['albums']) == 2
    assert client.get('/api/inbox/all/queue?status=pending').json['total'] == 32
    assert client.get('/api/inbox/all/queue?status=matched').json['total'] == 0
    assert client.get('/api/inbox/all/queue?page=0').status_code == 400
    for inbox in app.config['INBOXES']:
        assert client.get(f"/api/inbox/{inbox['id']}/queue").json['total'] == 16
    # A root change never resurrects the old root's reviews.
    configure_named_inboxes(app, [roots[0], tmp_path / 'replacement'])
    assert client.get('/api/inbox/all/queue').json['total'] == 16


def test_all_saved_rows_and_scoped_errors_survive_unavailable_source(tmp_path):
    app, client, roots = sources(tmp_path)
    roots[0].rename(tmp_path / 'offline')
    inbox = app.config['INBOXES'][0]
    assert client.post(f"/api/inbox/{inbox['id']}/queue/refresh", json={}).status_code == 409
    assert client.get('/api/inbox/all/queue').json['total'] == 4
    status = client.get('/api/inbox/all/discovery').json
    assert status['mode'] == 'all' and 'Lidarr' in status['error']
    assert len(status['inboxes']) == 2


def test_batch_refresh_is_source_aware_and_keeps_success_after_an_error(tmp_path):
    app, client, roots = sources(tmp_path)
    roots[0].rename(tmp_path / 'offline')
    steps = [{'id': inbox['id'], 'path': f"/api/inbox/{inbox['id']}/queue/refresh", 'album': inbox['name'], 'body': {}} for inbox in app.config['INBOXES']]
    run = submit(client, steps, mode='batch')
    finished = wait(client, run['id'])
    assert finished['scope'] == 'All Inboxes' and finished['href'] == '/inbox?source=all'
    assert finished['inbox_id'] is None
    assert len(finished['inbox_ids']) == 2
    assert finished['status'] == 'partial'
    assert [row['http_status'] for row in finished['rows']] == [409, 200]
    assert [row['inbox_name'] for row in finished['rows']] == ['Lidarr', 'Manual']
    assert client.get('/api/inbox/all/queue').json['total'] == 4
    assert submit(client, steps, mode='batch')['id'] == finished['id']


def test_mixed_delete_plans_and_exact_once_receipts_are_scoped(tmp_path):
    app, client, roots = sources(tmp_path, 1)
    albums = client.get('/api/inbox/all/queue').json['albums']
    steps = [{'id': album['source_id'], 'path': f"/api/inbox/{album['source_id']}/actions/preview", 'album': album['inbox_name'], 'body': {'kind': 'delete-albums', 'album_ids': [album['id']]}} for album in albums]
    preview = wait(client, submit(client, steps, mode='batch')['id'])
    assert preview['status'] == 'complete'
    assert all(row['result']['count'] == 1 for row in preview['rows'])
    # A plan from another Inbox must not be usable under this source.
    foreign = preview['rows'][1]['result']
    wrong = client.post(f"/api/inbox/{albums[0]['source_id']}/actions/apply", json={'preview_id': foreign['preview_id'], 'expected_fingerprint': foreign['fingerprint'], 'confirmed': True})
    assert wrong.status_code == 409
    apply = [{'id': row['id'], 'path': f"/api/inbox/{row['inbox_id']}/actions/apply", 'album': row['inbox_name'], 'body': {'preview_id': row['result']['preview_id'], 'expected_fingerprint': row['result']['fingerprint'], 'confirmed': True}} for row in preview['rows']]
    result = wait(client, submit(client, apply, request_id='delete-batch-request', mode='batch')['id'])
    assert result['status'] == 'complete'
    assert all(not (root / 'Album-00').exists() for root in roots)
    assert submit(client, apply, request_id='delete-batch-request', mode='batch')['id'] == result['id']
    for row in preview['rows']:
        saved = client.get(f"/api/inbox/{row['inbox_id']}/actions/{row['result']['preview_id']}").json
        assert saved['status'] == 'complete'


@pytest.mark.parametrize('paths', [['all/queue/refresh'], ['missing/actions/apply'], ['one/actions/preview', 'two/actions/apply']])
def test_batch_rejects_aggregate_unknown_or_mixed_action_routes(tmp_path, paths):
    app, client, _ = sources(tmp_path, 1)
    ids = [inbox['id'] for inbox in app.config['INBOXES']]
    steps = [{'id': str(index), 'path': '/api/inbox/' + path.replace('one', ids[0]).replace('two', ids[1]), 'body': {}} for index, path in enumerate(paths)]
    assert client.post('/api/activity', json={'request_id': 'invalid-batch-id', 'mode': 'batch', 'steps': steps}).status_code == 400


@pytest.mark.parametrize('kind', ['undo', 'delete-imported'])
def test_mixed_import_receipts_support_scoped_cleanup_and_undo(tmp_path, kind, monkeypatch):
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    app, client, roots = sources(tmp_path, 1)
    tagged_inbox_wav(roots[1] / 'Album-00/song.wav', artist='Second artist', album='Album 00')
    client.post(f"/api/inbox/{app.config['INBOXES'][1]['id']}/queue/refresh", json={})
    albums = client.get('/api/inbox/all/queue').json['albums']
    for album in albums:
        assert client.patch(f"/api/inbox/albums/{album['id']}", json={'decision': 'approved', 'candidate_id': None}).status_code == 200
    steps = [{'id': album['id'], 'album': album['album'], 'path': f"/api/inbox/{album['source_id']}/bulk/execute", 'body': {'album_ids': [album['id']], 'scopes': ['Album-00'], 'mode': 'selected', 'confirmed': True}} for album in albums]
    imported = wait(client, submit(client, steps, mode='import')['id'])
    assert imported['scope'] == 'All Inboxes'
    assert all(row['status'] == 'imported' and row['jobId'] for row in imported['rows']), imported
    assert all((root / 'Album-00/song.wav').exists() for root in roots)
    from beets.library import Library
    assert len(list(Library(app.config['BEETS_DB']).items())) == 2
    plans = []
    for row in imported['rows']:
        preview = client.post(f"/api/inbox/{row['inbox_id']}/actions/preview", json={'kind': kind, 'job_ids': [row['jobId']]})
        assert preview.status_code == 200, preview.json
        plans.append({'id': row['inbox_id'], 'album': row['inbox_name'], 'path': f"/api/inbox/{row['inbox_id']}/actions/apply", 'body': {'preview_id': preview.json['preview_id'], 'expected_fingerprint': preview.json['fingerprint'], 'confirmed': True}})
    result = wait(client, submit(client, plans, request_id='cleanup-actions-request', mode='batch')['id'])
    assert result['status'] == 'complete', result
    assert len(list(Library(app.config['BEETS_DB']).items())) == (0 if kind == 'undo' else 2)
    assert all((root / 'Album-00/song.wav').exists() == (kind == 'undo') for root in roots)


def test_mixed_delete_retains_stale_source_and_records_partial_results(tmp_path):
    app, client, roots = sources(tmp_path, 1)
    albums = client.get('/api/inbox/all/queue').json['albums']
    plans = []
    for album in albums:
        preview = client.post(f"/api/inbox/{album['source_id']}/actions/preview", json={'kind': 'delete-albums', 'album_ids': [album['id']]}).json
        plans.append({'id': album['source_id'], 'path': f"/api/inbox/{album['source_id']}/actions/apply", 'body': {'preview_id': preview['preview_id'], 'expected_fingerprint': preview['fingerprint'], 'confirmed': True}})
    target = next(root for root, inbox in zip(roots, app.config['INBOXES']) if inbox['id'] == plans[1]['id']) / 'Album-00/song.wav'
    target.write_bytes(target.read_bytes() + b'changed')
    result = wait(client, submit(client, plans, mode='batch')['id'])
    assert result['status'] == 'partial'
    assert result['rows'][0]['result']['status'] == 'complete'
    assert result['rows'][1]['http_status'] == 409 and target.exists()
    assert all(row['attempts'] == 1 for row in result['rows'])
