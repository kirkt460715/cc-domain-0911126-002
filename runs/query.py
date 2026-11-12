import base64
import json
from estate.catalog import canonical, digest, integer
from assessments.equity import report, aggregate
from allocation.verify import resource_ledger


DIMENSIONS = {'period', 'zone', 'stage', 'kind', 'demand'}
COLUMNS = DIMENSIONS | {'students', 'served', 'shortfall', 'coverage', 'minimum', 'minimum_shortfall', 'mean_minutes', 'cross_zone', 'destination_count', 'reason'}


def encode_cursor(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip('=')


def decode_cursor(value):
    if not isinstance(value, str) or len(value) > 20000:
        raise ValueError('invalid cursor')
    try:
        raw = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
        decoded = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('invalid cursor') from exc
    if not isinstance(decoded, dict):
        raise ValueError('invalid cursor object')
    return decoded


def filter_rows(rows, filters):
    if not isinstance(filters, dict) or filters.keys() - DIMENSIONS:
        raise ValueError('invalid query filters')
    for key, values in filters.items():
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise ValueError('filter values must be unique lists')
        if any(not isinstance(value, str) for value in values):
            raise ValueError('filter values must be strings')
    result = []
    for row in rows:
        if all(row[key] in values for key, values in filters.items()):
            result.append(row)
    return result


def query_report(catalog, result, filters=None, columns=None, limit=100, cursor=None, order='demand'):
    filters = filters or {}
    columns = sorted(COLUMNS) if columns is None else columns
    if not isinstance(columns, list) or not columns or len(columns) != len(set(columns)):
        raise ValueError('projection requires unique columns')
    if set(columns) - COLUMNS:
        raise ValueError('unknown projection column')
    if order not in COLUMNS:
        raise ValueError('invalid ordering column')
    integer(limit, 'limit', 1, 1000)
    rows = filter_rows(report(catalog, result)['demands'], filters)
    rows.sort(key=lambda row: (row[order] is None, row[order], row['demand']))
    context = digest({'result': digest(result), 'filters': filters, 'columns': columns, 'order': order})
    offset = 0
    if cursor is not None:
        token = decode_cursor(cursor)
        if set(token) != {'context', 'offset'} or token['context'] != context:
            raise ValueError('cursor belongs to another report or query')
        offset = integer(token['offset'], 'cursor.offset', 0, len(rows))
    page = rows[offset:offset + limit]
    next_offset = offset + len(page)
    next_cursor = encode_cursor({'context': context, 'offset': next_offset}) if next_offset < len(rows) else None
    return {
        'total': len(rows),
        'offset': offset,
        'rows': [{key: row[key] for key in columns} for row in page],
        'next_cursor': next_cursor,
        'result_digest': digest(result),
    }


def grouped_report(catalog, result, dimensions, filters=None):
    rows = filter_rows(report(catalog, result)['demands'], filters or {})
    return {'result_digest': digest(result), 'groups': aggregate(rows, dimensions)}


def room_report(catalog, result, period=None, building=None, only_saturated=False):
    ledger = resource_ledger(catalog, result)
    if period is not None and period not in catalog.periods:
        raise ValueError('unknown period')
    if building is not None and building not in catalog.buildings:
        raise ValueError('unknown building')
    if not isinstance(only_saturated, bool):
        raise ValueError('only_saturated must be boolean')
    result_rows = []
    for row in ledger['rooms']:
        room = catalog.rooms[row['room']]
        if period is not None and row['period'] != period:
            continue
        if building is not None and room['building'] != building:
            continue
        if only_saturated and row['unused']:
            continue
        result_rows.append({**row, 'building': room['building'], 'zone': catalog.buildings[room['building']]['zone']})
    return sorted(result_rows, key=lambda row: (row['period'], row['building'], row['room']))
