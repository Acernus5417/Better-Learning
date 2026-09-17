"""v1 -> v2 link migration: plan, apply, rollback.

The migration is conservative by design: it establishes explicit boundaries only
where the old layout proves the range, records everything it cannot resolve, and
never deletes teaching text. Derived data (locations, relations, callouts) is
rebuilt afterwards, not patched in place.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

from _common import atomic_text, inside, read_json, read_jsonl, rel, sha256, write_json
from entity_registry import GRAPH_POLICY_VERSION, LINK_SCHEMA_VERSION, Registry
from entity_sections import MARKER, WRAPPABLE_KINDS

JOURNAL = '_工作区/链接迁移记录.jsonl'
BACKUP_DIR = '_工作区/迁移备份'
PLAN_PREFIX = '_工作区/迁移计划-'

HEADING_RE = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$', re.M)
TAIL_BLOCK = re.compile(r'^[ \t]*\^([A-Za-z0-9-]+)[ \t]*$', re.M)
LEGACY_REL = re.compile(r'<!-- BL-RELATIONS -->.*?<!-- BL-END-RELATIONS -->\s*', re.S)


def now():
    return datetime.now(timezone.utc).isoformat()


def _payload_paths():
    return {'资料转写', '原始资料'}


def migration_documents(course):
    result = {}
    for path in sorted(course.rglob('*.md')):
        parts = path.relative_to(course).parts
        if any(part in _payload_paths() for part in parts):
            continue
        if '_工作区' in parts and '章节知识' not in parts:
            continue
        result[path.relative_to(course).as_posix()] = path.read_text(encoding='utf-8-sig')
    return result


def _heading_span(text, block):
    """Legacy range: nearest preceding heading through the tail block definition."""
    match = None
    for candidate in TAIL_BLOCK.finditer(text):
        if candidate[1] == block:
            match = candidate
    if not match:
        return None
    start = 0
    for heading in HEADING_RE.finditer(text[:match.start()]):
        start = heading.start()
    return start, match.end()


def _insert_after_first_heading(text, start, end, block):
    segment = text[start:end]
    body_start = 0
    blank_after = None
    for heading in HEADING_RE.finditer(segment):
        body_start = heading.end()
        blank_after = heading.start() + len(heading[0])
        break
    if blank_after is None:
        return None
    anchor = f'\n{block} ^{block}\n'
    return text[:start] + segment[:body_start] + anchor + segment[body_start:]


def _strip_tail_block(text, block):
    return TAIL_BLOCK.sub(lambda m: '' if m[1] == block else m[0], text, count=0)


DERIVED_DOCUMENTS = {'知识内容.md', '课程文档/知识内容.md'}


def _wrap_legacy_sections(course, plan, registry):
    """Add explicit boundaries where a legacy tail anchor proves the entity range."""
    documents = migration_documents(course)
    operations, unresolved = [], []
    for path, text in documents.items():
        if path in DERIVED_DOCUMENTS:
            continue  # rebuilt from the fragments after the migration
        if MARKER.search(text):
            legacy_rel = LEGACY_REL.search(text)
            if legacy_rel:
                operations.append({'file': path, 'op': 'drop-legacy-relations',
                                   'span': list(legacy_rel.span())})
            continue
        locations = [location for entity in registry.entities.values() for location in entity.locations
                     if location.path == path and location.section_kind in WRAPPABLE_KINDS]
        # innermost first: L wraps the whole file last, via _wrap_l_container
        order = {'EX': 0, 'SRC': 0, 'KP': 0, 'K': 1, 'TEACH': 1, 'PATH': 2, 'START': 2, 'MGMT': 2}
        for location in sorted(locations, key=lambda l: order.get(l.section_kind, 3)):
            kind = location.section_kind
            if kind in {'INDEX', 'CHAPTER', 'STEP', 'L'}:
                continue
            span = _heading_span(text, location.block_id)
            if not span:
                if where_needed(kind, location):
                    unresolved.append({'code': 'UNRESOLVED_MIGRATION', 'file': path,
                                       'entity': location.block_id,
                                       'reason': 'legacy range has no tail block anchor to prove its end',
                                       'suggestion': 'confirm the entity range manually, then re-run the plan'})
                continue
            operations.append({'file': path, 'op': 'wrap', 'kind': kind, 'entity': location.block_id,
                               'section_id': location.section_id, 'span': list(span)})
        # Knowledge entities also live in their authoring fragment, which is the
        # v2 source for the assembled view even though it is not canonical.
        index_path = course / '_工作区/章节索引.json'
        chapter_paths = {c['id']: c['path'] for c in read_json(index_path).get('chapters', [])} \
            if index_path.exists() else {}
        for entry in read_jsonl(course / '_工作区/知识索引.jsonl') \
                if (course / '_工作区/知识索引.jsonl').exists() else []:
            if entry.get('status') == 'merged' or chapter_paths.get(entry.get('chapter_id')) != path:
                continue
            span = _heading_span(text, entry['id'])
            if not span:
                unresolved.append({'code': 'UNRESOLVED_MIGRATION', 'file': path,
                                   'entity': entry['id'],
                                   'reason': '知识分片缺少可证明范围的末尾块锚点',
                                   'suggestion': '人工确认该知识条目范围后重新生成迁移计划'})
                continue
            operations.append({'file': path, 'op': 'wrap', 'kind': 'K', 'entity': entry['id'],
                               'section_id': entry['id'], 'span': list(span)})
        for entity in sorted((e for e in registry.entities.values() if e.type == 'IDX'), key=lambda e: e.id):
            for location in entity.locations:
                if location.path != path or location.section_kind != 'INDEX':
                    continue
                if f'<!-- BL-INDEX:BEGIN {entity.id} -->' in text:
                    continue
                title, note = ('复习目录', '核心复习目录；卡片以稳定 KP ID 定位。') \
                    if entity.id == 'IDX-CORE' else ('章节索引', '全部已登记知识分片的目录；每章以稳定 KC ID 定位。')
                operations.append({'file': path, 'op': 'insert-container', 'entity': entity.id,
                                   'title': title, 'note': note, 'span': [0, 0]})
    return operations, unresolved, documents


def where_needed(kind, location):
    return kind in {'K', 'EX', 'KP', 'SOURCE', 'SRC'}


def _apply_operations(course, operations, documents):
    updated = dict(documents)
    grouped = {}
    for operation in operations:
        grouped.setdefault(operation['file'], []).append(operation)
    for path, items in grouped.items():
        text = updated[path]
        for item in sorted(items, key=lambda o: (o.get('span', [0])[0], o.get('span', [0, 0])[1]),
                           reverse=True):
            name = item['op']
            if name == 'drop-legacy-relations':
                start, end = item['span']
                text = text[:start] + text[end:]
                continue
            if name == 'insert-container':
                from assemble_knowledge import index_block
                text = index_block(item['entity'], item['title'], item['note']) + '\n' + text.lstrip('\n')
                continue
            if name != 'wrap':
                continue
            start, end = item['span']
            kind, sid = item['kind'], item['section_id']
            block = item['entity']
            segment = _strip_tail_block(text[start:end], block)
            anchored = _insert_after_first_heading(segment, 0, len(segment), block)
            if anchored is None:
                anchored = f'{block} ^{block}\n\n' + segment
            marker_kind = {'L': 'BL-L', 'K': 'BL-K', 'EX': 'BL-EX', 'KP': 'BL-KP', 'PATH': 'BL-PATH',
                           'STEP': 'BL-STEP', 'START': 'BL-START', 'MGMT': 'BL-MGMT', 'INDEX': 'BL-INDEX',
                           'CHAPTER': 'BL-CHAPTER', 'TEACH': 'BL-TEACH', 'SOURCE': 'BL-SOURCE'}.get(kind, f'BL-{kind}')
            wrapped = f'<!-- {marker_kind}:BEGIN {sid} -->\n\n{anchored.rstrip()}\n\n<!-- {marker_kind}:END {sid} -->'
            text = text[:start] + wrapped + text[end:]
        updated[path] = text
    return updated


def _wrap_l_container(course, documents, registry):
    """BL-L wraps the whole lesson body so nested TEACH/EX sections stay inside."""
    for path, text in documents.items():
        if '<!-- BL-L:BEGIN ' in text or path in DERIVED_DOCUMENTS:
            continue
        lessons = [location for entity in registry.active_entities('L')
                   for location in entity.locations if location.path == path]
        if not lessons:
            continue
        location = lessons[0]
        frontmatter = ''
        body = text
        match = re.match(r'\A---\n.*?\n---\n', text, re.S)
        if match:
            frontmatter, body = match.group(0), text[match.end():]
        anchored = _insert_after_first_heading(body, 0, len(body), location.block_id)
        if anchored is None:
            anchored = f'{location.block_id} ^{location.block_id}\n\n' + body
        documents[path] = (frontmatter + f'<!-- BL-L:BEGIN {location.block_id} -->\n\n'
                           + anchored.rstrip() + f'\n\n<!-- BL-L:END {location.block_id} -->\n')


def _convert_legacy_links(course, documents, registry):
    """Unique mappings only; everything ambiguous is reported, never guessed."""
    converted, unresolved = {}, []
    for path, text in documents.items():
        mapping = {}
        for link in re.finditer(r'(!?)\[\[([^\]\n|]+)(?:\|([^\]\n]*))?\]\]', text):
            target, label, embed = link[2].strip(), link[3], bool(link[1])
            if '#' not in target or target.partition('#')[2].startswith('^'):
                continue
            file_path, _, heading = target.partition('#')
            candidates = []
            for entity in registry.entities.values():
                for location in entity.locations:
                    if location.path.removesuffix('.md') == file_path and entity.label == heading:
                        candidates.append(location)
            if len(candidates) == 1:
                location = candidates[0]
                mapping[target] = f'{location.path.removesuffix(".md")}#^{location.block_id}'
            else:
                unresolved.append({'code': 'UNRESOLVED_MIGRATION', 'file': path, 'target': target,
                                   'reason': 'no unique registered mapping for heading target',
                                   'expected': 'one registered location', 'actual': f'{len(candidates)} candidates'})
        if mapping:
            converted[path] = mapping
    return converted, unresolved


def plan_migration(course, to_version=2):
    course = Path(course)
    if (course / '_工作区/课程配置.json').exists():
        current = read_json(course / '_工作区/课程配置.json').get('link_schema_version', 1)
        if current >= to_version:
            return {'no_op': True, 'current_version': current, 'to_version': to_version}
    registry = Registry(course)
    operations, unresolved, documents = _wrap_legacy_sections(course, None, registry)
    converted, link_unresolved = _convert_legacy_links(course, documents, registry)
    unresolved.extend(link_unresolved)
    plan = {'schema_version': 1, 'kind': 'link-migration', 'to_version': to_version,
            'graph_policy_version': GRAPH_POLICY_VERSION, 'created_at': now(),
            'files': sorted({operation['file'] for operation in operations}),
            'operations': operations,
            'link_rewrites': {path: dict(mapping) for path, mapping in converted.items()},
            'unresolved': unresolved,
            'inventory': {path: sha256(course / path) for path in documents}}
    target = course / (PLAN_PREFIX + hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12] + '.json')
    write_json(target, plan)
    return {'plan': rel(course, target), 'files': len(plan['files']),
            'operations': len(operations), 'link_rewrites': sum(len(m) for m in converted.values()),
            'unresolved': unresolved}


def _backup(course, documents):
    stamp = now().replace(':', '').replace('-', '')[:15]
    folder = course / BACKUP_DIR / stamp
    for path, text in documents.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(target, text)
    for name in ('_工作区/课程配置.json', '_工作区/链接映射.json', '_工作区/章节索引.json',
                 '_工作区/知识索引.jsonl', '_工作区/核心卡片计划.json', '_工作区/练习索引.jsonl',
                 '_工作区/实体注册表.json'):
        source = course / name
        if source.exists():
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return folder


def apply_migration(course, plan_path):
    course = Path(course)
    plan = read_json(inside(course, plan_path))
    if plan.get('kind') != 'link-migration':
        raise ValueError('Not a link migration plan')
    from convert_materials import locked
    if any((course / ledger).exists() for ledger in ('_工作区/转写任务.json', '_工作区/讲义任务.json')):
        from obsidian_links import _active_attempts
        for ledger in ('转写任务.json', '讲义任务.json'):
            if _active_attempts(course, ledger):
                raise ValueError('先停止并收集全部活动成员，再执行迁移')
    registry = Registry(course)
    documents = migration_documents(course)
    for path, expected in plan.get('inventory', {}).items():
        if path not in documents:
            raise ValueError('迁移基线缺失：' + path)
        if sha256(course / path) != expected:
            raise ValueError('迁移基线变化，请重新生成计划：' + path)
    backup = _backup(course, documents)
    journal = course / JOURNAL
    entry = {'schema_version': 1, 'kind': 'apply', 'plan': plan_path, 'backup': rel(course, backup),
             'at': now(), 'files': {}, 'status': 'started'}
    try:
        # The version switch happens before any derived rebuild so that the
        # knowledge view and relation regions are produced by the v2 writers.
        config_path = course / '_工作区/课程配置.json'
        config = read_json(config_path) if config_path.exists() else {}
        config.update(link_mode='obsidian', link_schema_version=LINK_SCHEMA_VERSION,
                      graph_policy_version=GRAPH_POLICY_VERSION,
                      course_vault_prefix=config.get('course_vault_prefix', ''),
                      layout=config.get('layout', 'working'))
        write_json(config_path, config)
        updated = _apply_operations(course, plan['operations'], documents)
        _wrap_l_container(course, updated, registry)
        from obsidian_links import rewrite_targets
        for path, mapping in plan.get('link_rewrites', {}).items():
            if path not in updated:
                continue
            updated[path] = rewrite_targets(updated[path], dict(mapping))
        for path, text in updated.items():
            if text != documents.get(path):
                atomic_text(course / path, text)
                entry['files'][path] = {'before': plan['inventory'].get(path), 'after': sha256(course / path)}
        fragments_changed = any(path.startswith('_工作区/章节知识/') and updated.get(path) != documents.get(path)
                                for path in updated)
        if fragments_changed or not (course / '知识内容.md').is_file():
            from assemble_knowledge import rebuild_knowledge
            rebuild_knowledge(course)
        from obsidian_links import build_map
        build_map(course)
        from relation_renderer import sync_course_relations
        report = sync_course_relations(course, phase='working')
        registry = Registry(course)
        registry.save()
        errors = _validate_migrated(course, plan)
        if errors:
            raise ValueError('迁移后校验失败：' + '; '.join(errors))
        entry.update(status='committed', changed=sorted(entry['files']), relation_edges=report.get('edges', 0))
        atomic_text(journal, (journal.read_text(encoding='utf-8') if journal.exists() else '')
                    + json.dumps(entry, ensure_ascii=False) + '\n')
        return {'applied': True, 'plan': plan_path, 'backup': rel(course, backup),
                'changed': sorted(entry['files']), 'unresolved': plan.get('unresolved', []),
                'relations': report}
    except (OSError, ValueError, KeyError) as exc:
        entry.update(status='rolled-back', error=str(exc))
        atomic_text(journal, (journal.read_text(encoding='utf-8') if journal.exists() else '')
                    + json.dumps(entry, ensure_ascii=False) + '\n')
        rollback_migration(course, rel(course, journal), entry=entry)
        raise


def _validate_migrated(course, plan):
    from obsidian_links import validate_obsidian_links
    errors = validate_obsidian_links(course, phase='working')
    if plan.get('unresolved'):
        errors = [error for error in errors
                  if 'UNRESOLVED_MIGRATION' not in error]
    return errors


def rollback_migration(course, journal_path, entry=None):
    course = Path(course)
    journal = inside(course, journal_path)
    if entry is None:
        records = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines() if line.strip()]
        entry = next((record for record in reversed(records) if record.get('status') in
                      {'started', 'rolled-back'}), records[-1])
    backup = inside(course, entry['backup'])
    restored = []
    if backup.exists():
        for path in sorted(backup.rglob('*')):
            if path.is_dir():
                continue
            relative = path.relative_to(backup).as_posix()
            atomic_text(course / relative, path.read_text(encoding='utf-8'))
            restored.append(relative)
    return {'rolled_back': True, 'backup': entry['backup'], 'restored': sorted(restored)}
