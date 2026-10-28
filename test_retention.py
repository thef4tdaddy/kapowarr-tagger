import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from retention import Retention, RetentionError, Kavita, complete

DAY = 86400

class FakeKavita:
    scope = 'test-account'
    def __init__(self, path):
        self.protected = set()
        self.book = {'series': {'id': 2, 'libraryId': 1, 'name': 'Test'},
                     'chapter': {'id': 3, 'pages': 10, 'pagesRead': 10,
                         'lastReadingProgressUtc': '2026-01-01',
                         'files': [{'id': 4, 'filePath': '/comics/book.cbz'}]}}
        self.restores, self.scans = [], []
    def keep(self, _): return self.protected
    def inventory(self): return [copy.deepcopy(self.book)]
    def chapter(self, _): return copy.deepcopy(self.book['chapter'])
    def history(self, _): return [{'sessionId': 5, 'chapters': [{'chapterId': 3}]}]
    def restore(self, sid, cid):
        self.restores.append((sid, cid))
        self.book['chapter']['pagesRead'] = 10
    def request(self, route, body): self.scans.append(route); return None, {}

class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / 'comics'; self.root.mkdir()
        self.file = self.root / 'book.cbz'; self.file.write_bytes(b'comic pages')
        self.state = self.base / 'state'
        self.client = FakeKavita(self.file)
        self.volume = {'id': 7, 'comicvine_id': 77, 'issues': [
            {'id': 8, 'comicvine_id': 88, 'issue_number': '1', 'monitored': True,
             'files': [{'id': 9, 'filepath': str(self.file)}]}]}
        self.calls = []
        self.report = [{'file': str(self.file), 'status': 'unchanged'}]
        self.ret = self.make()
    def make(self, mode='write'):
        return Retention(self.state, self.root, '/comics', self.client, 1, mode=mode, kap=self.kap)
    def kap(self, route, payload=None, method=None):
        self.calls.append((route, payload, method))
        if route == 'volumes/7': return copy.deepcopy(self.volume)
        if route == 'issues/8':
            if method == 'PUT': self.volume['issues'][0]['monitored'] = payload['monitored']
            return copy.deepcopy(self.volume['issues'][0])
        if route == 'files/9' and method is None:
            return copy.deepcopy(self.volume['issues'][0]['files'][0])
        if route == 'files/9' and method == 'DELETE':
            saved = json.loads(self.ret.path.read_text())['records'][-1]
            self.assertEqual(saved['outcome'], 'delete_requested')
            self.assertTrue(saved['series_reading_history'])
            self.assertFalse(self.volume['issues'][0]['monitored'])
            self.file.unlink()
            return {}
        raise AssertionError(route)
    def cycle(self, now): return self.ret.cycle([self.volume], self.report, now=now)
    def test_observed_countdown_then_export_before_delete(self):
        self.assertEqual(self.cycle(0)['waiting'], 1)
        self.assertEqual(self.cycle(29*DAY)['deleted'], 0)
        self.assertEqual(self.cycle(30*DAY)['deleted'], 1)
        self.assertFalse(self.file.exists())
        self.assertEqual(self.ret.data['records'][0]['outcome'], 'deleted')
        self.assertEqual(self.client.scans, ['Library/scan?libraryId=1&force=false'])
    def test_preview_never_mutates_remote_or_file(self):
        self.ret = self.make('preview')
        self.cycle(0)
        self.assertEqual(self.cycle(31*DAY)['eligible'], 1)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.file.exists())
    def test_keep_and_unread_cancel_countdown(self):
        self.cycle(0)
        self.client.protected = {3}; self.cycle(31*DAY)
        self.assertEqual(self.ret.data['candidates'], {})
        self.client.protected = set(); self.cycle(32*DAY)
        self.client.book['chapter']['pagesRead'] = 1; self.cycle(63*DAY)
        self.assertEqual(self.ret.data['candidates'], {})
        self.assertTrue(self.file.exists())
    def test_missing_keep_fails_closed(self):
        self.cycle(0)
        with patch.object(self.client, 'keep', side_effect=ValueError('missing')):
            with self.assertRaises(ValueError): self.cycle(31*DAY)
        self.assertEqual(self.calls, [])
    def test_export_failure_never_unmonitors_or_deletes(self):
        self.cycle(0)
        with patch('retention.durable_json', side_effect=OSError('full disk')):
            with self.assertRaises(OSError): self.cycle(31*DAY)
        self.assertFalse(any(c[2] in ('PUT', 'DELETE') for c in self.calls))
        self.assertTrue(self.file.exists())
    def test_history_failure_blocks_delete(self):
        self.cycle(0)
        with patch.object(self.client, 'history', side_effect=ValueError('API failed')):
            with self.assertRaises(ValueError): self.cycle(31*DAY)
        self.assertEqual(self.calls, [])
    def test_new_keep_at_final_check_cancels_delete(self):
        self.cycle(0)
        with patch.object(self.client, 'keep', side_effect=[set(), set(), {3}]):
            self.cycle(31*DAY)
        self.assertTrue(self.file.exists())
        self.assertEqual(self.ret.data['records'][0]['outcome'], 'cancelled')
    def test_ambiguous_mapping_and_review_are_skipped(self):
        self.volume['issues'].append(copy.deepcopy(self.volume['issues'][0]))
        self.assertEqual(self.cycle(0)['mapped'], 0)
        self.volume['issues'].pop(); self.report[0]['status'] = 'review'
        self.assertEqual(self.cycle(0)['mapped'], 0)
    def test_multi_file_chapter_skipped(self):
        self.client.book['chapter']['files'] *= 2
        self.assertEqual(self.cycle(0)['mapped'], 0)
    def test_reimport_restores_once_and_restarts_countdown(self):
        self.cycle(0); self.cycle(30*DAY)
        self.file.write_bytes(b'comic pages')
        self.client.book['chapter']['id'] = 30
        self.client.book['chapter']['pagesRead'] = 0
        self.ret = self.make()
        self.assertEqual(self.cycle(31*DAY)['restored'], 1)
        self.assertEqual(self.client.restores, [(2,30)])
        self.assertEqual(self.cycle(32*DAY)['waiting'], 1)
        self.assertEqual(self.cycle(33*DAY)['restored'], 0)
        self.assertTrue(self.file.exists())
    def test_changed_edition_not_restored_or_deleted(self):
        self.cycle(0); self.cycle(30*DAY)
        self.file.write_bytes(b'different edition')
        self.client.book['chapter']['id'] = 30
        self.assertEqual(self.cycle(31*DAY)['protected'], 1)
        self.assertEqual(self.cycle(70*DAY)['deleted'], 0)
    def test_corrupt_or_changed_account_history_blocks(self):
        self.cycle(0)
        self.client.scope = 'different'
        with self.assertRaises((ValueError, RetentionError)): self.make()
        self.ret.path.write_text('broken json')
        with self.assertRaises((ValueError, RetentionError)): self.make()
    def test_unmonitor_failure_blocks_delete(self):
        self.cycle(0)
        original = self.kap
        def bad(route, payload=None, method=None):
            if method == 'PUT': return {}
            return original(route, payload, method)
        self.ret.kap = bad
        with self.assertRaises(RetentionError): self.cycle(30*DAY)
        self.assertTrue(self.file.exists())
    def test_pending_scan_survives_failure_and_restart(self):
        self.cycle(0)
        with patch.object(self.client, 'request', side_effect=ValueError('scan failed')):
            with self.assertRaises(ValueError): self.cycle(30*DAY)
        self.assertFalse(self.file.exists())
        self.ret = self.make()
        self.assertEqual(self.ret.data['pending_scan_libraries'], [1])
        self.cycle(31*DAY)
        self.assertEqual(self.ret.data['pending_scan_libraries'], [])
    def test_changed_mapping_blocks_deletion(self):
        self.cycle(0)
        self.ret.kap = lambda *a, **kw: {'issues': []}
        with self.assertRaises(RetentionError): self.cycle(30*DAY)
        self.assertTrue(self.file.exists())
    def test_stop_blocks_deletion(self):
        self.cycle(0)
        self.ret.stopped = lambda: True
        with self.assertRaises(RetentionError): self.cycle(30*DAY)
        self.assertTrue(self.file.exists())
    def test_partial_reread_is_not_overwritten(self):
        self.cycle(0); self.cycle(30*DAY)
        self.file.write_bytes(b'comic pages')
        self.client.book['chapter']['id'] = 30
        self.client.book['chapter']['pagesRead'] = 4
        self.cycle(31*DAY)
        self.assertEqual(self.client.restores, [])
        self.assertEqual(self.client.book['chapter']['pagesRead'], 4)
    def test_history_pagination_rejects_repeated_page(self):
        client = object.__new__(Kavita)
        with patch.object(client, 'request', return_value=([{'sessionId': 1}], {})):
            with self.assertRaises(RetentionError): client.history(2)
    def test_keep_list_malformed_is_not_empty(self):
        client = object.__new__(Kavita)
        with patch.object(client, 'request', side_effect=[({'id': 1}, {}), (None, {})]):
            with self.assertRaises(RetentionError): client.keep(1)
    def test_restore_request_does_not_fabricate_sessions(self):
        client = object.__new__(Kavita)
        with patch.object(client,'request') as request:
            client.restore(2,3)
            self.assertEqual(request.call_args.args[1],
                {'seriesId':2,'chapterId':3,'generateReadingSession':False})
    def test_completion_requires_real_page_counts(self):
        self.assertFalse(complete({'pages':0,'pagesRead':0}))
        self.assertFalse(complete({'pages':10,'pagesRead':9}))
        self.assertTrue(complete({'pages':10,'pagesRead':10}))

if __name__ == '__main__': unittest.main()
