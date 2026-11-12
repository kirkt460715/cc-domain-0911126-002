from collections import defaultdict
from estate.compatibility import reasons, room_capacity
from estate.catalog import InvalidPlan


def verify_result(catalog, result):
    errors = []
    if result.get('input_digest') != catalog.fingerprint:
        errors.append('input_digest_mismatch')
    served = defaultdict(int)
    room_use = defaultdict(int)
    building_use = defaultdict(int)
    room_stages = defaultdict(set)
    seen = set()
    assignments = result.get('assignments')
    if not isinstance(assignments, list):
        return ['assignments_not_list']
    for index, item in enumerate(assignments):
        prefix = f'assignments[{index}]'
        if not isinstance(item, dict):
            errors.append(prefix + ':not_object')
            continue
        demand = catalog.demands.get(item.get('demand'))
        room = catalog.rooms.get(item.get('room'))
        if demand is None or room is None:
            errors.append(prefix + ':unknown_reference')
            continue
        amount = item.get('students')
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            errors.append(prefix + ':invalid_students')
            continue
        key = (demand['id'], room['id'])
        if key in seen:
            errors.append(prefix + ':duplicate_assignment')
        seen.add(key)
        if item.get('period') != demand['period']:
            errors.append(prefix + ':wrong_period')
        if item.get('stage') != demand['stage']:
            errors.append(prefix + ':wrong_stage')
        if item.get('building') != room['building']:
            errors.append(prefix + ':wrong_building')
        denied = reasons(catalog, demand, room)
        if denied:
            errors.append(prefix + ':incompatible:' + '|'.join(denied))
        building = catalog.buildings[room['building']]
        travel = 0 if demand['zone'] == building['zone'] else catalog.routes.get((demand['zone'], building['zone']))
        if item.get('minutes') != travel:
            errors.append(prefix + ':wrong_travel')
        served[demand['id']] += amount
        room_use[(demand['period'], room['id'])] += amount
        building_use[(demand['period'], room['building'])] += amount
        room_stages[(demand['period'], room['id'])].add(demand['stage'])
    for ident, demand in catalog.demands.items():
        if served[ident] > demand['students']:
            errors.append(f'demand:{ident}:overserved')
        if catalog.document['policy'].get('require_minima', True):
            if served[ident] < demand.get('minimum', 0):
                errors.append(f'demand:{ident}:minimum_unmet')
    for (period, room_id), amount in room_use.items():
        stages = room_stages[(period, room_id)]
        if len(stages) != 1:
            errors.append(f'room:{period}:{room_id}:multiple_stages')
            continue
        stage = catalog.stages[next(iter(stages))]
        if amount > room_capacity(catalog.rooms[room_id], stage):
            errors.append(f'room:{period}:{room_id}:capacity_exceeded')
    for (period, building_id), amount in building_use.items():
        if amount > catalog.buildings[building_id]['seat_limit']:
            errors.append(f'building:{period}:{building_id}:capacity_exceeded')
    if sum(served.values()) != result.get('served'):
        errors.append('served_total_mismatch')
    period_rows = result.get('periods', [])
    expected_periods = set(catalog.periods)
    actual_periods = {p.get('period') for p in period_rows if isinstance(p, dict)}
    if expected_periods != actual_periods or len(period_rows) != len(expected_periods):
        errors.append('period_partition_mismatch')
    for period in period_rows:
        if not isinstance(period, dict):
            errors.append('period_not_object')
            continue
        ident = period.get('period')
        expected = [a for a in assignments if isinstance(a, dict) and a.get('period') == ident]
        if period.get('assignments') != expected:
            errors.append(f'period:{ident}:assignments_mismatch')
        if period.get('served') != sum(a.get('students', 0) for a in expected):
            errors.append(f'period:{ident}:served_mismatch')
        stages = period.get('room_stages', {})
        for item in expected:
            if stages.get(item['room']) != item['stage']:
                errors.append(f'period:{ident}:room_stage_mismatch')
    if sum(p.get('cost', 0) for p in period_rows if isinstance(p, dict)) != result.get('cost'):
        errors.append('cost_total_mismatch')
    return sorted(set(errors))


def require_valid_result(catalog, result):
    errors = verify_result(catalog, result)
    if errors:
        raise InvalidPlan('result', ';'.join(errors))
    return result


def resource_ledger(catalog, result):
    require_valid_result(catalog, result)
    rooms = defaultdict(int)
    buildings = defaultdict(int)
    stages = {}
    for item in result['assignments']:
        key = (item['period'], item['room'])
        rooms[key] += item['students']
        buildings[(item['period'], item['building'])] += item['students']
        stages[key] = item['stage']
    room_rows = []
    for period in result['periods']:
        for room_id, stage_id in period['room_stages'].items():
            capacity = room_capacity(catalog.rooms[room_id], catalog.stages[stage_id])
            used = rooms[(period['period'], room_id)]
            room_rows.append({
                'period': period['period'],
                'room': room_id,
                'stage': stage_id,
                'capacity': capacity,
                'used': used,
                'unused': capacity - used,
            })
    building_rows = []
    for period in sorted(catalog.periods):
        for ident, building in sorted(catalog.buildings.items()):
            used = buildings[(period, ident)]
            building_rows.append({
                'period': period,
                'building': ident,
                'capacity': building['seat_limit'],
                'used': used,
                'unused': building['seat_limit'] - used,
            })
    return {'rooms': room_rows, 'buildings': building_rows}
