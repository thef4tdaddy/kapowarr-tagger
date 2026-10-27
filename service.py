"""Headless polling service. Preview by default; explicit write mode required."""
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from companion import KapowarrAPIError, api, desired_tags, read_tags, secret
from writer import apply_one, sha256

LOG = logging.getLogger('tagger')
STOP = threading.Event()
WAKE = threading.Event()
WEBHOOK_SEEN = threading.Event()


def atomic_json(path, data):
    temp = path.with_suffix('.tmp')
    with temp.open('w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def fingerprint(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns]


def identity(value):
    return unicodedata.normalize('NFC', str(value))


def check_kavita():
    """Read-only authentication probe; does not prove scan authorization."""
    try:
        url = os.environ.get('KAVITA_URL', '').rstrip('/')
        key = secret('KAVITA_API_KEY')
        if urllib.parse.urlparse(url).scheme not in ('http', 'https'):
            raise ValueError('Invalid URL')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        request = urllib.request.Request(url + '/api/Library/libraries',
            headers={'x-api-key': key, 'Accept': 'application/json'}, method='GET')
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            data = json.load(response)
            if response.status != 200 or not isinstance(data, list):
                return {'status': 'invalid_response'}
            if any(not isinstance(item, dict) or 'id' not in item for item in data):
                return {'status': 'invalid_response'}
            return {'status': 'connected', 'accessible_libraries': len(data)}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {'status': 'authentication_failed' if exc.code == 401 else
                'access_denied' if exc.code == 403 else 'http_error', 'http_status': exc.code}
    except (KeyError, ValueError, OSError) as exc:
        # Never expose credential-bearing URLs, response bodies, or exception strings.
        return {'status': 'configuration_error' if isinstance(exc, (KeyError, ValueError)) else 'connection_failed'}


def request_scan(folder):
    url = os.environ['KAVITA_URL'].rstrip('/')
    key = secret('KAVITA_API_KEY')
    root = Path(os.environ.get('COMICS_ROOT', '/comics')).resolve()
    relative = Path(folder).resolve().relative_to(root)
    if not relative.parts:
        raise ValueError('Refusing library-root folder request')
    target = Path(os.environ['KAVITA_COMICS_ROOT']) / relative
    if not key or not target.is_absolute():
        raise ValueError('Missing key or absolute Kavita comics root')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(
        url + '/api/Library/scan-folder',
        data=json.dumps({'apiKey': key, 'folderPath': str(target),
                         'abortOnNoSeriesMatch': False}).encode(),
        method='POST', headers={'Content-Type': 'application/json'})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        if response.status not in (200, 202, 204):
            raise ValueError('Scan request not accepted')


def webhook_server(token, port):
    if len(token) < 24:
        raise ValueError('Webhook token must contain at least 24 characters')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # URL contains a secret; never log it.
        def do_POST(self):
            self.connection.settimeout(5)
            if not hmac.compare_digest(self.path, '/hooks/' + token):
                self.send_error(403); return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, TimeoutError):
                self.send_error(400); return
            if payload.get('event') == 'download_completed':
                WEBHOOK_SEEN.set()
                LOG.info('Kapowarr webhook received: download_completed; incremental check requested')
                WAKE.set()
            else:
                LOG.info('Kapowarr webhook received: authenticated event ignored (not download_completed)')
            self.send_response(202); self.end_headers()
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Service:
    def __init__(self, root, state_dir, mode='preview', settle=300):
        if mode not in ('preview', 'write'):
            raise ValueError('MODE must be preview or write')
        self.root = Path(root).resolve()
        self.directory = Path(state_dir).resolve()
        if self.directory.is_relative_to(self.root):
            raise ValueError('State directory must be outside the comics library')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mode, self.settle = mode, settle
        self.path = self.directory / 'state.json'
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'files': {}, 'pending_folders': []}

        self.state.setdefault('pending_refresh', [])
        self.state.setdefault('pending_folders', [])
        if self.state.pop('pending_scan', False):
            # Upgrade old outbox without falling back to a full-library request.
            self.state['pending_folders'] = sorted(set(self.state['pending_folders']) |
                {str(Path(k).parent) for k in self.state['files']})

    def save(self):
        atomic_json(self.path, self.state)

    def cycle(self, volumes, notify=request_scan, probe=check_kavita):
        connection = probe()
        LOG.info('Kavita connection check (read-only): %s', connection)
        mappings = {}
        for volume in volumes:
            for issue in volume.get('issues', []):
                for file in issue.get('files', []):
                    mappings.setdefault(identity(file['filepath']), []).append((file['filepath'], volume, issue))
        report = []
        changed = 0
        selected = set(filter(None, os.environ.get('VOLUME_IDS', '').split(',')))
        for key, matches in sorted(mappings.items()):
            if STOP.is_set():
                break
            if selected and not any(str(v['id']) in selected for _, v, _ in matches):
                continue
            path = Path(matches[0][0])
            row = {'file': str(path)}
            try:
                if len(matches) != 1:
                    raise ValueError('Multiple issue mappings; review required')
                if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    raise ValueError('Path outside comics root or symlink')
                if path.suffix.lower() not in ('.cbz', '.cbr'):
                    raise ValueError('Unsupported format; CBZ or CBR required')
                _, volume, issue = matches[0]
                desired = desired_tags(volume, issue)
                signature = hashlib.sha256(json.dumps(desired, sort_keys=True).encode()).hexdigest()
                current = fingerprint(path)
                old_state = self.state['files'].get(key, {})
                if old_state.get('observed') != current:
                    self.state['files'][key] = {'observed': current, 'first_seen': time.time()}
                    row['status'] = 'settling'
                    report.append(row)
                    continue
                if (time.time() - old_state['first_seen'] < self.settle
                        or time.time() - path.stat().st_mtime < self.settle):
                    row['status'] = 'settling'
                    report.append(row)
                    continue
                if old_state.get('done') == current and old_state.get('signature') == signature:
                    row['status'] = 'unchanged'
                    report.append(row)
                    continue
                if path.suffix.lower() == '.cbr':
                    if self.mode != 'write' or os.environ.get('CONVERT_CBR', 'false').lower() != 'true':
                        row['status'] = 'conversion_proposed'
                        report.append(row)
                        continue
                    if path.with_suffix('.cbz').exists():
                        raise ValueError('CBZ target already exists; review before conversion')
                    from cbr import convert_cbr
                    def queue_conversion_refresh():
                        # Called only after output verification, immediately before source removal.
                        # Persist first so interruption cannot lose a needed mapping refresh.
                        if volume['id'] not in self.state['pending_refresh']:
                            self.state['pending_refresh'].append(volume['id'])
                        folder = str(path.parent)
                        if folder not in self.state['pending_folders']:
                            self.state['pending_folders'].append(folder)
                        self.save()
                    result = convert_cbr(path, self.root, self.directory / 'metadata-backups',
                                         desired, before_commit=queue_conversion_refresh)
                    with (self.directory / 'journal.jsonl').open('a') as journal:
                        journal.write(json.dumps(dict(result, time=time.time())) + '\n')
                        journal.flush(); os.fsync(journal.fileno())
                    row.update(result)
                    report.append(row)
                    continue
                tags = read_tags(path)
                if tags.get('LocalizedSeries') or tags.get('SeriesSort'):
                    raise ValueError('Grouping override present; review required')
                changes = {k: {'before': tags.get(k), 'after': v} for k, v in desired.items() if tags.get(k) != v}
                if fingerprint(path) != current:
                    raise ValueError('File changed while being inspected')
                row.update(status='proposed' if changes else 'correct', changes=changes)
                if self.mode == 'write':
                    # Persist notification intent BEFORE writing; survives interruption.
                    folder = str(path.parent)
                    if folder not in self.state['pending_folders']:
                        self.state['pending_folders'].append(folder)
                    self.save()
                    if changes:
                        row['source_sha256'] = sha256(path)
                        if fingerprint(path) != current:
                            raise ValueError('Source changed while hashing')
                        result = apply_one(row, self.root, self.directory / 'metadata-backups')
                        with (self.directory / 'journal.jsonl').open('a') as journal:
                            journal.write(json.dumps(dict(result, time=time.time())) + '\n')
                            journal.flush()
                            os.fsync(journal.fileno())
                        row['status'] = 'written'
                        changed += 1
                    after = fingerprint(path)
                    self.state['files'][key] = {'observed': after, 'first_seen': time.time(),
                                               'done': after, 'signature': signature}
                    self.save()
            except Exception as exc:
                row.update(status='review', reason=str(exc))
            report.append(row)
        # Account for files not present in Kapowarr's issue/file mappings.
        for path in self.root.rglob('*'):
            if path.suffix.lower() in ('.cbz', '.cbr', '.cb7', '.cbt', '.pdf', '.epub') and path.is_file():
                if identity(path) not in mappings:
                    report.append({'file': str(path), 'status': 'unmapped'})
        self.save()
        if self.mode == 'write':
            for volume_id in list(self.state['pending_refresh']):
                # Reconcile old requests from rejected conversions, or a refresh that
                # completed server-side despite a timeout. Keep requests with stale paths.
                paths = [Path(f['filepath']) for v in volumes if str(v['id']) == str(volume_id)
                         for issue in v.get('issues', []) for f in issue.get('files', [])]
                if paths and all(p.resolve().is_relative_to(self.root) and p.is_file() for p in paths):
                    self.state['pending_refresh'].remove(volume_id)
                    self.save()
                    LOG.info('Cleared unnecessary Kapowarr refresh for volume %s: mapped files exist', volume_id)
                    continue
                try:
                    api('system/tasks', {'cmd': 'refresh_and_scan', 'volume_id': int(volume_id)})
                    self.state['pending_refresh'].remove(volume_id)
                    self.save()
                except Exception as exc:
                    LOG.warning('Kapowarr refresh failed (%s); retained for retry', type(exc).__name__)
        scan = 'not_requested'
        blocked = {str(Path(r['file']).parent) for r in report
                   if r['status'] in ('settling', 'review')}
        if self.mode == 'write' and not STOP.is_set():
            for folder in list(self.state['pending_folders']):
                if folder in blocked:
                    continue
                try:
                    notify(folder)
                    self.state['pending_folders'].remove(folder)
                    self.state['last_scan_requested'] = time.time()
                    self.save()
                    scan = 'accepted'
                except Exception as exc:
                    scan = 'pending_retry'
                    LOG.warning('Kavita folder request failed (%s); retained for retry', type(exc).__name__)
        summary = {status: sum(r['status'] == status for r in report) for status in {r['status'] for r in report}}
        atomic_json(self.directory / 'report.json', {'time': time.time(), 'mode': self.mode,
                    'counts': summary, 'scan_request': scan, 'kavita_connection': connection, 'files': report})
        atomic_json(self.directory / 'health.json', {'last_success': time.time(), 'mode': self.mode})
        LOG.info('Cycle complete: %s; scan=%s', summary, scan)
        return report


def next_delay(webhook_seen, report, state, mode, failed=False, poll=300, settle=300):
    """None means idle until a webhook; only actionable work schedules follow-ups."""
    if failed or not webhook_seen:
        return max(60, poll)
    if any(row['status'] == 'settling' for row in report):
        return max(60, settle)
    blocked = {str(Path(row['file']).parent) for row in report
               if row['status'] == 'review'}
    if mode == 'write' and (state.get('pending_refresh') or
            any(folder not in blocked for folder in state.get('pending_folders', []))):
        return max(60, poll)
    return None


def wait_for_work(service, delay, healthy=True):
    # Refresh liveness while idle; this performs no library/API polling.
    deadline = None if delay is None else time.monotonic() + delay
    while not STOP.is_set():
        if healthy:
            atomic_json(service.directory / 'health.json',
                        {'last_success': time.time(), 'mode': service.mode, 'idle': True})
        remaining = 60 if deadline is None else min(60, max(0, deadline - time.monotonic()))
        if remaining <= 0 or WAKE.wait(remaining):
            return


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (STOP.set(), WAKE.set()))
    service = Service(os.environ.get('COMICS_ROOT', '/comics'), os.environ.get('STATE_DIR', '/state'),
                      os.environ.get('MODE', 'preview'), int(os.environ.get('SETTLE_SECONDS', '300')))
    with (service.directory / 'service.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = secret('WEBHOOK_TOKEN', required=False)
        token_fingerprint = hashlib.sha256(token.encode()).hexdigest() if token else None
        WEBHOOK_SEEN.clear()
        if token and service.state.get('webhook_verified_token') == token_fingerprint:
            WEBHOOK_SEEN.set()
        server = webhook_server(token, int(os.environ.get('WEBHOOK_PORT', '8080'))) if token else None
        while not STOP.is_set():
            WAKE.clear()
            failed, report = False, []
            try:
                # Fetch complete snapshot before modifying anything; fail closed on API errors.
                volumes = [api('volumes/' + str(v['id'])) for v in api('volumes')]
                report = service.cycle(volumes)
            except Exception as exc:
                failed = True
                LOG.error('Cycle failed (%s); retry scheduled',
                          str(exc) if isinstance(exc, KapowarrAPIError) else type(exc).__name__)
            if os.environ.get('RUN_ONCE') == 'true':
                break
            verified = bool(token) and WEBHOOK_SEEN.is_set()
            if verified and service.state.get('webhook_verified_token') != token_fingerprint:
                service.state['webhook_verified_token'] = token_fingerprint
                service.save()
                LOG.info('Webhook verified; switching from fallback polling to event-driven checks')
            delay = next_delay(verified, report, service.state, service.mode, failed,
                               int(os.environ.get('POLL_SECONDS', '300')), service.settle)
            if delay is None:
                LOG.info('Idle: waiting for webhook; no library polling scheduled')
            else:
                LOG.info('Next check in %s seconds (%s)', delay,
                         'follow-up/retry' if verified else 'webhook not yet verified')
            wait_for_work(service, delay, healthy=not failed)
        if server:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
