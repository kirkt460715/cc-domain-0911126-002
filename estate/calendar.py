from datetime import date
from estate.catalog import InvalidPlan


def overlap(left_start, left_end, right_start, right_end):
    return left_start < right_end and right_start < left_end


def intersection(left, right):
    start = max(left[0], right[0])
    end = min(left[1], right[1])
    if start >= end:
        return None
    return start, end


def merged_intervals(intervals):
    result = []
    for start, end in sorted(intervals):
        if start >= end:
            raise InvalidPlan('calendar', 'invalid interval')
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(end, result[-1][1])
        else:
            result.append((start, end))
    return result


def subtract_intervals(window, closures):
    pieces = [window]
    for closure in merged_intervals(closures):
        next_pieces = []
        for start, end in pieces:
            cut = intersection((start, end), closure)
            if cut is None:
                next_pieces.append((start, end))
                continue
            if start < cut[0]:
                next_pieces.append((start, cut[0]))
            if cut[1] < end:
                next_pieces.append((cut[1], end))
        pieces = next_pieces
    return pieces


def building_status(building, period):
    if period['start'] < building['open']:
        return 'not_open'
    if period['end'] > building['close']:
        return 'expired'
    for closure in building['closures']:
        if overlap(period['start'], period['end'], closure['start'], closure['end']):
            return 'closed'
    return 'available'


def atomic_windows(catalog):
    boundaries = sorted({p[key] for p in catalog.periods.values() for key in ('start', 'end')})
    result = []
    for index in range(len(boundaries) - 1):
        start, end = boundaries[index:index + 2]
        covering = sorted(p['id'] for p in catalog.periods.values() if p['start'] <= start and end <= p['end'])
        if covering:
            result.append({'start': start, 'end': end, 'periods': covering})
    return result


def conflicting_periods(catalog):
    conflicts = set()
    periods = sorted(catalog.periods.values(), key=lambda p: p['id'])
    for i, left in enumerate(periods):
        for right in periods[i + 1:]:
            if overlap(left['start'], left['end'], right['start'], right['end']):
                conflicts.add((left['id'], right['id']))
    return conflicts


def assert_independent_periods(catalog):
    conflicts = conflicting_periods(catalog)
    if conflicts:
        raise InvalidPlan('periods', f'overlapping planning periods would reuse room inventory: {sorted(conflicts)}')


def availability_report(catalog):
    result = []
    for building in sorted(catalog.buildings.values(), key=lambda b: b['id']):
        for period in sorted(catalog.periods.values(), key=lambda p: p['id']):
            window = intersection((building['open'], building['close']), (period['start'], period['end']))
            available_days = 0
            if window:
                closures = [(c['start'], c['end']) for c in building['closures']]
                for start, end in subtract_intervals(window, closures):
                    available_days += (date.fromisoformat(end) - date.fromisoformat(start)).days
            total_days = (date.fromisoformat(period['end']) - date.fromisoformat(period['start'])).days
            result.append({
                'building': building['id'],
                'period': period['id'],
                'status': building_status(building, period),
                'available_days': available_days,
                'total_days': total_days,
                'availability': available_days / total_days,
            })
    return result


def closure_impact(catalog):
    result = []
    for building in catalog.buildings.values():
        relevant = [r for r in catalog.rooms.values() if r['building'] == building['id']]
        affected = []
        for period in catalog.periods.values():
            if building_status(building, period) == 'closed':
                affected.append(period['id'])
        if affected:
            result.append({
                'building': building['id'],
                'periods': sorted(affected),
                'rooms': len(relevant),
                'nominal_seats': sum(r['seats'] for r in relevant),
                'building_limit': building['seat_limit'],
            })
    return sorted(result, key=lambda row: row['building'])
