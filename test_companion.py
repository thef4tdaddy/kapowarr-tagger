import tempfile
import unittest
import zipfile
from pathlib import Path
from companion import desired_tags, read_tags, plan


class Tests(unittest.TestCase):
    def test_distinct_same_year_runs(self):
        issue = {'issue_number': '1'}
        a = desired_tags({'title': 'Batman', 'year': 2024, 'comicvine_id': 1}, issue)
        b = desired_tags({'title': 'Batman', 'year': 2024, 'comicvine_id': 2}, issue)
        self.assertNotEqual(a['Series'], b['Series'])
        self.assertEqual(a['Volume'], '2024')

    def test_missing_identity_rejected(self):
        with self.assertRaises(ValueError):
            desired_tags({'title': 'Batman', 'year': 2024}, {'issue_number': '1'})

    def test_read_without_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'book.cbz'
            with zipfile.ZipFile(p, 'w') as z:
                z.writestr('ComicInfo.xml', '<ComicInfo><Summary>Keep me</Summary></ComicInfo>')
            before = p.read_bytes()
            self.assertEqual(read_tags(p)['Summary'], 'Keep me')
            self.assertEqual(before, p.read_bytes())

    def test_ambiguous_mapping_held(self):
        v = {'id': 1, 'issues': [{'files': [{'filepath': '/comics/book.cbz'}]},
                               {'files': [{'filepath': '/comics/book.cbz'}]}]}
        rows = list(plan([v], '/comics'))
        self.assertEqual(rows[0]['status'], 'review')
        self.assertIn('multiple', rows[0]['reason'])


if __name__ == '__main__':
    unittest.main()
