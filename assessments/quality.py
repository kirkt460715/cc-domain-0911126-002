from collections import defaultdict
from estate.catalog import parse, digest
from estate.calendar import conflicting_periods, availability_report
from estate.compatibility import compatible_edges, capacity_inventory, reasons
from allocation.verify import verify_result


def inspect_input(document):
    catalog = parse(document)
    issues = []
    edges = compatible_edges(catalog)
    by_demand = defaultdict(list)
    by_room = defaultdict(list)
    for edge in edges:
        by_demand[edge['demand']].append(edge)
        by_room[(edge['period'], edge['room'])].append(edge)
    for left, right in sorted(conflicting_periods(catalog)):
        issues.append({'severity': 'error', 'code': 'overlapping_periods', 'entity': left, 'related': right})
    for ident, demand in sorted(catalog.demands.items()):
        options = by_demand[ident]
        if demand['students'] and not options:
            issues.append({'severity': 'warning', 'code': 'unreachable_demand', 'entity': ident})
        upper = sum(e['capacity'] for e in options)
        if upper < demand.get('minimum', 0):
            issues.append({'severity': 'error', 'code': 'individual_minimum_impossible', 'entity': ident, 'upper': upper})
        if not demand['students']:
            issues.append({'severity': 'info', 'code': 'explicit_zero_demand', 'entity': ident})
    for key, options in sorted(by_room.items()):
        stages = sorted({catalog.demands[e['demand']]['stage'] for e in options})
        if len(stages) > 1:
            issues.append({'severity': 'info', 'code': 'exclusive_stage_choice', 'entity': key[1], 'period': key[0], 'stages': stages})
    for ident, room in sorted(catalog.rooms.items()):
        if not any(key[1] == ident for key in by_room):
            issues.append({'severity': 'info', 'code': 'unused_room', 'entity': ident})
        permitted = set(room['allowed_stages'])
        possible = [catalog.stages[stage] for stage in permitted]
        if possible and all(set(stage['required_features']) - set(room['features']) for stage in possible):
            issues.append({'severity': 'warning', 'code': 'room_features_exclude_all_stages', 'entity': ident})
    for ident, building in sorted(catalog.buildings.items()):
        rooms = [r for r in catalog.rooms.values() if r['building'] == ident]
        nominal = sum(r['seats'] for r in rooms)
        if building['seat_limit'] > nominal:
            issues.append({'severity': 'info', 'code': 'building_limit_above_room_inventory', 'entity': ident, 'nominal': nominal})
        if not rooms:
            issues.append({'severity': 'warning', 'code': 'building_without_rooms', 'entity': ident})
    represented = {(d['zone'], d['stage'], d['period']) for d in catalog.demands.values()}
    missing = []
    for zone in sorted(catalog.zones):
        for stage in sorted(catalog.stages):
            for period in sorted(catalog.periods):
                if (zone, stage, period) not in represented:
                    missing.append({'zone': zone, 'stage': stage, 'period': period})
    return {
        'input_digest': catalog.fingerprint,
        'issues': issues,
        'missing_population_cells': missing,
        'counts': {level: sum(row['severity'] == level for row in issues) for level in ('error', 'warning', 'info')},
        'eligible_edges': len(edges),
        'availability': availability_report(catalog),
    }


def inspect_output(document, result):
    catalog = parse(document)
    errors = verify_result(catalog, result)
    return {
        'input_digest': catalog.fingerprint,
        'result_digest': digest(result),
        'valid': not errors,
        'errors': errors,
        'declared_served': result.get('served'),
        'computed_served': sum(a.get('students', 0) for a in result.get('assignments', []) if isinstance(a, dict)),
    }


def bottleneck_map(catalog, result):
    from allocation.verify import require_valid_result, resource_ledger
    require_valid_result(catalog, result)
    ledger = resource_ledger(catalog, result)
    room_rows = {(r['period'], r['room']): r for r in ledger['rooms']}
    building_rows = {(r['period'], r['building']): r for r in ledger['buildings']}
    served = defaultdict(int)
    for item in result['assignments']:
        served[item['demand']] += item['students']
    report = []
    for ident, demand in sorted(catalog.demands.items()):
        if served[ident] == demand['students']:
            continue
        candidates = []
        for room_id, room in sorted(catalog.rooms.items()):
            denied = reasons(catalog, demand, room)
            key = demand['period'], room_id
            assigned = room_rows.get(key)
            building = building_rows[(demand['period'], room['building'])]
            if denied:
                cause = 'incompatible'
            elif assigned is None:
                cause = 'unselected_stage'
            elif assigned['stage'] != demand['stage']:
                cause = 'exclusive_stage_choice'
            elif building['unused'] == 0:
                cause = 'building_limit'
            elif assigned['unused'] == 0:
                cause = 'room_limit'
            else:
                cause = 'network_competition'
            candidates.append({
                'room': room_id,
                'cause': cause,
                'exclusions': denied,
                'room_unused': assigned['unused'] if assigned else None,
                'building_unused': building['unused'],
            })
        report.append({'demand': ident, 'shortfall': demand['students'] - served[ident], 'candidates': candidates})
    return report
