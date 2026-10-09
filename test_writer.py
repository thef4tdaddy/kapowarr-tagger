import tempfile
import json
import base64
import unittest
from pathlib import Path
import zipfile
from writer import apply_one, sha256, inventory
from companion import read_tags


class WriterTests(unittest.TestCase):
    def test_preserves_pages_tags_and_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'comics'
            root.mkdir()
            p = root / '1.cbz'
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('001.jpg', b'page data' * 100)
                z.writestr('ComicInfo.xml', '<ComicInfo><Series>Deadpool</Series><Summary>Keep</Summary><Pages><Page Image="0"/></Pages></ComicInfo>')
            digest = sha256(p)
            inv = inventory(p)
            row = {'file': str(p), 'status': 'proposed', 'source_sha256': digest,
                   'changes': {'Volume': {'before': None, 'after': '2026'}}}
            result = apply_one(row, root, Path(d) / 'backup')
            backup = json.loads(Path(result['backup']).read_text())
            self.assertEqual(backup['original_archive_sha256'], digest)
            self.assertIn(b'<Summary>Keep</Summary>', base64.b64decode(backup['metadata_members']['ComicInfo.xml']))
            self.assertFalse(list((Path(d) / 'backup').glob('*.cbz')))
            self.assertEqual(inventory(p), inv)
            self.assertEqual(read_tags(p)['Summary'], 'Keep')
            self.assertEqual(read_tags(p)['Volume'], '2026')
            with self.assertRaises(ValueError):
                apply_one(row, root, Path(d) / 'backup')

    def test_rejects_unreviewed_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / '1.cbz'
            p.write_bytes(b'unchanged')
            with self.assertRaises(ValueError):
                apply_one({'file': str(p), 'status': 'proposed',
                           'changes': {'Series': {'before': None, 'after': 'x'}}}, d, '/tmp/backups')
            self.assertEqual(p.read_bytes(), b'unchanged')


if __name__ == '__main__':
    unittest.main()
