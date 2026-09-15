"""Shared local-only file utilities for better-learning."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(text)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def read_jsonl(path: Path) -> list:
    result = []
    for number, line in enumerate(path.read_text(encoding='utf-8-sig').splitlines(), 1):
        if line.strip():
            try:
                result.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f'{path.name}:{number}: invalid JSON') from exc
    return result


def inside(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f'Expected course-relative path: {relative!r}')
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError(f'Path leaves course directory: {relative}')
    return result


def rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def extraction_path(course: Path, source: dict) -> Path:
    return course / '_工作区' / '提取内容' / source['id'] / source['sha256']


def source_index(course: Path) -> dict:
    data = read_json(course / '_工作区' / '资料索引.json')
    if data.get('schema_version') != 1:
        raise ValueError('Unsupported source index schema_version')
    return data
