import hashlib
import json
from pathlib import Path
import sqlite3
from estate.catalog import canonical, parse
from allocation.verify import require_valid_result
from runs.store import Store, StateError


def audit_store(store):
    integrity = store.db.execute('PRAGMA integrity_check').fetchall()
    if [row[0] for row in integrity] != ['ok']:
        raise StateError('SQLite integrity check failed')
    violations = store.db.execute('PRAGMA foreign_key_check').fetchall()
    if violations:
        raise StateError('SQLite foreign key check failed')
    scenarios = store.scenarios()
    scenario_ids = {row['id'] for row in scenarios}
    for scenario in scenarios:
        parse(scenario['body'])
        seen = set()
        current = scenario
        while current['parent']:
            if current['id'] in seen:
                raise StateError('scenario lineage cycle')
            seen.add(current['id'])
            current = store.scenario(current['parent'])
            if current['state'] != 'sealed':
                raise StateError('lineage parent is not sealed')
    runs = [store.run(row['id']) for row in store.db.execute('SELECT id FROM runs')]
    for run in runs:
        scenario = store.scenario(run['scenario'])
        if scenario['state'] != 'sealed' or scenario['digest'] != run['input_digest']:
            raise StateError('run input does not match sealed scenario')
        if run['state'] == 'complete':
            if run['result'] is None:
                raise StateError('complete run has no result')
            require_valid_result(parse(scenario['body']), run['result'])
        elif run['result'] is not None:
            raise StateError('incomplete run contains result')
        if run['state'] == 'running':
            if not run['owner'] or run['deadline'] is None or run['generation'] < 1:
                raise StateError('running run has incomplete lease')
        elif run['owner'] is not None or run['deadline'] is not None:
            raise StateError('inactive run retains a lease')
    publications = [store.publication(row['label']) for row in store.db.execute('SELECT label FROM publications')]
    return {'scenarios': len(scenarios), 'runs': len(runs), 'publications': len(publications), 'valid': True}


def backup(store, destination):
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=False)
    connection = None
    try:
        connection = sqlite3.connect(str(target))
        store.db.backup(connection)
        connection.close()
        connection = None
        with Store(target) as check:
            summary = audit_store(check)
    except BaseException:
        if connection:
            connection.close()
        target.unlink(missing_ok=True)
        raise
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    return {'path': str(target), 'sha256': checksum, 'summary': summary}


def restore(source, destination, expected_digest):
    origin = Path(source)
    if origin.is_symlink() or not origin.is_file():
        raise ValueError('backup must be a regular file')
    actual = hashlib.sha256(origin.read_bytes()).hexdigest()
    if expected_digest != actual:
        raise ValueError('backup checksum mismatch')
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    connection = sqlite3.connect(origin.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('invalid backup database')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
        output = sqlite3.connect(str(target))
        try:
            connection.backup(output)
        finally:
            output.close()
        with Store(target) as store:
            summary = audit_store(store)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
    return {'path': str(target), 'summary': summary}
