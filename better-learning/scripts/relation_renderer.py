"""Deterministic rendering of typed links and folded relation callouts.

Nothing here calls a model or reads the body to guess relations: the region is a
pure projection of the relation table onto verified section offsets.
"""
from __future__ import annotations

import re

from entity_sections import SectionError, line_of, parse_sections
from relation_policy import RelationError, callout_title, group_edges

BAD_LABEL = re.compile(r'[\[\]|\r\n]')
SAFE_PATH = re.compile(r'^[^\\:*?"<>|\r\n]+$')


def canonical_target(target):
    """Course-relative canonical path, vault-root based, no .md suffix."""
    if not isinstance(target, str) or not target:
        raise ValueError('INVALID_LINK_TARGET: empty target')
    if target.startswith('/') or '\\' in target or ':' in target:
        raise ValueError('INVALID_LINK_TARGET: ' + target)
    if target.lower().endswith('.md'):
        raise ValueError('INVALID_LINK_TARGET: .md suffix is not allowed: ' + target)
    parts = target.split('/')
    if any(part in {'', '.', '..'} for part in parts):
        raise ValueError('INVALID_LINK_TARGET: ' + target)
    if any(c in target for c in '#|[]\n\r%^'):
        raise ValueError('INVALID_LINK_TARGET: reserved character in ' + target)
    if parts[0] == '_工作区':
        raise ValueError('INVALID_LINK_TARGET: working directory is not a link target: ' + target)
    if not SAFE_PATH.match(target):
        raise ValueError('INVALID_LINK_TARGET: ' + target)
    return target


def vault_target(path, vault_prefix=''):
    """Location paths keep .md for file access; WikiLink targets never do."""
    target = canonical_target(path[:-3] if path.lower().endswith('.md') else path)
    prefix = (vault_prefix or '').strip('/')
    return f'{prefix}/{target}' if prefix else target


def render_entity_link(location, label=None, vault_prefix='', table=False):
    """[[course-relative-path#^stable-block-id|label]] — the only v2 form."""
    if not location.block_id:
        raise ValueError('BLOCK_REQUIRED: ' + location.id)
    target = vault_target(location.path, vault_prefix)
    text = (label if label is not None else location.block_id) or location.block_id
    if BAD_LABEL.search(text):
        raise ValueError('INVALID_LINK_TARGET: illegal label for ' + location.id)
    separator = '\\|' if table else '|'
    return f'[[{target}#^{location.block_id}{separator}{text}]]'


def render_asset_link(path, label=None, embed=True):
    from entity_sections import block_ids
    if path.lower().endswith('.md'):
        raise ValueError('INVALID_LINK_TARGET: assets keep their extension, not .md')
    if not re.search(r'\.(png|jpe?g|gif|webp|svg|bmp|tiff?|pdf|mp4|webm|mp3|wav|m4a)$', path, re.I):
        raise ValueError('INVALID_LINK_TARGET: unknown asset type: ' + path)
    if any(c in path for c in '#|[]\n\r') or path.startswith('/') or '\\' in path:
        raise ValueError('INVALID_LINK_TARGET: ' + path)
    if label and BAD_LABEL.search(label):
        raise ValueError('INVALID_LINK_TARGET: illegal label for ' + path)
    return ('!' if embed else '') + '[[' + path + ('|' + label if label else '') + ']]'


def _link_for(edge, registry, vault_prefix=''):
    location = registry.location(edge['target_location_id'])
    return render_entity_link(location, registry.label(edge['target_id']), vault_prefix)


def render_relation_region(scope_id, graph, registry, vault_prefix=''):
    """Visible folded callout; empty string when the scope has no out-edges."""
    edges = graph.outgoing_for_scope(scope_id)
    if not edges:
        return ''
    top = edges[0]
    source_location = registry.location(top['source_location_id'])
    if source_location.section_kind in {'SOURCE', 'SRC'}:
        raise RelationError('SOURCE_NOT_LEAF', f'{scope_id} is a source leaf and may not render relations')
    lines = [f'<!-- BL-REL:BEGIN {scope_id} -->',
             f'> [!links]- {callout_title(scope_id, registry)}']
    for label, members in group_edges(edges):
        if len(lines) > 2:
            lines.append('>')
        lines.append(f'> **{label}**')
        for edge in members:
            lines.append('> - ' + _link_for(edge, registry, vault_prefix))
    lines.append(f'<!-- BL-REL:END {scope_id} -->')
    return '\n'.join(lines)


def _replace_span(text, start, end, replacement):
    return text[:start] + replacement + text[end:]


def _insert_at_body_end(text, section, region):
    body = text[section.body_start:section.body_end]
    trimmed = body.rstrip('\n')
    trailing = len(body) - len(trimmed)
    content = trimmed + ('\n\n' if trimmed else '') + region + '\n' + '\n' * trailing
    return _replace_span(text, section.body_start, section.body_end, content)


def refresh_relations(document, graph, registry, vault_prefix='', payload_spans=(), strict=True):
    """Replace or drop each managed relation region; never touch the body."""
    index = parse_sections(document, registry, payload_spans, strict_registry=False)
    edits = []
    for section in index.relation_scopes:
        region = render_relation_region(section.scope_id, graph, registry, vault_prefix)
        existing = [child for child in section.children if child.kind == 'REL']
        if len(existing) > 1:
            raise SectionError('SECTION_BOUNDARY_INVALID',
                               f'multiple BL-REL regions in {section.scope_id}',
                               line_of(document, section.start))
        if existing:
            if not region:
                start = existing[0].start
                end = existing[0].end
                if end < len(document) and document[end] == '\n':
                    end += 1
                edits.append((start, end, ''))
            else:
                edits.append((existing[0].start, existing[0].end, region))
        elif region:
            if strict and section.kind in {'SOURCE', 'SRC'}:
                raise RelationError('SOURCE_NOT_LEAF', f'{section.scope_id} may not own relations')
            prefix = '' if document[:section.body_end].endswith('\n\n') else '\n'
            edits.append((section.body_end, section.body_end, prefix + region + '\n'))
    return apply_edits(document, edits)


def apply_edits(text, edits):
    """Apply non-overlapping (start, end, replacement) edits in reverse order."""
    result = text
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        if start != end and replacement == '' and text[start:end].strip() == '':
            continue
        result = _replace_span(result, start, end, replacement)
    return result


def scope_documents(registry, documents):
    """Map path -> scopes that live in it, restricted to provided documents."""
    by_path = {}
    for entity in registry.entities.values():
        for location in entity.locations:
            if location.path in documents and location.scope_id:
                by_path.setdefault(location.path, set()).add(location.scope_id)
    return by_path


def refresh_documents(documents, graph, registry, vault_prefix=''):
    """documents: {path: text}; returns {path: new text} for relation-capable files."""
    updated = {}
    for path, text in documents.items():
        if '<!-- BL-' not in text:
            continue
        index_scopes = [section for section in parse_sections(text, registry, strict_registry=False).relation_scopes]
        if not index_scopes:
            continue
        new = refresh_relations(text, graph, registry, vault_prefix)
        if new != text:
            updated[path] = new
    return updated


def sync_course_relations(course, phase='working', write=True):
    """Derive the graph and rewrite every relation region in the course."""
    from _common import atomic_text, inside, read_json
    from entity_registry import Registry
    from relation_policy import derive_course_relations
    registry = Registry(course)
    graph = derive_course_relations(course, registry, phase=phase)
    documents = {}
    for entity in registry.entities.values():
        for location in entity.locations:
            if location.path in documents:
                continue
            path = inside(course, location.path)
            if path.is_file():
                documents[location.path] = path.read_text(encoding='utf-8')
    changed = []
    for path, text in refresh_documents(documents, graph, registry, registry.vault_prefix).items():
        if write:
            atomic_text(inside(course, path), text)
        changed.append(path)
    return {'phase': phase, 'edges': len(graph.edges), 'deferred': len(graph.deferred),
            'changed': sorted(changed), 'relation_hash': graph.relation_hash(),
            'registry_revision': registry.revision()}
