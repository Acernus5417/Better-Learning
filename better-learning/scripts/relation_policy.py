"""Typed relation policy: four top-level types, whitelisted subtypes, one matrix.

Only reviewed facts become edges. Determinism means the transformation from
approved indexes to relations and Markdown is reproducible; the program never
invents teaching semantics by string matching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

GRAPH_POLICY_VERSION = 'bl-typed-links-v2'

NAV, TEACH, DEP, SOURCE = 'NAV', 'TEACH', 'DEP', 'SOURCE'
TYPES = (NAV, TEACH, DEP, SOURCE)

SUBTYPES = {
    NAV: {'entry', 'step', 'previous', 'next', 'path', 'owner', 'index', 'management'},
    TEACH: {'lesson', 'knowledge', 'core', 'summarizes', 'practice', 'assesses', 'exercise'},
    DEP: {'prerequisite'},
    SOURCE: {'evidence'},
}

#: (source type, target type) -> allowed subtypes. Everything else is forbidden.
ALLOWED = {
    ('K', 'K'): {('DEP', 'prerequisite')},
    ('K', 'KP'): {('TEACH', 'core')},
    ('K', 'SRC'): {('SOURCE', 'evidence')},
    ('K', 'L'): {('TEACH', 'lesson')},
    ('KP', 'K'): {('TEACH', 'summarizes')},
    ('KP', 'EX'): {('TEACH', 'practice')},
    ('EX', 'K'): {('TEACH', 'assesses')},
    ('EX', 'SRC'): {('SOURCE', 'evidence')},
    ('EX', 'L'): {('NAV', 'owner')},
    ('L', 'K'): {('TEACH', 'knowledge')},
    ('L', 'EX'): {('TEACH', 'exercise')},
    ('L', 'L'): {('NAV', 'previous'), ('NAV', 'next')},
    ('L', 'P'): {('NAV', 'path')},
    ('L', 'START'): {('NAV', 'entry')},
    ('P', 'L'): {('NAV', 'step')},
    ('P', 'START'): {('NAV', 'entry')},
    ('START', 'KP'): {('NAV', 'entry')},
    ('START', 'L'): {('NAV', 'entry')},
    ('START', 'P'): {('NAV', 'entry')},
    ('START', 'IDX'): {('NAV', 'entry')},
    ('START', 'M'): {('NAV', 'management')},
    ('M', 'K'): {('NAV', 'management')},
    ('M', 'KP'): {('NAV', 'management')},
    ('M', 'EX'): {('NAV', 'management')},
    ('M', 'SRC'): {('NAV', 'management')},
    ('M', 'L'): {('NAV', 'management')},
    ('M', 'P'): {('NAV', 'management')},
    ('M', 'IDX'): {('NAV', 'management')},
    ('M', 'START'): {('NAV', 'entry')},
    ('M', 'M'): {('NAV', 'management')},
    ('IDX', 'KP'): {('NAV', 'index')},
    ('IDX', 'K'): {('NAV', 'index')},
    ('IDX', 'KC'): {('NAV', 'index')},
    ('IDX', 'START'): {('NAV', 'entry')},
    ('KC', 'K'): {('NAV', 'index')},
}

#: display order and labels inside one folded callout
DISPLAY_GROUPS = (
    ('前置 · DEP', lambda e: e['type'] == DEP),
    ('学习 · TEACH', lambda e: e['type'] == TEACH and e['subtype'] in {'lesson', 'knowledge'}),
    ('来源 · SOURCE', lambda e: e['type'] == SOURCE),
    ('核心复习 · TEACH.core', lambda e: e['type'] == TEACH and e['subtype'] in {'core', 'summarizes'}),
    ('练习 · TEACH.practice/exercise', lambda e: e['type'] == TEACH and e['subtype'] in {'practice', 'exercise'}),
    ('考查知识 · TEACH.assesses', lambda e: e['type'] == TEACH and e['subtype'] == 'assesses'),
    ('导航 · NAV', lambda e: e['type'] == NAV),
)

CALLER_TITLES = {
    'K': '知识关系', 'KP': '复习关系', 'EX': '练习关系', 'L': '章节导航与练习',
    'TEACH': '本节知识', 'PATH': '路线导航', 'STEP': '学习安排', 'START': '学习入口',
    'INDEX': '复习导航', 'MGMT': '报告导航', 'CHAPTER': '章节导航',
}

OPTIONAL_KEY_FIELDS = ('context_knowledge_id', 'role')


class RelationError(ValueError):
    def __init__(self, code, message, detail=None):
        super().__init__(f'{code}: {message}')
        self.code = code
        self.message = message
        self.detail = detail or {}


def record_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def canonical_key(edge):
    parts = [edge.get('type', ''), edge.get('subtype', ''), edge.get('source_id', ''),
             edge.get('source_location_id') or '', edge.get('source_scope_id') or '',
             edge.get('target_id', ''), edge.get('target_location_id') or '',
             edge.get('context_knowledge_id') or '', edge.get('role') or '']
    return '|'.join(parts)


def relation_id(edge):
    return 'R-' + hashlib.sha256(canonical_key(edge).encode()).hexdigest()[:20]


def make_edge(source_id, target_id, type_, subtype, *, registry=None,
              source_location_id=None, target_location_id=None, source_scope_id=None,
              context_knowledge_id='', role='', order=0, provenance=None, status='active'):
    if type_ not in TYPES:
        raise RelationError('RELATION_TYPE_FORBIDDEN', f'unknown type {type_}')
    if subtype not in SUBTYPES[type_]:
        raise RelationError('RELATION_TYPE_FORBIDDEN', f'{type_}.{subtype} is not a whitelisted subtype')
    edge = {'type': type_, 'subtype': subtype, 'source_id': source_id, 'target_id': target_id,
            'source_location_id': source_location_id, 'target_location_id': target_location_id,
            'source_scope_id': source_scope_id or source_id,
            'context_knowledge_id': context_knowledge_id, 'role': role, 'order': order,
            'status': status}
    if registry is not None:
        edge = attach_locations(edge, registry)
    edge['relation_key'] = canonical_key(edge)
    edge['relation_id'] = relation_id(edge)
    edge['provenance'] = provenance or {}
    return edge


def attach_locations(edge, registry):
    source, target = edge['source_id'], edge['target_id']
    if not registry.has(source):
        raise RelationError('UNKNOWN_ENTITY', f'source {source} is not registered')
    if not registry.has(target):
        raise RelationError('UNKNOWN_ENTITY', f'target {target} is not registered')
    if not edge.get('source_location_id'):
        edge['source_location_id'] = registry.canonical(source).id
    if not edge.get('target_location_id'):
        edge['target_location_id'] = registry.canonical(target).id
    if not edge.get('source_scope_id'):
        try:
            edge['source_scope_id'] = registry.location(edge['source_location_id']).scope_id
        except KeyError:
            edge['source_scope_id'] = source
    return edge


def check_matrix(edge, registry):
    source_type = registry.type_of(edge['source_id'])
    target_type = registry.type_of(edge['target_id'])
    allowed = ALLOWED.get((source_type, target_type), set())
    if (edge['type'], edge['subtype']) not in allowed:
        raise RelationError(
            'RELATION_TYPE_FORBIDDEN',
            f"{source_type} {edge['source_id']} -{edge['type']}.{edge['subtype']}-> "
            f"{target_type} {edge['target_id']} is outside the allow matrix",
            {'source_type': source_type, 'target_type': target_type})
    if edge['source_id'] == edge['target_id']:
        raise RelationError('DEPENDENCY_CYCLE', 'self relation is forbidden', {'entity': edge['source_id']})
    if source_type == 'SRC':
        raise RelationError('SOURCE_NOT_LEAF', f'SRC {edge["source_id"]} may not own out-edges')
    if not edge.get('provenance'):
        raise RelationError('RELATION_PROVENANCE_MISSING',
                            f'{edge["relation_key"]} has no reviewed fact behind it')
    return edge


def validate_dep_dag(edges):
    deps = {}
    for edge in edges:
        if edge['type'] == DEP:
            deps.setdefault(edge['source_id'], set()).add(edge['target_id'])
    visiting, done = set(), set()
    for node in sorted(deps):
        stack = [(node, iter(sorted(deps.get(node, ()))))]
        path = [node]
        visiting.add(node)
        while stack:
            current, children = stack[-1]
            advanced = False
            for child in children:
                if child in visiting:
                    cycle = path[path.index(child):] + [child] if child in path else path + [child]
                    raise RelationError('DEPENDENCY_CYCLE', ' -> '.join(cycle), {'cycle': cycle})
                if child not in done and child in deps:
                    stack.append((child, iter(sorted(deps.get(child, ())))))
                    path.append(child)
                    visiting.add(child)
                    advanced = True
                    break
            if not advanced:
                visiting.discard(current)
                done.add(current)
                stack.pop()
                if path:
                    path.pop()
    return True


def _logical_entity(edge, side, registry):
    """Path steps and teaching occurrences resolve to their owning entity."""
    if registry is None:
        return edge[f'{side}_id']
    location_id = edge.get(f'{side}_location_id')
    if not location_id:
        return edge[f'{side}_id']
    try:
        location = registry.location(location_id)
    except KeyError:
        return edge[f'{side}_id']
    if location.section_kind in {'STEP', 'TEACH'}:
        return location.owner_id
    return edge[f'{side}_id']


def validate_reciprocal_contracts(edges, registry=None):
    keys = {(edge['type'], edge['subtype'],
             _logical_entity(edge, 'source', registry), _logical_entity(edge, 'target', registry))
            for edge in edges}
    missing = []
    for edge in edges:
        etype, subtype = edge['type'], edge['subtype']
        source = _logical_entity(edge, 'source', registry)
        target = _logical_entity(edge, 'target', registry)
        pair = None
        if (etype, subtype) == (TEACH, 'lesson'):
            pair = (TEACH, 'knowledge', target, source)
        elif (etype, subtype) == (TEACH, 'knowledge'):
            pair = (TEACH, 'lesson', target, source)
        elif (etype, subtype) == (TEACH, 'core'):
            pair = (TEACH, 'summarizes', target, source)
        elif (etype, subtype) == (TEACH, 'summarizes'):
            pair = (TEACH, 'core', target, source)
        elif (etype, subtype) == (TEACH, 'exercise'):
            pair = (NAV, 'owner', target, source)
        elif (etype, subtype) == (NAV, 'owner'):
            pair = (TEACH, 'exercise', target, source)
        elif (etype, subtype) == (NAV, 'step'):
            pair = (NAV, 'path', target, source)
        elif (etype, subtype) == (NAV, 'path'):
            pair = (NAV, 'step', target, source)
        if pair and pair not in keys:
            missing.append({'code': 'RELATION_PROJECTION_MISMATCH',
                            'expected': list(pair), 'actual': 'missing reciprocal edge',
                            'source_id': source, 'target_id': target,
                            'message': f'required reciprocal edge is missing: {pair}',
                            'relation_key': edge['relation_key']})
    return missing


def validate_card_exercise_mapping(edges, registry, exercises):
    by_id = {ex['id']: ex for ex in exercises}
    knowledge_by_card = {}
    for edge in edges:
        if edge['type'] == TEACH and edge['subtype'] == 'summarizes':
            knowledge_by_card.setdefault(edge['source_id'], set()).add(edge['target_id'])
    problems = []
    for edge in edges:
        if edge['type'] == TEACH and edge['subtype'] == 'practice':
            card, exercise = edge['source_id'], edge['target_id']
            entry = by_id.get(exercise)
            if not entry:
                problems.append({'code': 'UNKNOWN_ENTITY', 'relation_key': edge['relation_key'],
                                 'detail': f'{card} links unregistered exercise {exercise}'})
                continue
            if entry.get('status') != 'used':
                problems.append({'code': 'RELATION_PROVENANCE_MISSING', 'relation_key': edge['relation_key'],
                                 'detail': f'reserved exercise {exercise} is not a valid target'})
            overlap = set(entry.get('assesses', [])) & knowledge_by_card.get(card, set())
            if not overlap:
                problems.append({'code': 'RELATION_PROVENANCE_MISSING', 'relation_key': edge['relation_key'],
                                 'detail': f'{exercise} assesses none of the knowledge summarized by {card}'})
    return problems


def display_key(edge):
    return (edge.get('order', 0), edge['target_id'], edge.get('target_location_id') or '')


def group_edges(edges):
    """Group edges for display in the fixed order; unknown groups go last."""
    groups = []
    used = set()
    for label, predicate in DISPLAY_GROUPS:
        members = [edge for edge in edges if predicate(edge)]
        if members:
            groups.append((label, sorted(members, key=display_key)))
            used.update(id(edge) for edge in members)
    rest = [edge for edge in edges if id(edge) not in used]
    if rest:
        groups.append(('其他 · OTHER', sorted(rest, key=display_key)))
    return groups


def callout_title(scope_id, registry):
    kinds = [location.section_kind for entity in registry.entities.values()
             for location in entity.locations if location.scope_id == scope_id]
    kind = kinds[0] if kinds else None
    return CALLER_TITLES.get(kind, '知识关系')


@dataclass
class Graph:
    edges: list = field(default_factory=list)
    deferred: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)

    def outgoing_for_scope(self, scope_id):
        return [edge for edge in self.edges if edge['source_scope_id'] == scope_id]

    def outgoing(self, entity_id):
        return [edge for edge in self.edges if edge['source_id'] == entity_id]

    def incoming(self, entity_id):
        return [edge for edge in self.edges if edge['target_id'] == entity_id]

    def out_degree(self, entity_id):
        return len(self.outgoing(entity_id))

    def expected_keys(self, scope_id):
        return {edge['relation_key'] for edge in self.outgoing_for_scope(scope_id)}

    def match_authorized_link(self, scope_id, target_id, target_location_id=None, surface='body'):
        for edge in self.outgoing_for_scope(scope_id):
            if edge['target_id'] != target_id:
                continue
            if target_location_id and edge.get('target_location_id') != target_location_id:
                continue
            surfaces = edge.get('provenance', {}).get('surfaces')
            if surfaces and surface not in surfaces:
                continue
            return edge
        return None

    def as_json(self):
        return {'graph_policy_version': GRAPH_POLICY_VERSION,
                'edges': [{key: edge[key] for key in
                           ('relation_id', 'relation_key', 'type', 'subtype', 'source_id',
                            'source_location_id', 'source_scope_id', 'target_id', 'target_location_id',
                            'context_knowledge_id', 'role', 'order', 'status', 'provenance')}
                          for edge in self.edges],
                'deferred': [edge['relation_key'] for edge in self.deferred],
                'relation_hash': self.relation_hash()}

    def relation_hash(self):
        payload = sorted(canonical_key(edge) + '|' + (edge.get('provenance', {}).get('input_hash') or '')
                         for edge in self.edges)
        return hashlib.sha256('\n'.join(payload).encode()).hexdigest()


def _phase_statuses(phase):
    if phase in {'working', 'draft'}:
        return {'active'}
    if phase in {'staged', 'staging'}:
        return {'active', 'staged'}
    if phase in {'final', 'packaged'}:
        return {'active'}
    raise RelationError('STALE_MANIFEST', f'unknown phase {phase}')


def select_phase(graph, registry, phase):
    statuses = _phase_statuses(phase)
    kept, deferred = [], []
    for edge in graph.edges:
        target_status = registry.status_of(edge['target_id']) or 'unknown'
        source_status = registry.status_of(edge['source_id']) or 'unknown'
        if phase in {'final', 'packaged'}:
            if target_status not in statuses:
                raise RelationError(
                    'MISSING_TARGET_BLOCK',
                    f"{edge['relation_key']} target {edge['target_id']} is {target_status}, not active",
                    {'relation_key': edge['relation_key'], 'target_status': target_status})
            if source_status == 'merged':
                raise RelationError('UNKNOWN_ENTITY',
                                    f"{edge['relation_key']} source {edge['source_id']} is merged away")
        if target_status in statuses and source_status in statuses | {'active'}:
            kept.append(edge)
        else:
            deferred.append(edge)
    graph.edges, graph.deferred = kept, deferred
    return graph


def derive_relations(data, registry, phase='working'):
    """Turn reviewed indexes into typed edges. Never guesses semantics."""
    edges = []

    def add(source_id, target_id, type_, subtype, **kwargs):
        provenance = kwargs.pop('provenance', None)
        edges.append(make_edge(source_id, target_id, type_, subtype, registry=registry,
                               provenance=provenance, **kwargs))

    for entry in data.get('knowledge', []):
        if entry.get('status') == 'merged':
            continue
        record = {'kind': 'knowledge_index', 'record_id': entry['id'], 'field': 'record',
                  'input_hash': record_hash(entry)}
        kid = entry['id']
        for dep in entry.get('prerequisites', []):
            add(kid, dep, DEP, 'prerequisite', order=10,
                provenance={**record, 'field': 'prerequisites'})
        for ref in entry.get('source_refs', []):
            unit = f"{ref['source_id']}-{ref['unit_id']}"
            add(kid, unit, SOURCE, 'evidence', order=20,
                provenance={**record, 'field': 'source_refs'})
        for lid in entry.get('lesson_ids', []):
            try:
                location = registry.teaching_location(lid, kid)
            except KeyError:
                continue
            add(kid, lid, TEACH, 'lesson', order=30, target_location_id=location.id,
                context_knowledge_id=kid, provenance={**record, 'field': 'lesson_ids'})
            add(lid, kid, TEACH, 'knowledge', order=30, source_location_id=location.id,
                source_scope_id=location.scope_id, role='assigned',
                provenance={**record, 'field': 'lesson_ids'})

    for card in data.get('cards', []):
        record = {'kind': 'card_plan', 'record_id': card['card_id'], 'field': 'record',
                  'input_hash': record_hash(card)}
        for index, kid in enumerate(sorted(set(card.get('knowledge_ids', []))), 1):
            add(kid, card['card_id'], TEACH, 'core', order=40 + index, provenance=record)
            add(card['card_id'], kid, TEACH, 'summarizes', order=40 + index, provenance=record)
        for index, exid in enumerate(card.get('exercise_ids', []), 1):
            add(card['card_id'], exid, TEACH, 'practice', order=60 + index, provenance=record)

    for ex in data.get('exercises', []):
        if ex.get('status') != 'used':
            continue
        record = {'kind': 'exercise_index', 'record_id': ex['id'], 'field': 'record',
                  'input_hash': record_hash(ex)}
        exid, owner = ex['id'], ex['owner_lesson_id']
        for kid in sorted(set(ex.get('assesses', []))):
            add(exid, kid, TEACH, 'assesses', order=70, provenance=record)
        for ref in ex.get('source_refs', []):
            add(exid, f"{ref['source_id']}-{ref['unit_id']}", SOURCE, 'evidence', order=20,
                provenance={**record, 'field': 'source_refs'})
        add(owner, exid, TEACH, 'exercise', order=40, provenance=record)
        add(exid, owner, NAV, 'owner', order=80, provenance=record)

    path = data.get('path') or {}
    if path:
        path_id = path.get('path_id', 'P-MAIN')
        record = {'kind': 'path_index', 'record_id': path_id, 'field': 'steps',
                  'input_hash': record_hash(path)}
        for step in path.get('steps', []):
            for lesson_id in step.get('lessons', []):
                add(step['id'], lesson_id, NAV, 'step', order=step.get('order', 0), provenance=record)
                add(lesson_id, path_id, NAV, 'path', order=step.get('order', 0), provenance=record)
        add(path_id, 'START', NAV, 'entry', order=90, provenance=record)

    for doc in data.get('management', []):
        record = {'kind': 'management_index', 'record_id': doc['id'], 'field': 'links',
                  'input_hash': record_hash(doc)}
        if doc.get('entry'):
            add(doc['id'], 'START', NAV, 'entry', order=95, provenance=record)
        for link in doc.get('links', []):
            add(doc['id'], link['target_id'], NAV, link.get('subtype', 'management'),
                order=link.get('order', 50), role=link.get('role', ''),
                provenance={**record, 'detail': link.get('reason', '')})

    start = data.get('start') or {}
    record = {'kind': 'start_entry', 'record_id': 'START', 'field': 'entries',
              'input_hash': record_hash(start)}
    for index, target in enumerate(start.get('entries', []), 1):
        add('START', target['target_id'], NAV, target.get('subtype', 'entry'),
            order=index, provenance=record)
    for index, target in enumerate(start.get('management', []), 1):
        add('START', target, NAV, 'management', order=100 + index, provenance=record)

    for index_container in data.get('indexes', []):
        record = {'kind': 'index_container', 'record_id': index_container['id'], 'field': 'members',
                  'input_hash': record_hash(index_container)}
        for index, member in enumerate(index_container.get('members', []), 1):
            add(index_container['id'], member, NAV, 'index', order=index, provenance=record)
        if index_container.get('entry'):
            add(index_container['id'], 'START', NAV, 'entry', order=99, provenance=record)

    for chapter in data.get('chapters', []):
        record = {'kind': 'chapter_index', 'record_id': chapter['id'], 'field': 'knowledge_ids',
                  'input_hash': record_hash(chapter)}
        for index, kid in enumerate(chapter.get('knowledge_ids', []), 1):
            add(chapter['id'], kid, NAV, 'index', order=index, provenance=record)

    for edge in edges:
        check_matrix(edge, registry)
    validate_dep_dag(edges)
    graph = Graph(edges=edges)
    graph.edges = unique_by_canonical_key(edges)
    return select_phase(graph, registry, phase)


def unique_by_canonical_key(edges):
    seen, result = set(), []
    for edge in edges:
        if edge['relation_key'] in seen:
            continue
        seen.add(edge['relation_key'])
        result.append(edge)
    return sorted(result, key=lambda e: (e['source_scope_id'], e['type'], e['order'],
                                         e['target_id'], e['target_location_id'] or ''))


def reviewed_data(course):
    """Collect the reviewed indexes into one bundle; missing files are allowed."""
    from entity_registry import _cards, _exercises, _knowledge, _lesson_index, _management, _path_index
    from _common import read_json
    chapters, lessons = _lesson_index(course)
    knowledge = _knowledge(course)
    chapter_members = {}
    for entry in knowledge:
        if entry.get('status') == 'merged':
            continue
        chapter_members.setdefault(entry.get('chapter_id'), []).append(entry['id'])
    path = _path_index(course)
    if not path:
        path = {'path_id': 'P-MAIN', 'title': '学习路径', 'derived': True,
                'steps': [{'id': f'P-MAIN-S{index:03d}', 'title': lesson.get('title', lesson['id']),
                           'lessons': [lesson['id']], 'order': index}
                          for index, lesson in enumerate(
                              sorted(lessons, key=lambda l: (l.get('order', 0), l['id'])), 1)]}
    management = _management(course)
    start_path = course / '_工作区/开始入口.json'
    start = read_json(start_path) if start_path.exists() else {'entries': [
        {'target_id': 'P-MAIN', 'subtype': 'entry'},
        {'target_id': 'IDX-CORE', 'subtype': 'entry'},
    ]}
    indexes = [{'id': 'IDX-KNOWLEDGE', 'members': sorted(chapter_members),
                'entry': start.get('index_entry', True)},
               {'id': 'IDX-CORE', 'members': [card['card_id'] for card in _cards(course)]}]
    return {'knowledge': knowledge, 'cards': _cards(course), 'exercises': _exercises(course),
            'path': path, 'management': management, 'start': start, 'indexes': indexes,
            'chapters': [{'id': cid, 'knowledge_ids': sorted(ids), 'title': chapters.get(cid, {}).get('title', cid)}
                         for cid, ids in sorted(chapter_members.items())]}


def derive_course_relations(course, registry, phase='working'):
    return derive_relations(reviewed_data(course), registry, phase)
