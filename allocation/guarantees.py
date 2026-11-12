from estate.catalog import parse, integer
from allocation.solver import solve, InfeasibleMinimum
from assessments.equity import report


def with_guarantee(document, basis_points, selected=None):
    integer(basis_points, 'basis_points', 0, 10000)
    catalog = parse(document)
    selected_ids = set(catalog.demands) if selected is None else set(selected)
    if selected is not None and len(selected_ids) != len(selected):
        raise ValueError('duplicate demand selection')
    unknown = selected_ids - catalog.demands.keys()
    if unknown:
        raise ValueError('unknown guarantee demand: ' + ','.join(sorted(unknown)))
    data = catalog.detached()
    data['policy']['require_minima'] = True
    for row in data['demands']:
        if row['id'] in selected_ids:
            guarantee = (row['students'] * basis_points + 9999) // 10000
            row['minimum'] = max(row.get('minimum', 0), guarantee)
    return parse(data)


def maximize_guarantee(document, selected=None, node_limit=100000):
    catalog = parse(document)
    ids = sorted(catalog.demands) if selected is None else sorted(selected)
    if not ids:
        raise ValueError('select at least one demand')
    if not any(catalog.demands[ident]['students'] > 0 for ident in ids if ident in catalog.demands):
        raise ValueError('guarantee requires positive selected demand')
    lower, upper = 0, 10000
    attempts = []
    best = None
    while lower <= upper:
        middle = (lower + upper) // 2
        trial = with_guarantee(catalog.document, middle, ids)
        try:
            result = solve(trial, node_limit)
        except InfeasibleMinimum as exc:
            attempts.append({'basis_points': middle, 'feasible': False, 'shortfalls': exc.shortfalls})
            upper = middle - 1
        else:
            attempts.append({'basis_points': middle, 'feasible': True, 'served': result['served']})
            best = trial, result, middle
            lower = middle + 1
    if best is None:
        raise InfeasibleMinimum({'existing_minima': 'infeasible_even_at_zero_guarantee'})
    trial, result, optimum = best
    evidence = []
    for row in report(trial, result)['demands']:
        if row['demand'] in ids:
            evidence.append({
                'demand': row['demand'],
                'students': row['students'],
                'required': trial.demands[row['demand']].get('minimum', 0),
                'served': row['served'],
                'coverage': row['coverage'],
            })
    return {
        'basis_points': optimum,
        'resolution': 'one_basis_point',
        'selected': ids,
        'attempts': attempts,
        'document': trial.document,
        'result': result,
        'evidence': evidence,
    }


def priority_service(catalog, result):
    from allocation.verify import require_valid_result
    require_valid_result(catalog, result)
    served = {ident: 0 for ident in catalog.demands}
    for item in result['assignments']:
        served[item['demand']] += item['students']
    levels = {}
    for ident, row in catalog.demands.items():
        level = levels.setdefault(row['priority'], {'priority': row['priority'], 'students': 0, 'served': 0})
        level['students'] += row['students']
        level['served'] += served[ident]
    result_rows = []
    cumulative_demand = 0
    cumulative_served = 0
    for priority in sorted(levels, reverse=True):
        row = levels[priority]
        cumulative_demand += row['students']
        cumulative_served += row['served']
        result_rows.append({
            **row,
            'coverage': row['served'] / row['students'] if row['students'] else None,
            'cumulative_students': cumulative_demand,
            'cumulative_served': cumulative_served,
            'cumulative_coverage': cumulative_served / cumulative_demand if cumulative_demand else None,
        })
    return result_rows
