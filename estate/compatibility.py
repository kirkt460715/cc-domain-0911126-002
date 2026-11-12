import math
from estate.calendar import building_status


def room_capacity(room, stage):
    area_capacity = math.floor(room['area'] / stage['area_per_student'] + 1e-10)
    return min(room['seats'], stage['class_limit'], area_capacity)


def reasons(catalog, demand, room):
    stage = catalog.stages[demand['stage']]
    building = catalog.buildings[room['building']]
    period = catalog.periods[demand['period']]
    result = []
    if demand['period'] not in room['periods']:
        result.append('room_not_scheduled')
    if stage['id'] not in room['allowed_stages']:
        result.append('stage_not_allowed')
    missing = sorted(set(stage['required_features']) - set(room['features']))
    if missing:
        result.append('missing_features:' + ','.join(missing))
    state = building_status(building, period)
    if state != 'available':
        result.append('building_' + state)
    origin, destination = demand['zone'], building['zone']
    travel = 0 if origin == destination else catalog.routes.get((origin, destination))
    if travel is None:
        result.append('no_directed_route')
    elif travel > demand['max_minutes']:
        result.append('travel_limit')
    if room_capacity(room, stage) <= 0:
        result.append('zero_usable_capacity')
    if building['seat_limit'] <= 0:
        result.append('building_capacity_zero')
    return result


def compatible_edges(catalog):
    result = []
    for demand in sorted(catalog.demands.values(), key=lambda d: d['id']):
        for room in sorted(catalog.rooms.values(), key=lambda r: r['id']):
            if reasons(catalog, demand, room):
                continue
            building = catalog.buildings[room['building']]
            travel = 0 if demand['zone'] == building['zone'] else catalog.routes[(demand['zone'], building['zone'])]
            result.append({
                'demand': demand['id'],
                'room': room['id'],
                'building': room['building'],
                'period': demand['period'],
                'capacity': room_capacity(room, catalog.stages[demand['stage']]),
                'minutes': travel,
                'cross_zone': demand['zone'] != building['zone'],
            })
    return result


def exclusion_matrix(catalog):
    result = []
    for demand in sorted(catalog.demands.values(), key=lambda d: d['id']):
        for room in sorted(catalog.rooms.values(), key=lambda r: r['id']):
            denied = reasons(catalog, demand, room)
            if denied:
                result.append({'demand': demand['id'], 'room': room['id'], 'reasons': denied})
    return result


def capacity_inventory(catalog):
    report = []
    for room in sorted(catalog.rooms.values(), key=lambda r: r['id']):
        building = catalog.buildings[room['building']]
        for period_id in sorted(room['periods']):
            period = catalog.periods[period_id]
            for stage_id in sorted(room['allowed_stages']):
                stage = catalog.stages[stage_id]
                missing = sorted(set(stage['required_features']) - set(room['features']))
                usable = building_status(building, period) == 'available' and not missing
                report.append({
                    'room': room['id'],
                    'building': building['id'],
                    'period': period_id,
                    'stage': stage_id,
                    'capacity': room_capacity(room, stage) if usable else 0,
                    'nominal_seats': room['seats'],
                    'area_limit': math.floor(room['area'] / stage['area_per_student'] + 1e-10),
                    'class_limit': stage['class_limit'],
                    'missing_features': missing,
                    'status': building_status(building, period),
                })
    return report


def diagnose_demand(catalog, edges, demand_id):
    demand = catalog.demands[demand_id]
    usable = [edge for edge in edges if edge['demand'] == demand_id]
    counts = {}
    for room in catalog.rooms.values():
        for reason in reasons(catalog, demand, room):
            category = reason.split(':')[0]
            counts[category] = counts.get(category, 0) + 1
    potential = sum(e['capacity'] for e in usable)
    return {
        'demand': demand_id,
        'students': demand['students'],
        'compatible_rooms': len(usable),
        'unshared_upper_bound': potential,
        'minimum_travel': min((e['minutes'] for e in usable), default=None),
        'exclusion_counts': dict(sorted(counts.items())),
        'structural_shortfall': max(0, demand['students'] - potential),
    }
