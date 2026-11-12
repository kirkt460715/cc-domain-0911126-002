from dataclasses import dataclass
from datetime import date
import copy
import hashlib
import json
import math


class InvalidPlan(ValueError):
    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__(f'{path}: {message}')


def text(value, path):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise InvalidPlan(path, 'expected nonempty text of at most 128 characters')
    if any(ord(c) < 32 for c in value):
        raise InvalidPlan(path, 'control characters are forbidden')
    return value


def integer(value, path, minimum=0, maximum=10000000):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidPlan(path, 'expected integer')
    if not minimum <= value <= maximum:
        raise InvalidPlan(path, f'expected {minimum}..{maximum}')
    return value


def number(value, path, minimum=0, maximum=1000000):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidPlan(path, 'expected finite number')
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise InvalidPlan(path, 'number out of range')
    return float(value)


def flag(value, path):
    if not isinstance(value, bool):
        raise InvalidPlan(path, 'expected boolean')
    return value


def day(value, path):
    if not isinstance(value, str):
        raise InvalidPlan(path, 'expected ISO date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidPlan(path, 'invalid date') from exc
    if parsed.isoformat() != value:
        raise InvalidPlan(path, 'expected YYYY-MM-DD')
    return value


def fields(row, path, required, optional=()):
    if not isinstance(row, dict):
        raise InvalidPlan(path, 'expected object')
    missing = set(required) - row.keys()
    extra = row.keys() - set(required) - set(optional)
    if missing:
        raise InvalidPlan(path, f'missing fields {sorted(missing)}')
    if extra:
        raise InvalidPlan(path, f'unknown fields {sorted(extra)}')


def rows(value, path, maximum=10000):
    if not isinstance(value, list) or len(value) > maximum:
        raise InvalidPlan(path, f'expected list with at most {maximum} rows')
    return value


def unique(items, path, key='id'):
    result = {}
    for index, item in enumerate(items):
        ident = item[key]
        if ident in result:
            raise InvalidPlan(f'{path}[{index}].{key}', 'duplicate identifier')
        result[ident] = item
    return result


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class Catalog:
    document: dict
    zones: dict
    stages: dict
    buildings: dict
    rooms: dict
    periods: dict
    demands: dict
    routes: dict
    fingerprint: str

    def detached(self):
        return copy.deepcopy(self.document)


def parse(document):
    data = copy.deepcopy(document)
    fields(data, '$', ('version', 'zones', 'stages', 'buildings', 'rooms', 'periods', 'demands', 'routes'), ('policy',))
    if data['version'] != 1 or isinstance(data['version'], bool):
        raise InvalidPlan('version', 'only version 1 is supported')
    for name in ('zones', 'stages', 'buildings', 'rooms', 'periods', 'demands', 'routes'):
        rows(data[name], name)
    for i, row in enumerate(data['zones']):
        p = f'zones[{i}]'
        fields(row, p, ('id', 'kind'))
        text(row['id'], p + '.id')
        if row['kind'] not in ('urban', 'rural'):
            raise InvalidPlan(p + '.kind', 'expected urban or rural')
    zones = unique(data['zones'], 'zones')
    for i, row in enumerate(data['stages']):
        p = f'stages[{i}]'
        fields(row, p, ('id', 'class_limit', 'area_per_student', 'required_features'))
        text(row['id'], p + '.id')
        integer(row['class_limit'], p + '.class_limit', 1, 100)
        number(row['area_per_student'], p + '.area_per_student', 0.1, 100)
        rows(row['required_features'], p + '.required_features', 50)
        for feature in row['required_features']:
            text(feature, p + '.required_features')
        if len(set(row['required_features'])) != len(row['required_features']):
            raise InvalidPlan(p, 'duplicate feature')
    stages = unique(data['stages'], 'stages')
    for i, row in enumerate(data['periods']):
        p = f'periods[{i}]'
        fields(row, p, ('id', 'start', 'end'))
        text(row['id'], p + '.id')
        day(row['start'], p + '.start')
        day(row['end'], p + '.end')
        if row['start'] >= row['end']:
            raise InvalidPlan(p, 'period is an empty or reversed half-open interval')
    periods = unique(data['periods'], 'periods')
    for i, row in enumerate(data['buildings']):
        p = f'buildings[{i}]'
        fields(row, p, ('id', 'zone', 'open', 'close', 'seat_limit', 'closures'))
        text(row['id'], p + '.id')
        if row['zone'] not in zones:
            raise InvalidPlan(p + '.zone', 'unknown zone')
        day(row['open'], p + '.open')
        day(row['close'], p + '.close')
        if row['open'] >= row['close']:
            raise InvalidPlan(p, 'building validity must be positive')
        integer(row['seat_limit'], p + '.seat_limit')
        for j, closure in enumerate(rows(row['closures'], p + '.closures')):
            q = f'{p}.closures[{j}]'
            fields(closure, q, ('start', 'end', 'reason'))
            day(closure['start'], q + '.start')
            day(closure['end'], q + '.end')
            text(closure['reason'], q + '.reason')
            if closure['start'] >= closure['end']:
                raise InvalidPlan(q, 'closure must have positive duration')
    buildings = unique(data['buildings'], 'buildings')
    for i, row in enumerate(data['rooms']):
        p = f'rooms[{i}]'
        fields(row, p, ('id', 'building', 'area', 'seats', 'features', 'allowed_stages', 'periods'))
        text(row['id'], p + '.id')
        if row['building'] not in buildings:
            raise InvalidPlan(p + '.building', 'unknown building')
        number(row['area'], p + '.area', 0.1, 100000)
        integer(row['seats'], p + '.seats', 1, 1000)
        for name, valid in (('allowed_stages', stages), ('periods', periods)):
            values = rows(row[name], p + '.' + name)
            if len(values) != len(set(values)):
                raise InvalidPlan(p + '.' + name, 'duplicate reference')
            if any(v not in valid for v in values):
                raise InvalidPlan(p + '.' + name, 'unknown reference')
        for feature in rows(row['features'], p + '.features', 50):
            text(feature, p + '.features')
        if len(row['features']) != len(set(row['features'])):
            raise InvalidPlan(p + '.features', 'duplicate feature')
    rooms_by_id = unique(data['rooms'], 'rooms')
    for i, row in enumerate(data['demands']):
        p = f'demands[{i}]'
        fields(row, p, ('id', 'zone', 'stage', 'period', 'students', 'max_minutes', 'priority'), ('minimum',))
        text(row['id'], p + '.id')
        for key, valid in (('zone', zones), ('stage', stages), ('period', periods)):
            if row[key] not in valid:
                raise InvalidPlan(p + '.' + key, 'unknown reference')
        integer(row['students'], p + '.students')
        integer(row['priority'], p + '.priority', 0, 1000)
        number(row['max_minutes'], p + '.max_minutes', 0, 10000)
        minimum = integer(row.get('minimum', 0), p + '.minimum')
        if minimum > row['students']:
            raise InvalidPlan(p + '.minimum', 'minimum exceeds demand')
    demands = unique(data['demands'], 'demands')
    routes = {}
    for i, row in enumerate(data['routes']):
        p = f'routes[{i}]'
        fields(row, p, ('from', 'to', 'minutes'))
        if row['from'] not in zones or row['to'] not in zones:
            raise InvalidPlan(p, 'route endpoints must be known zones')
        number(row['minutes'], p + '.minutes', 0, 10000)
        key = (row['from'], row['to'])
        if key in routes:
            raise InvalidPlan(p, 'duplicate directed route')
        routes[key] = row['minutes']
    policy = data.setdefault('policy', {})
    fields(policy, 'policy', (), ('travel_cost', 'priority_cost', 'require_minima'))
    integer(policy.get('travel_cost', 1), 'policy.travel_cost', 0, 10000)
    integer(policy.get('priority_cost', 10000), 'policy.priority_cost', 0, 1000000)
    flag(policy.get('require_minima', True), 'policy.require_minima')
    return Catalog(data, zones, stages, buildings, rooms_by_id, periods, demands, routes, digest(data))
