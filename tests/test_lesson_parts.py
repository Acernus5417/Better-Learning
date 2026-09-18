"""Synthetic interruption/oversize scenarios for main-agent chapter writing."""
import json
import unittest
from unittest.mock import patch

import test_v2_pipeline as support
from _common import atomic_text, read_json, write_json
import lesson_tasks as lessons
import core_cards as cards


class PartsTests(unittest.TestCase):
    setUp = support.V2Tests.setUp
    tearDown = support.V2Tests.tearDown
    knowledge = support.V2Tests.knowledge
    write_lesson = support.V2Tests.write_lesson
    lesson_row = support.V2Tests.lesson_row

    def start(self, lesson_id='L-01'):
        self.knowledge(1)
        return lesson_id

    def row(self, lesson_id='L-01'):
        data = read_json(self.course / lessons.LEDGER)
        return next(l for l in data['lessons'] if l['id'] == lesson_id)

    def partial(self, lesson_id='L-01'):
        self.write_lesson(lesson_id)
        row = self.row(lesson_id)
        section = row['sections'][1]
        atomic_text(self.course / section['directory'] / '001.md', '已完成的第一部分。' * 600)
        write_json(self.course / section['receipt'], {'schema_version': 1, 'section_id': section['id'],
                   'status': 'partial', 'files': ['001.md']})
        end = row['sections'][2]
        write_json(self.course / end['receipt'], {'schema_version': 1, 'section_id': end['id'],
                   'status': 'partial', 'files': []})
        write_json(self.course / row['result'], {'status': 'partial', 'lesson_id': lesson_id,
                   'knowledge_ids': ['K-01'], 'reason': 'simulated request too large',
                   'error_kind': 'WRITE_TOO_LARGE'})
        return row

    def finish_resume(self, lesson_id='L-01'):
        row = self.row(lesson_id)
        manifest = read_json(self.course / row['manifest'])
        for section in row['sections']:
            receipt = read_json(self.course / section['receipt'])
            if receipt['status'] == 'complete': continue
            name = f'{len(receipt["files"]) + 1:03d}.md'
            body = ('## 小节\n\n续写内容\n' + manifest['links']['knowledge']['K-01']
                    if section['kind'] == 'knowledge' else '## 章末\n练习答案与复习')
            atomic_text(self.course / section['directory'] / name, body)
            receipt.update(status='complete', files=receipt['files'] + [name])
            write_json(self.course / section['receipt'], receipt)
        write_json(self.course / row['result'], {'status': 'complete', 'lesson_id': lesson_id,
                                                 'knowledge_ids': ['K-01']})
        return lessons.commit(self.course, lesson_id)

    def test_partial_resume_merges_large_document_once(self):
        lesson_id = self.start(); self.partial(lesson_id)
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertEqual(result['draft_failure']['error_kind'], 'WRITE_TOO_LARGE')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())
        self.assertTrue(cards.check(self.course))
        # The main agent rewrites only the missing chunks; saved ones stay trusted.
        lessons.prepare(self.course)
        row = self.row(lesson_id)
        self.assertEqual(row['sections'][1]['next_chunk'], '002.md')
        self.assertTrue(row['protected_chunks'])
        self.assertIsNone(self.finish_resume(lesson_id)['failure_kind'])
        text = (self.course / '学习文档/01-章节.md').read_text('utf-8')
        self.assertGreater(len(text), 4000)
        self.assertEqual(text.count('已完成的第一部分。'), 600)
        self.assertEqual(text.count('^K-01\n'), 1)
        self.assertTrue(lessons.commit(self.course, lesson_id)['idempotent'])
        self.assertEqual(text, (self.course / '学习文档/01-章节.md').read_text('utf-8'))

    def test_no_final_result_still_retains_acknowledged_sections(self):
        lesson_id = self.start(); self.write_lesson(lesson_id)
        (self.course / self.row(lesson_id)['result']).unlink()
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['saved_sections'], 3)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertIsNone(self.finish_resume(lesson_id)['failure_kind'])

    def test_false_complete_cannot_bypass_missing_section(self):
        lesson_id = self.start(); self.partial(lesson_id)
        write_json(self.course / self.row(lesson_id)['result'],
                   {'status': 'complete', 'lesson_id': lesson_id, 'knowledge_ids': ['K-01']})
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())

    def test_duplicate_chunk_list_rejected_without_losing_other_sections(self):
        lesson_id = self.start(); self.write_lesson(lesson_id); row = self.row(lesson_id)
        section = row['sections'][1]
        receipt = read_json(self.course / section['receipt']); receipt['files'] = ['001.md', '001.md']
        write_json(self.course / section['receipt'], receipt)
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 2)

    def test_changed_saved_chunk_blocks_reuse(self):
        lesson_id = self.start(); row = self.partial(lesson_id)
        lessons.commit(self.course, lesson_id)
        lessons.prepare(self.course)  # rebuild input package, copying verified chunks
        atomic_text(self.course / row['sections'][0]['directory'] / '001.md', 'changed')
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertIn('Reused lesson chunk changed', result.get('error', ''))

    def test_changed_input_never_salvages_stale_chunks(self):
        lesson_id = self.start(); self.partial(lesson_id)
        atomic_text(self.course / '学习需求.md', 'changed')
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'STALE_INPUT')
        self.assertEqual(result['saved_sections'], 0)

    def test_exhausted_partial_retries_stop(self):
        lesson_id = self.start(); self.partial(lesson_id)
        for _ in range(3):
            result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertEqual(read_json(self.course / lessons.LEDGER)['lessons'][0]['status'], 'needs_review')

    def test_commit_crash_recovers_without_remerging(self):
        import os
        lesson_id = self.start(); self.write_lesson(lesson_id)
        target = self.course / '学习文档/01-章节.md'
        original = os.replace
        def interrupted(src, dst):
            original(src, dst)
            if dst == target: raise KeyboardInterrupt('simulated process death after rename')
        with patch('lesson_tasks.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): lessons.commit(self.course, lesson_id)
        before = target.read_bytes()
        result = lessons.commit(self.course, lesson_id)
        self.assertIsNone(result['failure_kind'])
        self.assertEqual(before, target.read_bytes())

    def test_malformed_final_result_preserves_section_checkpoints(self):
        lesson_id = self.start(); self.write_lesson(lesson_id)
        atomic_text(self.course / self.row(lesson_id)['result'], '{truncated')
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 3)
        self.assertIsNone(self.finish_resume(lesson_id)['failure_kind'])

    def test_incomplete_formula_rejects_only_its_section(self):
        lesson_id = self.start(); self.write_lesson(lesson_id); row = self.row(lesson_id)
        section = row['sections'][1]
        atomic_text(self.course / section['directory'] / '001.md', '$$\nx=1\n[[知识内容#^K-01|概念1]]\n')
        result = lessons.commit(self.course, lesson_id)
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 2)
        # Chunks live in a stable folder: the writer repairs the offending chunk in place.
        atomic_text(self.course / section['directory'] / '001.md',
                    '## 小节\n\n修正后的公式 $x=1$\n[[知识内容#^K-01|概念1]]\n')
        self.assertIsNone(lessons.commit(self.course, lesson_id)['failure_kind'])

    def test_math_boundaries_accept_complete_math_and_ignore_code(self):
        from lesson_parts import check_math_boundaries
        check_math_boundaries('$$x=1$$\n\\[x=2\\]\n`$$`\n```latex\n\\begin{cases}\n```')
        check_math_boundaries('$$\\begin{cases}x=1\\end{cases}$$')
        for bad in ['\\[x=2', '$$x=1', '$$\\begin{cases}x=1$$']:
            with self.assertRaises(ValueError): check_math_boundaries(bad)


if __name__ == '__main__': unittest.main()
