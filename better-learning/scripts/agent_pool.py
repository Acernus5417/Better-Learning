"""Shared, host-neutral pool bookkeeping. Call mutations under the course lock."""
ACTIVE_STATES = {'reserved', 'running', 'produced'}
INFRA_FAILURES = {'SPAWN_FAILURE', 'TEMP_TOOL_FAILURE', 'WRITE_FAILURE', 'TIMED_OUT', 'INVALID_OUTPUT'}


def active_attempts(data):
    return [a for a in data.get('attempts', []) if a['state'] in ACTIVE_STATES]


def batch_open(data):
    """A dispatch batch is still in flight; the next batch may not start yet."""
    return bool(active_attempts(data))


def available_slots(data, host=None):
    limit = min(data['max_concurrent'], data.get('host_limits', {}).get(host, 8))
    return max(0, limit - len(active_attempts(data)))


def priority_key(unit):
    return (0 if unit.get('next_profile') == 'strict' else
            1 if unit.get('failure_kind') in INFRA_FAILURES else 2)


def require_stopped(attempt, stopped_agent_id):
    if attempt['state'] not in {'running', 'produced'} or not stopped_agent_id or stopped_agent_id != attempt['host_agent_id']:
        raise ValueError('必须先确认宿主成员已停止，并提供匹配的真实 Agent ID')
