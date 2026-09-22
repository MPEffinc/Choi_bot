#!/usr/bin/env python3
"""Survey legacy TXT logs without writing anything.

Reports what the archive actually contains so the schema and migration are
based on the files, not on assumptions. Prints aggregates only: no message
bodies, so the output is safe to paste into a report.

  python3 scripts/inventory_logs.py [root]
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot.storage.legacy_parser import parse_bytes, coverage      # noqa: E402
from bot.storage.migration import classify_source, discover_sources  # noqa: E402


def inventory(root: Path):
    totals = {'files': 0, 'bytes': 0, 'messages': 0, 'multiline': 0, 'empty': 0,
              'issues': 0, 'covered': 0, 'undecodable': 0}
    by_kind, actors, issue_types, dates = {}, {}, {}, []
    for path in discover_sources(root):
        data = path.read_bytes()
        kind = classify_source(path, root)
        entry = by_kind.setdefault(kind, {'files': 0, 'bytes': 0, 'messages': 0})
        entry['files'] += 1
        entry['bytes'] += len(data)
        totals['files'] += 1
        totals['bytes'] += len(data)
        try:
            parsed = parse_bytes(data)
        except UnicodeDecodeError:
            totals['undecodable'] += 1
            issue_types['invalid_utf8'] = issue_types.get('invalid_utf8', 0) + 1
            continue
        covered, total = coverage(parsed, data)
        totals['covered'] += covered
        entry['messages'] += len(parsed.messages)
        totals['messages'] += len(parsed.messages)
        for message in parsed.messages:
            actors[message.raw_actor] = actors.get(message.raw_actor, 0) + 1
            totals['multiline'] += message.is_multiline
            totals['empty'] += message.content == ''
            if message.raw_timestamp:
                dates.append(message.raw_timestamp)
        for issue in parsed.issues:
            issue_types[issue.issue_type] = issue_types.get(issue.issue_type, 0) + 1
            totals['issues'] += 1
    return {'totals': totals, 'by_kind': by_kind, 'issue_types': issue_types,
            'actor_count': len(actors),
            'top_actors': sorted(actors.items(), key=lambda kv: -kv[1])[:15],
            'earliest': min(dates) if dates else None,
            'latest': max(dates) if dates else None}


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT
    report = inventory(root)
    totals = report['totals']
    print(f"root: {root}")
    print(f"files {totals['files']}, bytes {totals['bytes']:,}, "
          f"messages {totals['messages']:,}")
    print(f"byte coverage {totals['covered']:,}/{totals['bytes']:,} "
          f"({100 * totals['covered'] / max(1, totals['bytes']):.4f}%)")
    print(f"multiline {totals['multiline']}, empty body {totals['empty']}, "
          f"issues {totals['issues']}, undecodable files {totals['undecodable']}")
    print(f"actors {report['actor_count']}, range {report['earliest']} .. {report['latest']}")
    print('\nby source kind:')
    for kind, entry in sorted(report['by_kind'].items()):
        print(f"  {kind:16} files {entry['files']:4}  bytes {entry['bytes']:>12,}  "
              f"messages {entry['messages']:>8,}")
    if report['issue_types']:
        print('\nissue types:')
        for name, count in sorted(report['issue_types'].items(), key=lambda kv: -kv[1]):
            print(f'  {name:42} {count}')
    print('\ntop actors (record counts):')
    for name, count in report['top_actors']:
        print(f'  {name:36} {count:>8,}')
    if '--json' in sys.argv:
        print('\n' + json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
