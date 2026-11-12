from dataclasses import dataclass
from itertools import product
import math
from estate.catalog import InvalidPlan
from estate.calendar import assert_independent_periods
from estate.compatibility import compatible_edges, room_capacity
from allocation.network import Network


class SearchLimit(RuntimeError):
    pass


class InfeasibleMinimum(ValueError):
    def __init__(self, shortfalls):
        self.shortfalls = shortfalls
        super().__init__('mandatory minimum allocations are infeasible')


@dataclass
class FlowModel:
    network: Network
    assignment_handles: dict
    quota_handles: dict
    source: tuple
    sink: tuple
    total_demand: int
    reward: int


def choices_for_period(catalog, edges, period):
    options = {}
    for edge in edges:
        if edge['period'] != period:
            continue
        stage = catalog.demands[edge['demand']]['stage']
        options.setdefault(edge['room'], set()).add(stage)
    return [(room, sorted(stages)) for room, stages in sorted(options.items())]


def build_network(catalog, edges, period, room_stages):
    network = Network()
    source = ('source', period)
    sink = ('sink', period)
    network.node(source)
    network.node(sink)
    demands = [d for d in catalog.demands.values() if d['period'] == period]
    total = sum(d['students'] for d in demands)
    policy = catalog.document['policy']
    travel_weight = policy.get('travel_cost', 1)
    priority_weight = policy.get('priority_cost', 10000)
    max_priority = max((d['priority'] for d in demands), default=0)
    max_travel = max((math.ceil(e['minutes'] * 1000) for e in edges if e['period'] == period), default=0)
    maximum_unit_cost = max_travel * travel_weight + max_priority * priority_weight
    reward = (total + 1) * (maximum_unit_cost + 1)
    quotas = {}
    assignment_handles = {}
    for demand in sorted(demands, key=lambda d: d['id']):
        ident = demand['id']
        node = ('demand', ident)
        quota = demand.get('minimum', 0)
        penalty = (max_priority - demand['priority']) * priority_weight
        if quota:
            quotas[ident] = network.add(source, node, quota, penalty - reward, ('minimum', ident))
        network.add(source, node, demand['students'] - quota, penalty, ('demand', ident))
    for building in sorted(catalog.buildings.values(), key=lambda b: b['id']):
        network.add(('building', building['id']), sink, building['seat_limit'], label=('building', building['id']))
    for room_id, stage_id in sorted(room_stages.items()):
        room = catalog.rooms[room_id]
        capacity = room_capacity(room, catalog.stages[stage_id])
        network.add(('room', room_id), ('building', room['building']), capacity, label=('room', room_id))
    for edge in edges:
        if edge['period'] != period:
            continue
        demand = catalog.demands[edge['demand']]
        if room_stages.get(edge['room']) != demand['stage']:
            continue
        handle = network.add(
            ('demand', edge['demand']),
            ('room', edge['room']),
            min(edge['capacity'], demand['students']),
            math.ceil(edge['minutes'] * 1000) * travel_weight,
            ('assignment', edge['demand'], edge['room']),
        )
        assignment_handles[(edge['demand'], edge['room'])] = handle
    return FlowModel(network, assignment_handles, quotas, source, sink, total, reward)


def solve_fixed(catalog, edges, period, room_stages):
    model = build_network(catalog, edges, period, room_stages)
    amount, adjusted_cost = model.network.min_cost_flow(model.source, model.sink, model.total_demand)
    audit = model.network.verify(model.source, model.sink)
    if audit['flow'] != amount or audit['cost'] != adjusted_cost:
        raise AssertionError('solver flow audit mismatch')
    used_quota = sum(model.network.flow(handle) for handle in model.quota_handles.values())
    shortfalls = {}
    for ident, handle in model.quota_handles.items():
        missing = catalog.demands[ident]['minimum'] - model.network.flow(handle)
        if missing:
            shortfalls[ident] = missing
    assignments = []
    edge_map = {(e['demand'], e['room']): e for e in edges if e['period'] == period}
    for key, handle in sorted(model.assignment_handles.items()):
        count = model.network.flow(handle)
        if count:
            edge = edge_map[key]
            assignments.append({
                'demand': key[0],
                'room': key[1],
                'building': edge['building'],
                'period': period,
                'stage': catalog.demands[key[0]]['stage'],
                'students': count,
                'minutes': edge['minutes'],
            })
    cost = adjusted_cost + used_quota * model.reward
    return {
        'period': period,
        'served': amount,
        'cost': cost,
        'quota_served': used_quota,
        'minimum_shortfalls': shortfalls,
        'assignments': assignments,
        'room_stages': dict(sorted(room_stages.items())),
        'cut': model.network.cut(model.source),
    }


def optimistic_bound(catalog, edges, period, chosen, remaining):
    room_capacity_upper = {}
    for edge in edges:
        if edge['period'] != period:
            continue
        stage = catalog.demands[edge['demand']]['stage']
        room = edge['room']
        if room in chosen and chosen[room] != stage:
            continue
        if room not in chosen and room not in remaining:
            continue
        room_capacity_upper[room] = max(room_capacity_upper.get(room, 0), edge['capacity'])
    by_building = {}
    for room, capacity in room_capacity_upper.items():
        building = catalog.rooms[room]['building']
        by_building[building] = by_building.get(building, 0) + capacity
    capacity = sum(min(value, catalog.buildings[b]['seat_limit']) for b, value in by_building.items())
    demand = sum(d['students'] for d in catalog.demands.values() if d['period'] == period)
    return min(capacity, demand)


def allocation_signature(result):
    return tuple((a['demand'], a['room'], a['students']) for a in result['assignments'])


def solve_period(catalog, edges, period, node_limit=100000):
    options = choices_for_period(catalog, edges, period)
    options.sort(key=lambda item: (-len(item[1]), item[0]))
    required = catalog.document['policy'].get('require_minima', True)
    best = None
    best_key = None
    visited = 0
    pruned = 0
    infeasible = None

    def visit(index, chosen):
        nonlocal best, best_key, visited, pruned, infeasible
        visited += 1
        if visited > node_limit:
            raise SearchLimit(f'period {period}: exact search exceeded {node_limit} nodes; no partial result committed')
        remaining = {room for room, _ in options[index:]}
        upper = optimistic_bound(catalog, edges, period, chosen, remaining)
        if best is not None and upper < best['served']:
            pruned += 1
            return
        if index < len(options):
            room, stages = options[index]
            for stage in stages:
                chosen[room] = stage
                visit(index + 1, chosen)
            chosen.pop(room, None)
            return
        candidate = solve_fixed(catalog, edges, period, chosen)
        if candidate['minimum_shortfalls'] and required:
            shortfall_key = (sum(candidate['minimum_shortfalls'].values()), sorted(candidate['minimum_shortfalls'].items()))
            if infeasible is None or shortfall_key < infeasible[0]:
                infeasible = shortfall_key, candidate['minimum_shortfalls']
            return
        key = (-candidate['served'], -candidate['quota_served'], candidate['cost'], allocation_signature(candidate))
        if best_key is None or key < best_key:
            best = candidate
            best_key = key

    visit(0, {})
    if best is None:
        raise InfeasibleMinimum(infeasible[1] if infeasible else {})
    best['search'] = {'visited': visited, 'pruned': pruned, 'exact': True}
    return best


def solve(catalog, node_limit=100000):
    if isinstance(node_limit, bool) or not isinstance(node_limit, int) or node_limit < 1:
        raise ValueError('node_limit must be positive integer')
    assert_independent_periods(catalog)
    edges = compatible_edges(catalog)
    periods = []
    for period in sorted(catalog.periods):
        periods.append(solve_period(catalog, edges, period, node_limit))
    assignments = [a for p in periods for a in p['assignments']]
    return {
        'version': 1,
        'input_digest': catalog.fingerprint,
        'served': sum(p['served'] for p in periods),
        'cost': sum(p['cost'] for p in periods),
        'assignments': assignments,
        'periods': periods,
    }
