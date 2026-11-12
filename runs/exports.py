import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from estate.catalog import canonical, digest, parse
from allocation.verify import require_valid_result, resource_ledger
from assessments.equity import report
from assessments.quality import bottleneck_map


def csv_rows(rows, columns):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in columns})
    return stream.getvalue()


def export_payload(catalog, result):
    require_valid_result(catalog, result)
    equity = report(catalog, result)
    ledger = resource_ledger(catalog, result)
    payload = {
        'input.json': canonical(catalog.document),
        'allocation.json': canonical(result),
        'equity.json': canonical(equity),
        'resources.json': canonical(ledger),
        'bottlenecks.json': canonical(bottleneck_map(catalog, result)),
        'assignments.csv': csv_rows(result['assignments'], ['period', 'demand', 'stage', 'room', 'building', 'students', 'minutes']),
        'demand-coverage.csv': csv_rows(equity['demands'], ['period', 'demand', 'zone', 'kind', 'stage', 'students', 'served', 'shortfall', 'coverage', 'mean_minutes', 'reason']),
        'room-usage.csv': csv_rows(ledger['rooms'], ['period', 'room', 'stage', 'capacity', 'used', 'unused']),
    }
    manifest = {
        'version': 1,
        'input_digest': catalog.fingerprint,
        'result_digest': digest(result),
        'files': {name: hashlib.sha256(content.encode('utf-8')).hexdigest() for name, content in sorted(payload.items())},
    }
    payload['manifest.json'] = canonical(manifest)
    return payload


def write_bundle(catalog, result, destination):
    payload = export_payload(catalog, result)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.allocation-', dir=target.parent))
    try:
        for name, content in payload.items():
            path = temporary / name
            with path.open('w', encoding='utf-8', newline='') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return verify_bundle(target)


def verify_bundle(directory):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('bundle must be a real directory')
    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError('missing regular manifest')
    if manifest_path.stat().st_size > 100000:
        raise ValueError('manifest too large')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported manifest')
    names = set(manifest['files']) | {'manifest.json'}
    if {p.name for p in root.iterdir()} != names:
        raise ValueError('bundle file set mismatch')
    for name, expected in manifest['files'].items():
        if not name or Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe manifest filename')
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000_000:
            raise ValueError('bundle file is not a bounded regular file')
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError('bundle checksum mismatch: ' + name)
    catalog = parse(json.loads((root / 'input.json').read_text(encoding='utf-8')))
    result = json.loads((root / 'allocation.json').read_text(encoding='utf-8'))
    require_valid_result(catalog, result)
    if manifest['input_digest'] != catalog.fingerprint or manifest['result_digest'] != digest(result):
        raise ValueError('bundle semantic digest mismatch')
    reconstructed = export_payload(catalog, result)
    for name, content in reconstructed.items():
        if (root / name).read_bytes() != content.encode('utf-8'):
            raise ValueError('derived report mismatch: ' + name)
    return {'verified': True, 'input_digest': catalog.fingerprint, 'result_digest': digest(result), 'files': len(names)}
