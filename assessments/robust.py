"""Cross-scenario minimum-regret investment selection.

A portfolio of capital projects is chosen once, before any scenario is known.
Inside each scenario the same portfolio is kept; only student reassignment may
vary, and it obeys the existing rules (room/stage compatibility, directed
travel limits, shared building seat limits and mandatory minima).

Scenarios carry no probability.  Ranking is: minimise the maximum regret,
maximise the worst-scenario service count, minimise investment, minimise the
sum of scenario allocation costs, and finally order by project identifiers.

The search is exact only when every legal portfolio and every portfolio x
scenario allocation was fully solved.  When an explicit cap is reached the
result keeps candidates plus proven lower/upper bounds and never reports an
optimum.
"""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
from itertools import combinations

from estate.catalog import canonical, digest, fields, integer, parse, text
from allocation.solver import InfeasibleMinimum, assess
from allocation.verify import require_valid_result
from allocation.certificate import certificate, replay_certificate
from runs.variants import apply_changes


PROJECT_TABLES = ('rooms', 'buildings', 'stages')
SCENARIO_FIELDS = {'demands': {'students'}, 'buildings': {'closures'}}
FEASIBLE_STATES = ('complete', 'incomplete')
# 'undetermined': a cap stopped the search before any feasible candidate was
# found.  It is kept separate from a proven infeasibility because more search
# might still turn up a legal allocation.
UNPROVEN_STATES = ('undetermined', 'incomplete')


def _project_summary(proposals):
    rows = []
    for item in sorted(proposals, key=lambda p: p['id']):
        rows.append({
            'id': item['id'],
            'cost': item['cost'],
            'requires': sorted(item.get('requires', [])),
            'excludes': sorted(item.get('excludes', [])),
            'changes_digest': digest(item['changes']),
        })
    return {'projects': rows, 'digest': digest(rows)}


def _scenario_summary(scenarios, catalogs):
    rows = []
    for scenario in scenarios:
        catalog = catalogs[scenario['id']]
        rows.append({
            'id': scenario['id'],
            'changes_digest': digest(scenario['changes']),
            'catalog_digest': catalog.fingerprint,
            'demand_total': sum(d['students'] for d in catalog.demands.values()),
            'demands': sorted(catalog.demands),
            'buildings': sorted(catalog.buildings),
        })
    return {'scenarios': rows, 'digest': digest(rows)}


def _validate_projects(proposals):
    if not isinstance(proposals, list) or len(proposals) > 20:
        raise ValueError('expected at most 20 projects')
    ids = set()
    for index, item in enumerate(proposals):
        path = f'projects[{index}]'
        fields(item, path, ('id', 'cost', 'changes'), ('requires', 'excludes'))
        text(item['id'], path + '.id')
        integer(item['cost'], path + '.cost', 0, 10**12)
        if item['id'] in ids:
            raise ValueError('duplicate project identifier')
        ids.add(item['id'])
        if not isinstance(item['changes'], list):
            raise ValueError(path + '.changes: expected list')
        for change_index, change in enumerate(item['changes']):
            change_path = f'{path}.changes[{change_index}]'
            fields(change, change_path, ('table', 'id', 'field', 'value'))
            if change['table'] not in PROJECT_TABLES:
                raise ValueError(change_path + '.table: projects may only modify rooms, buildings or stages')
        for key in ('requires', 'excludes'):
            references = item.get(key, [])
            if not isinstance(references, list) or len(references) != len(set(references)):
                raise ValueError(path + '.' + key + ': expected unique list')
            if any(not isinstance(reference, str) for reference in references):
                raise ValueError(path + '.' + key + ': references must be text')
    mapping = {item['id']: item for item in proposals}
    for item in proposals:
        for key in ('requires', 'excludes'):
            for reference in item.get(key, []):
                if reference not in ids or reference == item['id']:
                    raise ValueError('unknown or self-referential project dependency')
    visiting = set()
    finished = set()

    def check_cycle(ident):
        if ident in visiting:
            raise ValueError('project requirement cycle')
        if ident in finished:
            return
        visiting.add(ident)
        for requirement in mapping[ident].get('requires', []):
            check_cycle(requirement)
        visiting.remove(ident)
        finished.add(ident)

    for ident in sorted(ids):
        check_cycle(ident)
    return mapping


def _validate_scenarios(scenarios, base):
    if not isinstance(scenarios, list) or not scenarios or len(scenarios) > 50:
        raise ValueError('expected 1..50 scenarios')
    names = set()
    catalogs = {}
    for index, scenario in enumerate(scenarios):
        path = f'scenarios[{index}]'
        fields(scenario, path, ('id', 'changes'))
        text(scenario['id'], path + '.id')
        if scenario['id'] in names:
            raise ValueError('scenario names must be unique')
        names.add(scenario['id'])
        if not isinstance(scenario['changes'], list):
            raise ValueError(path + '.changes: expected list')
        for change_index, change in enumerate(scenario['changes']):
            change_path = f'{path}.changes[{change_index}]'
            fields(change, change_path, ('table', 'id', 'field', 'value'))
            table = change['table']
            if table not in SCENARIO_FIELDS or change['field'] not in SCENARIO_FIELDS[table]:
                raise ValueError(change_path + ': scenarios may only change demand students or building closures')
        # apply_changes validates references, types and business invariants.
        catalogs[scenario['id']] = parse(apply_changes(base.document, scenario['changes']))
    return catalogs


def prepare(document, cases, budget=None):
    base = parse(document)
    if not isinstance(cases, dict):
        raise ValueError('planning cases must be an object')
    extra = cases.keys() - {'budget', 'proposals', 'scenarios'}
    if extra:
        raise ValueError('unknown planning case fields: ' + ','.join(sorted(extra)))
    if 'proposals' not in cases or 'scenarios' not in cases:
        raise ValueError('planning cases require proposals and scenarios')
    if budget is None:
        budget = cases.get('budget')
    if budget is None:
        raise ValueError('a budget is required')
    integer(budget, 'budget', 0, 10**12)
    mapping = _validate_projects(cases['proposals'])
    catalogs = _validate_scenarios(cases['scenarios'], base)
    return base, budget, mapping, cases['scenarios'], catalogs


def _legal_portfolios(mapping, budget):
    """Enumerate legal portfolios in (project count, then project id) order.

    Answers are yielded lazily so callers can stop at an explicit case cap
    without materialising an exponential candidate list first.
    """
    ids = sorted(mapping)
    for count in range(len(ids) + 1):
        for combination in combinations(ids, count):
            investment = sum(mapping[ident]['cost'] for ident in combination)
            if investment > budget:
                continue
            chosen = set(combination)
            legal = True
            for ident in combination:
                project = mapping[ident]
                if not set(project.get('requires', [])) <= chosen:
                    legal = False
                if set(project.get('excludes', [])) & chosen:
                    legal = False
            if legal:
                yield tuple(combination), investment


def _portfolio_name(projects):
    return '+'.join(projects) if projects else 'baseline'


def _portfolio_projects(name):
    return () if name == 'baseline' else tuple(name.split('+'))


def _project_changes(mapping, projects):
    return [change for ident in projects for change in mapping[ident]['changes']]


def _apply(context_catalog, mapping, projects):
    try:
        return parse(apply_changes(context_catalog.document, _project_changes(mapping, projects))), None
    except ValueError as exc:
        # apply_changes raises InvalidPlan for business rules and plain
        # ValueError for duplicate/unknown targets; both are data conflicts.
        return None, str(exc)


def _cell(scenario_id, status, catalog_digest, **values):
    base = {
        'scenario': scenario_id,
        'status': status,
        'served': None,
        'served_lower': 0,
        'served_upper': None,
        'allocation_cost': None,
        'catalog_digest': catalog_digest,
        'result_digest': None,
        'search': None,
        'shortfalls': None,
        'error': None,
        'candidate': None,
    }
    base.update(values)
    return base


def _evaluate_cell(context_catalog, mapping, projects, scenario_id, node_limit):
    catalog, conflict = _apply(context_catalog, mapping, projects)
    if conflict is not None:
        return _cell(
            scenario_id, 'data_conflict', context_catalog.fingerprint,
            error=conflict,
        )
    try:
        outcome = assess(catalog, node_limit)
    except InfeasibleMinimum as exc:
        return _cell(
            scenario_id, 'infeasible_minimum', catalog.fingerprint,
            shortfalls=exc.shortfalls,
        )
    except ValueError as exc:
        return _cell(
            scenario_id, 'data_conflict', catalog.fingerprint,
            error=str(exc),
        )
    if outcome['status'] == 'complete':
        result = outcome['result']
        require_valid_result(catalog, result)
        return _cell(
            scenario_id, 'complete', catalog.fingerprint,
            served=result['served'],
            served_lower=result['served'],
            served_upper=result['served'],
            allocation_cost=result['cost'],
            result_digest=digest(result),
            search=outcome['search'],
            candidate=result,
        )
    result = outcome['result']
    if result is None:
        return _cell(
            scenario_id, 'undetermined', catalog.fingerprint,
            served_lower=outcome['served_lower'],
            served_upper=outcome['served_upper'],
            search=outcome['search'],
            error='node limit reached before any feasible allocation was found',
        )
    return _cell(
        scenario_id, 'incomplete', catalog.fingerprint,
        served=result['served'],
        served_lower=outcome['served_lower'],
        served_upper=outcome['served_upper'],
        allocation_cost=result['cost'],
        result_digest=digest(result),
        search=outcome['search'],
        candidate=result,
        error='node limit reached; incumbent kept but optimality not proved',
    )


def _benchmarks(rows, scenario_ids, enumeration_complete=True):
    benchmarks = []
    for scenario_id in scenario_ids:
        cells = [row['cells'][scenario_id] for row in rows]
        feasible = [cell for cell in cells if cell['status'] in FEASIBLE_STATES]
        supported = [cell for cell in feasible if cell['status'] == 'complete' or cell['served_lower'] > 0]
        bounded = [
            cell for cell in cells
            if cell['status'] in UNPROVEN_STATES or cell['status'] in FEASIBLE_STATES
        ]
        if not supported:
            state = 'unresolved' if any(cell['status'] in UNPROVEN_STATES for cell in cells) or not enumeration_complete else 'no_feasible_portfolio'
            benchmarks.append({
                'scenario': scenario_id,
                'served': None,
                'exact': False,
                'lower': None,
                'upper': None,
                'state': state,
                'witnesses': [],
                'searched_portfolios': len(cells),
            })
            continue
        lower = max(cell['served_lower'] for cell in supported)
        if not enumeration_complete:
            # Unevaluated portfolios could serve more, so the post-hoc optimum
            # has no provable general upper bound; only a lower bound is available.
            benchmarks.append({
                'scenario': scenario_id,
                'served': None,
                'exact': False,
                'lower': lower,
                'upper': None,
                'state': 'partial_enumeration',
                'witnesses': sorted(
                    row['name'] for row in rows
                    if row['cells'][scenario_id]['status'] == 'complete'
                    and row['cells'][scenario_id]['served'] == lower
                ),
                'searched_portfolios': len(cells),
            })
            continue
        # Every searched portfolio's optimistic relaxation bounds what it could
        # serve, including cells whose search stopped before finding a candidate.
        # Only proven infeasibility and data conflicts can be discarded.
        upper_values = [
            cell['served_upper'] for cell in bounded
            if cell['served_upper'] is not None
        ]
        upper = max(upper_values) if upper_values else lower
        # Exact only when the bound meets a witness on both sides; an unresolved
        # cell whose relaxation is still above the witness blocks exactness.
        exact = lower == upper
        witnesses = sorted(
            row['name'] for row in rows
            if row['cells'][scenario_id]['status'] == 'complete'
            and row['cells'][scenario_id]['served'] == lower
        )
        if exact:
            witnesses += [
                row['name'] for row in rows
                if row['cells'][scenario_id]['status'] == 'incomplete'
                and row['cells'][scenario_id]['served_lower'] == lower
                and row['cells'][scenario_id]['served_upper'] == lower
            ]
        benchmarks.append({
            'scenario': scenario_id,
            'served': lower if exact else None,
            'exact': exact,
            'lower': lower,
            'upper': upper,
            'state': 'exact' if exact else ('bounded' if upper is not None else 'partial'),
            'witnesses': sorted(witnesses),
            'searched_portfolios': len(cells),
        })
    return benchmarks


def _regret(cell, benchmark):
    if benchmark['lower'] is None or cell['status'] not in FEASIBLE_STATES:
        return {'regret': None, 'regret_lower': None, 'regret_upper': None}  # unknown or not feasible
    lower = max(0, benchmark['lower'] - cell['served_upper'])
    if benchmark['upper'] is None or cell['served_upper'] is None:
        return {'regret': None, 'regret_lower': lower, 'regret_upper': None}
    upper = benchmark['upper'] - cell['served_lower']
    if benchmark['exact'] and cell['status'] == 'complete':
        return {'regret': upper, 'regret_lower': upper, 'regret_upper': upper}
    return {'regret': None, 'regret_lower': lower, 'regret_upper': upper}


def _metrics(cells, projects, investment, benchmarks):
    benchmark_by_scenario = {row['scenario']: row for row in benchmarks}
    feasible = [cell for cell in cells if cell['status'] in FEASIBLE_STATES]
    if len(feasible) != len(cells):
        return None
    proved = [cell for cell in feasible if cell['status'] == 'complete' or cell['candidate'] is not None]
    if len(proved) != len(cells):
        return None
    regrets = {cell['scenario']: _regret(cell, benchmark_by_scenario[cell['scenario']]) for cell in cells}
    exact = all(cell['status'] == 'complete' for cell in cells) and all(
        benchmark_by_scenario[cell['scenario']]['exact'] for cell in cells
    )
    max_regret = max((row['regret'] for row in regrets.values()), default=None) if exact else None
    regret_upper_values = [row['regret_upper'] for row in regrets.values() if row['regret_upper'] is not None]
    max_regret_upper = max(regret_upper_values) if regret_upper_values else None
    unbounded = any(row['regret_upper'] is None for row in regrets.values())
    max_regret_lower = max((row['regret_lower'] or 0) for row in regrets.values())
    served_min = min(cell['served_lower'] for cell in cells)
    upper_values = [cell['served_upper'] for cell in cells if cell['served_upper'] is not None]
    served_min_upper = min(upper_values) if upper_values else None
    costs = [cell['allocation_cost'] for cell in cells]
    cost_sum = sum(costs) if all(cost is not None for cost in costs) else None
    worst_service_scenario = min(
        cells, key=lambda cell: (cell['served_lower'], cell['scenario'])
    )['scenario']
    # The worst-regret label is only trustworthy when every per-cell regret is
    # an exact number (that is what ``exact`` guarantees), or when all upper
    # bounds are available.  Otherwise report the scenario with the largest
    # proven lower bound and mark the label as provisional.
    if exact:
        worst_regret_scenario = min(
            cells, key=lambda cell: (-regrets[cell['scenario']]['regret'], cell['scenario']),
        )['scenario']
        worst_regret_label = 'exact'
    elif not unbounded:
        worst_regret_scenario = min(
            cells, key=lambda cell: (-regrets[cell['scenario']]['regret_upper'], cell['scenario']),
        )['scenario']
        worst_regret_label = 'upper_bound'
    else:
        worst_regret_scenario = min(
            cells, key=lambda cell: (-(regrets[cell['scenario']]['regret_lower'] or 0), cell['scenario']),
        )['scenario']
        worst_regret_label = 'partial'
    return {
        'projects': list(projects),
        'investment': investment,
        'served_min': served_min,
        'served_min_upper': served_min_upper,
        'max_regret': max_regret,
        'max_regret_lower': max_regret_lower,
        'max_regret_upper': None if unbounded else max_regret_upper,
        'allocation_cost_sum': cost_sum,
        'worst_service_scenario': worst_service_scenario,
        'worst_regret_scenario': worst_regret_scenario,
        'worst_regret_label': worst_regret_label,
        'exact': exact,
    }


def _rank_key(metrics):
    cost_sum = metrics['allocation_cost_sum'] if metrics['allocation_cost_sum'] is not None else 10**30
    if metrics['exact']:
        return (0, metrics['max_regret'], -metrics['served_min'], metrics['investment'], cost_sum, tuple(metrics['projects']))
    upper = metrics['max_regret_upper'] if metrics['max_regret_upper'] is not None else 10**30
    return (1, upper, -metrics['served_min'], metrics['investment'], cost_sum, tuple(metrics['projects']))


def _rank_record(metrics):
    key = _rank_key(metrics)
    return {
        'regret_basis': 'exact' if metrics['exact'] else ('upper_bound' if metrics['max_regret_upper'] is not None else 'unbounded'),
        'max_regret': key[1],
        'negative_worst_service': key[2],
        'investment': key[3],
        'allocation_cost_sum': key[4],
        'projects': list(key[5]),
    }


def _verify_allocations(allocations, mapping, scenarios, catalogs, replay=True):
    allowed = {'portfolio', 'scenario', 'catalog_digest', 'result', 'result_digest',
               'certificate', 'certificate_digest', 'roles'}
    for index, item in enumerate(allocations):
        path = f'allocations[{index}]'
        if set(item) - allowed:
            raise ValueError(path + ': unknown allocation fields')
        if item.get('scenario') not in catalogs or not isinstance(item.get('portfolio'), str):
            raise ValueError(path + ': unknown scenario or portfolio')
        projects = _portfolio_projects(item['portfolio'])
        if any(project not in mapping for project in projects):
            raise ValueError(path + ': unknown project reference')
        if tuple(sorted(projects)) != projects:
            raise ValueError(path + ': portfolio is not in canonical project order')
        catalog = catalogs[item['scenario']]
        modified, conflict = _apply(catalog, mapping, projects)
        if conflict is not None:
            raise ValueError(path + ': allocation portfolio no longer applies')
        if modified.fingerprint != item.get('catalog_digest'):
            raise ValueError(path + ': allocation catalog digest mismatch')
        require_valid_result(modified, item.get('result'))
        if digest(item['result']) != item.get('result_digest'):
            raise ValueError(path + ': allocation result digest mismatch')
        evidence = item.get('certificate')
        if evidence is not None:
            if digest(evidence) != item.get('certificate_digest'):
                raise ValueError(path + ': certificate digest mismatch')
            if replay:
                replay_certificate(modified, item['result'], evidence)
        roles = item.get('roles')
        if not isinstance(roles, list) or not roles or set(roles) - {'selected', 'benchmark'}:
            raise ValueError(path + ': invalid allocation roles')
        if len(roles) != len(set(roles)):
            raise ValueError(path + ': duplicate allocation role')
    return True


def _decision_state(supported, incomplete, enumeration_complete):
    if supported:
        return 'optimal' if (enumeration_complete and not incomplete) else 'provisional'
    if incomplete or not enumeration_complete:
        return 'undetermined'
    return 'no_robust_portfolio'


def _build_allocations(portfolio_rows, benchmarks, scenario_ids, catalogs, mapping):
    wanted = []
    selected = None
    for row in portfolio_rows:
        if row['selected']:
            selected = row
    if selected is not None:
        wanted.extend((selected, scenario_id, ['selected']) for scenario_id in scenario_ids)
    for benchmark in benchmarks:
        for witness in benchmark['witnesses']:
            matched = next((row for row in portfolio_rows if row['name'] == witness), None)
            if matched is not None:
                wanted.append((matched, benchmark['scenario'], ['benchmark']))
    seen = {}
    for row, scenario_id, role in wanted:
        key = (row['name'], scenario_id)
        if key in seen:
            seen[key]['roles'] = sorted(set(seen[key]['roles']) | set(role))
            continue
        cell = row['cells'][scenario_id]
        result = cell.get('candidate')
        if cell['status'] != 'complete' or result is None:
            # Only fully proved allocations are stored; truncated candidates are
            # already exposed through the matrix bounds and are not certified.
            continue
        modified, conflict = _apply(catalogs[scenario_id], mapping, row['projects'])
        if conflict is not None:
            raise AssertionError('stored complete allocation no longer applies')
        evidence = certificate(modified, result)
        replay_certificate(modified, result, evidence)
        seen[key] = {
            'portfolio': row['name'],
            'scenario': scenario_id,
            'catalog_digest': modified.fingerprint,
            'result': result,
            'result_digest': digest(result),
            'certificate': evidence,
            'certificate_digest': digest(evidence),
            'roles': sorted(role),
        }
    return sorted(seen.values(), key=lambda item: (item['portfolio'], item['scenario']))


def robust_decision(document, cases, budget=None, max_cases=10000, node_limit=100000, include_allocations=True):
    integer(max_cases, 'max_cases', 1, 1000000)
    if isinstance(node_limit, bool) or not isinstance(node_limit, int) or node_limit < 1:
        raise ValueError('node_limit must be positive integer')
    base, selected_budget, mapping, scenarios, catalogs = prepare(document, cases, budget)
    scenario_ids = [scenario['id'] for scenario in scenarios]
    legal_count = 0
    rows = []
    enumeration_complete = True
    for projects, investment in _legal_portfolios(mapping, selected_budget):
        legal_count += 1
        if legal_count > max_cases:
            # Stop as soon as the cap is met; the remaining legal portfolios
            # are unevaluated and their service is unknown.
            enumeration_complete = False
            break
        cells = {
            scenario['id']: _evaluate_cell(catalogs[scenario['id']], mapping, projects, scenario['id'], node_limit)
            for scenario in scenarios
        }
        rows.append({
            'name': _portfolio_name(projects),
            'projects': list(projects),
            'investment': investment,
            'cells': cells,
        })
    legal_portfolios = legal_count if enumeration_complete else None

    benchmarks = _benchmarks(rows, scenario_ids, enumeration_complete)
    for row in rows:
        row['metrics'] = _metrics(
            [row['cells'][scenario_id] for scenario_id in scenario_ids],
            row['projects'], row['investment'], benchmarks,
        )
    supported = [row for row in rows if row['metrics'] is not None]
    incomplete_cells = [
        {'portfolio': row['name'], 'scenario': scenario_id,
         'status': row['cells'][scenario_id]['status'],
         'served_lower': row['cells'][scenario_id]['served_lower'],
         'served_upper': row['cells'][scenario_id]['served_upper'],
         'reason': row['cells'][scenario_id]['error'] or 'node limit reached before optimality was proved'}
        for row in rows for scenario_id in scenario_ids
        if row['cells'][scenario_id]['status'] in UNPROVEN_STATES
    ]
    conflicts = [
        {'portfolio': row['name'], 'scenario': scenario_id, 'error': row['cells'][scenario_id]['error']}
        for row in rows for scenario_id in scenario_ids
        if row['cells'][scenario_id]['status'] == 'data_conflict'
    ]
    guarantee_failures = [
        {'portfolio': row['name'], 'scenario': scenario_id,
         'shortfalls': row['cells'][scenario_id]['shortfalls'],
         'missing_total': sum(row['cells'][scenario_id]['shortfalls'].values())}
        for row in rows for scenario_id in scenario_ids
        if row['cells'][scenario_id]['status'] == 'infeasible_minimum'
    ]
    incomplete = bool(incomplete_cells) or not enumeration_complete
    state = _decision_state(supported, incomplete, enumeration_complete)
    ordered = sorted(supported, key=lambda row: _rank_key(row['metrics'])) if supported else []
    # A final selection is claimed only for a fully searched, fully enumerated
    # decision.  Any truncation downgrades the output to candidates plus bounds.
    selected = ordered[0] if ordered and state == 'optimal' else None
    leading_candidate = ordered[0] if ordered and state == 'provisional' else None
    for row in rows:
        row['selected'] = selected is not None and row['name'] == selected['name']
    candidates = ordered[:10]
    trigger_scenarios = sorted({
        scenario_id for scenario_id in scenario_ids
        if not any(row['cells'][scenario_id]['status'] in FEASIBLE_STATES
                   and (row['cells'][scenario_id]['candidate'] is not None
                        or row['cells'][scenario_id]['status'] == 'complete')
                   for row in rows)
    })
    scenario_gaps = []
    for scenario_id in scenario_ids:
        feasible = [row for row in rows if row['cells'][scenario_id]['status'] in FEASIBLE_STATES
                    and (row['cells'][scenario_id]['candidate'] is not None
                         or row['cells'][scenario_id]['status'] == 'complete')]
        if feasible:
            continue
        gaps = [item for item in guarantee_failures if item['scenario'] == scenario_id]
        problems = [item for item in conflicts if item['scenario'] == scenario_id]
        state_name = 'no_feasible_portfolio'
        if any(row['cells'][scenario_id]['status'] in UNPROVEN_STATES for row in rows):
            state_name = 'unresolved'
        scenario_gaps.append({
            'scenario': scenario_id,
            'state': state_name,
            'guarantee_gaps': gaps,
            'data_conflicts': problems,
        })

    matrix = []
    benchmark_by_scenario = {row['scenario']: row for row in benchmarks}
    for row in rows:
        for scenario_id in scenario_ids:
            cell = row['cells'][scenario_id]
            regret = _regret(cell, benchmark_by_scenario[scenario_id])
            matrix.append({
                'portfolio': row['name'],
                'projects': row['projects'],
                'investment': row['investment'],
                'scenario': scenario_id,
                'status': cell['status'],
                'served': cell['served'],
                'served_lower': cell['served_lower'],
                'served_upper': cell['served_upper'],
                'allocation_cost': cell['allocation_cost'],
                'benchmark': benchmark_by_scenario[scenario_id]['served'],
                'benchmark_lower': benchmark_by_scenario[scenario_id]['lower'],
                'benchmark_upper': benchmark_by_scenario[scenario_id]['upper'],
                'benchmark_exact': benchmark_by_scenario[scenario_id]['exact'],
                'regret': regret['regret'],
                'regret_lower': regret['regret_lower'],
                'regret_upper': regret['regret_upper'],
                'worst_service_scenario': row['metrics']['worst_service_scenario'] if row['metrics'] else None,
                'worst_regret_scenario': row['metrics']['worst_regret_scenario'] if row['metrics'] else None,
                'catalog_digest': cell['catalog_digest'],
                'result_digest': cell['result_digest'],
                'shortfalls': cell['shortfalls'],
                'error': cell['error'],
            })

    project_summary = _project_summary(cases['proposals'])
    scenario_summary = _scenario_summary(scenarios, catalogs)
    allocations = _build_allocations(rows, benchmarks, scenario_ids, catalogs, mapping) if include_allocations else []
    decision = {
        'version': 1,
        'input_digest': base.fingerprint,
        'budget': selected_budget,
        'node_limit': node_limit,
        'max_cases': max_cases,
        'project_digest': project_summary['digest'],
        'scenario_digest': scenario_summary['digest'],
        'projects': project_summary['projects'],
        'scenarios': scenario_summary['scenarios'],
        'enumeration': {
            'complete': enumeration_complete,
            'legal_portfolios': legal_portfolios,
            'evaluated': len(rows),
            'max_cases': max_cases,
        },
        'exact': state == 'optimal',
        'state': state,
        'benchmarks': benchmarks,
        'portfolios': [{
            'name': row['name'],
            'projects': row['projects'],
            'investment': row['investment'],
            'supported': row['metrics'] is not None,
            'metrics': row['metrics'],
            'worst_service_scenario': row['metrics']['worst_service_scenario'] if row['metrics'] else None,
            'worst_regret_scenario': row['metrics']['worst_regret_scenario'] if row['metrics'] else None,
            'selected': row['selected'],
            'cell_states': {scenario_id: row['cells'][scenario_id]['status'] for scenario_id in scenario_ids},
        } for row in rows],
        'matrix': matrix,
        'decision': {
            'selected': None if selected is None else selected['name'],
            'projects': [] if selected is None else selected['projects'],
            'investment': None if selected is None else selected['investment'],
            'metrics': None if selected is None else selected['metrics'],
            'rank_key': None if selected is None else _rank_record(selected['metrics']),
            'candidates': [{
                'portfolio': row['name'],
                'projects': row['projects'],
                'investment': row['investment'],
                'metrics': row['metrics'],
                'rank_key': _rank_record(row['metrics']),
            } for row in candidates],
            'trigger_scenarios': trigger_scenarios,
            'scenario_gaps': scenario_gaps,
            'leading_candidate': None if leading_candidate is None else {
                'portfolio': leading_candidate['name'],
                'projects': leading_candidate['projects'],
                'investment': leading_candidate['investment'],
                'metrics': leading_candidate['metrics'],
                'rank_key': _rank_record(leading_candidate['metrics']),
                'note': 'ranked first among searched portfolios only; not a proved optimum',
            },
            'interpretation': {
                'scenarios_carry_no_probability': True,
                'portfolio_fixed_once_and_shared_by_all_scenarios': True,
                'reallocation_only_within_the_chosen_portfolio': True,
                'minimum_shortfall_is_not_zero_service_success': True,
                'search_truncation_is_not_optimality': True,
            },
        },
        'infeasible_guarantees': guarantee_failures,
        'data_conflicts': conflicts,
        'incomplete_cells': incomplete_cells,
        'allocations': allocations,
    }
    decision['decision_digest'] = digest({key: value for key, value in decision.items() if key not in ('allocations', 'decision_digest')})
    return decision


def matrix_rows(decision):
    return list(decision['matrix'])


def _file_digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _manifest(payload, decision):
    return {
        'version': 1,
        'input_digest': decision['input_digest'],
        'project_digest': decision['project_digest'],
        'scenario_digest': decision['scenario_digest'],
        'decision_digest': decision['decision_digest'],
        'files': {name: _file_digest(content) for name, content in payload.items()},
    }


def _write_payload(destination, payload, manifest):
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.robust-', dir=target.parent))
    try:
        for name, content in payload.items():
            path = temporary / name
            with path.open('w', encoding='utf-8', newline='') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
        path = temporary / 'manifest.json'
        with path.open('w', encoding='utf-8', newline='') as output:
            output.write(canonical(manifest))
            output.flush()
            os.fsync(output.fileno())
        os.rename(temporary, target)
    except BaseException:
        shutil.rmtree(temporary)
        raise
    return target


def _check_bundle_identity(document, cases, decision):
    base, budget, mapping, scenarios, catalogs = prepare(document, cases, decision.get('budget'))
    if decision.get('input_digest') != base.fingerprint:
        raise ValueError('base catalog digest does not match decision')
    if decision.get('budget') != budget:
        raise ValueError('budget does not match planning cases')
    project_summary = _project_summary(cases['proposals'])
    if decision.get('project_digest') != project_summary['digest']:
        raise ValueError('project summary does not match planning cases')
    if canonical(decision.get('projects')) != canonical(project_summary['projects']):
        raise ValueError('project summary contents changed')
    scenario_summary = _scenario_summary(scenarios, catalogs)
    if decision.get('scenario_digest') != scenario_summary['digest']:
        raise ValueError('scenario summary does not match planning cases')
    if canonical(decision.get('scenarios')) != canonical(scenario_summary['scenarios']):
        raise ValueError('scenario summary contents changed')
    if decision.get('decision_digest') != digest({key: value for key, value in decision.items() if key not in ('allocations', 'decision_digest')}):
        raise ValueError('decision digest does not match decision body')
    _verify_allocations(decision.get('allocations', []), mapping, scenarios, catalogs)
    return base, budget, mapping, scenarios, catalogs


def save_decision(document, cases, decision, destination):
    """Persist a previously computed decision with its input and case file."""
    base, budget, mapping, scenarios, catalogs = _check_bundle_identity(document, cases, decision)
    payload = {
        'input.json': canonical(base.document),
        'cases.json': canonical(cases),
        'decision.json': canonical(decision),
    }
    _write_payload(destination, payload, _manifest(payload, decision))
    return verify_decision_bundle(destination, recompute=False)


def compute_and_save(document, cases, destination, budget=None, max_cases=10000, node_limit=100000):
    decision = robust_decision(document, cases, budget=budget, max_cases=max_cases, node_limit=node_limit)
    return save_decision(document, cases, decision, destination)


def _read_regular_json(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError('expected bounded regular JSON file: ' + str(path))
    return json.loads(path.read_text(encoding='utf-8'))


def verify_decision_bundle(directory, recompute=True):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise ValueError('decision bundle must be a real directory')
    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file() or manifest_path.stat().st_size > 2_000_000:
        raise ValueError('missing bounded regular manifest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
        raise ValueError('unsupported decision manifest')
    names = set(manifest['files']) | {'manifest.json'}
    if {path.name for path in root.iterdir()} != names:
        raise ValueError('decision bundle file set mismatch')
    for name, expected in manifest['files'].items():
        if not name or Path(name).name != name or name in ('.', '..'):
            raise ValueError('unsafe manifest filename')
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 200_000_000:
            raise ValueError('decision bundle file is not a bounded regular file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('decision bundle checksum mismatch: ' + name)
    for required in ('input.json', 'cases.json', 'decision.json'):
        if required not in manifest['files']:
            raise ValueError('decision bundle is missing ' + required)
    document = _read_regular_json(root / 'input.json', 50_000_000)
    cases = _read_regular_json(root / 'cases.json', 10_000_000)
    decision = _read_regular_json(root / 'decision.json', 200_000_000)
    base, budget, mapping, scenarios, catalogs = _check_bundle_identity(document, cases, decision)
    if manifest.get('decision_digest') != decision.get('decision_digest'):
        raise ValueError('manifest decision digest mismatch')
    if recompute:
        rebuilt = robust_decision(
            document, cases,
            budget=decision.get('budget'),
            max_cases=decision.get('max_cases', 10000),
            node_limit=decision.get('node_limit', 100000),
            include_allocations=True,
        )
        stored_body = {key: value for key, value in decision.items() if key != 'allocations'}
        rebuilt_body = {key: value for key, value in rebuilt.items() if key != 'allocations'}
        if canonical(rebuilt_body) != canonical(stored_body):
            raise ValueError('recomputed decision differs from saved decision')
        if decision.get('allocations') and canonical(rebuilt['allocations']) != canonical(decision['allocations']):
            raise ValueError('recomputed allocations differ from saved allocations')
    return {
        'verified': True,
        'input_digest': base.fingerprint,
        'project_digest': decision['project_digest'],
        'scenario_digest': decision['scenario_digest'],
        'decision_digest': decision['decision_digest'],
        'state': decision.get('state'),
        'selected': decision.get('decision', {}).get('selected'),
        'projects': len(decision.get('projects', [])),
        'scenarios': len(decision.get('scenarios', [])),
        'allocations': len(decision.get('allocations', [])),
        'files': len(manifest['files']) + 1,
    }


def load_decision(directory, recompute=True):
    summary = verify_decision_bundle(directory, recompute=recompute)
    root = Path(directory)
    return {
        'summary': summary,
        'document': _read_regular_json(root / 'input.json', 50_000_000),
        'cases': _read_regular_json(root / 'cases.json', 10_000_000),
        'decision': _read_regular_json(root / 'decision.json', 200_000_000),
    }


def _csv(rows, columns):
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in columns})
    return stream.getvalue()


def export_decision(directory, destination):
    loaded = load_decision(directory, recompute=False)
    document, cases, decision = loaded['document'], loaded['cases'], loaded['decision']
    matrix_columns = [
        'portfolio', 'projects', 'investment', 'scenario', 'status', 'served',
        'served_lower', 'served_upper', 'allocation_cost', 'benchmark',
        'benchmark_lower', 'benchmark_upper', 'benchmark_exact', 'regret',
        'regret_lower', 'regret_upper', 'worst_service_scenario',
        'catalog_digest', 'result_digest',
    ]
    benchmark_columns = ['scenario', 'served', 'exact', 'lower', 'upper', 'state', 'witnesses']
    allocation_columns = ['portfolio', 'scenario', 'period', 'demand', 'stage', 'room', 'building', 'students', 'minutes']
    assignment_rows = []
    for item in decision.get('allocations', []):
        for assignment in (item.get('result') or {}).get('assignments', []):
            assignment_rows.append({'portfolio': item['portfolio'], 'scenario': item['scenario'], **assignment})
    payload = {
        'input.json': canonical(document),
        'cases.json': canonical(cases),
        'decision.json': canonical(decision),
        'matrix.csv': _csv([
            {**row, 'projects': '+'.join(row['projects']) if row['projects'] else 'baseline'}
            for row in decision['matrix']
        ], matrix_columns),
        'benchmarks.csv': _csv([
            {**row, 'witnesses': '+'.join(row['witnesses']) if row['witnesses'] else ''}
            for row in decision['benchmarks']
        ], benchmark_columns),
        'allocations.csv': _csv(assignment_rows, allocation_columns),
    }
    _write_payload(destination, payload, _manifest(payload, decision))
    result = verify_decision_bundle(destination, recompute=False)
    # If a complete allocation set was stored, the saved bundle must recompute
    # identically.  An export without allocations is verified by checksums only.
    if decision.get('allocations'):
        recomputed = verify_decision_bundle(destination, recompute=True)
        if recomputed['decision_digest'] != decision['decision_digest']:
            raise ValueError('exported decision does not recompute')
    return {
        'exported': str(destination),
        'verified': result['verified'],
        'matrix_rows': len(decision['matrix']),
        'benchmark_rows': len(decision['benchmarks']),
        'assignment_rows': len(assignment_rows),
        'files': result['files'],
    }
