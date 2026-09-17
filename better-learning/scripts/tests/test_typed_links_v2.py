"""Acceptance tests for the v2 typed link graph (design §16.1).

Run from the scripts directory:
    py -3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from _common import atomic_text, read_json, read_jsonl, sha256, write_json  # noqa: E402
from entity_registry import Registry  # noqa: E402
from entity_sections import SectionError, entity_section, parse_sections  # noqa: E402
from obsidian_links import (parse_wikilinks, validate_candidate, validate_links,  # noqa: E402
                            validate_obsidian_links)
from relation_policy import (DEP, Graph, make_edge, select_phase, validate_dep_dag,  # noqa: E402
                             validate_reciprocal_contracts)
from relation_renderer import render_entity_link, sync_course_relations  # noqa: E402

LESSON = '''---
type: lesson
id: L-03
title: 特征值
order: 3
status: complete
---

<!-- BL-L:BEGIN L-03 -->

# 第 3 章：特征值

L-03 ^L-03

本章目标：理解特征值条件。

<!-- BL-TEACH:BEGIN L-03-K-02-01 -->

## 回顾：矩阵乘法

K-02-01 ^K-02-01

矩阵乘法把两个矩阵组合成一个新矩阵。

<!-- BL-TEACH:END L-03-K-02-01 -->

<!-- BL-TEACH:BEGIN L-03-K-03-04 -->

## 从变换理解特征值

K-03-04 ^K-03-04

若方阵 A 与非零向量 v 满足下式，则 λ 是 A 的特征值。

$$
Av = \\lambda v, \\qquad v \\ne 0
$$

| 条件 | 说明 |
|---|---|
| v ≠ 0 | 不可省略 |

<!-- BL-TEACH:END L-03-K-03-04 -->

<!-- BL-EX:BEGIN EX-L03-001 -->

## 自测：验证一个方向

EX-L03-001 ^EX-L03-001

令 A = diag(2, 3)，v = (1, 0)ᵀ。验证 v 是否为特征向量。

**答案与解释**：Av = (2, 0)ᵀ = 2v。

<!-- BL-EX:END EX-L03-001 -->

<!-- BL-L:END L-03 -->
'''

CARDS = '''# 核心知识点

<!-- BL-INDEX:BEGIN IDX-CORE -->

## 复习目录

IDX-CORE ^IDX-CORE

核心复习目录；卡片以稳定 KP ID 定位。

<!-- BL-INDEX:END IDX-CORE -->

<!-- BL-KP:BEGIN KP-03-02-01 -->

## 特征值：判断与复习

KP-03-02-01 ^KP-03-02-01

**一句话**：寻找被变换后仍在同一方向直线上的非零向量。

<!-- BL-KP:END KP-03-02-01 -->
'''

PATH_DOC = '''<!-- BL-PATH:BEGIN P-MAIN -->

# 学习路径

P-MAIN ^P-MAIN

按前置掌握情况推进。

<!-- BL-STEP:BEGIN P-MAIN-S003 -->

## 第三阶段：特征值

P-MAIN-S003 ^P-MAIN-S003

目标：能判断给定非零向量是否符合特征向量条件。

<!-- BL-STEP:END P-MAIN-S003 -->

<!-- BL-PATH:END P-MAIN -->
'''

START_DOC = '''<!-- BL-START:BEGIN START -->

# 开始学习

START ^START

第一次学习先查看路线；复习时使用核心目录。

<!-- BL-START:END START -->
'''

CHAPTER_FRAGMENT = '''<!-- BL-K:BEGIN K-02-01 -->

### 矩阵乘法

K-02-01 ^K-02-01

矩阵乘法把两个矩阵组合成一个新矩阵。

<!-- BL-K:END K-02-01 -->

<!-- BL-K:BEGIN K-03-04 -->

### 特征值

K-03-04 ^K-03-04

若方阵 A 与非零向量 v 满足 Av = λv，则 λ 是 A 的特征值。

$$
Av = \\lambda v, \\qquad v \\ne 0
$$

#### 几何意义

该方向上的向量经线性变换后仍落在同一条过原点的直线上。

#### 条件与常见误区

非零向量条件不可省略。

<!-- BL-K:END K-03-04 -->
'''


class Fixture:
    """A minimal v2 course whose happy path must validate."""

    def __init__(self, root: Path):
        self.root = root
        self.source_text = '第 37 页：特征值定义与几何意义。\n'
        self.build()

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, text)
        return path

    def build(self):
        payload = '此处逐字保存已核查转写正文。原文中的 [[线性代数#^K-01]] 样式文本不推导成知识关系。'
        payload_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        source_file = self.write('原始资料/线性代数讲义.txt', self.source_text)
        source_hash = hashlib.sha256(self.source_text.encode('utf-8')).hexdigest()
        transcript = f'''<!-- BL-SOURCE:BEGIN SRC-002 -->

# 线性代数：资料转写

SRC-002 ^SRC-002

来源版本：{source_hash}

<!-- BL-SRC:BEGIN SRC-002-U00037 -->

## 第 37 页

SRC-002-U00037 ^SRC-002-U00037

<!-- BL-SOURCE-TEXT:BEGIN SRC-002-U00037 -->
{payload}
<!-- BL-SOURCE-TEXT:END SRC-002-U00037 -->

<!-- BL-SRC:END SRC-002-U00037 -->

<!-- BL-SOURCE:END SRC-002 -->
'''
        self.write('_工作区/课程配置.json', json.dumps(
            {'link_mode': 'obsidian', 'link_schema_version': 2,
             'graph_policy_version': 'bl-typed-links-v2', 'course_vault_prefix': '',
             'layout': 'working'}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/章节索引.json', json.dumps({
            'schema_version': 1,
            'chapters': [{'id': 'KC-01', 'title': '线性代数基础', 'order': 1,
                          'path': '_工作区/章节知识/KC-01.md', 'status': 'complete'}],
            'lessons': [{'id': 'L-03', 'title': '特征值', 'order': 3,
                         'path': '学习文档/03-特征值.md', 'status': 'complete'}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/知识索引.jsonl', ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in [
            {'id': 'K-02-01', 'title': '矩阵乘法', 'chapter_id': 'KC-01', 'anchor': 'K-02-01',
             'kind': 'material', 'status': 'verified',
             'source_refs': [], 'prerequisites': [], 'lesson_ids': ['L-03'],
             'core': False},
            {'id': 'K-03-04', 'title': '特征值', 'chapter_id': 'KC-01', 'anchor': 'K-03-04',
             'kind': 'material', 'status': 'verified',
             'source_refs': [{'source_id': 'SRC-002', 'unit_id': 'U00037'}],
             'prerequisites': ['K-02-01'], 'lesson_ids': ['L-03'],
             'core': True, 'card_id': 'KP-03-02-01', 'card_anchor': 'KP-03-02-01',
             'core_reason': '本课程的关键概念'}]))
        self.write('_工作区/资料索引.json', json.dumps({'schema_version': 1, 'sources': [
            {'id': 'SRC-002', 'path': str(source_file), 'name': '线性代数讲义.txt',
             'sha256': source_hash, 'size': len(self.source_text), 'format': 'txt'}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write(f'_工作区/提取内容/SRC-002/{source_hash}/定位映射.json', json.dumps(
            {'source_id': 'SRC-002', 'source_hash': source_hash, 'total_units': 1,
             'units': [{'id': 'U00037', 'ordinal': 1, 'locator': '第 37 页', 'status': 'extracted',
                        'chunks': [], 'images': []}]}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/转写索引.json', json.dumps({
            'schema_version': 2, 'sources': [
                {'source_id': 'SRC-002', 'source_hash': source_hash,
                 'path': '资料转写/SRC-002-线性代数.md', 'engine': 'host-subagent', 'shell': 'v2',
                 'payloads': [{'unit_id': 'U00037', 'hash': payload_hash, 'mode': 'text',
                               'locator': '第 37 页', 'transformed': False}],
                 'transforms': []}]}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/练习索引.jsonl', json.dumps(
            {'id': 'EX-L03-001', 'owner_lesson_id': 'L-03', 'status': 'used',
             'assesses': ['K-03-04'], 'source_refs': [], 'title': '验证特征向量'},
            ensure_ascii=False) + '\n')
        self.write('_工作区/核心卡片计划.json', json.dumps({
            'schema_version': 2, 'graph_policy_version': 'bl-typed-links-v2', 'cards': [
                {'card_id': 'KP-03-02-01', 'concept_key': 'eigenvalue', 'order': 3,
                 'knowledge_ids': ['K-03-04'], 'exercise_ids': ['EX-L03-001'],
                 'reasons': ['本课程的关键概念']}]}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/核心卡片状态.json', json.dumps(
            {'status': 'complete', 'cards': ['KP-03-02-01']}, ensure_ascii=False) + '\n')
        self.write('_工作区/路径索引.json', json.dumps({
            'path_id': 'P-MAIN', 'title': '完整学习路线', 'steps': [
                {'id': 'P-MAIN-S003', 'title': '第三阶段：特征值', 'order': 3, 'lessons': ['L-03']}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/章节知识/KC-01.md', CHAPTER_FRAGMENT)
        self.write('资料转写/SRC-002-线性代数.md', transcript)
        self.write('学习文档/03-特征值.md', LESSON)
        self.write('核心知识点.md', CARDS)
        self.write('学习路径.md', PATH_DOC)
        self.write('开始学习.md', START_DOC)
        from assemble_knowledge import rebuild_knowledge
        rebuild_knowledge(self.root)
        from obsidian_links import initialize
        initialize(self.root)
        sync_course_relations(self.root, phase='final')

    def reload(self):
        return Registry(self.root)


class TypedLinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / 'course'
        self.course.mkdir(parents=True)
        self.fixture = Fixture(self.course)

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, relative):
        return (self.course / relative).read_text(encoding='utf-8')

    def write(self, relative, text):
        atomic_text(self.course / relative, text)

    # 1. structure ---------------------------------------------------------
    def test_entity_section_keeps_full_body(self):
        text = self.text('学习文档/03-特征值.md')
        section = entity_section(text, 'TEACH', 'L-03-K-03-04')
        self.assertIn('## 从变换理解特征值', section)
        self.assertIn('| 条件 | 说明 |', section)
        self.assertIn('$$', section)
        self.assertTrue(section.startswith('<!-- BL-TEACH:BEGIN L-03-K-03-04 -->'))
        self.assertTrue(section.rstrip().endswith('<!-- BL-TEACH:END L-03-K-03-04 -->'))

    def test_anchor_must_be_at_entry(self):
        text = self.text('知识内容.md')
        broken = text.replace('K-03-04 ^K-03-04\n', '', 1)
        broken = broken.replace('<!-- BL-K:END K-03-04 -->', 'K-03-04 ^K-03-04\n\n<!-- BL-K:END K-03-04 -->')
        with self.assertRaises(SectionError) as ctx:
            parse_sections(broken, None, strict_registry=False)
        self.assertEqual(ctx.exception.code, 'ANCHOR_NOT_AT_ENTRY')

    def test_code_fence_markers_are_ignored(self):
        text = ('<!-- BL-K:BEGIN K-02-01 -->\n\n## 样例\n\nK-02-01 ^K-02-01\n\n'
                '```markdown\n<!-- BL-K:END K-02-01 -->\n^K-02-01\n```\n\n'
                '<!-- BL-K:END K-02-01 -->\n')
        index = parse_sections(text, None, strict_registry=False)
        self.assertEqual(len(index.sections), 1)
        from entity_sections import block_ids
        self.assertEqual(block_ids(text), ['K-02-01'])

    def test_mismatched_boundaries_fail(self):
        text = ('<!-- BL-K:BEGIN K-02-01 -->\n\nK-02-01 ^K-02-01\n\n<!-- BL-K:END K-03-04 -->\n')
        with self.assertRaises(SectionError) as ctx:
            parse_sections(text, None, strict_registry=False)
        self.assertEqual(ctx.exception.code, 'SECTION_BOUNDARY_INVALID')

    def test_duplicate_block_is_reported(self):
        text = self.text('知识内容.md')
        broken = text.replace('K-03-04 ^K-03-04', 'K-03-04 ^K-03-04\n\nK-03-04 ^K-03-04', 1)
        diagnostics = validate_links(self.course, broken, registry_obj=Registry(self.course))
        self.assertIn('DUPLICATE_BLOCK', [item['code'] for item in diagnostics])

    def test_registered_occurrence_is_allowed_but_third_copy_fails(self):
        registry = Registry(self.course)
        locations = [(location.path, location.block_id) for location in registry.locations('K-03-04')]
        self.assertIn(('知识内容.md', 'K-03-04'), locations)
        self.assertIn(('学习文档/03-特征值.md', 'K-03-04'), locations)
        self.assertEqual(len(locations), 2)

    # 2. links and matrix --------------------------------------------------
    def test_kp_links_canonical_k_and_exercise(self):
        text = self.text('核心知识点.md')
        diagnostics = validate_links(self.course, text, registry_obj=Registry(self.course))
        self.assertEqual([], diagnostics)

    def test_kp_teaching_occurrence_target_is_invalid(self):
        registry = Registry(self.course)
        occurrence = registry.teaching_location('L-03', 'K-03-04')
        link = render_entity_link(occurrence, '第 3 章：特征值')
        text = self.text('核心知识点.md').replace(
            '<!-- BL-KP:END KP-03-02-01 -->', f'{link}\n\n<!-- BL-KP:END KP-03-02-01 -->')
        problems = validate_candidate(self.course, {'核心知识点.md': text}, phase='final')
        self.assertTrue(any('KP_TARGET_FORBIDDEN' in problem for problem in problems), problems)

    def test_kp_to_other_entities_is_forbidden(self):
        registry = Registry(self.course)
        location = registry.canonical('IDX-CORE')
        text = self.text('核心知识点.md').replace(
            '<!-- BL-KP:END KP-03-02-01 -->',
            f"{render_entity_link(location, '目录')}\n\n<!-- BL-KP:END KP-03-02-01 -->")
        problems = validate_candidate(self.course, {'核心知识点.md': text}, phase='final')
        self.assertTrue(any('KP_TARGET_FORBIDDEN' in problem for problem in problems), problems)

    def test_source_leaf_has_no_out_edges(self):
        from obsidian_links import check_kp_and_leaf_constraints
        from relation_policy import derive_course_relations
        registry = Registry(self.course)
        graph = derive_course_relations(self.course, registry, phase='final')
        self.assertEqual(0, graph.out_degree('SRC-002'))
        self.assertEqual(0, graph.out_degree('SRC-002-U00037'))
        self.assertEqual([], [item for item in check_kp_and_leaf_constraints(graph, registry)
                              if item['code'] == 'SOURCE_NOT_LEAF'])

    def test_literal_wikilink_in_payload_is_not_a_relation(self):
        from relation_policy import derive_course_relations
        registry = Registry(self.course)
        index = parse_sections(self.text('资料转写/SRC-002-线性代数.md'), None, strict_registry=False)
        spans = [(section.body_start, section.body_end) for section in index.sections.values()
                 if section.kind == 'SOURCE-TEXT']
        text = self.text('资料转写/SRC-002-线性代数.md')
        without = validate_links(self.course, text, registry_obj=registry, payload_spans=spans)
        self.assertNotIn('UNKNOWN_ENTITY', [item['code'] for item in without])
        graph = derive_course_relations(self.course, registry, phase='final')
        self.assertNotIn('K-01', {edge['target_id'] for edge in graph.edges})

    def test_k_to_k_requires_prerequisite_subtype(self):
        registry = Registry(self.course)
        edge = make_edge('K-03-04', 'K-02-01', 'TEACH', 'knowledge', registry=registry,
                         provenance={'kind': 'test'})
        with self.assertRaises(Exception) as ctx:
            from relation_policy import check_matrix
            check_matrix(edge, registry)
        self.assertIn('RELATION_TYPE_FORBIDDEN', str(ctx.exception))

    def test_dep_self_loop_and_cycle(self):
        edges = []
        for source, target in (('K-01', 'K-02'), ('K-02', 'K-03'), ('K-03', 'K-01')):
            edges.append({'type': DEP, 'source_id': source, 'target_id': target})
        with self.assertRaises(Exception) as ctx:
            validate_dep_dag(edges)
        self.assertIn('DEPENDENCY_CYCLE', str(ctx.exception))
        self.assertIn('K-01', str(ctx.exception))

    def test_missing_reciprocal_is_reported(self):
        edges = [{'type': 'TEACH', 'subtype': 'core', 'source_id': 'K-03-04',
                  'target_id': 'KP-03-02-01', 'relation_key': 'k'}]
        missing = validate_reciprocal_contracts(edges)
        self.assertEqual('RELATION_PROJECTION_MISMATCH', missing[0]['code'])

    def test_allowed_type_without_provenance_fails(self):
        registry = Registry(self.course)
        with self.assertRaises(Exception) as ctx:
            from relation_policy import check_matrix
            edge = make_edge('K-03-04', 'KP-03-02-01', 'TEACH', 'core', registry=registry)
            check_matrix(edge, registry)
        self.assertIn('RELATION_PROVENANCE_MISSING', str(ctx.exception))

    # 3. manifest and agent boundaries ------------------------------------
    def test_manifest_violation_for_guessed_link(self):
        text = self.text('学习文档/03-特征值.md').replace(
            '非零向量条件不可省略。'.replace('非零向量条件不可省略。', ''),
            '参见 [[学习文档/03-特征值#^L-03|本章]]。', 1)
        allowlist = ['知识内容#^K-03-04']
        diagnostics = validate_links(self.course, text, allowlist=allowlist,
                                     registry_obj=Registry(self.course))
        self.assertIn('MANIFEST_LINK_VIOLATION', [item['code'] for item in diagnostics])

    def test_reserved_exercise_is_not_a_valid_card_target(self):
        rows = read_jsonl(self.course / '_工作区/练习索引.jsonl')
        rows[0]['status'] = 'reserved'
        atomic_text(self.course / '_工作区/练习索引.jsonl',
                    json.dumps(rows[0], ensure_ascii=False) + '\n')
        registry = Registry(self.course)
        from relation_policy import make_edge, validate_card_exercise_mapping
        edge = make_edge('KP-03-02-01', 'EX-L03-001', 'TEACH', 'practice', registry=registry,
                         provenance={'kind': 'card_plan', 'record_id': 'KP-03-02-01'})
        problems = validate_card_exercise_mapping([edge], registry, rows)
        self.assertTrue(any('reserved' in item['detail'] for item in problems), problems)

    def test_stale_manifest_on_final_phase(self):
        rows = read_jsonl(self.course / '_工作区/练习索引.jsonl')
        rows[0]['status'] = 'reserved'
        atomic_text(self.course / '_工作区/练习索引.jsonl',
                    json.dumps(rows[0], ensure_ascii=False) + '\n')
        registry = Registry(self.course)
        from relation_policy import derive_course_relations
        try:
            derive_course_relations(self.course, registry, phase='final')
            self.fail('expected a stalled phase error')
        except Exception as exc:
            self.assertIn('MISSING_TARGET_BLOCK', str(exc))

    # 4. rendering ---------------------------------------------------------
    def test_render_is_idempotent(self):
        first = self.text('知识内容.md')
        sync_course_relations(self.course, phase='final')
        second = self.text('知识内容.md')
        self.assertEqual(first, second)
        self.assertEqual(1, second.count('<!-- BL-REL:BEGIN K-03-04 -->'))

    def test_projection_mismatch_when_edge_removed(self):
        text = self.text('知识内容.md')
        broken = text.replace('> - [[资料转写/SRC-002-线性代数#^SRC-002-U00037|', '> - [[')
        self.write('知识内容.md', broken)
        errors = validate_obsidian_links(self.course, phase='final')
        self.assertTrue(any('RELATION_PROJECTION_MISMATCH' in error for error in errors))
        sync_course_relations(self.course, phase='final')
        self.assertEqual([], validate_obsidian_links(self.course, phase='final'))

    def test_table_safe_separator_parses_and_cannot_inject(self):
        link = '[[核心知识点#^KP-03-02-01\\|特征值]]'
        parsed = parse_wikilinks(link)
        self.assertEqual('KP-03-02-01', parsed[0]['block'])
        self.assertEqual('特征值', parsed[0]['label'])
        self.assertTrue(parsed[0]['table_escaped'])
        injected = parse_wikilinks('[[核心知识点#^KP-03-02-01\\|x]] [[课程文档/知识内容#^K-03-04|y]]')
        self.assertEqual(['KP-03-02-01', 'K-03-04'], [item['block'] for item in injected])

    def test_semantic_hash_ignores_relation_refresh(self):
        from lesson_tasks import semantic
        before = semantic(self.text('知识内容.md'))
        sync_course_relations(self.course, phase='final')
        after = semantic(self.text('知识内容.md'))
        self.assertEqual(before, after)

    def test_full_document_set_validates(self):
        errors = validate_obsidian_links(self.course, phase='final')
        self.assertEqual([], errors)
        problems = validate_candidate(self.course, {'核心知识点.md': self.text('核心知识点.md')},
                                      phase='final')
        self.assertEqual([], problems)


class LegacyFixture:
    """A v1 package: tail anchors, no boundaries, no relation regions."""

    def __init__(self, root: Path):
        self.root = root
        self.source_text = '第 1 页：核心概念定义。\n'
        self.build()

    def write(self, relative, text):
        atomic_text(self.root / relative, text)

    def build(self):
        source_hash = hashlib.sha256(self.source_text.encode('utf-8')).hexdigest()
        source_file = self.root / '原始资料/讲义.txt'
        source_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(source_file, self.source_text)
        page = '## U00001 · 第 1 页\n\n核心概念的转写正文。\n\n^SRC-001-U00001'
        page_hash = hashlib.sha256(page.encode('utf-8')).hexdigest()
        self.write('_工作区/课程配置.json', json.dumps(
            {'link_mode': 'obsidian', 'embed_assets': True}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/章节索引.json', json.dumps({
            'schema_version': 1,
            'chapters': [{'id': 'KC-001', 'title': '基础', 'order': 1,
                          'path': '_工作区/章节知识/KC-001.md', 'status': 'complete'}],
            'lessons': [{'id': 'L-01', 'title': '第一章', 'order': 1,
                         'path': '学习文档/01-基础.md', 'status': 'complete'}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/知识索引.jsonl', json.dumps(
            {'id': 'K-01', 'title': '核心概念', 'chapter_id': 'KC-001', 'anchor': 'K-01',
             'kind': 'material', 'status': 'verified',
             'source_refs': [{'source_id': 'SRC-001', 'unit_id': 'U00001'}],
             'prerequisites': [], 'lesson_ids': ['L-01'], 'core': True,
             'card_id': 'KP-01-01-01', 'card_anchor': 'KP-01-01-01',
             'core_reason': '基础概念'}, ensure_ascii=False) + '\n')
        self.write('_工作区/资料索引.json', json.dumps({'schema_version': 1, 'sources': [
            {'id': 'SRC-001', 'path': str(source_file), 'name': '讲义.txt',
             'sha256': source_hash, 'size': len(self.source_text), 'format': 'txt'}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write(f'_工作区/提取内容/SRC-001/{source_hash}/定位映射.json', json.dumps(
            {'source_id': 'SRC-001', 'source_hash': source_hash, 'total_units': 1,
             'units': [{'id': 'U00001', 'ordinal': 1, 'locator': '第 1 页', 'status': 'extracted',
                        'chunks': [], 'images': []}]}, ensure_ascii=False, indent=2) + '\n')
        self.write('_工作区/转写索引.json', json.dumps({'schema_version': 1, 'sources': [
            {'source_id': 'SRC-001', 'source_hash': source_hash,
             'path': '资料转写/SRC-001-讲义.md', 'engine': 'host-subagent'}]},
            ensure_ascii=False, indent=2) + '\n')
        self.write('资料转写/SRC-001-讲义.md',
                   f'# 讲义：资料转写\n\n来源：SRC-001\n\n源版本：{source_hash}\n\n'
                   f'<!-- BL-PAGE U00001 complete text {page_hash} -->\n{page}\n<!-- BL-END U00001 -->\n')
        self.write('_工作区/章节知识/KC-001.md',
                   '## 基础概念\n\nK-01 的正文内容。\n\n^K-01\n')
        self.write('学习文档/01-基础.md',
                   '---\ntype: lesson\nid: L-01\ntitle: 第一章\norder: 1\nstatus: complete\n---\n\n'
                   '# 第一章：基础\n\n本章目标：理解核心概念。\n\n'
                   '## 基础概念\n\nK-01 的讲解内容。\n\n^K-01\n')
        self.write('核心知识点.md', '# 核心知识点\n\n## 核心概念卡\n\n一句话复习。\n\n^KP-01-01-01\n')


class MigrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / 'legacy'
        self.course.mkdir(parents=True)
        LegacyFixture(self.course)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_is_conservative_and_apply_creates_v2_shells(self):
        from link_migration import apply_migration, plan_migration, rollback_migration
        plan = plan_migration(self.course)
        self.assertFalse(plan.get('no_op'))
        self.assertTrue(plan['operations'])
        self.assertEqual([], plan['unresolved'])
        report = apply_migration(self.course, plan['plan'])
        self.assertTrue(report['applied'])
        config = read_json(self.course / '_工作区/课程配置.json')
        self.assertEqual(2, config['link_schema_version'])
        lesson = (self.course / '学习文档/01-基础.md').read_text(encoding='utf-8')
        self.assertIn('<!-- BL-L:BEGIN L-01 -->', lesson)
        self.assertIn('L-01 ^L-01', lesson)
        self.assertIn('<!-- BL-TEACH:BEGIN L-01-K-01 -->', lesson)
        self.assertIn('K-01 ^K-01', lesson)
        fragment = (self.course / '_工作区/章节知识/KC-001.md').read_text(encoding='utf-8')
        self.assertIn('<!-- BL-K:BEGIN K-01 -->', fragment)
        knowledge = (self.course / '知识内容.md').read_text(encoding='utf-8')
        self.assertIn('<!-- BL-CHAPTER:BEGIN KC-001 -->', knowledge)
        self.assertIn('IDX-KNOWLEDGE ^IDX-KNOWLEDGE', knowledge)
        cards = (self.course / '核心知识点.md').read_text(encoding='utf-8')
        self.assertIn('<!-- BL-INDEX:BEGIN IDX-CORE -->', cards)
        self.assertIn('<!-- BL-KP:BEGIN KP-01-01-01 -->', cards)
        self.assertIn('KP-01-01-01 ^KP-01-01-01', cards)
        # repeated plan is a no-op after migration
        self.assertTrue(plan_migration(self.course).get('no_op'))
        # rollback restores the legacy layout and version
        journal = sorted((self.course / '_工作区').glob('链接迁移记录.jsonl'))[0]
        rollback_migration(self.course, str(journal.relative_to(self.course)))
        self.assertNotIn('<!-- BL-K:BEGIN K-01 -->',
                         (self.course / '_工作区/章节知识/KC-001.md').read_text(encoding='utf-8'))
        self.assertEqual(1, read_json(self.course / '_工作区/课程配置.json').get('link_schema_version', 1))


class AssemblyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / 'course'
        self.course.mkdir(parents=True)
        Fixture(self.course)

    def tearDown(self):
        self.tmp.cleanup()

    def test_assembler_inserts_boundaries_anchors_and_exercises(self):
        from lesson_tasks import assemble_lesson_document
        folder = '_工作区/讲义尝试/L-test'
        parts = {
            'S001': [('intro', '---\ntype: lesson\nid: L-03\ntitle: 特征值\norder: 3\nstatus: complete\n---\n\n'
                               '# 第 3 章：特征值\n\n本章目标：理解特征值条件。')],
            'S002': [('knowledge', '## 回顾：矩阵乘法\n\n矩阵乘法把两个矩阵组合成一个新矩阵。')],
            'S003': [('knowledge', '## 从变换理解特征值\n\n若 Av = λv 且 v ≠ 0，则 λ 是特征值。')],
            'S004': [('closing', '## 本章速查\n\n关键条件：v ≠ 0。\n\n'
                                 '## 自测：验证一个方向\n\n令 A = diag(2, 3)，验证 v 是否为特征向量。')],
        }
        sections, progress = [], {}
        for index, (sid, items) in enumerate(sorted(parts.items()), 1):
            kind, body = items[0]
            path = f'{folder}/parts/{sid}/001.md'
            atomic_text(self.course / path, body)
            files = [{'path': path, 'hash': sha256(self.course / path)}]
            sections.append({'id': sid, 'kind': kind, 'title': sid,
                             'knowledge_ids': ['K-02-01'] if sid == 'S002'
                             else ['K-03-04'] if sid == 'S003' else [],
                             'directory': f'{folder}/parts/{sid}', 'receipt': f'{folder}/parts/{sid}/section.json'})
            progress[sid] = {'status': 'complete', 'files': files}
        attempt = {'id': 'L-test', 'sections': sections, 'part_progress': progress,
                   'allowed_blocks': ['K-02-01', 'K-03-04', 'EX-L03-001'],
                   'worker_result': {'status': 'complete', 'lesson_id': 'L-03',
                                     'knowledge_ids': ['K-02-01', 'K-03-04'],
                                     'exercises': [{'id': 'EX-L03-001', 'heading': '自测：验证一个方向'}]}}
        lesson = dict(read_json(self.course / '_工作区/章节索引.json')['lessons'][0],
                      knowledge_ids=['K-02-01', 'K-03-04'])
        raw = (self.course / f'{folder}/parts/S001/001.md').read_text(encoding='utf-8')
        assembled = assemble_lesson_document(self.course, attempt, lesson, raw)
        self.assertIn('<!-- BL-L:BEGIN L-03 -->', assembled)
        self.assertIn('L-03 ^L-03', assembled)
        self.assertIn('<!-- BL-TEACH:BEGIN L-03-K-03-04 -->', assembled)
        self.assertIn('K-03-04 ^K-03-04', assembled)
        self.assertIn('<!-- BL-EX:BEGIN EX-L03-001 -->', assembled)
        self.assertIn('EX-L03-001 ^EX-L03-001', assembled)
        self.assertIn('## 本章速查', assembled)
        from obsidian_links import sync_candidate
        documents, _graph = sync_candidate(self.course, {'学习文档/03-特征值.md': assembled},
                                           phase='staged')
        self.assertIn('<!-- BL-REL:BEGIN L-03-K-03-04 -->', documents['学习文档/03-特征值.md'])
        problems = validate_candidate(self.course, documents, phase='staged')
        self.assertEqual([], problems, problems)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.course = Path(self.tmp.name) / 'course'
        self.course.mkdir(parents=True)
        Fixture(self.course)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, script, *args):
        import subprocess
        result = subprocess.run([sys.executable, str(SCRIPTS / script), '--course', str(self.course), *args],
                                capture_output=True, text=True, encoding='utf-8')
        return result

    def test_obsidian_links_check_cli_passes(self):
        result = self.run_cli('obsidian_links.py', 'check', '--mode', 'final')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], json.loads(result.stdout)['errors'])

    def test_validate_package_and_migration_cli_run(self):
        result = self.run_cli('validate_package.py', '--mode', 'final')
        self.assertIn(result.returncode, {0, 1}, result.stderr)
        report = read_json(self.course / '_工作区/结构检查.json')
        self.assertFalse(report['passed'])
        self.assertTrue(report['errors'])
        plan = self.run_cli('obsidian_links.py', 'plan-migration')
        self.assertIn(plan.returncode, {0, 2}, plan.stdout + plan.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
