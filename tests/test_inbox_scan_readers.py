import threading

import beets_mvp as backend
from test_app import make_app, tagged_inbox_wav


def test_saved_queue_remains_readable_during_a_large_inventory_write(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    inbox = app.config['INBOXES'][0]
    tagged_inbox_wav(tmp_path / 'inbox/Saved/Song.wav')
    backend._scan_inbox_inventory(app, inbox)
    for i in range(100):
        path = tmp_path / f'inbox/New/{i:03}.wav'
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'fixture')

    paused, release, read_done = threading.Event(), threading.Event(), threading.Event()
    original_connect = backend._connect
    calls = 0
    class ScanConnection:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, name):
            return getattr(self.db, name)
        def __enter__(self):
            self.db.__enter__()
            return self
        def __exit__(self, *args):
            return self.db.__exit__(*args)
        def execute(self, sql, *args):
            nonlocal calls
            cursor = self.db.execute(sql, *args)
            if sql.startswith('UPDATE adoption_reviews SET candidates_json='):
                calls += 1
                if calls == 80:
                    paused.set()
                    assert release.wait(10)
            return cursor
    def small_cache(path):
        db = original_connect(path)
        db.execute('PRAGMA cache_size=8')
        return ScanConnection(db) if threading.current_thread().name == 'scan' else db
    monkeypatch.setattr(backend, '_connect', small_cache)
    monkeypatch.setattr(backend, '_inbox_tag_snapshot', lambda path: {'kind': 'source-tags', 'title': 'x' * 4000})
    failures = []
    def scan():
        try:
            backend._scan_inbox_inventory(app, inbox)
        except Exception as exc:
            failures.append(exc)
    result = []
    def read():
        try:
            result.append(app.test_client().get(f"/api/inbox/{inbox['id']}/queue"))
        finally:
            read_done.set()
    scanner = threading.Thread(target=scan, name='scan')
    scanner.start()
    assert paused.wait(5)
    reader = threading.Thread(target=read)
    reader.start()
    try:
        assert read_done.wait(2), 'saved albums were blocked behind the scan write transaction'
        assert result[0].status_code == 200
        assert result[0].json['total'] == 1
    finally:
        release.set()
        scanner.join(10)
        reader.join(10)
    assert not failures


def test_combined_activity_reports_tag_reads_before_inventory_writes(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    tagged_inbox_wav(tmp_path / 'inbox/New/Song.wav')
    stages = []
    monkeypatch.setattr(backend, 'report_progress', lambda stage, *args, **kwargs: stages.append(stage))
    original_snapshot = backend._inbox_tag_snapshot
    def snapshot(path):
        assert stages[-1] == 'Reading file tags'
        assert 'Saving inventory' not in stages
        return original_snapshot(path)
    monkeypatch.setattr(backend, '_inbox_tag_snapshot', snapshot)
    result = backend._scan_inbox_inventory(app, app.config['INBOXES'][0])
    assert result['files'] == 1
    assert 'Saving inventory' in stages
