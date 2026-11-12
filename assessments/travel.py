from collections import defaultdict
from estate.catalog import parse, number
from allocation.verify import require_valid_result
from allocation.solver import solve, InfeasibleMinimum


def travel_distribution(catalog, result, boundaries=(0, 10, 20, 30, 60)):
    require_valid_result(catalog, result)
    values = list(boundaries)
    if not values or values != sorted(set(values)):
        raise ValueError('travel boundaries must be nonempty, increasing and unique')
    for value in values:
        number(value, 'boundary', 0, 10000)
    buckets = [0] * (len(values) + 1)
    weighted = []
    for item in result['assignments']:
        minutes = item['minutes']
        index = next((i for i, boundary in enumerate(values) if minutes <= boundary), len(values))
        buckets[index] += item['students']
        weighted.append((minutes, item['students']))
    count = sum(weight for _, weight in weighted)
    total_minutes = sum(value * weight for value, weight in weighted)
    ordered = sorted(weighted)

    def quantile(fraction):
        if count == 0:
            return None
        target = max(1, __import__('math').ceil(fraction * count))
        cumulative = 0
        for value, weight in ordered:
            cumulative += weight
            if cumulative >= target:
                return value
        raise AssertionError('weighted quantile missing')

    return {
        'students': count,
        'mean': total_minutes / count if count else None,
        'median': quantile(0.5),
        'p90': quantile(0.9),
        'maximum': max((value for value, weight in weighted), default=None),
        'buckets': [
            {'lower_exclusive': None if i == 0 else values[i - 1], 'upper_inclusive': values[i] if i < len(values) else None, 'students': amount}
            for i, amount in enumerate(buckets)
        ],
    }


def zone_flows(catalog, result):
    require_valid_result(catalog, result)
    flows = defaultdict(lambda: {'students': 0, 'student_minutes': 0})
    for item in result['assignments']:
        demand = catalog.demands[item['demand']]
        destination = catalog.buildings[item['building']]['zone']
        key = item['period'], demand['zone'], destination, item['stage']
        flows[key]['students'] += item['students']
        flows[key]['student_minutes'] += item['students'] * item['minutes']
    return [
        {
            'period': key[0],
            'origin': key[1],
            'destination': key[2],
            'stage': key[3],
            **value,
            'mean_minutes': value['student_minutes'] / value['students'],
        }
        for key, value in sorted(flows.items())
    ]


def threshold_curve(document, thresholds, node_limit=100000):
    catalog = parse(document)
    if not isinstance(thresholds, list) or len(thresholds) > 100:
        raise ValueError('expected at most 100 travel thresholds')
    if thresholds != sorted(set(thresholds)):
        raise ValueError('thresholds must be increasing and unique')
    result = []
    previous_served = None
    for threshold in thresholds:
        number(threshold, 'threshold', 0, 10000)
        data = catalog.detached()
        for row in data['demands']:
            row['max_minutes'] = threshold
        modified = parse(data)
        try:
            allocation = solve(modified, node_limit)
        except InfeasibleMinimum as exc:
            result.append({'threshold': threshold, 'state': 'infeasible', 'shortfalls': exc.shortfalls})
            continue
        if previous_served is not None and allocation['served'] < previous_served:
            raise AssertionError('expanded travel eligibility reduced maximum service')
        result.append({
            'threshold': threshold,
            'state': 'complete',
            'served': allocation['served'],
            'additional_served': None if previous_served is None else allocation['served'] - previous_served,
            'travel': travel_distribution(modified, allocation),
        })
        previous_served = allocation['served']
    return {'base_digest': catalog.fingerprint, 'curve': result}
