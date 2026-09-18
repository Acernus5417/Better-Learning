"""Concatenate chapter knowledge verbatim into the complete knowledge document."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from _common import validation_run, validation_context, atomic_text, inside, read_json, read_jsonl, sha256, write_json
from entity_sections import SectionError, parse_sections


def knowledge_sections(text, registry=None):
    """K sections of a chapter fragment; missing boundaries are a hard error in v2."""
    try:
        index = parse_sections(text, registry, strict_registry=False)
    except SectionError as exc:
        raise ValueError('SECTION_BOUNDARY_INVALID: 知识分片必须使用显式 BL-K 边界（见迁移入口）: '
                         + exc.message) from exc
    return {section.id: section for section in index.sections.values() if section.kind == 'K'}


def index_block(container_id, title, note):
    return (f'<!-- BL-INDEX:BEGIN {container_id} -->\n\n## {title}\n\n{container_id} ^{container_id}\n\n{note}\n\n'
            f'<!-- BL-INDEX:END {container_id} -->\n')


def chapter_block(chapter_id, title, body):
    return (f'<!-- BL-CHAPTER:BEGIN {chapter_id} -->\n\n## {title}\n\n{chapter_id} ^{chapter_id}\n\n'
            f'{body.strip()}\n\n<!-- BL-CHAPTER:END {chapter_id} -->\n')


def rebase_links(text: str, fragment: Path, course: Path) -> str:
    """Keep text intact while resolving standard relative links from the new location."""
    def target(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or not parsed.path:
            return value
        path = (fragment.parent / unquote(parsed.path)).resolve()
        if not path.is_relative_to(course.resolve()):
            raise ValueError('Knowledge fragment link leaves course: ' + value)
        result = quote(path.relative_to(course.resolve()).as_posix(), safe='/')
        if parsed.query:
            result += '?' + parsed.query
        if parsed.fragment:
            result += '#' + parsed.fragment
        return result

    def inline(match):
        value = match.group(2) or match.group(3)
        replacement = target(value)
        # Only replace the destination; preserve title and label verbatim.
        start, end = match.span(2 if match.group(2) is not None else 3)
        return match.group(0)[:start - match.start()] + replacement + match.group(0)[end - match.start():]

    output = []
    fence = None
    pattern = r'(!?\[[^\]\n]*\]\(\s*)(?:<([^>]+)>|([^\s)]+))((?:\s+["\'][^\n]*?["\'])?\s*\))'
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            output.append(line)
            continue
        if fence is None:
            # Inline code may itself teach Markdown link syntax; leave it untouched.
            segments = []
            cursor = 0
            for code in re.finditer(r'(`+).*?\1', line):
                segments.append(re.sub(pattern, inline, line[cursor:code.start()]))
                segments.append(code.group(0))
                cursor = code.end()
            segments.append(re.sub(pattern, inline, line[cursor:]))
            line = ''.join(segments)
            line = re.sub(r'(^\s{0,3}\[[^\]\n]+\]:\s*)(?:<([^>]+)>|(\S+))', inline, line)
        output.append(line)
    from obsidian_links import enabled, link_map, rewrite_targets
    result = ''.join(output)
    if enabled(course):
        mapping = link_map(course)
        result = rewrite_targets(result, {'知识内容': mapping['knowledge'], '学习路径': mapping['learning_path'], '核心知识点': mapping['core_cards']})
    return result


@validation_run
def build_content(course: Path, allow_partial: bool = False, ai_request: str | None = None, validate_transcripts=True) -> str:
    from transcribe_materials import check_all
    if not validate_transcripts and str(course.resolve()) not in validation_context().transcripts_checked:
        raise ValueError('Unchecked build requires successful upstream validation in this command')
    errors = check_all(course) if validate_transcripts else []
    if ai_request:
        request = inside(course, ai_request)
        if not request.read_text(encoding='utf-8').strip():
            raise ValueError('必须保存用户明确要求 AI 补全的原话与范围')
        entries = read_jsonl(course / '_工作区/知识索引.jsonl')
        if not entries or any(k.get('kind') != 'ai_supplement' for k in entries):
            raise ValueError('原件未完成时的授权 AI 补全模式，所有知识必须标为 ai_supplement，不得冒充材料转写')
    if errors and not ai_request:
        raise ValueError('先完成全部文件转写，再汇总知识：' + '; '.join(errors))
    index = read_json(course / '_工作区' / '章节索引.json')
    chapters = index.get('chapters', [])
    if not chapters:
        raise ValueError('No knowledge chapters registered')
    ids = [c['id'] for c in chapters]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate knowledge chapter ID')
    unfinished = [c['id'] for c in chapters if c.get('status') != 'complete']
    if unfinished and not allow_partial:
        raise ValueError('Knowledge chapters are incomplete: ' + ', '.join(unfinished))
    chapters = sorted([c for c in chapters if c.get('status') == 'complete'], key=lambda c: (c['order'], c['id']))
    from entity_registry import is_v2
    if is_v2(course):
        return _build_content_v2(course, chapters, unfinished, ai_request, allow_partial)
    sections = ['# 知识内容\n\n',
                '> 未完成：仅含当前已完成章节。\n\n' if unfinished else '> 全部已登记知识分片的完整汇编；资料覆盖状态以质量报告为准。\n\n',
                '## 章节索引\n\n']
    if ai_request:
        sections.insert(1, '> 用户明确授权的 AI 补全知识库；不代表已读取原教材或完成资料覆盖。补全范围见用户请求记录。\n\n')
    from obsidian_links import enabled, link_map, render_wikilink
    obsidian = enabled(course)
    target = link_map(course)['knowledge'] if obsidian else None
    for c in chapters:
        sections.append('- ' + render_wikilink(target, block=c['id'], label=c['title']) + '\n' if obsidian else f"- [{c['title']}](#chapter-{c['id']})\n")
    for c in chapters:
        path = inside(course, c['path'])
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('Empty knowledge chapter: ' + c['id'])
        sections.append(f'\n\n---\n\n<a id="chapter-{c["id"]}"></a>\n\n')
        if obsidian: sections.append('^' + c['id'] + '\n\n')
        sections.append(rebase_links(text, path, course))
    return ''.join(sections)


def _build_content_v2(course, chapters, unfinished, ai_request, allow_partial):
    """v2 knowledge view: IDX container + one BL-CHAPTER per fragment, BL-K preserved."""
    knowledge = read_jsonl(course / '_工作区/知识索引.jsonl')
    members = {}
    for entry in knowledge:
        if entry.get('status') == 'merged':
            continue
        members.setdefault(entry.get('chapter_id'), []).append(entry['id'])
    parts = ['# 知识内容\n\n']
    if unfinished:
        parts.append('> 未完成：仅含当前已完成章节。\n\n')
    else:
        parts.append('> 全部已登记知识分片的完整汇编；资料覆盖状态以质量报告为准。\n\n')
    if ai_request:
        parts.insert(1, '> 用户明确授权的 AI 补全知识库；不代表已读取原教材或完成资料覆盖。'
                        '补全范围见用户请求记录。\n\n')
    parts.append(index_block('IDX-KNOWLEDGE', '章节索引',
                             '全部已登记知识分片的目录；每章以稳定 KC ID 定位。'))
    parts.append('\n---\n\n')
    for chapter in chapters:
        path = inside(course, chapter['path'])
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('Empty knowledge chapter: ' + chapter['id'])
        found = set(knowledge_sections(text))
        expected = set(members.get(chapter['id'], []))
        if expected != found:
            raise ValueError(
                f'SECTION_BOUNDARY_INVALID: {chapter["id"]} 的 BL-K 边界与知识索引不一致；'
                f'缺失 {sorted(expected - found)}，多出 {sorted(found - expected)}')
        parts.append(chapter_block(chapter['id'], chapter['title'], rebase_links(text, path, course)))
        parts.append('\n---\n\n')
    return ''.join(parts).rstrip() + '\n'


def rebuild_knowledge(course: Path, allow_partial: bool = False, ai_request: str | None = None):
    """Write the knowledge view and re-render its derived relation regions."""
    from entity_registry import is_v2
    from relation_renderer import sync_course_relations
    text = build_content(course, allow_partial, ai_request)
    atomic_text(course / '知识内容.md', text)
    report = {'output': str(course / '知识内容.md'), 'characters': len(text)}
    if is_v2(course):
        report.update(sync_course_relations(course, phase='working'))
    return report


REL_SPAN = re.compile(r'[ \t]*<!-- BL-REL:BEGIN[^>]*-->.*?<!-- BL-REL:END[^>]*-->\n?', re.S)


def _teaching_text(text):
    return REL_SPAN.sub('', text)


@validation_run
def require_knowledge(course: Path):
    path = course / '知识内容.md'
    if not path.is_file() or not path.read_text(encoding='utf-8').strip():
        raise ValueError('缺少非空知识内容.md；禁止先写学习路径、讲义或卡片')
    request = None
    auth_path = course / '_工作区/AI补全授权.json'
    if auth_path.exists():
        auth = read_json(auth_path)
        request = auth['request']
        if sha256(inside(course, request)) != auth['sha256']:
            raise ValueError('AI补全请求记录已变化，需要重新确认范围并生成知识库')
    # Relation regions are a derived view: rendering them must not look like a
    # knowledge base that no longer matches its fragments.
    if _teaching_text(path.read_text(encoding='utf-8')) != _teaching_text(build_content(course, ai_request=request)):
        raise ValueError('知识内容.md 与当前完整分片/授权范围不一致，请先重建知识库')
    return {'ready': True, 'knowledge': str(path), 'mode': 'ai_supplement' if request else 'materials'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--ai-request', help='仅用户明确要求改用AI补全时，指定保存其原话与范围的课程相对文件')
    parser.add_argument('--check', action='store_true', help='写路径/讲义/卡片之前验证知识库前置条件')
    args = parser.parse_args()
    try:
        course = args.course.resolve()
        if args.check:
            print(json.dumps(require_knowledge(course), ensure_ascii=False))
            return 0
        text = build_content(course, args.allow_partial, args.ai_request)
        previous = (course / '知识内容.md').read_text(encoding='utf-8') if (course / '知识内容.md').exists() else ''
        report = rebuild_knowledge(course, args.allow_partial, args.ai_request)
        from lesson_tasks import semantic, invalidate
        if semantic(previous) != semantic(text): invalidate(course, 'knowledge content changed')
        authorization = course / '_工作区/AI补全授权.json'
        if args.ai_request:
            write_json(authorization, {'request': args.ai_request, 'sha256': sha256(inside(course, args.ai_request))})
        else:
            authorization.unlink(missing_ok=True)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
