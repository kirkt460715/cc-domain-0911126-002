from contextlib import contextmanager
from datetime import datetime, timezone
import copy
import json
import sqlite3
import uuid
from estate.catalog import canonical, digest, parse, InvalidPlan
from allocation.verify import require_valid_result


class Conflict(ValueError):
    pass


class StateError(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS scenarios(
            id TEXT PRIMARY KEY,
            parent TEXT REFERENCES scenarios(id),
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            digest TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1,
            state TEXT NOT NULL CHECK(state IN ('draft','sealed')),
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs(
            id TEXT PRIMARY KEY,
            scenario TEXT NOT NULL REFERENCES scenarios(id),
            request_key TEXT NOT NULL UNIQUE,
            input_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('queued','running','complete','failed')),
            generation INTEGER NOT NULL DEFAULT 0,
            owner TEXT,
            deadline INTEGER,
            result TEXT,
            result_digest TEXT,
            error TEXT,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS publications(
            label TEXT PRIMARY KEY,
            run TEXT NOT NULL REFERENCES runs(id),
            result_digest TEXT NOT NULL,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS changes(
            sequence INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            entity TEXT NOT NULL,
            details TEXT NOT NULL,
            created TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS runs_queue ON runs(state,created,id);
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('COMMIT')

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def _change(self, kind, entity, details):
        self.db.execute('INSERT INTO changes(kind,entity,details,created) VALUES(?,?,?,?)',
                        (kind, entity, canonical(details), self._now()))

    def create(self, ident, title, document, parent=None):
        from estate.catalog import text
        text(ident, 'id')
        text(title, 'title')
        catalog = parse(document)
        with self.transaction():
            if self.db.execute('SELECT 1 FROM scenarios WHERE id=?', (ident,)).fetchone():
                raise Conflict('scenario already exists')
            if parent is not None:
                row = self.db.execute('SELECT state FROM scenarios WHERE id=?', (parent,)).fetchone()
                if row is None or row['state'] != 'sealed':
                    raise StateError('parent must exist and be sealed')
            self.db.execute('INSERT INTO scenarios(id,parent,title,body,digest,state,created) VALUES(?,?,?,?,?,?,?)',
                            (ident, parent, title, canonical(catalog.document), catalog.fingerprint, 'draft', self._now()))
            self._change('scenario_created', ident, {'parent': parent, 'digest': catalog.fingerprint})
        return self.scenario(ident)

    def scenario(self, ident):
        row = self.db.execute('SELECT * FROM scenarios WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        result = dict(row)
        result['body'] = json.loads(result['body'])
        if digest(result['body']) != result['digest']:
            raise StateError('scenario integrity failure')
        return result

    def replace(self, ident, document, expected_revision):
        catalog = parse(document)
        with self.transaction():
            scenario = self.scenario(ident)
            if scenario['state'] != 'draft':
                raise StateError('sealed scenarios are immutable')
            if scenario['revision'] != expected_revision:
                raise Conflict('stale scenario revision')
            revision = expected_revision + 1
            self.db.execute('UPDATE scenarios SET body=?,digest=?,revision=? WHERE id=?',
                            (canonical(catalog.document), catalog.fingerprint, revision, ident))
            self._change('scenario_replaced', ident, {'revision': revision, 'digest': catalog.fingerprint})
        return self.scenario(ident)

    def seal(self, ident, expected_revision):
        from estate.calendar import assert_independent_periods
        with self.transaction():
            scenario = self.scenario(ident)
            if scenario['revision'] != expected_revision:
                raise Conflict('stale scenario revision')
            assert_independent_periods(parse(scenario['body']))
            if scenario['state'] == 'draft':
                self.db.execute("UPDATE scenarios SET state='sealed' WHERE id=?", (ident,))
                self._change('scenario_sealed', ident, {'revision': expected_revision})
        return self.scenario(ident)

    def fork(self, parent, ident, title):
        scenario = self.scenario(parent)
        return self.create(ident, title, scenario['body'], parent)

    def enqueue(self, scenario_id, request_key):
        from estate.catalog import text
        text(request_key, 'request_key')
        with self.transaction():
            scenario = self.scenario(scenario_id)
            if scenario['state'] != 'sealed':
                raise StateError('only sealed scenarios may run')
            existing = self.db.execute('SELECT * FROM runs WHERE request_key=?', (request_key,)).fetchone()
            if existing:
                if existing['scenario'] != scenario_id or existing['input_digest'] != scenario['digest']:
                    raise Conflict('request key reused for another scenario')
                ident = existing['id']
            else:
                ident = uuid.uuid4().hex
                self.db.execute('INSERT INTO runs(id,scenario,request_key,input_digest,state,created) VALUES(?,?,?,?,?,?)',
                                (ident, scenario_id, request_key, scenario['digest'], 'queued', self._now()))
                self._change('run_queued', ident, {'scenario': scenario_id})
        return self.run(ident)

    def run(self, ident):
        row = self.db.execute('SELECT * FROM runs WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        value = dict(row)
        if value['result'] is not None:
            value['result'] = json.loads(value['result'])
            if digest(value['result']) != value['result_digest']:
                raise StateError('result integrity failure')
        return value

    def claim(self, ident, owner, now, duration=60):
        from estate.catalog import integer, text
        text(owner, 'owner')
        integer(now, 'now', 0, 10**15)
        integer(duration, 'duration', 1, 3600)
        with self.transaction():
            run = self.run(ident)
            if run['state'] == 'running' and run['deadline'] > now:
                raise Conflict('run already leased')
            if run['state'] not in ('queued', 'running'):
                raise StateError('run is not claimable')
            generation = run['generation'] + 1
            self.db.execute("UPDATE runs SET state='running',owner=?,deadline=?,generation=? WHERE id=?",
                            (owner, now + duration, generation, ident))
            self._change('run_claimed', ident, {'owner': owner, 'generation': generation, 'deadline': now + duration})
        return {'id': ident, 'owner': owner, 'generation': generation, 'deadline': now + duration}

    def _lease(self, token, now):
        run = self.run(token['id'])
        if run['state'] != 'running':
            raise StateError('run is not running')
        if run['owner'] != token['owner'] or run['generation'] != token['generation']:
            raise Conflict('stale worker fencing token')
        if run['deadline'] <= now:
            raise Conflict('lease expired')
        return run

    def heartbeat(self, token, now, duration=60):
        from estate.catalog import integer
        integer(now, 'now', 0, 10**15)
        integer(duration, 'duration', 1, 3600)
        with self.transaction():
            self._lease(token, now)
            self.db.execute('UPDATE runs SET deadline=? WHERE id=?', (now + duration, token['id']))
        return {**token, 'deadline': now + duration}

    def complete(self, token, result, now):
        with self.transaction():
            run = self._lease(token, now)
            scenario = self.scenario(run['scenario'])
            require_valid_result(parse(scenario['body']), result)
            from allocation.certificate import certificate
            certificate(parse(scenario['body']), result)
            self.db.execute("UPDATE runs SET state='complete',result=?,result_digest=?,owner=NULL,deadline=NULL,error=NULL WHERE id=?",
                            (canonical(result), digest(result), run['id']))
            self._change('run_completed', run['id'], {'digest': digest(result)})
        return self.run(token['id'])

    def fail(self, token, error, now):
        if not isinstance(error, str) or not error.strip():
            raise ValueError('failure must have a reason')
        with self.transaction():
            run = self._lease(token, now)
            self.db.execute("UPDATE runs SET state='failed',error=?,owner=NULL,deadline=NULL WHERE id=?",
                            (error[:2000], run['id']))
            self._change('run_failed', run['id'], {'error': error[:2000]})
        return self.run(token['id'])

    def retry(self, ident):
        with self.transaction():
            run = self.run(ident)
            if run['state'] != 'failed':
                raise StateError('only failed runs may be retried')
            self.db.execute("UPDATE runs SET state='queued',error=NULL WHERE id=?", (ident,))
            self._change('run_retried', ident, {})
        return self.run(ident)

    def publish(self, label, ident):
        from estate.catalog import text
        text(label, 'label')
        with self.transaction():
            run = self.run(ident)
            if run['state'] != 'complete':
                raise StateError('run must be complete')
            row = self.db.execute('SELECT * FROM publications WHERE label=?', (label,)).fetchone()
            if row:
                if row['run'] != ident or row['result_digest'] != run['result_digest']:
                    raise Conflict('publication label is immutable')
            else:
                self.db.execute('INSERT INTO publications VALUES(?,?,?,?)', (label, ident, run['result_digest'], self._now()))
                self._change('published', ident, {'label': label, 'digest': run['result_digest']})
        return self.publication(label)

    def publication(self, label):
        row = self.db.execute('SELECT * FROM publications WHERE label=?', (label,)).fetchone()
        if row is None:
            raise KeyError(label)
        run = self.run(row['run'])
        if run['state'] != 'complete' or run['result_digest'] != row['result_digest']:
            raise StateError('publication integrity failure')
        return {**dict(row), 'result': run['result'], 'scenario': run['scenario']}

    def scenarios(self, state=None):
        if state is not None and state not in ('draft', 'sealed'):
            raise ValueError('unknown scenario state')
        query = 'SELECT id FROM scenarios'
        args = ()
        if state:
            query += ' WHERE state=?'
            args = (state,)
        query += ' ORDER BY created,id'
        return [self.scenario(row['id']) for row in self.db.execute(query, args)]

    def queue(self, now):
        return [dict(row) for row in self.db.execute(
            "SELECT id,scenario,state,generation FROM runs WHERE state='queued' OR (state='running' AND deadline<=?) ORDER BY created,id", (now,))]

    def history(self, after=0, limit=100):
        from estate.catalog import integer
        integer(after, 'after', 0, 10**15)
        integer(limit, 'limit', 1, 1000)
        result = []
        for row in self.db.execute('SELECT * FROM changes WHERE sequence>? ORDER BY sequence LIMIT ?', (after, limit)):
            value = dict(row)
            value['details'] = json.loads(value['details'])
            result.append(value)
        return result
