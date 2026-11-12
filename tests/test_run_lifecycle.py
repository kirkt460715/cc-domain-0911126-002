import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_catalog_calendar import county
from estate.catalog import parse, digest
from allocation.solver import solve, SearchLimit
from runs.store import Store, Conflict, StateError
from runs.worker import execute, drain, run_scenario
from runs.variants import apply_changes, difference, scale_demands, scenario_lineage
from runs.recovery import audit_store, backup, restore


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / 'plans.sqlite'
        self.store = Store(self.path)
        self.store.create('base', 'Synthetic', county())

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def sealed_run(self, key='first'):
        self.store.seal('base', 1)
        return self.store.enqueue('base', key)

    def test_draft_revision_and_seal_immutability(self):
        with self.assertRaises(StateError):
            self.store.enqueue('base', 'key')
        changed = county()
        changed['rooms'][0]['seats'] = 31
        current = self.store.replace('base', changed, 1)
        self.assertEqual(current['revision'], 2)
        with self.assertRaises(Conflict):
            self.store.replace('base', changed, 1)
        with self.assertRaises(Conflict):
            self.store.seal('base', 1)
        self.store.seal('base', 2)
        self.store.seal('base', 2)
        with self.assertRaises(StateError):
            self.store.replace('base', county(), 2)
        self.assertEqual(self.store.scenario('base')['state'], 'sealed')

    def test_request_key_is_atomic_and_bound_to_scenario(self):
        original = self.sealed_run()
        duplicate = self.store.enqueue('base', 'first')
        self.assertEqual(original['id'], duplicate['id'])
        self.store.fork('base', 'fork', 'Branch')
        self.store.seal('fork', 1)
        with self.assertRaises(Conflict):
            self.store.enqueue('fork', 'first')
        self.assertEqual(len(self.store.queue(0)), 1)

    def test_claim_expiry_fences_previous_worker(self):
        run = self.sealed_run()
        first = self.store.claim(run['id'], 'old', 10, 10)
        with self.assertRaises(Conflict):
            self.store.claim(run['id'], 'new', 19, 10)
        second = self.store.claim(run['id'], 'new', 20, 10)
        self.assertEqual(second['generation'], first['generation'] + 1)
        result = solve(parse(county()))
        with self.assertRaises(Conflict):
            self.store.complete(first, result, 21)
        with self.assertRaises(Conflict):
            self.store.fail(first, 'stale error', 21)
        completed = self.store.complete(second, result, 21)
        self.assertEqual(completed['state'], 'complete')
        self.assertEqual(completed['result_digest'], digest(result))
        self.assertIsNone(completed['owner'])
        self.assertIsNone(completed['deadline'])

    def test_heartbeat_extends_current_lease_only(self):
        run = self.sealed_run()
        token = self.store.claim(run['id'], 'worker', 10, 5)
        token = self.store.heartbeat(token, 14, 20)
        self.assertEqual(token['deadline'], 34)
        with self.assertRaises(Conflict):
            self.store.claim(run['id'], 'other', 15)
        with self.assertRaises(Conflict):
            self.store.heartbeat(token, 34)
        with self.assertRaises(ValueError):
            self.store.heartbeat(token, 20, 0)
        self.assertEqual(self.store.run(run['id'])['deadline'], 34)

    def test_failure_requires_explicit_retry(self):
        run = self.sealed_run()
        token = self.store.claim(run['id'], 'worker', 1, 100)
        failed = self.store.fail(token, 'unsatisfied quota', 2)
        self.assertEqual(failed['state'], 'failed')
        self.assertEqual(failed['error'], 'unsatisfied quota')
        with self.assertRaises(StateError):
            self.store.claim(run['id'], 'worker', 3)
        self.store.retry(run['id'])
        replacement = self.store.claim(run['id'], 'worker', 3)
        self.assertGreater(replacement['generation'], token['generation'])
        with self.assertRaises(StateError):
            self.store.retry(run['id'])

    def test_invalid_result_does_not_consume_lease(self):
        run = self.sealed_run()
        token = self.store.claim(run['id'], 'worker', 1, 100)
        result = solve(parse(county()))
        wrong = copy.deepcopy(result)
        wrong['served'] += 1
        with self.assertRaises(ValueError):
            self.store.complete(token, wrong, 2)
        self.assertEqual(self.store.run(run['id'])['state'], 'running')
        self.store.complete(token, result, 2)
        self.assertTrue(audit_store(self.store)['valid'])

    def test_publication_is_immutable_and_idempotent(self):
        run = self.sealed_run()
        with self.assertRaises(StateError):
            self.store.publish('public', run['id'])
        finished = execute(self.store, run['id'], clock=lambda: 1)
        publication = self.store.publish('public', run['id'])
        self.assertEqual(self.store.publish('public', run['id']), publication)
        self.assertEqual(publication['result_digest'], finished['result_digest'])
        second = self.store.enqueue('base', 'second')
        execute(self.store, second['id'], clock=lambda: 1)
        with self.assertRaises(Conflict):
            self.store.publish('public', second['id'])
        self.assertEqual(self.store.publication('public')['run'], run['id'])

    def test_worker_search_failure_is_recorded_and_retryable(self):
        run = self.sealed_run()
        with self.assertRaises(SearchLimit):
            execute(self.store, run['id'], node_limit=1, clock=lambda: 10)
        self.assertEqual(self.store.run(run['id'])['state'], 'failed')
        self.assertIn('SearchLimit', self.store.run(run['id'])['error'])
        self.store.retry(run['id'])
        outcome = drain(self.store, clock=lambda: 20)
        self.assertEqual(outcome[0]['state'], 'complete')
        self.assertEqual(drain(self.store, clock=lambda: 30), [])

    def test_worker_duplicate_key_returns_original_completed_run(self):
        self.store.seal('base', 1)
        first = run_scenario(self.store, 'base', 'stable')
        second = run_scenario(self.store, 'base', 'stable')
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['generation'], second['generation'])
        self.assertEqual(first['result_digest'], second['result_digest'])

    def test_two_connections_race_for_same_request_key(self):
        self.store.seal('base', 1)
        def enqueue(_):
            with Store(self.path) as connection:
                return connection.enqueue('base', 'concurrent')['id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(enqueue, range(8)))
        self.assertEqual(len(set(identifiers)), 1)
        self.assertEqual(len(self.store.queue(0)), 1)
        self.assertEqual(sum(r['kind'] == 'run_queued' for r in self.store.history()), 1)

    def test_claim_race_has_one_winner(self):
        run = self.sealed_run()
        def claim(owner):
            with Store(self.path) as connection:
                try:
                    token = connection.claim(run['id'], owner, 1)
                except Conflict:
                    return None
                return token['owner']
        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(pool.map(claim, ['one', 'two']))
        self.assertEqual(sum(owner is not None for owner in winners), 1)
        self.assertIn(self.store.run(run['id'])['owner'], winners)

    def test_fork_and_lineage_leave_parent_unchanged(self):
        with self.assertRaises(StateError):
            self.store.fork('base', 'branch', 'Branch')
        self.store.seal('base', 1)
        branch = self.store.fork('base', 'branch', 'Branch')
        changed = apply_changes(branch['body'], [{'table': 'rooms', 'id': 'flex', 'field': 'seats', 'value': 40}])
        self.store.replace('branch', changed, 1)
        self.assertEqual(self.store.scenario('base')['body']['rooms'][0]['seats'], 30)
        self.assertEqual([row['id'] for row in scenario_lineage(self.store, 'branch')], ['base', 'branch'])
        delta = difference(self.store.scenario('base')['body'], changed)
        self.assertEqual(delta[0]['field'], 'seats')
        self.assertEqual(delta[0]['after'], 40)

    def test_scaling_conserves_period_rounded_totals(self):
        document = county()
        for row in document['demands']:
            row['minimum'] = 0
        scaled, rounding = scale_demands(document, 1, 3)
        self.assertEqual(sum(r['students'] for r in scaled['demands']), 30)
        self.assertEqual(sum(r['after'] for r in rounding), 30)
        self.assertEqual(sum(r['students'] for r in document['demands']), 90)
        with self.assertRaises(ValueError):
            scale_demands(county(), 0, 1)
        with self.assertRaises(ValueError):
            scale_demands(document, 1, 0)
        duplicate = [{'table': 'rooms', 'id': 'flex', 'field': 'seats', 'value': 40}] * 2
        with self.assertRaisesRegex(ValueError, 'duplicate change'):
            apply_changes(document, duplicate)

    def test_transaction_rolls_back_events_on_conflict(self):
        before = self.store.history()
        with self.assertRaises(Conflict):
            self.store.create('base', 'Duplicate', county())
        self.assertEqual(before, self.store.history())
        with self.assertRaises(Conflict):
            self.store.replace('base', county(), 999)
        self.assertEqual(before, self.store.history())
        self.assertEqual(self.store.scenario('base')['revision'], 1)

    def test_store_detects_content_tampering(self):
        self.store.db.execute("UPDATE scenarios SET body=? WHERE id='base'", (json.dumps({**county(), 'version': 2}),))
        with self.assertRaisesRegex(StateError, 'integrity'):
            self.store.scenario('base')
        with self.assertRaises(StateError):
            audit_store(self.store)

    def test_backup_restore_validates_full_business_state(self):
        self.store.seal('base', 1)
        run = run_scenario(self.store, 'base', 'backup-key')
        self.store.publish('backup-public', run['id'])
        saved = backup(self.store, self.root / 'backup.sqlite')
        restored_path = self.root / 'restore.sqlite'
        outcome = restore(saved['path'], restored_path, saved['sha256'])
        self.assertTrue(outcome['summary']['valid'])
        with Store(restored_path) as restored:
            self.assertEqual(restored.publication('backup-public')['result'], run['result'])
            self.assertEqual(audit_store(restored)['publications'], 1)
        with self.assertRaises(FileExistsError):
            restore(saved['path'], restored_path, saved['sha256'])
        with self.assertRaises(ValueError):
            restore(saved['path'], self.root / 'bad.sqlite', 'wrong')
        self.assertFalse((self.root / 'bad.sqlite').exists())
        with self.assertRaises(FileExistsError):
            backup(self.store, self.root / 'backup.sqlite')

    def test_history_cursor_is_monotone_and_bounded(self):
        self.sealed_run()
        first = self.store.history(limit=1)
        self.assertEqual(len(first), 1)
        second = self.store.history(after=first[0]['sequence'])
        self.assertTrue(all(r['sequence'] > first[0]['sequence'] for r in second))
        self.assertEqual(len(first + second), len(self.store.history()))
        with self.assertRaises(ValueError):
            self.store.history(limit=0)

    def test_completed_result_checksum_protects_publication_and_backup(self):
        self.store.seal('base', 1)
        run = run_scenario(self.store, 'base', 'protected-result')
        self.store.publish('protected', run['id'])
        changed = copy.deepcopy(run['result'])
        changed['served'] -= 1
        self.store.db.execute('UPDATE runs SET result=? WHERE id=?', (json.dumps(changed), run['id']))
        with self.assertRaisesRegex(StateError, 'result integrity'):
            self.store.run(run['id'])
        with self.assertRaisesRegex(StateError, 'result integrity'):
            self.store.publication('protected')
        with self.assertRaises(StateError):
            backup(self.store, self.root / 'invalid-backup.sqlite')
        self.assertFalse((self.root / 'invalid-backup.sqlite').exists())

    def test_expired_queue_can_be_resumed_without_a_new_run(self):
        run = self.sealed_run()
        self.store.claim(run['id'], 'crashed-worker', 10, 5)
        self.assertEqual(self.store.queue(14), [])
        eligible = self.store.queue(15)
        self.assertEqual([row['id'] for row in eligible], [run['id']])
        outcome = drain(self.store, clock=lambda: 15)
        self.assertEqual(outcome[0]['id'], run['id'])
        self.assertEqual(outcome[0]['state'], 'complete')
        self.assertEqual(self.store.run(run['id'])['generation'], 2)
        self.assertEqual(self.store.queue(16), [])
