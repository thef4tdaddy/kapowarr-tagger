"""Opt-in retention. Fail closed, export before deletion, preserve historical sessions."""
import hashlib
import json
import logging
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from companion import api, secret
from writer import sha256

LOG = logging.getLogger('tagger')

class RetentionError(RuntimeError):
    """Credential-safe operational diagnostic."""



def durable_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    if json.loads(path.read_text()) != data:
        raise RetentionError('History verification failed')


class Kavita:
    def __init__(self):
        self.url = os.environ['KAVITA_URL'].rstrip('/')
        self.key = secret('KAVITA_API_KEY')
        self.scope = hashlib.sha256((self.url + '\n' + self.key).encode()).hexdigest()

    def request(self, route, body=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        req = urllib.request.Request(self.url + '/api/' + route,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'x-api-key': self.key, 'Content-Type': 'application/json'})
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
                data = response.read()
                return (json.loads(data) if data else None), response.headers
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise RetentionError(f'Kavita retention API HTTP {code}') from None
        except (OSError, ValueError):
            raise RetentionError('Kavita retention API unavailable or invalid response') from None

    def paged(self, route, body=None):
        result, seen = [], set()
        for page in range(1, 10001):
            separator = '&' if '?' in route else '?'
            batch, headers = self.request(route + separator + f'pageNumber={page}&pageSize=100', body)
            if not isinstance(batch, list):
                raise RetentionError('Invalid paginated response')
            if not batch:
                return result
            signature = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
            if signature in seen:
                raise RetentionError('Repeated pagination; cleanup blocked')
            seen.add(signature)
            result.extend(batch)
            if headers.get('Pagination'):
                pages = int(json.loads(headers['Pagination'])['totalPages'])
                if page >= pages:
                    return result
        raise RetentionError('Pagination limit; cleanup blocked')

    def keep(self, list_id):
        info, _ = self.request(f'ReadingList?readingListId={list_id}')
        if not isinstance(info, dict) or info.get('id') != list_id:
            raise RetentionError('Keep list unavailable')
        items, _ = self.request(f'ReadingList/items?readingListId={list_id}')
        if not isinstance(items, list):
            raise RetentionError('Invalid Keep list')
        return {int(item['chapterId']) for item in items}

    def chapter(self, chapter_id):
        chapter, _ = self.request(f'Series/chapter?chapterId={chapter_id}')
        if not isinstance(chapter, dict) or chapter.get('id') != chapter_id:
            raise RetentionError('Invalid chapter')
        return chapter

    def inventory(self):
        books = []
        for series in self.paged('Series/all-v2', {'statements': []}):
            volumes, _ = self.request(f"Series/volumes?seriesId={int(series['id'])}")
            if not isinstance(volumes, list):
                raise RetentionError('Invalid volumes')
            for volume in volumes:
                for stub in volume['chapters']:
                    books.append({'series': series, 'chapter': self.chapter(int(stub['id']))})
        return books

    def history(self, series_id):
        return self.paged(f'Stats/reading-history/series/{series_id}?tzId=Etc%2FUTC')

    def restore(self, series_id, chapter_id):
        self.request('Reader/mark-chapter-read', {'seriesId': series_id,
            'chapterId': chapter_id, 'generateReadingSession': False})


def complete(chapter):
    pages, read = chapter.get('pages'), chapter.get('pagesRead')
    return (type(pages) is int and type(read) is int and pages > 0 and read >= pages)


def stamp(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns]


class Retention:
    def __init__(self, directory, root, kavita_root, client, keep_id, mode='preview', days=30, kap=api, stopped=lambda: False):
        if mode not in ('preview', 'write') or days < 30 or keep_id <= 0:
            raise RetentionError('Retention needs preview/write, Keep ID, and at least 30 days')
        self.directory, self.root = Path(directory), Path(root).resolve()
        self.kroot = Path(kavita_root)
        if not self.kroot.is_absolute() or self.directory.resolve().is_relative_to(self.root):
            raise RetentionError('Invalid retention paths')
        self.client, self.keep_id, self.mode, self.days, self.kap = client, keep_id, mode, days, kap
        self.stopped = stopped
        self.max_deletes = max(1, int(os.environ.get('CLEANUP_MAX_DELETIONS', '10')))
        self.path = self.directory / 'history' / 'historical-log.json'
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            'schema': 1, 'scope': client.scope, 'candidates': {}, 'records': [], 'restorations': {}}
        if self.data.get('schema') != 1 or self.data.get('scope') != client.scope:
            raise RetentionError('History account/server scope changed; review before cleanup')

    def save(self):
        durable_json(self.path, self.data)

    def queue_notice(self, event_id, file, status, **fields):
        queue = self.data.setdefault('notification_outbox', {})
        queue.setdefault(event_id, dict(file=file, status=status, mode=self.mode, **fields))

    def flush_notices(self, discord, now=None, allow_warnings=True):
        now = time.time() if now is None else now
        queue = self.data.setdefault('notification_outbox', {})
        for event_id, event in list(queue.items())[:20]:
            if event['status'] in ('countdown_started', 'three_days'):
                if not allow_warnings:
                    continue
                candidate = self.data['candidates'].get(event['identity'])
                # Cancel queued warnings if Keep/unread/changed/missing reset the countdown.
                if (not candidate or candidate.get('notice_token') != event['token']
                        or now >= event['due'] or event['mode'] != self.mode
                        or (event['status'] == 'countdown_started' and now >= event['due'] - 3 * 86400)):
                    del queue[event_id]
                    self.save()
                    continue
                remaining = max(1, int((event['due'] - now + 86399) // 86400))
                label = f"Deletion in {remaining} days; scheduled <t:{int(event['due'])}:f>"
                if event['mode'] == 'preview':
                    label = f"Preview: eligible in {remaining} days (<t:{int(event['due'])}:f>); deletion disabled"
                message = label + '\n' + event['file'][:300] + '\nAdd to Keep to retain it.'
            else:
                message = 'Kapowarr Tagger: ' + event['status'] + '\n' + event['file'][:300]
            try:
                if not discord.send(message):
                    return
            except Exception as exc:
                if hasattr(exc, 'close'):
                    exc.close()
                LOG.warning('Retention Discord notification failed (%s); retained for retry', type(exc).__name__)
                return
            del queue[event_id]
            self.save()

    def mapped(self, books, volumes, allowed):
        maps = {}
        for volume in volumes:
            for issue in volume.get('issues', []):
                for file in issue.get('files', []):
                    maps.setdefault(file['filepath'], []).append((volume, issue, file))
        results, counts = [], {}
        for book in books:
            chapter = book['chapter']
            files = chapter.get('files', [])
            if len(files) != 1:
                continue  # No destructive guesses for collections spanning files.
            raw = Path(files[0]['filePath'])
            try:
                relative = raw.relative_to(self.kroot)
                path = self.root / relative
                if not relative.parts or '..' in relative.parts or path.is_symlink() or not path.resolve().is_relative_to(self.root):
                    continue
                matches = maps.get(str(path), [])
                if len(matches) != 1 or allowed.get(str(path)) not in ('unchanged', 'correct') or not path.is_file():
                    continue
                volume, issue, file = matches[0]
                if not volume.get('comicvine_id') or not issue.get('comicvine_id') or not file.get('id'):
                    continue
                key = f"{int(volume['comicvine_id'])}:{int(issue['comicvine_id'])}"
                item = dict(book, path=path, volume=volume, issue=issue, file=file, key=key)
                results.append(item)
                counts[key] = counts.get(key, 0) + 1
            except (ValueError, KeyError):
                continue
        # Also reject one physical file represented by multiple Kavita chapters.
        path_counts = {}
        for item in results:
            path_counts[str(item['path'])] = path_counts.get(str(item['path']), 0) + 1
        return [b for b in results if counts[b['key']] == 1 and path_counts[str(b['path'])] == 1]

    def cycle(self, volumes, report, now=None):
        now = time.time() if now is None else now
        keep = self.client.keep(self.keep_id)  # A missing list never means "protect nothing".
        books = self.client.inventory()  # Finish the full snapshot before any mutation.
        allowed = {r['file']: r['status'] for r in report}
        mapped = self.mapped(books, volumes, allowed)
        summary = {'mode': self.mode, 'checked': len(books), 'mapped': len(mapped),
                   'protected': 0, 'waiting': 0, 'eligible': 0, 'deleted': 0,
                   'restore_proposed': 0, 'restored': 0, 'scan_libraries': [], 'events': []}
        seen = set()
        for book in mapped:
            if self.stopped():
                raise RetentionError('Cleanup interrupted before next book')
            key, chapter, path = book['key'], book['chapter'], book['path']
            seen.add(key)
            cid, sid = chapter['id'], book['series']['id']
            current = stamp(path)
            previous = next((r for r in reversed(self.data['records'])
                if r['identity'] == key and r['outcome'] in ('deleted', 'delete_requested')), None)
            restore_key = f'{key}:{cid}:{current}'
            if previous and restore_key not in self.data['restorations']:
                digest = sha256(path)
                if stamp(path) != current:
                    raise RetentionError('File changed during restore inspection')
                if digest != previous['sha256']:
                    # Same CV ID alone cannot prove same edition/content.
                    self.data['candidates'].pop(key, None)
                    summary['protected'] += 1
                    continue
                summary['restore_proposed'] += 1
                self.data['candidates'].pop(key, None)
                if self.mode == 'write':
                    fresh = self.client.chapter(cid)
                    if fresh.get('files') != chapter.get('files'):
                        raise RetentionError('Chapter changed before restoration')
                    # Preserve newer partial progress; do not overwrite a reread.
                    if fresh.get('pagesRead') == 0:
                        self.client.restore(sid, cid)
                        if not complete(self.client.chapter(cid)):
                            raise RetentionError('Completion restoration not confirmed')
                        summary['restored'] += 1
                        summary['events'].append({'file': path.name, 'status': 'restored'})
                        self.queue_notice('restored:' + restore_key, path.name, 'completion restored')
                    self.data['restorations'][restore_key] = {'time': now, 'chapter_id': cid}
                    self.save()
                continue  # No deletion in the restoration cycle.
            if cid in keep or not complete(chapter):
                self.data['candidates'].pop(key, None)
                summary['protected'] += 1
                continue
            candidate = self.data['candidates'].get(key)
            marker = {'chapter_id': cid, 'file': str(path), 'stamp': current,
                      'progress_time': chapter.get('lastReadingProgressUtc')}
            if not candidate or candidate['marker'] != marker:
                candidate = {'since': now, 'marker': marker}
                self.data['candidates'][key] = candidate
            due = candidate['since'] + self.days * 86400
            token = candidate.setdefault('notice_token', str(uuid.uuid4()))
            # Preview and write each have their own one-time warning milestones.
            notified = candidate.setdefault('notice_stages', [])
            stage = 'three_days' if due - now <= 3 * 86400 else 'countdown_started'
            stage_key = self.mode + ':' + stage
            if now < due and stage_key not in notified:
                self.queue_notice(token + ':' + stage_key, path.name, stage,
                    identity=key, token=token, due=due)
                notified.append(stage_key)
            if now < due:
                summary['waiting'] += 1
                continue
            summary['eligible'] += 1
            summary['events'].append({'file': path.name, 'status': 'eligible'})
            if self.mode != 'write' or summary['deleted'] >= self.max_deletes:
                continue
            history = self.client.history(sid)  # Failure blocks deletion; [] is valid.
            digest = sha256(path)
            if stamp(path) != current:
                raise RetentionError('File changed during history export')
            fresh = self.client.chapter(cid)
            if cid in self.client.keep(self.keep_id) or not complete(fresh) or fresh.get('files') != chapter.get('files') or fresh.get('lastReadingProgressUtc') != chapter.get('lastReadingProgressUtc'):
                self.data['candidates'].pop(key, None)
                continue
            # Re-fetch Kapowarr mapping immediately before exporting/removing.
            volume = self.kap('volumes/' + str(book['volume']['id']))
            refs = [(i, f) for i in volume['issues'] for f in i.get('files', [])
                    if f['id'] == book['file']['id'] or f['filepath'] == str(path)]
            if len(refs) != 1 or refs[0][0]['id'] != book['issue']['id'] or refs[0][1]['filepath'] != str(path) or refs[0][1]['id'] != book['file']['id']:
                raise RetentionError('Kapowarr mapping changed; deletion blocked')
            record = {'id': str(uuid.uuid4()), 'identity': key, 'sha256': digest,
                'exported_at': now, 'completed_observed_at': candidate['since'],
                'series': book['series'], 'chapter': fresh, 'file': book['file'],
                'comicvine_volume_id': book['volume']['comicvine_id'],
                'comicvine_issue_id': book['issue']['comicvine_id'],
                'kapowarr_volume': {k: book['volume'].get(k) for k in ('id', 'title', 'year', 'comicvine_id')},
                'kapowarr_issue': {k: book['issue'].get(k) for k in ('id', 'issue_number', 'title', 'date', 'comicvine_id')},
                'series_reading_history': history, 'outcome': 'exported', 'events': []}
            self.data['records'].append(record)
            self.save()  # fsync and read-back must succeed before any destructive request.
            self.kap('issues/' + str(book['issue']['id']), {'monitored': False}, method='PUT')
            check = self.kap('issues/' + str(book['issue']['id']))
            if check.get('monitored') is not False:
                raise RetentionError('Unmonitor not confirmed; deletion blocked')
            # Last-minute guards after remote requests and durable export.
            fresh = self.client.chapter(cid)
            if cid in self.client.keep(self.keep_id) or not complete(fresh) or fresh.get('lastReadingProgressUtc') != chapter.get('lastReadingProgressUtc') or fresh.get('files') != chapter.get('files') or stamp(path) != current:
                record['outcome'] = 'cancelled'
                self.data['candidates'].pop(key, None)
                self.save()
                continue
            latest_file = self.kap('files/' + str(book['file']['id']))
            if latest_file.get('filepath') != str(path):
                raise RetentionError('File ID now maps to another path; deletion blocked')
            if self.stopped():
                raise RetentionError('Cleanup interrupted before deletion')
            record['outcome'] = 'delete_requested'
            library_id = int(book['series']['libraryId'])
            self.data.setdefault('pending_scan_libraries', [])
            if library_id not in self.data['pending_scan_libraries']:
                self.data['pending_scan_libraries'].append(library_id)
            self.save()  # Crash leaves an explicit uncertain outcome, never a false success.
            self.kap('files/' + str(book['file']['id']), method='DELETE')
            if path.exists():
                raise RetentionError('Kapowarr deletion not confirmed on disk')
            record['outcome'] = 'deleted'
            record['deleted_at'] = now
            self.queue_notice('deleted:' + record['id'], path.name, 'deleted — history saved')
            self.data['candidates'].pop(key, None)
            self.save()
            summary['deleted'] += 1
            summary['events'].append({'file': path.name, 'status': 'deleted'})
            summary['scan_libraries'].append(int(book['series']['libraryId']))
        for key in set(self.data['candidates']) - seen:
            del self.data['candidates'][key]  # Missing/ambiguous files lose their countdown.
        self.save()
        if self.mode == 'write':
            for library_id in list(self.data.get('pending_scan_libraries', [])):
                self.client.request(f'Library/scan?libraryId={library_id}&force=false', {})
                self.data['pending_scan_libraries'].remove(library_id)
                self.save()
        durable_json(self.directory / 'retention-report.json', dict(summary, time=now,
            candidates=self.data['candidates']))
        return summary


def run_retention(service, volumes, report, discord):
    mode = os.environ.get('CLEANUP_MODE', 'off').lower()
    if mode == 'off':
        return
    if mode not in ('preview', 'write'):
        raise RetentionError('CLEANUP_MODE must be off, preview, or write')
    if mode == 'write' and service.mode != 'write':
        raise RetentionError('Cleanup write requires tagger MODE=write')
    interval = max(300, int(os.environ.get('CLEANUP_INTERVAL_SECONDS', '3600')))
    if time.time() - service.state.get('last_retention_check', 0) < interval:
        return
    retention = Retention(service.directory, service.root,
        os.environ['KAVITA_COMICS_ROOT'], Kavita(),
        int(os.environ.get('KEEP_READING_LIST_ID') or '0'), mode,
        int(os.environ.get('CLEANUP_DAYS', '30')), stopped=lambda: service.stopping())
    succeeded = False
    try:
        result = retention.cycle(volumes, report)
        succeeded = True
    finally:
        # A later scan/API failure must not lose already-confirmed deletion notices.
        retention.flush_notices(discord, allow_warnings=succeeded)
    LOG.info('Retention check: mode=%s mapped=%s protected=%s waiting=%s eligible=%s deleted=%s restored=%s',
        mode, result['mapped'], result['protected'], result['waiting'], result['eligible'], result['deleted'], result['restored'])
    service.state['last_retention_check'] = time.time()
    service.save()
