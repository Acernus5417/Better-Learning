"""Main-agent lesson writing: per-lesson input packages and snapshot-gated commits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from _common import Transaction, atomic_text, inside, read_json, read_jsonl, sha256, write_json, validation_run
from entity_sections import SectionError, parse_sections
from entity_registry import GRAPH_POLICY_VERSION, Registry, is_v2, teaching_section_id
from obsidian_links import (LEGACY_BLOCK_RELATIONS, block_ids, build_map, entity_section,
                            legacy_block_section, render_wikilink, sync_candidate,
                            validate_candidate, validate_document)
from relation_renderer import apply_edits
from templates import hash_of as template_hash

LEDGER = '_工作区/讲义任务.json'
INDEX = '_工作区/章节索引.json'
ANCHOR_LINE = re.compile(r'^[ \t]*[A-Za-z][A-Za-z0-9-]*[ \t]+\^[A-Za-z0-9-]+[ \t]*$\n?', re.M)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


REL_SPAN = re.compile(r'[ \t]*<!-- BL-REL:BEGIN[^>]*-->.*?<!-- BL-REL:END[^>]*-->\n?', re.S)


def semantic(text):
    """Teaching content only: derived relation regions and identity anchors excluded.

    Works on whole documents and on single knowledge fragments, so re-rendering
    relation regions never invalidates teaching content.
    """
    text = REL_SPAN.sub('', text)
    if '<!-- BL-' not in text:
        return LEGACY_BLOCK_RELATIONS.sub('', text)
    try:
        index = parse_sections(text, None, strict_registry=False)
    except SectionError:
        return LEGACY_BLOCK_RELATIONS.sub('', text)
    edits = [(section.start, section.end, '')
             for section in index.sections.values() if section.kind == 'REL']
    stripped = apply_edits(text, edits) if edits else text
    return ANCHOR_LINE.sub('', LEGACY_BLOCK_RELATIONS.sub('', stripped))


def document(course, name):
    direct = course / name
    return direct if direct.exists() else course / '课程文档' / name


def step_for_lesson(course, lesson_id):
    """Path step that schedules this lesson, from the path index (never guessed)."""
    path_file = course / '_工作区/路径索引.json'
    data = read_json(path_file) if path_file.exists() else {}
    for step in data.get('steps', []):
        if lesson_id in step.get('lessons', []):
            return step['id']
    index = read_json(course / INDEX)
    for item in index['lessons']:
        if item['id'] == lesson_id and isinstance(item.get('order'), int):
            return f"{data.get('path_id', 'P-MAIN')}-S{item['order']:03d}"
    return None


def validate_frontmatter(text, lesson):
    match = re.match(r'\A---\n(.*?)\n---(?:\n|$)', text, re.S)
    if not match: raise ValueError('Missing lesson frontmatter')
    fields = {}
    for key in ('type', 'id', 'title', 'order', 'status'):
        values = re.findall(r'^' + key + r':[ \t]*(.*?)[ \t]*$', match[1], re.M)
        if len(values) != 1: raise ValueError('Missing/duplicate frontmatter field: ' + key)
        value = values[0]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '\"\'': value = value[1:-1]
        fields[key] = value
    if (fields['type'] != 'lesson' or fields['id'] != lesson['id'] or not fields['title'].strip()
            or fields['order'] != str(lesson['order']) or fields['status'] != 'complete'):
        raise ValueError('Invalid lesson frontmatter identity/type/status/order')


def snapshot(course, lesson):
    index = read_json(course / INDEX)
    all_knowledge = read_jsonl(course / '_工作区/知识索引.jsonl')
    knowledge = [k for k in all_knowledge
                 if lesson['id'] in k.get('lesson_ids', []) and k.get('status') != 'merged']
    if not knowledge: raise ValueError('Lesson has no assigned knowledge: ' + lesson['id'])
    if any(k.get('status') != 'verified' for k in knowledge): raise ValueError('Assigned knowledge must be verified')
    chapters = {c['id']: c for c in index['chapters']}
    assigned = {k['id'] for k in knowledge}
    required = {dep for k in knowledge for dep in k.get('prerequisites', [])} - assigned
    prerequisites = [k for k in all_knowledge if k['id'] in required]
    if {k['id'] for k in prerequisites if k.get('status') == 'verified'} != required:
        raise ValueError('Missing/unverified prerequisite knowledge')
    registry = Registry(course)
    legacy = not is_v2(course)
    canonical_text = document(course, '知识内容.md').read_text(encoding='utf-8')
    slices, prerequisite_slices = {}, {}
    for k in knowledge + prerequisites:
        if legacy:
            section = legacy_block_section(canonical_text, k['id'])
        else:
            try:
                section = entity_section(canonical_text, 'K', k['id'], registry)
            except SectionError as exc:
                raise ValueError(f'SECTION_BOUNDARY_INVALID: 知识 {k["id"]} 缺少显式边界：{exc.message}') from exc
        if not section.strip(): raise ValueError('Knowledge needs stable block: ' + k['id'])
        (slices if k['id'] in assigned else prerequisite_slices)[k['id']] = semantic(section)
    records = [{key: value for key, value in k.items() if key not in
                {'core', 'core_reason', 'card_id', 'card_anchor', 'concept_key'}} for k in knowledge]
    path_text = document(course, '学习路径.md').read_text(encoding='utf-8')
    if not path_text.strip(): raise ValueError('学习路径必须完成并落盘')
    excerpt = lesson.get('path_excerpt')
    if not excerpt and not legacy:
        step_id = step_for_lesson(course, lesson['id'])
        if step_id:
            try:
                excerpt = entity_section(path_text, 'STEP', step_id, registry)
            except SectionError:
                excerpt = None
    if not excerpt:
        match = re.search(r'^#{1,6}[^\n]*\b' + re.escape(lesson['id']) + r'\b[^\n]*\n.*?(?=^#{1,6}\s|\Z)', path_text, re.M | re.S)
        excerpt = match.group(0) if match else None
    if not excerpt: raise ValueError('章节索引须含 path_excerpt，或学习路径中有带 lesson ID 的 BL-STEP 段落')
    requirements = document(course, '学习需求.md').read_text(encoding='utf-8')
    if not requirements.strip(): raise ValueError('学习需求不能为空')
    root = Path(__file__).resolve().parents[1]
    spec = {'knowledge_hash': digest(semantic(document(course, '知识内容.md').read_text(encoding='utf-8'))),
            'fragments': {k: digest(v) for k, v in slices.items()}, 'records': digest(records),
            'prerequisites': {k: digest(v) for k, v in prerequisite_slices.items()},
            # Teaching content only: relation regions are a derived view, so their
            # re-render (cards, Obsidian rebuild) must not invalidate a chapter.
            'path_hash': digest(semantic(path_text)),
            'requirements_hash': sha256(document(course, '学习需求.md')),
            'attachments': {p: sha256(inside(course, p)) for p in lesson.get('attachments', [])},
            'lesson': digest({k: lesson[k] for k in ('id', 'path', 'title', 'order', 'path_excerpt', 'exercise_count') if k in lesson}),
            'policy': sha256(root / 'references/lesson-writing-policy.md'),
            'parts_protocol': sha256(root / 'scripts/lesson_parts.py'),
            'writing_spec': sha256(root / 'references/chapter-writing.md'),
            'template': template_hash('lesson'),
            'card_template': template_hash('card'),
            'graph_policy_version': GRAPH_POLICY_VERSION,
            # Identity of the entities a chapter may link, not the whole registry:
            # registering cards or exercises later must not stale teaching content.
            'identity': digest({'knowledge': sorted(k['id'] for k in knowledge),
                                'lessons': sorted([l['id'], l.get('path', '')]
                                                  for l in index.get('lessons', []))}),
            'link_schema_version': 1 if legacy else 2}
    return {'input_hash': digest(spec), 'hashes': spec, 'slices': slices, 'knowledge': knowledge,
            'prerequisite_slices': prerequisite_slices, 'prerequisites': prerequisites,
            'path_excerpt': excerpt, 'requirements': requirements}


def lesson_links(course, lesson, snap, mapping, registry_obj):
    """Minimal capability list: every literal comes from a reviewed relation."""
    from relation_renderer import render_entity_link
    legacy = not is_v2(course)
    lid = lesson['id']
    links, allowed = {}, []
    links['knowledge'] = {}
    for kid in lesson['knowledge_ids']:
        location = registry_obj.canonical(kid) if registry_obj.has(kid) else None
        if legacy or location is None:
            links['knowledge'][kid] = render_wikilink(mapping['knowledge'], block=kid)
            allowed.append(mapping['knowledge'] + '#^' + kid)
        else:
            links['knowledge'][kid] = render_entity_link(location, registry_obj.label(kid),
                                                         registry_obj.vault_prefix)
            allowed.append(location.path.removesuffix('.md') + '#^' + kid)
    links['self_blocks'] = {kid: '^' + kid for kid in lesson['knowledge_ids']}
    step = step_for_lesson(course, lid)
    if step and registry_obj.has(step):
        location = registry_obj.canonical(step)
        links['learning_path'] = render_entity_link(location, '本章学习安排', registry_obj.vault_prefix)
        allowed.append(location.path.removesuffix('.md') + '#^' + step)
    else:
        links['learning_path'] = render_wikilink(mapping['learning_path'])
        allowed.append(mapping['learning_path'])
    # The rendered relation region also links the path itself (L → NAV.path).
    path_id = step.rsplit('-S', 1)[0] if step else 'P-MAIN'
    if registry_obj.has(path_id):
        location = registry_obj.canonical(path_id)
        allowed.append(location.path.removesuffix('.md') + '#^' + path_id)
    else:
        allowed.append(mapping['learning_path'] + '#^' + path_id)
    # Reserve identifiers, not a quota: workers only use IDs needed by the chapter.
    exercises = [f'EX-{lid.replace("-", "")}-{i:03d}'
                 for i in range(1, lesson.get('exercise_count', max(24, 6 * len(lesson['knowledge_ids']) + 12)) + 1)]
    lesson_path = lesson['path'].removesuffix('.md')
    allowed += [lesson_path + '#^' + ex for ex in exercises]
    if legacy:
        links['exercises'] = {ex: render_wikilink(lesson_path, block=ex) for ex in exercises}
    links['prerequisites'] = {}
    for k in snap['prerequisites']:
        location = registry_obj.canonical(k['id']) if registry_obj.has(k['id']) else None
        if legacy or location is None:
            links['prerequisites'][k['id']] = render_wikilink(mapping['knowledge'], block=k['id'])
            allowed.append(mapping['knowledge'] + '#^' + k['id'])
        else:
            links['prerequisites'][k['id']] = render_entity_link(location, registry_obj.label(k['id']),
                                                                 registry_obj.vault_prefix)
            allowed.append(location.path.removesuffix('.md') + '#^' + k['id'])
    # Sources are only authorized for exercises with a registered题源; lessons do not link sources.
    links['exercise_sources'] = {}
    exercise_index = {}
    index_path = course / '_工作区/练习索引.jsonl'
    if index_path.exists():
        exercise_index = {row['id']: row for row in read_jsonl(index_path)}
    for ex in exercises:
        for ref in (exercise_index.get(ex) or {}).get('source_refs', []):
            unit = f"{ref['source_id']}-{ref['unit_id']}"
            if not registry_obj.has(unit):
                continue
            location = registry_obj.canonical(unit)
            links['exercise_sources'].setdefault(ex, {})[unit] = render_entity_link(
                location, registry_obj.label(unit), registry_obj.vault_prefix)
            allowed.append(location.path.removesuffix('.md') + '#^' + unit)
    # Attachments must be explicitly assigned by the orchestrator in the lesson index.
    links['attachments'] = [render_wikilink(a, embed=True) for a in lesson.get('attachments', [])]
    allowed += lesson.get('attachments', [])
    return links, list(dict.fromkeys(allowed)), exercises


def insert_identity_anchor(body, entity_id):
    """Insert 'ID ^ID' after the first heading; the assembler owns this."""
    lines = body.splitlines()
    for position, line in enumerate(lines):
        if re.match(r'^#{1,6}\s+\S', line):
            return '\n'.join(lines[:position + 1] + ['', f'{entity_id} ^{entity_id}'] + lines[position + 1:])
    return f'{entity_id} ^{entity_id}\n\n' + body


def wrap_section(kind, section_id, body):
    return (f'<!-- BL-{kind}:BEGIN {section_id} -->\n\n{body.strip()}\n\n'
            f'<!-- BL-{kind}:END {section_id} -->')


def exercise_span(text, heading):
    """Span of one exercise: its heading to the next heading of the same or higher level."""
    match = re.search(r'^#{1,6}[ \t]+' + re.escape(heading.strip()) + r'[ \t]*$', text, re.M)
    if not match:
        return None
    level = len(re.match(r'^#+', match.group(0)).group(0))
    end = len(text)
    for other in re.finditer(r'^#{1,6}[ \t]+\S', text[match.end():], re.M):
        if len(re.match(r'^#+', other.group(0)).group(0)) <= level:
            end = match.end() + other.start()
            break
    return match.start(), end


def assemble_lesson_document(course, attempt, lesson, raw_text):
    """Insert frontmatter, BL-L/TEACH/EX boundaries and identity anchors."""
    v2 = is_v2(course)
    if not v2:
        return raw_text
    lid = lesson['id']
    registry_obj = Registry(course)
    frontmatter = ''
    body = raw_text
    match = re.match(r'\A---\n.*?\n---\n', raw_text, re.S)
    if match:
        frontmatter, body = match.group(0), raw_text[match.end():]
    progress = attempt.get('part_progress', {})
    sections = attempt.get('sections', [])
    intro_text, knowledge_text, closing_text = [], {}, []
    for section in sections:
        saved = progress.get(section['id'])
        if not saved:
            continue
        texts = []
        for entry in saved['files']:
            texts.append(inside(course, entry['path']).read_text(encoding='utf-8'))
        merged = '\n\n'.join(texts)
        if section['kind'] == 'intro':
            intro_text.append(merged)
        elif section['kind'] == 'knowledge' and section.get('knowledge_ids'):
            knowledge_text[section['knowledge_ids'][0]] = merged
        else:
            closing_text.append(merged)
    closing = '\n\n'.join(closing_text)
    receipt = attempt.get('worker_result') or {}
    for entry in receipt.get('exercises', []):
        ex_id, heading = entry.get('id'), entry.get('heading', '')
        if not ex_id or ex_id not in attempt.get('allowed_blocks', []):
            raise ValueError('INVALID_OUTPUT: 回执中的练习不在预留集合内：' + str(ex_id))
        span = exercise_span(closing, heading)
        if not span:
            raise ValueError('INVALID_OUTPUT: 找不到练习标题：' + heading)
        start, end = span
        chunk = closing[start:end]
        if re.search(r'^#{1,6}[ \t]+\S', chunk[len(chunk.splitlines()[0]):], re.M) and \
                any(other.get('heading', '') in chunk[len(chunk.splitlines()[0]):]
                    for other in receipt.get('exercises', []) if other is not entry):
            raise ValueError('INVALID_OUTPUT: 练习区间与其他练习重叠：' + heading)
        wrapped = wrap_section('EX', ex_id, insert_identity_anchor(chunk, ex_id))
        closing = closing[:start] + wrapped + '\n\n' + closing[end:]
    intro_merged = '\n\n'.join(item for item in intro_text if item.strip())
    if frontmatter:
        intro_merged = re.sub(r'\A---\n.*?\n---\n?', '', intro_merged, flags=re.S).lstrip('\n')
    sections_out = [frontmatter.rstrip('\n')] if frontmatter else []
    sections_out.append(f'<!-- BL-L:BEGIN {lid} -->')
    sections_out.append(insert_identity_anchor(intro_merged, lid) if intro_merged.strip() else f'{lid} ^{lid}')
    for kid in lesson['knowledge_ids']:
        text = knowledge_text.get(kid)
        if not text:
            raise ValueError('INVALID_OUTPUT: 缺少知识小节片段：' + kid)
        section_id = teaching_section_id(lid, kid)
        sections_out.append(wrap_section('TEACH', section_id, insert_identity_anchor(text, kid)))
    if closing.strip():
        sections_out.append(closing.strip())
    sections_out.append(f'<!-- BL-L:END {lid} -->')
    return '\n\n'.join(sections_out).strip() + '\n'


def invalidate(course, reason):
    path = course / LEDGER
    if path.exists():
        data = read_json(path)
        for lesson in data['lessons']:
            lesson.update(status='stale', stale_reason=reason)
        write_json(path, data)
        index = read_json(course / INDEX)
        for lesson in index.get('lessons', []): lesson['status'] = 'stale'
        write_json(course / INDEX, index)
    cards = course / '_工作区/核心卡片状态.json'
    if cards.exists():
        data = read_json(cards); data.update(status='stale', reason=reason); write_json(cards, data)


def commits_of(data):
    """Commit records; older ledgers stored them under 'attempts'."""
    return data.get('commits', data.get('attempts', []))


def active_records(data):
    return [c for c in commits_of(data) if c.get('state') in {'drafting', 'running', 'produced'}]


def build_lesson_input(course, definition, row, snap, mapping, root, previous=None):
    """Write the per-lesson input package the main agent reads before writing."""
    folder = row['folder']
    lid = row['id']
    inputs = {'requirements': f'{folder}/学习需求.md', 'path': f'{folder}/路径片段.md',
              'knowledge': f'{folder}/知识片段.md'}
    atomic_text(inside(course, inputs['requirements']), snap['requirements'])
    atomic_text(inside(course, inputs['path']), snap['path_excerpt'])
    atomic_text(inside(course, inputs['knowledge']), '\n\n'.join(snap['slices'].values()))
    if snap['prerequisite_slices']:
        inputs['prerequisites'] = f'{folder}/前置知识片段.md'
        atomic_text(inside(course, inputs['prerequisites']), '\n\n'.join(snap['prerequisite_slices'].values()))
    registry_obj = Registry(course)
    links, allowed, exercises = lesson_links(course, row, snap, mapping, registry_obj)
    from lesson_parts import provision
    sections, protected, resume = provision(course, folder, row, previous)
    if resume:
        inputs['continuity'] = f'{folder}/续写摘要.json'
        write_json(inside(course, inputs['continuity']), resume)
    v2 = is_v2(course)
    sections_meta = [dict(s, directory=str(inside(course, s['directory'])),
                          receipt=str(inside(course, s['receipt'])),
                          anchor_literal=(f"{s['knowledge_ids'][0]} ^{s['knowledge_ids'][0]}"
                                          if s.get('knowledge_ids') else None),
                          system_slots=['identity', 'relations'] if s.get('knowledge_ids') else [])
                     for s in sections]
    from templates import path as template_path
    manifest = {'schema_version': 1 if not v2 else 2, 'kind': 'lesson', 'author': 'main-agent',
                'lesson_id': lid, 'title': row['title'], 'order': row['order'],
                'knowledge_ids': row['knowledge_ids'],
                'graph_policy_version': GRAPH_POLICY_VERSION,
                'registry_revision': registry_obj.structure_revision(),
                'input_content_hash': snap['input_hash'],
                'inputs': {k: str(inside(course, p)) for k, p in inputs.items()},
                'policy': str(root / 'references/lesson-writing-policy.md'),
                'writing_spec': str(root / 'references/chapter-writing.md'),
                'template': str(template_path('lesson')),
                'result': str(inside(course, f'{folder}/{lid}.result.json')),
                'write_mode': 'parts-v1', 'sections': sections_meta,
                'links': links, 'allowed_blocks': row['knowledge_ids'] + exercises,
                'reserved_exercises': [{'id': ex, 'owner_lesson_id': lid} for ex in exercises],
                'agent_can_write_system_regions': False}
    manifest['link_manifest_hash'] = digest({'sections': sections_meta, 'links': links})
    task = folder + '/task.json'
    write_json(inside(course, task), manifest)
    row.update(manifest=task, task_hash=sha256(inside(course, task)), sections=sections,
               protected_chunks=protected, allowed_links=allowed,
               allowed_blocks=manifest['allowed_blocks'],
               link_manifest_hash=manifest['link_manifest_hash'],
               result=f'{folder}/{lid}.result.json', output=f'{folder}/{lid}.md',
               agent_may_write_system_regions=manifest['agent_can_write_system_regions'])
    return row


@validation_run
def prepare(course, max_attempts=3):
    """One input package per lesson; the main agent writes the chapter itself."""
    from assemble_knowledge import require_knowledge
    require_knowledge(course)
    if max_attempts < 1: raise ValueError('Invalid pool policy')
    path = course / LEDGER
    old = read_json(path) if path.exists() else {}
    if active_records(old): raise ValueError('先确认上一章的写入已提交或已放弃，再重建输入包')
    index = read_json(course / INDEX)
    if not index.get('lessons'): raise ValueError('学习路径与 lesson 索引尚未完成')
    from obsidian_links import initialize
    initialize(course); mapping = build_map(course)
    root = Path(__file__).resolve().parents[1]
    commits = commits_of(old)
    lessons = []
    for item in sorted(index['lessons'], key=lambda l: l['order']):
        if not item['path'].startswith('学习文档/') or not item['path'].endswith('.md'): raise ValueError('Invalid lesson path')
        snap = snapshot(course, item)
        previous = next((l for l in old.get('lessons', []) if l['id'] == item['id']), {})
        keep = (previous.get('status') == 'done' and previous.get('input_hash') == snap['input_hash']
                and inside(course, item['path']).exists() and sha256(inside(course, item['path'])) == previous.get('output_hash'))
        row = dict(item, status='done' if keep else 'pending', input_hash=snap['input_hash'],
                   attempt_count=previous.get('attempt_count', 0) if previous.get('input_hash') == snap['input_hash'] else 0,
                   knowledge_ids=[k['id'] for k in snap['knowledge']],
                   folder=f"_工作区/讲义输入/{item['id']}", manifest=f"_工作区/讲义输入/{item['id']}/task.json")
        if keep:
            for k in ('output_hash', 'committed_attempt', 'allowed_links', 'allowed_blocks'): row[k] = previous[k]
        item['status'] = 'complete' if keep else 'pending'
        if not keep:
            resume = None
            if previous.get('input_hash') == snap['input_hash'] and previous.get('resume_record'):
                resume = next((c for c in commits if c['id'] == previous['resume_record']), None)
                if resume: row['resume_record'] = resume['id']
            build_lesson_input(course, item, row, snap, mapping, root, resume)
        lessons.append(row)
    data = {'schema_version': 3, 'engine': 'main-agent', 'max_attempts': max_attempts,
            'lessons': lessons, 'commits': commits}
    write_json(course / INDEX, index); write_json(path, data)
    return {'lessons': len(lessons), 'pending': sum(l['status'] == 'pending' for l in lessons),
            'next': '主代理按 lesson order 逐章读取 manifest，按章节规范与模板写片段与 result.json；'
                    '写完运行 commit --lesson <L-ID>'}


def find(data, cid):
    record = next(c for c in commits_of(data) if c['id'] == cid)
    lesson = next(l for l in data['lessons'] if l['id'] == record['lesson_id'])
    return record, lesson


def register_exercises(course, lesson, result):
    """Exercises actually written into the chapter become 'used' relation targets."""
    path = course / '_工作区/练习索引.jsonl'
    rows = read_jsonl(path) if path.exists() else []
    index = {row['id']: row for row in rows}
    allowed = set(lesson.get('allowed_blocks', []))
    changed = False
    for entry in result.get('exercises', []) or []:
        exid = entry.get('id')
        if not exid or exid not in allowed: continue
        row = index.get(exid, {'id': exid, 'owner_lesson_id': lesson['id'], 'status': 'used',
                               'assesses': [], 'source_refs': [], 'title': entry.get('heading', '')})
        row.update(owner_lesson_id=lesson['id'], status='used',
                   title=entry.get('heading') or row.get('title', ''))
        if entry.get('assesses'):
            row['assesses'] = sorted(set(row.get('assesses', [])) | set(entry['assesses']))
        if entry.get('source_refs'):
            row['source_refs'] = entry['source_refs']
        index[exid] = row; changed = True
    if not changed: return 0
    atomic_text(path, ''.join(json.dumps(index[key], ensure_ascii=False) + '\n' for key in sorted(index)))
    return len(result.get('exercises', []) or [])


@validation_run
def commit(course, lesson_id):
    """Verify the main agent's chapter draft, assemble identity/links, then commit it."""
    from convert_materials import now
    data = read_json(course / LEDGER)
    lesson = next(l for l in data['lessons'] if l['id'] == lesson_id)
    if lesson['status'] == 'done':
        return {'lesson': lesson_id, 'state': 'done', 'idempotent': True}
    index = read_json(course / INDEX)
    mapping = build_map(course)
    output_error = ''
    cid = 'C-' + uuid.uuid4().hex
    a = {'id': cid, 'kind': 'main', 'lesson_id': lesson_id, 'state': 'drafting',
         'input_hash': lesson['input_hash'], 'input_hashes': lesson.get('input_hashes', {}),
         'task_manifest': lesson['manifest'], 'task_hash': lesson.get('task_hash'),
         'output': lesson.get('output'), 'result': lesson.get('result'),
         'allowed_links': lesson.get('allowed_links', []), 'allowed_blocks': lesson.get('allowed_blocks', []),
         'write_mode': 'parts-v1', 'sections': lesson.get('sections', []),
         'protected_chunks': lesson.get('protected_chunks', {}),
         'knowledge_target': mapping['knowledge'],
         'agent_may_write_system_regions': lesson.get('agent_may_write_system_regions', True),
         'link_manifest_hash': lesson.get('link_manifest_hash'), 'started_at': now()}
    definition = next(l for l in index['lessons'] if l['id'] == lesson['id'])
    kind = 'STALE_INPUT'
    try:
        from assemble_knowledge import require_knowledge
        require_knowledge(course)
        if snapshot(course, definition)['input_hash'] != a['input_hash']:
            kind = 'STALE_INPUT'; raise ValueError('Knowledge/path/requirements changed')
        if (sha256(inside(course, a['task_manifest'])) != a['task_hash'] or any(
                sha256(inside(course, p)) != h for p, h in a['input_hashes'].items())):
            kind = 'STALE_INPUT'; raise ValueError('Assigned inputs changed')
        from lesson_parts import collect_parts
        kind = 'INVALID_OUTPUT'
        if not collect_parts(course, a, lesson):
            kind = 'PARTIAL_OUTPUT'
            raise ValueError('章节草稿未写完；已保存片段保留，只补缺失片段')
        staged = inside(course, a['output'])
        if not staged.exists() or not inside(course, a['result']).exists():
            kind = 'WRITE_FAILURE'; raise ValueError('缺少本章合并输出或 result 回执')
        kind = 'INVALID_OUTPUT'
        result = read_json(inside(course, a['result']))
        if result.get('status') != 'complete' or result.get('lesson_id') != lesson['id'] or sorted(result.get('knowledge_ids', [])) != sorted(lesson['knowledge_ids']):
            raise ValueError('Incomplete/foreign lesson result')
        raw = staged.read_text(encoding='utf-8')
        if not raw.strip(): raise ValueError('Empty output')
        documents = {}
        if is_v2(course):
            if '<!-- BL-' in raw:
                raise ValueError('RESERVED_SYSTEM_MARKUP: 主代理草稿不得包含系统边界或关系区')
            assembled = assemble_lesson_document(course, a, lesson, raw)
            documents, _graph = sync_candidate(course, {lesson['path']: assembled}, phase='staged')
            # Mid-course commit: later lessons, steps and entries may not exist yet.
            problems = validate_candidate(course, documents, phase='staged', require_targets=False)
            if problems:
                kind = 'INVALID_OUTPUT'
                raise ValueError('; '.join(problems))
        else:
            validate_frontmatter(raw, lesson)
            if not set(lesson['knowledge_ids']) <= set(block_ids(raw)):
                raise ValueError('Missing assigned knowledge blocks')
            issues = validate_document(course, raw, a['allowed_links'], a['allowed_blocks'], require_targets=False)
            if issues: kind = 'INVALID_LINK_TARGET'; raise ValueError('; '.join(issues))
            from obsidian_links import _has, link_map
            mapping = link_map(course)
            if any(not _has(legacy_block_section(raw, k), mapping['knowledge'], k) for k in lesson['knowledge_ids']):
                kind = 'INVALID_LINK_TARGET'; raise ValueError('Missing knowledge backlink in its assigned block')
            documents = {lesson['path']: raw}
        tx = Transaction(course, course / '_工作区/事务记录.jsonl')
        # Only this chapter is committed; other documents keep their relation
        # regions until the Obsidian rebuild step, so the knowledge base is never
        # rewritten mid-course (which would stale every remaining chapter).
        tx.write(inside(course, lesson['path']), documents[lesson['path']])
        tx.commit()
        raw_hash = sha256(staged)
        if is_v2(course):
            atomic_text(inside(course, a['output'].removesuffix('.md') + '.assembled.md'),
                        documents[lesson['path']])
        a.update(state='committed', commit_hash=sha256(inside(course, lesson['path'])), raw_hash=raw_hash)
        lesson.update(status='done', output_hash=sha256(inside(course, lesson['path'])), committed_attempt=cid,
                      allowed_links=a['allowed_links'], allowed_blocks=a['allowed_blocks'])
        lesson.pop('resume_record', None)
        definition['status'] = 'complete'; kind = None
        register_exercises(course, lesson, result)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        if kind == 'INVALID_OUTPUT' and 'INVALID_LINK_TARGET' in str(exc): kind = 'INVALID_LINK_TARGET'
        a.update(state='collected', failure_kind=kind, error=str(exc))
        output_error = str(exc)
        lesson['status'] = ('stale' if kind == 'STALE_INPUT'
                            else 'needs_review' if lesson.get('attempt_count', 0) + 1 >= data.get('max_attempts', 3)
                            else 'failed')
        definition['status'] = lesson['status']
        if kind == 'STALE_INPUT': lesson.pop('resume_record', None)
        elif a.get('part_progress'): lesson['resume_record'] = cid
    lesson['attempt_count'] = lesson.get('attempt_count', 0) + 1
    a['finished_at'] = now()
    data.setdefault('commits', []).append(a)
    write_json(course / LEDGER, data); write_json(course / INDEX, index)
    progress_path = course / '_工作区/生成进度.json'
    progress = read_json(progress_path)
    progress['completed_lessons'] = [l['id'] for l in data['lessons'] if l['status'] == 'done']
    write_json(progress_path, progress)
    output = {'commit': cid, 'lesson': lesson['id'], 'state': a['state'], 'failure_kind': kind,
              'saved_sections': len(a.get('part_progress', {})), 'missing_sections': a.get('missing_sections', [])}
    if kind is not None: output['error'] = output_error
    if kind and a.get('worker_result'):
        worker = a['worker_result']
        output['draft_failure'] = {'status': worker.get('status'), 'error_kind': worker.get('error_kind'),
                                   'reason': str(worker.get('reason', ''))[:500]}
    if kind is None:
        output['next'] = '本章已提交；继续下一章，全部 commit 后运行 check'
    else:
        output['next'] = '按 failure_kind 修正草稿片段后重新 commit；已保存片段不会被清空'
    return output


@validation_run
def check(course):
    if not (course / LEDGER).exists(): return ['Missing lesson task ledger']
    data = read_json(course / LEDGER); errors = []
    if active_records(data): errors.append('有未提交的章节草稿正在处理')
    index = read_json(course / INDEX)
    definitions = {l['id']: l for l in index['lessons']}
    if set(definitions) != {l['id'] for l in data['lessons']}: errors.append('Lesson scope changed')
    for lesson in data['lessons']:
        try:
            if lesson['status'] != 'done' or definitions[lesson['id']]['status'] != 'complete': raise ValueError('Lesson not complete')
            if snapshot(course, definitions[lesson['id']])['input_hash'] != lesson['input_hash']: raise ValueError('Lesson input stale')
            path = inside(course, lesson['path'])
            if sha256(path) != lesson['output_hash']: raise ValueError('Lesson output changed')
            a, _ = find(data, lesson['committed_attempt'])
            if a['state'] != 'committed' or a.get('commit_hash') != lesson['output_hash']:
                raise ValueError('Lesson commit evidence missing')
            # The lesson's own identity block is defined by the assembler, not by the writer.
            allowed_blocks = list(lesson['allowed_blocks']) + [lesson['id']]
            errors.extend(validate_document(course, path.read_text(encoding='utf-8'),
                                            lesson['allowed_links'], allowed_blocks))
        except (OSError, ValueError, KeyError, StopIteration) as exc: errors.append(lesson['id'] + ': ' + str(exc))
    return errors


def record_link_relocation(course, target_moves):
    """Record orchestrator-only path/link edits after verifying old output hashes."""
    path = course / LEDGER
    if not path.exists(): return
    data = read_json(path)
    if active_records(data): raise ValueError('有未提交的章节草稿，不能搬迁链接')
    definitions = {l['id']: l for l in read_json(course / INDEX)['lessons']}
    def rewrite_key(key):
        target, sep, anchor = key.partition('#')
        return target_moves.get(target, target) + (sep + anchor if sep else '')
    for lesson in data['lessons']:
        if lesson['status'] != 'done': continue
        definition = definitions[lesson['id']]
        lesson['path'] = definition['path']
        lesson['input_hash'] = snapshot(course, definition)['input_hash']
        old_hash = lesson['output_hash']
        lesson['output_hash'] = sha256(inside(course, lesson['path']))
        lesson['allowed_links'] = [rewrite_key(k) for k in lesson['allowed_links']]
        attempt, _ = find(data, lesson['committed_attempt'])
        attempt.setdefault('link_relocations', []).append({'moves': target_moves, 'before': old_hash, 'after': lesson['output_hash']})
        attempt['commit_hash'] = lesson['output_hash']
        attempt['input_hash'] = lesson['input_hash']
    write_json(path, data)
    cards = course / '_工作区/核心卡片状态.json'
    if cards.exists():
        state = read_json(cards)
        if state.get('status') == 'complete':
            from core_cards import inputs
            state.update(inputs(course), cards_hash=sha256(course / '核心知识点.md'))
            write_json(cards, state)


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--course', required=True, type=Path)
    sub = p.add_subparsers(dest='command', required=True)
    q = sub.add_parser('prepare'); q.add_argument('--max-attempts', type=int, default=3)
    q = sub.add_parser('commit'); q.add_argument('--lesson', required=True)
    sub.add_parser('check'); sub.add_parser('status')
    a = p.parse_args(); course = a.course.resolve()
    from convert_materials import locked
    try:
        with locked(course):
            if a.command == 'prepare': result = prepare(course, a.max_attempts)
            elif a.command == 'commit': result = commit(course, a.lesson)
            elif a.command == 'check': result = {'errors': check(course)}
            else:
                data = read_json(course / LEDGER)
                result = {'engine': data.get('engine', 'main-agent'),
                          'lessons': [{'id': l['id'], 'status': l['status'], 'attempt_count': l['attempt_count'],
                                       'manifest': l.get('manifest'), 'folder': l.get('folder'),
                                       'resume_record': l.get('resume_record')} for l in data['lessons']],
                          'pending': [l['id'] for l in data['lessons'] if l['status'] != 'done'],
                          'active': [{'commit': x['id'], 'lesson': x['lesson_id'], 'state': x['state']}
                                     for x in active_records(data)]}
        print(json.dumps(result, ensure_ascii=False)); return 1 if result.get('errors') else 0
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False)); return 2


if __name__ == '__main__': raise SystemExit(main())
