"""Concatenate chapter knowledge verbatim into the complete knowledge document."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from _common import atomic_text, inside, read_json


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
    return ''.join(output)


def build_content(course: Path, allow_partial: bool = False) -> str:
    from transcribe_materials import check_all
    errors = check_all(course)
    if errors:
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
    sections = ['# 知识内容\n\n',
                '> 未完成：仅含当前已完成章节。\n\n' if unfinished else '> 全部已登记知识分片的完整汇编；资料覆盖状态以质量报告为准。\n\n',
                '## 章节索引\n\n']
    for c in chapters:
        sections.append(f"- [{c['title']}](#chapter-{c['id']})\n")
    for c in chapters:
        path = inside(course, c['path'])
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            raise ValueError('Empty knowledge chapter: ' + c['id'])
        sections.append(f'\n\n---\n\n<a id="chapter-{c["id"]}"></a>\n\n')
        sections.append(rebase_links(text, path, course))
    return ''.join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    try:
        course = args.course.resolve()
        text = build_content(course, args.allow_partial)
        atomic_text(course / '知识内容.md', text)
        print(json.dumps({'output': str(course / '知识内容.md'), 'characters': len(text)}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
