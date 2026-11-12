from collections import defaultdict
import math
from estate.catalog import digest, parse, canonical
from estate.compatibility import compatible_edges, room_capacity
from allocation.solver import build_network, solve_period
from allocation.verify import require_valid_result


def cost_components(catalog, result):
    require_valid_result(catalog, result)
    policy = catalog.document['policy']
    travel_weight = policy.get('travel_cost', 1)
    priority_weight = policy.get('priority_cost', 10000)
    maximum = {}
    for demand in catalog.demands.values():
        period = demand['period']
        maximum[period] = max(maximum.get(period, 0), demand['priority'])
    rows = []
    by_period = defaultdict(lambda: {'travel': 0, 'priority': 0, 'total': 0})
    for item in result['assignments']:
        demand = catalog.demands[item['demand']]
        travel_cost = math.ceil(item['minutes'] * 1000) * travel_weight * item['students']
        priority_cost = (maximum[item['period']] - demand['priority']) * priority_weight * item['students']
        total = travel_cost + priority_cost
        rows.append({
            'demand': item['demand'],
            'room': item['room'],
            'period': item['period'],
            'students': item['students'],
            'travel_cost': travel_cost,
            'priority_cost': priority_cost,
            'total_cost': total,
        })
        group = by_period[item['period']]
        group['travel'] += travel_cost
        group['priority'] += priority_cost
        group['total'] += total
    total_cost = sum(row['total_cost'] for row in rows)
    for period in result['periods']:
        computed = by_period[period['period']]['total']
        if computed != period['cost']:
            raise ValueError('declared period cost does not match assignment economics')
    if total_cost != result['cost']:
        raise ValueError('declared total cost does not match assignment economics')
    return {
        'rows': rows,
        'periods': [{'period': p, **by_period[p]} for p in sorted(catalog.periods)],
        'total': total_cost,
        'units': 'travel_minutes_times_1000_plus_priority_penalty',
    }


def fixed_stage_certificate(catalog, period_result):
    period = period_result['period']
    edges = compatible_edges(catalog)
    selected = period_result['room_stages']
    model = build_network(catalog, edges, period, selected)
    handles = model.assignment_handles
    assigned = {(item['demand'], item['room']): item['students'] for item in period_result['assignments']}
    served = defaultdict(int)
    room_used = defaultdict(int)
    building_used = defaultdict(int)
    for item in period_result['assignments']:
        served[item['demand']] += item['students']
        room_used[item['room']] += item['students']
        building_used[item['building']] += item['students']
    for u, index in model.network.forward:
        edge = model.network.graph[u][index]
        label = edge.label
        kind = label[0] if label else None
        if kind == 'assignment':
            amount = assigned.get((label[1], label[2]), 0)
        elif kind == 'room':
            amount = room_used[label[1]]
        elif kind == 'building':
            amount = building_used[label[1]]
        elif kind == 'minimum':
            amount = min(served[label[1]], catalog.demands[label[1]].get('minimum', 0))
        elif kind == 'demand':
            minimum = catalog.demands[label[1]].get('minimum', 0)
            amount = max(0, served[label[1]] - minimum)
        else:
            raise AssertionError('unrecognized network edge')
        if amount > edge.initial:
            raise ValueError('result exceeds reconstructed network capacity')
        edge.capacity = edge.initial - amount
        model.network.graph[edge.target][edge.reverse].capacity = amount
    audit = model.network.verify(model.source, model.sink)
    reachable = model.network.reachable(model.source)
    if model.sink in reachable:
        raise ValueError('allocation has an augmenting path for selected room stages')
    cut = model.network.cut(model.source)
    capacity = sum(row['capacity'] for row in cut)
    if capacity != audit['flow']:
        raise ValueError('cut capacity does not certify selected-stage maximum flow')
    served_total = sum(assigned.values())
    if served_total != period_result['served']:
        raise ValueError('period service mismatch')
    return {
        'period': period,
        'served': served_total,
        'cut_capacity': capacity,
        'max_flow_certified_for_selected_stages': True,
        'global_stage_optimality_requires_search_replay': True,
        'reachable': [list(node) for node in sorted(reachable)],
        'cut': cut,
        'cost_proof': residual_cost_proof(model.network),
    }


def certificate(catalog, result):
    require_valid_result(catalog, result)
    costs = cost_components(catalog, result)
    periods = [fixed_stage_certificate(catalog, period) for period in result['periods']]
    for period in periods:
        validate_cost_proof(period['cost_proof'])
    body = {
        'version': 1,
        'input_digest': catalog.fingerprint,
        'result_digest': digest(result),
        'costs': costs,
        'periods': periods,
    }
    return {**body, 'certificate_digest': digest(body)}


def replay_certificate(catalog, result, evidence, node_limit=100000):
    expected = certificate(catalog, result)
    if canonical(evidence) != canonical(expected):
        raise ValueError('certificate changed or belongs to another result')
    edges = compatible_edges(catalog)
    for period in result['periods']:
        replay = solve_period(catalog, edges, period['period'], node_limit)
        if replay['assignments'] != period['assignments']:
            raise ValueError('exact stage-choice replay produced another allocation')
        if replay['cost'] != period['cost'] or replay['quota_served'] != period['quota_served']:
            raise ValueError('replayed objective differs')
    return {'verified': True, 'result_digest': digest(result), 'global_stage_optimality_replayed': True}


def capacity_waterfall(catalog):
    rows = []
    for period_id in sorted(catalog.periods):
        for room_id, room in sorted(catalog.rooms.items()):
            building = catalog.buildings[room['building']]
            for stage_id in sorted(room['allowed_stages']):
                stage = catalog.stages[stage_id]
                physical = room['seats']
                class_cap = min(physical, stage['class_limit'])
                area_cap = min(class_cap, math.floor(room['area'] / stage['area_per_student'] + 1e-10))
                feature_cap = area_cap if not set(stage['required_features']) - set(room['features']) else 0
                scheduled = feature_cap if period_id in room['periods'] else 0
                from estate.calendar import building_status
                calendar_cap = scheduled if building_status(building, catalog.periods[period_id]) == 'available' else 0
                rows.append({
                    'period': period_id,
                    'room': room_id,
                    'stage': stage_id,
                    'physical_seats': physical,
                    'after_class_limit': class_cap,
                    'after_area_limit': area_cap,
                    'after_features': feature_cap,
                    'after_room_schedule': scheduled,
                    'after_building_calendar': calendar_cap,
                    'shared_building_limit': building['seat_limit'],
                    'class_limit_loss': physical - class_cap,
                    'area_loss': class_cap - area_cap,
                    'feature_loss': area_cap - feature_cap,
                    'schedule_loss': feature_cap - scheduled,
                    'calendar_loss': scheduled - calendar_cap,
                    'additive_across_stages': False,
                })
    return rows


def residual_cost_proof(network):
    count = len(network.graph)
    potentials = [0] * count
    for iteration in range(count):
        changed = False
        for source, arcs in enumerate(network.graph):
            for edge in arcs:
                if edge.capacity <= 0:
                    continue
                candidate = potentials[source] + edge.cost
                if candidate < potentials[edge.target]:
                    potentials[edge.target] = candidate
                    changed = True
        if not changed:
            break
    else:
        raise ValueError('residual negative cycle permits a cheaper same-volume assignment')
    residual_arcs = []
    for source, arcs in enumerate(network.graph):
        for edge in arcs:
            if edge.capacity <= 0:
                continue
            reduced = edge.cost + potentials[source] - potentials[edge.target]
            if reduced < 0:
                raise AssertionError('cost proof potential does not make residual costs nonnegative')
            residual_arcs.append({
                'source': network.names[source],
                'target': network.names[edge.target],
                'remaining_capacity': edge.capacity,
                'cost': edge.cost,
                'reduced_cost': reduced,
            })
    return {
        'no_negative_residual_cycle': True,
        'potentials': [{'node': network.names[index], 'potential': value} for index, value in enumerate(potentials)],
        'residual_arcs': residual_arcs,
    }


def validate_cost_proof(proof):
    if not isinstance(proof, dict) or proof.get('no_negative_residual_cycle') is not True:
        raise ValueError('unsupported cost proof')
    potentials = {}
    for item in proof['potentials']:
        node = tuple(item['node'])
        if node in potentials:
            raise ValueError('duplicate potential node')
        if not isinstance(item['potential'], int):
            raise ValueError('nonintegral potential')
        potentials[node] = item['potential']
    for edge in proof['residual_arcs']:
        source, target = tuple(edge['source']), tuple(edge['target'])
        if source not in potentials or target not in potentials:
            raise ValueError('unknown potential node')
        if edge['remaining_capacity'] <= 0:
            raise ValueError('nonpositive residual capacity')
        actual = edge['cost'] + potentials[source] - potentials[target]
        if actual < 0 or actual != edge['reduced_cost']:
            raise ValueError('invalid reduced cost witness')
    return True


def perturbation_bounds(catalog, result):
    require_valid_result(catalog, result)
    counts = defaultdict(int)
    for item in result['assignments']:
        counts[item['demand']] += item['students']
    edges = compatible_edges(catalog)
    rows = []
    for ident, demand in sorted(catalog.demands.items()):
        capacities = [edge['capacity'] for edge in edges if edge['demand'] == ident]
        individual_upper = min(demand['students'], sum(capacities))
        rows.append({
            'demand': ident,
            'current_served': counts[ident],
            'current_shortfall': demand['students'] - counts[ident],
            'individual_upper': individual_upper,
            'maximum_possible_service_gain_without_new_eligibility': max(0, individual_upper - counts[ident]),
            'proven_shortfall_without_new_eligibility': demand['students'] - individual_upper,
            'bound_includes_competition': False,
        })
    return rows
