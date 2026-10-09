"""Apply a saved preview locally; continue past held files and retain a durable journal."""
import argparse
import fcntl
import json
from pathlib import Path
import shutil
import time
from companion import read_tags
from writer import apply_one, sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--root', required=True)
    parser.add_argument('--backups', required=True)
    parser.add_argument('--journal', required=True)
    args = parser.parse_args()
    rows = json.loads(Path(args.plan).read_text())
    backups = Path(args.backups)
    backups.mkdir(parents=True, exist_ok=True)
    pending_bytes = max((Path(r['file']).stat().st_size for r in rows if r['status'] == 'proposed'), default=0)
    if shutil.disk_usage(backups).free < pending_bytes + 5 * 1024**3:
        raise RuntimeError('Insufficient backup capacity plus working-space reserve')
    counts = {}
    with open(args.journal, 'a') as journal:
        fcntl.flock(journal, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for n, row in enumerate(rows, 1):
            print(f'Checking {n}/{len(rows)}: {Path(row["file"]).name}', flush=True)
            try:
                if row['status'] != 'proposed':
                    result = {'file': row['file'], 'status': row['status'], 'reason': row.get('reason')}
                else:
                    current = read_tags(row['file'])
                    if all(current.get(k) == v['after'] for k, v in row['changes'].items()):
                        result = {'file': row['file'], 'status': 'already_applied'}
                    else:
                        # apply_one rechecks every before-value against the saved preview.
                        row['source_sha256'] = sha256(row['file'])
                        result = apply_one(row, args.root, backups)
            except Exception as exc:
                result = {'file': row['file'], 'status': 'failed', 'reason': str(exc)}
            result['time'] = time.time()
            journal.write(json.dumps(result) + '\n')
            journal.flush()
            import os
            os.fsync(journal.fileno())
            counts[result['status']] = counts.get(result['status'], 0) + 1
            print(json.dumps({'progress': n, 'total': len(rows), 'counts': counts, 'last': result['status']}), flush=True)
    print('FINISHED ' + json.dumps(counts), flush=True)


if __name__ == '__main__':
    main()
