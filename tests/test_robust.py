import hashlib
import json
import tempfile
import unittest
from itertools import combinations
from pathlib import Path

from estate.catalog import canonical, digest, parse
from allocation.solver import assess, solve, InfeasibleMinimum
from allocation.verify import require_valid_result
from runs.variants import apply_changes
from estate_cli import main, parser, dispatch
from assessments.robust import (
    robust_decision,
    matrix_rows,
    save_decision,
    verify_decision_bundle,
    load_decision,
    export_decision,
)


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/county.json'
CASES = Path(__file__).resolve().parents[1] / 'fixtures/planning_cases.json'


def county():
    return json.loads(FIXTURE.read_text())


def planning_cases():
    return json.loads(CASES.read_text())


def oracle(document, cases):
    """Independent exhaustive decision: best per-scenario service then fixed portfolios."""
    base = parse(document)
    proposals = {item['id']: item for item in cases['proposals']}
    ids = sorted(proposals)
    budget = cases['budget']
    scenarios = [scenario['id'] for scenario in cases['scenarios']]
    scenario_documents = {
        scenario['id']: apply_changes(document, scenario['changes'])
        for scenario in cases['scenarios']
    }

    def apply(document_value, projects):
        changes = [change for ident in projects for change in proposals[ident]['changes']]
        return parse(apply_changes(document_value, changes))

    legal = []
    for count in range(len(ids) + 1):
        for combo in combinations(ids, count):
            chosen = set(combo)
            cost = sum(proposals[ident]['cost'] for ident in combo)
            if cost > budget:
                continue
            valid = True
            for ident in combo:
                if not set(proposals[ident].get('requires', [])) <= chosen:
                    valid = False
                if set(proposals[ident].get('excludes', [])) & chosen:
                    valid = False
            if valid:
                legal.append((combo, cost))

    served = {}
    costs = {}
    infeasible = {}
    for scenario in scenarios:
        for combo, _ in legal:
            try:
                result = solve(apply(scenario_documents[scenario], combo))
            except InfeasibleMinimum as exc:
                infeasible[(combo, scenario)] = exc.shortfalls
                continue
            served[(combo, scenario)] = result['served']
            costs[(combo, scenario)] = result['cost']

    benchmarks = {}
    for scenario in scenarios:
        values = [served[(combo, scenario)] for combo, _ in legal if (combo, scenario) in served]
        benchmarks[scenario] = {
            'scenario': scenario,
            'served': max(values) if values else None,
            'witnesses': sorted(
                '+'.join(combo) if combo else 'baseline'
                for combo, _ in legal
                if (combo, scenario) in served and served[(combo, scenario)] == max(values)
            ) if values else [],
            'infeasible_portfolios': sorted(
                '+'.join(combo) if combo else 'baseline'
                for combo, _ in legal if (combo, scenario) in infeasible
            ),
        }

    rows = []
    for combo, cost in legal:
        if not all((combo, scenario) in served for scenario in scenarios):
            continue
        regrets = [benchmarks[scenario]['served'] - served[(combo, scenario)] for scenario in scenarios]
        rows.append({
            'portfolio': '+'.join(combo) if combo else 'baseline',
            'projects': list(combo),
            'investment': cost,
            'served_min': min(served[(combo, scenario)] for scenario in scenarios),
            'max_regret': max(regrets),
            'allocation_cost_sum': sum(costs[(combo, scenario)] for scenario in scenarios),
            'worst_regret_scenario': scenarios[regrets.index(max(regrets))],
        })
    rows.sort(key=lambda row: (row['max_regret'], -row['served_min'], row['investment'],
                              row['allocation_cost_sum'], row['projects']))
    return {
        'legal': [(list(combo), cost) for combo, cost in legal],
        'served': served,
        'costs': costs,
        'infeasible': infeasible,
        'benchmarks': benchmarks,
        'ranking': rows,
        'selected': rows[0] if rows else None,
    }


class RobustDecisionTests(unittest.TestCase):
    def setUp(self):
        self.document = county()
        self.cases = planning_cases()
        self.expected = oracle(self.document, self.cases)
        self.decision = robust_decision(self.document, self.cases)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_selected_portfolio_matches_independent_exhaustive_oracle(self):
        expected = self.expected
        self.assertEqual(self.decision['state'], 'optimal')
        self.assertTrue(self.decision['exact'])
        self.assertEqual(len(self.decision['portfolios']), len(expected['legal']))
        self.assertEqual(self.decision['decision']['selected'], expected['selected']['portfolio'])
        self.assertEqual(self.decision['decision']['projects'], expected['selected']['projects'])
        metrics = self.decision['decision']['metrics']
        self.assertEqual(metrics['max_regret'], expected['selected']['max_regret'])
        self.assertEqual(metrics['served_min'], expected['selected']['served_min'])
        self.assertEqual(metrics['allocation_cost_sum'], expected['selected']['allocation_cost_sum'])
        self.assertEqual(
            [row['portfolio'] for row in expected['ranking']],
            [row['portfolio'] for row in self.decision['decision']['candidates']],
        )

    def test_regret_matrix_and_benchmarks_reproduce_oracle(self):
        matrix = {(row['portfolio'], row['scenario']): row for row in self.decision['matrix']}
        found = {row['scenario']: row for row in self.decision['benchmarks']}
        for scenario_id, benchmark in self.expected['benchmarks'].items():
            self.assertEqual(found[scenario_id]['served'], benchmark['served'])
            self.assertTrue(found[scenario_id]['exact'])
            self.assertEqual(sorted(found[scenario_id]['witnesses']), sorted(benchmark['witnesses']))
        for (portfolio, scenario_id), served in self.expected['served'].items():
            name = '+'.join(portfolio) if portfolio else 'baseline'
            cell = matrix[(name, scenario_id)]
            self.assertEqual(cell['served'], served)
            self.assertEqual(cell['allocation_cost'], self.expected['costs'][(portfolio, scenario_id)])
            expected_regret = self.expected['benchmarks'][scenario_id]['served'] - served
            self.assertEqual(cell['regret'], expected_regret)
            self.assertEqual(cell['regret_lower'], expected_regret)
            self.assertEqual(cell['regret_upper'], expected_regret)
            self.assertEqual(cell['worst_regret_scenario'], matrix[(name, scenario_id)]['worst_regret_scenario'])
        matrix_size = len(self.decision['portfolios']) * len(self.cases['scenarios'])
        self.assertEqual(len(self.decision['matrix']), matrix_size)
        for portfolio, scenario_id in self.expected['infeasible']:
            name = '+'.join(portfolio) if portfolio else 'baseline'
            self.assertEqual(matrix[(name, scenario_id)]['status'], 'infeasible_minimum')

    def test_single_scenario_optimum_loses_in_another_scenario(self):
        by_name = {}
        for row in self.decision['matrix']:
            by_name.setdefault(row['portfolio'], {})[row['scenario']] = row
        # expand-village is the unique best single project under village-growth,
        # but it buys nothing when the rural building is closed.  The combined
        # portfolio alone reaches the benchmark in all three scenarios.
        self.assertEqual(by_name['expand-village']['village-growth']['served'], 85)
        self.assertEqual(by_name['expand-village']['reference']['served'], 80)
        self.assertEqual(by_name['expand-village']['rural-closure']['served'], 50)
        self.assertEqual(by_name['expand-village']['rural-closure']['regret'], 5)
        self.assertEqual(by_name['expand-shared']['rural-closure']['served'], 55)
        self.assertEqual(by_name['expand-shared']['rural-closure']['regret'], 0)
        self.assertGreater(by_name['expand-village']['reference']['regret'], 0)
        for scenario_id in ('reference', 'village-growth', 'rural-closure'):
            self.assertEqual(by_name['expand-shared+expand-village'][scenario_id]['regret'], 0)
        self.assertEqual(self.decision['decision']['selected'], 'expand-shared+expand-village')

    def test_dependency_and_exclusion_prune_illegal_portfolios(self):
        all_names = {tuple(row['projects']) for row in self.decision['portfolios']}
        self.assertNotIn(('town-secondary-lab',), all_names)
        self.assertNotIn(('close-rural', 'expand-village'), all_names)
        self.assertNotIn(('close-rural', 'expand-village', 'expand-shared'), all_names)
        self.assertIn(('expand-shared', 'town-secondary-lab'), all_names)
        expected = {tuple(row['projects']) for row in self.expected['ranking']}
        self.assertEqual(len(all_names), len(self.expected['legal']))
        for row in self.decision['portfolios']:
            chosen = set(row['projects'])
            for ident in row['projects']:
                project = next(item for item in self.cases['proposals'] if item['id'] == ident)
                self.assertTrue(set(project.get('requires', [])) <= chosen)
                self.assertFalse(set(project.get('excludes', [])) & chosen)
        self.assertEqual(self.decision['enumeration']['legal_portfolios'], len(self.expected['legal']))
        self.assertTrue(self.decision['enumeration']['complete'])

    def test_guarantee_failure_is_not_reported_as_zero_service(self):
        data = county()
        for row in data['demands']:
            row['minimum'] = 0
        data['demands'][1]['minimum'] = 10
        cases = {
            'budget': 5,
            'proposals': [
                {'id': 'close-shared', 'cost': 1, 'changes': [
                    {'table': 'buildings', 'id': 'shared', 'field': 'seat_limit', 'value': 0}
                ]},
                {'id': 'keep', 'cost': 0, 'changes': []},
            ],
            'scenarios': [
                {'id': 'reference', 'changes': []},
                {'id': 'village-growth', 'changes': [
                    {'table': 'demands', 'id': 'village-primary', 'field': 'students', 'value': 40}
                ]},
            ],
        }
        decision = robust_decision(data, cases)
        self.assertEqual(decision['state'], 'optimal')
        failure = next(item for item in decision['infeasible_guarantees']
                       if item['portfolio'] == 'close-shared' and item['scenario'] == 'reference')
        self.assertEqual(failure['shortfalls'], {'town-secondary': 10})
        self.assertEqual(failure['missing_total'], 10)
        close_row = next(row for row in decision['portfolios'] if row['name'] == 'close-shared')
        self.assertEqual(close_row['cell_states']['reference'], 'infeasible_minimum')
        self.assertIsNone(close_row['metrics'])
        # 'keep' has no changes and ties baseline, so the project-ID tie-break
        # prefers the empty portfolio.
        self.assertEqual(decision['decision']['selected'], 'baseline')
        self.assertEqual(decision['decision']['scenario_gaps'], [])

    def test_no_robust_portfolio_reports_trigger_scenario_and_gap(self):
        data = county()
        data['demands'][1]['minimum'] = 10
        cases = {
            'budget': 5,
            'proposals': [
                {'id': 'close-shared', 'cost': 5, 'changes': [
                    {'table': 'buildings', 'id': 'shared', 'field': 'seat_limit', 'value': 0}
                ]},
            ],
            'scenarios': [
                {'id': 'reference', 'changes': [
                    {'table': 'buildings', 'id': 'shared', 'field': 'closures', 'value': [
                        {'start': '2026-09-01', 'end': '2027-01-01', 'reason': 'temporary-closure'}
                    ]}
                ]},
                {'id': 'village-growth', 'changes': [
                    {'table': 'demands', 'id': 'village-primary', 'field': 'students', 'value': 40}
                ]},
            ],
        }
        decision = robust_decision(data, cases)
        self.assertEqual(decision['state'], 'no_robust_portfolio')
        self.assertTrue(decision['infeasible_guarantees'])
        self.assertEqual(decision['decision']['trigger_scenarios'], ['reference'])
        gap = next(item for item in decision['decision']['scenario_gaps'] if item['scenario'] == 'reference')
        self.assertEqual(gap['state'], 'no_feasible_portfolio')
        self.assertTrue(gap['guarantee_gaps'])
        self.assertEqual(gap['guarantee_gaps'][0]['missing_total'], 20)

    def test_data_conflict_is_separate_from_guarantee_and_search(self):
        data = county()
        cases = {
            'budget': 10,
            'proposals': [
                {'id': 'invalid-capacity', 'cost': 1, 'changes': [
                    {'table': 'buildings', 'id': 'rural-school', 'field': 'seat_limit', 'value': 999999999}
                ]},
                {'id': 'keep', 'cost': 0, 'changes': []},
            ],
            'scenarios': [{'id': 'reference', 'changes': []}],
        }
        decision = robust_decision(data, cases)
        conflict = next(item for item in decision['data_conflicts'] if item['portfolio'] == 'invalid-capacity')
        self.assertIn('seat_limit', conflict['error'])
        self.assertEqual(decision['state'], 'optimal')
        # A robust portfolio ('keep') remains, so the conflict is catalogued
        # but does not become a trigger scenario.
        self.assertEqual(decision['decision']['trigger_scenarios'], [])
        self.assertEqual(decision['incomplete_cells'], [])
        self.assertFalse(any(item['portfolio'] == 'invalid-capacity' for item in decision['infeasible_guarantees']))
        self.assertIsNone(
            next(row for row in decision['portfolios'] if row['name'] == 'invalid-capacity')['metrics']
        )

    def test_truncated_search_returns_candidates_and_bounds_not_optimality(self):
        decision = robust_decision(self.document, self.cases, node_limit=5)
        self.assertEqual(decision['state'], 'provisional')
        self.assertFalse(decision['exact'])
        # A truncated search may name a leading candidate but never a selection.
        self.assertIsNone(decision['decision']['selected'])
        self.assertEqual(
            decision['decision']['leading_candidate']['portfolio'],
            'close-rural+expand-shared',
        )
        self.assertIn('not a proved optimum', decision['decision']['leading_candidate']['note'])
        self.assertEqual(decision['decision']['rank_key'], None)
        for benchmark in decision['benchmarks']:
            if benchmark['scenario'] == 'rural-closure':
                self.assertEqual(benchmark['state'], 'exact')
            else:
                self.assertEqual(benchmark['state'], 'bounded')
                self.assertIsNone(benchmark['served'])
                self.assertIsNotNone(benchmark['upper'])
        supported = [row for row in decision['portfolios'] if row['metrics']]
        self.assertTrue(supported)
        for row in supported:
            self.assertFalse(row['metrics']['exact'])
            self.assertIsNotNone(row['metrics']['max_regret_upper'])
            self.assertGreaterEqual(row['metrics']['max_regret_upper'], row['metrics']['max_regret_lower'])
        self.assertTrue(decision['incomplete_cells'])
        for row in decision['incomplete_cells']:
            self.assertIsNotNone(row['served_upper'])
            self.assertGreaterEqual(row['served_upper'], row['served_lower'])
        self.assertTrue(all(row['status'] in ('incomplete', 'undetermined') for row in decision['incomplete_cells']))
        many = robust_decision(self.document, self.cases, max_cases=3)
        self.assertEqual(many['state'], 'provisional')
        self.assertFalse(many['enumeration']['complete'])
        self.assertEqual(many['enumeration']['evaluated'], 3)
        for benchmark in many['benchmarks']:
            self.assertEqual(benchmark['state'], 'partial_enumeration')
            self.assertIsNone(benchmark['served'])
        with self.assertRaises(ValueError):
            robust_decision(self.document, self.cases, node_limit=0)
        with self.assertRaises(ValueError):
            robust_decision(self.document, self.cases, max_cases=0)

    def test_search_never_claims_optimality_when_undetermined(self):
        decision = robust_decision(self.document, self.cases, node_limit=4)
        self.assertEqual(decision['state'], 'undetermined')
        self.assertIsNone(decision['decision']['selected'])
        self.assertEqual(decision['decision']['candidates'][:1], [])
        for benchmark in decision['benchmarks']:
            self.assertEqual(benchmark['state'], 'unresolved')
            self.assertIsNone(benchmark['served'])
            self.assertEqual(benchmark['witnesses'], [])

    def test_exported_bundle_verifies_allocations_and_recomputes(self):
        saved = save_decision(self.document, self.cases, self.decision, self.root / 'decision')
        self.assertTrue(saved['verified'])
        verified = verify_decision_bundle(self.root / 'decision')
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['selected'], 'expand-shared+expand-village')
        export = export_decision(self.root / 'decision', self.root / 'export')
        self.assertTrue(export['verified'])
        self.assertEqual(export['matrix_rows'], len(self.decision['matrix']))
        self.assertEqual(export['benchmark_rows'], len(self.decision['benchmarks']))
        loaded = load_decision(self.root / 'decision', recompute=False)
        self.assertEqual(loaded['decision']['decision_digest'], self.decision['decision_digest'])
        for item in self.decision['allocations']:
            scenario = next(entry for entry in self.cases['scenarios'] if entry['id'] == item['scenario'])
            projects = tuple(part for part in item['portfolio'].split('+') if part != 'baseline') if item['portfolio'] != 'baseline' else ()
            catalog = parse(apply_changes(apply_changes(self.document, scenario['changes']),
                                          [change for project in projects
                                           for change in next(p for p in self.cases['proposals'] if p['id'] == project)['changes']]))
            require_valid_result(catalog, item['result'])
        roles = {role for item in self.decision['allocations'] for role in item['roles']}
        self.assertEqual(roles, {'selected', 'benchmark'})

    def test_saved_decision_rejects_tampered_cases_and_summaries(self):
        save_decision(self.document, self.cases, self.decision, self.root / 'decision')
        path = self.root / 'decision' / 'cases.json'
        changed = json.loads(path.read_text())
        changed['scenarios'][1]['changes'][0]['value'] = 46
        path.write_text(canonical(changed))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify_decision_bundle(self.root / 'decision')
        manifest_path = self.root / 'decision' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['cases.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, 'scenario summary'):
            verify_decision_bundle(self.root / 'decision')

    def test_cli_exposes_robust_save_verify_export_roundtrip(self):
        fixture = str(FIXTURE)
        cases = str(CASES)
        args = parser().parse_args(['robust', '--fixture', fixture, '--cases', cases])
        outcome = dispatch(args)
        self.assertEqual(outcome['decision']['selected'], 'expand-shared+expand-village')
        self.assertEqual(outcome['state'], 'optimal')
        saved = str(self.root / 'saved')
        args = parser().parse_args(['robust', '--fixture', fixture, '--cases', cases, '--save', saved])
        outcome = dispatch(args)
        self.assertTrue(outcome['bundle']['verified'])
        args = parser().parse_args(['robust-verify', '--bundle', saved])
        self.assertEqual(dispatch(args)['selected'], 'expand-shared+expand-village')
        args = parser().parse_args(['robust-matrix', '--bundle', saved, '--recompute'])
        matrix = dispatch(args)
        self.assertEqual(len(matrix['matrix']), len(outcome['decision']['matrix']))
        self.assertEqual(matrix['summary']['state'], 'optimal')
        target = str(self.root / 'cli-export')
        args = parser().parse_args(['robust-export', '--bundle', saved, target])
        result = dispatch(args)
        self.assertTrue(result['verified'])
        bad = parser().parse_args(['robust', '--fixture', fixture, '--cases', cases])
        bad.node_limit = 0
        with self.assertRaises(ValueError):
            dispatch(bad)
        with __import__('contextlib').redirect_stdout(__import__('io').StringIO()):
            self.assertEqual(main(['robust', '--fixture', fixture, '--cases', cases]), 0)

    def test_solver_assess_reports_bounds_when_node_cap_hits(self):
        catalog = parse(self.document)
        outcome = assess(catalog, node_limit=1)
        self.assertEqual(outcome['status'], 'incomplete')
        self.assertIsNone(outcome['result'])
        self.assertEqual(outcome['served_upper'], 75)
        self.assertEqual(outcome['served_lower'], 0)
        self.assertFalse(outcome['search']['autumn']['exact'])
        full = assess(catalog, node_limit=100)
        self.assertEqual(full['status'], 'complete')
        self.assertEqual(full['result']['served'], 75)
        with self.assertRaises(Exception):
            solve(catalog, node_limit=1)
