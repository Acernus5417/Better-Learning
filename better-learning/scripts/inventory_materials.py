"""Inventory explicit material inputs without moving or uploading originals."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _common import atomic_text, read_json, sha256, write_json
from templates import snapshot as template_snapshot


def inventory(course: Path, inputs: list[Path]) -> dict:
    course = course.resolve()
    files = set()
    for item in inputs:
        item = item.resolve()
        if not item.exists():
            raise ValueError(f'Input does not exist: {item}')
        candidates = item.rglob('*') if item.is_dir() else [item]
        for candidate in candidates:
            resolved = candidate.resolve()
            # Accept explicitly archived originals, but never re-ingest generated output.
            in_output = resolved.is_relative_to(course) and not resolved.is_relative_to(course / '原始资料')
            operational = item.is_dir() and any(
                part in {'.git', '.codex', '.agents', '__pycache__'}
                for part in candidate.relative_to(item).parts)
            if candidate.is_file() and not in_output and not operational:
                files.add(resolved)
    if not files:
        raise ValueError('No input files outside the output course directory')
    work = course / '_工作区'
    index_file = work / '资料索引.json'
    new_course = not index_file.exists()
    previous = read_json(index_file) if index_file.exists() else {'sources': [], 'next_id': 1}
    by_path = {s['path']: s for s in previous['sources']}
    next_id = max(previous.get('next_id', 1), 1 + max(
        [int(s['id'].split('-')[-1]) for s in previous['sources']] or [0]))
    sources = []
    hashes = {}
    for path in sorted(files, key=lambda p: str(p).casefold()):
        old = by_path.get(str(path))
        sid = old['id'] if old else f'SRC-{next_id:03d}'
        if not old:
            next_id += 1
        stamp = path.stat()
        digest = (old['sha256'] if old and old.get('size') == stamp.st_size
                  and old.get('mtime_ns') == stamp.st_mtime_ns and old.get('ctime_ns') == stamp.st_ctime_ns
                  else sha256(path))
        entry = {'id': sid, 'path': str(path), 'name': path.name,
                 'sha256': digest, 'size': stamp.st_size, 'mtime_ns': stamp.st_mtime_ns, 'ctime_ns': stamp.st_ctime_ns,
                 'format': path.suffix.lower().lstrip('.') or 'unknown'}
        if digest in hashes:
            entry['duplicate_of'] = hashes[digest]
        else:
            hashes[digest] = sid
        sources.append(entry)
    changed = {(s['id'], s['path'], s['sha256']) for s in sources} != {
        (s['id'], s['path'], s['sha256']) for s in previous['sources']}
    data = {'schema_version': 1, 'next_id': next_id, 'sources': sources}
    write_json(index_file, data)
    if new_course:
        from obsidian_links import initialize
        initialize(course)
    elif changed:
        from lesson_tasks import invalidate
        invalidate(course, 'materials changed')
    rows = ['# 资料清单', '', '本清单由盘点脚本生成；资料范围是否齐全由用户确认，记录在学习需求及进度中。', '',
            '| ID | 文件 | 格式 | 字节数 | 完全重复来源 |', '| --- | --- | --- | --- | --- |']
    for s in sources:
        name = s['name'].replace('|', '\\|').replace('\n', ' ')
        rows.append(f"| {s['id']} | {name} | {s['format']} | {s['size']} | {s.get('duplicate_of', '')} |")
    atomic_text(course / '资料清单.md', '\n'.join(rows) + '\n')
    state_file = work / '生成进度.json'
    if state_file.exists():
        state = read_json(state_file)
        state['template_hashes'] = template_snapshot()
        if changed:
            state.setdefault('confirmations', {})['materials'] = False
            state['stage'] = 'needs_update'
            state['next_action'] = '资料范围或内容变化：重新确认范围，更新受影响的知识、路径、讲义和卡片'
        write_json(state_file, state)
    else:
        write_json(state_file, {'schema_version': 1,
            'confirmations': {'goal': False, 'materials': False, 'baseline': False},
            'stage': 'intake', 'completed_chapters': [], 'completed_lessons': [],
            'blockers': [], 'next_action': '确认学习目标和资料范围',
            'template_hashes': template_snapshot()})
    return {'sources': len(sources), 'changed': changed, 'manifest': str(index_file)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', type=Path, required=True)
    parser.add_argument('inputs', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(inventory(args.course, args.inputs), ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
