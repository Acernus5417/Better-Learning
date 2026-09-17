"""Synthetic interruption/oversize scenarios, not live model quality tests."""
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

    def start(self):
        self.knowledge(1)
        return lessons.pump(self.course, 'codex', 'fixture')['tickets'][0]

    def attempt(self, job):
        return lessons.find(read_json(self.course / lessons.LEDGER), job['attempt'])[0]

    def partial(self, job):
        self.write_lesson(job)
        a = self.attempt(job)
        section = a['sections'][1]
        atomic_text(self.course / section['directory'] / '001.md', '已完成的第一部分。' * 600)
        write_json(self.course / section['receipt'], {'schema_version': 1, 'section_id': section['id'],
                   'status': 'partial', 'files': ['001.md']})
        end = a['sections'][2]
        write_json(self.course / end['receipt'], {'schema_version': 1, 'section_id': end['id'], 'status': 'partial', 'files': []})
        write_json(self.course / a['result'], {'status': 'partial', 'lesson_id': 'L-01', 'knowledge_ids': ['K-01'],
                   'reason': 'simulated request too large', 'error_kind': 'WRITE_TOO_LARGE'})
        return a

    def finish_resume(self, job):
        a = self.attempt(job)
        lessons.mark_running(self.course, a['id'], job['member'])
        for section in a['sections']:
            if section['status'] == 'complete': continue
            receipt = read_json(self.course / section['receipt'])
            name = f'{len(receipt["files"]) + 1:03d}.md'
            body = '续写内容\n[[知识内容#^K-01]]\n^K-01\n' if section['kind'] == 'knowledge' else '## 章末\n练习答案与复习'
            atomic_text(self.course / section['directory'] / name, body)
            receipt.update(status='complete', files=receipt['files'] + [name])
            write_json(self.course / section['receipt'], receipt)
        write_json(self.course / a['result'], {'status': 'complete', 'lesson_id': 'L-01', 'knowledge_ids': ['K-01']})
        return lessons.collect(self.course, a['id'], job['member'])

    def test_partial_resume_merges_large_document_once(self):
        job = self.start(); original = self.partial(job)
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertEqual(result['worker_failure']['error_kind'], 'WRITE_TOO_LARGE')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())
        self.assertTrue(cards.check(self.course))
        retry = result['refill'][0]; a = self.attempt(retry)
        self.assertEqual(a['parent_attempt_id'], original['id'])
        self.assertEqual(a['sections'][1]['next_chunk'], '002.md')
        self.assertTrue(a['protected_chunks'])
        self.assertIsNone(self.finish_resume(retry)['failure_kind'])
        text = (self.course / '学习文档/01-章节.md').read_text('utf-8')
        self.assertGreater(len(text), 4000)
        self.assertEqual(text.count('已完成的第一部分。'), 600)
        self.assertEqual(text.count('^K-01\n'), 1)
        self.assertTrue(lessons.collect(self.course, retry['attempt'], retry['member'])['idempotent'])
        self.assertEqual(text, (self.course / '学习文档/01-章节.md').read_text('utf-8'))

    def test_no_final_result_still_retains_acknowledged_sections(self):
        job = self.start(); self.write_lesson(job); a = self.attempt(job)
        (self.course / a['result']).unlink()
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['saved_sections'], 3)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        retry = result['refill'][0]
        self.assertTrue(all(s['status'] == 'complete' for s in self.attempt(retry)['sections']))
        self.assertIsNone(self.finish_resume(retry)['failure_kind'])

    def test_false_complete_cannot_bypass_missing_section(self):
        job = self.start(); a = self.partial(job)
        write_json(self.course / a['result'], {'status': 'complete', 'lesson_id': 'L-01', 'knowledge_ids': ['K-01']})
        result = lessons.collect(self.course, job['attempt'], job['member'], refill=False)
        self.assertEqual(result['failure_kind'], 'PARTIAL_OUTPUT')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())

    def test_duplicate_chunk_list_rejected_without_losing_other_sections(self):
        job = self.start(); self.write_lesson(job); a = self.attempt(job); section = a['sections'][1]
        receipt = read_json(self.course / section['receipt']); receipt['files'] = ['001.md', '001.md']
        write_json(self.course / section['receipt'], receipt)
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 2)
        self.assertEqual(self.attempt(result['refill'][0])['sections'][1]['completed_files'], [])

    def test_changed_saved_chunk_blocks_reuse(self):
        job = self.start(); a = self.partial(job)
        lessons.collect(self.course, job['attempt'], job['member'], refill=False)
        atomic_text(self.course / a['sections'][0]['directory'] / '001.md', 'changed')
        result = lessons.pump(self.course, 'codex', 'fixture')
        self.assertFalse(result['tickets']); self.assertTrue(result['blocked'])

    def test_changed_input_never_salvages_stale_chunks(self):
        job = self.start(); self.partial(job)
        atomic_text(self.course / '学习需求.md', 'changed')
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['failure_kind'], 'STALE_INPUT')
        self.assertFalse(result['refill']); self.assertEqual(result['saved_sections'], 0)

    def test_timeout_salvages_and_refills(self):
        job = self.start(); self.partial(job)
        result = lessons.fail_attempt(self.course, job['attempt'], 'TIMED_OUT', job['member'])
        self.assertEqual(result['saved_sections'], 2)
        self.assertTrue(result['refill'])
        self.assertEqual(self.attempt(job)['reported_failure'], 'TIMED_OUT')

    def test_exhausted_partial_retries_stop(self):
        job = self.start(); self.partial(job)
        for _ in range(3):
            result = lessons.collect(self.course, job['attempt'], job['member'])
            if not result['refill']: break
            job = result['refill'][0]
            lessons.mark_running(self.course, job['attempt'], job['member'])
        self.assertFalse(result['refill'])
        self.assertEqual(read_json(self.course / lessons.LEDGER)['lessons'][0]['status'], 'needs_review')

    def test_unconfirmed_stop_cannot_collect_parts(self):
        job = self.start(); self.partial(job)
        with self.assertRaises(ValueError): lessons.collect(self.course, job['attempt'], 'not-the-member')

    def test_commit_crash_recovers_without_remerging(self):
        import os
        job = self.start(); self.write_lesson(job)
        target = self.course / '学习文档/01-章节.md'
        original = os.replace
        def interrupted(src, dst):
            original(src, dst)
            if dst == target: raise KeyboardInterrupt('simulated process death after rename')
        with patch('lesson_tasks.os.replace', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt): lessons.collect(self.course, job['attempt'], job['member'])
        before = target.read_bytes()
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertIsNone(result['failure_kind'])
        self.assertEqual(before, target.read_bytes())

    def test_copied_chunks_cannot_be_rewritten_by_retry(self):
        job = self.start(); self.partial(job)
        result = lessons.collect(self.course, job['attempt'], job['member'])
        retry = result['refill'][0]; a = self.attempt(retry)
        lessons.mark_running(self.course, a['id'], retry['member'])
        atomic_text(self.course / a['sections'][0]['directory'] / '001.md', 'tampered')
        result = lessons.collect(self.course, a['id'], retry['member'], refill=False)
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())

    def test_legacy_attempt_retains_single_file_collection(self):
        job = self.start()
        data = read_json(self.course / lessons.LEDGER); a, _ = lessons.find(data, job['attempt'])
        a.pop('write_mode'); write_json(self.course / lessons.LEDGER, data)
        self.write_lesson(job)
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertIsNone(result['failure_kind'])

    def test_malformed_final_result_preserves_section_checkpoints(self):
        job = self.start(); self.write_lesson(job); a = self.attempt(job)
        atomic_text(self.course / a['result'], '{truncated')
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 3)
        self.assertIsNone(self.finish_resume(result['refill'][0])['failure_kind'])

    def test_incomplete_formula_rejects_only_its_section(self):
        job = self.start(); self.write_lesson(job); a = self.attempt(job)
        section = a['sections'][1]
        path = self.course / section['directory'] / '001.md'
        atomic_text(path, '$$\nx=1\n[[知识内容#^K-01]]\n^K-01\n')
        result = lessons.collect(self.course, job['attempt'], job['member'])
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')
        self.assertEqual(result['saved_sections'], 2)
        self.assertIsNone(self.finish_resume(result['refill'][0])['failure_kind'])

    def test_math_boundaries_accept_complete_math_and_ignore_code(self):
        from lesson_parts import check_math_boundaries
        check_math_boundaries('$$x=1$$\n\\[x=2\\]\n`$$`\n```latex\n\\begin{cases}\n```')
        check_math_boundaries('$$\\begin{cases}x=1\\end{cases}$$')
        for bad in ['\\[x=2', '$$x=1', '$$\\begin{cases}x=1$$']:
            with self.assertRaises(ValueError): check_math_boundaries(bad)


if __name__ == '__main__': unittest.main()
