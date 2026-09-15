"""Host orchestration behavior; visual execution is exercised with real host members."""
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
from _common import atomic_text, write_json
from inventory_materials import inventory
from convert_materials import prepare, probe, dispatch, collect, resolve, assemble, load
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

    def setup_pages(self, n=3, batch=8, with_probe=True):
        from reportlab.pdfgen.canvas import Canvas
        p = Canvas(str(self.inputs / 'book.pdf'))
        for i in range(n):
            p.drawString(40, 700, f'Page {i}')
            p.showPage()
        p.save()
        inventory(self.course, [self.inputs])
        prepare(self.course, batch_size=batch)
        if with_probe:
            atomic_text(self.course / '_工作区/probe.txt', 'ok')
            probe(self.course, 'test-writer')

    def put(self, n, text):
        unit = load(self.course)['sources'][0]['units'][n-1]
        atomic_text(self.course / unit['output'], text)

    def finish(self, bid='B01'):
        job = dispatch(self.course, 'SRC-001', bid)
        for u in load(self.course)['sources'][0]['units']:
            if u['id'] in job['units']:
                atomic_text(self.course / u['output'], '真实的测试正文 ' + u['id'])
        collect(self.course, 'SRC-001', bid)

    def test_probe_gate(self):
        self.setup_pages(with_probe=False)
        with self.assertRaisesRegex(ValueError, 'probe'):
            dispatch(self.course, 'SRC-001', 'B01')
        with self.assertRaises(FileNotFoundError):
            probe(self.course, 'nonwriter')
        self.assertTrue((self.course / '转写拆分计划.md').exists())

    def test_partial_retry_and_assembly_gate(self):
        self.setup_pages()
        dispatch(self.course, 'SRC-001', 'B01')
        self.put(1, '第一页')
        self.put(2, '[待核实] 第二页')
        result = collect(self.course, 'SRC-001', 'B01')
        self.assertEqual([u['status'] for u in result['units']], ['done','unresolved','failed'])
        with self.assertRaises(ValueError): assemble(self.course)
        with self.assertRaises(ValueError): build_content(self.course)
        retry = dispatch(self.course, 'SRC-001', 'B01')
        self.assertEqual(retry['units'], ['U00002','U00003'])
        self.assertTrue(retry['member'].endswith('-r1'))
        self.put(2, '第二页修正')
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
        prepare(self.course, chapter_boundaries='_工作区/chapters.json')
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


if __name__ == '__main__':
    unittest.main()
