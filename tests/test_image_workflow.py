"""Regression cases for image-only reading and coverage."""
import argparse
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
from _common import atomic_text, extraction_path, read_json, write_json
from extract_materials import extract
from inventory_materials import inventory
from prepare_visual import prepare, record, check_manifest, boxes
from validate_package import validate


class ImageWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='better-learning-images-')
        self.root = Path(self.temp.name)
        self.course = self.root / 'course'
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def extract_file(self, path):
        inventory(self.course, [path])
        source = read_json(self.course / '_工作区/资料索引.json')['sources'][0]
        args = argparse.Namespace(start=1, end=10, max_chars=10000, scale=2, ocr='never', ocr_lang='eng')
        extract(self.course, source['id'], args)
        mapping = read_json(extraction_path(self.course, source) / '定位映射.json')
        return source, mapping

    def image_source(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (1700, 2100), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((30, 30), 'Speed = distance / time', fill='black')
        draw.rectangle((1650, 2050, 1699, 2099), fill='red')
        path = self.inputs / 'scan.png'
        image.save(path)
        return self.extract_file(path)

    def test_tiles_cover_all_pixels_at_original_resolution(self):
        from PIL import Image, ImageDraw
        source, mapping = self.image_source()
        result = prepare(self.course, source['id'], 'U00001')
        data = read_json(self.course / result['manifest'])
        mask = Image.new('L', tuple(data['dimensions']), 0)
        with Image.open(self.course / data['image']) as original:
            for tile in data['tiles']:
                x0, y0, x1, y1 = tile['box']
                ImageDraw.Draw(mask).rectangle((x0, y0, x1 - 1, y1 - 1), fill=1)
                with Image.open(self.course / tile['path']) as cropped:
                    self.assertLessEqual(cropped.width, 1280)
                    self.assertLessEqual(cropped.height, 1600)
                    self.assertEqual(cropped.tobytes(), original.crop(tile['box']).tobytes())
        self.assertEqual(mask.getextrema(), (1, 1))
        with Image.open(self.course / data['overview']) as overview:
            self.assertLessEqual(max(overview.size), 1200)
        self.assertFalse(result['ready_for_synthesis'])
        self.assertEqual(mapping['units'][0]['content_mode'], 'visual_first')

    def test_region_notes_survive_restart_and_unreadable_is_not_complete(self):
        source, _ = self.image_source()
        result = prepare(self.course, source['id'], 'U00001')
        note = '_工作区/视觉笔记/first.md'
        atomic_text(self.course / note, 'Actual fixture note: the first region contains a definition of speed.')
        result = record(self.course, result['manifest'], result['next']['id'], note)
        resumed = prepare(self.course, source['id'], 'U00001')
        self.assertEqual(resumed['read'], 1)
        self.assertEqual(resumed['next'], result['next'])
        for tile in read_json(self.course / result['manifest'])['tiles'][1:]:
            path = f'_工作区/视觉笔记/{tile["id"]}.md'
            atomic_text(self.course / path, 'Unreadable area recorded for test.')
            record(self.course, result['manifest'], tile['id'], path, 'unreadable')
        done = prepare(self.course, source['id'], 'U00001')
        self.assertIsNone(done['next'])
        self.assertFalse(done['ready_for_synthesis'])
        self.assertTrue(check_manifest(self.course, read_json(self.course / result['manifest']), require_read=True))
        atomic_text(self.course / note, 'Changed without re-recording')
        with self.assertRaisesRegex(ValueError, '笔记'):
            prepare(self.course, source['id'], 'U00001')

    def test_multiframe_tiff_reads_every_frame(self):
        from PIL import Image
        path = self.inputs / 'pages.tiff'
        Image.new('RGB', (360, 480), 'red').save(path, save_all=True,
                                               append_images=[Image.new('RGB', (360, 480), 'blue')])
        _, mapping = self.extract_file(path)
        self.assertEqual(mapping['total_units'], 2)
        for index, color in enumerate([(255, 0, 0), (0, 0, 255)]):
            unit = mapping['units'][index]
            self.assertEqual(unit['status'], 'needs_review')
            with Image.open(self.course / unit['images'][0]) as image:
                self.assertEqual(image.getpixel((10, 10)), color)

    def test_exif_rotation_is_applied(self):
        from PIL import Image
        path = self.inputs / 'rotated.jpg'
        exif = Image.Exif()
        exif[274] = 6
        Image.new('RGB', (640, 320), 'white').save(path, exif=exif)
        _, mapping = self.extract_file(path)
        with Image.open(self.course / mapping['units'][0]['images'][0]) as result:
            self.assertEqual(result.size, (320, 640))

    def test_pure_scan_pdf_uses_visual_path_without_ocr(self):
        from PIL import Image
        from reportlab.pdfgen.canvas import Canvas
        raster = self.inputs / 'scan.png'
        Image.new('RGB', (800, 1000), 'white').save(raster)
        pdf = self.inputs / 'scan.pdf'
        canvas = Canvas(str(pdf))
        canvas.drawImage(str(raster), 0, 0, 500, 700)
        canvas.save()
        source, mapping = self.extract_file(pdf)
        unit = mapping['units'][0]
        self.assertEqual(unit['native_text_characters'], 0)
        self.assertEqual(unit['content_mode'], 'visual_first')
        self.assertEqual((self.course / unit['chunks'][0]).read_text(encoding='utf-8'), '')
        result = prepare(self.course, source['id'], unit['id'])
        self.assertIsNotNone(result['next'])
        with patch('pypdf.PdfReader', side_effect=ValueError('simulated text parser failure')):
            _, fallback = self.extract_file(pdf)
        self.assertEqual(fallback['units'][0]['status'], 'needs_review')
        self.assertTrue(fallback['units'][0]['images'])

    def test_visual_coverage_requires_complete_region_evidence(self):
        source, _ = self.image_source()
        coverage = {'source_id': source['id'], 'source_hash': source['sha256'], 'unit_id': 'U00001',
                    'status': 'non_teaching', 'knowledge_ids': [], 'reason': 'Structural fixture',
                    'visual_reviewed': True, 'evidence': 'Structural fixture note'}
        cov = self.course / '_工作区/覆盖台账.jsonl'
        atomic_text(cov, json.dumps(coverage))
        self.assertTrue(any('缺少逐区域' in e for e in validate(self.course)['errors']))
        result = prepare(self.course, source['id'], 'U00001')
        coverage['visual_manifests'] = [result['manifest']]
        coverage['visual_note'] = '_工作区/视觉笔记/page.md'
        atomic_text(self.course / coverage['visual_note'], 'Fixture page note.')
        atomic_text(cov, json.dumps(coverage))
        self.assertTrue(any('尚未读完' in e for e in validate(self.course)['errors']))
        for tile in read_json(self.course / result['manifest'])['tiles']:
            note = f'_工作区/视觉笔记/{tile["id"]}.md'
            atomic_text(self.course / note, 'Fixture per-region evidence.')
            record(self.course, result['manifest'], tile['id'], note)
        self.assertFalse(any('视觉分块' in e for e in validate(self.course)['errors']))



if __name__ == '__main__':
    unittest.main(verbosity=2)
