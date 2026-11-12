from estate.catalog import parse
from allocation.solver import solve, InfeasibleMinimum, SearchLimit
from assessments.equity import report
from assessments.comparison import frontier
from runs.variants import apply_changes


def study(document, cases, node_limit=100000):
    base = parse(document)
    if not isinstance(cases, list) or len(cases) > 100:
        raise ValueError('expected at most 100 sensitivity cases')
    names = set()
    results = []
    feasible = []
    for case in cases:
        if not isinstance(case, dict) or set(case) != {'name', 'changes'}:
            raise ValueError('case requires name and changes')
        name = case['name']
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('case names must be unique nonempty strings')
        names.add(name)
        catalog = parse(apply_changes(base.document, case['changes']))
        try:
            result = solve(catalog, node_limit)
        except InfeasibleMinimum as exc:
            results.append({'name': name, 'state': 'infeasible', 'shortfalls': exc.shortfalls})
            continue
        except SearchLimit as exc:
            results.append({'name': name, 'state': 'search_limit', 'error': str(exc)})
            continue
        equity = report(catalog, result)
        row = {
            'name': name,
            'state': 'complete',
            'digest': catalog.fingerprint,
            'served': result['served'],
            'cost': result['cost'],
            'unserved_groups': equity['coverage_distribution']['unserved_groups'],
            'minimum_coverage': equity['coverage_distribution']['minimum'],
            'rural_urban_gap': equity['rural_urban_gap'],
        }
        feasible.append(row)
        results.append(row)
    pareto = {row['name']: row for row in frontier(feasible)}
    return {
        'base_digest': base.fingerprint,
        'cases': [pareto.get(row['name'], row) for row in results],
        'complete': len(feasible),
        'failed': len(results) - len(feasible),
    }


def marginal_room_capacity(document, room_ids, increment=1, node_limit=100000):
    if isinstance(increment, bool) or not isinstance(increment, int) or increment <= 0:
        raise ValueError('increment must be positive integer')
    catalog = parse(document)
    if len(room_ids) != len(set(room_ids)):
        raise ValueError('duplicate rooms')
    base_result = solve(catalog, node_limit)
    rows = []
    for ident in room_ids:
        if ident not in catalog.rooms:
            raise KeyError(ident)
        room = catalog.rooms[ident]
        modified = apply_changes(catalog.document, [{'table': 'rooms', 'id': ident, 'field': 'seats', 'value': room['seats'] + increment}])
        after = solve(parse(modified), node_limit)
        rows.append({
            'room': ident,
            'increment': increment,
            'before_served': base_result['served'],
            'after_served': after['served'],
            'marginal_served': after['served'] - base_result['served'],
            'cost_change': after['cost'] - base_result['cost'],
        })
    return rows
