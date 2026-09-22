#!/usr/bin/env python3
"""Import legacy TXT logs into the SQLite store. Safe to re-run.

Point it at a frozen snapshot, never at a directory the bot is writing to, when
you need reproducible numbers. Re-running over the same snapshot inserts nothing.

  python3 scripts/migrate_logs.py --root <snapshot> --db /tmp/candidate.sqlite3
  python3 scripts/migrate_logs.py --root . --db logs/db/choi_bot.sqlite3
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.storage.database import connect, initialize, integrity_report, journal_mode  # noqa: E402
from bot.storage.migration import run_migration  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(ROOT), help='directory holding logs/ and logs_bak/')
    parser.add_argument('--db', default=None, help='database path (default: CHOI_DB_PATH or logs/db)')
    parser.add_argument('--mode', default='import')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()

    connection = initialize(connect(args.db))
    started = time.time()
    run_id, stats = run_migration(connection, args.root, mode=args.mode)
    elapsed = time.time() - started
    report = integrity_report(connection)
    counts = {name: connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
              for name in ('messages', 'message_origins', 'log_sources', 'parse_issues')}
    connection.close()

    summary = {'run_id': run_id, 'elapsed_seconds': round(elapsed, 2),
               'journal_mode': journal_mode(initialize(connect(args.db))),
               'stats': stats.as_dict(), 'table_counts': counts,
               'integrity_ok': report['ok']}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return
    print(f"run {run_id} finished in {elapsed:.1f}s  (integrity_ok={report['ok']})")
    print(f"  sources {stats.sources} (unchanged {stats.skipped_sources}), "
          f"inserted {stats.inserted:,}, matched {stats.matched:,}")
    print(f"  multiline {stats.multiline:,}, empty body {stats.empty_content:,}, "
          f"issues {stats.issues}")
    print(f"  secondary exact {stats.secondary_exact:,}, "
          f"ambiguous {stats.secondary_ambiguous:,}")
    print(f"  bytes {stats.bytes_covered:,}/{stats.bytes_read:,} covered")
    print('  tables ' + ', '.join(f'{k}={v:,}' for k, v in counts.items()))
    if stats.failures:
        print('  FAILURES: ' + json.dumps(stats.failures, ensure_ascii=False))


if __name__ == '__main__':
    main()
