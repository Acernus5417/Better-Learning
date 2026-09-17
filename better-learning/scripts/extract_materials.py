"""Extract bounded material units; never mark them as semantically read."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

from _common import validation_run, validation_context, atomic_text, extraction_path, read_json, rel, sha256, source_index, write_json


def tag(element):
    return element.tag.rsplit('}', 1)[-1]


def xml_text(data: bytes) -> str:
    def visit(node):
        name = tag(node)
        if name in {'t', 'instrText'}:
            return node.text or ''
        if name == 'tab':
            return '\t'
        if name in {'br', 'cr'}:
            return '\n'
        content = ''.join(visit(child) for child in node)
        if name == 'tc':
            return content.strip().replace('\n', ' / ') + '\t'
        if name == 'tr':
            return content.rstrip('\t') + '\n'
        if name == 'p':
            return content + '\n'
        return content
    return visit(ET.fromstring(data)).strip()


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag_name, attrs):
        if tag_name in {'script', 'style'}:
            self.hidden += 1
        if tag_name in {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr'}:
            self.parts.append('\n')
        if tag_name in {'td', 'th'}:
            self.parts.append('\t')
        if tag_name == 'img':
            self.parts.append('[图像 ' + str(dict(attrs).get('src', '')) + ']')

    def handle_endtag(self, tag_name):
        if tag_name in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(data: str) -> str:
    parser = PlainHTML()
    parser.feed(data)
    return ''.join(parser.parts)


def decode(data: bytes) -> str:
    # Strict decoding: corrupted bytes must not silently disappear.
    for encoding in ('utf-8-sig', 'utf-16' if data[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    raise ValueError('Text encoding is not recognized; provide a UTF-8 export')


def chunks(text: str, limit: int) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or ['']


def relationships(z: zipfile.ZipFile, part: str) -> dict:
    name = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
    if name not in z.namelist():
        return {}
    result = {}
    for item in ET.fromstring(z.read(name)):
        if item.attrib.get('TargetMode') == 'External':
            continue
        target = unquote(item.attrib.get('Target', ''))
        resolved = target.lstrip('/') if target.startswith('/') else posixpath.normpath(
            posixpath.join(posixpath.dirname(part), target))
        result[item.attrib['Id']] = (resolved, item.attrib.get('Type', ''))
    return result


def media(z: zipfile.ZipFile, names: list[str], folder: Path, course: Path) -> list[str]:
    result = []
    for name in sorted(set(names)):
        if name not in z.namelist():
            continue
        # Generate our own file names; never extract archive paths directly.
        suffix = Path(name).suffix.lower()
        output = folder / (hashlib.sha256(name.encode()).hexdigest()[:16] + suffix)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(z.read(name))
        result.append(rel(course, output))
    return result


REL_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
LAYOUT_VERSION = 2


EXTRACTOR_VERSION = 3


def mixed_part(z, part, locator, limit):
    """Walk XML in document/shape order; emit images at their actual reference."""
    rs = relationships(z, part)
    result, pending = [], ''
    pending_locator = locator
    counters = {'paragraph': 0, 'shape': 0}

    def flush():
        nonlocal pending, pending_locator
        for content in chunks(pending.strip(), limit):
            if content:
                result.append({'kind': 'text', 'text': content, 'part': part,
                               'locator': pending_locator, 'media': []})
        pending = ''
        pending_locator = locator

    def walk(node, position=''):
        nonlocal pending, pending_locator
        name = tag(node)
        if name == 'p':
            counters['paragraph'] += 1
            position += f'; paragraph {counters["paragraph"]}'
        if part.startswith('ppt/') and name in {'sp', 'pic', 'graphicFrame', 'cxnSp', 'grpSp'}:
            counters['shape'] += 1
            position += f'; shape {counters["shape"]}'
        if name in {'blip', 'imagedata'}:
            flush()
            rid = node.attrib.get(REL_NS + 'embed') or node.attrib.get(REL_NS + 'id')
            target = rs.get(rid, ('', ''))[0]
            result.append({'kind': 'image', 'text': '', 'part': part,
                           'locator': locator + position + f'; image {rid or "unresolved"}',
                           'media': [target] if target and target in z.namelist() else [],
                           'unresolved_media': not target or target not in z.namelist()})
            return
        if name in {'t', 'instrText'}:
            if not pending.strip():
                pending_locator = locator + position
            pending += node.text or ''
            return
        if name == 'tab':
            pending += '\t'
        elif name in {'br', 'cr'}:
            pending += '\n'
        for child in node:
            walk(child, position)
        if name in {'p', 'tr'}:
            pending += '\n'
        elif name == 'tc':
            pending = pending.rstrip('\n') + '\t'
        # Bound text buffers and give each slide shape an explicit boundary.
        if len(pending) >= limit or name in {'sp', 'graphicFrame'}:
            flush()

    root = ET.fromstring(z.read(part))
    visual_tags = {'chart', 'relIds', 'diagram', 'cxnSp', 'grpSp', 'prstGeom', 'custGeom',
                   'anchor', 'pict', 'oMath', 'oMathPara', 'graphicData',
                   'shape', 'group', 'line', 'rect', 'object', 'AlternateContent', 'svgBlip'}
    features = sorted({tag(n) for n in root.iter()} & visual_tags)
    walk(root)
    flush()
    if features:
        result.append({'kind': 'image', 'text': '', 'part': part,
                       'locator': locator + '; full visual coverage: ' + ','.join(features),
                       'media': [], 'visual_features': features, 'render_required': True})
    return result or [{'kind': 'text', 'text': '', 'part': part,
                       'locator': locator + '; empty', 'media': []}]


def normalize_embedded_images(paths, course):
    """Only deliver independently decodable, single-frame PNG assets to vision."""
    from PIL import Image, ImageOps
    normalized = []
    for relative in paths:
        source = course / relative
        if source.suffix.lower() in {'.svg', '.emf', '.wmf'}:
            raise ValueError(f'嵌入矢量图片暂不能可靠解码，请渲染为 PNG 后处理：{relative}')
        with Image.open(source) as original:
            if getattr(original, 'n_frames', 1) != 1:
                raise ValueError(f'嵌入图片包含多个帧，需逐帧拆分后处理，禁止只识别第一帧：{relative}')
            raster = ImageOps.exif_transpose(original).convert('RGBA')
            background = Image.new('RGBA', raster.size, 'white')
            background.alpha_composite(raster)
            output = source.with_name(source.stem + '-normalized.png')
            background.convert('RGB').save(output)
            normalized.append(rel(course, output))
    return normalized




class Reader:
    def __init__(self, source: dict, folder: Path, course: Path, args):
        self.source, self.folder, self.course, self.args = source, folder, course, args
        self.path = Path(source['path'])
        self.fmt = source['format']
        self.z = None
        self.pdf = None
        self.pdf_render = None
        self.parts = []
        self.mixed_units = []
        self.texts = []
        self.error = None
        self.media_names = []
        self.render_error = None
        self.text_error = None
        try:
            if self.fmt == 'pdf':
                try:
                    from pypdf import PdfReader
                    candidate = PdfReader(str(self.path))
                    if candidate.is_encrypted and not candidate.decrypt(''):
                        raise ValueError('PDF 需要密码或未加密导出版本')
                    text_page_count = len(candidate.pages)
                    self.pdf = candidate
                except Exception as exc:
                    self.text_error = str(exc)
                try:
                    import pypdfium2
                    self.pdf_render = pypdfium2.PdfDocument(str(self.path))
                except Exception as exc:
                    self.render_error = str(exc)
                if self.pdf is None and self.pdf_render is None:
                    raise ValueError(f'PDF 文本及渲染均不可用：{self.text_error}; {self.render_error}')
                self.total = text_page_count if self.pdf is not None else len(self.pdf_render)
            elif self.fmt in {'pptx', 'epub', 'docx'}:
                self.z = zipfile.ZipFile(self.path)
                names = self.z.namelist()
                if self.fmt == 'pptx':
                    rs = relationships(self.z, 'ppt/presentation.xml')
                    root = ET.fromstring(self.z.read('ppt/presentation.xml'))
                    for item in root.iter():
                        if tag(item) == 'sldId':
                            rid = item.attrib.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')
                            if rid not in rs:
                                raise ValueError('PPTX slide relationship is missing')
                            self.parts.append(rs[rid][0])
                elif self.fmt == 'epub':
                    container = ET.fromstring(self.z.read('META-INF/container.xml'))
                    opf = next(n.attrib['full-path'] for n in container.iter() if tag(n) == 'rootfile')
                    root = ET.fromstring(self.z.read(opf))
                    base = posixpath.dirname(opf)
                    manifest = {n.attrib['id']: n.attrib for n in root.iter() if tag(n) == 'item'}
                    for node in root.iter():
                        if tag(node) == 'itemref':
                            item = manifest[node.attrib['idref']]
                            self.parts.append(posixpath.normpath(posixpath.join(base, unquote(item['href'].split('#')[0]))))
                    # Some books keep notes/appendices outside their primary reading spine.
                    for item in manifest.values():
                        if item.get('media-type') in {'application/xhtml+xml', 'text/html'}:
                            target = posixpath.normpath(posixpath.join(base, unquote(item['href'].split('#')[0])))
                            if target not in self.parts:
                                self.parts.append(target)
                    self.media_names = [posixpath.normpath(posixpath.join(base, unquote(n['href'])))
                                        for n in manifest.values() if n.get('media-type', '').startswith('image/')]
                else:
                    self.parts = [n for n in ['word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml'] if n in names]
                if self.fmt in {'pptx', 'docx'}:
                    for number, part in enumerate(self.parts, 1):
                        locator = (f'slide {number}; ' if self.fmt == 'pptx' else '') + part
                        self.mixed_units.extend(mixed_part(self.z, part, locator, args.max_chars))
                        if self.fmt == 'pptx':
                            for target, kind in relationships(self.z, part).values():
                                if kind.endswith('/notesSlide'):
                                    self.mixed_units.extend(mixed_part(self.z, target,
                                        f'slide {number}; notes; {target}', args.max_chars))
                    expanded = []
                    renders = getattr(args, 'office_renders', {}) or {}
                    for item in self.mixed_units:
                        pages = renders.get(source['id'], {}).get(item['part'])
                        if item.get('render_required') and isinstance(pages, list) and pages:
                            for page_number, page_path in enumerate(pages, 1):
                                expanded.append(dict(item, render_path=page_path,
                                    locator=item['locator'] + f'; rendered page {page_number}'))
                        else:
                            expanded.append(item)
                    self.mixed_units = expanded
                    self.total = len(self.mixed_units)
                else:
                    self.total = len(self.parts)
            elif self.fmt in {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'gif'}:
                from PIL import Image
                with Image.open(self.path) as image:
                    self.total = getattr(image, 'n_frames', 1)
            elif self.fmt in {'txt', 'md', 'markdown', 'csv', 'html', 'htm'}:
                text = decode(self.path.read_bytes())
                if self.fmt in {'html', 'htm'}:
                    text = html_text(text)
                self.texts = chunks(text, args.max_chars)
                self.total = len(self.texts)
            else:
                raise ValueError(f'暂不直接解析 .{self.fmt}；需要可读的转换版本或其他工具')
            if not self.total:
                raise ValueError('未找到内容单元，需检查材料')
        except Exception as exc:
            self.total = 1
            self.error = f'{type(exc).__name__}: {exc}'

    def close(self):
        if self.z:
            self.z.close()
        if self.pdf_render:
            self.pdf_render.close()

    def read(self, ordinal: int) -> dict:
        uid = f'U{ordinal:05d}'
        unit = {'id': uid, 'ordinal': ordinal, 'locator': f'unit {ordinal}',
                'kind': 'page' if self.fmt == 'pdf' else 'text', 'assets': [],
                'status': 'extracted', 'chunks': [], 'images': [],
                'visual_review_required': False, 'warnings': []}
        output = self.folder / uid
        output.mkdir(parents=True, exist_ok=True)
        text = ''
        try:
            if self.error:
                raise ValueError(self.error)
            if self.fmt == 'pdf':
                unit['locator'] = f'PDF physical page {ordinal}'
                unit['visual_review_required'] = True
                try:
                    text = (self.pdf.pages[ordinal - 1].extract_text() or '') if self.pdf is not None else ''
                    if self.text_error:
                        unit['warnings'].append('文字层不可用，使用视觉路径：' + self.text_error)
                except Exception as exc:
                    unit['warnings'].append('文字层读取失败：' + str(exc))
                unit['native_text_characters'] = len(text.strip())
                if self.pdf_render:
                    p = self.pdf_render[ordinal - 1]
                    bitmap = None
                    try:
                        width, height = p.get_size()
                        scale = min(self.args.scale, math.sqrt(12_000_000 / max(width * height, 1)))
                        bitmap = p.render(scale=scale)
                        unit['render_scale'] = scale
                        if scale < self.args.scale:
                            unit['warnings'].append('页面过大，渲染限制为 1200 万像素；小字需回原件放大局部复核')
                        image_path = output / 'page.png'
                        bitmap.to_pil().save(image_path)
                        unit['images'].append(rel(self.course, image_path))
                    finally:
                        if bitmap is not None:
                            bitmap.close()
                        p.close()

                else:
                    unit['warnings'].append('页面渲染不可用，需要其他工具看图：' + str(self.render_error))
            elif self.mixed_units:
                item = self.mixed_units[ordinal - 1]
                unit.update(kind=item['kind'], locator=item['locator'])
                raw_output = self.folder / 'raw' / (hashlib.sha256(item['part'].encode()).hexdigest()[:16] + '.xml')
                if not raw_output.exists():
                    raw_output.parent.mkdir(parents=True, exist_ok=True)
                    raw_output.write_bytes(self.z.read(item['part']))
                unit['raw'] = rel(self.course, raw_output)
                text = item['text']
                unit['original_images'] = media(self.z, item['media'], self.folder / 'media', self.course)
                unit['visual_review_required'] = item['kind'] == 'image'
                if item.get('render_required'):
                    unit['visual_features'] = item['visual_features']
                    unit['render_required'] = True
                    renders = getattr(self.args, 'office_renders', {}) or {}
                    render = item.get('render_path') or renders.get(self.source['id'], {}).get(item['part'])
                    if not render:
                        unit['unresolved_media'] = True
                        raise ValueError('原生图表/图形需完整页面视觉覆盖：导出该页 PNG，用 --office-renders 注册来源/part 映射；或将文档导出 PDF 后重新盘点。禁止仅用文字代替。')
                    from _common import inside
                    render_paths = render if isinstance(render, list) else [render]
                    unit['original_images'] = [rel(self.course, inside(self.course, r)) for r in render_paths]
                    unit['images'] = normalize_embedded_images(unit['original_images'], self.course)
                    unit['render_evidence'] = render
                if item.get('unresolved_media'):
                    unit['unresolved_media'] = True
                    raise ValueError('文档内图片关系缺失或为外部链接，需要归档该图片后重试')
                if item['kind'] == 'image' and not item.get('render_required'):
                    unit['unresolved_media'] = True
                    unit['images'] = normalize_embedded_images(unit['original_images'], self.course)
                    unit.pop('unresolved_media', None)
            elif self.z:
                part = self.parts[ordinal - 1]
                raw = self.z.read(part)
                unit['locator'] = (f'slide {ordinal}; ' if self.fmt == 'pptx' else '') + part
                unit['visual_review_required'] = True
                raw_output = output / ('source.xhtml' if self.fmt == 'epub' else 'source.xml')
                raw_output.write_bytes(raw)
                unit['raw'] = rel(self.course, raw_output)
                if self.fmt == 'epub':
                    text = html_text(decode(raw))
                    images = self.media_names
                else:
                    text = xml_text(raw)
                    images = list(self.media_names)
                    rs = relationships(self.z, part)
                    images.extend(target for target, kind in rs.values() if kind.endswith('/image'))
                    if self.fmt == 'pptx':
                        for target, kind in rs.values():
                            if kind.endswith('/notesSlide'):
                                notes = self.z.read(target)
                                text += '\n\n[幻灯片备注]\n' + xml_text(notes)
                                notes_file = output / 'notes.xml'
                                notes_file.write_bytes(notes)
                                unit['notes_raw'] = rel(self.course, notes_file)
                                images.extend(t for t, k in relationships(self.z, target).values() if k.endswith('/image'))
                unit['images'] = media(self.z, images, self.folder / 'media', self.course)
                unit['warnings'].append('已提取文字及媒体；布局、公式和关系仍需查看原始文档或渲染页')
                unit['mixed_order_unknown'] = True
                unit['warnings'].append('EPUB 混合图片顺序尚未细分，媒体列表不能代表正文插入顺序')
            elif self.texts:
                text = self.texts[ordinal - 1]
                unit['locator'] = f'text block {ordinal}; character offset {(ordinal - 1) * self.args.max_chars}'
                if self.fmt in {'html', 'htm'}:
                    unit['visual_review_required'] = True
                    unit['warnings'].append('HTML 图片及布局需通过原件补读；字符偏移指提取后的文本')
                if self.fmt in {'html', 'htm', 'md', 'markdown'}:
                    original_text = decode(self.path.read_bytes())
                    if re.search(r'<img\b|!\[', original_text, re.I):
                        unit['unresolved_media'] = True
                        unit['warnings'].append('外部/内嵌图片引用尚未归档识别：取得图片并纳入资料范围，或导出自包含 PDF 后使用 独立视觉请求 转写')
            else:
                unit['kind'] = 'image'
                unit['visual_review_required'] = True
                unit['locator'] = f'image frame {ordinal} of {self.total}'
                from PIL import Image, ImageOps
                image_path = output / 'image.png'
                with Image.open(self.path) as original:
                    original.seek(ordinal - 1)
                    normalized = ImageOps.exif_transpose(original.copy()).convert('RGBA')
                    background = Image.new('RGBA', normalized.size, 'white')
                    background.alpha_composite(normalized)
                    background.convert('RGB').save(image_path)
                unit['images'] = [rel(self.course, image_path)]
            if self.fmt == 'pdf' or (not self.z and not self.texts):
                unit['content_mode'] = 'visual_first'
                unit['warnings'].append('未执行本地 OCR；须完成独立视觉转写后才能整理知识')
                if unit['images']:
                    unit['reading_route'] = 'convert_materials.py → 独立视觉请求 → per-source Markdown'
            for number, content in enumerate(chunks(text, self.args.max_chars), 1):
                dest = output / f'text-{number:03d}.md'
                atomic_text(dest, content)
                unit['chunks'].append(rel(self.course, dest))
            if unit['visual_review_required'] or not text.strip():
                unit['status'] = 'needs_review'
        except Exception as exc:
            unit['status'] = 'blocked'
            unit['warnings'].append(f'{type(exc).__name__}: {exc}')
        unit['assets'] = list(unit['images'] if unit['kind'] in {'page', 'image'} else unit['chunks'])
        safe_text = (unit['kind'] == 'text' and bool(text.strip())
                     and not any(unit.get(k) for k in ('visual_review_required', 'unresolved_media', 'mixed_order_unknown'))
                     and self.fmt in {'txt', 'md', 'markdown', 'csv', 'docx', 'pptx'})
        unit['processing_route'] = ('blocked' if unit['status'] == 'blocked' or unit.get('unresolved_media')
                                    or (unit.get('visual_review_required') and not unit['images'])
                                    else 'deterministic_text' if safe_text else 'vision' if unit['images'] else 'blocked')
        if unit['processing_route'] == 'blocked' and not unit['warnings']:
            unit['warnings'].append('无法确定可靠文本或完整视觉路径，请提供可读的完整转换版本')
        unit['complexity_hints'] = [k for k in ('mixed_order_unknown', 'render_required') if unit.get(k)]
        if any('页面过大' in w for w in unit['warnings']):
            unit['complexity_hints'].append('large_page_downscaled')
        return unit


@validation_run
def extract(course: Path, sid: str, args) -> dict:
    course = course.resolve()
    source = next((s for s in source_index(course)['sources'] if s['id'] == sid), None)
    if not source:
        raise ValueError(f'Unknown source ID: {sid}')
    if sha256(Path(source['path'])) != source['sha256']:
        raise ValueError('Source changed since inventory; inventory the complete input set again')
    folder = extraction_path(course, source)
    map_file = folder / '定位映射.json'
    from importlib.metadata import version, PackageNotFoundError
    try:
        renderer = version('pypdfium2')
    except PackageNotFoundError:
        renderer = 'unavailable'
    renders = getattr(args, 'office_renders', {}) or {}
    from _common import inside
    config = {'max_chars': args.max_chars, 'scale': args.scale,
              'extractor_version': EXTRACTOR_VERSION, 'layout_version': LAYOUT_VERSION,
              'pixel_limit': 12_000_000, 'renderer_version': renderer,
              'office_renders': renders.get(sid, {}),
              'render_hashes': {r: sha256(inside(course, r)) for paths in renders.get(sid, {}).values() for r in (paths if isinstance(paths, list) else [paths])}}
    state = read_json(map_file) if map_file.exists() else None
    if state and state.get('layout_version') != LAYOUT_VERSION:
        backup = map_file.with_name('定位映射.legacy-' + sha256(map_file)[:12] + '.json')
        if not backup.exists():
            atomic_text(backup, map_file.read_text(encoding='utf-8'))
        state = None
    if state and (state.get('config') != config or state.get('source_hash') != source['sha256']):
        state = None
    if state and (len(state['units']) != state['total_units']
                  or [u['ordinal'] for u in state['units']] != list(range(1, state['total_units'] + 1))):
        state = None
    def cached(unit):
        assets = unit.get('artifact_hashes', {})
        return (unit.get('status') not in {'pending', 'blocked'} and bool(assets)
                and all(inside(course, a).is_file() and sha256(inside(course, a)) == h for a, h in assets.items()))
    if state and all(cached(u) for u in state['units']):
        visual_input = source['format'] in {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'gif'}
        end = min(args.end if args.end is not None else args.start + (0 if visual_input else 7), state['total_units'])
        if args.start < 1 or args.start > state['total_units'] or end < args.start:
            raise ValueError('Invalid cached extraction range')
        return {'source': sid, 'total_units': state['total_units'], 'processed': [],
                'reused': end - args.start + 1,
                'next_start': end + 1 if end < state['total_units'] else None, 'map': str(map_file)}
    reader = Reader(source, folder, course, args)
    try:
        if state and state['total_units'] != reader.total:
            state = None
        if state is None:
            state = {'schema_version': 1, 'layout_version': LAYOUT_VERSION, 'source_id': sid, 'source_hash': source['sha256'],
                     'config': config, 'total_units': reader.total,
                     'units': [{'id': f'U{i:05d}', 'ordinal': i, 'locator': f'unit {i}',
                                'kind': reader.mixed_units[i - 1]['kind'] if reader.mixed_units else ('page' if source['format'] == 'pdf' else ('image' if source['format'] in {'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'gif'} else 'text')),
                                'assets': [],
                                'status': 'pending', 'chunks': [], 'images': [],
                                'visual_review_required': False, 'warnings': []}
                               for i in range(1, reader.total + 1)]}
        visual_input = source['format'] in {'pdf', 'png', 'jpg', 'jpeg', 'webp', 'bmp', 'tif', 'tiff', 'gif'}
        end = min(args.end if args.end is not None else args.start + (0 if visual_input else 7), reader.total)
        if args.start < 1 or args.start > reader.total or end < args.start:
            raise ValueError(f'Invalid range; source has {reader.total} units')
        outcomes = []
        for ordinal in range(args.start, end + 1):
            existing = state['units'][ordinal - 1]
            if cached(existing):
                continue
            unit = reader.read(ordinal)
            artifacts = list(dict.fromkeys(unit['chunks'] + unit['images'] + ([unit['raw']] if unit.get('raw') else [])))
            unit['artifact_hashes'] = {a: sha256(inside(course, a)) for a in artifacts}
            unit['input_key'] = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
            state['units'][ordinal - 1] = unit
            outcomes.append({'unit': unit['id'], 'status': unit['status']})
            write_json(map_file, state)
        return {'source': sid, 'total_units': reader.total, 'processed': outcomes,
                'next_start': end + 1 if end < reader.total else None, 'map': str(map_file)}
    finally:
        reader.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    parser.add_argument('--source', required=True)
    parser.add_argument('--start', type=int, default=1)
    parser.add_argument('--end', type=int)
    parser.add_argument('--max-chars', type=int, default=10000)
    parser.add_argument('--scale', type=float, default=2)
    args = parser.parse_args()
    if args.start < 1 or args.max_chars < 256 or not 0.5 <= args.scale <= 4:
        parser.error('start >= 1, max-chars >= 256, and 0.5 <= scale <= 4 are required')
    try:
        result = extract(args.course, args.source, args)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if any(u['status'] == 'blocked' for u in result['processed']) else 0
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
