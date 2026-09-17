"""Entity identity and physical locations.

The registry is the single source for "which ID lives where". It is derived
from reviewed indexes (knowledge/chapter/transcript/card/exercise/path) plus a
persistent store that keeps ID allocation history and tombstones. Physical
locations are rebuildable; identity is not.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from pathlib import Path

from _common import inside, read_json, read_jsonl, write_json

STORE = '_工作区/实体注册表.json'
CONFIG = '_工作区/课程配置.json'
GRAPH_POLICY_VERSION = 'bl-typed-links-v2'
LINK_SCHEMA_VERSION = 2

ENTITY_TYPES = {'K', 'KP', 'EX', 'SRC', 'L', 'P', 'START', 'M', 'IDX', 'KC'}
SECTION_KIND = {'K': 'K', 'KP': 'KP', 'EX': 'EX', 'SRC': 'SRC', 'L': 'L', 'P': 'PATH',
                'START': 'START', 'M': 'MGMT', 'IDX': 'INDEX', 'KC': 'CHAPTER'}
ID_PATTERN = re.compile(r'[A-Za-z0-9-]+')


def type_of_id(entity_id):
    if entity_id.startswith('EX-'):
        return 'EX'
    if entity_id.startswith('KP-'):
        return 'KP'
    if entity_id.startswith('SRC-'):
        return 'SRC'
    if entity_id.startswith('KC-'):
        return 'KC'
    if entity_id.startswith('K-'):
        return 'K'
    if entity_id.startswith('L-'):
        return 'L'
    if entity_id.startswith('P-'):
        return 'P'
    if entity_id.startswith('IDX-'):
        return 'IDX'
    if entity_id.startswith('M-'):
        return 'M'
    if entity_id == 'START':
        return 'START'
    return None


def valid_id(entity_id):
    return bool(entity_id) and bool(ID_PATTERN.fullmatch(entity_id))


def teaching_section_id(lesson_id, knowledge_id):
    return f'{lesson_id}-{knowledge_id}'


@dataclass
class Location:
    id: str
    role: str
    owner_id: str
    path: str
    block_id: str
    section_kind: str
    section_id: str
    scope_id: str
    status: str = 'active'

    def as_json(self):
        return {'id': self.id, 'role': self.role, 'owner_id': self.owner_id, 'path': self.path,
                'block_id': self.block_id, 'section_kind': self.section_kind,
                'section_id': self.section_id, 'scope_id': self.scope_id, 'status': self.status}


@dataclass
class Entity:
    id: str
    type: str
    status: str = 'active'
    label: str = ''
    order: int = 0
    canonical_location_id: str | None = None
    locations: list = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def as_json(self):
        return {'id': self.id, 'type': self.type, 'status': self.status, 'label': self.label,
                'order': self.order, 'canonical_location_id': self.canonical_location_id,
                'locations': [loc.as_json() for loc in self.locations]}


def config(course):
    path = course / CONFIG
    data = read_json(path) if path.exists() else {}
    data.setdefault('link_mode', 'obsidian')
    data.setdefault('link_schema_version', 1)
    data.setdefault('graph_policy_version', GRAPH_POLICY_VERSION)
    data.setdefault('course_vault_prefix', '')
    data.setdefault('layout', 'working')
    return data


def is_v2(course):
    return config(course).get('link_schema_version', 1) >= LINK_SCHEMA_VERSION


def document(course, name, prefer_packaged=True):
    """Course-relative path of a canonical document in either layout."""
    candidates = []
    if prefer_packaged:
        candidates.append('课程文档/' + name)
    candidates.append(name)
    for candidate in candidates:
        if (course / candidate).is_file():
            return candidate
    return candidates[0] if prefer_packaged else candidates[-1]


def _lesson_index(course):
    path = course / '_工作区/章节索引.json'
    if not path.exists():
        return {}, []
    index = read_json(path)
    return {c['id']: c for c in index.get('chapters', [])}, list(index.get('lessons', []))


def _knowledge(course):
    path = course / '_工作区/知识索引.jsonl'
    return read_jsonl(path) if path.exists() else []


def _exercises(course):
    path = course / '_工作区/练习索引.jsonl'
    return read_jsonl(path) if path.exists() else []


def _cards(course):
    path = course / '_工作区/核心卡片计划.json'
    return read_json(path).get('cards', []) if path.exists() else []


def _path_index(course):
    path = course / '_工作区/路径索引.json'
    if path.exists():
        return read_json(path)
    return {}

def _management(course):
    path = course / '_工作区/管理文档索引.json'
    if path.exists():
        return read_json(path).get('documents', [])
    return []


class Registry:
    def __init__(self, course: Path):
        self.course = Path(course)
        self.entities: dict[str, Entity] = {}
        self.tombstones: dict[str, dict] = {}
        self.config = config(self.course)
        self.vault_prefix = str(self.config.get('course_vault_prefix') or '').strip('/')
        self.warnings: list[str] = []
        self.block_owners: dict[tuple, str] = {}
        self._build()

    # ------------------------------------------------------------------ build
    def _entity(self, entity_id, label='', order=0, data=None, status='active'):
        entity = self.entities.get(entity_id)
        if entity is None:
            entity = Entity(id=entity_id, type=type_of_id(entity_id), label=label or entity_id,
                            order=order, data=data or {})
            self.entities[entity_id] = entity
        else:
            if label:
                entity.label = label
            if order:
                entity.order = order
            if data:
                entity.data.update(data)
        if status != 'active':
            entity.status = status
        return entity

    def _add(self, entity_id, role, path, block_id, section_kind, section_id, scope_id,
             owner_id=None, status='active', label=None):
        entity = self._entity(entity_id, label=label)
        for location in entity.locations:
            if location.id == f'LOC-{section_id}':
                return location
        location = Location(id=f'LOC-{section_id}', role=role, owner_id=owner_id or entity_id,
                            path=path, block_id=block_id, section_kind=section_kind,
                            section_id=section_id, scope_id=scope_id, status=status)
        entity.locations.append(location)
        if entity.canonical_location_id is None and role in {'canonical', 'unit'}:
            entity.canonical_location_id = location.id
        return location

    def _build(self):
        store = self.course / STORE
        if store.exists():
            saved = read_json(store)
            self.tombstones = saved.get('tombstones', {})
            self._saved = saved
        else:
            self._saved = {}
        cards_done = False
        state_path = self.course / '_工作区/核心卡片状态.json'
        if state_path.exists():
            try:
                cards_done = read_json(state_path).get('status') == 'complete'
            except (OSError, ValueError):
                cards_done = False
        knowledge_file = document(self.course, '知识内容.md')
        cards_file = document(self.course, '核心知识点.md')
        path_file = document(self.course, '学习路径.md')
        chapters, lessons = _lesson_index(self.course)
        lesson_by_id = {l['id']: l for l in lessons}
        lessons = sorted(lessons, key=lambda l: (l.get('order', 0), l['id']))
        knowledge = [k for k in _knowledge(self.course) if k.get('status') != 'merged']
        merged = [k for k in _knowledge(self.course) if k.get('status') == 'merged']
        for entry in merged:
            self.tombstones.setdefault(entry['id'], {'merged_into': entry.get('merged_into')})

        # index containers
        self._entity('IDX-KNOWLEDGE', label='知识总目录')
        self._add('IDX-KNOWLEDGE', 'canonical', knowledge_file, 'IDX-KNOWLEDGE', 'INDEX',
                  'IDX-KNOWLEDGE', 'IDX-KNOWLEDGE', owner_id='IDX-KNOWLEDGE')
        self._entity('IDX-CORE', label='核心复习目录')
        self._add('IDX-CORE', 'canonical', cards_file, 'IDX-CORE', 'INDEX', 'IDX-CORE', 'IDX-CORE',
                  owner_id='IDX-CORE')

        for order, chapter in enumerate(sorted(chapters.values(), key=lambda c: (c.get('order', 0), c['id'])), 1):
            self._entity(chapter['id'], label=chapter.get('title', chapter['id']), order=order)
            self._add(chapter['id'], 'canonical', knowledge_file, chapter['id'], 'CHAPTER',
                      chapter['id'], chapter['id'], owner_id=chapter['id'])

        for order, lesson in enumerate(lessons, 1):
            self._entity(lesson['id'], label=lesson.get('title', lesson['id']), order=order,
                         data={'path': lesson['path'], 'status': lesson.get('status')})
            self._add(lesson['id'], 'canonical', lesson['path'], lesson['id'], 'L',
                      lesson['id'], lesson['id'], owner_id=lesson['id'],
                      status=lesson.get('status', 'pending'))

        for entry in knowledge:
            entity = self._entity(entry['id'], label=entry.get('title', entry['id']),
                                  order=entry.get('order', 0), data=entry)
            self._add(entry['id'], 'canonical', knowledge_file, entry['id'], 'K', entry['id'],
                      entry['id'], owner_id=entry['id'],
                      status='active' if entry.get('status') == 'verified' else 'staged')
            for lid in entry.get('lesson_ids', []):
                lesson = lesson_by_id.get(lid)
                if not lesson:
                    self.warnings.append(f'UNKNOWN_ENTITY: knowledge {entry["id"]} references lesson {lid}')
                    continue
                section_id = teaching_section_id(lid, entry['id'])
                self._add(entry['id'], 'teaching', lesson['path'], entry['id'], 'TEACH',
                          section_id, section_id, owner_id=lid, status='staged')

        for card in _cards(self.course):
            self._entity(card['card_id'], label=card.get('title') or card.get('concept_key', card['card_id']),
                         order=int(card.get('order', 0) or 0), data=card)
            self._add(card['card_id'], 'canonical', cards_file, card['card_id'], 'KP',
                      card['card_id'], card['card_id'], owner_id=card['card_id'], status='staged')
        # v1 packages only record the card id in the knowledge index.
        for entry in knowledge:
            card_id = entry.get('card_id')
            if not card_id or not entry.get('core') or card_id in self.entities:
                continue
            self._entity(card_id, label=entry.get('title', card_id), order=entry.get('order', 0),
                         data={'knowledge_ids': [entry['id']]})
            self._add(card_id, 'canonical', cards_file, card_id, 'KP', card_id, card_id,
                      owner_id=card_id, status='staged')

        for ex in _exercises(self.course):
            lesson = lesson_by_id.get(ex.get('owner_lesson_id'))
            if not lesson:
                self.warnings.append(f'UNKNOWN_ENTITY: exercise {ex["id"]} has unknown owner lesson')
                continue
            self._entity(ex['id'], label=ex.get('title') or ex['id'], order=ex.get('order', 0), data=ex,
                         status='staged' if ex.get('status') == 'used' else 'reserved')
            self._add(ex['id'], 'canonical', lesson['path'], ex['id'], 'EX', ex['id'], ex['id'],
                      owner_id=ex['owner_lesson_id'], status='staged' if ex.get('status') == 'used' else 'reserved')

        path_index = _path_index(self.course)
        path_id = path_index.get('path_id', 'P-MAIN')
        self._entity(path_id, label=path_index.get('title', '学习路径'))
        self._add(path_id, 'canonical', path_file, path_id, 'PATH', path_id, path_id,
                  owner_id=path_id, status='staged' if path_index else 'active')
        steps = path_index.get('steps')
        if not steps:
            steps = [{'id': f'{path_id}-S{index:03d}', 'title': lesson.get('title', lesson['id']),
                      'lessons': [lesson['id']], 'order': index}
                     for index, lesson in enumerate(lessons, 1)]
        for step in steps:
            self._entity(step['id'], label=step.get('title', step['id']), order=step.get('order', 0))
            self._add(step['id'], 'canonical', path_file, step['id'], 'STEP', step['id'], step['id'],
                      owner_id=path_id, status='staged' if path_index else 'active')

        self._entity('START', label='开始学习')
        self._add('START', 'canonical', document(self.course, '开始学习.md'), 'START', 'START',
                  'START', 'START', owner_id='START')

        from _common import extraction_path
        transcript_index = self.course / '_工作区/转写索引.json'
        source_manifest = self.course / '_工作区/资料索引.json'
        entries = read_json(transcript_index).get('sources', []) if transcript_index.exists() else []
        names = {s['id']: s.get('name', s['id'])
                 for s in (read_json(source_manifest).get('sources', []) if source_manifest.exists() else [])}
        for entry in entries:
            self._entity(entry['source_id'], label=f"{names.get(entry['source_id'], entry['source_id'])}：资料转写")
            self._add(entry['source_id'], 'canonical', entry['path'], entry['source_id'], 'SOURCE',
                      entry['source_id'], entry['source_id'], owner_id=entry['source_id'])
            mapping_path = None
            try:
                source = next((s for s in read_json(source_manifest).get('sources', [])
                               if s['id'] == entry['source_id']), None)
                if source:
                    mapping_path = extraction_path(self.course, source) / '定位映射.json'
            except (OSError, ValueError, KeyError):
                mapping_path = None
            if mapping_path and mapping_path.exists():
                mapping = read_json(mapping_path)
                for unit in mapping.get('units', []):
                    uid = f"{entry['source_id']}-{unit['id']}"
                    self._entity(uid, label=f"{names.get(entry['source_id'], entry['source_id'])} · {unit.get('locator', unit['id'])}")
                    self._add(uid, 'unit', entry['path'], uid, 'SRC', uid, uid,
                              owner_id=entry['source_id'])

        for doc in _management(self.course):
            self._entity(doc['id'], label=doc.get('title', doc['id']), order=doc.get('order', 0), data=doc)
            self._add(doc['id'], 'canonical', doc.get('path', doc['id'] + '.md'), doc['id'], 'MGMT',
                      doc['id'], doc['id'], owner_id=doc['id'])

        # Saved registry keeps locations that derivations can no longer see
        # (for example a purged extraction cache in a packaged course).
        for entity_id, payload in (self._saved.get('entities') or {}).items():
            if entity_id not in self.entities:
                entity = Entity(id=entity_id, type=payload.get('type') or type_of_id(entity_id) or 'K',
                                status=payload.get('status', 'active'), label=payload.get('label', ''),
                                order=payload.get('order', 0),
                                canonical_location_id=payload.get('canonical_location_id'))
                self.entities[entity_id] = entity
            else:
                entity = self.entities[entity_id]
            known = {location.id for location in entity.locations}
            for item in payload.get('locations', []):
                if item.get('id') in known:
                    continue
                entity.locations.append(Location(id=item['id'], role=item.get('role', 'canonical'),
                                                 owner_id=item.get('owner_id', entity_id),
                                                 path=item.get('path', ''), block_id=item.get('block_id', ''),
                                                 section_kind=item.get('section_kind', SECTION_KIND.get(entity.type, 'K')),
                                                 section_id=item.get('section_id', entity_id),
                                                 scope_id=item.get('scope_id', entity_id),
                                                 status=item.get('status', 'active')))
            if entity.canonical_location_id is None and payload.get('canonical_location_id'):
                entity.canonical_location_id = payload['canonical_location_id']
        # Status normalization: active means the entity's file/section already exists.
        def exists(path):
            return bool(path) and (self.course / path).is_file()

        for entity in self.entities.values():
            if entity.type == 'K':
                entity.status = 'active' if entity.data.get('status') == 'verified' else 'staged'
            elif entity.type == 'L':
                entity.status = 'active' if entity.data.get('status') == 'complete' else 'staged'
            elif entity.type == 'EX':
                if entity.data.get('status') != 'used':
                    entity.status = 'reserved'
                else:
                    owner = self.entities.get(entity.data.get('owner_lesson_id'))
                    entity.status = 'active' if owner and owner.status == 'active' else 'staged'
            elif entity.type == 'KP':
                entity.status = 'active' if cards_done else 'staged'
            else:
                entity.status = 'active' if any(exists(location.path) for location in entity.locations) \
                    else 'staged'
        for entity_id, tomb in self.tombstones.items():
            if entity_id in self.entities:
                self.entities[entity_id].status = 'merged'
                self.entities[entity_id].data['merged_into'] = tomb.get('merged_into')

    # ------------------------------------------------------------------ query
    def has(self, entity_id):
        return entity_id in self.entities

    def entity(self, entity_id):
        if entity_id not in self.entities:
            raise KeyError(f'UNKNOWN_ENTITY: {entity_id}')
        return self.entities[entity_id]

    def type_of(self, entity_id):
        entity = self.entities.get(entity_id)
        return entity.type if entity else None

    def status_of(self, entity_id):
        entity = self.entities.get(entity_id)
        return entity.status if entity else None

    def label(self, entity_id):
        entity = self.entities.get(entity_id)
        return entity.label if entity and entity.label else entity_id

    def canonical(self, entity_id):
        entity = self.entity(entity_id)
        for location in entity.locations:
            if location.id == entity.canonical_location_id:
                return location
        raise KeyError(f'CANONICAL_LOCATION_CONFLICT: {entity_id}')

    def location(self, location_id):
        for entity in self.entities.values():
            for location in entity.locations:
                if location.id == location_id:
                    return location
        raise KeyError(f'UNKNOWN_ENTITY: location {location_id}')

    def locations(self, entity_id):
        return list(self.entity(entity_id).locations)

    def owner_locations(self, owner_id):
        return [location for entity in self.entities.values() for location in entity.locations
                if location.owner_id == owner_id]

    def teaching_location(self, lesson_id, knowledge_id):
        section_id = teaching_section_id(lesson_id, knowledge_id)
        for location in self.locations(knowledge_id):
            if location.section_id == section_id:
                return location
        raise KeyError(f'UNKNOWN_ENTITY: teaching location {section_id}')

    def resolve_location(self, entity_id, location_id=None):
        if location_id:
            location = self.location(location_id)
            if location.owner_id != entity_id and entity_id not in (location.id or ''):
                entity = self.entity(entity_id)
                if location not in entity.locations:
                    raise KeyError(f'INVALID_LINK_TARGET: {entity_id} has no location {location_id}')
            return location
        return self.canonical(entity_id)

    def path_of(self, location):
        return location.path

    def block_owner(self, path, block_id):
        return self.block_owners.get((path, block_id))

    def register_blocks(self, path, block_ids, owner_scope):
        for block in block_ids:
            key = (path, block)
            self.block_owners.setdefault(key, owner_scope)

    def active_entities(self, entity_type=None, statuses=('active', 'staged')):
        return [entity for entity in self.entities.values()
                if (entity_type is None or entity.type == entity_type) and entity.status in statuses]

    def outgoing_targets(self, entity_id):
        return sorted({edge for edge in self.entities.get(entity_id, Entity(entity_id, '')).data.get('targets', [])
                       if edge})

    def revision(self):
        payload = {entity_id: entity.as_json() for entity_id, entity in sorted(self.entities.items())}
        payload['_tombstones'] = self.tombstones
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def structure_revision(self):
        """Identity/location revision: stable across status changes elsewhere."""
        payload = {}
        for entity_id, entity in sorted(self.entities.items()):
            payload[entity_id] = {
                'type': entity.type, 'label': entity.label,
                'canonical_location_id': entity.canonical_location_id,
                'locations': [{key: value for key, value in location.as_json().items() if key != 'status'}
                              for location in entity.locations]}
        payload['_tombstones'] = self.tombstones
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def as_json(self):
        return {'schema_version': 1, 'graph_policy_version': GRAPH_POLICY_VERSION,
                'revision': self.revision(),
                'entities': {entity_id: entity.as_json() for entity_id, entity in sorted(self.entities.items())},
                'tombstones': self.tombstones}

    def save(self):
        write_json(self.course / STORE, self.as_json())


def load(course):
    return Registry(Path(course))


def validate_locations(registry, documents):
    """documents: {course-relative path: text}. Checks block uniqueness per file."""
    diagnostics = []
    for path, text in documents.items():
        from entity_sections import block_occurrences
        seen = {}
        for block, offset in block_occurrences(text):
            if block in seen:
                diagnostics.append({'code': 'DUPLICATE_BLOCK', 'file': path,
                                    'line': text.count('\n', 0, offset) + 1, 'block': block,
                                    'expected': 'exactly one block definition',
                                    'actual': f'duplicate {block}'})
            seen[block] = offset
        targets = registry.block_owners
        for block, offset in seen.items():
            owner = None
            for entity in registry.entities.values():
                for location in entity.locations:
                    if location.path == path and location.block_id == block:
                        owner = location
            if owner is None and (path, block) not in targets:
                diagnostics.append({'code': 'UNKNOWN_ENTITY', 'file': path,
                                    'line': text.count('\n', 0, offset) + 1, 'block': block,
                                    'expected': 'registered entity block',
                                    'actual': 'unregistered block definition'})
    return diagnostics


def course_lock_path(course):
    return Path(course) / '_工作区/转写任务.lock'
