import tempfile
import unittest
from pathlib import Path
import zipfile
from cbr import convert_cbr
from companion import read_tags

class ConversionTests(unittest.TestCase):
    def test_preserves_content_and_tags(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)/'comics'; root.mkdir()
            p = root/'book.cbr'
            with zipfile.ZipFile(p,'w') as z:
                z.writestr('001.jpg', b'page bytes')
                z.writestr('ComicInfo.xml','<ComicInfo><Writer>Original</Writer></ComicInfo>')
            result = convert_cbr(p, root, Path(d)/'backups', {'Series':'Test [CV-1]', 'Volume':'2026', 'Number':'1'})
            target = Path(result['file'])
            self.assertFalse(p.exists())
            with zipfile.ZipFile(target) as z: self.assertEqual(z.read('001.jpg'),b'page bytes')
            self.assertEqual(read_tags(target)['Writer'],'Original')
            self.assertEqual(read_tags(target)['Volume'],'2026')

    def test_collision_keeps_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'book.cbr';p.write_bytes(b'original')
            p.with_suffix('.cbz').write_bytes(b'existing')
            with self.assertRaises(ValueError): convert_cbr(p, d, Path(d).parent/'backups', {})
            self.assertEqual(p.read_bytes(),b'original')
            self.assertEqual(p.with_suffix('.cbz').read_bytes(),b'existing')

    def test_unsafe_member_keeps_source(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'book.cbr'
            with zipfile.ZipFile(p,'w') as z: z.writestr('../001.jpg', b'page')
            before=p.read_bytes()
            with self.assertRaises(ValueError): convert_cbr(p,d,Path(d).parent/'backups',{})
            self.assertEqual(p.read_bytes(),before)
            self.assertFalse(p.with_suffix('.cbz').exists())
