import copy
import json
from pathlib import Path
import unittest
from estate.catalog import parse, InvalidPlan, canonical, digest, integer, number, text, day
from estate.calendar import overlap, intersection, merged_intervals, subtract_intervals, building_status, atomic_windows, conflicting_periods, assert_independent_periods, availability_report, closure_impact
from estate.compatibility import room_capacity, reasons, compatible_edges, capacity_inventory, exclusion_matrix, diagnose_demand
from estate.coverage import eligibility_components, hall_deficits
from assessments.quality import inspect_input


FIXTURE = Path(__file__).resolve().parents[1] / 'fixtures/county.json'


def county():
    return json.loads(FIXTURE.read_text())


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.data = county()

    def test_catalog_has_independent_document(self):
        parsed = parse(self.data)
        self.data['rooms'][0]['seats'] = 1
        self.assertEqual(parsed.rooms['flex']['seats'], 30)
        detached = parsed.detached()
        detached['rooms'][0]['seats'] = 2
        self.assertEqual(parsed.rooms['flex']['seats'], 30)
        self.assertEqual(parsed.fingerprint, digest(parsed.document))

    def test_required_and_unknown_fields(self):
        del self.data['routes']
        with self.assertRaisesRegex(InvalidPlan, 'missing'):
            parse(self.data)
        self.data = county()
        self.data['mystery'] = True
        with self.assertRaisesRegex(InvalidPlan, 'unknown'):
            parse(self.data)

    def test_version_is_explicit(self):
        for value in (0, 2, True, '1'):
            self.data['version'] = value
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                parse(self.data)

    def test_duplicate_identifiers_are_rejected(self):
        for table in ('zones', 'stages', 'rooms', 'buildings', 'periods', 'demands'):
            data = county()
            data[table].append(copy.deepcopy(data[table][0]))
            with self.subTest(table=table), self.assertRaisesRegex(InvalidPlan, 'duplicate'):
                parse(data)

    def test_all_references_resolve_before_solving(self):
        for table, field in (('rooms', 'building'), ('buildings', 'zone'), ('demands', 'zone'), ('demands', 'stage'), ('demands', 'period')):
            data = county()
            data[table][0][field] = 'absent'
            with self.subTest(table=table, field=field), self.assertRaisesRegex(InvalidPlan, 'unknown'):
                parse(data)

    def test_room_reference_lists(self):
        self.data['rooms'][0]['allowed_stages'] = ['primary', 'primary']
        with self.assertRaisesRegex(InvalidPlan, 'duplicate'):
            parse(self.data)
        self.data['rooms'][0]['allowed_stages'] = ['absent']
        with self.assertRaisesRegex(InvalidPlan, 'unknown'):
            parse(self.data)
        self.data['rooms'][0]['allowed_stages'] = ['primary']
        self.data['rooms'][0]['periods'] = ['absent']
        with self.assertRaisesRegex(InvalidPlan, 'unknown'):
            parse(self.data)

    def test_minimum_cannot_exceed_population(self):
        self.data['demands'][0]['minimum'] = 36
        with self.assertRaisesRegex(InvalidPlan, 'minimum exceeds'):
            parse(self.data)
        self.data['demands'][0]['minimum'] = 35
        self.assertEqual(parse(self.data).demands['town-primary']['minimum'], 35)

    def test_numeric_validation_avoids_bool_and_nonfinite(self):
        for value in (True, -1, '30', 1.5):
            self.data['rooms'][0]['seats'] = value
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                parse(self.data)
        for value in (float('nan'), float('inf'), -1, True):
            self.data = county()
            self.data['rooms'][0]['area'] = value
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                parse(self.data)

    def test_feature_uniqueness(self):
        self.data['stages'][0]['required_features'] = ['lab', 'lab']
        with self.assertRaisesRegex(InvalidPlan, 'duplicate feature'):
            parse(self.data)
        self.data = county()
        self.data['rooms'][0]['features'] = ['lab', 'lab']
        with self.assertRaisesRegex(InvalidPlan, 'duplicate feature'):
            parse(self.data)

    def test_routes_are_directed_and_unique(self):
        self.data['routes'].append(copy.deepcopy(self.data['routes'][0]))
        with self.assertRaisesRegex(InvalidPlan, 'duplicate directed'):
            parse(self.data)
        self.data = county()
        self.data['routes'][0]['to'] = 'unknown'
        with self.assertRaisesRegex(InvalidPlan, 'endpoints'):
            parse(self.data)

    def test_policy_rejects_silent_unsupported_options(self):
        self.data['policy']['split_penalty'] = 5
        with self.assertRaisesRegex(InvalidPlan, 'unknown'):
            parse(self.data)
        self.data['policy'] = {'require_minima': 1}
        with self.assertRaisesRegex(InvalidPlan, 'boolean'):
            parse(self.data)

    def test_dates_are_canonical_half_open_intervals(self):
        for value in ('2026-2-01', '2026-02-30', 20260101):
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                day(value, 'date')
        self.data['periods'][0]['end'] = self.data['periods'][0]['start']
        with self.assertRaisesRegex(InvalidPlan, 'empty'):
            parse(self.data)
        self.data = county()
        self.data['buildings'][0]['close'] = '2019-01-01'
        with self.assertRaisesRegex(InvalidPlan, 'positive'):
            parse(self.data)

    def test_invalid_closure_is_rejected(self):
        self.data['buildings'][0]['closures'] = [{'start': '2026-10-01', 'end': '2026-09-01', 'reason': 'renovation'}]
        with self.assertRaisesRegex(InvalidPlan, 'positive duration'):
            parse(self.data)

    def test_identifier_and_canonical_rules(self):
        for value in ('', ' ', 'a\nb', 1, 'x' * 129):
            with self.subTest(value=value), self.assertRaises(InvalidPlan):
                text(value, 'id')
        self.assertEqual(canonical({'b': 2, 'a': 1}), '{"a":1,"b":2}')
        self.assertEqual(digest({'a': 1, 'b': 2}), digest({'b': 2, 'a': 1}))
        self.assertNotEqual(digest({'a': 1}), digest({'a': 2}))


class CalendarTests(unittest.TestCase):
    def test_overlap_uses_half_open_boundaries(self):
        self.assertFalse(overlap('2026-01-01', '2026-02-01', '2026-02-01', '2026-03-01'))
        self.assertTrue(overlap('2026-01-01', '2026-02-02', '2026-02-01', '2026-03-01'))
        self.assertEqual(intersection(('a', 'd'), ('b', 'c')), ('b', 'c'))
        self.assertIsNone(intersection(('a', 'b'), ('b', 'c')))

    def test_closure_union_is_not_double_subtracted(self):
        closures = [('b', 'd'), ('c', 'e'), ('e', 'f')]
        self.assertEqual(merged_intervals(closures), [('b', 'f')])
        self.assertEqual(subtract_intervals(('a', 'g'), closures), [('a', 'b'), ('f', 'g')])
        self.assertEqual(subtract_intervals(('b', 'e'), closures), [])
        with self.assertRaises(InvalidPlan):
            merged_intervals([('z', 'a')])

    def test_building_status_requires_entire_period(self):
        building = county()['buildings'][0]
        period = county()['periods'][0]
        self.assertEqual(building_status(building, period), 'available')
        building['open'] = '2026-09-02'
        self.assertEqual(building_status(building, period), 'not_open')
        building['open'] = '2020-01-01'
        building['close'] = '2026-12-31'
        self.assertEqual(building_status(building, period), 'expired')
        building['close'] = '2030-01-01'
        building['closures'] = [{'start': '2026-10-01', 'end': '2026-10-02', 'reason': 'test'}]
        self.assertEqual(building_status(building, period), 'closed')

    def test_overlapping_planning_periods_rejected(self):
        data = county()
        data['periods'].append({'id': 'winter', 'start': '2026-12-01', 'end': '2027-02-01'})
        catalog = parse(data)
        self.assertEqual(conflicting_periods(catalog), {('autumn', 'winter')})
        with self.assertRaisesRegex(InvalidPlan, 'reuse'):
            assert_independent_periods(catalog)
        self.assertEqual(len(atomic_windows(catalog)), 3)
        self.assertEqual(atomic_windows(catalog)[1]['periods'], ['autumn', 'winter'])

    def test_adjacent_periods_can_reuse_rooms(self):
        data = county()
        data['periods'].append({'id': 'spring', 'start': '2027-01-01', 'end': '2027-05-01'})
        catalog = parse(data)
        assert_independent_periods(catalog)
        self.assertEqual(conflicting_periods(catalog), set())
        self.assertEqual(len(atomic_windows(catalog)), 2)

    def test_availability_and_closure_impact(self):
        data = county()
        data['buildings'][0]['closures'] = [{'start': '2026-09-01', 'end': '2026-09-11', 'reason': 'repair'}]
        catalog = parse(data)
        row = next(r for r in availability_report(catalog) if r['building'] == 'shared')
        self.assertEqual(row['total_days'] - row['available_days'], 10)
        self.assertEqual(row['status'], 'closed')
        impact = closure_impact(catalog)
        self.assertEqual(impact[0]['nominal_seats'], 60)
        self.assertEqual(impact[0]['rooms'], 2)


class CompatibilityTests(unittest.TestCase):
    def test_area_class_and_physical_capacity_interact(self):
        room = county()['rooms'][0]
        stage = county()['stages'][0]
        self.assertEqual(room_capacity(room, stage), 30)
        room['area'] = 39
        self.assertEqual(room_capacity(room, stage), 19)
        room['seats'] = 10
        self.assertEqual(room_capacity(room, stage), 10)
        stage['class_limit'] = 5
        self.assertEqual(room_capacity(room, stage), 5)

    def test_features_and_stage_restrictions_are_separate(self):
        catalog = parse(county())
        demand = catalog.demands['town-secondary']
        excluded = reasons(catalog, demand, catalog.rooms['primary-only'])
        self.assertIn('stage_not_allowed', excluded)
        self.assertIn('missing_features:lab', excluded)
        self.assertEqual(reasons(catalog, demand, catalog.rooms['flex']), [])

    def test_directed_route_does_not_imply_reverse(self):
        data = county()
        data['routes'] = [{'from': 'town', 'to': 'village', 'minutes': 5}]
        catalog = parse(data)
        denied = reasons(catalog, catalog.demands['village-primary'], catalog.rooms['flex'])
        self.assertIn('no_directed_route', denied)
        self.assertNotIn('travel_limit', denied)

    def test_travel_limit_inclusive_and_no_route_transitivity(self):
        data = county()
        data['demands'][2]['max_minutes'] = 20
        catalog = parse(data)
        self.assertEqual(reasons(catalog, catalog.demands['village-primary'], catalog.rooms['flex']), [])
        data['demands'][2]['max_minutes'] = 19
        catalog = parse(data)
        self.assertIn('travel_limit', reasons(catalog, catalog.demands['village-primary'], catalog.rooms['flex']))

    def test_exclusion_matrix_and_individual_upper_bound(self):
        catalog = parse(county())
        edges = compatible_edges(catalog)
        diagnostic = diagnose_demand(catalog, edges, 'town-secondary')
        self.assertEqual(diagnostic['compatible_rooms'], 1)
        self.assertEqual(diagnostic['unshared_upper_bound'], 25)
        self.assertEqual(diagnostic['structural_shortfall'], 0)
        self.assertTrue(any(row['demand'] == 'town-secondary' for row in exclusion_matrix(catalog)))
        inventory = capacity_inventory(catalog)
        self.assertTrue(any(row['stage'] == 'secondary' and row['capacity'] == 25 for row in inventory))

    def test_zero_building_capacity_and_unreachable_data_quality(self):
        data = county()
        data['buildings'][0]['seat_limit'] = 0
        data['demands'][0]['minimum'] = 0
        data['demands'][1]['minimum'] = 0
        catalog = parse(data)
        denied = reasons(catalog, catalog.demands['town-primary'], catalog.rooms['flex'])
        self.assertIn('building_capacity_zero', denied)
        inspection = inspect_input(data)
        self.assertTrue(any(row['code'] == 'unreachable_demand' for row in inspection['issues']))
        self.assertEqual(inspection['counts']['error'], 0)

    def test_connected_components_share_building_constraint(self):
        catalog = parse(county())
        components = eligibility_components(catalog)
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]['students'], 90)
        self.assertEqual(components[0]['nominal_building_limit'], 75)
        deficits = hall_deficits(catalog)
        self.assertEqual(deficits[0]['state'], 'complete')
        self.assertTrue(any(row['proven_shortfall'] > 0 for row in deficits[0]['minimal_deficit_sets']))

    def test_missing_population_is_not_explicit_zero(self):
        data = county()
        data['demands'][2]['students'] = 0
        data['demands'][2]['minimum'] = 0
        inspection = inspect_input(data)
        self.assertTrue(any(row['code'] == 'explicit_zero_demand' for row in inspection['issues']))
        self.assertTrue(any(row['zone'] == 'village' and row['stage'] == 'secondary' for row in inspection['missing_population_cells']))
        self.assertFalse(any(row['zone'] == 'village' and row['stage'] == 'primary' for row in inspection['missing_population_cells']))
