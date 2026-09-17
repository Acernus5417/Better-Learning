"""Validate course structure and coverage; semantic teaching review is separate."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

from _common import validation_run, validation_context, extraction_path, inside, read_json, read_jsonl, sha256, source_index, write_json
from assemble_knowledge import build_content
from entity_registry import Registry, config as course_config, document as registry_document, is_v2
from entity_sections import SectionError, parse_sections

REQUIRED = ['开始学习.md', '学习需求.md', '资料清单.md', '知识内容.md', '学习路径.md',
            '核心知识点.md', '学习反馈.md', '质量报告.md']
SNAPSHOT = '_工作区/链接校验报告.json'
MODES = ('legacy-report', 'staged', 'final', 'packaged')


def find_document(course, name):
    """Look in both layouts: course root and 课程文档/. Never assume only one."""
    for candidate in (name, '课程文档/' + name):
        if (course / candidate).is_file():
            return course / candidate
    return course / name


def code(prefix, message):
    return f'[{prefix}] {message}' if prefix else message


def without_code(text: str) -> str:
    text = re.sub(r'^\s*(`{3,}|~{3,}).*?^\s*\1\s*$', '', text, flags=re.M | re.S)
    return re.sub(r'(`+).*?\1', '', text, flags=re.S)


def anchors(text: str) -> tuple[set[str], list[str]]:
    text = without_code(text)
    explicit = re.findall(r'<a\s+[^>]*id=["\']([^"\']+)["\'][^>]*>', text, re.I)
    from obsidian_links import block_ids
    explicit += block_ids(text)
    result = set(explicit)
    seen = Counter()
    for match in re.finditer(r'^#{1,6}\s+(.+?)\s*#*\s*$', text, re.M):
        title = re.sub(r'<[^>]+>', '', match.group(1)).lower()
        title = re.sub(r'[^\w\-\s]', '', title)
        title = re.sub(r'\s', '-', title)
        count = seen[title]
        result.add(title if count == 0 else title + f'-{count}')
        seen[title] += 1
    return result, [key for key, value in Counter(explicit).items() if value > 1]


def destinations(text: str) -> list[str]:
    text = without_code(text)
    result = []
    pattern = r'!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+["\'][^\n]*?["\'])?\s*\)'
    for match in re.finditer(pattern, text):
        result.append(match.group(1) or match.group(2))
    # Reference link definitions are also checked even if not currently used.
    for match in re.finditer(r'^\s{0,3}\[[^\]\n]+\]:\s*(?:<([^>]+)>|(\S+))', text, re.M):
        result.append(match.group(1) or match.group(2))
    for match in re.finditer(r'<(?:img|a)\s+[^>]*(?:src|href)=["\']([^"\']+)["\']', text, re.I):
        result.append(match.group(1))
    return result


@validation_run
def validate(course: Path, mode: str = 'final') -> dict:
    if mode not in MODES:
        raise ValueError('Unknown validator mode: ' + mode)
    phase = {'legacy-report': 'working', 'staged': 'staged'}.get(mode, mode)
    course = course.resolve()
    errors = []
    warnings = ['结构检查不证明教学正确性、公式渲染或实际学习效果；须完成内容复核。']
    counts = {'sources': 0, 'units': 0, 'knowledge': 0, 'lessons': 0, 'cards': 0}
    v2 = is_v2(course)
    registry_obj = Registry(course) if v2 else None

    def fail(message):
        errors.append(message)

    def load(relative, lines=False, fallback=None):
        try:
            path = inside(course, relative)
            return read_jsonl(path) if lines else read_json(path)
        except (OSError, ValueError, TypeError) as exc:
            fail(f'{relative}: {exc}')
            return fallback

    def file(relative):
        try:
            path = find_document(course, relative)
            if not path.is_file() or not path.stat().st_size:
                fail('缺少或为空：' + relative)
                return None
            return path
        except (OSError, ValueError, TypeError) as exc:
            fail(str(exc))
            return None

    for name in REQUIRED:
        file(name)
    state = load('_工作区/生成进度.json', fallback={})
    for key in ['goal', 'materials', 'baseline']:
        if state.get('confirmations', {}).get(key) is not True:
            fail('用户确认未完成：' + key)
    if state.get('blockers'):
        fail('生成进度中仍有阻塞项')
    if state.get('stage') in {'needs_update', 'blocked'}:
        fail('生成进度尚处于 ' + state['stage'])

    manifest = load('_工作区/资料索引.json', fallback={'sources': []})
    sources = manifest.get('sources', [])
    counts['sources'] = len(sources)
    from transcribe_materials import check_all, context as transcript_context, verify_vision
    for error in check_all(course):
        fail(error)
    if not sources:
        fail('没有源文件')
    if len({s['id'] for s in sources}) != len(sources):
        fail('资料 ID 重复')
    source_by_id = {s['id']: s for s in sources}
    units = {}
    for source in sources:
        try:
            if sha256(Path(source['path'])) != source['sha256']:
                fail('源文件已变更，需要重新盘点：' + source['id'])
        except OSError as exc:
            fail('源文件不可访问：' + source['id'] + ': ' + str(exc))
        relative = (extraction_path(course, source) / '定位映射.json').relative_to(course).as_posix()
        mapping = load(relative, fallback={})
        entries = mapping.get('units', [])
        if mapping.get('source_hash') != source['sha256'] or mapping.get('source_id') != source['id']:
            fail('提取版本与源文件不匹配：' + source['id'])
        expected = mapping.get('total_units', 0)
        if expected < 1 or len(entries) != expected:
            fail('源单元数量不完整：' + source['id'])
        if [u.get('ordinal') for u in entries] != list(range(1, expected + 1)):
            fail('源单元顺序缺失或重复：' + source['id'])
        for unit in entries:
            key = (source['id'], unit['id'])
            if key in units:
                fail('重复源单元：' + str(key))
            units[key] = unit
            for target in unit.get('chunks', []) + unit.get('images', []):
                # Empty extracted text is allowed, but its file must exist.
                try:
                    if not inside(course, target).is_file():
                        fail('缺少提取文件：' + target)
                except ValueError as exc:
                    fail(str(exc))
    counts['units'] = len(units)

    index = load('_工作区/章节索引.json', fallback={})
    chapters = index.get('chapters', [])
    lessons = index.get('lessons', [])
    counts['lessons'] = len(lessons)
    chapter_by_id = {c['id']: c for c in chapters}
    lesson_by_id = {c['id']: c for c in lessons}
    for collection, label in [(chapters, '知识章节'), (lessons, '学习章节')]:
        if not collection:
            fail('没有' + label)
        if len({c['id'] for c in collection}) != len(collection):
            fail(label + ' ID 重复')
        if len({c['order'] for c in collection}) != len(collection):
            fail(label + '排序重复')
        for chapter in collection:
            file(chapter['path'])
            if label == '学习章节' and not chapter['path'].replace('\\', '/').startswith('学习文档/'):
                fail('学习章节未保存在学习文档目录：' + chapter['id'])
            if chapter.get('status') != 'complete':
                fail(label + '未完成：' + chapter['id'])

    knowledge = load('_工作区/知识索引.jsonl', lines=True, fallback=[])
    counts['knowledge'] = len(knowledge)
    knowledge = [k for k in knowledge if k.get('status') != 'merged']
    known = {k['id']: k for k in knowledge}
    if not knowledge:
        fail('没有知识条目')
    if len(known) != len(knowledge):
        fail('知识 ID 重复')
    cache = {}

    def get_anchors(relative):
        if relative not in cache:
            path = file(relative)
            cache[relative] = anchors(path.read_text(encoding='utf-8-sig'))[0] if path else set()
        return cache[relative]

    knowledge_doc = registry_document(course, '知识内容.md')
    cards_doc = registry_document(course, '核心知识点.md')
    parsed = {}
    if v2:
        for label, relative, kind in (('knowledge', knowledge_doc, 'K'), ('cards', cards_doc, 'KP')):
            try:
                text = (course / relative).read_text(encoding='utf-8-sig')
                parsed[label] = parse_sections(text, None, strict_registry=False)
            except (OSError, SectionError) as exc:
                fail(code('SECTION_BOUNDARY_INVALID', f'{relative}: {exc}'))
                parsed[label] = None

    def has_section(label, kind, section_id):
        index = parsed.get(label)
        return bool(index and index.get(kind, section_id))

    def fragment_has(relative, kind, section_id):
        try:
            index = parse_sections((course / relative).read_text(encoding='utf-8-sig'), None, strict_registry=False)
        except (OSError, SectionError) as exc:
            fail(code('SECTION_BOUNDARY_INVALID', f'{relative}: {exc}'))
            return None
        return bool(index.get(kind, section_id))

    for k in knowledge:
        kid = k['id']
        if k.get('status') != 'verified':
            fail('知识尚未复核：' + kid)
        chapter = chapter_by_id.get(k.get('chapter_id'))
        if not chapter:
            fail('知识没有有效分片：' + kid)
        elif v2:
            if not has_section('knowledge', 'K', kid):
                fail(code('SECTION_BOUNDARY_INVALID', '知识总文件缺少 BL-K 区间：' + kid))
            if fragment_has(chapter['path'], 'K', kid) is False:
                fail(code('SECTION_BOUNDARY_INVALID', '知识分片缺少 BL-K 区间：' + kid))
        elif k.get('anchor') not in get_anchors(chapter['path']):
            fail('知识分片缺少条目锚点：' + kid)
        if not v2 and k.get('anchor') not in get_anchors('知识内容.md'):
            fail('知识总文件缺少条目锚点：' + kid)
        if k.get('kind') not in {'material', 'ai_supplement'}:
            fail('知识 kind 无效：' + kid)
        if k.get('kind') == 'material' and not k.get('source_refs'):
            fail('材料知识没有来源：' + kid)
        for ref in k.get('source_refs', []):
            if (ref.get('source_id'), ref.get('unit_id')) not in units:
                fail('知识来源单元无效：' + kid)
        if not k.get('lesson_ids'):
            fail('知识没有学习文档去向：' + kid)
        for lid in k.get('lesson_ids', []):
            lesson = lesson_by_id.get(lid)
            if not lesson:
                fail('知识引用不存在的学习章节：' + kid + ' → ' + lid)
            elif v2:
                if fragment_has(lesson['path'], 'TEACH', f'{lid}-{kid}') is False:
                    fail(code('SECTION_BOUNDARY_INVALID', f'学习章节缺少教学区间 {lid}-{kid}'))
            elif k.get('anchor') not in get_anchors(lesson['path']):
                fail('学习章节缺少知识锚点：' + kid + ' → ' + lid)
        for dependency in k.get('prerequisites', []):
            if dependency not in known:
                fail('未知前置知识：' + kid + ' → ' + dependency)
        if not isinstance(k.get('core'), bool):
            fail('知识未注明是否核心：' + kid)
        elif k['core']:
            if not k.get('core_reason'):
                fail('核心知识缺少筛选依据：' + kid)
            if not re.fullmatch(r'KP-\d{2,}-\d{2,}-\d{2,}', k.get('card_id', '')):
                fail('核心卡片 ID 无效：' + kid)
            if v2:
                if not has_section('cards', 'KP', k.get('card_id', '')):
                    fail(code('SECTION_BOUNDARY_INVALID', '核心知识点缺少 BL-KP 区间：' + kid))
            elif k.get('card_anchor') != k.get('card_id') or k.get('card_anchor') not in get_anchors('核心知识点.md'):
                fail('核心知识缺少对应卡片：' + kid)
    counts['cards'] = len({k.get('card_id') for k in knowledge if k.get('core') and k.get('card_id')})
    indexed_cards = {k.get('card_id') for k in knowledge if k.get('core') and k.get('card_id')}
    if v2:
        actual_cards = {section.id for section in (parsed.get('cards') or parse_sections('')).sections.values()
                        if section.kind == 'KP'}
    else:
        actual_cards = {a for a in get_anchors('核心知识点.md') if re.fullmatch(r'KP-\d{2,}-\d{2,}-\d{2,}', a)}
    for orphan in sorted(actual_cards - indexed_cards):
        fail('卡片没有对应知识条目：' + orphan)
    if knowledge and not counts['cards']:
        fail('没有核心知识卡片')
    for lid in lesson_by_id:
        if not any(lid in k.get('lesson_ids', []) for k in knowledge):
            fail('学习章节未映射任何知识：' + lid)

    # Iterative topological sort avoids recursion limits for large courses.
    indegree = {kid: 0 for kid in known}
    children = {kid: [] for kid in known}
    for kid, k in known.items():
        for dep in set(k.get('prerequisites', [])):
            if dep in known:
                indegree[kid] += 1
                children[dep].append(kid)
                current_orders = [lesson_by_id[l]['order'] for l in k.get('lesson_ids', []) if l in lesson_by_id]
                dependency_orders = [lesson_by_id[l]['order'] for l in known[dep].get('lesson_ids', []) if l in lesson_by_id]
                if current_orders and dependency_orders and min(dependency_orders) > min(current_orders):
                    fail('前置知识教学顺序在后：' + dep + ' → ' + kid)
    ready = [kid for kid, value in indegree.items() if not value]
    visited = 0
    while ready:
        kid = ready.pop()
        visited += 1
        for child in children[kid]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if visited != len(known):
        fail('知识前置依赖存在循环')

    coverage = load('_工作区/覆盖台账.jsonl', lines=True, fallback=[])
    covered = {}
    for record in coverage:
        key = (record.get('source_id'), record.get('unit_id'))
        if key in covered:
            fail('覆盖台账重复：' + str(key))
        covered[key] = record
        if key not in units:
            fail('覆盖台账引用当前范围外的单元：' + str(key))
    statuses = Counter()
    for key, unit in units.items():
        record = covered.get(key)
        if not record:
            fail('尚未登记覆盖：' + str(key))
            continue
        status = record.get('status')
        statuses[status] += 1
        if record.get('source_hash') != source_by_id[key[0]]['sha256']:
            fail('覆盖记录源版本过期：' + str(key))
        if status not in {'covered', 'duplicate', 'non_teaching'}:
            fail('来源内容未解决：' + str(key) + ' ' + str(status))
        if status in {'covered', 'duplicate'} and not record.get('knowledge_ids'):
            fail('覆盖记录缺少知识去向：' + str(key))
        if status in {'duplicate', 'non_teaching'} and not record.get('reason'):
            fail('跳过/合并来源未说明理由：' + str(key))
        for kid in record.get('knowledge_ids', []):
            if kid not in known:
                fail('覆盖记录引用未知知识：' + str(key) + ' → ' + kid)
        manual = record.get('manual_read_evidence')
        if manual:
            file(manual)
        if unit.get('status') in {'pending', 'blocked'} and not manual:
            fail('来源未提取且缺少补读证据：' + str(key))
        tool_transcribed = False
        try:
            _, _, _, _, _, transcript_pages = transcript_context(course, key[0])
            tool_transcribed = transcript_pages.get(key[1], {}).get('mode') == 'vision'
            if tool_transcribed:
                verify_vision(course, source_by_id[key[0]], unit)
        except (OSError, ValueError, KeyError):
            tool_transcribed = False
        if unit.get('visual_review_required') and record.get('visual_reviewed') is not True and not tool_transcribed:
            fail('视觉内容尚未复核：' + str(key))
        manifests = record.get('visual_manifests', [])
        if unit.get('content_mode') == 'visual_first' and not manifests and not tool_transcribed:
            fail('纯图像来源缺少逐区域阅读证据：' + str(key))
        if manifests:
            from prepare_visual import check_manifest
            if not record.get('visual_note'):
                fail('缺少整页视觉笔记：' + str(key))
            else:
                file(record['visual_note'])
            seen_images = set()
            for visual_path in manifests:
                try:
                    visual = read_json(inside(course, visual_path))
                    if (visual['source_id'], visual['unit_id']) != key:
                        fail('视觉证据引用其他来源单元：' + str(key))
                    seen_images.add(visual['image'])
                    for problem in check_manifest(course, visual, require_read=True):
                        fail(str(key) + ': ' + problem)
                except (OSError, ValueError, KeyError) as exc:
                    fail('视觉证据不可用：' + str(key) + ': ' + str(exc))
            if not set(unit.get('images', [])).issubset(seen_images):
                fail('源单元仍有图片未逐区域读取：' + str(key))
        if not record.get('evidence'):
            fail('缺少阅读/复核说明：' + str(key))
    for k in knowledge:
        for ref in k.get('source_refs', []):
            record = covered.get((ref.get('source_id'), ref.get('unit_id')), {})
            if k['id'] not in record.get('knowledge_ids', []):
                fail('知识与来源覆盖映射不一致：' + k['id'])
    counts['coverage'] = dict(statuses)

    try:
        expected = build_content(course, validate_transcripts=str(course.resolve()) not in validation_context().transcripts_checked)
        actual = (course / '知识内容.md').read_text(encoding='utf-8')
        if actual != expected:
            fail('知识内容.md 与知识分片不一致，请重新运行合并脚本')
    except (OSError, ValueError, KeyError) as exc:
        fail('知识汇编检查失败：' + str(exc))

    if v2:
        for error in validate_source_shells(course):
            fail(error)
    # Check learner-facing Markdown plus indexed chapter fragments, not extracted source text.
    paths = [p for p in course.rglob('*.md') if '_工作区' not in p.relative_to(course).parts]
    for p in paths:
        if any(part in {'资料转写', '原始资料'} for part in p.relative_to(course).parts): continue
        body = p.read_text(encoding='utf-8-sig')
        _, duplicates = anchors(body)
        if duplicates:
            fail(f'{p.name}: 重复锚点 {duplicates}')
        from obsidian_links import enabled, validate_document
        if enabled(course):
            for error in validate_document(course, body): fail(p.name + ': ' + error)
        for target in destinations(body):
            parsed = urlsplit(target)
            if parsed.scheme in {'https', 'http', 'mailto', 'data'}:
                continue
            if parsed.scheme or parsed.netloc:
                fail(f'{p.name}: 不可移植的链接 {target}')
                continue
            destination = (p.parent / unquote(parsed.path)).resolve() if parsed.path else p
            if not destination.is_relative_to(course):
                fail(f'{p.name}: 链接离开课程目录 {target}')
            elif not destination.exists():
                fail(f'{p.name}: 链接目标不存在 {target}')
            elif parsed.fragment and destination.suffix.lower() == '.md':
                target_anchors = anchors(destination.read_text(encoding='utf-8-sig'))[0]
                if unquote(parsed.fragment) not in target_anchors:
                    fail(f'{p.name}: 锚点不存在 {target}')
    from obsidian_links import enabled, validate_obsidian_links
    if enabled(course):
        for error in validate_obsidian_links(course, phase=phase):
            fail(code('LINKS', error))
    if v2 and mode != 'legacy-report':
        from obsidian_links import validate_links
        for relative, text in learner_texts(course).items():
            for diagnostic in validate_links(course, text, registry_obj=registry_obj, phase=phase):
                fail(code(diagnostic['code'], f"{relative}: {diagnostic['message']}"))
        for relative, text in learner_texts(course).items():
            try:
                parse_sections(text, None, strict_registry=False)
            except SectionError as exc:
                fail(code(exc.code, f'{relative}:{exc.line}: {exc.message}'))
    if enabled(course) or (course / '_工作区/讲义任务.json').exists():
        from lesson_tasks import check as check_lessons
        from core_cards import check as check_cards
        for error in check_lessons(course) + check_cards(course): fail(error)
    report = {'schema_version': 2, 'mode': mode, 'passed': not errors, 'counts': counts,
              'errors': list(dict.fromkeys(errors)), 'warnings': warnings,
              'layout': course_config(course).get('layout', 'working'),
              'graph_policy_version': course_config(course).get('graph_policy_version')}
    if v2 and mode == 'final' and not errors:
        snapshot = {'schema_version': 1, 'mode': 'final',
                    'files': {relative: sha256(course / relative)
                              for relative in learner_texts(course)},
                    'registry_revision': registry_obj.revision() if registry_obj else None}
        write_json(course / SNAPSHOT, snapshot)
        report['snapshot'] = SNAPSHOT
    if v2 and mode == 'packaged':
        for problem in validate_packaged(course, snapshot_receipt(course), errors):
            fail(problem)
        report['passed'] = not errors
        report['errors'] = list(dict.fromkeys(errors))
    return report


def snapshot_receipt(course):
    path = course / SNAPSHOT
    return read_json(path) if path.exists() else {}


def learner_texts(course):
    result = {}
    for path in sorted(course.rglob('*.md')):
        parts = path.relative_to(course).parts
        if any(part in {'资料转写', '原始资料'} for part in parts):
            continue
        if '_工作区' in parts:
            continue
        result[path.relative_to(course).as_posix()] = path.read_text(encoding='utf-8-sig')
    return result


def validate_source_shells(course):
    """Source fidelity: managed shell + precise payload exemption, never a blank skip."""
    from obsidian_links import validate_links
    problems = []
    index_path = course / '_工作区/转写索引.json'
    if not index_path.exists():
        return problems
    for entry in read_json(index_path).get('sources', []):
        if entry.get('shell') != 'v2':
            continue
        path = inside(course, entry['path'])
        text = path.read_text(encoding='utf-8-sig')
        try:
            index = parse_sections(text, None, strict_registry=False)
        except SectionError as exc:
            problems.append(code(exc.code, f'{entry["path"]}:{exc.line}: {exc.message}'))
            continue
        if index.get('SOURCE', entry['source_id']) is None:
            problems.append(code('SECTION_BOUNDARY_INVALID', f'{entry["path"]}: 缺少 BL-SOURCE 外壳'))
            continue
        if any(section.kind == 'REL' for section in index.sections.values()):
            problems.append(code('SOURCE_NOT_LEAF', f'{entry["path"]}: 来源文件不得包含关系区'))
        for payload in entry.get('payloads', []):
            uid = f"{entry['source_id']}-{payload['unit_id']}"
            if index.get('SRC', uid) is None:
                problems.append(code('SECTION_BOUNDARY_INVALID', f'{entry["path"]}: 缺少来源单元 {uid}'))
        spans = [(section.body_start, section.body_end) for section in index.sections.values()
                 if section.kind == 'SOURCE-TEXT']
        for diagnostic in validate_links(course, text, require_targets=False, payload_spans=spans,
                                         phase='working'):
            if diagnostic['code'] != 'BLOCK_REQUIRED':
                problems.append(code(diagnostic['code'], f'{entry["path"]}: {diagnostic["message"]}'))
    return problems


def validate_packaged(course, snapshot, errors):
    """Packaged layout: targets must re-resolve from retained receipts."""
    from obsidian_links import validate_obsidian_links
    problems = []
    if not snapshot:
        problems.append(code('STALE_MANIFEST', '缺少 final 阶段快照，无法证明打包后目标仍可解析'))
    for relative, digest in (snapshot.get('files') or {}).items():
        path = find_document(course, relative.split('/')[-1]) if not (course / relative).exists() else course / relative
        if not path.is_file():
            problems.append(code('MISSING_TARGET_BLOCK', '打包后缺少文件：' + relative))
            continue
        import hashlib
        current = hashlib.sha256(path.read_bytes()).hexdigest()
        if current != digest:
            problems.append(code('STALE_MANIFEST', '打包后文件内容与 final 快照不一致：' + relative))
    for error in validate_obsidian_links(course, phase='packaged'):
        problems.append(code('LINKS', error))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=Path, required=True)
    parser.add_argument('--mode', default='final', choices=list(MODES),
                        help='legacy-report|staged|final|packaged; v2 strict modes never accept legacy syntax')
    args = parser.parse_args()
    try:
        report = validate(args.course, args.mode)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = {'schema_version': 2, 'mode': args.mode, 'passed': False,
                  'errors': ['数据结构不完整或无法执行检查：' + str(exc)], 'warnings': []}
    write_json(args.course / '_工作区' / ('结构检查.json' if args.mode != 'packaged' else '打包检查.json'), report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
