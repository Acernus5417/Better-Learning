"""Observable resource bounds and attempt ownership; no real model calls."""
import argparse
from collections import Counter
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
import _common
from _common import atomic_text, read_json, write_json, validation_run, ValidationContext
from convert_materials import prepare, dispatch, collect, assemble, load, load_ledger
from attempt_tasks import mark_running, fail_attempt
from capability_probe import start, finish
from inventory_materials import inventory
from extract_materials import Reader
from transcribe_materials import check_all, context, V2_UNIT
from validate_package import validate


class RefactorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.course = self.root / 'course'

    def tearDown(self):
        self.tmp.cleanup()

    def probe(self):
        with patch('capability_probe.secrets.token_hex', return_value='ABCD'), patch('capability_probe.secrets.randbelow', return_value=0):
            job = start(self.course, 'codex', 'fixture')
        write_json(self.course / job['response'], {'can_read_images': True, 'lines': ['ABCD', 'ABCD', 'y = 2x + 10']})
        finish(self.course, 'probe-id', 'codex', 'fixture')

    def visual(self, pages=1):
        from reportlab.pdfgen.canvas import Canvas
        path = self.root / 'book.pdf'
        canvas = Canvas(str(path))
        for i in range(pages):
            canvas.drawString(30, 600, str(i))
            canvas.showPage()
        canvas.save()
        inventory(self.course, [path])
        self.probe()  # Must work before heavy preparation.
        self.assertFalse((self.course / '_工作区/转写任务.json').exists())
        prepare(self.course)
        return path

    def start_attempt(self, bid='B01'):
        job = dispatch(self.course, 'SRC-001', bid, model='fixture')
        mark_running(self.course, job['attempt'], job['member'])
        return job

    def staged(self, job, text='fixture transcript'):
        data = load_ledger(self.course)
        attempt = next(a for a in data['attempts'] if a['id'] == job['attempt'])
        for uid in attempt['unit_ids']:
            output = self.course / attempt['staging_dir'] / 'SRC-001' / (uid + '.md')
            atomic_text(output, text)
            from v3_support import sidecar
            sidecar(self.course, attempt, uid, output, text)

    def collect(self, job):
        return collect(self.course, attempt_id=job['attempt'], stopped_agent_id=job['member'])

    def test_hash_cache_and_invalidation(self):
        path = self.root / 'file'
        path.write_text('first')
        ctx = ValidationContext()
        with patch('_common._sha256', wraps=_common._sha256) as digest:
            values = [ctx.hash(path) for _ in range(100)]
            self.assertEqual(digest.call_count, 1)
            atomic_text(path, 'changed')
            ctx.invalidate(path)
            self.assertNotEqual(ctx.hash(path), values[0])
            self.assertEqual(digest.call_count, 2)

    def test_old_canonical_cannot_satisfy_new_attempt(self):
        self.visual()
        unit = load(self.course)['sources'][0]['units'][0]
        atomic_text(self.course / unit['output'], 'old body')
        job = self.start_attempt()
        result = self.collect(job)
        self.assertEqual(result['results'][0]['failure_kind'], 'WRITE_FAILURE')
        self.assertEqual((self.course / unit['output']).read_text(), 'old body')
        retry = self.start_attempt()
        self.staged(retry, 'old body')
        self.assertEqual(self.collect(retry)['state'], 'committed')
        self.assertTrue(self.collect(retry)['idempotent'])
        assemble(self.course)
        self.assertFalse(check_all(self.course))

    def test_reserved_not_running_and_failed_spawn_releases_slot(self):
        self.visual()
        job = dispatch(self.course, 'SRC-001', 'B01', model='fixture')
        with self.assertRaises(ValueError):
            self.collect(job)
        fail_attempt(self.course, job['attempt'], 'SPAWN_FAILURE')
        next_job = dispatch(self.course, 'SRC-001', 'B01', model='fixture')
        self.assertNotEqual(job['attempt'], next_job['attempt'])
        self.assertNotIn('units', next_job)
        self.assertNotIn('assets', next_job['bootstrap'])

    def test_retry_limit_and_unreadable(self):
        self.visual()
        prepare(self.course, max_attempts=2)
        for _ in range(2):
            job = self.start_attempt()
            self.staged(job, '[待核实]')
            result = self.collect(job)
            self.assertEqual(result['results'][0]['failure_kind'], 'UNREADABLE_CONTENT')
        unit = load(self.course)['sources'][0]['units'][0]
        self.assertEqual(unit['status'], 'needs_review')
        self.assertFalse(dispatch(self.course, 'SRC-001', 'B01', model='fixture')['dispatched'])

    def test_source_change_classified_and_lease_released(self):
        source = self.visual()
        job = self.start_attempt()
        self.staged(job)
        with source.open('ab') as stream:
            stream.write(b'changed')
        result = self.collect(job)
        self.assertEqual(result['results'][0]['failure_kind'], 'ASSET_CHANGED')
        self.assertFalse(load_ledger(self.course)['sources'][0]['batches'][0]['active_member'])

    def test_vision_failure_stops_pipeline(self):
        self.visual()
        job = self.start_attempt()
        self.staged(job, '[不具备读图能力]')
        self.assertTrue(self.collect(job)['capability_blocked'])
        with self.assertRaises(ValueError):
            assemble(self.course)

    def test_cache_hit_does_not_open_reader(self):
        self.visual(2)
        with patch('extract_materials.Reader', side_effect=AssertionError('cache miss')):
            prepare(self.course)
        from extract_materials import extract
        source = load(self.course)['sources'][0]
        with patch.object(Reader, 'read', autospec=True, side_effect=Reader.read) as read:
            extract(self.course, source['id'], argparse.Namespace(start=1, end=2, max_chars=10000, scale=1))
            self.assertEqual(read.call_count, 2)

    def test_text_needs_no_probe_or_agent(self):
        txt = self.root / 'one.txt'
        md = self.root / 'two.md'
        txt.write_text('definition: example', encoding='utf-8')
        md.write_text('# Heading\n\nText', encoding='utf-8')
        inventory(self.course, [txt, md])
        prepare(self.course)
        data = load(self.course)
        self.assertFalse(data['attempts'])
        self.assertTrue(all(u['status'] == 'done' and u['processing_route'] == 'deterministic_text'
                            for s in data['sources'] for u in s['units']))
        assemble(self.course)
        self.assertFalse(check_all(self.course))
        self.assertTrue(all(r['mode'] == 'text' for r in context(self.course, 'SRC-001')[-1].values()))

    def test_final_validation_hashes_and_parses_each_source_once(self):
        source_path = self.visual(4)
        for batch in load(self.course)['sources'][0]['batches']:
            job = self.start_attempt(batch['id'])
            self.staged(job)
            self.collect(job)
        assemble(self.course)
        entries = [{'source_id': 'SRC-001', 'unit_id': u['id'], 'source_hash': load(self.course)['sources'][0]['sha256'],
                    'status': 'covered', 'knowledge_ids': [], 'evidence': 'test fixture'}
                   for u in load(self.course)['sources'][0]['units']]
        import json
        atomic_text(self.course / '_工作区/覆盖台账.jsonl', ''.join(json.dumps(e) + '\n' for e in entries))
        calls = Counter()
        original = _common._sha256
        def counted(path):
            calls[str(path)] += 1
            return original(path)
        # v2 transcripts are parsed with V2_UNIT; the whole run must parse each source once.
        with patch('_common._sha256', side_effect=counted), patch('transcribe_materials.V2_UNIT') as parser:
            parser.finditer.side_effect = V2_UNIT.finditer
            # Minimal course intentionally lacks teaching docs. Performance checks still run.
            validate(self.course)
            self.assertEqual(parser.finditer.call_count, 1)
        self.assertEqual(calls[str(source_path.resolve())], 1)
        self.assertTrue(all(n == 1 for n in calls.values()), calls)

    def test_office_native_chart_blocks_until_render_registered(self):
        from PIL import Image
        source = self.root / 'chart.pptx'
        with zipfile.ZipFile(source, 'w') as z:
            z.writestr('ppt/presentation.xml', '<presentation xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sldId r:id="s"/></presentation>')
            z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="s" Target="slides/slide1.xml"/></Relationships>')
            z.writestr('ppt/slides/slide1.xml', '<slide><sp><p><t>Sales</t></p></sp><chart/></slide>')
        inventory(self.course, [source])
        prepare(self.course)
        units = load(self.course)['sources'][0]['units']
        self.assertEqual(units[0]['processing_route'], 'deterministic_text')
        self.assertTrue(units[-1]['blocked'])
        rendered = self.course / '附件/slide.png'
        rendered.parent.mkdir(parents=True)
        Image.new('RGB', (320, 200), 'white').save(rendered)
        write_json(self.course / 'renders.json', {'SRC-001': {'ppt/slides/slide1.xml': '附件/slide.png'}})
        prepare(self.course, office_renders='renders.json')
        self.assertEqual(load(self.course)['sources'][0]['units'][-1]['processing_route'], 'vision')
        with self.assertRaises(ValueError):
            assemble(self.course)  # A render exists, but actual vision is still mandatory.

    def test_success_does_not_rewrite_plan_or_create_fragments(self):
        self.visual()
        plan = self.course / '转写拆分计划.md'
        before = plan.stat().st_mtime_ns
        job = self.start_attempt()
        self.staged(job)
        self.collect(job)
        self.assertEqual(plan.stat().st_mtime_ns, before)
        self.assertFalse((self.course / '资料转写/_分片').exists())

    def test_interrupted_commit_resumes_from_journal(self):
        self.visual()
        job = self.start_attempt()
        self.staged(job)
        with patch('convert_materials.assess', side_effect=RuntimeError('simulated process interruption')):
            with self.assertRaises(RuntimeError):
                self.collect(job)
        result = self.collect(job)
        self.assertEqual(result['state'], 'committed')
        assemble(self.course)
        self.assertFalse(check_all(self.course))

    def test_legacy_migration_requires_all_stopped_members(self):
        from convert_materials import migrate_legacy
        self.visual()
        data = load_ledger(self.course)
        data['schema_version'] = 1
        data['sources'][0]['batches'][0]['active_member'] = 'old_member'
        data['sources'][0]['units'][0]['status'] = 'dispatched'
        write_json(self.course / '_工作区/转写任务.json', data)
        with self.assertRaises(ValueError):
            migrate_legacy(self.course, [])
        result = migrate_legacy(self.course, ['old_member'])
        self.assertTrue(Path(result['backup']).is_file())
        prepare(self.course)
        self.assertEqual(load(self.course)['schema_version'], 3)
        self.assertEqual(load(self.course)['sources'][0]['units'][0]['status'], 'pending')

    def test_reopen_invalidates_downstream_until_new_attempt_commits(self):
        from convert_materials import reopen_unit, summarize
        self.visual()
        job = self.start_attempt()
        self.staged(job)
        self.collect(job)
        assemble(self.course)
        reopen_unit(self.course, 'SRC-001', 'U00001', '复核公式')
        with self.assertRaises(ValueError):
            assemble(self.course)
        self.assertEqual(summarize(load(self.course))['next_batches'], [{'source': 'SRC-001', 'batch': 'B01'}])
        retry = self.start_attempt()
        self.assertEqual(self.collect(retry)['results'][0]['failure_kind'], 'WRITE_FAILURE')

    def test_docx_rendered_pages_are_separate_visual_units(self):
        from PIL import Image
        source = self.root / 'diagram.docx'
        with zipfile.ZipFile(source, 'w') as z:
            z.writestr('word/document.xml', '<document><p><t>caption</t></p><oMath/></document>')
        inventory(self.course, [source])
        self.course.mkdir(exist_ok=True)
        for name in ['page1.png', 'page2.png']:
            Image.new('RGB', (100, 100), 'white').save(self.course / name)
        write_json(self.course / 'renders.json', {'SRC-001': {'word/document.xml': ['page1.png', 'page2.png']}})
        prepare(self.course, office_renders='renders.json')
        visual = [u for u in load(self.course)['sources'][0]['units'] if u['processing_route'] == 'vision']
        self.assertEqual(len(visual), 2)
        self.assertTrue(all(len(u['assets']) == 1 for u in visual))

    def test_mixed_course_deterministic_and_visual_routes(self):
        from PIL import Image
        import io
        pdf = self.visual(2)
        txt, md, ppt = [self.root / name for name in ('a.txt', 'b.md', 'c.pptx')]
        txt.write_text('Plain lesson', encoding='utf-8')
        md.write_text('# Markdown lesson', encoding='utf-8')
        image = io.BytesIO()
        Image.new('RGB', (32, 32), 'white').save(image, format='PNG')
        with zipfile.ZipFile(ppt, 'w') as z:
            r = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            z.writestr('ppt/presentation.xml', f'<presentation xmlns:r="{r}"><sldId r:id="s"/></presentation>')
            z.writestr('ppt/_rels/presentation.xml.rels', '<Relationships><Relationship Id="s" Target="slides/slide1.xml"/></Relationships>')
            z.writestr('ppt/slides/slide1.xml', f'<slide xmlns:r="{r}"><sp><p><t>Text</t></p></sp><blip r:embed="pic"/><chart/></slide>')
            z.writestr('ppt/slides/_rels/slide1.xml.rels', '<Relationships><Relationship Id="pic" Target="../media/pic.png"/></Relationships>')
            z.writestr('ppt/media/pic.png', image.getvalue())
        inventory(self.course, [pdf, txt, md, ppt])
        from _common import source_index
        ppt_id = next(s['id'] for s in source_index(self.course)['sources'] if s['format'] == 'pptx')
        (self.course / 'slide.png').write_bytes(image.getvalue())
        write_json(self.course / 'renders.json', {ppt_id: {'ppt/slides/slide1.xml': 'slide.png'}})
        prepare(self.course, office_renders='renders.json')
        for source in load(self.course)['sources']:
            for batch in source['batches']:
                job = dispatch(self.course, source['id'], batch['id'], model='fixture')
                if not job['dispatched']:
                    continue
                mark_running(self.course, job['attempt'], job['member'])
                attempt = next(a for a in load_ledger(self.course)['attempts'] if a['id'] == job['attempt'])
                for uid in attempt['unit_ids']:
                    output = self.course / attempt['staging_dir'] / source['id'] / (uid + '.md')
                    atomic_text(output, 'synthetic vision fixture')
                    from v3_support import sidecar
                    sidecar(self.course, attempt, uid, output, 'synthetic vision fixture')
                self.collect(job)
        assemble(self.course)
        self.assertFalse(check_all(self.course))
        data = load(self.course)
        self.assertEqual(len(data['attempts']), 3)  # Two PDF pages, image, full-slide coverage.
        self.assertEqual(len(list((self.course / '资料转写').glob('*.md'))), 4)
        from assemble_knowledge import require_knowledge
        with self.assertRaises(ValueError):
            require_knowledge(self.course)


if __name__ == '__main__':
    unittest.main()
