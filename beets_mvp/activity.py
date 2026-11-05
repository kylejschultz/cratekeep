"""Small, single-worker activity registry around existing validated application routes.

Live counters stay in memory so progress cannot contend with an inventory write
transaction. Accepted work and terminal results survive reload/restart in SQLite.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

from .activity_history import History, group_key, summary, operation_details

_current = ContextVar('cratekeep_activity', default=None)
ACTIVE = {'queued', 'running', 'waiting', 'stopping'}


def report(stage, done=None, total=None, unit='files', current=None, files_found=None):
    active = _current.get()
    if active:
        service, job_id = active
        service.update(job_id, stage=stage, stage_done=done, stage_total=total,
                       stage_unit=unit, current=current,
                       **({'files_found': files_found} if files_found is not None else {}))


def record_inbox_scan(result):
    active = _current.get()
    if active:
        service, job_id = active
        with service.lock:
            job = service.jobs[job_id]
            if job['label'] == 'Scanning Inbox':
                service.update(job_id, scan_added=job.get('scan_added', 0) + result['added'])


@contextmanager
def observed(app, label, scope, href):
    service = app.extensions.get('activity')
    if not service or _current.get() or app.testing:
        yield
        return
    try:
        job = service.new_external(label, scope, href)
    except sqlite3.Error:
        app.logger.exception('Activity tracking unavailable')
        yield
        return
    token = _current.set((service, job['id']))
    try:
        yield job
    except Exception as exc:
        service.finish(job['id'], 'failed', error=str(exc))
        raise
    else:
        service.finish(job['id'], 'partial' if service.jobs[job['id']].get('had_errors') else 'complete')
    finally:
        _current.reset(token)


class Activity:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.jobs = {}
        self.stops = {}
        self.slots = threading.Semaphore(4)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS activity_jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, work_key TEXT, snapshot TEXT NOT NULL)')
            for row in db.execute("SELECT id,snapshot FROM activity_jobs WHERE json_extract(snapshot,'$.status') IN ('queued','running','waiting','stopping')"):
                job = json.loads(row[1])
                if job['status'] in ACTIVE:
                    for item in job.get('rows', []):
                        if item['status'] == 'queued':
                            item['status'] = 'not-started'
                        elif item['status'] in {'running', 'matching', 'importing', 'cooldown'}:
                            item['status'] = 'needs-retry' if job.get('mode') == 'matching' else 'unknown'
                    job.update(status='interrupted', stage='Interrupted by server restart',
                               error='Check saved results before restarting unfinished work.', finished_at=time.time())
                    db.execute('UPDATE activity_jobs SET snapshot=? WHERE id=?', (json.dumps(job), row[0]))

        self.history = History(self)

    def db(self):
        return sqlite3.connect(self.app.config['APP_DB'], timeout=10)

    def list(self):
        with self.lock:
            live = deepcopy(self.jobs)
        with self.db() as db:
            saved = [json.loads(row[0]) for row in db.execute("SELECT snapshot FROM activity_jobs WHERE history_cleared=0 AND history_status NOT IN ('queued','running','waiting','stopping') ORDER BY rowid DESC LIMIT 20")]
        return [summary(job) for job in sorted([live.pop(job['id'], job) for job in saved] + list(live.values()), key=lambda job: job['created_at'], reverse=True)]

    def get(self, job_id):
        with self.lock:
            if job_id in self.jobs:
                return deepcopy(self.jobs[job_id])
        with self.db() as db:
            row = db.execute('SELECT snapshot FROM activity_jobs WHERE id=?', (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def checkpoint(self, job_id):
        with self.lock:
            snapshot = json.dumps(self.jobs[job_id])
        try:
            with sqlite3.connect(self.app.config['APP_DB'], timeout=.2) as db:
                db.execute('UPDATE activity_jobs SET snapshot=?,history_group=?,history_status=? WHERE id=?', (snapshot, group_key(json.loads(snapshot)), json.loads(snapshot)['status'], job_id))
        except sqlite3.Error:
            self.app.logger.warning('Activity checkpoint unavailable; live results retained')

    def update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values, updated_at=time.time())

    def finish(self, job_id, status, **values):
        self.update(job_id, status=status, stage={'complete': 'Finished', 'partial': 'Finished with errors', 'stopped': 'Stopped', 'failed': 'Failed'}.get(status, status), finished_at=time.time(), **values)
        with self.lock:
            job = deepcopy(self.jobs[job_id])
        try:
            with self.db() as db:
                db.execute('UPDATE activity_jobs SET snapshot=?,history_group=?,history_status=? WHERE id=?', (json.dumps(job), group_key(job), job['status'], job_id))
                self.history.compact_scan(db, job)
                self.history.compact_check(db, job)
        except sqlite3.Error:
            self.app.logger.exception('Activity result persistence failed; retaining live result')
            return
        with self.lock:
            self.jobs.pop(job_id, None)
            self.stops.pop(job_id, None)

    def create(self, label, scope, href, request_id, key, rows, *, steps=None, mode=None, inbox_id=None, check_id=None):
        job = dict(id=uuid.uuid4().hex, label=label, scope=scope, href=href, status='queued',
                   stage='Waiting for an available worker', stage_done=None, stage_total=None,
                   stage_unit='files', current=None, processed=0, total=len(rows), rows=rows,
                   created_at=time.time(), updated_at=time.time(), stop_requested=False, work_key=key, automatic_scan=label == 'Scanning Inbox' and steps is None)
        if check_id:
            job['check_id'] = check_id
        if steps is not None:
            presentation_steps = deepcopy(steps)
            for step in presentation_steps:
                if step['path'].endswith('/actions/apply'):
                    with self.db() as db:
                        saved = db.execute('SELECT kind FROM inbox_file_actions WHERE id=?', (step.get('body', {}).get('preview_id'),)).fetchone()
                    if saved:
                        step['body']['kind'] = saved[0]
            job.update(operation_details(presentation_steps, mode))
            job.update(steps=steps, mode=mode, inbox_id=inbox_id)
        with self.db() as db:
            db.execute('INSERT INTO activity_jobs(id,request_id,work_key,snapshot,history_group,history_status) VALUES (?,?,?,?,?,?)', (job['id'], request_id, key, json.dumps(job), group_key(job), job['status']))
        with self.lock:
            self.jobs[job['id']] = job
            self.stops[job['id']] = threading.Event()
        return deepcopy(job)

    def new_external(self, label, scope, href):
        job = self.create(label, scope, href, uuid.uuid4().hex, '', [])
        self.update(job['id'], status='running')
        return job

    def validate(self, values):
        if not isinstance(values, dict) or set(values) - {'request_id', 'steps', 'label', 'mode', 'check_id'}:
            raise ValueError('Expected an operation request and steps.')
        if not isinstance(values.get('request_id'), str) or not re.fullmatch(r'[a-zA-Z0-9-]{8,80}', values['request_id']):
            raise ValueError('A unique request_id is required.')
        steps = values.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 1000:
            raise ValueError('Provide between 1 and 1000 operation steps.')
        mode = values.get('mode', 'operation')
        if mode not in {'operation', 'matching', 'import', 'batch'} or (mode == 'operation' and len(steps) != 1):
            raise ValueError('Invalid operation mode.')
        if mode != 'operation' and any(not isinstance(step, dict) or type(step.get('id')) is not (str if mode == 'batch' else int) for step in steps):
            raise ValueError('Batch steps require valid source or album IDs.')
        if mode != "operation" and len({step.get("id") for step in steps if isinstance(step, dict)}) != len(steps):
            raise ValueError("Batch albums must be unique.")
        batch_sources, batch_actions = [], set()
        for step in steps:
            if not isinstance(step, dict) or set(step) - {'path', 'method', 'body', 'id', 'album'}:
                raise ValueError('Invalid operation step.')
            path, method = step.get('path'), step.get('method', 'POST')
            if not isinstance(path, str) or not re.fullmatch(r'/api/(?:inbox|library-import|library|imports)/[a-zA-Z0-9_/-]+', path) or '..' in path:
                raise ValueError('Unsupported operation path.')
            if method not in {'POST', 'PATCH', 'DELETE'} or not isinstance(step.get('body', {}), dict):
                raise ValueError('Operation steps must contain JSON mutation requests.')
            try:
                self.app.url_map.bind('').match(path, method=method)
            except HTTPException:
                raise ValueError('Unsupported operation route.') from None
            if mode == 'batch':
                match = re.fullmatch(r'/api/inbox/([a-zA-Z0-9_-]+)/(actions/(?:preview|apply)|(?:queue|scopes)/refresh)', path)
                if not match or not any(inbox['id'] == match[1] for inbox in self.app.config['INBOXES']):
                    raise ValueError('Inbox operation batches require configured source routes.')
                if step.get('id') != match[1]:
                    raise ValueError('An Inbox operation step must identify its actual source.')
                batch_sources.append(match[1])
                batch_actions.add(match[2])
            if mode == 'matching' and not path.endswith('/candidates'):
                raise ValueError('Matching batches may contain only matching requests.')
            if mode == 'import' and not path.endswith('/bulk/execute'):
                raise ValueError('Import batches may contain only album import requests.')
            if mode in {'matching', 'import'}:
                body = step.get('body', {})
                if mode == 'matching' and (type(body.get('album_id')) is not int or body.get('album_id') != step.get('id')):
                    raise ValueError('Each matching step must identify one album.')
                if mode == 'import' and body.get('album_ids') != [step.get('id')]:
                    raise ValueError('Each import step must identify one album.')
            if not isinstance(step.get('album', ''), str) or len(step.get('album', '')) > 500:
                raise ValueError('Invalid album label.')
        if mode == 'batch' and (len(batch_actions) != 1 or len(set(batch_sources)) != len(batch_sources)):
            raise ValueError('Use one action per Inbox and the same action type throughout a batch.')
        return steps, mode

    def submit(self, values):
        steps, mode = self.validate(values)
        check_id = values.get('check_id')
        if check_id is not None:
            if not isinstance(check_id, str) or not re.fullmatch('[a-f0-9]{32}', check_id):
                raise ValueError('Invalid preflight reference.')
            check = self.get(check_id)
            check_paths = [re.sub(r'/(preview|apply)$', '', step['path']) for step in (check or {}).get('steps', [])]
            paths = [re.sub(r'/(preview|apply)$', '', step['path']) for step in steps]
            if not check or not check.get('is_check') or check['status'] != 'complete' or check_paths != paths:
                raise ValueError('A successful preflight for this operation is required.')
        key = hashlib.sha256(json.dumps([mode, steps], sort_keys=True).encode()).hexdigest()
        with self.lock:
            with self.db() as db:
                existing = db.execute('SELECT snapshot FROM activity_jobs WHERE request_id=?', (values['request_id'],)).fetchone()
            if existing:
                return self.get(json.loads(existing[0])['id'])
            for job in self.jobs.values():
                if job.get('work_key') == key and job['status'] in ACTIVE:
                    return deepcopy(job)
            if sum(job['status'] in ACTIVE for job in self.jobs.values()) >= 20:
                raise ValueError('Too many operations are queued. Wait for current work to finish.')
            first = steps[0]['path']
            inbox_id = first.split('/')[3] if first.startswith('/api/inbox/') and first.split('/')[3] not in {'albums', 'candidates', 'reviews', 'scan'} else None
            inbox = next((row for row in self.app.config['INBOXES'] if row['id'] == inbox_id), None)
            album_sources = {}
            if first == '/api/inbox/candidates' or re.fullmatch(r'/api/inbox/albums/[0-9]+/execute', first):
                identities = [step['body'].get('album_id') if step['path'] == '/api/inbox/candidates' else int(step['path'].split('/')[4]) for step in steps]
                with self.db() as db:
                    album_sources = dict(db.execute('SELECT id,source_id FROM album_reviews WHERE id IN (' + ','.join('?' for _ in identities) + ')', identities))
            source_ids = set(album_sources.values()) or {step['path'].split('/')[3] for step in steps if step['path'].startswith('/api/inbox/')}
            sources = [entry for entry in self.app.config['INBOXES'] if entry['id'] in source_ids]
            inbox = sources[0] if len(sources) == 1 else None
            mixed = len(sources) > 1
            scope = 'All Inboxes' if mixed else inbox['name'] if inbox else 'Inbox' if first.startswith('/api/inbox/') else 'Library'
            href = '/inbox?source=all' if mixed else '/inbox' + ('?source=' + inbox['id'] if inbox else '') if first.startswith('/api/inbox/') else '/library-import' if first.startswith('/api/library-import/') else '/library'
            label = {'matching': 'Matching albums', 'import': 'Importing albums'}.get(mode)
            if not label:
                label = 'Refreshing files' if first.endswith('/refresh') else 'Finding matches' if '/candidates' in first or '/rematch/' in first else 'Updating library files'
            if first.startswith('/api/inbox/') and first.endswith(('/refresh', '/scan')):
                label = 'Scanning Inbox'
            rows = [dict(id=step.get('id'), album=step.get('album', ''), status='queued', attempts=0) for step in steps]
            for step, row in zip(steps, rows):
                if album_sources:
                    album_id = step.get('id') if step['path'] == '/api/inbox/candidates' else int(step['path'].split('/')[4])
                    source_id = album_sources.get(album_id)
                else:
                    source_id = step['path'].split('/')[3] if step['path'].startswith('/api/inbox/') else None
                row['inbox_id'] = source_id
                row['inbox_name'] = next((entry['name'] for entry in sources if entry['id'] == source_id), '')
            job = self.create(label, scope, href, values['request_id'], key, rows, steps=steps, mode=mode, inbox_id=inbox['id'] if inbox else None, check_id=check_id)
            job['inbox_ids'] = [entry['id'] for entry in sources]
            self.update(job['id'], inbox_ids=job['inbox_ids'])
        threading.Thread(target=self.run, args=(job['id'], steps, mode), daemon=True, name='cratekeep-activity').start()
        return self.get(job['id'])

    def stop(self, job_id):
        with self.lock:
            if job_id not in self.stops:
                return self.get(job_id)
            self.stops[job_id].set()
            self.update(job_id, stop_requested=True, status='stopping', stage='Stopping after current album')
        return self.get(job_id)

    def wait_retry(self, event, seconds):
        return event.wait(seconds)

    def run(self, job_id, steps, mode):
        event = self.stops[job_id]
        while not self.slots.acquire(timeout=.2):
            if event.is_set():
                self.update(job_id, rows=[{**row, 'status': 'not-started'} for row in self.jobs[job_id]['rows']])
                self.finish(job_id, 'stopped')
                return
        token = _current.set((self, job_id))
        try:
            with self.app.app_context():
                deadline = time.monotonic() + 15 * 60 if mode == 'matching' else float('inf')
                rows = deepcopy(self.jobs[job_id]['rows'])
                last_result, last_status = None, 200
                for index, step in enumerate(steps):
                    if event.is_set() or time.monotonic() > deadline:
                        break
                    for attempt in range(1, 4 if mode == 'matching' else 2):
                        rows[index].update(status='matching' if mode == 'matching' else 'importing' if mode == 'import' else 'running', attempts=attempt)
                        self.update(job_id, status='running', rows=deepcopy(rows), stage='Waiting for other library work', current=step.get('album'), stage_done=None, stage_total=None, processed=index)
                        self.checkpoint(job_id)
                        with self.app.test_request_context(step['path'], method=step.get('method', 'POST'), json=step.get('body', {})):
                            response = self.app.make_response(self.app.full_dispatch_request())
                            last_status = response.status_code
                            last_result = response.get_json(silent=True) or {}
                        error = ((last_result.get('result') or last_result).get('errors') or [{}])[0] if mode == 'matching' else {}
                        if mode == 'matching':
                            data = last_result.get('album')
                            success = last_status < 400 and data and data.get('candidate_status') == 'complete' and not error
                            if success:
                                rows[index]['reason'] = ''
                            rows[index]['status'] = ('cached' if (last_result.get('already_matched') or last_result.get('result', {}).get('already_matched')) else 'matched' if data.get('candidates') else 'no-match') if success else 'failed'
                            reason = error.get('error') or last_result.get('error') or (data or {}).get('candidate_error') or 'Matching did not complete.'
                            retryable = error.get('retryable', last_result.get('retryable', last_status in {429, 502, 503, 504}))
                            if not success and retryable:
                                seconds = max(10 * 2 ** (attempt - 1), error.get('retry_after_seconds', last_result.get('retry_after_seconds', 0)) or 0)
                                if attempt < 3 and time.monotonic() + seconds < deadline and not event.is_set():
                                    rows[index].update(status='cooldown', reason=reason)
                                    self.update(job_id, status='waiting', stage='MusicBrainz cooldown', resume_at=time.time() + seconds, rows=deepcopy(rows))
                                    self.wait_retry(event, seconds)
                                    if not event.is_set():
                                        continue
                                rows[index]['status'] = 'needs-retry'
                                event.set()
                            if not success:
                                rows[index]['reason'] = reason
                        elif mode == 'import':
                            receipt = next((row for row in last_result.get('imported', []) if row.get('album_review_id') == step.get('id')), None)
                            if receipt:
                                rows[index].update(status='imported', jobId=receipt.get('job_id'))
                            else:
                                rows[index].update(status='unknown' if last_status >= 500 else 'failed', reason=(last_result.get('failed') or last_result.get('skipped') or [{'error': last_result.get('error', 'Check the saved result.')}])[0].get('error', 'Album was not imported.'))
                                if last_status >= 500:
                                    event.set()
                        else:
                            rows[index]['status'] = 'complete' if last_status < 400 and not (last_result.get('errors') or last_result.get('failed') or last_result.get('cleanup', {}).get('errors')) else 'failed'
                            if last_status >= 400:
                                rows[index]['reason'] = last_result.get('error', 'Operation failed.')
                        if mode != 'matching':
                            rows[index]['result'] = last_result
                        rows[index]['http_status'] = last_status
                        break
                    self.update(job_id, rows=deepcopy(rows), processed=index + 1, resume_at=None)
                    self.checkpoint(job_id)
                for row in rows:
                    if row['status'] == 'queued':
                        row['status'] = 'not-started'
                failed = any(row['status'] in {'failed', 'unknown', 'needs-retry'} for row in rows)
                state = 'partial' if failed else 'stopped' if any(row['status'] == 'not-started' for row in rows) else 'complete'
                self.update(job_id, rows=rows)
                self.finish(job_id, state, result=last_result, http_status=last_status)
        except Exception as exc:
            self.finish(job_id, 'failed', error=f'Operation stopped; check saved results before retrying: {exc}', http_status=500)
        finally:
            _current.reset(token)
            self.slots.release()


def register(app):
    service = Activity(app)
    app.extensions['activity'] = service

    @app.get('/api/activity')
    def activity_list():
        return jsonify(jobs=service.list())

    @app.get('/api/activity/history')
    def activity_history():
        try:
            def cursor(name):
                value = request.args.get(name)
                if value is None:
                    return None
                if not value.isdecimal() or len(value) > 18:
                    raise ValueError('Invalid history cursor.')
                return int(value)
            return jsonify(service.history.page(group=request.args.get('group'), before=cursor('before'), snapshot=cursor('snapshot'), priority=cursor('priority')))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/activity/history/clear')
    def activity_clear_history():
        try:
            return jsonify(service.history.clear(request.get_json(silent=True)))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400

    @app.post('/api/activity')
    def activity_submit():
        try:
            return jsonify(service.submit(request.get_json(silent=True))), 202
        except ValueError as exc:
            return jsonify(error=str(exc), code='invalid_activity'), 400

    @app.post('/api/activity/<job_id>/resume')
    def activity_resume(job_id):
        job = service.get(job_id)
        if not job or job.get('mode') != 'matching' or job['status'] in ACTIVE:
            return jsonify(error='Only finished matching runs can resume unfinished albums.'), 409
        unfinished = {row['id'] for row in job['rows'] if row['status'] in {'failed', 'needs-retry', 'not-started'}}
        steps = [step for step in job.get('steps', []) if step.get('id') in unfinished]
        if not steps:
            return jsonify(error='No unfinished matching albums.'), 409
        return jsonify(service.submit({'request_id': uuid.uuid4().hex, 'mode': 'matching', 'steps': steps})), 202

    @app.get('/api/activity/<job_id>')
    def activity_status(job_id):
        job = service.get(job_id)
        return (jsonify(job), 200) if job else (jsonify(error='Activity not found'), 404)

    @app.post('/api/activity/<job_id>/stop')
    def activity_stop(job_id):
        job = service.stop(job_id)
        return (jsonify(job), 200) if job else (jsonify(error='Activity not found'), 404)


def observe(label):
    from functools import wraps
    def decorate(function):
        @wraps(function)
        def wrapped(app, *args, **kwargs):
            inbox = args[0] if args and isinstance(args[0], dict) and 'name' in args[0] else next((row for row in app.config['INBOXES'] if row['id'] == kwargs.get('source_id')), None)
            scope = inbox['name'] if inbox else 'Library'
            href = '/inbox?source=' + inbox['id'] if inbox else '/library-import'
            with observed(app, label, scope, href) as job:
                result = function(app, *args, **kwargs)
                if job:
                    app.extensions['activity'].update(job['id'], result=result, had_errors=bool(result.get('errors')))
                return result
        return wrapped
    return decorate
