"""Shared local-only file utilities for better-learning."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path


_validation = ContextVar('validation', default=None)


def fingerprint(path):
    st = Path(path).stat()
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


class ValidationContext:
    """Command-local evidence cache. Never persists trust between commands."""
    def __init__(self):
        self.file_hash_cache = {}
        self.transcripts = {}
        self.json_files = {}
        self.transcripts_checked = set()

    def hash(self, path):
        path = Path(path).resolve()
        stamp = fingerprint(path)
        cached = self.file_hash_cache.get(path)
        if cached and cached[0] == stamp:
            return cached[1]
        value = _sha256(path)
        if fingerprint(path) != stamp:
            raise ValueError('File changed during validation: ' + str(path))
        self.file_hash_cache[path] = (stamp, value)
        return value

    def invalidate(self, path):
        self.file_hash_cache.pop(Path(path).resolve(), None)
        self.json_files.pop(Path(path).resolve(), None)
        self.transcripts.clear()
        self.transcripts_checked.clear()


def validation_context():
    return _validation.get()


def validation_run(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        if _validation.get() is not None:
            return fn(*args, **kwargs)
        token = _validation.set(ValidationContext())
        try:
            return fn(*args, **kwargs)
        finally:
            _validation.reset(token)
    return run


def sha256(path: Path) -> str:
    ctx = validation_context()
    return ctx.hash(path) if ctx else _sha256(path)


def _sha256(path: Path) -> str:
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
        if validation_context():
            validation_context().invalidate(path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_json(path: Path, value) -> None:
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def read_json(path: Path):
    path = Path(path).resolve()
    ctx = validation_context()
    stamp = fingerprint(path)
    cached = ctx.json_files.get(path) if ctx else None
    if cached and cached[0] == stamp:
        return cached[1]
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if ctx:
        ctx.json_files[path] = (stamp, value)
    return value


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def course_lock(course: Path, name: str = '课程.lock'):
    """Course-wide write lock; separate from the transcription scheduler lock."""
    folder = Path(course) / '_工作区'
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / name
    try:
        handle = lock.open('x', encoding='utf-8')
    except FileExistsError as exc:
        raise ValueError('课程正在被其他命令写入；确认无调度命令运行后才可移除过期锁') from exc
    try:
        with handle:
            handle.write(utc_now())
        yield
    finally:
        lock.unlink(missing_ok=True)


class Transaction:
    """Journaled multi-file commit: per-file atomic replace is not a transaction.

    Originals are copied to a per-transaction backup folder before the first
    write, so an interrupted commit can be restored instead of guessed at.
    """

    def __init__(self, course: Path, journal: Path):
        self.course = Path(course)
        self.journal = Path(journal)
        self.staged = []
        self.receipt = {'schema_version': 1, 'kind': 'file-transaction', 'at': utc_now(),
                        'files': [], 'status': 'staged'}

    def write(self, path: Path, text: str):
        self.staged.append((Path(path), text))

    def _backup_dir(self):
        payload = json.dumps([[str(path) for path, _ in self.staged]], ensure_ascii=False)
        name = hashlib.sha256((payload + utc_now()).encode()).hexdigest()[:12]
        folder = self.journal.parent / '事务备份' / name
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def commit(self):
        originals = {path: (path.read_text(encoding='utf-8') if path.exists() else None)
                     for path, _ in self.staged}
        backup = self._backup_dir()
        for index, (path, _) in enumerate(self.staged):
            self.receipt['files'].append({
                'index': index, 'path': str(path),
                'before': None if originals[path] is None
                else hashlib.sha256(originals[path].encode('utf-8')).hexdigest(),
                'backup': str(backup / f'{index}.bak')})
        for index, (path, _) in enumerate(self.staged):
            if originals[path] is not None:
                atomic_text(backup / f'{index}.bak', originals[path])
        self._append(self.receipt)
        try:
            for path, text in self.staged:
                atomic_text(path, text)
        except OSError:
            self._restore(self.receipt)
            self.receipt['status'] = 'rolled-back'
            self._append(self.receipt)
            raise
        self.receipt['status'] = 'committed'
        self.receipt['after'] = [{'path': str(path),
                                  'hash': hashlib.sha256(text.encode('utf-8')).hexdigest()}
                                 for path, text in self.staged]
        self._append(self.receipt)
        return self.receipt

    def _append(self, record):
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        with self.journal.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    @staticmethod
    def _restore(record):
        for item in reversed(record.get('files', [])):
            path = Path(item['path'])
            if item.get('before') is None:
                path.unlink(missing_ok=True)
                continue
            backup = Path(item['backup'])
            if backup.exists():
                atomic_text(path, backup.read_text(encoding='utf-8'))
            else:
                raise ValueError('缺少事务备份，无法自动恢复：' + str(path))

    @staticmethod
    def recover(course: Path, journal: Path):
        """Restore files from interrupted 'staged' receipts; never guess content."""
        journal = Path(journal)
        if not journal.exists():
            return {'recovered': []}
        records = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines() if line.strip()]
        restored = []
        for record in records:
            if record.get('kind') != 'file-transaction' or record.get('status') != 'staged':
                continue
            Transaction._restore(record)
            record['status'] = 'rolled-back'
            journal.parent.mkdir(parents=True, exist_ok=True)
            with journal.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            restored.append(record['at'])
        return {'recovered': restored}
