import copy
from itertools import product
import random
import unittest
from test_catalog_calendar import county
from estate.catalog import parse
from estate.compatibility import compatible_edges, room_capacity
from allocation.network import Network
from allocation.solver import solve, SearchLimit, InfeasibleMinimum
from allocation.verify import verify_result, require_valid_result, resource_ledger
from allocation.guarantees import maximize_guarantee, with_guarantee
from assessments.equity import report, weighted_gini, aggregate
from assessments.comparison import compare, frontier
from assessments.travel import travel_distribution, zone_flows, threshold_curve
from assessments.sensitivity import study, marginal_room_capacity
from assessments.interventions import select_interventions, replay_intervention
from runs.variants import apply_changes


def tiny():
    data = county()
    data['zones'] = [data['zones'][0]]
    data['buildings'] = [data['buildings'][0]]
    data['buildings'][0]['seat_limit'] = 3
    data['rooms'] = data['rooms'][:2]
    for room in data['rooms']:
        room['seats'] = 2
    data['demands'] = data['demands'][:2]
    for demand in data['demands']:
        demand['students'] = 2
        demand['minimum'] = 0
    data['routes'] = []
    return data


def brute_objective(catalog):
    edges = compatible_edges(catalog)
    best = None
    max_priority = max(d['priority'] for d in catalog.demands.values())
    for amounts in product(*[range(min(catalog.demands[e['demand']]['students'], e['capacity']) + 1) for e in edges]):
        demand_use = {ident: 0 for ident in catalog.demands}
        room_use = {ident: 0 for ident in catalog.rooms}
        building_use = {ident: 0 for ident in catalog.buildings}
        stages = {ident: set() for ident in catalog.rooms}
        cost = 0
        for edge, amount in zip(edges, amounts):
            demand = catalog.demands[edge['demand']]
            demand_use[demand['id']] += amount
            room_use[edge['room']] += amount
            building_use[edge['building']] += amount
            if amount:
                stages[edge['room']].add(demand['stage'])
            cost += amount * ((max_priority - demand['priority']) * catalog.document['policy'].get('priority_cost', 10000) + __import__('math').ceil(edge['minutes'] * 1000) * catalog.document['policy'].get('travel_cost', 1))
        if any(demand_use[ident] > demand['students'] for ident, demand in catalog.demands.items()):
            continue
        if any(len(values) > 1 for values in stages.values()):
            continue
        if any(room_use[ident] > room_capacity(catalog.rooms[ident], catalog.stages[next(iter(values))]) for ident, values in stages.items() if values):
            continue
        if any(building_use[ident] > building['seat_limit'] for ident, building in catalog.buildings.items()):
            continue
        quota = sum(min(demand_use[ident], demand.get('minimum', 0)) for ident, demand in catalog.demands.items())
        missing = any(demand_use[ident] < demand.get('minimum', 0) for ident, demand in catalog.demands.items())
        if missing and catalog.document['policy'].get('require_minima', True):
            continue
        key = (-sum(demand_use.values()), -quota, cost)
        if best is None or key < best:
            best = key
    return best


class NetworkTests(unittest.TestCase):
    def test_residual_reroutes_earlier_assignment(self):
        net = Network()
        net.add('s', 'a', 1)
        net.add('s', 'b', 1)
        net.add('a', 'x', 1)
        net.add('a', 'y', 1)
        net.add('b', 'x', 1)
        net.add('x', 't', 1)
        net.add('y', 't', 1)
        self.assertEqual(net.max_flow('s', 't'), 2)
        self.assertEqual(net.verify('s', 't')['flow'], 2)
        self.assertEqual(sum(row['capacity'] for row in net.cut('s')), 2)
        net.reset()
        self.assertEqual(net.max_flow('s', 't', 1), 1)
        self.assertEqual(net.max_flow('s', 't'), 1)

    def test_min_cost_negative_quota_edges(self):
        net = Network()
        mandatory = net.add('s', 'a', 1, -100)
        net.add('s', 'b', 2, 0)
        net.add('a', 't', 1, 50)
        net.add('b', 't', 2, 0)
        flow, cost = net.min_cost_flow('s', 't', 2)
        self.assertEqual((flow, cost), (2, -50))
        self.assertEqual(net.flow(mandatory), 1)
        self.assertEqual(net.verify('s', 't'), {'flow': 2, 'cost': -50})
        net.reset()
        self.assertEqual(net.min_cost_flow('s', 't', 3), (3, -50))

    def test_network_rejects_invalid_capacity_and_negative_cycle(self):
        net = Network()
        for capacity in (-1, True, 1.5):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                net.add('s', 't', capacity)
        with self.assertRaises(ValueError):
            net.add('s', 's', 1)
        net.add('s', 'a', 1)
        net.add('a', 'b', 1, -2)
        net.add('b', 'a', 1, 0)
        net.add('b', 't', 1)
        with self.assertRaisesRegex(ValueError, 'negative cost cycle'):
            net.min_cost_flow('s', 't', 1)


class AllocationTests(unittest.TestCase):
    def test_county_building_limit_and_room_stage_exclusivity(self):
        catalog = parse(county())
        result = solve(catalog)
        self.assertEqual(result['served'], 75)
        self.assertEqual(verify_result(catalog, result), [])
        self.assertEqual(result['periods'][0]['room_stages']['flex'], 'secondary')
        equity = report(catalog, result)
        served = {row['demand']: row['served'] for row in equity['demands']}
        self.assertEqual(served['town-secondary'], 25)
        self.assertEqual(served['town-primary'], 25)
        self.assertEqual(served['village-primary'], 25)
        self.assertEqual(sum(row['used'] for row in resource_ledger(catalog, result)['rooms']), 75)

    def test_solver_matches_independent_exhaustive_integer_oracle(self):
        rng = random.Random(20261112)
        for case in range(35):
            data = tiny()
            for room in data['rooms']:
                room['seats'] = rng.randint(1, 3)
            data['buildings'][0]['seat_limit'] = rng.randint(1, 5)
            for demand in data['demands']:
                demand['students'] = rng.randint(0, 3)
                demand['priority'] = rng.randint(0, 3)
                demand['minimum'] = rng.randint(0, demand['students'])
            data['policy']['require_minima'] = bool(case % 2)
            catalog = parse(data)
            expected = brute_objective(catalog)
            if expected is None:
                with self.subTest(case=case), self.assertRaises(InfeasibleMinimum):
                    solve(catalog)
                continue
            result = solve(catalog)
            served = {ident: 0 for ident in catalog.demands}
            for item in result['assignments']:
                served[item['demand']] += item['students']
            quota = sum(min(served[ident], demand.get('minimum', 0)) for ident, demand in catalog.demands.items())
            with self.subTest(case=case):
                self.assertEqual((-result['served'], -quota, result['cost']), expected)
                self.assertEqual(verify_result(catalog, result), [])

    def test_minimum_overrides_cheaper_high_priority_assignment(self):
        data = tiny()
        data['buildings'][0]['seat_limit'] = 2
        data['demands'][0]['priority'] = 100
        data['demands'][1]['priority'] = 0
        data['demands'][1]['minimum'] = 1
        catalog = parse(data)
        result = solve(catalog)
        counts = {row['demand']: row['served'] for row in report(catalog, result)['demands']}
        self.assertEqual(counts, {'town-primary': 1, 'town-secondary': 1})
        data['demands'][1]['minimum'] = 2
        data['demands'][0]['minimum'] = 1
        with self.assertRaises(InfeasibleMinimum):
            solve(parse(data))

    def test_search_budget_never_returns_partial_optimum(self):
        catalog = parse(tiny())
        with self.assertRaises(SearchLimit):
            solve(catalog, node_limit=1)
        self.assertTrue(solve(catalog)['periods'][0]['search']['exact'])
        with self.assertRaises(ValueError):
            solve(catalog, node_limit=0)

    def test_empty_demand_and_empty_supply(self):
        data = tiny()
        data['demands'] = []
        result = solve(parse(data))
        self.assertEqual(result['served'], 0)
        data = tiny()
        data['rooms'] = []
        result = solve(parse(data))
        self.assertEqual(result['served'], 0)
        self.assertTrue(all(row['reason'] == 'no_compatible_room' for row in report(parse(data), result)['demands']))

    def test_capacity_can_be_reused_only_in_adjacent_period(self):
        data = tiny()
        data['periods'].append({'id': 'spring', 'start': '2027-01-01', 'end': '2027-05-01'})
        for room in data['rooms']:
            room['periods'].append('spring')
        for row in list(data['demands']):
            data['demands'].append({**row, 'id': row['id'] + '-spring', 'period': 'spring'})
        result = solve(parse(data))
        self.assertEqual(result['served'], 6)
        data['periods'][1]['start'] = '2026-12-31'
        with self.assertRaisesRegex(ValueError, 'reuse'):
            solve(parse(data))

    def test_result_verifier_catches_tampering(self):
        catalog = parse(county())
        original = solve(catalog)
        mutations = [
            lambda r: r.update(input_digest='wrong'),
            lambda r: r.update(served=999),
            lambda r: r['assignments'][0].update(students=1000),
            lambda r: r['assignments'][0].update(minutes=999),
            lambda r: r['assignments'][0].update(stage='secondary' if r['assignments'][0]['stage'] == 'primary' else 'primary'),
            lambda r: r['assignments'].append(copy.deepcopy(r['assignments'][0])),
        ]
        for index, mutate in enumerate(mutations):
            changed = copy.deepcopy(original)
            mutate(changed)
            with self.subTest(index=index):
                self.assertTrue(verify_result(catalog, changed))
                with self.assertRaises(ValueError):
                    require_valid_result(catalog, changed)


class AssessmentTests(unittest.TestCase):
    def test_equity_aggregates_weighted_denominators(self):
        catalog = parse(county())
        result = solve(catalog)
        equity = report(catalog, result)
        self.assertEqual(equity['total'][0]['students'], 90)
        self.assertEqual(equity['total'][0]['shortfall'], 15)
        self.assertAlmostEqual(equity['total'][0]['coverage'], 75 / 90)
        self.assertAlmostEqual(equity['rural_urban_gap'][0]['urban_minus_rural'], 0)
        self.assertEqual(weighted_gini([(1, 2), (1, 3)]), 0)
        self.assertIsNone(weighted_gini([(0, 1), (0, 2)]))
        self.assertAlmostEqual(weighted_gini([(0, 1), (1, 1)]), 0.5)
        with self.assertRaises(ValueError):
            aggregate(equity['demands'], ['zone', 'zone'])

    def test_zero_population_denominator_is_unknown(self):
        data = tiny()
        for row in data['demands']:
            row['students'] = 0
        catalog = parse(data)
        equity = report(catalog, solve(catalog))
        self.assertIsNone(equity['total'][0]['coverage'])
        self.assertIsNone(equity['coverage_distribution']['minimum'])
        self.assertIsNone(travel_distribution(catalog, solve(catalog))['median'])

    def test_guarantee_is_maximal_at_basis_point_resolution(self):
        data = tiny()
        guarantee = maximize_guarantee(data)
        self.assertEqual(guarantee['basis_points'], 5000)
        self.assertEqual(guarantee['result']['served'], 3)
        with self.assertRaises(InfeasibleMinimum):
            solve(with_guarantee(data, 5001))
        with self.assertRaises(ValueError):
            with_guarantee(data, 10001)
        with self.assertRaises(ValueError):
            maximize_guarantee(data, [])

    def test_travel_histogram_and_flow_conservation(self):
        data = county()
        data['demands'][0]['max_minutes'] = 20
        data['demands'][2]['students'] = 0
        data['demands'][2]['minimum'] = 0
        catalog = parse(data)
        result = solve(catalog)
        travel = travel_distribution(catalog, result)
        self.assertEqual(sum(row['students'] for row in travel['buckets']), result['served'])
        self.assertEqual(sum(row['students'] for row in zone_flows(catalog, result)), result['served'])
        self.assertEqual(travel['maximum'], 20)
        with self.assertRaises(ValueError):
            travel_distribution(catalog, result, (20, 10))
        curve = threshold_curve(data, [0, 10, 20, 30])['curve']
        served = [row['served'] for row in curve if row['state'] == 'complete']
        self.assertEqual(served, sorted(served))

    def test_sensitivity_isolates_cases_and_marks_infeasible(self):
        data = tiny()
        cases = [
            {'name': 'more-building', 'changes': [{'table': 'buildings', 'id': 'shared', 'field': 'seat_limit', 'value': 4}]},
            {'name': 'mandatory-secondary', 'changes': [{'table': 'demands', 'id': 'town-secondary', 'field': 'minimum', 'value': 2}, {'table': 'rooms', 'id': 'flex', 'field': 'allowed_stages', 'value': ['primary']}]},
        ]
        outcome = study(data, cases)
        self.assertEqual(outcome['complete'], 1)
        self.assertEqual(outcome['failed'], 1)
        self.assertEqual(outcome['cases'][1]['state'], 'infeasible')
        self.assertEqual(data['buildings'][0]['seat_limit'], 3)
        marginal = marginal_room_capacity(data, ['primary-only'])
        self.assertEqual(marginal[0]['marginal_served'], 0)

    def test_pair_comparison_excludes_changed_population_definition(self):
        data = tiny()
        left = parse(data)
        old = solve(left)
        changed = copy.deepcopy(data)
        changed['demands'][0]['stage'] = 'secondary'
        right = parse(changed)
        comparison = compare(left, old, right, solve(right))
        self.assertEqual(comparison['excluded'][0]['reason'], 'changed_population_definition')
        self.assertEqual(len(comparison['paired']), 1)
        self.assertTrue(comparison['input_changes'])

    def test_investment_dependencies_exclusions_and_replay(self):
        data = tiny()
        proposals = [
            {'id': 'building', 'cost': 5, 'changes': [{'table': 'buildings', 'id': 'shared', 'field': 'seat_limit', 'value': 4}]},
            {'id': 'room', 'cost': 2, 'requires': ['building'], 'changes': [{'table': 'rooms', 'id': 'primary-only', 'field': 'seats', 'value': 3}]},
            {'id': 'close', 'cost': 1, 'excludes': ['building'], 'changes': [{'table': 'buildings', 'id': 'shared', 'field': 'seat_limit', 'value': 0}]},
        ]
        selected = select_interventions(data, proposals, 7)
        self.assertTrue(selected['exact'])
        self.assertEqual(selected['best']['served'], 4)
        self.assertEqual(selected['best']['proposals'], ['building'])
        replay = replay_intervention(data, proposals, selected)
        self.assertEqual(replay['result']['served'], 4)
        self.assertFalse(any(row['proposals'] == ['room'] for row in selected['cases']))
        proposals[0]['changes'][0]['value'] = 5
        with self.assertRaisesRegex(ValueError, 'changed'):
            replay_intervention(data, proposals, selected)
        with self.assertRaises(SearchLimit):
            select_interventions(data, proposals, 100, max_cases=1)
