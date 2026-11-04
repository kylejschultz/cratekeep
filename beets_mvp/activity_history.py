"""Paged presentation of durable activity; clearing never removes execution evidence."""
import hashlib
import json
import re

ACTIVE = ('queued', 'running', 'waiting', 'stopping')
ATTENTION = ('partial', 'failed', 'interrupted')
PAGE_SIZE = 20


def added_tracks(job):
    added = job.get('scan_added')
    if type(added) is int and added >= 0:
        return added
    result = job.get('result') or {}
    inventory = result.get('inventory', result)
    added = inventory.get('added') if isinstance(inventory, dict) else None
    return added if type(added) is int and added >= 0 else None


def group_key(job):
    if job.get('is_inbox_status'):
        return 'inbox-scans'
    if job['label'] == 'Scanning Inbox':
        if job['status'] in ACTIVE:
            return 'inbox-scans'
        return 'inbox-scan-' + job['id']
    operations = sorted({(step.get('method', 'POST'), re.sub(r'/[0-9]+(?=/|$)', '/:id', step['path'])) for step in job.get('steps', [])})
    return hashlib.sha256(json.dumps([job['label'], job['href'], operations]).encode()).hexdigest()


def summary(job):
    return {key: value for key, value in job.items() if key not in {'rows', 'steps', 'result'}} | {'rows': []}


class History:
    def __init__(self, activity):
        self.activity = activity
        with activity.db() as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(activity_jobs)')}
            for column, definition in [('history_group', 'TEXT'), ('history_status', 'TEXT'), ('history_cleared', 'INTEGER NOT NULL DEFAULT 0'), ('history_scan_policy', 'INTEGER NOT NULL DEFAULT 0')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE activity_jobs ADD COLUMN {column} {definition}')
            for job_id, snapshot in db.execute('SELECT id,snapshot FROM activity_jobs WHERE history_group IS NULL OR history_status IS NULL'):
                job = json.loads(snapshot)
                db.execute('UPDATE activity_jobs SET history_group=?,history_status=? WHERE id=?', (group_key(job), job['status'], job_id))
            db.execute('CREATE INDEX IF NOT EXISTS activity_history_groups ON activity_jobs(history_group,history_cleared)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_history_status ON activity_jobs(history_status,history_cleared)')
            # Restart interruption is resolved by Activity before history is opened.
            db.execute("UPDATE activity_jobs SET history_status=json_extract(snapshot,'$.status') WHERE history_status IN ('queued','running','waiting','stopping')")
            # Existing scan rows are reclassified once. Only proven zero-arrival
            # successes can be compacted; unknown older outcomes remain accessible.
            for job_id, snapshot, cleared in db.execute("SELECT id,snapshot,history_cleared FROM activity_jobs WHERE history_scan_policy=0 AND json_extract(snapshot,'$.label')='Scanning Inbox' AND history_status NOT IN ('queued','running','waiting','stopping') ORDER BY COALESCE(json_extract(snapshot,'$.finished_at'),json_extract(snapshot,'$.created_at'))"):
                job = json.loads(snapshot)
                db.execute('UPDATE activity_jobs SET history_group=? WHERE id=?', (group_key(job), job_id))
                self.compact_scan(db, job, visible=not cleared)

    def compact_scan(self, db, job, *, visible=True):
        if job['label'] != 'Scanning Inbox' or job['status'] in ACTIVE:
            return
        db.execute('UPDATE activity_jobs SET history_scan_policy=1 WHERE id=?', (job['id'],))
        added = added_tracks(job)
        if job['status'] != 'complete' or added is None:
            return
        if visible:
            # The status is intentionally a single replaceable snapshot, not a
            # run receipt. Reinsertion keeps cursor ordering current and monotonic.
            status = dict(id='inbox-scan-status', label='Inbox scans', scope='All Inboxes', href='/inbox',
                          status='complete', stage='Success', rows=[], processed=0, total=0,
                          created_at=job.get('finished_at', job['created_at']),
                          finished_at=job.get('finished_at', job['created_at']), is_inbox_status=True)
            previous = db.execute("SELECT snapshot FROM activity_jobs WHERE id='inbox-scan-status'").fetchone()
            if previous:
                previous = json.loads(previous[0])
                if previous['finished_at'] > status['finished_at']:
                    status = previous
            db.execute("DELETE FROM activity_jobs WHERE id='inbox-scan-status'")
            db.execute("INSERT INTO activity_jobs(id,request_id,work_key,snapshot,history_group,history_status,history_scan_policy) VALUES (?,?,?,?,'inbox-scans','complete',1)", (status['id'], None, '', json.dumps(status)))
        if added == 0:
            if not job.get('steps'):
                # Automatic scans have no browser-owned request to replay. Their
                # only useful successful outcome is the latest check timestamp.
                db.execute('DELETE FROM activity_jobs WHERE id=?', (job['id'],))
            else:
                # Keep explicit request results/idempotency without UI repetition.
                db.execute('UPDATE activity_jobs SET history_cleared=1 WHERE id=?', (job['id'],))

    def page(self, *, group=None, before=None, snapshot=None):
        with self.activity.lock, self.activity.db() as db:
            # Automatic success rows may be compacted concurrently. Pin this
            # read snapshot until representative IDs and summaries are resolved.
            db.execute('BEGIN')
            ceiling = db.execute('SELECT COALESCE(MAX(rowid),0) FROM activity_jobs').fetchone()[0]
            snapshot = min(snapshot, ceiling) if snapshot is not None else ceiling
            before = before if before is not None else snapshot + 1
            clearable = db.execute("SELECT COUNT(*) FROM activity_jobs WHERE history_cleared=0 AND history_status NOT IN ('queued','running','waiting','stopping')").fetchone()[0]
            if group is not None:
                rows = db.execute('SELECT rowid,id FROM activity_jobs WHERE history_group=? AND history_cleared=0 AND rowid<=? AND rowid<? ORDER BY rowid DESC LIMIT ?', (group, snapshot, before, PAGE_SIZE + 1)).fetchall()
                jobs = [summary(self.activity.get(row[1])) for row in rows[:PAGE_SIZE]]
                return dict(jobs=jobs, snapshot=snapshot, next_before=rows[PAGE_SIZE - 1][0] if len(rows) > PAGE_SIZE else None)
            rows = db.execute("""SELECT history_group, MAX(rowid) AS anchor,
                SUM(history_cleared=0) AS count,
                SUM(history_cleared=0 AND history_status IN ('queued','running','waiting','stopping')) AS active,
                SUM(history_cleared=0 AND history_status IN ('partial','failed','interrupted')) AS attention,
                MAX(CASE WHEN history_cleared=0 THEN rowid END) AS latest,
                MAX(CASE WHEN history_cleared=0 AND history_status IN ('queued','running','waiting','stopping') THEN rowid END) AS working
                FROM activity_jobs WHERE rowid<=? GROUP BY history_group
                HAVING count>0 AND anchor<? ORDER BY anchor DESC LIMIT ?""", (snapshot, before, PAGE_SIZE + 1)).fetchall()
            groups = []
            for key, anchor, count, active, attention, latest, working in rows[:PAGE_SIZE]:
                latest_id = db.execute('SELECT id FROM activity_jobs WHERE rowid=?', (latest,)).fetchone()[0]
                job = self.activity.get(latest_id)
                representative = job
                if working:
                    working_id = db.execute('SELECT id FROM activity_jobs WHERE rowid=?', (working,)).fetchone()[0]
                    representative = self.activity.get(working_id)
                label = 'Inbox scans' if key == 'inbox-scans' else 'Inbox arrivals' if job['label'] == 'Scanning Inbox' and job['status'] == 'complete' and (added_tracks(job) or 0) > 0 else job['label']
                latest_summary = summary(representative)
                if representative['label'] == 'Scanning Inbox' and (added_tracks(representative) or 0) > 0:
                    latest_summary['scan_added'] = added_tracks(representative)
                groups.append(dict(key=key, label=label,
                                   scope='All Inboxes' if key == 'inbox-scans' else job['scope'], count=count,
                                   active=active, attention=attention, finished=count-active,
                                   timestamp=job.get('finished_at') or job['created_at'], latest=latest_summary))
            return dict(groups=groups, snapshot=snapshot, clearable=clearable,
                        next_before=rows[PAGE_SIZE - 1][1] if len(rows) > PAGE_SIZE else None)

    def clear(self, values):
        if not isinstance(values, dict) or set(values) - {'group', 'ids'} or ('group' in values and 'ids' in values):
            raise ValueError('Clear all, one group, or identified finished runs.')
        where, args = '', []
        if 'group' in values:
            if not isinstance(values['group'], str) or len(values['group']) > 100:
                raise ValueError('Invalid activity group.')
            where, args = ' AND history_group=?', [values['group']]
        if 'ids' in values:
            ids = values['ids']
            if not isinstance(ids, list) or not 1 <= len(ids) <= 1000 or any(not isinstance(item, str) or len(item) > 80 for item in ids):
                raise ValueError('Invalid activity IDs.')
            where, args = ' AND id IN (' + ','.join('?' for _ in ids) + ')', ids
        # One atomic statement: jobs still active at this snapshot are excluded,
        # even if they finish while this request is returning to the browser.
        with self.activity.db() as db:
            count = db.execute("UPDATE activity_jobs SET history_cleared=1 WHERE history_cleared=0 AND history_status NOT IN ('queued','running','waiting','stopping')" + where, args).rowcount
        return {'cleared': count}
