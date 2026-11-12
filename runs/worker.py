import time
from estate.catalog import parse
from allocation.solver import solve, SearchLimit, InfeasibleMinimum
from allocation.verify import require_valid_result
from runs.store import Conflict, StateError


def execute(store, run_id, owner='local', node_limit=100000, clock=None, duration=3600):
    clock = clock or (lambda: int(time.time()))
    token = store.claim(run_id, owner, clock(), duration)
    run = store.run(run_id)
    scenario = store.scenario(run['scenario'])
    catalog = parse(scenario['body'])
    try:
        result = solve(catalog, node_limit)
        require_valid_result(catalog, result)
    except (SearchLimit, InfeasibleMinimum, ValueError) as exc:
        store.fail(token, type(exc).__name__ + ': ' + str(exc), clock())
        raise
    return store.complete(token, result, clock())


def drain(store, owner='local', limit=10, node_limit=100000, clock=None):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError('limit must be 1..1000')
    clock = clock or (lambda: int(time.time()))
    outcomes = []
    for row in store.queue(clock())[:limit]:
        try:
            result = execute(store, row['id'], owner, node_limit, clock)
        except Conflict as exc:
            outcomes.append({'id': row['id'], 'state': 'conflict', 'error': str(exc)})
        except (SearchLimit, InfeasibleMinimum, ValueError) as exc:
            outcomes.append({'id': row['id'], 'state': 'failed', 'error': str(exc)})
        else:
            outcomes.append({'id': row['id'], 'state': result['state'], 'digest': result['result_digest']})
    return outcomes


def run_scenario(store, scenario_id, request_key, node_limit=100000):
    run = store.enqueue(scenario_id, request_key)
    if run['state'] == 'complete':
        return run
    if run['state'] == 'failed':
        raise StateError('request already failed; explicitly retry before execution')
    return execute(store, run['id'], node_limit=node_limit)
