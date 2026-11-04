"""Paged presentation of durable activity; clearing never removes execution evidence."""
import hashlib
import json
import re

ACTIVE = ('queued', 'running', 'waiting', 'stopping')
ATTENTION = ('partial', 'failed', 'interrupted')
PAGE_SIZE = 20


def group_key(job):
    if job['label'] == 'Scanning Inbox':
        return 'inbox-scans'
    operations = sorted({(step.get('method', 'POST'), re.sub(r'/[0-9]+(?=/|$)', '/:id', step['path'])) for step in job.get('steps', [])})
    return hashlib.sha256(json.dumps([job['label'], job['href'], operations]).encode()).hexdigest()


def summary(job):
    return {key: value for key, value in job.items() if key not in {'rows', 'steps', 'result'}} | {'rows': []}


class History:
    def __init__(self, activity):
        self.activity = activity
        with activity.db() as db:
            columns = {row[1] for row in db.execute('PRAGMA table_info(activity_jobs)')}
            for column, definition in [('history_group', 'TEXT'), ('history_status', 'TEXT'), ('history_cleared', 'INTEGER NOT NULL DEFAULT 0')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE activity_jobs ADD COLUMN {column} {definition}')
            for job_id, snapshot in db.execute('SELECT id,snapshot FROM activity_jobs WHERE history_group IS NULL OR history_status IS NULL'):
                job = json.loads(snapshot)
                db.execute('UPDATE activity_jobs SET history_group=?,history_status=? WHERE id=?', (group_key(job), job['status'], job_id))
            db.execute('CREATE INDEX IF NOT EXISTS activity_history_groups ON activity_jobs(history_group,history_cleared)')
            db.execute('CREATE INDEX IF NOT EXISTS activity_history_status ON activity_jobs(history_status,history_cleared)')
            # Restart interruption is resolved by Activity before history is opened.
            db.execute("UPDATE activity_jobs SET history_status=json_extract(snapshot,'$.status') WHERE history_status IN ('queued','running','waiting','stopping')")

    def page(self, *, group=None, before=None, snapshot=None):
        with self.activity.db() as db:
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
                groups.append(dict(key=key, label='Inbox scans' if key == 'inbox-scans' else job['label'],
                                   scope='All Inboxes' if key == 'inbox-scans' else job['scope'], count=count,
                                   active=active, attention=attention, finished=count-active,
                                   timestamp=job.get('finished_at') or job['created_at'], latest=summary(representative)))
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
