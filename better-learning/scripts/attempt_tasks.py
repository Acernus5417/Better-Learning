"""Bounded host tasks; the orchestrator alone commits canonical transcripts."""
import os
import uuid
from pathlib import Path

from _common import atomic_text, inside, read_json, rel, sha256, validation_run, write_json


def estimate_cost(course, item):
    """Conservative scheduling weight, not a claim about billed model tokens."""
    pixels = 0
    if item.get('images'):
        from PIL import Image
    for path in item.get('images', []):
        with Image.open(inside(course, path)) as image:
            pixels += image.width * image.height
    return (2000 + pixels // 1000 + item.get('native_text_characters', 0) * 2
            + len(item.get('images', [])) * 1000 + len(item.get('warnings', [])) * 500)


def complexity_flags(item):
    flags = list(item.get('complexity_hints', []))
    for key in ('mixed_order_unknown', 'render_required'):
        if item.get(key):
            flags.append(key)
    if len(item.get('images', [])) > 1:
        flags.append('multiple_visual_assets')
    if any('页面过大' in w or '小字' in w for w in item.get('warnings', [])):
        flags.append('large_page_downscaled')
    return sorted(set(flags))


def derive_strict_reasons(item, previous, cost, threshold, explicit=False):
    reasons = list(previous.get('strict_reasons', []))
    if previous.get('status') == 'unresolved': reasons.append('PRIOR_UNRESOLVED')
    if previous.get('failure_kind') == 'UNREADABLE_CONTENT': reasons.append('UNREADABLE')
    if (previous.get('last_result') or {}).get('status') == 'uncertain': reasons.append('MEMBER_UNCERTAIN')
    if complexity_flags(item): reasons.append('HIGH_COMPLEXITY')
    if cost >= threshold: reasons.append('COST_THRESHOLD')
    if explicit: reasons.append('EXPLICIT_STRICT')
    return sorted(set(reasons))


def attempt_by_id(data, aid):
    found = next((a for a in data.get('attempts', []) if a['id'] == aid), None)
    if found is None:
        raise ValueError('Unknown attempt')
    return found


@validation_run
def reserve(course, sid, bid, host, model, batch_no=None):
    from convert_materials import load, find, batch_units, valid_unit, asset_hashes, HOSTS, now, save, TERMINAL, RETRYABLE
    from capability_probe import require
    data = load(course)
    if data['schema_version'] != 3:
        raise ValueError('旧台账需确认成员停止后重新 prepare；不接受旧 canonical 作为新尝试结果')
    if host not in HOSTS:
        raise ValueError('Unknown host')
    source, batch = find(data, sid, bid)
    if batch.get('active_member'):
        raise ValueError('此批已有活跃成员，禁止重复派发')
    from agent_pool import available_slots
    if not available_slots(data, host):
        raise ValueError('已达到宿主成员并发上限')
    selected = []
    for unit in batch_units(source, batch):
        if unit['status'] in TERMINAL:
            valid_unit(course, source, unit)
        elif unit['status'] in RETRYABLE and not unit.get('blocked'):
            if unit.get('attempt_count', 0) >= data.get('max_attempts', 3):
                unit.update(status='needs_review', failure_kind='USER_REVIEW_REQUIRED')
            elif unit.get('processing_route') == 'vision':
                selected.append(unit)
    if not selected:
        save(course, data)
        return {'dispatched': False, 'reason': '没有可派发单元；检查是否需用户复核'}
    require(data, course, host, model)
    strict = [u for u in selected if u.get('next_profile') == 'strict']
    selected = strict[:1] if strict else selected[:data['policy']['bounded_batch_size']]
    profile = 'strict' if strict else 'bounded'
    reasons = sorted({r for u in selected for r in u.get('strict_reasons', [])})
    for unit in selected:
        if asset_hashes(course, unit) != unit['asset_hashes']:
            raise ValueError('ASSET_CHANGED：重新 prepare')
    aid = 'A-' + uuid.uuid4().hex
    member = batch['member'] + '_' + aid[-8:]
    folder = f'_工作区/转写尝试/{aid}'
    attempt = {'id': aid, 'source_id': sid, 'batch_id': bid, 'logical_member': member,
               'dispatch_batch_no': batch_no,
               'host': host, 'model': model, 'host_agent_id': None, 'state': 'reserved',
               'reserved_at': now(), 'started_at': None, 'finished_at': None,
               'unit_ids': [u['id'] for u in selected], 'staging_dir': folder,
               'failure_kind': None, 'failure_message': None, 'commits': {},
               'profile': profile, 'strict_reasons': reasons, 'result_contract': 2,
               'parent_attempt_id': selected[0].get('last_attempt_id'), 'visual_assets': {}}
    tasks = []
    for unit in selected:
        index = source['units'].index(unit)
        tail = ''
        if index:
            prior = source['units'][index - 1]
            if prior['status'] == 'done' and valid_unit(course, source, prior):
                tail = inside(course, prior['output']).read_text(encoding='utf-8')[-100:]
        unit.update(status='reserved', current_attempt=aid, member=member,
                    attempt_count=unit.get('attempt_count', 0) + 1)
        unit['dispatch_profile'] = profile
        output = f'{folder}/{sid}/{unit["id"]}.md'
        visual = {}
        if profile == 'strict':
            from prepare_visual import strict_assets
            visual = strict_assets(course, sid, unit['id'])
            attempt['visual_assets'][unit['id']] = visual
        inside(course, output).parent.mkdir(parents=True, exist_ok=True)
        tasks.append({'id': unit['id'], 'kind': unit['kind'], 'locator': unit['locator'],
                      'previous_tail': tail, 'assets': [str(inside(course, p)) for p in unit['assets']],
                      'output': str(inside(course, output)),
                      'result': str(inside(course, output.replace('.md', '.result.json'))), 'visual': visual})
    policy = Path(__file__).resolve().parents[1] / 'references/worker-policy.md'
    manifest = folder + '/task.json'
    write_json(inside(course, manifest), {'schema_version': 1, 'attempt_id': aid,
        'source_id': sid, 'batch_id': bid, 'policy_path': str(policy), 'profile': profile,
        'strict_reasons': reasons, 'result_contract': 2,
        'tools': {k: HOSTS[host][k] for k in ('read_text', 'read_image', 'write')}, 'units': tasks})
    attempt['task_manifest'] = manifest
    data.setdefault('attempts', []).append(attempt)
    batch.update(active_member=member, current_attempt=aid, assigned=attempt['unit_ids'],
                 host=host, model=model, attempts=batch.get('attempts', 0) + 1)
    save(course, data)
    bootstrap = (f'用宿主允许的文本读取工具读取策略 {policy} 和任务 {inside(course, manifest)}，'
                 '按策略执行。仅访问这两个文件及任务分配的资产/暂存输出；不继承历史、不派生。只回短状态。')
    return {'dispatched': True, 'attempt': aid, 'profile': profile, 'member': member, 'state': 'reserved',
            'bootstrap': bootstrap, 'host_action': HOSTS[host]['spawn'],
            'next': '宿主启动成功后 mark-running --attempt ID --agent-id REAL_ID；明确未启动则 fail-attempt --kind SPAWN_FAILURE'}


@validation_run
def mark_running(course, aid, agent_id):
    from convert_materials import load_ledger, find, save, now
    data = load_ledger(course)
    attempt = attempt_by_id(data, aid)
    if not agent_id.strip() or attempt['state'] != 'reserved':
        raise ValueError('Only reserved attempts can bind a real host agent ID')
    if any(a.get('host_agent_id') == agent_id for a in data['attempts']):
        raise ValueError('Host agent ID already bound; each attempt needs a fresh context')
    attempt.update(state='running', host_agent_id=agent_id, started_at=now())
    source, _ = find(data, attempt['source_id'], attempt['batch_id'])
    for u in source['units']:
        if u.get('current_attempt') == aid:
            u['status'] = 'running'
    save(course, data)
    return {'attempt': aid, 'state': 'running'}


def release(data, attempt):
    from convert_materials import find
    _, batch = find(data, attempt['source_id'], attempt['batch_id'])
    batch.update(active_member=None, assigned=[], current_attempt=None)


@validation_run
def fail_attempt(course, aid, kind, stopped_agent_id=None, message=''):
    from convert_materials import load_ledger, find, save, now
    kinds = {'SPAWN_FAILURE', 'TEMP_TOOL_FAILURE', 'WRITE_FAILURE', 'VISION_UNAVAILABLE',
             'UNREADABLE_CONTENT', 'INVALID_OUTPUT', 'ASSET_CHANGED', 'USER_REVIEW_REQUIRED', 'TIMED_OUT'}
    data = load_ledger(course)
    attempt = attempt_by_id(data, aid)
    if kind not in kinds or attempt['state'] not in {'reserved', 'running'}:
        raise ValueError('Invalid failure transition')
    if attempt['state'] == 'reserved' and kind != 'SPAWN_FAILURE':
        raise ValueError('预约状态只能报告已确认的 SPAWN_FAILURE；启动结果不确定时先查询宿主并绑定 ID')
    if attempt['state'] == 'running' and stopped_agent_id != attempt['host_agent_id']:
        raise ValueError('先通过宿主确认停止，提供匹配的 --stopped-agent-id')
    if kind == 'SPAWN_FAILURE' and attempt['state'] != 'reserved':
        raise ValueError('SPAWN_FAILURE only applies to confirmed failure to create a member')
    source, _ = find(data, attempt['source_id'], attempt['batch_id'])
    for u in source['units']:
        if u.get('current_attempt') == aid:
            classify(u, kind, data, message)
    if kind == 'VISION_UNAVAILABLE':
        block_vision(course)
    attempt.update(state='failed', failure_kind=kind, failure_message=message, finished_at=now())
    release(data, attempt)
    save(course, data, render=True)
    return {'attempt': aid, 'state': 'failed', 'failure_kind': kind,
            'next': '本批其余成员确认停止并收集后，再 pump 下一批'}


def classify(unit, kind, data, message=''):
    blocked = kind in {'VISION_UNAVAILABLE', 'ASSET_CHANGED', 'USER_REVIEW_REQUIRED'}
    exhausted = unit.get('attempt_count', 0) >= data.get('max_attempts', 3)
    state = 'needs_review' if exhausted or blocked else 'unresolved' if kind in {'UNREADABLE_CONTENT', 'MEMBER_UNCERTAIN'} else 'failed'
    if kind in {'UNREADABLE_CONTENT', 'MEMBER_UNCERTAIN'}:
        unit['next_profile'] = 'strict'
        unit['strict_reasons'] = sorted(set(unit.get('strict_reasons', []) + ['UNREADABLE' if kind == 'UNREADABLE_CONTENT' else 'MEMBER_UNCERTAIN']))
    unit.update(status=state, failure_kind=kind, error=message or kind,
                blocked=blocked, current_attempt=None)


def block_vision(course):
    from capability_probe import read_state, save_state
    state = read_state(course)
    state['capability_blocked'] = True
    state.setdefault('probe', {})['passed'] = False
    save_state(course, state)


@validation_run
def collect_attempt(course, sid=None, bid=None, aid=None, stopped_agent_id=None):
    from convert_materials import load_ledger, validate_sources, find, save, asset_hashes, assess, now
    data = load_ledger(course)
    if aid is None:
        _, batch = find(data, sid, bid)
        aid = batch.get('current_attempt')
    attempt = attempt_by_id(data, aid)
    if attempt['state'] in {'committed', 'collected'}:
        return {'attempt': aid, 'state': attempt['state'], 'idempotent': True}
    if attempt['state'] not in {'running', 'produced'} or stopped_agent_id != attempt['host_agent_id']:
        raise ValueError('收集前须确认宿主成员停止并提供匹配的 --stopped-agent-id；reserved 不是已启动')
    source, _ = find(data, attempt['source_id'], attempt['batch_id'])
    try:
        validate_sources(course, data)
        source_changed = False
    except (OSError, ValueError):
        source_changed = True
    results = []
    for unit in source['units']:
        if unit['id'] not in attempt['unit_ids']:
            continue
        canonical = inside(course, unit['output'])
        staged = inside(course, f"{attempt['staging_dir']}/{source['id']}/{unit['id']}.md")
        kind = None
        try:
            if source_changed or asset_hashes(course, unit) != unit['asset_hashes']:
                kind = 'ASSET_CHANGED'
                raise ValueError('来源或提取资产发生变化')
            receipt = attempt['commits'].get(unit['id'])
            # Resume a crash after atomic replace, using the journal persisted before replace.
            candidate = canonical if receipt and not staged.exists() else staged
            if not candidate.is_file():
                kind = 'WRITE_FAILURE'
                raise ValueError('本次 attempt 没有落盘文件；旧正式输出不能替代')
            text = candidate.read_text(encoding='utf-8')
            if receipt and sha256(candidate) != receipt:
                kind = 'INVALID_OUTPUT'
                raise ValueError('提交日志与输出不一致')
            sidecar = staged.with_suffix('.result.json')
            if not sidecar.is_file():
                kind = 'WRITE_FAILURE'
                raise ValueError('缺少本次 result sidecar')
            try:
                result = read_json(sidecar)
                if (result.get('schema_version') != 1 or result.get('status') not in
                        {'complete', 'uncertain', 'unreadable', 'vision_unavailable'}
                        or not isinstance(result.get('tiles_read', []), list)):
                    raise ValueError('Invalid result contract')
            except (ValueError, TypeError, AttributeError):
                kind = 'INVALID_OUTPUT'
                raise ValueError('result sidecar 格式错误')
            unit['last_result'] = result
            unit['last_attempt_id'] = aid
            kind = {'uncertain': 'MEMBER_UNCERTAIN', 'unreadable': 'UNREADABLE_CONTENT',
                    'vision_unavailable': 'VISION_UNAVAILABLE'}.get(result['status'])
            visual = attempt.get('visual_assets', {}).get(unit['id'], {})
            for p, h in visual.get('hashes', {}).items():
                if sha256(inside(course, p)) != h:
                    kind = 'ASSET_CHANGED'
                    raise ValueError('Strict visual asset changed')
            if not kind and attempt.get('profile') == 'strict':
                expected = {t['id'] for t in visual['tiles']}
                if set(result['tiles_read']) != expected or len(result['tiles_read']) != len(expected):
                    kind = 'INVALID_OUTPUT'
                    raise ValueError('strict 必须覆盖全部 tile ID')
            if not kind and (not text.strip() or '<!-- BL-' in text):
                kind = 'INVALID_OUTPUT'
            if kind:
                raise ValueError(kind)
            attempt['commits'][unit['id']] = sha256(candidate)
            attempt['state'] = 'produced'
            save(course, data)
            if candidate == staged:
                canonical.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, canonical)
            assess(course, source, unit, attempt['logical_member'], structured=True)
            if unit['status'] != 'done':
                raise ValueError('提交后验证失败')
            meta = read_json(inside(course, unit['meta']))
            meta.update(attempt_id=aid, host_agent_id=attempt['host_agent_id'], result=result,
                        profile=attempt['profile'], visual_assets=visual)
            write_json(inside(course, unit['meta']), meta)
            unit.update(current_attempt=None, committed_attempt=aid)
            unit.pop('failure_kind', None)
        except (OSError, ValueError) as exc:
            kind = kind or 'WRITE_FAILURE'
            classify(unit, kind, data, str(exc))
            if kind == 'VISION_UNAVAILABLE':
                block_vision(course)
        results.append({'unit': unit['id'], 'status': unit['status'], 'failure_kind': kind})
    failed = [r for r in results if r['status'] != 'done']
    attempt.update(state='collected' if failed else 'committed', finished_at=now(), results=results,
                   failure_kind=failed[0]['failure_kind'] if failed else None)
    release(data, attempt)
    save(course, data, render=bool(failed))
    return {'attempt': aid, 'state': attempt['state'], 'results': results,
            'capability_blocked': any(r['failure_kind'] == 'VISION_UNAVAILABLE' for r in results)}


@validation_run
def pump(course, host, model, max_fill=None, host_limit=None):
    """Reserve one whole dispatch batch.

    Batches are serialized: while any attempt of the current batch is still
    reserved/running/produced, no new ticket is issued. Slot refill on
    completion is intentionally absent — the orchestrator collects the whole
    batch first and only then pumps the next one.
    """
    from convert_materials import load, save, batch_units
    from capability_probe import require
    from agent_pool import active_attempts, available_slots, priority_key
    data = load(course)
    if host_limit is not None:
        if not 1 <= host_limit <= 8:
            raise ValueError('host-limit must be 1..8')
        data.setdefault('host_limits', {})[host] = host_limit
        save(course, data)
    if max_fill is not None and max_fill < 0:
        raise ValueError('max-fill cannot be negative')
    in_flight = active_attempts(data)
    if in_flight:
        return {'tickets': [], 'blocked': None, 'batch_open': True,
                'batch_no': data.get('current_batch_no', 0),
                'active': [a['id'] for a in in_flight],
                'hint': '本批仍有 %d 个未收集的成员；逐个确认宿主停止并 collect，'
                        '全部结束后再 pump 下一批。批内不补位。' % len(in_flight)}
    tickets = []
    blocked = None
    limit = available_slots(data, host)
    if max_fill is not None:
        limit = min(limit, max_fill)
    batch_no = data.get('current_batch_no', 0) + 1
    for _ in range(limit):
        data = load(course)
        ready = []
        for source in data['sources']:
            for batch in source['batches']:
                if batch.get('active_member'): continue
                units = [u for u in batch_units(source, batch) if u['status'] in {'pending', 'failed', 'unresolved'}
                         and not u.get('blocked') and u.get('processing_route') == 'vision'
                         and u.get('attempt_count', 0) < data['max_attempts']]
                if units:
                    ready.append((min(priority_key(u) for u in units), source['id'], batch['id']))
        if not ready: break
        try:
            require(data, course, host, model)
            _, sid, bid = min(ready)
            ticket = reserve(course, sid, bid, host, model, batch_no)
            ticket['batch_no'] = batch_no
            tickets.append(ticket)
        except (OSError, ValueError, KeyError) as exc:
            blocked = str(exc)
            break
    if tickets:
        data = load(course)
        data['current_batch_no'] = batch_no
        save(course, data)
    return {'tickets': tickets, 'blocked': blocked, 'batch_open': False, 'batch_no': batch_no if tickets else data.get('current_batch_no', 0)}
