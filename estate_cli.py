import argparse
import json
from pathlib import Path
import sys
import tempfile
from estate.catalog import parse, canonical
from estate.tabular import load_directory, write_directory
from estate.calendar import availability_report
from estate.compatibility import capacity_inventory, exclusion_matrix
from estate.coverage import eligibility_components, hall_deficits
from allocation.solver import solve
from allocation.verify import require_valid_result, resource_ledger
from allocation.guarantees import maximize_guarantee, priority_service
from assessments.equity import report
from assessments.quality import inspect_input, inspect_output, bottleneck_map
from assessments.comparison import compare
from assessments.sensitivity import study, marginal_room_capacity
from assessments.travel import travel_distribution, zone_flows, threshold_curve
from assessments.interventions import select_interventions, replay_intervention
from allocation.certificate import certificate, replay_certificate
from assessments.decision import review_packet, prioritize_shortfalls
from assessments.robust import (
    robust_decision,
    matrix_rows,
    compute_and_save,
    save_decision,
    verify_decision_bundle,
    load_decision,
    export_decision,
)
from runs.store import Store
from runs.worker import run_scenario, drain
from runs.variants import apply_changes, scale_demands, scenario_lineage
from runs.exports import write_bundle, verify_bundle
from runs.recovery import backup, restore, audit_store
from runs.query import query_report, grouped_report, room_report


def load_json(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > 50_000_000:
        raise ValueError('JSON input must be a bounded regular file')
    return json.loads(path.read_text(encoding='utf-8'))


def analysis(document, node_limit=100000):
    catalog = parse(document)
    result = solve(catalog, node_limit)
    require_valid_result(catalog, result)
    return {
        'allocation': result,
        'equity': report(catalog, result),
        'resources': resource_ledger(catalog, result),
        'bottlenecks': bottleneck_map(catalog, result),
        'travel': travel_distribution(catalog, result),
        'flows': zone_flows(catalog, result),
        'priorities': priority_service(catalog, result),
        'review': review_packet(catalog, result),
        'shortfall_priorities': prioritize_shortfalls(catalog, result),
    }


def demo(fixture):
    document = load_json(fixture)
    catalog = parse(document)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with Store(root / 'plans.sqlite') as store:
            store.create('baseline', '合成县', document)
            store.seal('baseline', 1)
            run = run_scenario(store, 'baseline', 'baseline-run')
            publication = store.publish('baseline-public', run['id'])
            result = publication['result']
            require_valid_result(catalog, result)
            write_bundle(catalog, result, root / 'report')
            verified = verify_bundle(root / 'report')
            evidence = certificate(catalog, result)
            replay_certificate(catalog, result, evidence)
            write_directory(document, root / 'tables')
            roundtrip = load_directory(root / 'tables')
            if solve(roundtrip)['served'] != result['served']:
                raise AssertionError('CSV roundtrip changed allocation')
            store.fork('baseline', 'expanded', '扩容方案')
            variant = store.scenario('expanded')
            room = variant['body']['rooms'][0]
            changed = apply_changes(variant['body'], [{'table': 'rooms', 'id': room['id'], 'field': 'seats', 'value': min(1000, room['seats'] + 5)}])
            store.replace('expanded', changed, 1)
            store.seal('expanded', 2)
            second = run_scenario(store, 'expanded', 'expanded-run')
            comparison = compare(catalog, result, parse(changed), second['result'])
            saved = backup(store, root / 'backup.sqlite')
            restored = restore(root / 'backup.sqlite', root / 'restored.sqlite', saved['sha256'])
            summary = audit_store(store)
            guarantee = maximize_guarantee(document)
            return {
                'synthetic': True,
                'served': result['served'],
                'total_demand': sum(d['students'] for d in catalog.demands.values()),
                'exact_search': [p['search'] for p in result['periods']],
                'bundle': verified,
                'comparison': comparison,
                'guarantee_basis_points': guarantee['basis_points'],
                'store': summary,
                'restore': restored['summary'],
                'lineage': scenario_lineage(store, 'expanded'),
            }


def parser():
    result = argparse.ArgumentParser(prog='estate_cli')
    result.add_argument('--db', default='plans.sqlite')
    result.add_argument('--node-limit', type=int, default=100000)
    sub = result.add_subparsers(dest='command', required=True)
    for name in ('solve', 'inspect', 'inventory', 'exclusions', 'components', 'deficits', 'guarantee', 'travel', 'demo'):
        command = sub.add_parser(name)
        command.add_argument('--fixture', required=True)
    command = sub.add_parser('import')
    command.add_argument('id')
    command.add_argument('title')
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument('--fixture')
    source.add_argument('--tables')
    command = sub.add_parser('seal')
    command.add_argument('id')
    command.add_argument('--revision', type=int, required=True)
    command = sub.add_parser('fork')
    command.add_argument('parent')
    command.add_argument('id')
    command.add_argument('title')
    command = sub.add_parser('change')
    command.add_argument('id')
    command.add_argument('--revision', type=int, required=True)
    command.add_argument('--changes', required=True)
    command = sub.add_parser('scale')
    command.add_argument('id')
    command.add_argument('--revision', type=int, required=True)
    command.add_argument('--numerator', type=int, required=True)
    command.add_argument('--denominator', type=int, required=True)
    command = sub.add_parser('run')
    command.add_argument('id')
    command.add_argument('--key', required=True)
    command = sub.add_parser('publish')
    command.add_argument('run')
    command.add_argument('label')
    command = sub.add_parser('query')
    command.add_argument('label')
    command.add_argument('--filters', default='{}')
    command.add_argument('--columns')
    command.add_argument('--cursor')
    command.add_argument('--limit', type=int, default=100)
    command = sub.add_parser('export')
    command.add_argument('label')
    command.add_argument('directory')
    command = sub.add_parser('verify-bundle')
    command.add_argument('directory')
    command = sub.add_parser('backup')
    command.add_argument('destination')
    command = sub.add_parser('restore')
    command.add_argument('source')
    command.add_argument('destination')
    command.add_argument('checksum')
    command = sub.add_parser('sensitivity')
    command.add_argument('--fixture', required=True)
    command.add_argument('--cases', required=True)
    command = sub.add_parser('interventions')
    command.add_argument('--fixture', required=True)
    command.add_argument('--proposals', required=True)
    command.add_argument('--budget', type=int, required=True)
    command = sub.add_parser('robust')
    command.add_argument('--fixture', required=True)
    command.add_argument('--cases', required=True)
    command.add_argument('--budget', type=int)
    command.add_argument('--max-cases', type=int, default=10000)
    command.add_argument('--save')
    command = sub.add_parser('robust-matrix')
    command.add_argument('--bundle', required=True)
    command.add_argument('--recompute', action='store_true')
    command = sub.add_parser('robust-verify')
    command.add_argument('--bundle', required=True)
    command.add_argument('--no-recompute', action='store_true')
    command = sub.add_parser('robust-export')
    command.add_argument('--bundle', required=True)
    command.add_argument('destination')
    command = sub.add_parser('travel-curve')
    command.add_argument('--fixture', required=True)
    command.add_argument('--thresholds', required=True)
    command = sub.add_parser('marginal-seats')
    command.add_argument('--fixture', required=True)
    command.add_argument('--rooms', required=True)
    command.add_argument('--increment', type=int, default=1)
    command = sub.add_parser('review')
    command.add_argument('--fixture', required=True)
    command.add_argument('--minimum-coverage', type=float, default=0)
    command.add_argument('--maximum-travel', type=float)
    command.add_argument('--maximum-gap', type=float)
    sub.add_parser('audit')
    sub.add_parser('drain')
    sub.add_parser('history')
    sub.add_parser('list')
    return result


def dispatch(args):
    command = args.command
    if command == 'demo':
        return demo(args.fixture)
    if command == 'verify-bundle':
        return verify_bundle(args.directory)
    if command == 'restore':
        return restore(args.source, args.destination, args.checksum)
    if command == 'robust-verify':
        return verify_decision_bundle(args.bundle, recompute=not args.no_recompute)
    if command == 'robust-matrix':
        outcome = load_decision(args.bundle, recompute=args.recompute)
        return {
            'summary': outcome['summary'],
            'benchmarks': outcome['decision']['benchmarks'],
            'matrix': matrix_rows(outcome['decision']),
            'decision': outcome['decision']['decision'],
        }
    if command == 'robust-export':
        return export_decision(args.bundle, args.destination)
    if command == 'robust':
        document = load_json(args.fixture)
        cases = load_json(args.cases)
        if args.save:
            decision = robust_decision(
                document, cases, budget=args.budget,
                max_cases=args.max_cases, node_limit=args.node_limit,
            )
            saved = compute_and_save(
                document, cases, args.save, budget=args.budget,
                max_cases=args.max_cases, node_limit=args.node_limit,
            )
            return {'decision': decision, 'bundle': saved}
        return robust_decision(
            document, cases, budget=args.budget,
            max_cases=args.max_cases, node_limit=args.node_limit,
        )
    if hasattr(args, 'fixture') and args.fixture:
        document = load_json(args.fixture)
        catalog = parse(document)
        if command == 'solve':
            return analysis(document, args.node_limit)
        if command == 'inspect':
            return inspect_input(document)
        if command == 'inventory':
            return {'calendar': availability_report(catalog), 'capacity': capacity_inventory(catalog)}
        if command == 'exclusions':
            return exclusion_matrix(catalog)
        if command == 'components':
            return eligibility_components(catalog)
        if command == 'deficits':
            return hall_deficits(catalog)
        if command == 'guarantee':
            return maximize_guarantee(document, node_limit=args.node_limit)
        if command == 'travel':
            allocation = solve(catalog, args.node_limit)
            return {'distribution': travel_distribution(catalog, allocation), 'flows': zone_flows(catalog, allocation)}
        if command == 'travel-curve':
            return threshold_curve(document, json.loads(args.thresholds), args.node_limit)
        if command == 'marginal-seats':
            return marginal_room_capacity(document, args.rooms.split(','), args.increment, args.node_limit)
        if command == 'review':
            allocation = solve(catalog, args.node_limit)
            thresholds = {'min_coverage': args.minimum_coverage, 'max_travel': args.maximum_travel, 'maximum_gap': args.maximum_gap}
            return review_packet(catalog, allocation, thresholds)
        if command == 'sensitivity':
            return study(document, load_json(args.cases), args.node_limit)
        if command == 'interventions':
            proposals = load_json(args.proposals)
            selection = select_interventions(document, proposals, args.budget, node_limit=args.node_limit)
            return {'selection': selection, 'replay': replay_intervention(document, proposals, selection, args.node_limit)}
    with Store(args.db) as store:
        if command == 'import':
            document = load_json(args.fixture) if args.fixture else load_directory(args.tables).document
            return store.create(args.id, args.title, document)
        if command == 'seal':
            return store.seal(args.id, args.revision)
        if command == 'fork':
            return store.fork(args.parent, args.id, args.title)
        if command == 'change':
            scenario = store.scenario(args.id)
            changed = apply_changes(scenario['body'], load_json(args.changes))
            return store.replace(args.id, changed, args.revision)
        if command == 'scale':
            scenario = store.scenario(args.id)
            changed, rounding = scale_demands(scenario['body'], args.numerator, args.denominator)
            return {'scenario': store.replace(args.id, changed, args.revision), 'rounding': rounding}
        if command == 'run':
            return run_scenario(store, args.id, args.key, args.node_limit)
        if command == 'publish':
            return store.publish(args.label, args.run)
        if command in ('query', 'export'):
            publication = store.publication(args.label)
            catalog = parse(store.scenario(publication['scenario'])['body'])
            if command == 'export':
                return write_bundle(catalog, publication['result'], args.directory)
            columns = args.columns.split(',') if args.columns else None
            return query_report(catalog, publication['result'], json.loads(args.filters), columns, args.limit, args.cursor)
        if command == 'backup':
            return backup(store, args.destination)
        if command == 'audit':
            return audit_store(store)
        if command == 'drain':
            return drain(store, node_limit=args.node_limit)
        if command == 'history':
            return store.history()
        if command == 'list':
            return store.scenarios()
    raise ValueError('unknown command')


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
    except (ValueError, KeyError, RuntimeError, OSError) as exc:
        print(canonical({'error': type(exc).__name__, 'message': str(exc)}), file=sys.stderr)
        return 2
    print(canonical(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
