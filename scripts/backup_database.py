#!/usr/bin/env python3
"""Back up the log database with the SQLite backup API and verify the copy.

A plain file copy of an open SQLite database can capture a torn page or miss the
WAL tail, so this uses Connection.backup(), then reopens the copy and compares
counts, date range and a sample of rows against the source.

  python3 scripts/backup_database.py --db logs/db/choi_bot.sqlite3 --out backups/
"""
import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.storage.database import backup, connect, integrity_report  # noqa: E402

CHECKS = (
    ('messages', 'SELECT COUNT(*) FROM messages'),
    ('message_origins', 'SELECT COUNT(*) FROM message_origins'),
    ('log_sources', 'SELECT COUNT(*) FROM log_sources'),
    ('parse_issues', 'SELECT COUNT(*) FROM parse_issues'),
    ('date_range', 'SELECT MIN(local_date) || ".." || MAX(local_date) FROM messages'),
    ('source_hashes', 'SELECT COUNT(DISTINCT sha256) FROM log_sources'),
    ('sample_ids', 'SELECT group_concat(id) FROM (SELECT id FROM messages ORDER BY id LIMIT 5)'),
    ('sample_tail', 'SELECT group_concat(content) FROM '
                    '(SELECT content FROM messages ORDER BY id DESC LIMIT 3)'),
)


def snapshot(connection):
    return {name: connection.execute(sql).fetchone()[0] for name, sql in CHECKS}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=None)
    parser.add_argument('--out', required=True, help='directory to write the backup into')
    args = parser.parse_args()

    source = connect(args.db, create=False)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    destination = Path(args.out) / f'choi_bot-{stamp}.sqlite3'
    before = snapshot(source)
    backup(source, destination)
    source.close()

    restored = connect(destination, create=False)
    after = snapshot(restored)
    report = integrity_report(restored)
    restored.close()

    print(f'backup written: {destination} ({destination.stat().st_size:,} bytes)')
    ok = report['ok']
    for name, _ in CHECKS:
        same = before[name] == after[name]
        ok = ok and same
        shown = before[name] if name not in ('sample_tail',) else '<content withheld>'
        print(f'  {name:16} {"match" if same else "DIFFERENT"}   {shown}')
    print(f'  integrity_check  {"ok" if report["ok"] else report["integrity_check"]}')
    print('\nRESULT:', 'RESTORE VERIFIED' if ok else 'VERIFICATION FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
