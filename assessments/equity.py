from collections import defaultdict
from estate.compatibility import compatible_edges, diagnose_demand
from allocation.verify import require_valid_result


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def weighted_gini(values):
    total_weight = sum(weight for value, weight in values)
    weighted_total = sum(value * weight for value, weight in values)
    if total_weight == 0 or weighted_total == 0:
        return None
    ordered = sorted(values)
    weight_before = 0
    value_before = 0
    difference = 0
    for value, weight in ordered:
        difference += weight * (value * weight_before - value_before)
        weight_before += weight
        value_before += value * weight
    return difference / (total_weight * weighted_total)


def demand_report(catalog, result):
    require_valid_result(catalog, result)
    served = defaultdict(int)
    travel = defaultdict(float)
    destinations = defaultdict(set)
    cross_zone = defaultdict(int)
    for item in result['assignments']:
        ident = item['demand']
        served[ident] += item['students']
        travel[ident] += item['students'] * item['minutes']
        destinations[ident].add(item['building'])
        if catalog.demands[ident]['zone'] != catalog.buildings[item['building']]['zone']:
            cross_zone[ident] += item['students']
    edges = compatible_edges(catalog)
    report = []
    for ident, demand in sorted(catalog.demands.items()):
        analysis = diagnose_demand(catalog, edges, ident)
        count = served[ident]
        shortfall = demand['students'] - count
        if not shortfall:
            reason = 'satisfied'
        elif not analysis['compatible_rooms']:
            reason = 'no_compatible_room'
        elif analysis['structural_shortfall']:
            reason = 'individual_capacity_and_competition'
        else:
            reason = 'shared_capacity_or_stage_competition'
        report.append({
            'demand': ident,
            'zone': demand['zone'],
            'kind': catalog.zones[demand['zone']]['kind'],
            'stage': demand['stage'],
            'period': demand['period'],
            'students': demand['students'],
            'served': count,
            'shortfall': shortfall,
            'coverage': ratio(count, demand['students']),
            'minimum': demand.get('minimum', 0),
            'minimum_shortfall': max(0, demand.get('minimum', 0) - count),
            'mean_minutes': ratio(travel[ident], count),
            'cross_zone': cross_zone[ident],
            'destination_count': len(destinations[ident]),
            'reason': reason,
            'eligibility': analysis,
        })
    return report


def aggregate(report, dimensions):
    allowed = {'zone', 'kind', 'stage', 'period'}
    if len(dimensions) != len(set(dimensions)) or set(dimensions) - allowed:
        raise ValueError('invalid aggregation dimensions')
    groups = {}
    for row in report:
        key = tuple(row[d] for d in dimensions)
        group = groups.setdefault(key, {
            **dict(zip(dimensions, key)),
            'students': 0,
            'served': 0,
            'shortfall': 0,
            'minimum_shortfall': 0,
            'cross_zone': 0,
            'travel_student_minutes': 0,
            'demand_count': 0,
        })
        for metric in ('students', 'served', 'shortfall', 'minimum_shortfall', 'cross_zone'):
            group[metric] += row[metric]
        group['travel_student_minutes'] += (row['mean_minutes'] or 0) * row['served']
        group['demand_count'] += 1
    result = []
    for key in sorted(groups):
        group = groups[key]
        group['coverage'] = ratio(group['served'], group['students'])
        group['mean_minutes'] = ratio(group['travel_student_minutes'], group['served'])
        group['cross_zone_rate'] = ratio(group['cross_zone'], group['served'])
        result.append(group)
    return result


def rural_urban_gap(report):
    grouped = aggregate(report, ['period', 'kind'])
    periods = sorted({r['period'] for r in report})
    result = []
    for period in periods:
        groups = {r['kind']: r for r in grouped if r['period'] == period}
        rural = groups.get('rural', {}).get('coverage')
        urban = groups.get('urban', {}).get('coverage')
        gap = None if rural is None or urban is None else urban - rural
        result.append({'period': period, 'rural': rural, 'urban': urban, 'urban_minus_rural': gap})
    return result


def coverage_distribution(report):
    available = [(r['coverage'], r['students']) for r in report if r['coverage'] is not None]
    if not available:
        return {'minimum': None, 'maximum': None, 'weighted_gini': None, 'unserved_groups': 0}
    return {
        'minimum': min(v for v, _ in available),
        'maximum': max(v for v, _ in available),
        'weighted_gini': weighted_gini(available),
        'unserved_groups': sum(r['students'] > 0 and r['served'] == 0 for r in report),
    }


def report(catalog, result):
    demands = demand_report(catalog, result)
    return {
        'input_digest': catalog.fingerprint,
        'demands': demands,
        'by_zone': aggregate(demands, ['period', 'zone']),
        'by_stage': aggregate(demands, ['period', 'stage']),
        'by_kind': aggregate(demands, ['period', 'kind']),
        'rural_urban_gap': rural_urban_gap(demands),
        'coverage_distribution': coverage_distribution(demands),
        'total': aggregate(demands, []),
    }
