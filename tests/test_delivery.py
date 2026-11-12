import copy
import json
from pathlib import Path
import tempfile
import unittest
from test_catalog_calendar import county
from estate.catalog import parse, canonical
from estate.tabular import dump_table, load_table, write_directory, load_directory
from allocation.solver import solve
from allocation.network import Network
from allocation.certificate import certificate, replay_certificate, residual_cost_proof, validate_cost_proof, cost_components, capacity_waterfall, perturbation_bounds
from assessments.decision import readiness, review_packet, prioritize_shortfalls
from runs.exports import write_bundle, verify_bundle, export_payload
from runs.query import query_report, grouped_report, room_report, decode_cursor, encode_cursor
from estate_cli import main, demo, analysis


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = parse(county())
        self.result = solve(self.catalog)

    def tearDown(self):
        self.temporary.cleanup()

    def test_csv_roundtrip_preserves_business_allocation(self):
        directory = self.root / 'tables'
        files = write_directory(self.catalog.document, directory)
        self.assertEqual(len(files), 8)
        restored = load_directory(directory)
        allocation = solve(restored)
        self.assertEqual(allocation['assignments'], self.result['assignments'])
        self.assertEqual(allocation['served'], self.result['served'])
        self.assertEqual(restored.rooms['flex']['features'], ['accessible', 'lab'])
        with self.assertRaises(FileExistsError):
            write_directory(self.catalog.document, directory)

    def test_csv_is_strict_about_headers_rows_and_types(self):
        with self.assertRaisesRegex(ValueError, 'duplicate CSV'):
            load_table('id,id,kind\na,b,urban\n', 'zones')
        with self.assertRaisesRegex(ValueError, 'columns'):
            load_table('id,unknown\na,b\n', 'zones')
        with self.assertRaisesRegex(ValueError, 'row width'):
            load_table('id,kind\na,urban,extra\n', 'zones')
        with self.assertRaisesRegex(ValueError, 'invalid int'):
            load_table('id,class_limit,area_per_student,required_features\np,3.2,2,[]\n', 'stages')
        with self.assertRaisesRegex(ValueError, 'invalid json'):
            load_table('id,class_limit,area_per_student,required_features\np,3,2,garbage\n', 'stages')

    def test_directory_import_rejects_added_missing_and_symlink_files(self):
        directory = self.root / 'tables'
        write_directory(self.catalog.document, directory)
        extra = directory / 'unexpected.csv'
        extra.write_text('anything')
        with self.assertRaisesRegex(ValueError, 'expected files'):
            load_directory(directory)
        extra.unlink()
        original = (directory / 'zones.csv').read_text()
        (directory / 'zones.csv').unlink()
        target = self.root / 'zones.csv'
        target.write_text(original)
        (directory / 'zones.csv').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'regular CSV'):
            load_directory(directory)

    def test_bundle_roundtrip_and_mutation_detection(self):
        directory = self.root / 'bundle'
        outcome = write_bundle(self.catalog, self.result, directory)
        self.assertTrue(outcome['verified'])
        self.assertEqual(outcome['files'], 9)
        self.assertEqual(verify_bundle(directory), outcome)
        changed = directory / 'assignments.csv'
        changed.write_text(changed.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            verify_bundle(directory)
        with self.assertRaises(FileExistsError):
            write_bundle(self.catalog, self.result, directory)

    def test_bundle_detects_added_files_and_modified_derived_reports(self):
        directory = self.root / 'bundle'
        write_bundle(self.catalog, self.result, directory)
        extra = directory / 'extra'
        extra.write_text('x')
        with self.assertRaisesRegex(ValueError, 'file set'):
            verify_bundle(directory)
        extra.unlink()
        import hashlib
        path = directory / 'equity.json'
        path.write_text('{}')
        manifest_path = directory / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['equity.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError, 'derived report'):
            verify_bundle(directory)

    def test_report_paging_covers_each_demand_exactly_once(self):
        rows = []
        cursor = None
        while True:
            page = query_report(self.catalog, self.result, columns=['demand', 'served'], limit=1, cursor=cursor)
            self.assertEqual(page['total'], 3)
            self.assertLessEqual(len(page['rows']), 1)
            rows.extend(page['rows'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['demand'] for r in rows}), 3)
        self.assertEqual(sum(row['served'] for row in rows), self.result['served'])

    def test_cursor_is_bound_to_projection_filters_and_result(self):
        first = query_report(self.catalog, self.result, columns=['demand'], limit=1)
        cursor = first['next_cursor']
        with self.assertRaisesRegex(ValueError, 'another report'):
            query_report(self.catalog, self.result, columns=['served'], cursor=cursor)
        with self.assertRaisesRegex(ValueError, 'another report'):
            query_report(self.catalog, self.result, filters={'kind': ['rural']}, columns=['demand'], cursor=cursor)
        with self.assertRaisesRegex(ValueError, 'invalid cursor'):
            query_report(self.catalog, self.result, cursor='!invalid!')
        token = decode_cursor(cursor)
        token['offset'] = 999
        with self.assertRaises(ValueError):
            query_report(self.catalog, self.result, columns=['demand'], cursor=encode_cursor(token))

    def test_query_filters_and_projection_validation(self):
        page = query_report(self.catalog, self.result, filters={'kind': ['rural']}, columns=['demand', 'shortfall'])
        self.assertEqual(page['rows'], [{'demand': 'village-primary', 'shortfall': 5}])
        self.assertEqual(query_report(self.catalog, self.result, filters={'kind': []})['total'], 0)
        for kwargs in ({'filters': {'wrong': []}}, {'columns': ['demand', 'demand']}, {'limit': 0}, {'order': 'wrong'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                query_report(self.catalog, self.result, **kwargs)
        grouped = grouped_report(self.catalog, self.result, ['kind'])
        self.assertEqual(sum(row['served'] for row in grouped['groups']), 75)
        saturated = room_report(self.catalog, self.result, only_saturated=True)
        self.assertTrue(all(row['unused'] == 0 for row in saturated))
        self.assertTrue(any(row['room'] == 'flex' for row in saturated))

    def test_certificate_reconstructs_maxflow_and_cost_witnesses(self):
        evidence = certificate(self.catalog, self.result)
        self.assertEqual(evidence['costs']['total'], self.result['cost'])
        for period in evidence['periods']:
            self.assertEqual(period['cut_capacity'], period['served'])
            self.assertTrue(validate_cost_proof(period['cost_proof']))
        outcome = replay_certificate(self.catalog, self.result, evidence)
        self.assertTrue(outcome['global_stage_optimality_replayed'])
        corrupted = copy.deepcopy(evidence)
        corrupted['periods'][0]['served'] += 1
        with self.assertRaisesRegex(ValueError, 'certificate changed'):
            replay_certificate(self.catalog, self.result, corrupted)

    def test_cost_witness_detects_improving_cycle_and_tampering(self):
        network = Network()
        network.add('one', 'two', 1, -2)
        network.add('two', 'one', 1, 1)
        with self.assertRaisesRegex(ValueError, 'negative cycle'):
            residual_cost_proof(network)
        evidence = certificate(self.catalog, self.result)['periods'][0]['cost_proof']
        changed = copy.deepcopy(evidence)
        changed['residual_arcs'][0]['reduced_cost'] += 1
        with self.assertRaisesRegex(ValueError, 'reduced cost'):
            validate_cost_proof(changed)
        result = copy.deepcopy(self.result)
        result['cost'] += 1
        result['periods'][0]['cost'] += 1
        with self.assertRaisesRegex(ValueError, 'economics'):
            cost_components(self.catalog, result)

    def test_waterfall_losses_conserve_each_alternative_room_capacity(self):
        rows = capacity_waterfall(self.catalog)
        self.assertGreater(len(rows), 3)
        for row in rows:
            losses = sum(row[key] for key in ('class_limit_loss', 'area_loss', 'feature_loss', 'schedule_loss', 'calendar_loss'))
            self.assertEqual(row['physical_seats'], losses + row['after_building_calendar'])
            self.assertFalse(row['additive_across_stages'])
        bounds = perturbation_bounds(self.catalog, self.result)
        self.assertTrue(all(row['individual_upper'] <= self.catalog.demands[row['demand']]['students'] for row in bounds))
        self.assertEqual(sum(row['current_served'] for row in bounds), self.result['served'])

    def test_review_thresholds_do_not_modify_optimization(self):
        before = copy.deepcopy(self.result)
        review = readiness(self.catalog, self.result, min_coverage=0.9)
        self.assertFalse(review['meets_review_thresholds'])
        self.assertTrue(any(row['code'] == 'coverage_below_threshold' for row in review['findings']))
        packet = review_packet(self.catalog, self.result, {'min_coverage': 0.5})
        self.assertTrue(packet['review']['meets_review_thresholds'])
        self.assertEqual(self.result, before)
        priorities = prioritize_shortfalls(self.catalog, self.result)
        self.assertEqual(priorities[0]['demand'], 'town-primary')
        self.assertEqual([row['review_rank'] for row in priorities], [1, 2])
        self.assertTrue(all(row['shortfall'] > 0 for row in priorities))

    def test_actual_cli_synthetic_demo_covers_store_export_restore(self):
        fixture = Path(__file__).resolve().parents[1] / 'fixtures/county.json'
        outcome = demo(fixture)
        self.assertTrue(outcome['synthetic'])
        self.assertEqual(outcome['served'], 75)
        self.assertTrue(outcome['bundle']['verified'])
        self.assertEqual(outcome['store']['scenarios'], 2)
        self.assertEqual(outcome['restore']['publications'], 1)
        self.assertGreater(outcome['guarantee_basis_points'], 0)
        evaluated = analysis(county())
        self.assertEqual(evaluated['allocation']['served'], 75)
        self.assertIn('certificate', evaluated['review'])

    def test_certificate_survives_json_transport(self):
        evidence = certificate(self.catalog, self.result)
        transported = json.loads(canonical(evidence))
        verified = replay_certificate(self.catalog, self.result, transported)
        self.assertTrue(verified['verified'])
        self.assertEqual(verified['result_digest'], evidence['result_digest'])
        changed = copy.deepcopy(transported)
        changed['costs']['total'] += 1
        with self.assertRaises(ValueError):
            replay_certificate(self.catalog, self.result, changed)

    def test_cli_review_and_travel_curve_are_observable_commands(self):
        from estate_cli import parser, dispatch
        fixture = str(Path(__file__).resolve().parents[1] / 'fixtures/county.json')
        args = parser().parse_args(['review', '--fixture', fixture, '--minimum-coverage', '0.9'])
        result = dispatch(args)
        self.assertFalse(result['review']['meets_review_thresholds'])
        self.assertEqual(result['equity']['total'][0]['served'], 75)
        args = parser().parse_args(['travel-curve', '--fixture', fixture, '--thresholds', '[0,20]'])
        curve = dispatch(args)['curve']
        self.assertEqual([row['threshold'] for row in curve], [0, 20])
        self.assertTrue(all(row['state'] == 'complete' for row in curve))
        args = parser().parse_args(['marginal-seats', '--fixture', fixture, '--rooms', 'primary-only'])
        marginal = dispatch(args)
        self.assertEqual(marginal[0]['room'], 'primary-only')
        self.assertGreaterEqual(marginal[0]['after_served'], marginal[0]['before_served'])

    def test_invalid_review_parameters_and_capacity_query(self):
        for kwargs in ({'min_coverage': -1}, {'max_travel': -1}, {'maximum_gap': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                readiness(self.catalog, self.result, **kwargs)
        with self.assertRaises(ValueError):
            review_packet(self.catalog, self.result, {'unknown': 0})
        with self.assertRaises(ValueError):
            room_report(self.catalog, self.result, period='missing')
        with self.assertRaises(ValueError):
            room_report(self.catalog, self.result, building='missing')
        with self.assertRaises(ValueError):
            room_report(self.catalog, self.result, only_saturated='yes')
        selected = room_report(self.catalog, self.result, building='shared')
        self.assertEqual({row['room'] for row in selected}, {'flex', 'primary-only'})
        self.assertEqual(sum(row['used'] for row in selected), 50)
