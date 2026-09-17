"""Host orchestration behavior; visual execution is exercised with real host members."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
from _common import atomic_text, write_json, read_json
from inventory_materials import inventory
from convert_materials import prepare, probe, dispatch as real_dispatch, collect as real_collect, resolve, assemble, load
from capability_probe import start as start_probe
from unittest.mock import patch

def dispatch(*args, **kwargs):
    job = real_dispatch(*args, model="test-model", **kwargs)
    if job['dispatched']:
        from attempt_tasks import mark_running
        mark_running(args[0], job['attempt'], job['member'])
        data = load(args[0])
        attempt = next(a for a in data['attempts'] if a['id'] == job['attempt'])
        job['units'] = attempt['unit_ids']
        job['prompt'] = attempt['task_manifest']
    return job


def collect(course, sid, bid, member=None):
    data = load(course)
    batch = next(b for s in data['sources'] if s['id'] == sid for b in s['batches'] if b['id'] == bid)
    return real_collect(course, attempt_id=batch.get('current_attempt'),
                        stopped_agent_id=member or batch.get('active_member'))
from transcribe_materials import check_all
from assemble_knowledge import build_content


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.course = self.root / 'course'

    def tearDown(self):
        self.temp.cleanup()

    def setup_pages(self, n=3, batch=5, with_probe=True):
        from reportlab.pdfgen.canvas import Canvas
        p = Canvas(str(self.inputs / 'book.pdf'))
        for i in range(n):
            p.drawString(40, 700, f'Page {i}')
            p.showPage()
        p.save()
        inventory(self.course, [self.inputs])
        prepare(self.course, batch_size=batch, max_concurrent=2, mode='bounded', batch_budget=1000000)
        if with_probe:
            with patch('capability_probe.secrets.token_hex', return_value='A1B2C3D4'), patch('capability_probe.secrets.randbelow', return_value=0):
                challenge = start_probe(self.course, 'codex', 'test-model')
            write_json(self.course / challenge['response'], {'can_read_images': True, 'lines': ['A1B2C3D4', 'A1B2C3D4', 'y = 2x + 10']})
            probe(self.course, 'test-writer', model='test-model')

    def put(self, n, text):
        unit = load(self.course)['sources'][0]['units'][n-1]
        output = unit['output']
        if unit.get('current_attempt'):
            a = next(a for a in load(self.course)['attempts'] if a['id'] == unit['current_attempt'])
            output = f"{a['staging_dir']}/SRC-001/{unit['id']}.md"
        atomic_text(self.course / output, text)
        if unit.get('current_attempt'):
            from v3_support import sidecar
            sidecar(self.course, a, unit['id'], self.course / output, text)

    def finish(self, bid='B01'):
        job = dispatch(self.course, 'SRC-001', bid)
        for u in load(self.course)['sources'][0]['units']:
            if u['id'] in job['units']:
                self.put(int(u['id'][1:]), '真实的测试正文 ' + u['id'])
        collect(self.course, 'SRC-001', bid)

    def test_probe_gate(self):
        self.setup_pages(with_probe=False)
        with self.assertRaisesRegex(ValueError, 'probe'):
            dispatch(self.course, 'SRC-001', 'B01')
        with self.assertRaises(ValueError):
            probe(self.course, 'nonwriter')
        self.assertTrue((self.course / '转写拆分计划.md').exists())

    def test_partial_retry_and_assembly_gate(self):
        self.setup_pages()
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '第一页')
        self.put(2, '[待核实] 第二页')
        result = collect(self.course, 'SRC-001', 'B01')
        self.assertEqual([u['status'] for u in result['results']], ['done','unresolved','failed'])
        with self.assertRaises(ValueError): assemble(self.course)
        with self.assertRaises(ValueError): build_content(self.course)
        retry = dispatch(self.course, 'SRC-001', 'B01')
        self.assertEqual(retry['units'], ['U00002'])
        self.put(2, '第二页修正')
        collect(self.course, 'SRC-001', 'B01')
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(3, '第三页')
        collect(self.course, 'SRC-001', 'B01')
        assemble(self.course)
        self.assertFalse(check_all(self.course))
        p = self.course / load(self.course)['sources'][0]['transcript']
        before = p.read_bytes()
        assemble(self.course)
        self.assertEqual(before, p.read_bytes())
        self.assertEqual(before.count(b'<!-- BL-PAGE'), 3)

    def test_concurrency_and_recovery(self):
        self.setup_pages(batch=1)
        first = dispatch(self.course, 'SRC-001', 'B01')
        dispatch(self.course, 'SRC-001', 'B02')
        with self.assertRaises(ValueError): dispatch(self.course, 'SRC-001', 'B01')
        with self.assertRaises(ValueError): dispatch(self.course, 'SRC-001', 'B03')
        with self.assertRaises(ValueError): prepare(self.course)
        with self.assertRaises(ValueError): collect(self.course, 'SRC-001', 'B01', 'wrong')
        collect(self.course, 'SRC-001', 'B01', first['member'])
        self.assertTrue(dispatch(self.course, 'SRC-001', 'B03')['dispatched'])

    def test_tamper_and_changed_source(self):
        self.setup_pages(n=1)
        self.finish()
        assemble(self.course)
        self.put(1, 'tampered')
        self.assertTrue(check_all(self.course))
        with self.assertRaises(ValueError): assemble(self.course)
        prepare(self.course)
        self.assertEqual(load(self.course)['sources'][0]['units'][0]['status'], 'failed')
        with (self.inputs / 'book.pdf').open('ab') as f: f.write(b'changed')
        with self.assertRaises(ValueError): dispatch(self.course, 'SRC-001', 'B01')

    def test_chapter_boundaries_and_tail(self):
        self.setup_pages(n=4)
        write_json(self.course / '_工作区/chapters.json', {'SRC-001':[3]})
        prepare(self.course, chapter_boundaries='_工作区/chapters.json', mode='bounded', batch_budget=1000000)
        self.assertEqual([b['units'] for b in load(self.course)['sources'][0]['batches']],
                         [['U00001','U00002'],['U00003','U00004']])
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '页一')
        self.put(2, '不得带入的前文' + '尾'*100)
        collect(self.course, 'SRC-001', 'B01')
        job = dispatch(self.course, 'SRC-001', 'B02')
        prompt = (self.course / job['prompt']).read_text(encoding='utf-8')
        self.assertNotIn('不得带入的前文', prompt)
        self.assertIn('尾'*100, prompt)

    def test_resolution_and_evidence(self):
        self.setup_pages(n=1)
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '[待核实] 封底')
        collect(self.course, 'SRC-001', 'B01')
        atomic_text(self.course / '_工作区/proof.md', '已查看：封底只有出版社信息。')
        resolve(self.course, 'SRC-001', 'U00001', 'non_teaching', '封底出版社信息', '_工作区/proof.md')
        assemble(self.course)
        self.assertFalse(check_all(self.course))
        self.assertIn('non_teaching', (self.course / '_工作区/覆盖台账.jsonl').read_text(encoding='utf-8'))
        atomic_text(self.course / '_工作区/proof.md', 'changed')
        self.assertTrue(check_all(self.course))

    def test_duplicate_requires_verified_target(self):
        self.setup_pages(n=2)
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '原文')
        self.put(2, '[待核实] 重复页')
        collect(self.course, 'SRC-001', 'B01')
        atomic_text(self.course / '_工作区/proof.md', '经原件对比，第二页为第一页重复。')
        with self.assertRaises(ValueError):
            resolve(self.course, 'SRC-001', 'U00002', 'duplicate', '重复', '_工作区/proof.md', 'U00002')
        resolve(self.course, 'SRC-001', 'U00002', 'duplicate', '重复', '_工作区/proof.md', 'U00001')
        assemble(self.course)
        self.assertFalse(check_all(self.course))

    def test_preexisting_file_without_dispatch(self):
        self.setup_pages(n=1)
        self.put(1, 'preexisting')
        with self.assertRaises(ValueError): collect(self.course, 'SRC-001', 'B01')

    def test_reprepare_keeps_completed(self):
        self.setup_pages(n=1)
        self.finish()
        prepare(self.course)
        self.assertEqual(load(self.course)['sources'][0]['units'][0]['status'], 'done')
        self.assertFalse(dispatch(self.course, 'SRC-001', 'B01')['dispatched'])

    def test_source_transcript_tamper(self):
        self.setup_pages(n=1)
        self.finish()
        assemble(self.course)
        p = self.course / load(self.course)['sources'][0]['transcript']
        p.write_text(p.read_text(encoding='utf-8')+'unexpected',encoding='utf-8')
        self.assertTrue(check_all(self.course))

    def test_nonvisual_model_stops_dispatch_and_assembly(self):
        self.setup_pages(n=1, with_probe=False)
        challenge = start_probe(self.course, 'codex', 'text-only')
        write_json(self.course / challenge['response'], {'can_read_images': False, 'lines': []})
        with self.assertRaisesRegex(ValueError, '更换'):
            probe(self.course, 'text-worker', model='text-only')
        with self.assertRaises(ValueError):
            real_dispatch(self.course, 'SRC-001', 'B01', model='text-only')
        with self.assertRaises(ValueError):
            assemble(self.course)
        prepare(self.course)
        self.assertTrue(check_all(self.course))

    def test_guessing_and_write_only_probe_do_not_pass(self):
        self.setup_pages(n=1, with_probe=False)
        atomic_text(self.course / '_工作区/probe.txt', 'ok')
        with self.assertRaises(ValueError):
            probe(self.course, 'writer', model='test-model')
        challenge = start_probe(self.course, 'codex', 'test-model')
        write_json(self.course / challenge['response'],
                   {'can_read_images': True, 'lines': ['目录', '第一章', '常识']})
        with self.assertRaises(ValueError):
            probe(self.course, 'guessing-worker', model='test-model')

    def test_model_change_needs_fresh_probe(self):
        self.setup_pages(n=1)
        with self.assertRaises(ValueError):
            real_dispatch(self.course, 'SRC-001', 'B01', model='another-model')
        with self.assertRaises(ValueError):
            real_dispatch(self.course, 'SRC-001', 'B01', host='claude', model='test-model')

    def test_runtime_loss_of_vision_blocks_following_work(self):
        self.setup_pages(n=1)
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '[不具备读图能力]')
        collect(self.course, 'SRC-001', 'B01')
        self.assertTrue(read_json(self.course / '_工作区/能力探针/current.json')['capability_blocked'])
        self.assertFalse(dispatch(self.course, 'SRC-001', 'B01')['dispatched'])
        with self.assertRaises(ValueError):
            assemble(self.course)

    def test_ai_request_still_requires_materialized_knowledge(self):
        import subprocess
        from assemble_knowledge import require_knowledge
        self.setup_pages(n=1, with_probe=False)
        with self.assertRaises(ValueError):
            require_knowledge(self.course)
        atomic_text(self.course / '_工作区/用户请求.md', '请按基础函数主题由AI补全，不声称读取原教材。')
        atomic_text(self.course / '_工作区/章节知识/KC-001.md', '# 函数\nAI补充：函数建立输入与输出的对应。')
        write_json(self.course / '_工作区/章节索引.json', {'chapters': [
            {'id':'KC-001','title':'函数','order':1,'status':'complete','path':'_工作区/章节知识/KC-001.md'}]})
        atomic_text(self.course / '_工作区/知识索引.jsonl', '{"id":"K-001","kind":"ai_supplement"}\n')
        with self.assertRaises(ValueError):
            build_content(self.course)
        script = Path(__file__).resolve().parents[1] / 'better-learning/scripts/assemble_knowledge.py'
        result = subprocess.run([sys.executable, '-X', 'utf8', '-B', str(script), '--course', str(self.course),
            '--ai-request', '_工作区/用户请求.md'], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(require_knowledge(self.course)['mode'], 'ai_supplement')
        self.assertTrue(check_all(self.course))  # Original unread material remains incomplete.
        atomic_text(self.course / '_工作区/章节知识/KC-001.md', 'changed knowledge')
        with self.assertRaises(ValueError):
            require_knowledge(self.course)


if __name__ == '__main__':
    unittest.main()
