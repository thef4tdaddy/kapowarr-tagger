import json
import tempfile
import unittest
from pathlib import Path
import zipfile
from unittest.mock import Mock
from service import Service
from companion import read_tags
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch
from service import request_scan


class ServiceTests(unittest.TestCase):
    def test_scan_http_contract(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.end_headers()
            def log_message(self, *_):
                pass
        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as d:
                key = Path(d) / 'key'
                key.write_text('test-only-key')
                with patch.dict(os.environ, {'KAVITA_URL': f'http://127.0.0.1:{server.server_port}',
                                             'COMICS_ROOT': d, 'KAVITA_COMICS_ROOT': '/library', 'KAVITA_API_KEY_FILE': str(key)}):
                    request_scan(Path(d) / 'Test')
            self.assertEqual(seen, [('/api/Library/scan-folder', {'apiKey': 'test-only-key', 'folderPath': '/library/Test', 'abortOnNoSeriesMatch': False})])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def setup_library(self, d):
        root = Path(d) / 'comics'
        root.mkdir()
        p = root / 'one.cbz'
        with zipfile.ZipFile(p, 'w') as z:
            z.writestr('page.jpg', b'unchanged page')
        volumes = [{'id': 1, 'title': 'Test', 'year': 2026, 'comicvine_id': 123,
                    'issues': [{'issue_number': '1', 'files': [{'filepath': str(p)}]}]}]
        return root, p, volumes

    def test_write_retry_and_no_rewrite(self):
        with tempfile.TemporaryDirectory() as d:
            root, p, volumes = self.setup_library(d)
            svc = Service(root, Path(d) / 'state', 'write', settle=0)
            notify = Mock(side_effect=ConnectionError())
            self.assertEqual(svc.cycle(volumes, notify)[0]['status'], 'settling')
            notify.assert_not_called()
            self.assertEqual(svc.cycle(volumes, notify)[0]['status'], 'written')
            self.assertTrue(svc.state['pending_folders'])
            self.assertEqual(read_tags(p)['Volume'], '2026')
            before = p.read_bytes()
            # Restart must preserve notification retry without rewriting the book.
            svc = Service(root, Path(d) / 'state', 'write', settle=0)
            success = Mock()
            self.assertEqual(svc.cycle(volumes, success)[0]['status'], 'unchanged')
            success.assert_called_once()
            self.assertFalse(svc.state['pending_folders'])
            self.assertEqual(p.read_bytes(), before)
            self.assertFalse(list((Path(d) / 'state').rglob('*.cbz')))

    def test_preview_and_unmapped(self):
        with tempfile.TemporaryDirectory() as d:
            root, p, volumes = self.setup_library(d)
            (root / 'unknown.pdf').write_bytes(b'pdf')
            svc = Service(root, Path(d) / 'state', 'preview', settle=0)
            notify = Mock()
            before = p.read_bytes()
            svc.cycle(volumes, notify)
            report = svc.cycle(volumes, notify)
            self.assertEqual({r['status'] for r in report}, {'proposed', 'unmapped'})
            notify.assert_not_called()
            self.assertEqual(p.read_bytes(), before)

    def test_metadata_change_reprocessed(self):
        with tempfile.TemporaryDirectory() as d:
            root, p, volumes = self.setup_library(d)
            svc = Service(root, Path(d) / 'state', 'write', settle=0)
            svc.cycle(volumes, Mock()); svc.cycle(volumes, Mock())
            volumes[0]['year'] = 2025
            self.assertEqual(svc.cycle(volumes, Mock())[0]['status'], 'written')
            self.assertEqual(read_tags(p)['Volume'], '2025')


if __name__ == '__main__':
    unittest.main()

class WebhookTests(unittest.TestCase):
    def test_authenticated_event_only(self):
        import urllib.request
        import urllib.error
        from service import webhook_server, WAKE
        token = 'a' * 32
        server = webhook_server(token, 0)
        try:
            WAKE.clear()
            def post(path, event):
                req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/hooks/{path}',
                    data=json.dumps({'event': event}).encode(), method='POST')
                return urllib.request.urlopen(req)
            with self.assertRaises(urllib.error.HTTPError):
                post('wrong', 'download_completed')
            self.assertFalse(WAKE.is_set())
            with post(token, 'import_failed') as response:
                self.assertEqual(response.status, 202)
            self.assertFalse(WAKE.is_set())
            with self.assertLogs('tagger', level='INFO') as logs:
                with post(token, 'download_completed') as response:
                    self.assertEqual(response.status, 202)
            self.assertIn('Kapowarr webhook received: download_completed', logs.output[0])
            self.assertNotIn(token, str(logs.output))
            self.assertTrue(WAKE.is_set())
        finally:
            server.shutdown(); server.server_close(); WAKE.clear()

class CbrServiceTests(unittest.TestCase):
    def test_conversion_refresh_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'comics';root.mkdir()
            folder=root/'Series';folder.mkdir()
            p=folder/'one.cbr'
            with zipfile.ZipFile(p,'w') as z: z.writestr('001.jpg',b'page bytes')
            volumes=[{'id':1,'title':'Test','year':2026,'comicvine_id':123,
                      'issues':[{'issue_number':'1','files':[{'filepath':str(p)}]}]}]
            svc=Service(root,Path(d)/'state','write',settle=0)
            with patch.dict(os.environ,{'CONVERT_CBR':'true'}), patch('service.api',side_effect=ConnectionError()):
                svc.cycle(volumes,Mock())
                self.assertEqual(svc.cycle(volumes,Mock())[0]['status'],'converted')
            self.assertEqual(svc.state['pending_refresh'],[1])
            svc=Service(root,Path(d)/'state','write',settle=0)
            with patch('service.api') as api:
                svc.cycle(volumes,Mock())
                api.assert_called_once_with('system/tasks',{'cmd':'refresh_and_scan','volume_id':1})
            self.assertFalse(svc.state['pending_refresh'])


class RefreshQueueTests(unittest.TestCase):
    def test_collision_clears_legacy_refresh_without_requeue(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'comics';root.mkdir()
            p=root/'one.cbr';p.write_bytes(b'original')
            p.with_suffix('.cbz').write_bytes(b'existing')
            volumes=[{'id':48,'title':'Test','year':2026,'comicvine_id':123,
                      'issues':[{'issue_number':'1','files':[{'filepath':str(p)}]}]}]
            svc=Service(root,Path(d)/'state','write',settle=0)
            svc.state['pending_refresh']=[48];svc.save()
            with patch.dict(os.environ,{'CONVERT_CBR':'true'}), patch('service.api') as api:
                svc.cycle(volumes,Mock())
                for _ in range(2):
                    report=svc.cycle(volumes,Mock())
                    self.assertEqual(report[0]['status'],'review')
                    self.assertIn('target already exists',report[0]['reason'])
                api.assert_not_called()
            self.assertFalse(svc.state['pending_refresh'])
            self.assertEqual(p.read_bytes(),b'original')
            self.assertEqual(p.with_suffix('.cbz').read_bytes(),b'existing')
