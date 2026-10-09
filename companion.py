"""Read-only Kapowarr metadata planner. Never modifies comic archives."""
import json
import logging
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

LOG = logging.getLogger('tagger')


def api(route):
    key = Path(os.environ['KAPOWARR_API_KEY_FILE']).read_text().strip()
    url = os.environ.get('KAPOWARR_URL', 'http://kapowarr:5656').rstrip('/')
    request = urllib.request.Request(url + '/api/' + route + '?' +
                                     urllib.parse.urlencode({'api_key': key}))
    # Do not follow redirects carrying credentials to a different destination.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        body = json.load(response)
    if body.get('error'):
        raise ValueError('Kapowarr returned an API error')
    return body['result']


def read_tags(path):
    with zipfile.ZipFile(path) as archive:
        members = [i for i in archive.infolist()
                   if i.filename.lower().split('/')[-1] == 'comicinfo.xml']
        if not members:
            return {}
        if len(members) != 1 or members[0].filename != 'ComicInfo.xml':
            raise ValueError('Duplicate, nested, or noncanonical ComicInfo.xml; review required')
        if members[0].file_size > 2_000_000:
            raise ValueError('Oversized ComicInfo.xml')
        data = archive.read(members[0])
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('XML declarations require review')
        root = ET.fromstring(data)
        if root.tag != 'ComicInfo':
            raise ValueError('Unexpected ComicInfo root')
        tags = {}
        for child in root:
            if child.tag in tags:
                raise ValueError('Repeated metadata field; review required')
            tags[child.tag] = child.text or ''
        return tags


def desired_tags(volume, issue):
    title = str(volume.get('title') or '').strip()
    year = volume.get('year')
    cv = volume.get('comicvine_id')
    number = str(issue.get('issue_number') or '').strip()
    if not title or not year or not cv or not number:
        raise ValueError('Missing confirmed title, run year, Comic Vine identity, or issue number')
    # This is a proposed identity, intentionally visible in preview. Distinguishes
    # same-title/same-year series and trade vs single series without relying on year alone.
    return {'Series': f'{title} [CV-{int(cv)}]', 'Volume': str(int(year)),
            'Number': number}


def plan(volumes, root, only=()):
    root = Path(root).resolve()
    mappings = {}
    for volume in volumes:
        for issue in volume.get('issues', []):
            for file in issue.get('files', []):
                mappings.setdefault(file['filepath'], []).append((volume, issue))
    for filename, matches in sorted(mappings.items()):
        if only and not any(str(v['id']) in only for v, _ in matches):
            continue
        row = {'file': filename}
        try:
            if len(matches) != 1:
                raise ValueError('File maps to multiple issues or volumes; manual review')
            path = Path(filename)
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Path outside comics root or symlink')
            if path.suffix.lower() != '.cbz':
                raise ValueError('Only CBZ supported in this preview')
            before = path.stat()
            if time.time() - before.st_mtime < 300:
                raise ValueError('Recently changed file; retry later')
            old = read_tags(path)
            new = desired_tags(*matches[0])
            if old.get('LocalizedSeries') or old.get('SeriesSort'):
                raise ValueError('Grouping override tag present; review required')
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ValueError('File changed during inspection')
            row['changes'] = {k: {'before': old.get(k), 'after': val}
                              for k, val in new.items() if old.get(k) != val}
            row['status'] = 'proposed' if row['changes'] else 'unchanged'
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            row.update(status='review', reason=str(exc))
        yield row


def run():
    volumes = [api('volumes/' + str(v['id'])) for v in api('volumes')]
    selected = tuple(filter(None, os.environ.get('VOLUME_IDS', '').split(',')))
    rows = list(plan(volumes, os.environ.get('COMICS_ROOT', '/comics'), selected))
    directory = Path(os.environ.get('STATE_DIR', '/state'))
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / 'preview.tmp'
    temporary.write_text(json.dumps(rows, indent=2) + '\n')
    temporary.replace(directory / 'preview.json')
    LOG.info('Preview complete: %s files, %s proposals, %s need review; no comics changed',
             len(rows), sum(r['status'] == 'proposed' for r in rows),
             sum(r['status'] == 'review' for r in rows))


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    while True:
        try:
            run()
        except Exception as exc:
            # HTTP exceptions can contain the secret-bearing request URL.
            LOG.error('Preview failed (%s); check API access and mounts', type(exc).__name__)
        time.sleep(max(60, int(os.environ.get('POLL_SECONDS', '900'))))
