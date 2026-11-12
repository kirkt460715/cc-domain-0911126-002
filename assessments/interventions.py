from itertools import combinations
from estate.catalog import parse, fields, integer, text
from runs.variants import apply_changes
from allocation.solver import solve, SearchLimit, InfeasibleMinimum
from assessments.comparison import frontier


def select_interventions(document, proposals, budget, max_cases=10000, node_limit=100000):
    catalog = parse(document)
    integer(budget, 'budget', 0, 10**12)
    integer(max_cases, 'max_cases', 1, 1000000)
    if not isinstance(proposals, list) or len(proposals) > 20:
        raise ValueError('expected at most 20 proposals')
    ids = set()
    for i, item in enumerate(proposals):
        path = f'proposals[{i}]'
        fields(item, path, ('id', 'cost', 'changes'), ('requires', 'excludes'))
        text(item['id'], path + '.id')
        integer(item['cost'], path + '.cost', 0, 10**12)
        if item['id'] in ids:
            raise ValueError('duplicate proposal')
        ids.add(item['id'])
        apply_changes(catalog.document, item['changes'])
    proposal_map = {p['id']: p for p in proposals}
    for item in proposals:
        for key in ('requires', 'excludes'):
            references = item.get(key, [])
            if not isinstance(references, list) or len(references) != len(set(references)):
                raise ValueError('proposal references must be unique lists')
            if any(ident not in ids or ident == item['id'] for ident in references):
                raise ValueError('unknown or self-referential proposal dependency')
    visiting = set()
    visited = set()

    def check_cycle(ident):
        if ident in visiting:
            raise ValueError('proposal requirement cycle')
        if ident in visited:
            return
        visiting.add(ident)
        for requirement in proposal_map[ident].get('requires', []):
            check_cycle(requirement)
        visiting.remove(ident)
        visited.add(ident)

    for ident in sorted(ids):
        check_cycle(ident)
    cases = []
    failures = []
    examined = 0
    ordered = sorted(ids)
    for count in range(len(ordered) + 1):
        for combination in combinations(ordered, count):
            chosen = set(combination)
            cost = sum(proposal_map[ident]['cost'] for ident in combination)
            if cost > budget:
                continue
            valid = True
            for ident in combination:
                proposal = proposal_map[ident]
                if not set(proposal.get('requires', [])) <= chosen:
                    valid = False
                if set(proposal.get('excludes', [])) & chosen:
                    valid = False
            if not valid:
                continue
            examined += 1
            if examined > max_cases:
                raise SearchLimit('intervention search exceeded case limit; no optimum claimed')
            changes = [change for ident in combination for change in proposal_map[ident]['changes']]
            try:
                modified = parse(apply_changes(catalog.document, changes))
            except ValueError as exc:
                failures.append({'proposals': list(combination), 'state': 'conflicting_changes', 'error': str(exc)})
                continue
            try:
                allocation = solve(modified, node_limit)
            except InfeasibleMinimum as exc:
                failures.append({'proposals': list(combination), 'state': 'infeasible', 'shortfalls': exc.shortfalls})
                continue
            cases.append({
                'name': '+'.join(combination) or 'baseline',
                'proposals': list(combination),
                'investment': cost,
                'served': allocation['served'],
                'allocation_cost': allocation['cost'],
                'digest': modified.fingerprint,
            })
    if not cases:
        raise InfeasibleMinimum({'proposals': 'no_feasible_combination'})
    best = min(cases, key=lambda row: (-row['served'], row['investment'], row['allocation_cost'], row['proposals']))
    efficiency = []
    for case in cases:
        dominators = []
        for other in cases:
            if other['served'] >= case['served'] and other['investment'] <= case['investment']:
                if other['served'] > case['served'] or other['investment'] < case['investment']:
                    dominators.append(other['name'])
        efficiency.append({**case, 'pareto': not dominators, 'dominated_by': sorted(dominators)})
    return {
        'base_digest': catalog.fingerprint,
        'budget': budget,
        'examined': examined,
        'exact': True,
        'best': best,
        'cases': efficiency,
        'infeasible_cases': failures,
    }


def replay_intervention(document, proposals, selection, node_limit=100000):
    by_id = {item['id']: item for item in proposals}
    if len(by_id) != len(proposals):
        raise ValueError('duplicate proposals')
    chosen = selection['best']['proposals']
    if any(ident not in by_id for ident in chosen):
        raise ValueError('selection references unknown proposal')
    changes = [change for ident in chosen for change in by_id[ident]['changes']]
    catalog = parse(apply_changes(document, changes))
    if catalog.fingerprint != selection['best']['digest']:
        raise ValueError('intervention input changed since selection')
    allocation = solve(catalog, node_limit)
    if allocation['served'] != selection['best']['served']:
        raise ValueError('intervention replay service count mismatch')
    if allocation['cost'] != selection['best']['allocation_cost']:
        raise ValueError('intervention replay allocation cost mismatch')
    return {'document': catalog.document, 'result': allocation}
