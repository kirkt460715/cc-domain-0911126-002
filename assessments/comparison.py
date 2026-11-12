from estate.catalog import parse
from assessments.equity import report, ratio
from runs.variants import difference


def compare(left_catalog, left_result, right_catalog, right_result):
    left = report(left_catalog, left_result)
    right = report(right_catalog, right_result)
    a = {r['demand']: r for r in left['demands']}
    b = {r['demand']: r for r in right['demands']}
    paired = []
    excluded = []
    for ident in sorted(a.keys() | b.keys()):
        if ident not in a or ident not in b:
            excluded.append({'demand': ident, 'reason': 'unpaired'})
            continue
        old, new = a[ident], b[ident]
        if any(old[k] != new[k] for k in ('zone', 'stage', 'period')):
            excluded.append({'demand': ident, 'reason': 'changed_population_definition'})
            continue
        paired.append({
            'demand': ident,
            'before_students': old['students'],
            'after_students': new['students'],
            'before_served': old['served'],
            'after_served': new['served'],
            'served_change': new['served'] - old['served'],
            'shortfall_change': new['shortfall'] - old['shortfall'],
            'coverage_change': None if old['coverage'] is None or new['coverage'] is None else new['coverage'] - old['coverage'],
            'travel_change': None if old['mean_minutes'] is None or new['mean_minutes'] is None else new['mean_minutes'] - old['mean_minutes'],
        })
    reassigned = []
    old_links = {(r['demand'], r['room']): r['students'] for r in left_result['assignments']}
    new_links = {(r['demand'], r['room']): r['students'] for r in right_result['assignments']}
    for key in sorted(old_links.keys() | new_links.keys()):
        before, after = old_links.get(key, 0), new_links.get(key, 0)
        if before != after:
            reassigned.append({'demand': key[0], 'room': key[1], 'before': before, 'after': after, 'change': after - before})
    return {
        'before_digest': left_catalog.fingerprint,
        'after_digest': right_catalog.fingerprint,
        'input_changes': difference(left_catalog.document, right_catalog.document),
        'paired': paired,
        'excluded': excluded,
        'reassignments': reassigned,
        'served_change_all': right_result['served'] - left_result['served'],
        'served_change_paired': sum(r['served_change'] for r in paired),
        'improved_groups': sum(r['coverage_change'] is not None and r['coverage_change'] > 0 for r in paired),
        'worsened_groups': sum(r['coverage_change'] is not None and r['coverage_change'] < 0 for r in paired),
    }


def frontier(cases):
    result = []
    for index, case in enumerate(cases):
        dominated_by = []
        for other_index, other in enumerate(cases):
            if index == other_index:
                continue
            weak = other['served'] >= case['served'] and other['cost'] <= case['cost']
            strict = other['served'] > case['served'] or other['cost'] < case['cost']
            if weak and strict:
                dominated_by.append(other['name'])
        result.append({**case, 'pareto': not dominated_by, 'dominated_by': sorted(dominated_by)})
    return sorted(result, key=lambda row: row['name'])
