"""Prepare bounded image tiles and persist evidence for each visual reading step."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from _common import validation_run, validation_context, extraction_path, inside, read_json, rel, sha256, source_index, write_json


def boxes(width, height, tile_width=1280, tile_height=1600, overlap=128):
    if min(width, height) < 1 or min(tile_width, tile_height) < 256 or not 0 <= overlap < min(tile_width, tile_height):
        raise ValueError('Invalid image dimensions/tile dimensions/overlap')
    def offsets(length, size):
        result = [0]
        while result[-1] + size < length:
            result.append(result[-1] + size - overlap)
        return result
    return [(x, y, min(x + tile_width, width), min(y + tile_height, height))
            for y in offsets(height, tile_height) for x in offsets(width, tile_width)]


@validation_run
def check_manifest(course: Path, data: dict, require_read=False) -> list[str]:
    errors = []
    source = next((s for s in source_index(course)['sources'] if s['id'] == data['source_id']), None)
    if not source or source['sha256'] != data['source_hash'] or sha256(Path(source['path'])) != data['source_hash']:
        errors.append('视觉阅读来源版本已变更')
        return errors
    mapping = read_json(extraction_path(course, source) / '定位映射.json')
    unit = next((u for u in mapping['units'] if u['id'] == data['unit_id']), None)
    if not unit or data['image'] not in unit.get('images', []):
        errors.append('视觉图像不是当前源单元的图像')
    if sha256(inside(course, data['image'])) != data['image_hash']:
        errors.append('视觉图像已变更，需要重新准备')
    if data.get('overview_hash') and sha256(inside(course, data['overview'])) != data['overview_hash']:
        errors.append('视觉概览已变更')
    expected = boxes(*data['dimensions'], **data['config'])
    if [t['box'] for t in data['tiles']] != [list(b) for b in expected]:
        errors.append('视觉分块未完整覆盖页面')
    for tile in data['tiles']:
        if sha256(inside(course, tile['path'])) != tile['sha256']:
            errors.append('视觉分块文件已变更：' + tile['id'])
        if require_read and tile['status'] != 'read':
            errors.append('视觉分块尚未读完或无法辨识：' + tile['id'])
        if tile['status'] == 'read':
            note = inside(course, tile['note'])
            if not note.read_text(encoding='utf-8').strip() or sha256(note) != tile['note_hash']:
                errors.append('阅读笔记缺失或已变更：' + tile['id'])
    return errors


def summary(course, path, data):
    pending = [t for t in data['tiles'] if t['status'] == 'pending']
    unresolved = [t['id'] for t in data['tiles'] if t['status'] == 'unreadable']
    return {'manifest': rel(course, path), 'overview': data['overview'], 'tiles': len(data['tiles']),
            'read': sum(t['status'] == 'read' for t in data['tiles']), 'unreadable': unresolved,
            'next': {k: pending[0][k] for k in ['id', 'path', 'box']} if pending else None,
            'ready_for_synthesis': not pending and not unresolved}


def prepare(course, sid, uid, tile_width=1280, tile_height=1600, overlap=128, image_index=1):
    from PIL import Image, ImageOps
    course = course.resolve()
    source = next((s for s in source_index(course)['sources'] if s['id'] == sid), None)
    if not source:
        raise ValueError('Unknown source')
    if sha256(Path(source['path'])) != source['sha256']:
        raise ValueError('Source changed; inventory again')
    mapping = read_json(extraction_path(course, source) / '定位映射.json')
    unit = next((u for u in mapping['units'] if u['id'] == uid), None)
    if not unit or not 1 <= image_index <= len(unit.get('images', [])):
        raise ValueError('Extract this unit to an image before preparing visual reading')
    image_rel = unit['images'][image_index - 1]
    original = inside(course, image_rel)
    config = dict(tile_width=tile_width, tile_height=tile_height, overlap=overlap)
    digest = sha256(original)
    version = hashlib.sha256((digest + json.dumps(config, sort_keys=True)).encode()).hexdigest()[:16]
    directory = extraction_path(course, source) / uid / 'visual' / version
    manifest = directory / 'visual-reading.json'
    if manifest.exists():
        data = read_json(manifest)
        errors = check_manifest(course, data)
        if errors:
            raise ValueError('; '.join(errors))
        return summary(course, manifest, data)
    directory.mkdir(parents=True, exist_ok=True)
    with Image.open(original) as image:
        image = ImageOps.exif_transpose(image).convert('RGB')
        regions = boxes(*image.size, **config)
        overview = image.copy()
        overview.thumbnail((1200, 1200))
        overview_path = directory / 'overview.jpg'
        overview.save(overview_path, quality=85)
        data = {'schema_version': 1, 'source_id': sid, 'source_hash': source['sha256'],
                'unit_id': uid, 'image': image_rel, 'image_hash': digest,
                'dimensions': list(image.size), 'overview': rel(course, overview_path), 'overview_hash': sha256(overview_path),
                'config': config, 'tiles': []}
        for number, box in enumerate(regions, 1):
            target = directory / f'V{number:04d}.png'
            image.crop(box).save(target)
            data['tiles'].append({'id': f'V{number:04d}', 'box': list(box),
                                 'path': rel(course, target), 'sha256': sha256(target), 'status': 'pending'})
    write_json(manifest, data)
    return summary(course, manifest, data)


def record(course, manifest, tile_id, note, status='read'):
    path = inside(course, manifest)
    data = read_json(path)
    # Permit correcting a note by recording it again; verify other records as usual.
    tile = next((t for t in data['tiles'] if t['id'] == tile_id), None)
    if not tile or status not in {'read', 'unreadable'}:
        raise ValueError('Unknown tile or status')
    old_status = tile['status']
    tile['status'] = 'pending'
    errors = check_manifest(course, data)
    tile['status'] = old_status
    if errors:
        raise ValueError('; '.join(errors))
    note_path = inside(course, note)
    if not note_path.read_text(encoding='utf-8').strip():
        raise ValueError('Reading note cannot be empty')
    tile.update(status=status, note=note, note_hash=sha256(note_path))
    write_json(path, data)
    return summary(course, path, data)


@validation_run
def strict_assets(course, sid, uid):
    source = next(s for s in source_index(course)['sources'] if s['id'] == sid)
    unit = next(u for u in read_json(extraction_path(course, source) / '定位映射.json')['units'] if u['id'] == uid)
    manifests, overviews, tiles, hashes = [], [], [], {}
    for i in range(1, len(unit['images']) + 1):
        result = prepare(course, sid, uid, image_index=i)
        data = read_json(inside(course, result['manifest']))
        manifests.append(result['manifest'])
        overviews.append(str(inside(course, data['overview'])))
        for tile in data['tiles']:
            tiles.append({'id': f'I{i}-{tile["id"]}', 'path': str(inside(course, tile['path'])), 'box': tile['box']})
            hashes[tile['path']] = sha256(inside(course, tile['path']))
        hashes[data['overview']] = sha256(inside(course, data['overview']))
    return {'manifests': manifests, 'overviews': overviews, 'tiles': tiles, 'hashes': hashes}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--course', type=Path, required=True)
    sub = p.add_subparsers(dest='command', required=True)
    init = sub.add_parser('prepare')
    init.add_argument('--source', required=True)
    init.add_argument('--unit', required=True)
    init.add_argument('--image-index', type=int, default=1)
    init.add_argument('--tile-width', type=int, default=1280)
    init.add_argument('--tile-height', type=int, default=1600)
    init.add_argument('--overlap', type=int, default=128)
    nxt = sub.add_parser('next')
    nxt.add_argument('--manifest', required=True)
    rec = sub.add_parser('record')
    rec.add_argument('--manifest', required=True)
    rec.add_argument('--tile', required=True)
    rec.add_argument('--note', required=True)
    rec.add_argument('--status', choices=['read', 'unreadable'], default='read')
    args = p.parse_args()
    course = args.course.resolve()
    try:
        if args.command == 'prepare':
            result = prepare(course, args.source, args.unit, args.tile_width, args.tile_height, args.overlap, args.image_index)
        elif args.command == 'record':
            result = record(course, args.manifest, args.tile, args.note, args.status)
        else:
            path = inside(course, args.manifest)
            data = read_json(path)
            errors = check_manifest(course, data)
            if errors:
                raise ValueError('; '.join(errors))
            result = summary(course, path, data)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, ImportError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
