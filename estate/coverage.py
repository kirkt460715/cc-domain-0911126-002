from collections import defaultdict
from estate.catalog import parse
from estate.compatibility import compatible_edges


def eligibility_components(catalog):
    edges = compatible_edges(catalog)
    neighbours = defaultdict(set)
    for ident, demand in catalog.demands.items():
        neighbours[('demand', ident, demand['period'])]
    for edge in edges:
        demand = ('demand', edge['demand'], edge['period'])
        room = ('room', edge['room'], edge['period'])
        building = ('building', edge['building'], edge['period'])
        neighbours[demand].add(room)
        neighbours[room].add(demand)
        neighbours[room].add(building)
        neighbours[building].add(room)
    unseen = set(neighbours)
    components = []
    while unseen:
        start = min(unseen)
        stack = [start]
        visited = set()
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            unseen.discard(node)
            stack.extend(neighbours[node] - visited)
        demands = sorted(node[1] for node in visited if node[0] == 'demand')
        rooms = sorted(node[1] for node in visited if node[0] == 'room')
        buildings = sorted(node[1] for node in visited if node[0] == 'building')
        components.append({
            'period': start[2],
            'demands': demands,
            'rooms': rooms,
            'buildings': buildings,
            'students': sum(catalog.demands[ident]['students'] for ident in demands),
            'minimum': sum(catalog.demands[ident].get('minimum', 0) for ident in demands),
            'nominal_building_limit': sum(catalog.buildings[ident]['seat_limit'] for ident in buildings),
            'isolated': not rooms,
        })
    return sorted(components, key=lambda row: (row['period'], row['demands']))


def hall_deficits(catalog, max_groups=16):
    edges = compatible_edges(catalog)
    components = eligibility_components(catalog)
    findings = []
    for component in components:
        groups = component['demands']
        if len(groups) > max_groups:
            findings.append({'period': component['period'], 'state': 'not_enumerated', 'groups': len(groups)})
            continue
        edge_map = defaultdict(list)
        for edge in edges:
            if edge['period'] == component['period']:
                edge_map[edge['demand']].append(edge)
        deficits = []
        for mask in range(1, 1 << len(groups)):
            subset = [groups[index] for index in range(len(groups)) if mask & (1 << index)]
            capacity_by_room = {}
            for ident in subset:
                for edge in edge_map[ident]:
                    capacity_by_room[edge['room']] = max(capacity_by_room.get(edge['room'], 0), edge['capacity'])
            by_building = defaultdict(int)
            for room, capacity in capacity_by_room.items():
                by_building[catalog.rooms[room]['building']] += capacity
            upper = sum(min(value, catalog.buildings[ident]['seat_limit']) for ident, value in by_building.items())
            students = sum(catalog.demands[ident]['students'] for ident in subset)
            if students > upper:
                deficits.append({'demands': subset, 'students': students, 'relaxed_upper_bound': upper, 'proven_shortfall': students - upper})
        minimal = []
        for item in deficits:
            group_set = set(item['demands'])
            if any(set(other['demands']) < group_set for other in deficits):
                continue
            minimal.append(item)
        findings.append({'period': component['period'], 'state': 'complete', 'minimal_deficit_sets': minimal})
    return findings
