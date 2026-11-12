import copy
from estate.catalog import parse, InvalidPlan, fields, integer, number


def apply_changes(document, changes):
    catalog = parse(document)
    data = catalog.detached()
    if not isinstance(changes, list) or len(changes) > 1000:
        raise InvalidPlan('changes', 'expected at most 1000 changes')
    touched = set()
    for index, change in enumerate(changes):
        path = f'changes[{index}]'
        fields(change, path, ('table', 'id', 'field', 'value'))
        table = change['table']
        field = change['field']
        allowed = {
            'demands': {'students', 'minimum', 'max_minutes', 'priority'},
            'rooms': {'seats', 'area', 'features', 'allowed_stages', 'periods'},
            'buildings': {'seat_limit', 'closures', 'open', 'close'},
            'stages': {'class_limit', 'area_per_student', 'required_features'},
        }
        if table not in allowed or field not in allowed[table]:
            raise InvalidPlan(path, 'field cannot be modified in a scenario change')
        key = (table, change['id'], field)
        if key in touched:
            raise InvalidPlan(path, 'duplicate change target')
        touched.add(key)
        matches = [row for row in data[table] if row['id'] == change['id']]
        if not matches:
            raise InvalidPlan(path, 'unknown change target')
        matches[0][field] = copy.deepcopy(change['value'])
    return parse(data).document


def difference(left, right):
    a, b = parse(left), parse(right)
    changes = []
    for table in ('zones', 'stages', 'buildings', 'rooms', 'periods', 'demands'):
        old = {row['id']: row for row in a.document[table]}
        new = {row['id']: row for row in b.document[table]}
        for ident in sorted(old.keys() | new.keys()):
            if ident not in old:
                changes.append({'table': table, 'id': ident, 'kind': 'added', 'value': new[ident]})
            elif ident not in new:
                changes.append({'table': table, 'id': ident, 'kind': 'removed', 'value': old[ident]})
            else:
                for field in sorted(old[ident].keys() | new[ident].keys()):
                    if old[ident].get(field) != new[ident].get(field):
                        changes.append({
                            'table': table,
                            'id': ident,
                            'kind': 'changed',
                            'field': field,
                            'before': old[ident].get(field),
                            'after': new[ident].get(field),
                        })
    if a.document['routes'] != b.document['routes']:
        changes.append({'table': 'routes', 'kind': 'changed', 'before': a.document['routes'], 'after': b.document['routes']})
    if a.document['policy'] != b.document['policy']:
        changes.append({'table': 'policy', 'kind': 'changed', 'before': a.document['policy'], 'after': b.document['policy']})
    return changes


def scale_demands(document, numerator, denominator):
    integer(numerator, 'numerator', 0, 10000)
    integer(denominator, 'denominator', 1, 10000)
    data = parse(document).detached()
    groups = {}
    for row in data['demands']:
        groups.setdefault(row['period'], []).append(row)
    rounding = []
    for period, rows in sorted(groups.items()):
        original = sum(row['students'] for row in rows)
        target = (original * numerator + denominator // 2) // denominator
        base = {row['id']: row['students'] * numerator // denominator for row in rows}
        remainders = sorted(rows, key=lambda row: (-(row['students'] * numerator % denominator), row['id']))
        residual = target - sum(base.values())
        for row in remainders[:residual]:
            base[row['id']] += 1
        for row in rows:
            before = row['students']
            row['students'] = base[row['id']]
            if row.get('minimum', 0) > row['students']:
                raise InvalidPlan('minimum', f'scaled demand {row["id"]} falls below its mandatory minimum')
            rounding.append({'demand': row['id'], 'before': before, 'after': row['students']})
        if sum(row['students'] for row in rows) != target:
            raise AssertionError('largest remainder scaling did not conserve rounded total')
    return parse(data).document, rounding


def scenario_lineage(store, ident):
    seen = set()
    result = []
    while ident is not None:
        if ident in seen:
            raise ValueError('scenario parent cycle')
        seen.add(ident)
        current = store.scenario(ident)
        result.append({'id': ident, 'digest': current['digest'], 'state': current['state'], 'revision': current['revision']})
        ident = current['parent']
    result.reverse()
    return result
