"""Structural parsing of BL-* entity sections.

Offsets are preserved so callers can build span-safe edits. Markers inside
fenced code, inline code or a trusted source payload never act as boundaries.
Missing, duplicated, crossed or unclosed boundaries are hard errors: there is
no fallback to "next heading" or "next block ID" guessing in v2.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re

MARKER = re.compile(
    r'^[ \t]*<!--[ \t]*BL-([A-Z][A-Z-]*):(BEGIN|END)[ \t]+([A-Za-z0-9-]+)[ \t]*-->[ \t]*$', re.M)
ANY_RESERVED = re.compile(r'<!--[ \t]*BL-')
BLOCK = re.compile(r'(?:^|\s)\^([A-Za-z0-9-]+)\s*$', re.M)

#: entity kinds that own a stable identity anchor and may carry a relation region
ENTITY_KINDS = {'K', 'KP', 'EX', 'SOURCE', 'SRC', 'L', 'PATH', 'STEP', 'START', 'MGMT', 'INDEX', 'CHAPTER'}
#: kinds whose section range is either a registered entity or a registered teaching occurrence
WRAPPABLE_KINDS = ENTITY_KINDS | {'TEACH'}
#: all legal marker kinds (entities plus structural/derived regions)
MARKER_KINDS = ENTITY_KINDS | {'TEACH', 'REL', 'SOURCE-TEXT'}

TOP_LEVEL = {'K', 'KP', 'EX', 'SOURCE', 'L', 'PATH', 'START', 'MGMT', 'INDEX', 'CHAPTER'}
NESTING = {
    'CHAPTER': {'K', 'REL'},
    'K': {'REL'},
    'KP': {'REL'},
    'EX': {'REL'},
    'SOURCE': {'SRC'},
    'SRC': {'SOURCE-TEXT'},
    'SOURCE-TEXT': set(),
    'L': {'TEACH', 'EX', 'REL'},
    'TEACH': {'REL'},
    'PATH': {'STEP', 'REL'},
    'STEP': {'REL'},
    'START': {'REL'},
    'MGMT': {'REL'},
    'INDEX': {'REL'},
    'REL': set(),
}
#: section kinds whose own id becomes the relation scope id
RELATION_CAPABLE = {'K', 'KP', 'EX', 'L', 'TEACH', 'PATH', 'STEP', 'START', 'MGMT', 'INDEX', 'CHAPTER'}
#: kinds that never receive a BL-REL region (evidence leaves)
LEAF_KINDS = {'SOURCE', 'SRC', 'SOURCE-TEXT'}


class SectionError(ValueError):
    """Boundary/anchor problem carrying a v2 error code."""

    def __init__(self, code, message, line=None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.line = line


def line_of(text, offset):
    return text.count('\n', 0, max(0, offset)) + 1


def mask_code(text):
    """Blank out fenced/inline code while keeping every offset and newline."""
    lines, fence = [], None
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            token = match[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append(re.sub(r'[^\r\n]', ' ', line))
        elif fence:
            lines.append(re.sub(r'[^\r\n]', ' ', line))
        else:
            lines.append(re.sub(r'(`+).*?\1', lambda m: ' ' * len(m[0]), line))
    return ''.join(lines)


def blank_spans(text, spans):
    """Blank the given [start, end) spans, preserving offsets and newlines."""
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] not in '\r\n':
                chars[index] = ' '
    return ''.join(chars)


def mask_payload(text, spans):
    return blank_spans(text, spans)


def block_ids(text, payload_spans=()):
    masked = mask_payload(mask_code(text), payload_spans)
    return BLOCK.findall(masked)


def block_occurrences(text, payload_spans=()):
    masked = mask_payload(mask_code(text), payload_spans)
    return [(m[1], m.start()) for m in BLOCK.finditer(masked)]


@dataclass
class Marker:
    kind: str
    id: str
    is_begin: bool
    start: int
    end: int
    line: int


@dataclass
class Section:
    kind: str
    id: str
    start: int
    end: int
    body_start: int
    body_end: int
    children: list = field(default_factory=list)

    @property
    def scope_id(self):
        return None if self.kind == 'REL' else self.id

    def spans(self):
        yield self
        for child in self.children:
            yield from child.spans()


def tokenize(text, payload_spans=()):
    """Return (markers, masked_text). Payload spans are treated as opaque."""
    masked = mask_payload(mask_code(text), payload_spans)
    markers = []
    for match in MARKER.finditer(masked):
        kind, action, sid = match[1], match[2], match[3]
        markers.append(Marker(kind, sid, action == 'BEGIN', match.start(), match.end(),
                              line_of(text, match.start())))
    return markers, masked


@dataclass
class SectionIndex:
    sections: dict
    roots: list
    text: str

    def get(self, kind, section_id):
        return self.sections.get((kind, section_id))

    def find(self, section_id):
        """All sections carrying this id (K has canonical + teaching occurrences)."""
        return [section for (kind, sid), section in self.sections.items() if sid == section_id]

    def require(self, kind, section_id):
        section = self.get(kind, section_id)
        if section is None:
            raise SectionError('SECTION_BOUNDARY_INVALID', f'missing {kind} section {section_id}')
        return section

    def marker_spans(self, kinds=None):
        result = []
        for section in self.sections.values():
            if kinds is None or section.kind in kinds:
                result.append((section.start, section.end))
        return result

    @property
    def relation_scopes(self):
        return [section for section in self.sections.values() if section.kind in RELATION_CAPABLE]

    def nested_scopes(self, section):
        result = []
        for child in section.spans():
            if child is section:
                continue
            if child.kind in RELATION_CAPABLE or child.kind in ENTITY_KINDS:
                result.append(child)
        return result

    def body(self, section):
        return self.text[section.body_start:section.body_end]

    def full(self, section):
        return self.text[section.start:section.end]


def _marker_span(text, span, payload_spans):
    for start, end in payload_spans:
        if start <= span[0] < end:
            return True
    return False


def parse_sections(text, registry=None, payload_spans=(), strict_registry=True):
    """Parse the entity tree. Raises SectionError on any structural violation."""
    markers, _ = tokenize(text, payload_spans)
    markers = [m for m in markers if not _marker_span(text, (m.start, m.end), payload_spans)]
    stack, roots, sections = [], [], {}
    for marker in markers:
        if marker.kind not in MARKER_KINDS:
            raise SectionError('SECTION_BOUNDARY_INVALID', f'unknown marker kind BL-{marker.kind}', marker.line)
        if marker.is_begin:
            parent = stack[-1] if stack else None
            allowed = NESTING.get(parent.kind, set()) if parent else set(TOP_LEVEL)
            if marker.kind not in allowed:
                where = f'inside {parent.kind} {parent.id}' if parent else 'at top level'
                raise SectionError('SECTION_BOUNDARY_INVALID',
                                   f'{marker.kind} {marker.id} not allowed {where}', marker.line)
            node = Section(marker.kind, marker.id, marker.start, None, marker.end, None)
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
            if (marker.kind, marker.id) in sections:
                raise SectionError('SECTION_BOUNDARY_INVALID',
                                   f'duplicate section {marker.kind} {marker.id}', marker.line)
            sections[(marker.kind, marker.id)] = node
            stack.append(node)
            continue
        if not stack:
            raise SectionError('SECTION_BOUNDARY_INVALID', f'END without BEGIN: {marker.kind} {marker.id}',
                               marker.line)
        node = stack.pop()
        if (node.kind, node.id) != (marker.kind, marker.id):
            raise SectionError('SECTION_BOUNDARY_INVALID',
                               f'crossed/mismatched boundaries: {node.kind} {node.id} closed by '
                               f'{marker.kind} {marker.id}', marker.line)
        node.end = marker.end
        node.body_end = marker.start
    if stack:
        node = stack[-1]
        raise SectionError('SECTION_BOUNDARY_INVALID',
                           f'unclosed section {node.kind} {node.id}',
                           line_of(text, node.start))
    if registry is not None and strict_registry:
        for (kind, sid), section in sections.items():
            if kind in ENTITY_KINDS and not registry.has(sid):
                raise SectionError('UNKNOWN_ENTITY', f'{kind} section {sid} is not registered',
                                   line_of(text, section.start))
    index = SectionIndex(sections, roots, text)
    validate_anchor_positions(index, registry)
    return index


def _own_block_anchor(section, block):
    """Anchor paragraph looks like ``ID ^ID`` (not a bare ^ID line)."""
    return re.compile(r'^[ \t]*' + re.escape(block) + r'[ \t]+\^' + re.escape(block) + r'[ \t]*$', re.M)


def validate_anchor_positions(index, registry=None):
    """The identity anchor must be at the entry of its section, never at the tail."""
    text = index.text
    for section in index.sections.values():
        if section.kind not in ENTITY_KINDS and section.kind != 'TEACH':
            continue
        body = text[section.body_start:section.body_end]
        if section.kind == 'TEACH':
            # the teaching occurrence anchors the knowledge entity it teaches
            matches = list(re.compile(r'^[ \t]*([A-Za-z][A-Za-z0-9-]*)[ \t]+\^\1[ \t]*$', re.M)
                           .finditer(mask_code(body)))
            if len(matches) != 1:
                raise SectionError('ANCHOR_NOT_AT_ENTRY',
                                   f'TEACH {section.id} needs exactly one knowledge anchor at its entry',
                                   line_of(text, section.start))
            anchor_id = matches[0][1]
            if registry is not None and not registry.has(anchor_id):
                raise SectionError('UNKNOWN_ENTITY',
                                   f'TEACH {section.id} anchors unregistered entity {anchor_id}',
                                   line_of(text, section.body_start + matches[0].start()))
        else:
            matches = list(_own_block_anchor(section, section.id).finditer(mask_code(body)))
            if len(matches) != 1:
                raise SectionError('ANCHOR_NOT_AT_ENTRY',
                                   f'{section.kind} {section.id} needs exactly one identity anchor '
                                   f'("{section.id} ^{section.id}")', line_of(text, section.start))
        offset = section.body_start + matches[0].start()
        first_child = min([child.start for child in section.children], default=section.body_end)
        rel = [child for child in section.children if child.kind == 'REL']
        if rel:
            first_child = min(first_child, min(child.start for child in rel))
        if offset > first_child:
            raise SectionError('ANCHOR_NOT_AT_ENTRY',
                               f'{section.kind} {section.id} anchor appears after body/child sections',
                               line_of(text, offset))
        before = [line for line in text[section.body_start:offset].splitlines() if line.strip()]
        if len(before) > 3:
            raise SectionError('ANCHOR_NOT_AT_ENTRY',
                               f'{section.kind} {section.id} anchor is too far below the entry',
                               line_of(text, offset))


def entity_section(text, kind, section_id, registry=None, payload_spans=()):
    """Span of the complete entity content. Missing boundaries are an error."""
    index = parse_sections(text, registry, payload_spans, strict_registry=False)
    return index.full(index.require(kind, section_id))


def duplicate_block_ids(text, payload_spans=()):
    seen, duplicates = set(), []
    for value, _ in block_occurrences(text, payload_spans):
        if value in seen:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def reserved_marker_conflicts(text, payload_spans=()):
    """Source payload may not contain BL-* lines or a redefinition of a system block."""
    payload = ''.join(text[start:end] for start, end in payload_spans)
    conflicts = []
    for match in ANY_RESERVED.finditer(payload):
        conflicts.append((match.start(), payload[match.start():match.start() + 60].splitlines()[0]))
    if payload_spans:
        system = set()
        for index, (start, end) in enumerate(payload_spans):
            system |= set(block_ids(text[:start])) | set(block_ids(text[end:]))
        for value, offset in block_occurrences(payload):
            if value in system:
                conflicts.append((offset, f'block id redefined in payload: {value}'))
    return conflicts


def payload_hash(text, spans):
    payload = ''.join(text[start:end] for start, end in spans)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def normalize_payload(text, spans):
    """Reversible display escape for reserved markers inside source payload."""
    spans = sorted(spans)
    pieces, cursor = [], 0
    mapping = []
    for start, end in spans:
        pieces.append(text[cursor:start])
        payload = text[start:end]
        escaped = re.sub(r'<!--([ \t]*BL-)', r'<!--&#32;\1', payload)
        mapping.append({'start': start, 'end': end,
                        'escaped': escaped != payload})
        pieces.append(escaped)
        cursor = end
    pieces.append(text[cursor:])
    return ''.join(pieces), mapping
