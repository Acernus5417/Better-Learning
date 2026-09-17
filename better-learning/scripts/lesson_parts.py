"""Checkpointed lesson sections. Workers write pieces; the collector merges them."""
from collections import Counter
from pathlib import Path
import re

from _common import atomic_text, inside, read_json, sha256, write_json
from obsidian_links import block_ids, mask_code, validate_document

SYSTEM_ANCHOR = re.compile(r'^\s*[A-Za-z][A-Za-z0-9-]*\s+\^[A-Za-z0-9-]+\s*$', re.M)
SYSTEM_BLOCK = re.compile(r'(?:^|\s)\^[A-Za-z0-9-]+\s*$', re.M)


def check_math_boundaries(body):
    text = mask_code(body)
    if len(re.findall(r'(?<!\\)\$\$', text)) % 2:
        raise ValueError('Unclosed display formula in chunk')
    stack = []
    for m in re.finditer(r'(?<!\\)\\([\[\]()])', text):
        token = m[1]
        if token in '[(': stack.append(token)
        elif not stack or stack.pop() != {']': '[', ')': '('}[token]:
            raise ValueError('Mismatched math delimiter in chunk')
    if stack: raise ValueError('Unclosed math delimiter in chunk')
    environments = []
    for m in re.finditer(r'(?<!\\)\\(begin|end)\{([A-Za-z*]+)\}', text):
        if m[1] == 'begin': environments.append(m[2])
        elif not environments or environments.pop() != m[2]:
            raise ValueError('Mismatched LaTeX environment in chunk')
    if environments: raise ValueError('Unclosed LaTeX environment in chunk')


def plan(lesson):
    rows = [('intro', '章首导学、目标、前置自测、术语', [])]
    rows += [('knowledge', '知识小节 ' + k, [k]) for k in lesson['knowledge_ids']]
    rows += [('closing', '章末应用、速查、练习、答案、复习、来源', [])]
    return [dict(id=f'S{i:03d}', kind=kind, title=title, knowledge_ids=kids)
            for i, (kind, title, kids) in enumerate(rows, 1)]


def provision(course, folder, lesson, previous=None):
    """Copy only collector-verified chunks into an isolated new attempt."""
    sections, protected, resume = [], {}, []
    progress = (previous or {}).get('part_progress', {})
    for section in plan(lesson):
        sid = section['id']
        directory = folder + '/parts/' + sid
        receipt = directory + '/section.json'
        saved = progress.get(sid)
        files = []
        if saved:
            # A later edit to stopped-worker output must never become trusted input.
            if any(sha256(inside(course, f['path'])) != f['hash'] for f in saved['files']):
                raise ValueError('Saved lesson chunk changed: ' + sid)
            for f in saved['files']:
                target = directory + '/' + Path(f['path']).name
                atomic_text(inside(course, target), inside(course, f['path']).read_text(encoding='utf-8'))
                protected[target] = sha256(inside(course, target))
                files.append(Path(target).name)
        status = saved['status'] if saved else 'partial'
        write_json(inside(course, receipt), {'schema_version': 1, 'section_id': sid,
                   'status': status, 'files': files})
        item = dict(section, directory=directory, receipt=receipt,
                    completed_files=files, status=status, next_chunk=f'{len(files) + 1:03d}.md')
        sections.append(item)
        if saved:
            text = '\n\n'.join(inside(course, f['path']).read_text(encoding='utf-8') for f in saved['files'])
            resume.append({'section': sid, 'status': status, 'files': files,
                           'used_blocks': block_ids(text),
                           'headings': re.findall(r'^#{1,6} .+$', text, re.M),
                           'tail': text[-300:] if status != 'complete' else ''})
    return sections, protected, resume


def inspect(course, attempt, lesson):
    """No final result is needed to salvage individually acknowledged chunks."""
    progress, errors, texts = {}, [], {}
    for section in attempt['sections']:
        sid = section['id']
        try:
            receipt = read_json(inside(course, section['receipt']))
            if (receipt.get('schema_version') != 1 or receipt.get('section_id') != sid
                    or receipt.get('status') not in {'partial', 'complete'}):
                raise ValueError('Invalid section receipt')
            names = receipt.get('files')
            if not isinstance(names, list) or names != [f'{i:03d}.md' for i in range(1, len(names) + 1)]:
                raise ValueError('Chunks must be unique and contiguous from 001.md')
            previous = section.get('completed_files', [])
            if names[:len(previous)] != previous or (section.get('status') == 'complete' and
                    (receipt['status'] != 'complete' or names != previous)):
                raise ValueError('Cannot discard or extend a completed checkpoint')
            if not names:
                if receipt['status'] == 'complete': raise ValueError('Empty complete section')
                continue
            files, bodies = [], []
            for name in names:
                path = section['directory'] + '/' + name
                body = inside(course, path).read_text(encoding='utf-8')
                if not body.strip() or '<!-- BL-' in body: raise ValueError('Empty/reserved chunk')
                # Chunks end at natural Markdown boundaries, not halfway through a code block.
                fence = None
                for line in body.splitlines():
                    m = re.match(r'^\s*(`{3,}|~{3,})', line)
                    if m:
                        if fence is None: fence = m[1]
                        elif m[1][0] == fence[0] and len(m[1]) >= len(fence): fence = None
                if fence: raise ValueError('Unclosed fenced block in chunk')
                check_math_boundaries(body)
                files.append({'path': path, 'hash': sha256(inside(course, path))})
                bodies.append(body)
            text = '\n\n'.join(bodies)
            v2 = not attempt.get('agent_may_write_system_regions', True)
            if SYSTEM_ANCHOR.search(mask_code(text)) or (v2 and SYSTEM_BLOCK.search(mask_code(text))):
                raise ValueError('RESERVED_SYSTEM_MARKUP: 成员片段不得写入系统锚点、块 ID 或边界标记')
            own_blocks = [] if v2 else (section['knowledge_ids'] +
                                        [b for b in attempt['allowed_blocks'] if b.startswith('EX-')])
            issues = validate_document(course, text, attempt['allowed_links'], own_blocks, require_targets=False)
            if issues: raise ValueError('; '.join(issues))
            if section['kind'] == 'intro':
                from lesson_tasks import validate_frontmatter
                validate_frontmatter(text, lesson)
            elif text.startswith('---\n'):
                raise ValueError('Only intro may contain frontmatter')
            if receipt['status'] == 'complete' and section['knowledge_ids']:
                if not re.search(r'^#{1,6}\s+\S', text, re.M):
                    raise ValueError('Knowledge section incomplete (missing heading): '
                                     + ', '.join(section['knowledge_ids']))
                if not v2:
                    # legacy workers wrote the trailing ^K anchor themselves
                    for kid in section['knowledge_ids']:
                        if block_ids(text).count(kid) != 1:
                            raise ValueError('Knowledge section incomplete: ' + kid)
            progress[sid] = {'status': receipt['status'], 'files': files}
            texts[sid] = text
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(sid + ': ' + str(exc))
    counts = Counter(b for text in texts.values() for b in block_ids(text))
    duplicates = {b for b, count in counts.items() if count > 1}
    for sid, text in list(texts.items()):
        if duplicates.intersection(block_ids(text)):
            errors.append(sid + ': Duplicate cross-section block ID')
            progress.pop(sid, None); texts.pop(sid, None)
    return progress, errors, texts


def collect_parts(course, attempt, lesson):
    for path, expected in attempt.get('protected_chunks', {}).items():
        if sha256(inside(course, path)) != expected:
            raise ValueError('Reused lesson chunk changed: ' + path)
    progress, errors, texts = inspect(course, attempt, lesson)
    attempt['part_progress'] = progress
    attempt['part_errors'] = errors
    missing = [s['id'] for s in attempt['sections'] if progress.get(s['id'], {}).get('status') != 'complete']
    attempt['missing_sections'] = missing
    result_path = inside(course, attempt['result'])
    result = read_json(result_path) if result_path.exists() else {'status': 'partial', 'reason': 'No final result receipt'}
    if (result.get('status') not in {'complete', 'partial', 'failed'} or
            (result_path.exists() and (result.get('lesson_id') != lesson['id'] or
             sorted(result.get('knowledge_ids', [])) != sorted(lesson['knowledge_ids'])))):
        raise ValueError('Invalid/foreign lesson result')
    attempt['worker_result'] = result
    if errors: raise ValueError('; '.join(errors))
    if missing or result['status'] != 'complete': return False
    # Replace deterministically, never append. Repeated collect cannot duplicate content.
    atomic_text(inside(course, attempt['output']), '\n\n'.join(texts[s['id']] for s in attempt['sections']) + '\n')
    return True
