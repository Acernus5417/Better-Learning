"""v3 dispatch, chapter scheduling and graph contracts using synthetic workers."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
from _common import atomic_text, read_json, write_json
from inventory_materials import inventory
import convert_materials as cm
import attempt_tasks as tasks
import lesson_tasks as lessons
import core_cards as cards
import obsidian_links as links
from assemble_knowledge import build_content
from v3_support import sidecar


class V2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name); self.course = self.root / 'course'

    def tearDown(self): self.tmp.cleanup()

    def visual(self, n=20):
        from reportlab.pdfgen.canvas import Canvas
        from capability_probe import start, finish
        source = self.root / 'scan.pdf'; c = Canvas(str(source))
        for i in range(n): c.drawString(20, 500, f'{i}'); c.showPage()
        c.save(); inventory(self.course, [source])
        with patch('capability_probe.secrets.token_hex', return_value='ABCD'), patch('capability_probe.secrets.randbelow', return_value=0):
            probe = start(self.course, 'codex', 'fixture')
        write_json(self.course / probe['response'], {'can_read_images': True, 'lines': ['ABCD','ABCD','y = 2x + 10']})
        finish(self.course, 'probe', 'codex', 'fixture'); cm.prepare(self.course)

    def produce(self, job, status='complete', text='Synthetic page'):
        data = cm.load_ledger(self.course); a = next(a for a in data['attempts'] if a['id'] == job['attempt'])
        tasks.mark_running(self.course, a['id'], job['member'])
        for uid in a['unit_ids']:
            path = self.course / a['staging_dir'] / a['source_id'] / (uid + '.md')
            atomic_text(path, text); sidecar(self.course, a, uid, path, text, status=status)
        return a

    def test_twenty_pages_are_four_bounded_batches(self):
        self.visual()
        data = cm.load(self.course)
        self.assertEqual([len(b['units']) for b in data['sources'][0]['batches']], [5,5,5,5])
        jobs = tasks.pump(self.course, 'codex', 'fixture')['tickets']
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(j['profile'] == 'bounded' for j in jobs))

    def test_uncertain_escalates_with_lineage_and_tiles(self):
        self.visual(10)
        job = tasks.pump(self.course, 'codex', 'fixture', max_fill=1)['tickets'][0]
        a = self.produce(job, status='uncertain')
        cm.collect(self.course, attempt_id=a['id'], stopped_agent_id=job['member'])
        retry = tasks.pump(self.course, 'codex', 'fixture')['tickets'][0]
        self.assertEqual(retry['profile'], 'strict')
        attempt = next(x for x in cm.load(self.course)['attempts'] if x['id'] == retry['attempt'])
        self.assertEqual(attempt['parent_attempt_id'], a['id'])
        self.assertEqual(len(attempt['unit_ids']), 1)
        self.assertTrue(attempt['visual_assets'][attempt['unit_ids'][0]]['tiles'])
        self.assertIn('MEMBER_UNCERTAIN', attempt['strict_reasons'])

    def test_infrastructure_retry_stays_bounded(self):
        self.visual(5)
        job = tasks.pump(self.course, 'codex', 'fixture')['tickets'][0]
        tasks.fail_attempt(self.course, job['attempt'], 'SPAWN_FAILURE')
        retry = tasks.pump(self.course, 'codex', 'fixture')['tickets'][0]
        self.assertEqual(retry['profile'], 'bounded')

    def test_next_batch_waits_for_current_batch(self):
        """No slot refill: the next pump is refused until the whole batch is collected."""
        self.visual(25)
        jobs = tasks.pump(self.course, 'codex', 'fixture')['tickets']
        self.assertEqual(len(jobs), 4)
        blocked = tasks.pump(self.course, 'codex', 'fixture')
        self.assertFalse(blocked['tickets'])
        self.assertTrue(blocked['batch_open'])
        self.produce(jobs[0])
        result = cm.collect(self.course, attempt_id=jobs[0]['attempt'], stopped_agent_id=jobs[0]['member'])
        self.assertNotIn('refill', result)
        self.assertEqual(cm.summarize(cm.load(self.course))['active_count'], 3)
        self.assertTrue(tasks.pump(self.course, 'codex', 'fixture')['batch_open'])
        for job in jobs[1:]:
            a = self.produce(job)
            cm.collect(self.course, attempt_id=a['id'], stopped_agent_id=job['member'])
        nxt = tasks.pump(self.course, 'codex', 'fixture')
        self.assertFalse(nxt['batch_open'])
        self.assertTrue(nxt['tickets'])

    def test_capability_block_stops_new_batches(self):
        self.visual(10)
        job = tasks.pump(self.course, 'codex', 'fixture', max_fill=1)['tickets'][0]
        self.produce(job, 'vision_unavailable')
        result = cm.collect(self.course, attempt_id=job['attempt'], stopped_agent_id=job['member'])
        self.assertTrue(result['capability_blocked'])
        self.assertFalse(tasks.pump(self.course, 'codex', 'fixture')['tickets'])

    def test_body_sentinel_is_not_control_status(self):
        self.visual(1)
        job = tasks.pump(self.course, 'codex', 'fixture')['tickets'][0]
        self.produce(job, text='教材介绍字符串 [待核实] 与 [不具备读图能力]。')
        self.assertEqual(cm.collect(self.course, attempt_id=job['attempt'], stopped_agent_id=job['member'])['state'], 'committed')

    def test_explicit_strict_requires_all_tiles(self):
        self.visual(2)
        cm.prepare(self.course, force_strict_units=['SRC-001/U00001'])
        job = tasks.pump(self.course, 'codex', 'fixture', max_fill=1)['tickets'][0]
        a = self.produce(job)
        path = self.course / a['staging_dir'] / 'SRC-001/U00001.result.json'
        data = read_json(path); data['tiles_read'] = []; write_json(path, data)
        result = cm.collect(self.course, attempt_id=a['id'], stopped_agent_id=job['member'])
        self.assertEqual(result['results'][0]['failure_kind'], 'INVALID_OUTPUT')

    def knowledge(self, n=8):
        source = self.root / 'book.txt'; source.write_text('A concept.', encoding='utf-8')
        inventory(self.course, [source]); cm.prepare(self.course); cm.assemble(self.course)
        atomic_text(self.course / '学习需求.md', '# 学习需求\n基础学习')
        steps = '\n'.join(
            f'<!-- BL-STEP:BEGIN P-MAIN-S{i:03d} -->\n\n## 第{i}阶段\n\nP-MAIN-S{i:03d} ^P-MAIN-S{i:03d}\n\n'
            f'目标：掌握概念{i}\n\n<!-- BL-STEP:END P-MAIN-S{i:03d} -->\n' for i in range(1, n + 1))
        atomic_text(self.course / '学习路径.md',
                    '<!-- BL-PATH:BEGIN P-MAIN -->\n\n# 学习路径\n\nP-MAIN ^P-MAIN\n\n按序学习\n\n'
                    + steps + '\n<!-- BL-PATH:END P-MAIN -->\n')
        write_json(self.course / '_工作区/路径索引.json',
                   {'path_id': 'P-MAIN', 'title': '完整路线',
                    'steps': [{'id': f'P-MAIN-S{i:03d}', 'title': f'第{i}阶段', 'order': i,
                               'lessons': [f'L-{i:02d}']} for i in range(1, n + 1)]})
        atomic_text(self.course / '开始学习.md',
                    '<!-- BL-START:BEGIN START -->\n\n# 开始学习\n\nSTART ^START\n\n先看路线。\n\n'
                    '<!-- BL-START:END START -->\n')
        chapter = '_工作区/章节知识/KC-01.md'
        items = [{'id': f'K-{i:02d}', 'title': f'概念{i}', 'chapter_id': 'KC-01', 'anchor': f'K-{i:02d}',
                  'status': 'verified', 'kind': 'material', 'source_refs': [{'source_id':'SRC-001','unit_id':'U00001'}],
                  'lesson_ids': [f'L-{i:02d}'], 'prerequisites': [], 'core': False} for i in range(1,n+1)]
        definitions = [{'id': f'L-{i:02d}', 'title': f'章节{i}', 'order': i, 'path': f'学习文档/{i:02d}-章节.md',
                        'path_excerpt': f'第{i}章目标', 'status': 'pending'} for i in range(1,n+1)]
        write_json(self.course / '_工作区/章节索引.json', {'chapters':[{'id':'KC-01','title':'知识','order':1,'status':'complete','path':chapter}], 'lessons': definitions})
        atomic_text(self.course / chapter, '\n'.join(
            f'<!-- BL-K:BEGIN K-{i:02d} -->\n\n## 概念{i}\n\nK-{i:02d} ^K-{i:02d}\n\n概念内容{i}\n\n'
            f'<!-- BL-K:END K-{i:02d} -->\n' for i in range(1, n+1)))
        atomic_text(self.course / '_工作区/知识索引.jsonl', ''.join(json.dumps(k,ensure_ascii=False)+'\n' for k in items))
        links.build_map(self.course)
        atomic_text(self.course / '知识内容.md', build_content(self.course))
        lessons.prepare(self.course)

    def lesson_row(self, lesson_id):
        data = read_json(self.course / lessons.LEDGER)
        return next(l for l in data['lessons'] if l['id'] == lesson_id)

    def write_lesson(self, lesson_id, extra=''):
        """Main-agent equivalent: write every section chunk and the result receipt."""
        row = self.lesson_row(lesson_id)
        manifest = read_json(self.course / row['manifest'])
        body = (f'---\ntype: lesson\nid: {row["id"]}\ntitle: 测试\norder: {row["order"]}\n'
                'status: complete\n---\n# 讲义\n')
        for section in row['sections']:
            if section['kind'] == 'intro':
                content = body.split('# 讲义')[0] + '# 讲义\n'
            elif section['kind'] == 'closing':
                content = '## 章末\n测试复习与来源' + extra
            else:
                content = '\n'.join('## 小节\n\n教学解释\n' + manifest['links']['knowledge'][kid]
                                    for kid in section['knowledge_ids'])
            atomic_text(self.course / section['directory'] / '001.md', content)
            write_json(self.course / section['receipt'], {'schema_version': 1, 'section_id': section['id'],
                                                          'status': 'complete', 'files': ['001.md']})
        write_json(self.course / row['result'], {'status': 'complete', 'lesson_id': row['id'],
                                                 'knowledge_ids': row['knowledge_ids']})
        return row

    def finish_lessons(self):
        data = read_json(self.course / lessons.LEDGER)
        for row in data['lessons']:
            if row['status'] == 'done': continue
            self.write_lesson(row['id'])
            result = lessons.commit(self.course, row['id'])
            self.assertIsNone(result['failure_kind'], result)

    def finish_cards(self, n):
        atomic_text(self.course / '_工作区/核心候选.jsonl', '\n'.join(json.dumps({'concept_key':'shared','knowledge_ids':[f'K-{i:02d}'],'reason':'核心'}) for i in range(1,n+1)))
        cards.prepare(self.course)
        entry = read_json(self.course / '_工作区/核心卡片计划.json')['cards'][0]
        literals = list(entry['links']['knowledge'].values()) + list(entry['links']['exercises'].values())
        atomic_text(self.course / '核心知识点.md', self.card_document(entry, literals))
        cards.finalize(self.course)
        # Cards are active once finalized: re-render the course in the working phase.
        links.sync_course_relations(self.course, phase='working')

    def card_document(self, entry, literals):
        return ('# 核心知识点\n\n'
                + '<!-- BL-INDEX:BEGIN IDX-CORE -->\n\n## 核心复习目录\n\nIDX-CORE ^IDX-CORE\n\n'
                + '按知识卡片复习。\n\n<!-- BL-INDEX:END IDX-CORE -->\n\n'
                + f'<!-- BL-KP:BEGIN {entry["card_id"]} -->\n\n## 核心卡片\n\n'
                + f'{entry["card_id"]} ^{entry["card_id"]}\n\n统一定义\n'
                + '\n'.join(literals) + '\n\n'
                + f'<!-- BL-KP:END {entry["card_id"]} -->\n')

    def test_lessons_are_committed_one_by_one(self):
        """No chapter sub-agents: the main agent writes and commits each lesson."""
        self.knowledge()
        data = read_json(self.course / lessons.LEDGER)
        self.assertEqual(len([l for l in data['lessons'] if l['status'] != 'done']), 8)
        self.write_lesson('L-01')
        first = lessons.commit(self.course, 'L-01')
        self.assertIsNone(first['failure_kind'], first)
        self.assertEqual(self.lesson_row('L-01')['status'], 'done')
        self.assertEqual(self.lesson_row('L-02')['status'], 'pending')
        self.assertEqual(len(read_json(self.course / lessons.LEDGER)['commits']), 1)

    def test_lesson_input_change_rejected(self):
        self.knowledge(1); self.write_lesson('L-01')
        atomic_text(self.course / '学习需求.md', 'new requirements')
        result = lessons.commit(self.course, 'L-01')
        self.assertEqual(result['failure_kind'],'STALE_INPUT')

    def test_unknown_lesson_link_rejected(self):
        self.knowledge(1); self.write_lesson('L-01', '\n[[矩阵]]\n')
        result = lessons.commit(self.course, 'L-01')
        self.assertIsNotNone(result['failure_kind'])
        self.assertIn('BLOCK_REQUIRED', result.get('error', ''))
        self.assertEqual(self.lesson_row('L-01')['status'], 'failed')
        self.assertFalse((self.course / '学习文档/01-章节.md').exists())

    def test_cards_wait_for_lessons_and_global_dedup(self):
        self.knowledge(2)
        atomic_text(self.course / '_工作区/核心候选.jsonl', '\n'.join(json.dumps({'concept_key':'same concept','knowledge_ids':[f'K-{i:02d}'],'reason':'核心定义'}) for i in (1,2)))
        with self.assertRaises(ValueError): cards.prepare(self.course)
        self.finish_lessons(); self.assertFalse(lessons.check(self.course))
        self.assertEqual(cards.prepare(self.course)['cards'],1)
        self.assertFalse(lessons.check(self.course))  # Graph enrichment must not stale teaching content.
        plan=read_json(self.course / '_工作区/核心卡片计划.json')['cards']
        entry=plan[0]
        literals = list(entry['links']['knowledge'].values()) + list(entry['links']['exercises'].values())
        atomic_text(self.course / '核心知识点.md', self.card_document(entry, literals))
        self.assertTrue(cards.finalize(self.course)['complete'])
        self.assertFalse(cards.check(self.course))
        self.assertFalse(links.validate_obsidian_links(self.course))
        path=self.course/'学习文档/01-章节.md'; atomic_text(path,path.read_text('utf-8')+'changed')
        self.assertTrue(cards.check(self.course))

    def test_link_parser_code_and_invalid_targets(self):
        text='[[知识内容#^K-01|知识]]\n`[[忽略]]`\n```md\n[[忽略]]\n```\n[参考](https://example.com)'
        self.assertEqual(len(links.parse_wikilinks(text)),1)
        for target in ['../知识内容','./知识内容','C:\\Users\\x','/tmp/x','知识内容.md']:
            with self.assertRaises(ValueError): links.render_wikilink(target)
        moved=links.rewrite_targets(text,{'知识内容':'课程文档/知识内容'})
        self.assertIn('[[课程文档/知识内容#^K-01|知识]]',moved)
        self.assertIn('`[[忽略]]`',moved)

    def test_link_missing_duplicate_and_source_literal(self):
        self.knowledge(1); self.finish_lessons()
        # Missing/duplicate blocks are detected by the document-level check.
        self.assertTrue(links.validate_document(self.course,'[[知识内容#^K-missing]]'))
        self.assertTrue(links.validate_document(self.course,'text\n^K-01\n\ntext\n^K-01\n'))
        # A bare literal inside a source payload is not absorbed into system links.
        atomic_text(self.course/'资料转写/source.md','text [[foo]]')
        self.assertFalse(links.validate_obsidian_links(self.course,relationships=False))

    def test_rename_preserves_id_and_rewrites_links(self):
        self.knowledge(1); self.finish_lessons()
        # START out-edges only exist when registered in 开始入口.json.
        write_json(self.course / '_工作区/开始入口.json',
                   {'entries': [{'target_id': 'P-MAIN', 'subtype': 'entry'},
                                {'target_id': 'L-01', 'subtype': 'entry'},
                                {'target_id': 'IDX-CORE', 'subtype': 'entry'}],
                    'management': [], 'index_entry': True})
        atomic_text(self.course/'开始学习.md',
                    '<!-- BL-START:BEGIN START -->\n\n# 开始学习\n\nSTART ^START\n\n'
                    '先看 [[学习文档/01-章节#^L-01]]。\n\n<!-- BL-START:END START -->\n')
        result=links.rename_lesson(self.course,'L-01','学习文档/01-新名.md')
        self.assertEqual(result['lesson_id'],'L-01')
        self.assertIn('[[学习文档/01-新名#^L-01]]',(self.course/'开始学习.md').read_text('utf-8'))
        self.assertFalse(lessons.check(self.course))

    def test_package_rewrites_graph_and_preserves_receipts(self):
        from validate_package import validate, REQUIRED
        self.knowledge(2); self.finish_lessons(); self.finish_cards(2)
        for name in REQUIRED:
            if not (self.course/name).exists(): atomic_text(self.course/name,'# '+name.removesuffix('.md'))
        progress=read_json(self.course/'_工作区/生成进度.json')
        progress.update(confirmations={'goal':True,'materials':True,'baseline':True}, stage='complete', blockers=[])
        write_json(self.course/'_工作区/生成进度.json',progress)
        source=cm.load(self.course)['sources'][0]
        atomic_text(self.course/'_工作区/覆盖台账.jsonl',json.dumps({'source_id':source['id'],'source_hash':source['sha256'],
            'unit_id':'U00001','status':'covered','knowledge_ids':['K-01','K-02'],'evidence':'fixture verified'})+'\n')
        report=validate(self.course)
        self.assertTrue(report['passed'],report['errors'])
        cm.package(self.course)
        self.assertTrue((self.course/'课程文档/知识内容.md').exists())
        self.assertIn('[[课程文档/知识内容#^K-01|概念1]]',(self.course/'学习文档/01-章节.md').read_text('utf-8'))
        self.assertFalse(links.validate_obsidian_links(self.course))
        self.assertFalse(lessons.check(self.course))
        self.assertFalse(cards.check(self.course))
        self.assertFalse((self.course/'_工作区/讲义尝试').exists())
        self.assertFalse((self.course/'_工作区/讲义输入').exists())

    def test_strict_cost_and_mechanical_complexity_are_local(self):
        self.visual(12)
        cm.prepare(self.course, force_strict_units=['SRC-001/U00007'])
        units=cm.load(self.course)['sources'][0]['units']
        self.assertEqual([u['id'] for u in units if u['next_profile']=='strict'],['U00007'])
        self.assertIn('HIGH_COMPLEXITY',tasks.derive_strict_reasons({'render_required':True},{},100,12000))
        self.assertIn('COST_THRESHOLD',tasks.derive_strict_reasons({}, {}, 12000,12000))
        self.assertFalse(tasks.derive_strict_reasons({}, {'failure_kind':'TIMED_OUT'},100,12000))

    def test_source_change_stops_new_batches(self):
        self.visual(10); job=tasks.pump(self.course,'codex','fixture',max_fill=1)['tickets'][0]
        self.produce(job)
        with (self.root/'scan.pdf').open('ab') as f: f.write(b'change')
        result=cm.collect(self.course,attempt_id=job['attempt'],stopped_agent_id=job['member'])
        self.assertTrue(all(r['failure_kind']=='ASSET_CHANGED' for r in result['results']))

    def test_prerequisite_context_and_links_are_assigned(self):
        self.knowledge(2)
        path = self.course / '_工作区/知识索引.jsonl'
        rows = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
        rows[1]['prerequisites'] = ['K-01']
        atomic_text(path, ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))
        lessons.prepare(self.course)
        row = self.lesson_row('L-02')
        manifest = read_json(self.course / row['manifest'])
        self.assertIn('概念内容1', Path(manifest['inputs']['prerequisites']).read_text('utf-8'))
        self.assertEqual(manifest['links']['prerequisites']['K-01'], '[[知识内容#^K-01|概念1]]')
        self.assertGreaterEqual(len(manifest['reserved_exercises']), 24)
        self.assertNotIn('K-01', manifest['allowed_blocks'])
        self.assertFalse(manifest['agent_can_write_system_regions'])

    def test_source_filename_special_characters_remain_linkable(self):
        source = self.root / '教材[复习]#1%20.txt'
        source.write_text('text', encoding='utf-8')
        inventory(self.course, [source]); cm.prepare(self.course); cm.assemble(self.course)
        from entity_registry import Registry
        location = Registry(self.course).canonical('SRC-001-U00001')
        rendered = links.render_wikilink(location.path.removesuffix('.md'), block=location.block_id)
        self.assertEqual(len(links.parse_wikilinks(rendered)), 1)
        self.assertFalse(links.check_physical_target(self.course, location))
        self.assertTrue((self.course / location.path).exists())

    def test_frontmatter_identity_cannot_come_from_body(self):
        self.knowledge(1)
        self.write_lesson('L-01')
        row = self.lesson_row('L-01')
        path = self.course / row['sections'][0]['directory'] / '001.md'
        text = path.read_text('utf-8').replace('type: lesson', 'type: source').replace('id: L-01\n', '')
        atomic_text(path, text + '\nid: L-01\n')
        result = lessons.commit(self.course, 'L-01')
        self.assertEqual(result['failure_kind'], 'INVALID_OUTPUT')

    def test_cards_check_detects_changed_requirements(self):
        self.knowledge(1); self.finish_lessons(); self.finish_cards(1)
        atomic_text(self.course / '学习需求.md', 'new scope')
        self.assertTrue(cards.check(self.course))

    def test_rename_cannot_accept_unfinalized_card_edits(self):
        self.knowledge(1); self.finish_lessons(); self.finish_cards(1)
        card = self.course / '核心知识点.md'
        atomic_text(card, card.read_text('utf-8') + '\nchanged')
        with self.assertRaises(ValueError): links.rename_lesson(self.course, 'L-01', '学习文档/新名.md')
        self.assertTrue(cards.check(self.course))
        self.assertTrue((self.course / '学习文档/01-章节.md').exists())

    def test_link_relocation_preserves_external_urls(self):
        text = '[[知识内容]]\n[外部参考](https://example.com/[[知识内容]])'
        moved = links.rewrite_targets(text, {'知识内容': '课程文档/知识内容'})
        self.assertEqual(moved, '[[课程文档/知识内容]]\n[外部参考](https://example.com/[[知识内容]])')

    def test_new_course_cannot_bypass_lesson_receipts(self):
        from validate_package import validate
        self.knowledge(1); self.finish_lessons(); self.finish_cards(1)
        (self.course / lessons.LEDGER).unlink()
        report = validate(self.course)
        self.assertFalse(report['passed'])
        self.assertTrue(any('Missing lesson task ledger' in error for error in report['errors']))


if __name__ == '__main__': unittest.main()
