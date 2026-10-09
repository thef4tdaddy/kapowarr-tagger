"""Verified CBR-to-CBZ conversion. Never recompress image content lossily."""
import hashlib
import base64
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile
import libarchive
from writer import sha256, inventory, apply_one
from companion import read_tags


def convert_cbr(source, root, backups, desired):
    source, root = Path(source), Path(root).resolve()
    if source.is_symlink() or not source.resolve().is_relative_to(root) or source.suffix.lower() != '.cbr':
        raise ValueError('Unsafe CBR source')
    target = source.with_suffix('.cbz')
    if target.exists():
        raise ValueError('CBZ target already exists; review before conversion')
    if shutil.disk_usage(source.parent).free < source.stat().st_size * 3 + 1024**3:
        raise ValueError('Insufficient conversion working space')
    original_hash = sha256(source)
    mode = source.stat().st_mode & 0o777
    fd, temporary = tempfile.mkstemp(prefix='.kapowarr-cbr-', suffix='.cbz', dir=source.parent)
    os.close(fd)
    temp = Path(temporary)
    published = False
    try:
        expected, names = {}, set()
        with libarchive.file_reader(str(source)) as archive, zipfile.ZipFile(temp, 'w', allowZip64=True) as output:
            for entry in archive:
                name = entry.pathname
                path = PurePosixPath(name)
                if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                        or name in names or ':' in name):
                    raise ValueError('Unsafe or duplicate archive member')
                names.add(name)
                if entry.isdir:
                    output.writestr(name.rstrip('/') + '/', b'')
                    expected[name.rstrip('/') + '/'] = (0, hashlib.sha256(b'').hexdigest())
                    continue
                if not entry.isfile or entry.issym or entry.islnk:
                    raise ValueError('Archive contains a link or special member')
                h, size = hashlib.sha256(), 0
                with output.open(name, 'w', force_zip64=True) as dst:
                    for block in entry.get_blocks():
                        dst.write(block); h.update(block); size += len(block)
                if size != entry.size:
                    raise ValueError('Incomplete archive member')
                if name != 'ComicInfo.xml':
                    expected[name] = (size, h.hexdigest())
        if not expected or not any(n.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif')) for n in expected):
            raise ValueError('No comic pages found')
        if inventory(temp) != expected:
            raise ValueError('Conversion content verification failed')
        old = read_tags(temp)
        if old.get('LocalizedSeries') or old.get('SeriesSort'):
            raise ValueError('Grouping override requires review')
        backup = Path(backups).resolve() / (original_hash + '.cbr-metadata.json')
        if backup.is_relative_to(root):
            raise ValueError('Metadata backups must be outside comics root')
        backup.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(temp) as converted:
            metadata = {n: base64.b64encode(converted.read(n)).decode()
                        for n in converted.namelist() if n == 'ComicInfo.xml'}
        backup_data = {'file': str(source), 'target': str(target),
                       'original_archive_sha256': original_hash, 'metadata_members': metadata}
        if backup.exists():
            if json.loads(backup.read_text()) != backup_data:
                raise ValueError('Existing CBR metadata backup differs')
        else:
            with backup.open('x') as out:
                json.dump(backup_data, out, indent=2); out.flush(); os.fsync(out.fileno())
        changes = {k: {'before': old.get(k), 'after': v} for k,v in desired.items() if old.get(k) != v}
        if changes:
            apply_one({'file': str(temp), 'status': 'proposed', 'changes': changes,
                       'source_sha256': sha256(temp)}, root, backups)
        if inventory(temp) != expected or sha256(source) != original_hash:
            raise ValueError('Source changed or converted content differs')
        os.chmod(temp, mode)
        # Exclusive creation refuses collisions. Original remains until full verification.
        with target.open('xb') as out, temp.open('rb') as inp:
            published = True
            shutil.copyfileobj(inp, out, 1024 * 1024)
            out.flush(); os.fsync(out.fileno())
        os.chmod(target, mode)
        if sha256(target) != sha256(temp) or sha256(source) != original_hash:
            raise ValueError('Published conversion verification failed')
        source.unlink()
        return {'file': str(target), 'source': str(source), 'status': 'converted',
                'source_sha256': original_hash, 'after_sha256': sha256(target)}
    except Exception:
        if published and source.exists():
            target.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
