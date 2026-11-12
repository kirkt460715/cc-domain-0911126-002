import csv
import io
import json
from pathlib import Path
from estate.catalog import parse, InvalidPlan, canonical


SCHEMAS = {
    'zones': {'id': 'text', 'kind': 'text'},
    'stages': {'id': 'text', 'class_limit': 'int', 'area_per_student': 'float', 'required_features': 'json'},
    'buildings': {'id': 'text', 'zone': 'text', 'open': 'text', 'close': 'text', 'seat_limit': 'int', 'closures': 'json'},
    'rooms': {'id': 'text', 'building': 'text', 'area': 'float', 'seats': 'int', 'features': 'json', 'allowed_stages': 'json', 'periods': 'json'},
    'periods': {'id': 'text', 'start': 'text', 'end': 'text'},
    'demands': {'id': 'text', 'zone': 'text', 'stage': 'text', 'period': 'text', 'students': 'int', 'max_minutes': 'float', 'priority': 'int', 'minimum': 'int'},
    'routes': {'from': 'text', 'to': 'text', 'minutes': 'float'},
}


def decode_cell(value, kind, path):
    if kind == 'text':
        return value
    try:
        if kind == 'int':
            if not value or value.strip() != value or any(c not in '-0123456789' for c in value):
                raise ValueError('not a decimal integer')
            return int(value)
        if kind == 'float':
            if value.strip() != value or not value:
                raise ValueError('empty or padded numeric cell')
            return float(value)
        if kind == 'json':
            return json.loads(value)
    except (ValueError, TypeError) as exc:
        raise InvalidPlan(path, f'invalid {kind} cell') from exc
    raise InvalidPlan(path, 'unknown cell type')


def load_table(content, table):
    if table not in SCHEMAS:
        raise InvalidPlan('table', 'unknown table')
    if not isinstance(content, str) or len(content.encode()) > 20_000_000:
        raise InvalidPlan(table, 'table too large or not text')
    schema = SCHEMAS[table]
    try:
        reader = csv.reader(io.StringIO(content), strict=True)
        header = next(reader, None)
        if header is None:
            raise InvalidPlan(table, 'missing CSV header')
        if len(header) != len(set(header)):
            raise InvalidPlan(table, 'duplicate CSV column')
        required = set(schema)
        if table == 'demands':
            required.remove('minimum')
        if not required <= set(header) or set(header) - schema.keys():
            raise InvalidPlan(table, 'CSV columns do not match schema')
        result = []
        for index, values in enumerate(reader, 2):
            if len(values) != len(header):
                raise InvalidPlan(f'{table}:{index}', 'wrong CSV row width')
            row = {}
            for field, value in zip(header, values):
                row[field] = decode_cell(value, schema[field], f'{table}:{index}.{field}')
            result.append(row)
            if len(result) > 10000:
                raise InvalidPlan(table, 'too many rows')
        return result
    except csv.Error as exc:
        raise InvalidPlan(table, f'malformed CSV: {exc}') from exc


def dump_table(document, table):
    catalog = parse(document)
    if table not in SCHEMAS:
        raise InvalidPlan('table', 'unknown table')
    schema = SCHEMAS[table]
    stream = io.StringIO(newline='')
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(schema)
    for row in catalog.document[table]:
        cells = []
        for field, kind in schema.items():
            value = row.get(field, 0 if field == 'minimum' else None)
            if kind == 'json':
                cells.append(canonical(value))
            else:
                cells.append(value)
        writer.writerow(cells)
    return stream.getvalue()


def load_directory(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise InvalidPlan('directory', 'expected real directory')
    expected = {name + '.csv' for name in SCHEMAS} | {'policy.json'}
    actual = {path.name for path in root.iterdir()}
    if actual != expected:
        raise InvalidPlan('directory', f'expected files {sorted(expected)}')
    data = {'version': 1}
    for table in SCHEMAS:
        path = root / (table + '.csv')
        if path.is_symlink() or not path.is_file():
            raise InvalidPlan(table, 'expected regular CSV file')
        if path.stat().st_size > 20_000_000:
            raise InvalidPlan(table, 'table too large')
        data[table] = load_table(path.read_text(encoding='utf-8-sig'), table)
    policy_path = root / 'policy.json'
    if policy_path.is_symlink() or not policy_path.is_file() or policy_path.stat().st_size > 10000:
        raise InvalidPlan('policy', 'expected small regular policy file')
    try:
        data['policy'] = json.loads(policy_path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError) as exc:
        raise InvalidPlan('policy', 'invalid JSON policy') from exc
    return parse(data)


def write_directory(document, directory):
    catalog = parse(document)
    root = Path(directory)
    if root.exists() or root.is_symlink():
        raise FileExistsError(root)
    root.mkdir(parents=True)
    written = []
    try:
        for table in SCHEMAS:
            path = root / (table + '.csv')
            path.write_text(dump_table(catalog.document, table), encoding='utf-8')
            written.append(path)
        policy = root / 'policy.json'
        policy.write_text(canonical(catalog.document['policy']), encoding='utf-8')
        written.append(policy)
    except BaseException:
        for path in written:
            path.unlink()
        root.rmdir()
        raise
    return sorted(path.name for path in written)
