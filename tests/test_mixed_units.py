"""Order, attachment scope and upgrade regressions for mixed Office materials."""
import argparse
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'better-learning/scripts'))
from _common import read_json, write_json
from inventory_materials import inventory
from extract_materials import extract

R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'


def raster(fmt='PNG', frames=1):
    from PIL import Image
    stream = io.BytesIO()
    first = Image.new('RGB', (16, 16), 'white')
    first.save(stream, format=fmt, save_all=True, append_images=[Image.new('RGB', (16, 16), 'black')] * (frames - 1)) if frames > 1 else first.save(stream, format=fmt)
    return stream.getvalue()


class MixedUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.course = self.root / 'course'

    def tearDown(self):
        self.tmp.cleanup()

    def run_extract(self, filename, entries):
        src = self.root / filename
        with zipfile.ZipFile(src, 'w') as z:
            for name, value in entries.items():
                z.writestr(name, value)
        inventory(self.course, [src])
        self.args = argparse.Namespace(start=1, end=100, max_chars=256, scale=1)
        self.sid = read_json(self.course / '_工作区/资料索引.json')['sources'][0]['id']
        output = extract(self.course, self.sid, self.args)
        self.map = Path(output['map'])
        return read_json(self.map)['units']

    def texts(self, units):
        return [''.join((self.course / p).read_text(encoding='utf-8') for p in u['chunks']) for u in units]

    def test_docx_image_stays_between_text_and_notes_not_duplicated(self):
        units = self.run_extract('mixed.docx', {
            'word/document.xml': f'<document xmlns:r="{R}"><p><t>before</t><drawing><blip r:embed="pic"/></drawing><t>after</t></p><tbl><tr><tc><p><t>table</t></p></tc></tr></tbl></document>',
            'word/_rels/document.xml.rels': '<Relationships><Relationship Id="pic" Target="media/a.png" Type="image"/></Relationships>',
            'word/footnotes.xml': '<footnotes><p><t>footnote</t></p></footnotes>',
            'word/endnotes.xml': '<endnotes><p><t>endnote</t></p></endnotes>',
            'word/media/a.png': raster(), 'word/media/unused.png': b'unused'})
        self.assertEqual([u['kind'] for u in units], ['text', 'image', 'text', 'text', 'text'])
        self.assertEqual(self.texts(units)[0], 'before')
        self.assertIn('after', self.texts(units)[2])
        self.assertIn('table', self.texts(units)[2])
        self.assertEqual(sum(len(u['images']) for u in units), 1)
        self.assertEqual(units[1]['assets'], units[1]['images'])
        self.assertIn('paragraph 1', units[1]['locator'])
        self.assertTrue(units[1]['images'][0].endswith('-normalized.png'))
        self.assertIn('footnote', self.texts(units)[3])

    def test_pptx_shape_order_then_notes(self):
        units = self.run_extract('mixed.pptx', {
            'ppt/presentation.xml': f'<presentation xmlns:r="{R}"><sldId r:id="s"/></presentation>',
            'ppt/_rels/presentation.xml.rels': '<Relationships><Relationship Id="s" Target="slides/slide1.xml"/></Relationships>',
            'ppt/slides/slide1.xml': f'<slide xmlns:r="{R}"><sp><p><t>first</t></p></sp><pic><blip r:embed="pic"/></pic><sp><p><t>last</t></p></sp></slide>',
            'ppt/slides/_rels/slide1.xml.rels': '<Relationships><Relationship Id="pic" Target="../media/a.png"/><Relationship Id="notes" Target="../notesSlides/notesSlide1.xml" Type="x/notesSlide"/></Relationships>',
            'ppt/media/a.png': raster(),
            'ppt/notesSlides/notesSlide1.xml': '<notes><sp><p><t>speaker notes</t></p></sp></notes>'})
        self.assertEqual([u['kind'] for u in units], ['text', 'image', 'text', 'text'])
        self.assertEqual(self.texts(units), ['first', '', 'last', 'speaker notes'])
        self.assertIn('notes', units[-1]['locator'])
        self.assertIn('shape 2', units[1]['locator'])

    def test_jpeg_normalized_and_multiframe_or_vector_blocked(self):
        for suffix, data, blocked in [('jpg', raster('JPEG'), False), ('tiff', raster('TIFF', 2), True), ('svg', b'<svg/>', True)]:
            with self.subTest(suffix=suffix):
                units = self.run_extract('asset.docx', {
                    'word/document.xml': f'<document xmlns:r="{R}"><p><blip r:embed="pic"/></p></document>',
                    'word/_rels/document.xml.rels': f'<Relationships><Relationship Id="pic" Target="media/a.{suffix}"/></Relationships>',
                    f'word/media/a.{suffix}': data})
                self.assertEqual(units[0]['status'] == 'blocked', blocked)
                if blocked:
                    self.assertTrue(units[0]['unresolved_media'])
                    self.assertEqual(units[0]['assets'], [])
                else:
                    self.assertTrue(units[0]['assets'][0].endswith('.png'))

    def test_legacy_mapping_is_backed_up_before_repartition(self):
        self.run_extract('plain.docx', {'word/document.xml': '<document><p><t>hello</t></p></document>'})
        legacy = read_json(self.map)
        legacy.pop('layout_version')
        legacy['total_units'] = 20
        write_json(self.map, legacy)
        extract(self.course, self.sid, self.args)
        backups = list(self.map.parent.glob('定位映射.legacy-*.json'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(read_json(backups[0])['total_units'], 20)
        self.assertEqual(read_json(self.map)['total_units'], 1)

    def test_missing_image_is_explicitly_blocked(self):
        units = self.run_extract('broken.docx', {'word/document.xml': f'<document xmlns:r="{R}"><drawing><blip r:embed="missing"/></drawing></document>'})
        self.assertEqual(units[0]['kind'], 'image')
        self.assertEqual(units[0]['status'], 'blocked')
        self.assertTrue(units[0]['unresolved_media'])


if __name__ == '__main__':
    unittest.main()
