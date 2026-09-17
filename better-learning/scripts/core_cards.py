"""Main-agent card consolidation: deterministic IDs, links, and freshness receipts."""
import argparse
import json
from pathlib import Path
import re

from _common import atomic_text, inside, read_json, read_jsonl, sha256, write_json, validation_run
from entity_sections import parse_sections
from entity_registry import GRAPH_POLICY_VERSION, Registry, document as registry_document, is_v2
from obsidian_links import block_ids, build_map, render_wikilink, validate_candidate, validate_document, validate_obsidian_links
from relation_renderer import render_entity_link, sync_course_relations

STATE = '_工作区/核心卡片状态.json'


def cards_path(course):
    return registry_document(course, '核心知识点.md')


def semantic_hash(course, path):
    from lesson_tasks import semantic
    return sha256_text(semantic(inside(course, path).read_text(encoding='utf-8')))


def sha256_text(text):
    import hashlib
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def inputs(course):
    lessons = read_json(course / '_工作区/章节索引.json')['lessons']
    exercise_index = course / '_工作区/练习索引.jsonl'
    return {'lesson_hashes': {l['id']: semantic_hash(course, l['path']) for l in lessons},
            'knowledge_index_hash': sha256(course / '_工作区/知识索引.jsonl'),
            'knowledge_hash': semantic_hash(course, registry_document(course, '知识内容.md')),
            'exercise_index_hash': sha256(exercise_index) if exercise_index.exists() else None,
            'graph_policy_version': GRAPH_POLICY_VERSION}


def require_lessons(course):
    from lesson_tasks import check
    from assemble_knowledge import require_knowledge
    require_knowledge(course)
    errors = check(course)
    if errors: raise ValueError('全部讲义完成且有效后才能生成卡片：' + '; '.join(errors))


def _exercise_rows(course):
    path = course / '_工作区/练习索引.jsonl'
    return read_jsonl(path) if path.exists() else []


@validation_run
def prepare(course):
    require_lessons(course)
    candidates = read_jsonl(course / '_工作区/核心候选.jsonl')
    index_path = course / '_工作区/知识索引.jsonl'
    knowledge = read_jsonl(index_path); by_id = {k['id']: k for k in knowledge if k.get('status') != 'merged'}
    if not candidates: raise ValueError('主代理须先逐章保存核心候选及 concept_key')
    exercises = {row['id']: row for row in _exercise_rows(course) if row.get('status') == 'used'}
    grouped, membership = {}, {}
    for row in candidates:
        key = row.get('concept_key', '').strip().casefold()
        if not key or not row.get('reason') or not row.get('knowledge_ids'): raise ValueError('Invalid core candidate')
        ids = set(row['knowledge_ids'])
        if not ids <= set(by_id): raise ValueError('Unknown candidate knowledge IDs')
        for kid in ids:
            if kid in membership and membership[kid] != key: raise ValueError('同一知识不得分配给多个概念组')
            membership[kid] = key
        entry = grouped.setdefault(key, {'concept_key': key, 'knowledge_ids': set(), 'reasons': [],
                                         'exercise_ids': set(), 'exercise_reasons': []})
        entry['knowledge_ids'].update(ids); entry['reasons'].append(row['reason'])
        for exid in row.get('exercise_ids', []):
            entry['exercise_ids'].add(exid)
            if row.get('exercise_reason'):
                entry['exercise_reasons'].append(row['exercise_reason'])
    registry_path = course / '_工作区/核心卡片ID.json'
    registry = read_json(registry_path) if registry_path.exists() else {}
    used = set(registry.values())
    mapping = build_map(course)
    entity_registry = Registry(course)
    cards_file = cards_path(course).removesuffix('.md')
    lessons = read_json(course / '_工作区/章节索引.json')['lessons']
    orders = {l['id']: l['order'] for l in lessons}
    plan = []
    for key, entry in sorted(grouped.items()):
        kids = sorted(entry['knowledge_ids'])
        order = min(orders[lid] for kid in kids for lid in by_id[kid]['lesson_ids'])
        card = registry.get(key)
        if not card:
            n = 1
            while f'KP-{order:02d}-{n:02d}-01' in used: n += 1
            card = f'KP-{order:02d}-{n:02d}-01'; registry[key] = card; used.add(card)
        exids = sorted(entry['exercise_ids'])
        for exid in exids:
            row = exercises.get(exid)
            if row is None:
                raise ValueError('RELATION_PROVENANCE_MISSING: 练习未登记或未使用：' + exid)
            owners = {lid for kid in kids for lid in by_id[kid]['lesson_ids']}
            if row.get('owner_lesson_id') not in owners:
                raise ValueError('RELATION_PROVENANCE_MISSING: 练习不属于本卡任何知识所在讲义：' + exid)
            if not set(row.get('assesses', [])) & set(kids):
                raise ValueError('RELATION_PROVENANCE_MISSING: 练习未考查本卡知识：' + exid)
        entry.update(card_id=card, knowledge_ids=kids, exercise_ids=exids, order=order)
        entry['links'] = {'knowledge': {}, 'exercises': {}}
        for kid in kids:
            location = entity_registry.canonical(kid)
            entry['links']['knowledge'][kid] = render_entity_link(
                location, entity_registry.label(kid), entity_registry.vault_prefix)
        for exid in exids:
            location = entity_registry.canonical(exid)
            entry['links']['exercises'][exid] = render_entity_link(
                location, entity_registry.label(exid), entity_registry.vault_prefix)
        plan.append(entry)
    for k in knowledge:
        k.update(core=False); k.pop('card_id', None); k.pop('card_anchor', None); k.pop('core_reason', None)
    for entry in plan:
        for kid in entry['knowledge_ids']:
            by_id[kid].update(core=True, card_id=entry['card_id'], card_anchor=entry['card_id'],
                              core_reason='; '.join(entry['reasons']))
    atomic_text(index_path, ''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in knowledge))
    write_json(registry_path, registry)
    write_json(course / '_工作区/核心卡片计划.json',
               {'schema_version': 2, 'graph_policy_version': GRAPH_POLICY_VERSION, 'cards': plan})
    # Relations are derived, not patched into the knowledge file.
    from assemble_knowledge import rebuild_knowledge
    rebuild_knowledge(course)
    write_json(course / STATE, {'status': 'prepared', **inputs(course), 'cards': [e['card_id'] for e in plan]})
    return {'cards': len(plan), 'plan': '_工作区/核心卡片计划.json',
            'next': '主代理统一写核心知识点.md（只使用计划中的 canonical K/EX 字面量）；'
                    '写完运行 core_cards.py finalize'}


def add_relations(course, knowledge=None, mapping=None):
    """Compatibility entry: relation regions are always renderer output."""
    report = sync_course_relations(course, phase='working')
    return {'rendered': report['changed'], 'edges': report['edges']}


@validation_run
def finalize(course):
    require_lessons(course)
    state = read_json(course / STATE)
    if any(state.get(k) != v for k, v in inputs(course).items()):
        raise ValueError('卡片输入已变化，请重新全局整理')
    relative = cards_path(course)
    text = inside(course, relative).read_text(encoding='utf-8')
    entity_registry = Registry(course)
    if is_v2(course):
        index = parse_sections(text, None, strict_registry=False)
        actual = sorted(section.id for section in index.sections.values() if section.kind == 'KP')
        problems = validate_candidate(course, {relative: text}, phase='staged')
        if problems: raise ValueError('; '.join(problems))
        relinked, graph = None, None
        from obsidian_links import sync_candidate
        if any(location.section_kind == 'KP' for entity in entity_registry.entities.values()
               for location in entity.locations):
            documents, graph = sync_candidate(course, {relative: text}, phase='staged')
            if documents.get(relative) != text:
                atomic_text(inside(course, relative), documents[relative])
                text = documents[relative]
        for entity in entity_registry.active_entities('KP', ('active', 'staged')):
            for edge in (graph.outgoing(entity.id) if graph else []):
                if entity_registry.type_of(edge['target_id']) not in {'K', 'EX'}:
                    raise ValueError('KP_TARGET_FORBIDDEN: ' + entity.id + ' → ' + edge['target_id'])
    else:
        actual = [b for b in block_ids(text) if b.startswith('KP-')]
        problems = validate_document(course, text) + validate_obsidian_links(course)
        if problems: raise ValueError('; '.join(problems))
    if sorted(actual) != sorted(state['cards']):
        raise ValueError('卡片 block ID 与全局分配结果不一致')
    state.update(status='complete', cards_hash=sha256(inside(course, relative)))
    write_json(course / STATE, state)
    return {'complete': True, 'cards': len(actual)}


@validation_run
def check(course):
    try:
        from lesson_tasks import check as check_lessons
        lesson_errors = check_lessons(course)
        if lesson_errors: return ['核心卡片上游讲义已失效：' + '; '.join(lesson_errors)]
        state = read_json(course / STATE)
        if state.get('status') != 'complete': return ['核心卡片尚未统一完成或已失效']
        if any(state.get(k) != v for k, v in inputs(course).items()): return ['核心卡片基于旧讲义或旧知识生成']
        relative = cards_path(course)
        if sha256(inside(course, relative)) != state.get('cards_hash'):
            return ['核心卡片正文已修改，需重新 finalize']
        if is_v2(course):
            problems = validate_candidate(course, {relative: inside(course, relative).read_text(encoding='utf-8')},
                                          phase='working')
            if problems: return ['核心卡片关系或结构不一致：' + '; '.join(problems)]
        return []
    except (OSError, KeyError, ValueError) as exc: return ['核心卡片快照缺失/不可用：' + str(exc)]


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--course', required=True, type=Path)
    p.add_argument('command', choices=['prepare', 'finalize', 'check'])
    a = p.parse_args(); course = a.course.resolve()
    from convert_materials import locked
    try:
        with locked(course):
            result = {'errors': check(course)} if a.command == 'check' else globals()[a.command](course)
        print(json.dumps(result, ensure_ascii=False)); return 1 if result.get('errors') else 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False)); return 2


if __name__ == '__main__': raise SystemExit(main())
