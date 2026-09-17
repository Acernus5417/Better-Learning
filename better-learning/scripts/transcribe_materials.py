"""Maintain one Markdown transcript per source; persist each recognized page immediately."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from _common import validation_run, validation_context, fingerprint, atomic_text, extraction_path, inside, read_json, sha256, source_index, write_json

VISUAL = {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'gif'}
BLOCK = re.compile(r'<!-- BL-PAGE (U\d+) (complete|unresolved) (visual|text|vision) ([a-f0-9]{64}) -->\n(.*?)\n<!-- BL-END \1 -->\n', re.S)
V2_UNIT = re.compile(r'<!-- BL-SRC:BEGIN ([A-Za-z0-9-]+) -->\n(.*?)\n<!-- BL-SRC:END \1 -->\n', re.S)
V2_TEXT = re.compile(r'<!-- BL-SOURCE-TEXT:BEGIN ([A-Za-z0-9-]+) -->\n(.*?)\n<!-- BL-SOURCE-TEXT:END \1 -->\n', re.S)


def initialize(course):
    course = course.resolve()
    if (course / '_工作区/转写任务.json').exists():
        raise ValueError('宿主模式由 convert_materials.py assemble 统一合成，不能单独初始化旧转写')
    entries = []
    for source in source_index(course)['sources']:
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', Path(source['name']).stem)[:40].rstrip('. ')
        relative = f"资料转写/{source['id']}-{stem}-{source['sha256'][:12]}-vision-v1.md"
        path = inside(course, relative)
        if not path.exists():
            atomic_text(path, f"# {source['name']}：资料转写\n\n来源：{source['id']}\n\n源版本：{source['sha256']}\n\n")
        entries.append({'source_id': source['id'], 'source_hash': source['sha256'], 'path': relative})
    write_json(course / '_工作区/转写索引.json', {'schema_version': 1, 'sources': entries})
    return {'files': len(entries), 'index': '_工作区/转写索引.json'}


def context(course, sid):
    ctx = validation_context()
    key = (str(course.resolve()), sid)
    if ctx and key in ctx.transcripts:
        result, stamps = ctx.transcripts[key]
        if all(fingerprint(p) == stamp for p, stamp in stamps.items()):
            return result
        del ctx.transcripts[key]
        ctx.transcripts_checked.clear()
    source = next((s for s in source_index(course)['sources'] if s['id'] == sid), None)
    if not source or sha256(Path(source['path'])) != source['sha256']:
        raise ValueError('Source missing or changed; inventory and initialize current transcripts again')
    entry = next((s for s in read_json(course / '_工作区/转写索引.json')['sources'] if s['source_id'] == sid), None)
    if not entry or entry['source_hash'] != source['sha256']:
        raise ValueError('Transcript index missing or stale')
    mapping = read_json(extraction_path(course, source) / '定位映射.json')
    if mapping['source_hash'] != source['sha256']:
        raise ValueError('Extraction mapping stale')
    path = inside(course, entry['path'])
    text = path.read_text(encoding='utf-8')
    if entry.get('shell') == 'v2':
        records, units = {}, {f"{entry['source_id']}-{u['id']}": u for u in mapping['units']}
        payloads = {item['unit_id']: item for item in entry.get('payloads', [])}
        for match in V2_UNIT.finditer(text):
            uid, block = match[1], match[2]
            unit = units.get(uid)
            if unit is None:
                raise ValueError('Unknown source unit shell: ' + uid)
            payload_match = V2_TEXT.search(block)
            if not payload_match or payload_match[1] != uid:
                raise ValueError('Malformed source payload: ' + uid)
            body = payload_match[2]
            payload = payloads.get(unit['id']) or {}
            if payload.get('hash') and hashlib.sha256(body.encode('utf-8')).hexdigest() != payload['hash']:
                raise ValueError('Source payload changed: ' + unit['id'])
            if unit['id'] in records:
                raise ValueError('Duplicate source unit: ' + unit['id'])
            records[unit['id']] = {'state': 'complete', 'mode': payload.get('mode', 'text'),
                                   'body': body, 'span': match.span()}
        if text.count('<!-- BL-SRC:BEGIN ') != len(records):
            raise ValueError('Malformed source unit marker')
        expected = [u['id'] for u in mapping['units']]
        if list(records) != expected[:len(records)]:
            raise ValueError('Source unit order differs from mapping')
        result = (source, entry, mapping, path, text, records)
        if ctx:
            dependencies = [Path(source['path']), path, extraction_path(course, source) / '定位映射.json',
                            course / '_工作区/资料索引.json', course / '_工作区/转写索引.json']
            ctx.transcripts[key] = (result, {p: fingerprint(p) for p in dependencies})
        return result
    records = {}
    for match in BLOCK.finditer(text):
        uid, state, mode, digest, body = match.groups()
        if uid in records or hashlib.sha256(body.encode('utf-8')).hexdigest() != digest:
            raise ValueError('Duplicate or modified transcript page: ' + uid)
        records[uid] = {'state': state, 'mode': mode, 'body': body, 'span': match.span()}
    if text.count('<!-- BL-PAGE ') != len(records):
        raise ValueError('Malformed transcript page marker')
    expected = [u['id'] for u in mapping['units']]
    if list(records) != expected[:len(records)]:
        raise ValueError('Transcript page order differs from source mapping')
    result = (source, entry, mapping, path, text, records)
    if ctx:
        dependencies = [Path(source['path']), path, extraction_path(course, source) / '定位映射.json',
                        course / '_工作区/资料索引.json', course / '_工作区/转写索引.json']
        ctx.transcripts[key] = (result, {p: fingerprint(p) for p in dependencies})
    return result


@validation_run
def status(course, sid, verify=False):
    source, entry, mapping, _, _, records = context(course, sid)
    pending = [u['id'] for u in mapping['units'] if u['id'] not in records]
    unresolved = [uid for uid, r in records.items() if r['state'] != 'complete']
    if source['format'] in VISUAL:
        unresolved.extend(uid for uid, r in records.items() if r['mode'] not in {'visual', 'vision'} and uid not in unresolved)
    unit_index = {u['id']: u for u in mapping['units']}
    for uid, result in records.items():
        if verify and result['mode'] == 'vision':
            unit = unit_index[uid]
            verify_vision(course, source, unit)
    return {'source': sid, 'transcript': entry['path'], 'total_pages': mapping['total_units'],
            'written_pages': len(records), 'next_page': pending[0] if pending else None,
            'unresolved': unresolved, 'complete': not pending and not unresolved and bool(records)}


def verify_vision(course, source, unit):
    if (course / '_工作区/转写任务.json').exists():
        from convert_materials import load_ledger, find, valid_unit
        task_source, task_unit = find(load_ledger(course), source['id'], uid=unit['id'])
        if not valid_unit(course, task_source, task_unit):
            raise ValueError('宿主成员转写尚未通过校验')
        return
    evidence = read_json(extraction_path(course, source) / unit['id'] / 'vision.json')
    if evidence.get('source_hash') != source['sha256'] or evidence.get('unit_id') != unit['id'] or evidence.get('engine') != 'vision':
        raise ValueError('Vision evidence missing or stale')
    for image in evidence.get('images', []):
        if sha256(inside(course, image['path'])) != image['sha256'] or sha256(inside(course, image['markdown'])) != image['markdown_hash']:
            raise ValueError('Vision input/output changed')
    if set(unit.get('images', [])) != {i['path'] for i in evidence.get('images', [])}:
        raise ValueError('Some embedded images lack Vision results')

def record(course, sid, uid, input_path, visual_checked=False, unresolved=False, replace=False, vision=False):
    if (course / '_工作区/转写任务.json').exists():
        raise ValueError('宿主模式请修订单元文件后 collect，再 assemble；不得直接改合成文件')
    source, entry, mapping, path, text, records = context(course, sid)
    unit = next((u for u in mapping['units'] if u['id'] == uid), None)
    if not unit or unit['status'] in {'pending', 'blocked'}:
        raise ValueError('Extract/read this source page before transcribing it')
    if source['format'] in VISUAL and not (visual_checked or vision):
        raise ValueError('PDF/image page requires actual visual reading and --visual-checked')
    if vision:
        verify_vision(course, source, unit)
    body = inside(course, input_path).read_text(encoding='utf-8').strip()
    if not body or '<!-- BL-' in body:
        raise ValueError('Transcript page is empty or contains reserved markers')
    page = f"## {uid} · {unit['locator']}\n\n{body}"
    state = 'unresolved' if unresolved else 'complete'
    mode = 'vision' if vision else ('visual' if visual_checked else 'text')
    digest = hashlib.sha256(page.encode('utf-8')).hexdigest()
    block = f'<!-- BL-PAGE {uid} {state} {mode} {digest} -->\n{page}\n<!-- BL-END {uid} -->\n'
    if uid in records:
        old = records[uid]
        if page == old['body'] and state == old['state'] and mode == old['mode']:
            return status(course, sid)
        if not replace:
            raise ValueError('Page already exists; review correction then use --replace')
        start, end = old['span']
        text = text[:start] + block + text[end:]
    else:
        next_page = status(course, sid)['next_page']
        if uid != next_page:
            raise ValueError('Transcribe pages in order; next page is ' + str(next_page))
        text += '\n' + block
    # Update the same per-source Markdown immediately. No delayed full-file synthesis.
    atomic_text(path, text)
    return status(course, sid)


@validation_run
def check_all(course):
    errors = []
    if (course / '_工作区/转写任务.json').exists():
        from convert_materials import check
        try:
            errors.extend(check(course))
        except (OSError, ValueError, KeyError) as exc:
            errors.append(str(exc))
    sources = source_index(course)['sources']
    if not sources:
        return ['没有已盘点的输入文件']
    for source in sources:
        try:
            current = status(course, source['id'], verify=True)
            if not current['complete']:
                errors.append(f"{source['id']}: 转写未完成，下一页 {current['next_page']}，未解决 {current['unresolved']}")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"{source['id']}: 转写不可用：{exc}")
    if validation_context():
        validation_context().transcripts_checked.discard(str(course.resolve()))
        if not errors:
            validation_context().transcripts_checked.add(str(course.resolve()))
    return errors


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--course', required=True, type=Path)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    sub.add_parser('check')
    s = sub.add_parser('status')
    s.add_argument('--source', required=True)
    r = sub.add_parser('record')
    r.add_argument('--source', required=True)
    r.add_argument('--unit', required=True)
    r.add_argument('--input', required=True)
    r.add_argument('--visual-checked', action='store_true')
    r.add_argument('--vision', action='store_true')
    r.add_argument('--unresolved', action='store_true')
    r.add_argument('--replace', action='store_true')
    args = p.parse_args()
    course = args.course.resolve()
    try:
        if args.command == 'init':
            result = initialize(course)
        elif args.command == 'record':
            result = record(course, args.source, args.unit, args.input, args.visual_checked, args.unresolved, args.replace, args.vision)
        elif args.command == 'status':
            result = status(course, args.source)
        else:
            errors = check_all(course)
            result = {'complete': not errors, 'errors': errors}
        print(json.dumps(result, ensure_ascii=False))
        return 1 if args.command == 'check' and result['errors'] else 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
