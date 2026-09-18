"""Prepare and validate host-subagent transcription. Never call models, OCR, or a CLI."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import posixpath
import re
import shutil
from urllib.parse import unquote, urlsplit

from _common import validation_run, validation_context, atomic_text, extraction_path, inside, read_json, read_jsonl, rel, sha256, source_index, write_json
from extract_materials import extract

LEDGER = '_工作区/转写任务.json'
TERMINAL = {'done', 'non_teaching', 'duplicate'}
RETRYABLE = {'pending', 'failed', 'unresolved'}

from host_agents import HOSTS, DEFAULT_HOST



def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def load_ledger(course):
    data = read_json(course / LEDGER)
    if data.get('schema_version') not in {1, 2, 3} or data.get('engine') != 'host-subagent':
        raise ValueError('Unsupported transcription ledger')
    return data


def validate_sources(course, data):
    current = source_index(course)['sources']
    if {(s['id'], s['sha256'], s['path']) for s in current} != {
            (s['id'], s['sha256'], s['path']) for s in data['sources']}:
        raise ValueError('资料范围已改变；确认旧成员已停止后重新 prepare')
    for source in current:
        if sha256(Path(source['path'])) != source['sha256']:
            raise ValueError('原件改变；重新盘点完整资料范围后 prepare')
    return data


def load(course):
    return validate_sources(course, load_ledger(course))


@contextmanager
def locked(course):
    folder = course / '_工作区'
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / '转写任务.lock'
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('调度器正在写台账；确认无调度命令运行后才可移除过期锁') from exc
    try:
        with handle:
            handle.write(now())
        yield
    finally:
        lock.unlink(missing_ok=True)


def find(data, sid, bid=None, uid=None):
    ctx = validation_context()
    if ctx is not None:
        if not hasattr(ctx, 'lookups'):
            ctx.lookups = {}
        key = ('find', id(data))
        if key not in ctx.lookups:
            sources = {s['id']: s for s in data['sources']}
            units = {(s['id'], u['id']): u for s in data['sources'] for u in s['units']}
            batches = {(s['id'], b['id']): b for s in data['sources'] for b in s['batches']}
            ctx.lookups[key] = (data, sources, units, batches)
        _, sources, units, batches = ctx.lookups[key]
        source = sources.get(sid)
    else:
        source = next((s for s in data['sources'] if s['id'] == sid), None)
    if source is None:
        raise ValueError('Unknown source')
    if uid is not None:
        unit = units.get((sid, uid)) if ctx else next((u for u in source['units'] if u['id'] == uid), None)
        if unit is None:
            raise ValueError('Unknown unit')
        return source, unit
    batch = batches.get((sid, bid)) if ctx else next((b for b in source['batches'] if b['id'] == bid), None)
    if batch is None:
        raise ValueError('Unknown batch')
    return source, batch


def batch_units(source, batch):
    ctx = validation_context()
    key = ('units', id(source), len(source['units']))
    if ctx is not None:
        if not hasattr(ctx, 'lookups'):
            ctx.lookups = {}
        if key not in ctx.lookups:
            ctx.lookups[key] = {u['id']: u for u in source['units']}
        units = ctx.lookups[key]
    else:
        units = {u['id']: u for u in source['units']}
    return [units[uid] for uid in batch['units']]


def asset_hashes(course, unit):
    return {p: sha256(inside(course, p)) for p in unit['assets']}


def valid_unit(course, source, unit):
    """Verify an accepted result against its source, inputs and independently saved metadata."""
    if unit['status'] not in TERMINAL:
        return False
    meta = read_json(inside(course, unit['meta']))
    output = inside(course, unit['output'])
    if (meta.get('source_hash') != source['sha256'] or meta.get('unit_id') != unit['id']
            or meta.get('status') != unit['status'] or meta.get('output_hash') != sha256(output)
            or meta.get('output_hash') != unit.get('output_hash')
            or meta.get('asset_hashes') != asset_hashes(course, unit)
            or meta.get('asset_hashes') != unit['asset_hashes']):
        raise ValueError('来源/资源/转写/meta 哈希不一致：' + source['id'] + '/' + unit['id'])
    if unit['status'] != 'done':
        evidence = inside(course, unit['resolution']['evidence'])
        if sha256(evidence) != unit['resolution']['evidence_hash']:
            raise ValueError('排除依据发生变化')
        if unit['status'] == 'duplicate':
            target = next(u for u in source['units'] if u['id'] == unit['resolution']['duplicate_of'])
            if target['status'] != 'done':
                raise ValueError('重复单元目标必须是同来源的已完成正文单元')
            valid_unit(course, source, target)
    if meta.get('profile') == 'strict':
        visual = meta['visual_assets']
        if set(meta['result']['tiles_read']) != {t['id'] for t in visual['tiles']}:
            raise ValueError('Strict tile coverage incomplete')
        if any(sha256(inside(course, p)) != h for p, h in visual['hashes'].items()):
            raise ValueError('Strict evidence changed')
    return True


def summarize(data):
    units = [u for s in data['sources'] for u in s['units']]
    counts = {state: sum(u['status'] == state for u in units)
              for state in ('pending', 'reserved', 'running', 'done', 'unresolved', 'failed', 'needs_review', 'non_teaching', 'duplicate')}
    result = {'sources': len(data['sources']), 'units': len(units), 'counts': counts,
              'plan': '转写拆分计划.md', 'ledger': LEDGER}
    active = [a for a in data.get('attempts', []) if a['state'] in {'reserved', 'running', 'produced'}]
    result['active'] = [{'attempt': a['id'], 'state': a['state'], 'host_agent_id': a['host_agent_id'],
                         'member': a.get('logical_member'), 'task_manifest': a.get('task_manifest'),
                         'profile': a.get('profile'), 'host': a.get('host')} for a in active]
    available = max(0, data['max_concurrent'] - len(active))
    ready = []
    for source in data['sources']:
        for batch in source['batches']:
            if not batch.get('active_member') and any(
                    u['status'] in RETRYABLE and not u.get('blocked')
                    and u.get('processing_route') == 'vision'
                    and u.get('attempt_count', 0) < data.get('max_attempts', 3)
                    for u in batch_units(source, batch)):
                ready.append({'source': source['id'], 'batch': batch['id']})
    result['next_batches'] = ready[:available]
    result.update(active_count=len(active), available_slots=available,
                  batch={'open': bool(active), 'no': data.get('current_batch_no', 0),
                         'active': [a['id'] for a in active],
                         'next': ('本批未收口：逐个确认宿主停止并 collect，全部结束后再 pump 下一批（批内不补位）'
                                  if active else '本批已收口，可以 pump 下一批')},
                  strict_ready=sum(u.get('next_profile') == 'strict' and u['status'] in RETRYABLE and not u.get('blocked') for u in units),
                  bounded_ready=sum(u.get('next_profile') == 'bounded' and u['status'] in RETRYABLE and not u.get('blocked') for u in units))
    scanned = [s['id'] + '（' + str(len(s['units'])) + ' 单元）' for s in data['sources'] if s.get('scanned')]
    if scanned:
        result['scanned_sources'] = scanned
        result['range_hint'] = ('以下来源无文字层，脚本无法判定其内容覆盖范围：' + '；'.join(scanned) +
                                '。范围不清时向用户确认，或记为「范围待转写阶段核实」后直接 dispatch——'
                                '主代理不得打开页面图自行查看。')
    return result


def save(course, data, render=False):
    attempts = {a['id']: a for a in data.get('attempts', [])}
    for source in data['sources']:
        for batch in source['batches']:
            if batch.get('active_member'):
                batch['status'] = attempts.get(batch.get('current_attempt'), {}).get('state', 'dispatched')
            else:
                states = {u['status'] for u in batch_units(source, batch)}
                batch['status'] = ('done' if states <= TERMINAL else 'needs_review' if 'needs_review' in states else 'failed' if 'failed' in states
                                   else 'unresolved' if 'unresolved' in states else 'pending')
    data['updated_at'] = now()
    write_json(course / LEDGER, data)
    if not render:
        return
    render_plan(course, data)


def render_plan(course, data):
    safe = lambda x: str(x).replace('|', '／').replace('\n', ' ')
    rows = ['# 转写拆分计划', '', '- 生成时间：' + data['generated_at'],
            '- 执行体：宿主可写子 Agent；并发上限：' + str(data['max_concurrent']),
            '- 输入文件数：' + str(len(data['sources'])) + '；单元总数：' +
            str(sum(len(s['units']) for s in data['sources'])), '']
    scanned = [s['id'] for s in data['sources'] if s.get('scanned')]
    if scanned:
        rows += ['- 无文字层来源（内容覆盖范围脚本不可判定）：' + '，'.join(scanned) +
                 '；请向用户确认范围，或记为「待转写阶段核实」，不要读图', '']
    rows += ['## 一、来源清单', '', '| 来源 | 文件 | 格式 | 哈希前12 | 单元数 | 拆分流 |',
             '| --- | --- | --- | --- | --- | --- |']
    for source in data['sources']:
        kinds = {k: sum(u['kind'] == k for u in source['units']) for k in ('page', 'text', 'image')}
        rows.append('| ' + ' | '.join(map(safe, [source['id'], source['name'], source['format'],
                    source['sha256'][:12], len(source['units']), json.dumps(kinds)])) + ' |')
    rows += ['', '## 二、单元清单', '', '| 来源/单元 | 类型 | 定位 | 资源 | 批次 | 成员 | 状态 | Profile | Strict原因 | 成本 | 最后结果 |',
             '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |']
    for source in data['sources']:
        for u in source['units']:
            rows.append('| ' + ' | '.join(map(safe, [source['id'] + '/' + u['id'], u['kind'],
                        u['locator'], ', '.join(u['assets']), u['batch'], u.get('member', ''), u['status'], u.get('next_profile'), ','.join(u.get('strict_reasons', [])), u.get('estimated_cost'), (u.get('last_result') or {}).get('status', '')])) + ' |')
    rows += ['', '## 三、批次分配', '', '| 来源/批次 | 成员 | 宿主 | 单元 | 输出 | 数量 | 状态 |',
             '| --- | --- | --- | --- | --- | --- | --- |']
    for source in data['sources']:
        for b in source['batches']:
            units = batch_units(source, b)
            rows.append('| ' + ' | '.join(map(safe, [source['id'] + '/' + b['id'],
                        b.get('active_member') or b['member'], b.get('host', ''), ', '.join(u['id'] for u in units),
                        '单元正式输出', len(units), b['status']])) + ' |')
    rows += ['', '## 四、批次划分规则', '',
             '普通页 bounded 最多5页；异常页 strict 单视觉单元，overview + tiles 精细识图。'
             '重试仅分配未完成项，疑难单元单独复核。bounded 批内仍有上下文累积。',
             '', '## 五、状态图例', '', 'pending / reserved / running / done / unresolved / failed / needs_review / non_teaching / duplicate',
             '', '## 六、更新规则', '',
             'JSON台账是唯一状态来源，本文件由脚本生成。仅主调度器写台账；成员只写各自单元正文。'
             'collect机械校验；只重派未完成项。旧成员仍在运行时不得回收或重派同一单元。']
    atomic_text(course / '转写拆分计划.md', '\n'.join(rows) + '\n')
    problems = ['# 转写待核实问题', '']
    for source in data['sources']:
        for u in source['units']:
            if u['status'] in {'failed', 'unresolved', 'needs_review'}:
                problems.append(f"- {source['id']}/{u['id']} · {u['locator']}：{u.get('error', u['status'])}；结果：{u['output']}")
    # Keep user-authored problem notes separate from this generated view.
    atomic_text(course / '_工作区/转写待核实问题.md', '\n'.join(problems) + '\n')
    issues = course / '待核实问题.md'
    if not issues.exists():
        atomic_text(issues, '# 待核实问题\n\n转写阶段问题与位置见运行报告 `_工作区/转写待核实问题.md`。\n')


@validation_run
def prepare(course, batch_size=5, max_concurrent=4, chapter_boundaries=None, mode=None, batch_budget=100000, max_attempts=3, office_renders=None, strict_cost_threshold=12000, force_strict_units=None):
    if not 1 <= batch_size <= 5 or not 1 <= max_concurrent <= 8:
        raise ValueError('batch-size must be 1..5; max-concurrent must be 1..8')
    old = read_json(course / LEDGER) if (course / LEDGER).exists() else {}
    if any(b.get('active_member') for s in old.get('sources', []) for b in s['batches']):
        raise ValueError('仍有已派发成员，先 collect 或确认终止后 recover')
    if mode not in {None, 'strict', 'bounded'} or batch_budget < 1 or max_attempts < 1 or strict_cost_threshold < 1:
        raise ValueError('Invalid scheduling limits')
    if old and old.get('schema_version') != 3:
        backup = course / '_工作区' / ('转写任务.v2-' + sha256(course / LEDGER)[:12] + '.json')
        if not backup.exists():
            atomic_text(backup, (course / LEDGER).read_text(encoding='utf-8'))
    renders = read_json(inside(course, office_renders)) if office_renders else {}
    boundaries = read_json(inside(course, chapter_boundaries)) if chapter_boundaries else {}
    data = {'schema_version': 3, 'engine': 'host-subagent', 'generated_at': now(),
            'mode': 'bounded', 'batch_budget': batch_budget, 'max_attempts': max_attempts,
            'current_batch_no': old.get('current_batch_no', 0) if old.get('schema_version') == 3 else 0,
            'policy': {'bounded_batch_size': batch_size, 'max_concurrent': max_concurrent, 'strict_cost_threshold': strict_cost_threshold, 'max_attempts': max_attempts},
            'attempts': old.get('attempts', []) if old.get('schema_version') == 3 else [], 'probe_ref': '_工作区/能力探针/current.json',
            'batch_size': batch_size, 'max_concurrent': max_concurrent, 'sources': []}
    for source in source_index(course)['sources']:
        args = argparse.Namespace(start=1, end=10**9, max_chars=10000, scale=3, office_renders=renders)
        outcome = extract(course, source['id'], args)
        mapping = read_json(extraction_path(course, source) / '定位映射.json')
        starts = boundaries.get(source['id'], [])
        if not isinstance(starts, list) or any(not isinstance(n, int) or n < 1 or n > mapping['total_units'] for n in starts):
            raise ValueError('Invalid chapter boundary units')
        current = dict(source)
        stem = re.sub(r'[<>:"/\\|?*#\[\]%\x00-\x1f]', '_', Path(source['name']).stem)[:40].rstrip('. ')
        current.update(units=[], batches=[], transcript=f"资料转写/{source['id']}-{stem}-{source['sha256'][:12]}-agent-v1.md")
        native = [u['native_text_characters'] for u in mapping['units'] if 'native_text_characters' in u]
        current['scanned'] = bool(native) and not any(native)
        old_source = next((s for s in old.get('sources', []) if s['id'] == source['id'] and s['sha256'] == source['sha256']), {})
        batch, text_chars, cost_sum = None, 0, 0
        previous_units = {u['id']: u for u in old_source.get('units', [])}
        for item in mapping['units']:
            assets = list(dict.fromkeys(item.get('assets') or item['chunks'] + item['images']))
            if item.get('mixed_order_unknown') and item['images']:
                assets = list(dict.fromkeys(item['chunks'] + item['images']))
            kind = item.get('kind') or ('page' if source['format'] == 'pdf' else 'image' if item['images'] else 'text')
            chars = sum(len(inside(course, p).read_text(encoding='utf-8')) for p in item['chunks'])
            route = item.get('processing_route', 'blocked')
            from attempt_tasks import estimate_cost, derive_strict_reasons, complexity_flags
            cost = estimate_cost(course, item)
            prior = previous_units.get(item['id'], {})
            reasons = derive_strict_reasons(item, prior, cost, strict_cost_threshold, mode == 'strict' or source['id'] + '/' + item['id'] in (force_strict_units or [])) if route == 'vision' else []
            profile = 'strict' if reasons else 'bounded'
            if (batch is None or len(batch['units']) >= (1 if profile == 'strict' else batch_size)
                    or batch.get('profile') != profile or batch.get('route') != route
                    or item['ordinal'] in starts or text_chars + chars > 30000
                    or cost_sum + cost > batch_budget):
                bid = f"B{len(current['batches']) + 1:02d}"
                member = f"vis_{source['id'].lower().replace('-', '')}_{bid.lower()}"
                batch = {'id': bid, 'member': member, 'profile': profile, 'route': route, 'status': 'pending', 'units': [], 'attempts': 0}
                current['batches'].append(batch)
                text_chars, cost_sum = 0, 0
            folder = extraction_path(course, source) / item['id']
            unit = {'id': item['id'], 'kind': kind, 'processing_route': route, 'estimated_cost': cost, 'attempt_count': 0,
                    'dispatch_profile': profile, 'next_profile': profile, 'strict_reasons': reasons,
                    'complexity_flags': complexity_flags(item), 'last_attempt_id': prior.get('last_attempt_id'), 'last_result': prior.get('last_result'), 'locator': item['locator'], 'assets': assets,
                    'asset_hashes': {p: sha256(inside(course, p)) for p in assets},
                    'batch': batch['id'], 'member': batch['member'], 'status': 'pending',
                    'output': rel(course, folder / 'transcript-agent.md'), 'meta': rel(course, folder / 'meta.json'),
                    'chars': 0, 'unresolved': False}
            if (route == 'blocked' or item['status'] == 'blocked' or item.get('unresolved_media')
                    or (item.get('mixed_order_unknown') and item['images'])
                    or (kind in {'page', 'image'} and not assets)):
                unit.update(status='failed', blocked=True, error='; '.join(item['warnings']))
            previous = previous_units.get(unit['id'])
            if previous and previous['asset_hashes'] == unit['asset_hashes'] and previous['locator'] == unit['locator']:
                unit['attempt_count'] = previous.get('attempt_count', 0)
                if previous.get('reviews'):
                    unit['reviews'] = previous['reviews']
                if previous['status'] in TERMINAL:
                    try:
                        valid_unit(course, old_source, previous)
                        for key in ('status', 'chars', 'unresolved', 'output_hash', 'resolution', 'committed_attempt'):
                            if key in previous:
                                unit[key] = previous[key]
                    except (OSError, ValueError, KeyError):
                        unit.update(status='failed', error='旧转写证据失效，需要重派')
            if route == 'deterministic_text' and not unit.get('blocked') and unit['status'] not in TERMINAL:
                text = '\n\n'.join(inside(course, p).read_text(encoding='utf-8') for p in item['chunks'])
                atomic_text(inside(course, unit['output']), text)
                assess(course, current, unit, 'deterministic_text')
            current['units'].append(unit)
            batch['units'].append(unit['id'])
            text_chars += chars
            cost_sum += cost
        data['sources'].append(current)
    if not data['sources']:
        raise ValueError('没有已盘点资料')
    save(course, data, render=True)
    return summarize(data)


def probe(course, member, host=DEFAULT_HOST, model=''):
    from capability_probe import finish
    return finish(course, member, host, model)


@validation_run
def dispatch(course, sid, bid, host=DEFAULT_HOST, model=''):
    from attempt_tasks import reserve
    return reserve(course, sid, bid, host, model)



def assess(course, source, unit, member, structured=False):
    try:
        if asset_hashes(course, unit) != unit['asset_hashes']:
            raise ValueError('提取资源发生变化')
        path = inside(course, unit['output'])
        text = path.read_text(encoding='utf-8').strip()
        state = 'unresolved' if not text or (not structured and ('[待核实]' in text or '[不具备读图能力]' in text)) else 'done'
        unit['capability_failed'] = not structured and '[不具备读图能力]' in text
        if '<!-- BL-' in text:
            raise ValueError('正文包含保留的合成标记')
        if not text:
            unit['error'] = '转写为空'
        elif state == 'unresolved':
            unit['error'] = '包含待核实内容，需要重读/修订或有依据的排除'
        else:
            unit.pop('error', None)
        unit.update(status=state, chars=len(text), unresolved=state == 'unresolved', output_hash=sha256(path))
        write_json(inside(course, unit['meta']), {'source_id': source['id'], 'source_hash': source['sha256'],
                   'unit_id': unit['id'], 'member': member, 'status': state, 'chars': len(text),
                   'output_hash': unit['output_hash'], 'asset_hashes': unit['asset_hashes'], 'checked_at': now()})
    except (OSError, ValueError) as exc:
        unit.update(status='failed', chars=0, unresolved=False, error=str(exc))
        unit.pop('output_hash', None)


@validation_run
def collect(course, sid=None, bid=None, stopped_member=None, attempt_id=None, stopped_agent_id=None):
    from attempt_tasks import collect_attempt
    return collect_attempt(course, sid, bid, attempt_id, stopped_agent_id)


@validation_run
def resolve(course, sid, uid, state, reason, evidence, duplicate_of=None):
    data = load(course)
    source, unit = find(data, sid, uid=uid)
    if unit['status'] == 'dispatched' or any(b.get('active_member') for b in source['batches'] if b['id'] == unit['batch']):
        raise ValueError('成员运行中，不能同时修订')
    if state not in {'non_teaching', 'duplicate'} or not reason.strip():
        raise ValueError('只接受有明确理由和证据的排除')
    proof = inside(course, evidence)
    if not proof.read_text(encoding='utf-8').strip() or proof == inside(course, unit['output']):
        raise ValueError('需要独立且非空的核查证据')
    if duplicate_of:
        _, target = find(data, sid, uid=duplicate_of)
        if target['id'] == uid or target['status'] != 'done':
            raise ValueError('重复目标必须为同来源其他已完成单元')
        valid_unit(course, source, target)
    elif state == 'duplicate':
        raise ValueError('duplicate requires --duplicate-of')
    path = inside(course, unit['output'])
    if path.exists():
        atomic_text(path.with_name('transcript-before-resolution.md'), path.read_text(encoding='utf-8'))
    atomic_text(path, f"[{state}] {reason.strip()}\n")
    assess(course, source, unit, 'reviewed-resolution')
    if unit['status'] != 'done':
        raise ValueError('排除记录无效')
    resolution = {'reason': reason, 'evidence': evidence, 'evidence_hash': sha256(proof), 'duplicate_of': duplicate_of}
    unit.update(status=state, resolution=resolution)
    meta = read_json(inside(course, unit['meta']))
    meta.update(status=state, resolution=resolution)
    write_json(inside(course, unit['meta']), meta)
    coverage_path = course / '_工作区/覆盖台账.jsonl'
    rows = read_jsonl(coverage_path) if coverage_path.exists() else []
    row = next((r for r in rows if r.get('source_id') == sid and r.get('unit_id') == uid), None)
    if row is None:
        row = {'source_id': sid, 'unit_id': uid, 'knowledge_ids': []}
        rows.append(row)
    row.update(source_hash=source['sha256'], status=state, reason=reason,
               evidence='已审阅排除依据：' + evidence, manual_read_evidence=evidence)
    if state == 'duplicate':
        target_row = next((r for r in rows if r.get('source_id') == sid and r.get('unit_id') == duplicate_of), {})
        row['knowledge_ids'] = target_row.get('knowledge_ids', [])
        row['duplicate_of'] = duplicate_of
    atomic_text(coverage_path, ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    save(course, data)
    return {'source': sid, 'unit': uid, 'status': state}


@validation_run
def check(course, require_assembled=True):
    data = load(course)
    if any(u.get('processing_route', 'vision') == 'vision' for s in data['sources'] for u in s['units']):
        from capability_probe import require, read_state
        probe_data = read_state(course)
        probe_info = probe_data.get('probe', {})
        try:
            require(probe_data, course, probe_info.get('host'), probe_info.get('model'))
        except (OSError, ValueError, KeyError) as exc:
            return [str(exc)]
    if any(b.get('active_member') for s in data['sources'] for b in s['batches']):
        return ['仍有未结束的转写尝试']
    if data.get('packaged') and not (course / '_工作区' / '提取内容').exists():
        # 已打包交付：结构检查结果保存在 _工作区/结构检查.json，中间产物已按设计清理。
        return []
    errors = []
    for source in data['sources']:
        for unit in source['units']:
            try:
                if not valid_unit(course, source, unit):
                    errors.append(f"{source['id']}/{unit['id']}: {unit['status']}")
            except (OSError, ValueError, KeyError, StopIteration) as exc:
                errors.append(str(exc))
        if require_assembled:
            path = inside(course, source['transcript'])
            if not path.exists() or not source.get('transcript_hash') or sha256(path) != source['transcript_hash']:
                errors.append(source['id'] + ': 来源转写未合成或已变化')
    return errors


@validation_run
def assemble(course):
    errors = check(course, require_assembled=False)
    if errors:
        raise ValueError('全部单元完成后才允许合成：' + '; '.join(errors))
    data = load(course)
    from entity_registry import is_v2
    v2 = is_v2(course)
    index = {'schema_version': 2 if v2 else 1, 'sources': []}
    for source in data['sources']:
        header = (f"# {source['name']}：资料转写\n\n来源：{source['id']}\n\n源版本：{source['sha256']}\n\n"
                  f"执行体：host-subagent / agent-v1\n")
        blocks = [header] if not v2 else [header]
        payloads, transforms = [], []
        for unit in source['units']:
            body = inside(course, unit['output']).read_text(encoding='utf-8').strip()
            mode = 'text' if unit.get('processing_route') == 'deterministic_text' else 'vision'
            if v2:
                uid = f"{source['id']}-{unit['id']}"
                from entity_sections import normalize_payload, payload_hash, reserved_marker_conflicts
                conflicts = reserved_marker_conflicts(body)
                normalized, mapping = normalize_payload(body, [(0, len(body))]) if conflicts else (body, [])
                if conflicts:
                    transforms.append({'unit_id': unit['id'], 'escaped': True,
                                       'original_hash': payload_hash(body, [(0, len(body))]),
                                       'conflicts': [item[1] for item in conflicts]})
                page = (f"## {unit['locator']}\n\n{uid} ^{uid}\n\n"
                        f"<!-- BL-SOURCE-TEXT:BEGIN {uid} -->\n{normalized}\n<!-- BL-SOURCE-TEXT:END {uid} -->")
                blocks.append(f"<!-- BL-SRC:BEGIN {uid} -->\n\n{page}\n\n<!-- BL-SRC:END {uid} -->\n")
                payloads.append({'unit_id': unit['id'], 'hash': digest(normalized), 'mode': mode,
                                 'locator': unit['locator'], 'transformed': bool(conflicts)})
            else:
                page = f"## {unit['id']} · {unit['locator']}\n\n{body}\n\n^{source['id']}-{unit['id']}"
                blocks.append(f"<!-- BL-PAGE {unit['id']} complete {mode} {digest(page)} -->\n{page}\n<!-- BL-END {unit['id']} -->\n")
        if v2:
            body_text = (f"<!-- BL-SOURCE:BEGIN {source['id']} -->\n\n# {source['name']}：资料转写\n\n"
                         f"{source['id']} ^{source['id']}\n\n来源版本：{source['sha256']}\n\n"
                         + '\n'.join(blocks[1:]) + f"\n<!-- BL-SOURCE:END {source['id']} -->\n")
        else:
            body_text = '\n'.join(blocks)
        path = inside(course, source['transcript'])
        atomic_text(path, body_text)
        source['transcript_hash'] = sha256(path)
        entry = {'source_id': source['id'], 'source_hash': source['sha256'],
                 'path': source['transcript'], 'engine': 'host-subagent'}
        if v2:
            entry.update(shell='v2', payloads=payloads, transforms=transforms)
        index['sources'].append(entry)
    write_json(course / '_工作区/转写索引.json', index)
    save(course, data, render=True)
    from obsidian_links import enabled, build_map
    if enabled(course): build_map(course)
    return {'complete': True, 'sources': len(data['sources']), 'paths': [s['transcript'] for s in data['sources']],
            'next': '转写已全部完成。请按宿主适配确认所有子 Agent 已停止（不再有成员在写），'
                    '并运行 `watchdog.py --course COURSE stop` 停止看门狗进程；'
                    '随后按章整理知识内容.md、学习路径、讲义和卡片，验收后才可 package。'}


DOC_DIR = '课程文档'
DOC_FILES = ('学习需求.md', '学习路径.md', '资料清单.md', '知识内容.md',
             '质量报告.md', '待核实问题.md', '学习反馈.md', '转写拆分计划.md')
PURGE = ('_工作区/讲义尝试', '_工作区/讲义输入', '_工作区/转写尝试', '资料转写/_分片', '_工作区/派发提示',
         '_工作区/提取内容', '_工作区/看门狗')
LINK = re.compile(r'(!?\[[^\]\n]*\]\(\s*)(?:<([^>]+)>|([^\s)]+))((?:\s+["\'][^\n]*?["\'])?\s*\))')


def move_docs(course):
    """把报告类 Markdown 归入交付文档目录；返回 {原相对路径: 新相对路径}。"""
    moves = {name: DOC_DIR + '/' + name for name in DOC_FILES if (course / name).is_file()}
    if not moves:
        return moves
    (course / DOC_DIR).mkdir(exist_ok=True)
    for old, new in moves.items():
        (course / old).replace(course / new)
    return moves


def rebase_after_move(course, moves):
    """文档位置变化后重算学习者可见 Markdown 的相对链接；不改代码块与外部链接。"""
    reverse = {new: old for old, new in moves.items()}
    for path in course.rglob('*.md'):
        if any(p in {'资料转写', '原始资料'} for p in path.relative_to(course).parts):
            continue
        relative = path.relative_to(course)
        if '_工作区' in relative.parts:
            continue
        now = relative.as_posix()
        before = reverse.get(now, now)
        old_base, new_base = posixpath.dirname(before), posixpath.dirname(now)
        text = path.read_text(encoding='utf-8-sig')

        def replace(found):
            destination = found.group(2) or found.group(3)
            parsed = urlsplit(destination)
            if parsed.scheme or parsed.netloc or not parsed.path:
                return found.group(0)
            absolute = posixpath.normpath(posixpath.join(old_base, unquote(parsed.path)))
            target = moves.get(absolute, absolute)
            rewritten = posixpath.relpath(target, new_base) if new_base else target
            if parsed.query:
                rewritten += '?' + parsed.query
            if parsed.fragment:
                rewritten += '#' + parsed.fragment
            start, end = found.span(2 if found.group(2) is not None else 3)
            return found.group(0)[:start - found.start()] + rewritten + found.group(0)[end - found.start():]

        lines, fence = [], None
        for line in text.splitlines(keepends=True):
            marker = re.match(r'^\s*(`{3,}|~{3,})', line)
            if marker:
                token = marker.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
                lines.append(line)
                continue
            if fence is None:
                # 行内代码可能示范链接写法，保持原样。
                segments, cursor = [], 0
                for code in re.finditer(r'(`+).*?\1', line):
                    segments.append(LINK.sub(replace, line[cursor:code.start()]))
                    segments.append(code.group(0))
                    cursor = code.end()
                segments.append(LINK.sub(replace, line[cursor:]))
                line = ''.join(segments)
            lines.append(line)
        updated = ''.join(lines)
        if updated != text:
            atomic_text(path, updated)


@validation_run
def package(course, dry_run=False):
    """收尾：清理过程文件并把报告类文档归入目录，形成最终交付结构。

    这是终态动作——被清理的是已合成的中间产物；结构检查结果已保存在
    _工作区/结构检查.json，打包后不再重复校验。
    """
    from assemble_knowledge import require_knowledge
    from watchdog import is_running
    require_knowledge(course)
    if is_running(course):
        raise ValueError('看门狗进程仍在运行；转写阶段结束后先运行 '
                         '`watchdog.py --course COURSE stop`，再打包')
    data = load(course)
    active = [b['active_member'] for s in data['sources'] for b in s['batches'] if b.get('active_member')]
    if active:
        raise ValueError('仍有活跃成员租约；确认这些成员已停止并运行 recover：' + ', '.join(active))
    errors = check(course)
    if errors:
        raise ValueError('全部单元完成并合成后才允许打包：' + '; '.join(errors))

    def size(path):
        return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())

    from obsidian_links import enabled
    if enabled(course):
        from validate_package import validate
        report = validate(course)
        if not report['passed']: raise ValueError('最终验收未通过：' + '; '.join(report['errors']))
        write_json(course / '_工作区/结构检查.json', report)
    before = size(course)
    removed = []
    for name in PURGE:
        target = course / name
        if target.exists():
            removed.append({'path': name, 'bytes': size(target)})
            if not dry_run:
                shutil.rmtree(target)
    pending = [name for name in DOC_FILES if (course / name).is_file()]
    if dry_run:
        return {'dry_run': True, 'would_remove': removed, 'would_move': pending,
                'before_bytes': before}
    data['packaged'] = now()
    save(course, data)
    moves = move_docs(course)
    rebase_after_move(course, moves)
    from obsidian_links import enabled, rewrite_course, build_map, validate_obsidian_links
    if enabled(course):
        rewrite_course(course, {k.removesuffix('.md'): v.removesuffix('.md') for k, v in moves.items()})
        build_map(course, moves)
        from lesson_tasks import record_link_relocation
        record_link_relocation(course, {k.removesuffix('.md'): v.removesuffix('.md') for k, v in moves.items()})
        # Deliverables were just moved and relinked: refresh the receipt the
        # packaged check compares against, then verify targets still resolve.
        from validate_package import record_snapshot
        record_snapshot(course, 'packaged')
        link_errors = validate_obsidian_links(course, phase='packaged')
        if link_errors: raise ValueError('打包后链接检查失败：' + '; '.join(link_errors))
        from validate_package import validate as validate_all
        report = validate_all(course, 'packaged')
        if not report['passed']:
            raise ValueError('打包后复验未通过：' + '; '.join(report['errors']))
        write_json(course / '_工作区/打包回执.json',
                   {'schema_version': 1, 'packaged_at': now(), 'moves': moves,
                    'report': {'passed': report['passed'], 'counts': report.get('counts')},
                    'layout': 'packaged'})
    return {'packaged': True, 'removed': removed, 'moved': sorted(moves.values()),
            'before_bytes': before, 'after_bytes': size(course),
            'layout': {'外层': ['开始学习.md', '核心知识点.md', '学习文档/', '资料转写/'],
                       '文档目录': DOC_DIR + '/',
                       '工作区': '_工作区/（仅台账与结构检查；中间产物已清理）'}}


@validation_run
def migrate_legacy(course, stopped_members):
    data = load_ledger(course)
    if data['schema_version'] not in {1, 2}:
        raise ValueError('Only schema 1/2 ledgers need legacy migration')
    active = {b['active_member'] for s in data['sources'] for b in s['batches'] if b.get('active_member')}
    if active != set(stopped_members):
        raise ValueError('必须逐一通过宿主确认旧成员停止，并提供全部活动逻辑成员名')
    backup = course / '_工作区' / ('转写任务.legacy-' + sha256(course / LEDGER)[:12] + '.json')
    if not backup.exists():
        atomic_text(backup, (course / LEDGER).read_text(encoding='utf-8'))
    for source in data['sources']:
        for batch in source['batches']:
            batch.update(active_member=None, assigned=[])
        for unit in source['units']:
            if unit['status'] == 'dispatched':
                unit['status'] = 'pending'
    save(course, data)
    return {'legacy_released': True, 'backup': str(backup), 'next': '重新 prepare；旧成功结果仍需验证，旧在途结果不能充当新尝试'}


@validation_run
def reopen_unit(course, sid, uid, reason, reset_attempts=False, strict=False):
    data = load(course)
    source, unit = find(data, sid, uid=uid)
    if not reason.strip() or unit.get('processing_route') != 'vision' or not unit['assets']:
        raise ValueError('需说明具体复核原因，且仅可重开已有视觉资产的单元')
    if any(b.get('active_member') for b in source['batches']):
        raise ValueError('先确认该来源成员结束并收集，再重开复核')
    if asset_hashes(course, unit) != unit['asset_hashes']:
        raise ValueError('ASSET_CHANGED：先重新 prepare')
    if unit.get('attempt_count', 0) >= data.get('max_attempts', 3) and not reset_attempts:
        raise ValueError('已耗尽重试次数；用户明确要求继续后才能 --reset-attempts')
    unit.setdefault('reviews', []).append({'reason': reason, 'at': now(), 'reset_attempts': reset_attempts})
    if reset_attempts:
        unit['attempt_count'] = 0
    unit.update(status='pending', blocked=False, current_attempt=None)
    if strict:
        unit.update(next_profile='strict', dispatch_profile='strict', strict_reasons=sorted(set(unit.get('strict_reasons', []) + ['EXPLICIT_STRICT'])))
    source.pop('transcript_hash', None)
    save(course, data, render=True)
    return {'source': sid, 'unit': uid, 'status': 'pending', 'next': '重新 dispatch，仅 staging 可作为新结果'}


@validation_run
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--batch-size', type=int, default=5)
    p.add_argument('--max-concurrent', type=int, default=4)
    p.add_argument('--chapter-boundaries')
    p.add_argument('--mode', choices=['strict', 'bounded'], default=None, help='兼容参数：请优先使用 --force-strict-unit')
    p.add_argument('--batch-budget', type=int, default=100000)
    p.add_argument('--max-attempts', type=int, default=3)
    p.add_argument('--strict-cost-threshold', type=int, default=12000)
    p.add_argument('--force-strict-unit', action='append', default=[], help='SRC-ID/U00001，可重复')
    p.add_argument('--office-renders', help='课程相对 JSON：source_id → Office part → 课程相对 PNG 路径')
    p = commands.add_parser('probe-start')
    p.add_argument('--host', default=DEFAULT_HOST, choices=sorted(HOSTS))
    p.add_argument('--model', required=True)
    p = commands.add_parser('probe')
    p.add_argument('--member', required=True)
    p.add_argument('--host', default=DEFAULT_HOST, choices=sorted(HOSTS))
    p.add_argument('--model', required=True)
    p = commands.add_parser('dispatch')
    p.add_argument('--source', required=True)
    p.add_argument('--batch', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--host', default=DEFAULT_HOST, choices=sorted(HOSTS))
    p = commands.add_parser('pump')
    p.add_argument('--host', default=DEFAULT_HOST, choices=sorted(HOSTS))
    p.add_argument('--model', required=True)
    p.add_argument('--max-fill', type=int)
    p.add_argument('--host-limit', type=int)
    p = commands.add_parser('mark-running')
    p.add_argument('--attempt', required=True)
    p.add_argument('--agent-id', required=True)
    p = commands.add_parser('fail-attempt')
    p.add_argument('--attempt', required=True)
    p.add_argument('--kind', required=True)
    p.add_argument('--stopped-agent-id')
    p.add_argument('--message', default='')
    for name in ('collect', 'recover'):
        p = commands.add_parser(name)
        p.add_argument('--attempt', required=True)
        p.add_argument('--stopped-agent-id', required=True)
    commands.add_parser('report')
    commands.add_parser('status')
    p = commands.add_parser('reopen')
    p.add_argument('--source', required=True)
    p.add_argument('--unit', required=True)
    p.add_argument('--reason', required=True)
    p.add_argument('--reset-attempts', action='store_true')
    p.add_argument('--strict', action='store_true')
    p = commands.add_parser('migrate-legacy')
    p.add_argument('--stopped-member', action='append', default=[], help='已由宿主确认停止的旧逻辑成员名，可重复')
    p = commands.add_parser('resolve')
    p.add_argument('--source', required=True)
    p.add_argument('--unit', required=True)
    p.add_argument('--status', required=True, choices=['non_teaching', 'duplicate'])
    p.add_argument('--reason', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--duplicate-of')
    commands.add_parser('assemble')
    commands.add_parser('check')
    p = commands.add_parser('package')
    p.add_argument('--dry-run', action='store_true', help='只报告将删除与移动的内容，不改动磁盘')
    args = parser.parse_args()
    course = args.course.resolve()
    try:
        with locked(course):
            if args.command == 'prepare':
                result = prepare(course, args.batch_size, args.max_concurrent, args.chapter_boundaries, args.mode, args.batch_budget, args.max_attempts, args.office_renders, args.strict_cost_threshold, args.force_strict_unit)
            elif args.command == 'probe-start':
                from capability_probe import start
                result = start(course, args.host, args.model)
            elif args.command == 'probe':
                result = probe(course, args.member, args.host, args.model)
            elif args.command == 'dispatch':
                result = dispatch(course, args.source, args.batch, args.host, args.model)
            elif args.command in {'collect', 'recover'}:
                result = collect(course, attempt_id=args.attempt, stopped_agent_id=args.stopped_agent_id)
            elif args.command == 'pump':
                from attempt_tasks import pump
                result = pump(course, args.host, args.model, args.max_fill, args.host_limit)
            elif args.command == 'mark-running':
                from attempt_tasks import mark_running
                result = mark_running(course, args.attempt, args.agent_id)
            elif args.command == 'fail-attempt':
                from attempt_tasks import fail_attempt
                result = fail_attempt(course, args.attempt, args.kind, args.stopped_agent_id, args.message)
            elif args.command == 'report':
                data = load_ledger(course)
                render_plan(course, data)
                result = summarize(data)
            elif args.command == 'migrate-legacy':
                result = migrate_legacy(course, args.stopped_member)
            elif args.command == 'status':
                result = summarize(load_ledger(course))
            elif args.command == 'reopen':
                result = reopen_unit(course, args.source, args.unit, args.reason, args.reset_attempts, args.strict)
            elif args.command == 'resolve':
                result = resolve(course, args.source, args.unit, args.status, args.reason, args.evidence, args.duplicate_of)
            elif args.command == 'assemble':
                result = assemble(course)
            elif args.command == 'package':
                result = package(course, dry_run=args.dry_run)
            else:
                errors = check(course)
                result = {'complete': not errors, 'errors': errors}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('errors') else 0
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
