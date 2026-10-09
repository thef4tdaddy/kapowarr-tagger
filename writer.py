"""Explicit, one-shot application of reviewed metadata plans with rollback copies."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from companion import read_tags


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(set(names)) != len(names):
            raise ValueError('Duplicate ZIP members')
        result = {}
        for info in z.infolist():
            if info.filename == 'ComicInfo.xml':
                continue
            with z.open(info) as f:
                h = hashlib.sha256()
                for block in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(block)
            result[info.filename] = (info.file_size, h.hexdigest())
        return result


def apply_one(row, root, backups):
    p = Path(row['file'])
    root = Path(root).resolve()
    if p.is_symlink() or not p.resolve().is_relative_to(root) or p.suffix.lower() != '.cbz':
        raise ValueError('Unsafe path')
    if row.get('status') != 'proposed':
        return {'file': str(p), 'status': 'skipped'}
    changes = row['changes']
    if not changes or set(changes) - {'Series', 'Volume', 'Number'}:
        raise ValueError('Only reviewed grouping fields may change')
    if not row.get('source_sha256') or sha256(p) != row['source_sha256']:
        raise ValueError('Source no longer matches reviewed plan')
    old = read_tags(p)
    if old.get('LocalizedSeries') or old.get('SeriesSort'):
        raise ValueError('Grouping override requires review')
    for k, change in changes.items():
        if old.get(k) != change['before'] or not isinstance(change['after'], str) or not change['after']:
            raise ValueError('Invalid or stale field change')
    original_stat = p.stat()
    original_inventory = inventory(p)
    backup = Path(backups).resolve() / (row['source_sha256'] + '.metadata.json')
    if backup.is_relative_to(root):
        raise ValueError('Backups must be outside the scanned comics root')
    backup.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p) as src:
        data = {'original_archive_sha256': row['source_sha256'],
                'file': str(p),
                'metadata_members': {n: base64.b64encode(src.read(n)).decode()
                                     for n in src.namelist() if n == 'ComicInfo.xml'},
                'archive_comment': base64.b64encode(src.comment).decode()}
    if backup.exists():
        existing = json.loads(backup.read_text())
        if any(existing.get(k) != data[k] for k in ('original_archive_sha256', 'metadata_members', 'archive_comment')):
            raise ValueError('Existing metadata backup differs')
    else:
        with backup.open('x') as dst:
            json.dump(data, dst, indent=2)
            dst.flush()
            os.fsync(dst.fileno())
    verified = json.loads(backup.read_text())
    if verified['metadata_members'] != data['metadata_members']:
        raise ValueError('Metadata backup verification failed')
    fd, temp = tempfile.mkstemp(prefix='.kapowarr-tagger-', suffix='.tmp', dir=p.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(p) as src, zipfile.ZipFile(temp, 'w', allowZip64=True) as dst:
            dst.comment = src.comment
            if 'ComicInfo.xml' in src.namelist():
                parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
                xml = ET.fromstring(src.read('ComicInfo.xml'), parser=parser)
            else:
                xml = ET.Element('ComicInfo')
            for k, change in changes.items():
                child = xml.find(k)
                if child is None:
                    child = ET.SubElement(xml, k)
                child.text = change['after']
            for info in src.infolist():
                if info.filename == 'ComicInfo.xml':
                    continue
                with src.open(info) as inp, dst.open(info, 'w', force_zip64=True) as out:
                    shutil.copyfileobj(inp, out, 1024 * 1024)
            dst.writestr('ComicInfo.xml', ET.tostring(xml, encoding='utf-8', xml_declaration=True),
                         compress_type=zipfile.ZIP_DEFLATED)
        if inventory(temp) != original_inventory:
            raise ValueError('Page/content verification failed')
        result_tags = read_tags(temp)
        for k, val in old.items():
            if k not in changes and result_tags.get(k) != val:
                raise ValueError('Unrelated metadata changed')
        for k, val in changes.items():
            if result_tags.get(k) != val['after']:
                raise ValueError('Written tags failed validation')
        if sha256(p) != row['source_sha256']:
            raise ValueError('Source changed during rewrite')
        os.chmod(temp, original_stat.st_mode & 0o777)
        with open(temp, 'rb') as f:
            os.fsync(f.fileno())
        os.replace(temp, p)
        return {'file': str(p), 'status': 'written', 'backup': str(backup),
                'before_sha256': row['source_sha256'], 'after_sha256': sha256(p)}
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--backups', required=True)
    parser.add_argument('--journal', required=True)
    args = parser.parse_args()
    # Exclusive lock prevents simultaneous writers sharing this journal.
    import fcntl
    with open(args.journal, 'a') as journal:
        fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for row in json.loads(Path(args.plan).read_text()):
            result = apply_one(row, args.root, args.backups)
            result['time'] = time.time()
            journal.write(json.dumps(result) + '\n')
            journal.flush()
            os.fsync(journal.fileno())
            print(json.dumps(result), flush=True)


if __name__ == '__main__':
    main()
