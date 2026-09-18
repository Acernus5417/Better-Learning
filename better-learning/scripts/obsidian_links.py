"""Canonical course-root WikiLinks, stable blocks, and typed relation checks.

v2 contract: machine-managed internal entity links always use
``[[course-relative-path#^stable-block-id|label]]``; relations are derived from
reviewed indexes and rendered by the orchestrator, never written by an Agent.
Legacy heading/bare-file targets survive only for ``legacy-report`` diagnostics
and the explicit migration entry points.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from _common import atomic_text, inside, read_json, read_jsonl, sha256, write_json, validation_run
from entity_sections import (ENTITY_KINDS, LEAF_KINDS, SectionError, block_ids as _block_ids,
                             block_occurrences, duplicate_block_ids, entity_section as _entity_section,
                             line_of, mask_code, parse_sections, reserved_marker_conflicts)
from entity_registry import (GRAPH_POLICY_VERSION, LINK_SCHEMA_VERSION, Registry, config,
                             document as registry_document, is_v2, type_of_id, valid_id)
from relation_policy import (ALLOWED, DEP, NAV, RelationError, SOURCE, SUBTYPES, TEACH, TYPES,
                             canonical_key, derive_course_relations, validate_card_exercise_mapping,
                             validate_dep_dag, validate_reciprocal_contracts)
from relation_renderer import (canonical_target, refresh_documents, render_asset_link,
                               render_entity_link, sync_course_relations, vault_target)

WIKI_START = re.compile(r'(!?)\[\[\s*')
URL = re.compile(r'(?:https?://|mailto:)[^\s<>]+')
HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*#*\s*$', re.M)
REL_MARKER = re.compile(r'<!--[ \t]*BL-REL:(BEGIN|END)[ \t]+([A-Za-z0-9-]+)[ \t]*-->')
LEGACY_BLOCK_RELATIONS = re.compile(r'<!-- BL-RELATIONS -->.*?<!-- BL-END-RELATIONS -->\s*', re.S)
MANAGEMENT = {'学习需求.md', '资料清单.md', '学习反馈.md', '质量报告.md', '待核实问题.md',
              '转写拆分计划.md'}

ERROR_CODES = (
    'UNKNOWN_ENTITY', 'DUPLICATE_BLOCK', 'CANONICAL_LOCATION_CONFLICT', 'SECTION_BOUNDARY_INVALID',
    'ANCHOR_NOT_AT_ENTRY', 'HEADING_LINK_FORBIDDEN', 'BLOCK_REQUIRED', 'INVALID_LINK_TARGET',
    'MISSING_TARGET_BLOCK', 'MANIFEST_LINK_VIOLATION', 'RELATION_TYPE_FORBIDDEN',
    'RELATION_PROVENANCE_MISSING', 'RELATION_PROJECTION_MISMATCH', 'KP_TARGET_FORBIDDEN',
    'SOURCE_NOT_LEAF', 'DEPENDENCY_CYCLE', 'STALE_MANIFEST', 'RESERVED_SYSTEM_MARKUP',
    'UNRESOLVED_MIGRATION',
)


def _code(message):
    for code in ERROR_CODES:
        if message.startswith(code):
            return code
    return 'INVALID_LINK_TARGET'


# --------------------------------------------------------------------- parsing
def parse_wikilinks(text, payload_spans=()):
    """Wikilinks with offsets. ``\\|`` (table-safe) separators are understood."""
    masked = mask_code(text)
    if payload_spans:
        from entity_sections import blank_spans
        masked = blank_spans(masked, payload_spans)
    urls = [m.span() for m in URL.finditer(masked)]
    results = []
    for start_match in WIKI_START.finditer(masked):
        if any(a <= start_match.start() < b for a, b in urls):
            continue
        embed = bool(start_match[1])
        cursor = start_match.end()
        while cursor < len(masked):
            if masked[cursor] == '\\':
                cursor += 2
                continue
            if masked.startswith(']]', cursor):
                break
            if masked[cursor] == '\n':
                cursor = -1
                break
            cursor += 1
        if cursor < 0 or cursor >= len(masked) or not masked.startswith(']]', cursor):
            continue
        inner = text[start_match.end():cursor]
        end = cursor + 2
        separator, escaped = None, False
        for index, char in enumerate(inner):
            if char == '\\' and inner[index + 1:index + 2] == '|':
                separator, escaped = index, True
                break
            if char == '|':
                separator, escaped = index, False
                break
        if separator is None:
            body, label = inner, None
        else:
            body = inner[:separator]
            label = inner[separator + (2 if escaped else 1):].replace('\\|', '|')
        target, marker, anchor = body.partition('#')
        block = anchor[1:] if anchor.startswith('^') else None
        heading = anchor if marker and not anchor.startswith('^') else None
        results.append({'target': target.strip(), 'block': block, 'heading': heading, 'label': label,
                        'embed': embed, 'span': (start_match.start(), end),
                        'key': target.strip() + ('#' + anchor if marker else ''),
                        'table_escaped': separator is not None and inner[separator:separator + 2] == '\\|'})
    return results


def block_ids(text, payload_spans=()):
    return _block_ids(text, payload_spans)


def block_section(text, block):
    raise ValueError('block_section() is not part of v2; use entity_section() or legacy_block_section()')


def legacy_block_section(text, block):
    """Diagnostics/migration only: range from the previous same-family block ID."""
    from entity_sections import BLOCK, mask_code as _mask
    start = 0
    family = block.split('-', 1)[0] + '-'
    for match in BLOCK.finditer(_mask(text)):
        if match[1] == block:
            return text[start:match.end()]
        if match[1].startswith(family) or (family == 'K-' and match[1].startswith('KC-')):
            start = match.end()
    return ''


def entity_section(text, kind, section_id, registry=None, payload_spans=()):
    return _entity_section(text, kind, section_id, registry, payload_spans)


def _has(text, target, block):
    """Diagnostic helper only; acceptance compares observed vs expected relation keys."""
    return any(link['target'].removesuffix('.md') == target.removesuffix('.md') and link['block'] == block
               for link in parse_wikilinks(text))


def parse_entity_sections(text, registry=None, payload_spans=()):
    return parse_sections(text, registry, payload_spans, strict_registry=False)


# ------------------------------------------------------------------- rendering
def render_wikilink(target, block=None, heading=None, label=None, embed=False, table=False):
    """Legacy helper kept for v1 courses; v2 code uses render_entity_link()."""
    canonical_target(target)
    if embed and block is None and heading is None:
        return render_asset_link(target, label)
    if heading:
        raise ValueError('HEADING_LINK_FORBIDDEN: v2 has no heading targets')
    if not block:
        raise ValueError('BLOCK_REQUIRED: internal entity links need #^block')
    if not re.fullmatch(r'[A-Za-z0-9-]+', block):
        raise ValueError('INVALID_LINK_TARGET: invalid block ' + block)
    if any(c in (label or '') for c in '[]|\n\r'):
        raise ValueError('INVALID_LINK_TARGET: invalid label')
    target = vault_target(target)
    separator = '\\|' if table else '|'
    return ('!' if embed else '') + '[[' + target + '#^' + block + (separator + label if label else '') + ']]'


def render_heading_link(target, heading, label=None, embed=False):
    """Legacy-only renderer for diagnostics and v1 compatibility."""
    canonical_target(target)
    if any(c in (label or '') + heading for c in '[]|\n\r'):
        raise ValueError('INVALID_LINK_TARGET: invalid heading/label')
    return ('!' if embed else '') + '[[' + target + '#' + heading + ('|' + label if label else '') + ']]'


# --------------------------------------------------------------------- mapping
def build_map(course, moves=None):
    """Versioned entity/location map; legacy top-level keys stay for adapters."""
    moves = moves or {}
    registry = Registry(course)
    config_data = registry.config

    def move(path):
        without = path.removesuffix('.md')
        return moves.get(without, without)

    data = {'schema_version': LINK_SCHEMA_VERSION, 'graph_policy_version': GRAPH_POLICY_VERSION,
            'registry_revision': registry.revision(), 'vault_prefix': registry.vault_prefix,
            'layout': config_data.get('layout', 'working'),
            'knowledge': move(registry.canonical('IDX-KNOWLEDGE').path.removesuffix('.md') if False
                              else registry_document(course, '知识内容.md').removesuffix('.md')),
            'learning_path': move(registry_document(course, '学习路径.md').removesuffix('.md')),
            'core_cards': move(registry_document(course, '核心知识点.md').removesuffix('.md')),
            'start': move(registry_document(course, '开始学习.md').removesuffix('.md')),
            'lessons': {}, 'sources': {}, 'cards': {}, 'paths': {}, 'management': {},
            'locations': {}}
    for entity in registry.entities.values():
        for location in entity.locations:
            data['locations'][location.id] = {'path': move(location.path.removesuffix('.md')),
                                              'block': location.block_id, 'type': entity.type,
                                              'label': entity.label, 'scope_id': location.scope_id,
                                              'role': location.role, 'owner_id': location.owner_id}
    for entity in registry.active_entities('L', ('active', 'staged', 'reserved')):
        data['lessons'][entity.id] = move(entity.data.get('path', '').removesuffix('.md'))
    index = course / '_工作区/转写索引.json'
    if index.exists():
        for entry in read_json(index).get('sources', []):
            data['sources'][entry['source_id']] = move(entry['path'].removesuffix('.md'))
    for entity in registry.active_entities('KP', ('active', 'staged')):
        data['cards'][entity.id] = data['core_cards']
    for entity in registry.active_entities('P', ('active', 'staged')):
        data['paths'][entity.id] = data['learning_path']
    for entity in registry.active_entities('M', ('active', 'staged')):
        for location in entity.locations:
            data['management'][entity.id] = move(location.path.removesuffix('.md'))
    write_json(course / '_工作区/链接映射.json', data)
    registry.save()
    return data


def link_map(course):
    path = course / '_工作区/链接映射.json'
    if path.exists():
        data = read_json(path)
        if data.get('schema_version') == LINK_SCHEMA_VERSION:
            return data
    return build_map(course)


def registry(course):
    return Registry(course)


def strip_vault_prefix(target, prefix):
    prefix = (prefix or '').strip('/')
    if prefix and target.startswith(prefix + '/'):
        return target[len(prefix) + 1:]
    return target


def validate_candidate(course, documents, phase='staged', lesson_id=None, require_targets=True):
    """Validate a virtual file set (staged candidates) without writing it.

    `require_targets=False` is for mid-course commits: a chapter may legitimately
    link lessons, steps and entries that have not been written yet. Final
    validation (--mode final) still requires every physical target.
    """
    registry_obj = Registry(course)
    diagnostics = []
    for path, text in documents.items():
        try:
            parse_sections(text, None, strict_registry=False)
        except SectionError as exc:
            diagnostics.append({'code': exc.code, 'file': path, 'line': exc.line, 'message': exc.message})
            continue
        diagnostics.extend(validate_links(course, text, registry_obj=registry_obj, phase=phase,
                                          require_targets=require_targets))
    graph = derive_course_relations(course, registry_obj, phase=phase)
    merged = dict(learner_documents(course))
    merged.update(documents)
    for item in check_projection(course, graph, registry_obj, merged):
        diagnostics.append(item)
    for item in check_scope_link_policy(graph, registry_obj, merged):
        diagnostics.append(item)
    for item in check_kp_and_leaf_constraints(graph, registry_obj):
        diagnostics.append(item)
    return ['[{}] {}: {}'.format(item.get('code'), item.get('file', ''),
                                 item.get('message') or item.get('detail') or '')
            for item in diagnostics]


def sync_candidate(course, documents, phase='staged'):
    """Render relation regions over a virtual file set; returns the full file set."""
    registry_obj = Registry(course)
    graph = derive_course_relations(course, registry_obj, phase=phase)
    merged = dict(learner_documents(course))
    merged.update(documents)
    refreshed = refresh_documents(merged, graph, registry_obj, registry_obj.vault_prefix)
    result = {}
    for path in documents:
        result[path] = refreshed.get(path, documents[path])
    for path, text in refreshed.items():
        result.setdefault(path, text)
    return result, graph


# ------------------------------------------------------------------ validation
def _link_problem(link, code, detail):
    return {'code': code, 'message': detail, 'key': link.get('key'), 'span': link.get('span')}


def validate_links(course, text, allowlist=None, allowed_blocks=None, require_targets=True,
                   registry_obj=None, manifest=None, scope=None, payload_spans=(), phase='final'):
    """Return structured diagnostics for the links of one document or scope."""
    registry_obj = registry_obj or Registry(course)
    v2 = is_v2(course)
    diagnostics = []
    if allowed_blocks is not None:
        unknown = sorted(set(block_ids(text, payload_spans)) - set(allowed_blocks))
        for block in unknown:
            diagnostics.append({'code': 'UNKNOWN_ENTITY', 'message': f'unregistered block: {block}'})
    for block in sorted(set(duplicate_block_ids(text, payload_spans))):
        diagnostics.append({'code': 'DUPLICATE_BLOCK', 'message': f'duplicate block definition: {block}'})
    for link in parse_wikilinks(text, payload_spans):
        if link['embed']:
            continue
        try:
            canonical_target(link['target'])
        except ValueError as exc:
            diagnostics.append({'code': 'INVALID_LINK_TARGET', 'message': str(exc), 'key': link['key']})
            continue
        if link['heading']:
            diagnostics.append({'code': 'HEADING_LINK_FORBIDDEN',
                                'message': f'heading target is not a v2 entity link: {link["key"]}'})
            continue
        if not link['block']:
            diagnostics.append({'code': 'BLOCK_REQUIRED',
                                'message': f'internal entity link needs #^block: {link["key"]}'})
            continue
        if not valid_id(link['block']):
            diagnostics.append({'code': 'INVALID_LINK_TARGET',
                                'message': f'invalid block id in link: {link["key"]}'})
            continue
        normalized = strip_vault_prefix(link['target'], registry_obj.vault_prefix)
        key = normalized + ('#' + ('^' + link['block'] if link['block'] else '') if link['block'] else '')
        try:
            location_id = None
            entity = registry_obj.entity(link['block'])
            for location in entity.locations:
                if location.path.removesuffix('.md') == normalized:
                    location_id = location.id
                    break
            if location_id is None:
                if entity.type == 'SRC' and normalized.endswith(entity.id):
                    target_location = registry_obj.canonical(link['block'])
                else:
                    diagnostics.append({'code': 'INVALID_LINK_TARGET',
                                        'message': f'non-canonical target for {link["block"]}: {link["target"]}'})
                    continue
            else:
                target_location = registry_obj.location(location_id)
        except KeyError:
            diagnostics.append({'code': 'UNKNOWN_ENTITY', 'message': f'unregistered link target: {link["key"]}'})
            continue
        if allowlist is not None and key not in allowlist:
            diagnostics.append({'code': 'MANIFEST_LINK_VIOLATION',
                                'message': f'literal not present in this section manifest: {key}'})
            continue
        if require_targets:
            problems = check_physical_target(course, target_location, registry_obj)
            diagnostics.extend(problems)
    for match in re.finditer(r'!?\[[^\]\n]*\]\(([^)]+)\)', mask_code(text)):
        scheme = urlsplit(match[1].strip('<>')).scheme
        if not scheme:
            diagnostics.append({'code': 'INVALID_LINK_TARGET',
                                'message': 'internal links must use canonical WikiLink: ' + match[1]})
        elif scheme not in {'http', 'https', 'mailto'}:
            diagnostics.append({'code': 'INVALID_LINK_TARGET',
                                'message': 'unsupported external link scheme: ' + scheme})
    if v2 and scope is not None:
        for diagnostic in diagnostics:
            diagnostic.setdefault('scope', scope)
    return diagnostics


def check_physical_target(course, location, registry_obj=None):
    diagnostics = []
    try:
        path = inside(course, location.path)
    except ValueError as exc:
        return [{'code': 'INVALID_LINK_TARGET', 'message': str(exc)}]
    packaged = config(course).get('layout') == 'packaged'
    if not path.is_file():
        packaged = True
        alternatives = [candidate for candidate in
                        (course / '课程文档' / location.path, course / location.path) if candidate.is_file()]
        if not alternatives:
            return [{'code': 'MISSING_TARGET_BLOCK',
                     'file': location.path, 'expected': f'file with block {location.block_id}',
                     'actual': 'file not found', 'suggestion': 'run package or rebuild the course layout'}]
        path = alternatives[0]
    text = path.read_text(encoding='utf-8')
    occurrences = [block for block, _ in block_occurrences(text) if block == location.block_id]
    if len(occurrences) == 0:
        diagnostics.append({'code': 'MISSING_TARGET_BLOCK', 'file': location.path,
                            'expected': f'one definition of ^{location.block_id}',
                            'actual': 'block not found',
                            'suggestion': 're-run the assembler or migration for this entity'})
    elif len(occurrences) > 1:
        diagnostics.append({'code': 'DUPLICATE_BLOCK', 'file': location.path,
                            'expected': f'exactly one ^{location.block_id}',
                            'actual': f'{len(occurrences)} definitions'})
    if packaged and registry_obj is not None:
        pass
    return diagnostics


def validate_document(course, text, allowlist=None, allowed_blocks=None, require_targets=True,
                      registry_obj=None, manifest=None, scope=None, payload_spans=(), phase='final'):
    """Backwards-compatible string list wrapper around validate_links()."""
    diagnostics = validate_links(course, text, allowlist, allowed_blocks, require_targets,
                                 registry_obj, manifest, scope, payload_spans, phase)
    return [f"{item['code']}: {item['message']}" for item in diagnostics]


def learner_documents(course, include_working=False):
    documents = {}
    for path in sorted(course.rglob('*.md')):
        parts = path.relative_to(course).parts
        if any(part in {'资料转写', '原始资料'} for part in parts):
            continue
        if '_工作区' in parts and not (include_working and '章节知识' in parts):
            continue
        documents[path.relative_to(course).as_posix()] = path.read_text(encoding='utf-8-sig')
    return documents


def validate_structure(course, documents=None, phase='final'):
    """Section boundaries, anchors, duplicate blocks and reserved regions."""
    documents = documents if documents is not None else learner_documents(course)
    diagnostics = []
    for name, text in documents.items():
        try:
            parse_sections(text, None, strict_registry=False)
        except SectionError as exc:
            diagnostics.append({'code': exc.code, 'file': name, 'line': exc.line, 'message': exc.message})
            continue
        index = parse_sections(text, None, strict_registry=False)
        for location in [loc for entity in Registry(course).entities.values() for loc in entity.locations
                         if loc.path == name]:
            section = index.get(location.section_kind, location.section_id)
            if section is None:
                diagnostics.append({'code': 'SECTION_BOUNDARY_INVALID', 'file': name,
                                    'message': f'missing section {location.section_kind} {location.section_id}',
                                    'expected': location.section_id, 'actual': 'missing'})
    return diagnostics


@validation_run
def validate_obsidian_links(course, relationships=True, phase='final'):
    """Location-first validation, then typed graph and projection equality."""
    if not enabled(course): return []
    errors, diagnostics = [], []
    registry_obj = Registry(course)
    documents = learner_documents(course)
    for name, text in documents.items():
        for diagnostic in validate_links(course, text, registry_obj=registry_obj, phase=phase):
            diagnostic['file'] = name
            diagnostics.append(diagnostic)
    for item in validate_structure(course, documents, phase):
        diagnostics.append(item)
    for warning in registry_obj.warnings:
        diagnostics.append({'code': 'UNKNOWN_ENTITY', 'message': warning})
    if relationships:
        try:
            graph = derive_course_relations(course, registry_obj, phase=phase)
            diagnostics.extend(validate_card_exercise_mapping(
                graph.edges, registry_obj, read_jsonl(course / '_工作区/练习索引.jsonl')
                if (course / '_工作区/练习索引.jsonl').exists() else []))
            diagnostics.extend(validate_reciprocal_contracts(graph.edges, registry_obj)
                               if phase in {'final', 'packaged'} else [])
            diagnostics.extend(check_projection(course, graph, registry_obj, documents))
            diagnostics.extend(check_scope_link_policy(graph, registry_obj, documents))
            diagnostics.extend(check_kp_and_leaf_constraints(graph, registry_obj))
        except RelationError as exc:
            diagnostics.append({'code': exc.code, 'message': exc.message, **(exc.detail or {})})
    for diagnostic in diagnostics:
        detail = diagnostic.get('message') or diagnostic.get('detail') or ''
        location = diagnostic.get('file', '')
        if diagnostic.get('line'):
            location += ':' + str(diagnostic['line'])
        errors.append(f"[{diagnostic.get('code', 'INVALID_LINK_TARGET')}] {location + ': ' if location else ''}{detail}")
    return list(dict.fromkeys(errors))


def check_projection(course, graph, registry_obj, documents):
    """The rendered callout must equal the expected relation set, edge by edge."""
    diagnostics = []
    for entity in registry_obj.entities.values():
        for location in entity.locations:
            text = documents.get(location.path)
            if text is None or location.section_kind in LEAF_KINDS:
                continue
            index = parse_sections(text, None, strict_registry=False)
            section = index.get(location.section_kind, location.section_id)
            if section is None:
                continue
            rel = [child for child in section.children if child.kind == 'REL']
            observed = set()
            if rel:
                body = text[rel[0].body_start:rel[0].body_end]
                for link in parse_wikilinks(body):
                    if link['block']:
                        observed.add(link['block'] + '@' + link['target'])
            expected = {(registry_obj.location(edge['target_location_id']).block_id + '@' +
                         registry_obj.location(edge['target_location_id']).path.removesuffix('.md'))
                        for edge in graph.outgoing_for_scope(location.scope_id)}
            if observed != expected:
                diagnostics.append({
                    'code': 'RELATION_PROJECTION_MISMATCH', 'file': location.path,
                    'scope': location.scope_id,
                    'expected': sorted(expected), 'actual': sorted(observed),
                    'message': f'{location.scope_id} callout differs from the derived relation set',
                    'suggestion': 'run the relation renderer; do not hand-edit relation callouts'})
    return diagnostics


def check_scope_link_policy(graph, registry_obj, documents):
    """Active body links must be authorized relations of the innermost scope.

    Covers callout, prose, lists and tables alike; a KP cannot smuggle a
    forbidden target through its body, and nested scopes are excluded.
    """
    diagnostics = []
    for entity in registry_obj.entities.values():
        if entity.status == 'merged':
            continue
        for location in registry_obj.locations(entity.id):
            if location.section_kind in LEAF_KINDS:
                continue
            text = documents.get(location.path)
            if text is None:
                continue
            try:
                index = parse_sections(text, None, strict_registry=False)
            except SectionError:
                continue
            section = index.get(location.section_kind, location.section_id)
            if section is None:
                continue
            spans = sorted((child.start, child.end) for child in section.children)
            pieces, cursor = [], section.body_start
            for start, end in spans:
                pieces.append(text[cursor:start])
                cursor = end
            pieces.append(text[cursor:section.body_end])
            for link in parse_wikilinks('\n'.join(pieces)):
                if link['embed'] or link['heading'] or not link['block']:
                    continue
                target_entity = registry_obj.entities.get(link['block'])
                if target_entity is None:
                    continue
                normalized = strip_vault_prefix(link['target'], registry_obj.vault_prefix)
                target_location = next((candidate for candidate in target_entity.locations
                                        if candidate.path.removesuffix('.md') == normalized), None)
                if target_location is None:
                    continue
                if graph.match_authorized_link(location.scope_id, link['block'],
                                               target_location.id, surface='body') is None:
                    diagnostics.append({
                        'code': 'KP_TARGET_FORBIDDEN' if entity.type == 'KP' else 'RELATION_PROVENANCE_MISSING',
                        'file': location.path, 'scope': location.scope_id,
                        'message': f'{link["key"]} is not an authorized {entity.type} relation',
                        'expected': 'a reviewed edge for this scope', 'actual': link['key'],
                        'suggestion': 'remove the link or register the relation after review'})
                    continue
                if entity.type == 'KP' and target_location.id != registry_obj.canonical(link['block']).id:
                    diagnostics.append({
                        'code': 'KP_TARGET_FORBIDDEN', 'file': location.path, 'scope': location.scope_id,
                        'message': f'{entity.id} must resolve to the canonical location of {link["block"]}',
                        'expected': registry_obj.canonical(link['block']).id,
                        'actual': target_location.id})
    return diagnostics


def check_kp_and_leaf_constraints(graph, registry_obj):
    diagnostics = []
    for entity in registry_obj.entities.values():
        if entity.status == 'merged':
            continue
        if entity.type == 'KP':
            for edge in graph.outgoing(entity.id):
                if registry_obj.type_of(edge['target_id']) not in {'K', 'EX'}:
                    diagnostics.append({'code': 'KP_TARGET_FORBIDDEN',
                                        'message': f'{entity.id} links {edge["target_id"]}',
                                        'relation_key': edge['relation_key']})
                for location in registry_obj.locations(entity.id):
                    if location.role == 'teaching':
                        diagnostics.append({'code': 'KP_TARGET_FORBIDDEN',
                                            'message': f'{entity.id} resolves to a teaching occurrence'})
        if entity.type == 'SRC' and graph.out_degree(entity.id):
            diagnostics.append({'code': 'SOURCE_NOT_LEAF',
                                'message': f'{entity.id} owns system out-edges'})
    return diagnostics


# -------------------------------------------------------------------- rewriting
def rewrite_targets(text, moves, registry_obj=None, payload_spans=()):
    """Span-based rewrite of registered links only; code and payload are untouched."""
    result = text
    for link in reversed(parse_wikilinks(text, payload_spans)):
        target = moves.get(link['target'], link['target'])
        key = moves.get(link['key'])
        block, heading = link['block'], link['heading']
        if key:
            moved, _, anchor = key.partition('#')
            target = moved
            block = anchor[1:] if anchor.startswith('^') else None
            heading = anchor if anchor and not anchor.startswith('^') else None
        if target == link['target'] and not key:
            continue
        if heading:
            replacement = render_heading_link(target, heading, link['label'], link['embed'])
        elif block:
            replacement = render_wikilink(target, block, None, link['label'], link['embed'],
                                          table=link.get('table_escaped', False))
        else:
            replacement = render_asset_link(target, link['label'], link['embed']) if link['embed'] \
                else '[[' + vault_target(target) + (('|' + link['label']) if link['label'] else '') + ']]'
        start, end = link['span']
        result = result[:start] + replacement + result[end:]
    return result


def rewrite_course(course, moves, payload_paths=('资料转写', '原始资料')):
    changed = []
    for path in sorted(course.rglob('*.md')):
        parts = path.relative_to(course).parts
        if any(part in payload_paths for part in parts):
            continue
        if '_工作区' in parts and '章节知识' not in parts:
            continue
        old = path.read_text(encoding='utf-8')
        new = rewrite_targets(old, moves)
        if new != old:
            atomic_text(path, new)
            changed.append(path.relative_to(course).as_posix())
    return changed


# --------------------------------------------------------------------- helpers
def invalidate(course, reason):
    from lesson_tasks import invalidate as invalidate_lessons
    invalidate_lessons(course, reason)


def relocate_links(course, moves, phase='working'):
    """Registered path/prefix moves: rewrite spans, rebuild locations, re-render."""
    for attempt in read_jsonl(course / '_工作区/链接迁移记录.jsonl') if False else []:
        pass
    changed = rewrite_course(course, moves)
    build_map(course)
    report = {'moves': moves, 'changed': changed}
    if enabled(course):
        report.update(sync_course_relations(course, phase=phase))
    return report


def initialize(course, upgrade=False):
    """New courses get schema v2; existing packages are never silently upgraded."""
    path = course / '_工作区/课程配置.json'
    existed = path.exists()
    data = read_json(path) if existed else {}
    data.setdefault('link_mode', 'obsidian')
    data.setdefault('embed_assets', True)
    data.setdefault('wikilink_extension', False)
    if not existed or upgrade:
        data['link_schema_version'] = LINK_SCHEMA_VERSION
        data.setdefault('graph_policy_version', GRAPH_POLICY_VERSION)
        data.setdefault('course_vault_prefix', '')
        data.setdefault('layout', 'working')
    else:
        # v1 packages keep their extraction method until an explicit migration.
        data.setdefault('link_schema_version', 1)
    if data.get('link_schema_version', 1) >= LINK_SCHEMA_VERSION:
        data.setdefault('graph_policy_version', GRAPH_POLICY_VERSION)
        data.setdefault('course_vault_prefix', '')
        data.setdefault('layout', 'working')
    write_json(path, data)
    return build_map(course)


def enabled(course):
    path = course / '_工作区/课程配置.json'
    return path.exists() and read_json(path).get('link_mode') == 'obsidian'


def _active_attempts(course, ledger):
    path = course / '_工作区' / ledger
    if not path.exists():
        return False
    from agent_pool import active_attempts
    return bool(active_attempts(read_json(path)))


# ------------------------------------------------------------- rename / merge
def rename_lesson(course, lesson_id, new_path):
    """Transactional path change: keep IDs, rebuild locations, re-render, verify."""
    if _active_attempts(course, '讲义任务.json'):
        raise ValueError('Stop and collect lesson members before renaming')
    ledger_path = course / '_工作区/讲义任务.json'
    if ledger_path.exists():
        from lesson_tasks import check
        errors = check(course)
        if errors:
            raise ValueError('修复旧讲义状态后再重命名：' + '; '.join(errors))
        from core_cards import STATE, check as check_cards
        if (course / STATE).exists() and read_json(course / STATE).get('status') == 'complete':
            errors = check_cards(course)
            if errors:
                raise ValueError('修复旧卡片状态后再重命名：' + '; '.join(errors))
    index_path = course / '_工作区/章节索引.json'
    index = read_json(index_path)
    lesson = next(l for l in index['lessons'] if l['id'] == lesson_id)
    canonical_target(new_path.removesuffix('.md'))
    if not new_path.replace('\\', '/').startswith('学习文档/') or not new_path.endswith('.md'):
        raise ValueError('Lesson path must stay under 学习文档/')
    destination = inside(course, new_path)
    if destination.exists():
        raise ValueError('Rename target already exists')
    old = lesson['path']
    before_index = json.dumps(index, ensure_ascii=False, sort_keys=True)
    source = inside(course, old)
    backup = source.read_bytes() if source.exists() else None
    moves = {old.removesuffix('.md'): new_path.removesuffix('.md')}
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            source.rename(destination)
        lesson['path'] = new_path
        write_json(index_path, index)
        rewrite_course(course, moves)
        build_map(course, moves)
        if ledger_path.exists():
            from lesson_tasks import record_link_relocation
            record_link_relocation(course, moves)
        report = {'lesson_id': lesson_id, 'path': new_path, 'changed': rewrite_course(course, {})}
        if enabled(course):
            report.update(sync_course_relations(course, phase='working'))
        errors = validate_obsidian_links(course, phase='working')
        if errors:
            raise ValueError('重命名后链接检查失败：' + '; '.join(errors))
        report['link_errors'] = []
        return report
    except (OSError, ValueError, KeyError) as exc:
        if backup is not None and not destination.exists() and not source.exists():
            source.write_bytes(backup)
        elif destination.exists() and not source.exists():
            destination.rename(source)
        atomic_text(index_path, before_index + '\n')
        build_map(course)
        raise ValueError(f'rename rolled back: {exc}') from exc


def merge_knowledge(course, old_id, target_id):
    """Identity migration only: tombstone, repoint edges, keep teaching text."""
    for ledger in ('讲义任务.json', '转写任务.json'):
        if _active_attempts(course, ledger):
            raise ValueError('先停止并收集活动成员')
    path = course / '_工作区/知识索引.jsonl'
    entries = read_jsonl(path)
    by_id = {k['id']: k for k in entries}
    if old_id == target_id or old_id not in by_id or target_id not in by_id:
        raise ValueError('Invalid merge IDs')
    old, target = by_id[old_id], by_id[target_id]
    if old.get('status') == 'merged' or target.get('status') == 'merged':
        raise ValueError('Merge target must be active')
    if target_id in old.get('prerequisites', []):
        raise ValueError('DEPENDENCY_CYCLE: merge would create a dependency cycle')
    old.update(status='merged', merged_into=target_id)
    target['lesson_ids'] = sorted(set(target.get('lesson_ids', []) + old.get('lesson_ids', [])))
    refs = target.get('source_refs', []) + old.get('source_refs', [])
    target['source_refs'] = list({json.dumps(r, sort_keys=True): r for r in refs}.values())
    target['status'] = 'needs_review'
    for k in entries:
        k['prerequisites'] = sorted({target_id if dep == old_id else dep for dep in k.get('prerequisites', [])}
                                    - {k['id']})
    atomic_text(path, ''.join(json.dumps(k, ensure_ascii=False) + '\n' for k in entries))
    moves = {}
    for note in course.rglob('*.md'):
        if any(p in {'资料转写', '原始资料'} for p in note.relative_to(course).parts):
            continue
        for link in parse_wikilinks(note.read_text(encoding='utf-8')):
            if link['block'] == old_id:
                moves[link['key']] = link['target'] + '#^' + target_id
    rewrite_course(course, moves)
    for candidate in course.rglob('*.md'):
        if any(p in {'资料转写', '原始资料'} for p in candidate.relative_to(course).parts):
            continue
        text = candidate.read_text(encoding='utf-8')
        legacy = LEGACY_BLOCK_RELATIONS.sub('', text)
        if legacy != text:
            atomic_text(candidate, legacy)
    build_map(course)
    from lesson_tasks import invalidate as invalidate_lessons
    invalidate_lessons(course, 'knowledge merge requires content review: ' + old_id + ' -> ' + target_id)
    if enabled(course):
        report = sync_course_relations(course, phase='working')
        report['link_errors'] = validate_obsidian_links(course, phase='working')
    else:
        report = {'link_errors': []}
    return {'merged': old_id, 'merged_into': target_id, 'status': 'needs_review',
            'next': '保留原知识正文；主代理合并审阅目标分片、移除重复块、更新覆盖台账，再重建知识库与下游。',
            **report}


# -------------------------------------------------------------------- CLI glue
def migrate(course, command, **kwargs):
    from link_migration import apply_migration, plan_migration, rollback_migration
    if command == 'plan-migration':
        return plan_migration(course, to_version=2)
    if command == 'apply-migration':
        return apply_migration(course, kwargs['plan'])
    return rollback_migration(course, kwargs.get('journal'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--course', required=True, type=Path)
    parser.add_argument('--mode', default='final',
                        choices=['legacy-report', 'staged', 'final', 'packaged'],
                        help='validator mode; only legacy-report tolerates v1 syntax')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init')
    r = sub.add_parser('check')
    r.add_argument('--mode', dest='check_mode', default=None,
                   choices=['legacy-report', 'staged', 'final', 'packaged'])
    sub.add_parser('rebuild')
    r = sub.add_parser('rename-lesson'); r.add_argument('--lesson', required=True); r.add_argument('--path', required=True)
    r = sub.add_parser('merge-knowledge'); r.add_argument('--from-id', required=True); r.add_argument('--into-id', required=True)
    r = sub.add_parser('plan-migration'); r.add_argument('--to-version', type=int, default=2)
    r = sub.add_parser('apply-migration'); r.add_argument('--plan', required=True)
    r = sub.add_parser('rollback-migration'); r.add_argument('--journal', required=True)
    args = parser.parse_args()
    course = args.course.resolve()
    from convert_materials import locked
    try:
        with locked(course):
            if args.command == 'init':
                result = initialize(course)
            elif args.command == 'rename-lesson':
                result = rename_lesson(course, args.lesson, args.path)
            elif args.command == 'merge-knowledge':
                result = merge_knowledge(course, args.from_id, args.into_id)
            elif args.command == 'rebuild':
                result = sync_course_relations(course, phase='working')
            elif args.command == 'check':
                mode = getattr(args, 'check_mode', None) or args.mode
                phase = {'legacy-report': 'working', 'staged': 'staged'}.get(mode, mode)
                result = {'mode': mode, 'errors': validate_obsidian_links(course, phase=phase)}
            else:
                plan = None
                if args.command == 'apply-migration':
                    plan = args.plan
                result = migrate(course, args.command, plan=plan, journal=getattr(args, 'journal', None))
        print(json.dumps(result, ensure_ascii=False))
        return 1 if isinstance(result, dict) and result.get('errors') else 0
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
