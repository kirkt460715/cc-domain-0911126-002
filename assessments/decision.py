from collections import defaultdict
from estate.catalog import parse, digest
from allocation.verify import require_valid_result
from allocation.certificate import certificate, capacity_waterfall
from assessments.equity import report
from assessments.quality import bottleneck_map


def readiness(catalog, result, min_coverage=0, max_travel=None, maximum_gap=None):
    from estate.catalog import number
    number(min_coverage, 'min_coverage', 0, 1)
    if max_travel is not None:
        number(max_travel, 'max_travel', 0, 10000)
    if maximum_gap is not None:
        number(maximum_gap, 'maximum_gap', 0, 1)
    require_valid_result(catalog, result)
    equity = report(catalog, result)
    findings = []
    for row in equity['demands']:
        if row['coverage'] is not None and row['coverage'] < min_coverage:
            findings.append({'code': 'coverage_below_threshold', 'demand': row['demand'], 'actual': row['coverage'], 'required': min_coverage})
        if row['minimum_shortfall']:
            findings.append({'code': 'mandatory_minimum_unmet', 'demand': row['demand'], 'missing': row['minimum_shortfall']})
    if max_travel is not None:
        for assignment in result['assignments']:
            if assignment['minutes'] > max_travel:
                findings.append({'code': 'travel_above_review_threshold', 'demand': assignment['demand'], 'room': assignment['room'], 'actual': assignment['minutes'], 'maximum': max_travel})
    if maximum_gap is not None:
        for row in equity['rural_urban_gap']:
            gap = row['urban_minus_rural']
            if gap is not None and abs(gap) > maximum_gap:
                findings.append({'code': 'rural_urban_gap', 'period': row['period'], 'actual': gap, 'maximum_absolute': maximum_gap})
    return {
        'input_digest': catalog.fingerprint,
        'result_digest': digest(result),
        'meets_review_thresholds': not findings,
        'findings': findings,
        'thresholds': {'minimum_coverage': min_coverage, 'maximum_travel': max_travel, 'maximum_absolute_gap': maximum_gap},
    }


def review_packet(catalog, result, thresholds=None):
    thresholds = thresholds or {}
    if set(thresholds) - {'min_coverage', 'max_travel', 'maximum_gap'}:
        raise ValueError('unknown review threshold')
    equity = report(catalog, result)
    bottlenecks = bottleneck_map(catalog, result)
    waterfall = capacity_waterfall(catalog)
    causes = defaultdict(int)
    for item in bottlenecks:
        distinct = {candidate['cause'] for candidate in item['candidates']}
        for cause in distinct:
            causes[cause] += 1
    evidence = certificate(catalog, result)
    return {
        'input_digest': catalog.fingerprint,
        'result_digest': digest(result),
        'review': readiness(catalog, result, **thresholds),
        'equity': equity,
        'bottlenecks': bottlenecks,
        'capacity_waterfall': waterfall,
        'bottleneck_group_counts': dict(sorted(causes.items())),
        'certificate': evidence,
        'interpretation': {
            'capacity_stage_rows_are_alternatives': True,
            'missing_population_cells_are_not_zero': True,
            'travel_routes_are_directed_explicit_pairs': True,
            'review_thresholds_do_not_change_optimization': True,
        },
    }


def prioritize_shortfalls(catalog, result):
    equity = report(catalog, result)
    rows = []
    for item in equity['demands']:
        if item['shortfall'] <= 0:
            continue
        demand = catalog.demands[item['demand']]
        rows.append({
            'demand': item['demand'],
            'period': item['period'],
            'zone': item['zone'],
            'kind': item['kind'],
            'priority': demand['priority'],
            'shortfall': item['shortfall'],
            'coverage': item['coverage'],
            'minimum_shortfall': item['minimum_shortfall'],
            'reason': item['reason'],
        })
    rows.sort(key=lambda row: (-row['minimum_shortfall'], -row['priority'], row['coverage'], -row['shortfall'], row['demand']))
    return [{**row, 'review_rank': index + 1} for index, row in enumerate(rows)]
