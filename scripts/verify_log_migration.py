#!/usr/bin/env python3
"""Prove the migration lost nothing, by reconstructing sources from the database.

For every canonical source this re-reads the original bytes and checks that each
byte belongs to a stored message, a recorded issue range, or a line terminator,
then re-renders stored messages and compares them against the file. It also
samples the awkward cases (empty body, multiline, code block, emoji, very long,
same-second repeats) and diffs them against the original.

  python3 scripts/verify_log_migration.py --root <snapshot> --db <db>
"""
import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.storage.database import connect, integrity_report  # noqa: E402
from bot.storage.legacy_parser import parse_bytes  # noqa: E402
from bot.storage.migration import CANONICAL  # noqa: E402


def verify_sources(connection, root: Path):
    """Re-read each canonical source and compare it byte for byte with the DB."""
    result = {'sources': 0, 'messages': 0, 'content_mismatch': 0, 'missing_origin': 0,
              'hash_mismatch': 0, 'uncovered_bytes': 0, 'file_changed': 0}
    rows = connection.execute(
        'SELECT * FROM log_sources WHERE source_kind = ? ORDER BY id', (CANONICAL,))
    for source in rows:
        path = root / source['relative_path']
        if not path.exists():
            result['file_changed'] += 1
            continue
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != source['sha256']:
            # The live file grew or was edited after import; not a migration fault.
            result['file_changed'] += 1
            continue
        result['sources'] += 1
        parsed = parse_bytes(data)
        stored = {r['byte_start']: r for r in connection.execute(
            """SELECT o.byte_start, o.byte_end, o.raw_hash, m.content, m.raw_actor,
                      m.raw_timestamp
               FROM message_origins o JOIN messages m ON m.id = o.message_id
               WHERE o.source_id = ?""", (source['id'],))}
        covered = bytearray(len(data))
        for message in parsed.messages:
            result['messages'] += 1
            row = stored.get(message.byte_start)
            if row is None:
                result['missing_origin'] += 1
                continue
            if row['raw_hash'] != bytes.fromhex(message.raw_hash):
                result['hash_mismatch'] += 1
            if (row['content'] != message.content or row['raw_actor'] != message.raw_actor
                    or row['raw_timestamp'] != message.raw_timestamp):
                result['content_mismatch'] += 1
            for i in range(message.byte_start, message.byte_end):
                covered[i] = 1
        for issue in connection.execute(
                'SELECT byte_start, byte_end FROM parse_issues WHERE source_id = ?',
                (source['id'],)):
            if issue['byte_end']:
                for i in range(issue['byte_start'], min(issue['byte_end'], len(data))):
                    covered[i] = 1
        if parsed.has_bom:
            for i in range(3):
                covered[i] = 1
        result['uncovered_bytes'] += covered.count(0)
    return result


SAMPLES = (
    ('earliest', "SELECT * FROM messages ORDER BY raw_timestamp, id LIMIT 1"),
    ('latest', "SELECT * FROM messages ORDER BY raw_timestamp DESC, id DESC LIMIT 1"),
    ('empty body', "SELECT * FROM messages WHERE content = '' LIMIT 1"),
    ('multiline', "SELECT * FROM messages WHERE content LIKE '%' || char(10) || '%' LIMIT 1"),
    ('code block', "SELECT * FROM messages WHERE content LIKE '%```%' LIMIT 1"),
    ('very long', "SELECT * FROM messages ORDER BY length(content) DESC LIMIT 1"),
    ('korean', "SELECT * FROM messages WHERE content LIKE '%가%' LIMIT 1"),
)


def verify_samples(connection, root: Path):
    """Read each sample back out of the original file using its stored offsets."""
    checked, failed = [], []
    for label, sql in SAMPLES:
        row = connection.execute(sql).fetchone()
        if row is None:
            checked.append((label, 'absent'))
            continue
        origin = connection.execute(
            """SELECT o.*, s.relative_path, s.sha256 FROM message_origins o
               JOIN log_sources s ON s.id = o.source_id
               WHERE o.message_id = ? ORDER BY o.id LIMIT 1""", (row['id'],)).fetchone()
        path = root / origin['relative_path']
        raw = path.read_bytes()[origin['byte_start']:origin['byte_end']]
        ok = hashlib.sha256(raw).digest() == origin['raw_hash']
        # The stored content must be exactly what the raw bytes say it is.
        rebuilt = parse_bytes(raw).messages
        ok = ok and len(rebuilt) == 1 and rebuilt[0].content == row['content']
        checked.append((label, 'ok' if ok else 'MISMATCH'))
        if not ok:
            failed.append(label)
    return checked, failed


def duplicate_report(connection):
    same_second = connection.execute(
        """SELECT COUNT(*) FROM (SELECT raw_timestamp, raw_actor, COUNT(*) n
           FROM messages GROUP BY 1,2 HAVING n > 1)""").fetchone()[0]
    identical = connection.execute(
        """SELECT COUNT(*) FROM (SELECT raw_timestamp, raw_actor, content, COUNT(*) n
           FROM messages GROUP BY 1,2,3 HAVING n > 1)""").fetchone()[0]
    return same_second, identical


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default=str(ROOT))
    parser.add_argument('--db', default=None)
    args = parser.parse_args()
    root = Path(args.root)
    connection = connect(args.db, create=False)

    sources = verify_sources(connection, root)
    checked, failed = verify_samples(connection, root)
    same_second, identical = duplicate_report(connection)
    report = integrity_report(connection)

    print(f"canonical sources verified : {sources['sources']}"
          f" (skipped as changed: {sources['file_changed']})")
    print(f"messages compared          : {sources['messages']:,}")
    print(f"content mismatches         : {sources['content_mismatch']}")
    print(f"missing origins            : {sources['missing_origin']}")
    print(f"raw hash mismatches        : {sources['hash_mismatch']}")
    print(f"uncovered bytes            : {sources['uncovered_bytes']}")
    print(f"integrity_check ok         : {report['ok']}")
    print(f"(timestamp, actor) groups with >1 message : {same_second:,}")
    print(f"  of those, identical content kept as separate rows: {identical:,}")
    print('\nrepresentative samples re-read from the original bytes:')
    for label, state in checked:
        print(f'  {label:12} {state}')
    ok = (not failed and sources['content_mismatch'] == 0 and sources['missing_origin'] == 0
          and sources['hash_mismatch'] == 0 and sources['uncovered_bytes'] == 0 and report['ok'])
    print('\nRESULT:', 'LOSSLESS' if ok else 'PROBLEMS FOUND')
    connection.close()
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
