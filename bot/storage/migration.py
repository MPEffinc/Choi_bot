"""Import legacy TXT snapshots into the log store, repeatably and losslessly.

Occurrence identity is (source_id, byte_start, raw_hash). Re-importing the same
snapshot inserts nothing; importing a file that grew only inserts the new tail,
because the unchanged prefix keeps the same offsets and hashes.

A source file whose bytes changed gets a new log_sources row (the UNIQUE key is
(sha256, relative_path)), so an edited archive never silently overwrites the
record of what the previous version said.
"""
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from pathlib import Path
import re
import uuid

from .legacy_parser import PARSER_VERSION, parse_bytes, coverage


def _digest(hex_hash):
    """Store sha256 as 32 raw bytes; hex text would double the index size."""
    return bytes.fromhex(hex_hash)
from .repository import LEGACY_TIMEZONE_ASSUMPTION, classify_legacy

DAILY_NAME = re.compile(r'^(\d{4}-\d{2}-\d{2})\.txt$')

# Only dated files under logs/ are canonical. The per-user *_all.txt exports and
# logs_bak/ are secondary: logs_bak was edited in place by logs_bak/clear.py,
# which stripped DEBUG lines, so it is not a faithful copy of anything.
CANONICAL, MERGED_EXPORT, BACKUP_COPY = 'legacy_daily', 'legacy_merged', 'legacy_backup'


@dataclass
class ImportStats:
    sources: int = 0
    inserted: int = 0
    matched: int = 0
    skipped_sources: int = 0
    issues: int = 0
    multiline: int = 0
    empty_content: int = 0
    secondary_exact: int = 0
    secondary_ambiguous: int = 0
    bytes_read: int = 0
    bytes_covered: int = 0
    failures: list = field(default_factory=list)

    def as_dict(self):
        data = dict(self.__dict__)
        data['failures'] = list(self.failures)
        return data


def classify_source(path: Path, root: Path):
    relative = path.relative_to(root).as_posix()
    if relative.startswith('logs_bak/'):
        return BACKUP_COPY
    if DAILY_NAME.match(path.name):
        return CANONICAL
    return MERGED_EXPORT


def discover_sources(root: Path):
    """Canonical dated files first, then secondary sources, each in stable order."""
    root = Path(root)
    found = []
    for directory in ('logs', 'logs_bak'):
        base = root / directory
        if not base.is_dir():
            continue
        for path in sorted(base.iterdir()):
            if path.is_file() and path.suffix == '.txt':
                found.append(path)
    return sorted(found, key=lambda p: (classify_source(p, root) != CANONICAL, p.as_posix()))


def _sha256(data: bytes):
    return hashlib.sha256(data).hexdigest()


def _register_source(connection, path: Path, root: Path, data: bytes, parsed):
    """Return (source_id, is_new) for this file's current bytes.

    save__logs only ever appends, so a daily file that grew is the same snapshot
    extended: its row is updated in place, which keeps one source per file
    instead of one per catch-up run. A file whose existing prefix changed is a
    rewrite, not an append, so it gets a new row and the old record is kept.
    """
    relative = path.relative_to(root).as_posix()
    digest = _sha256(data)
    row = connection.execute(
        'SELECT id FROM log_sources WHERE sha256 = ? AND relative_path = ?',
        (digest, relative)).fetchone()
    if row:
        return row['id'], False
    stat = path.stat()
    for previous in connection.execute(
            'SELECT id, file_size, sha256 FROM log_sources WHERE relative_path = ?'
            ' ORDER BY id DESC', (relative,)):
        size = previous['file_size']
        if size <= len(data) and _sha256(data[:size]) == previous['sha256']:
            connection.execute(
                """UPDATE log_sources SET file_size = ?, sha256 = ?, mtime = ?,
                       newline_style = ?, has_bom = ?, parser_version = ?, imported_at = ?
                   WHERE id = ?""",
                (len(data), digest,
                 datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'),
                 parsed.newline_style, int(parsed.has_bom), PARSER_VERSION,
                 datetime.now().isoformat(timespec='seconds'), previous['id']))
            return previous['id'], False
    cursor = connection.execute(
        """INSERT INTO log_sources (source_kind, relative_path, original_path, filename,
               file_size, mtime, sha256, encoding, newline_style, has_bom,
               timezone_assumption, parser_version, imported_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (classify_source(path, root), relative, None, path.name, stat.st_size,
         datetime.fromtimestamp(stat.st_mtime).isoformat(timespec='seconds'), digest,
         parsed.encoding, parsed.newline_style, int(parsed.has_bom),
         LEGACY_TIMEZONE_ASSUMPTION, PARSER_VERSION,
         datetime.now().isoformat(timespec='seconds')))
    return cursor.lastrowid, True


def _local_date(raw_timestamp, path: Path):
    if raw_timestamp:
        return raw_timestamp[:10]
    match = DAILY_NAME.match(path.name)
    return match.group(1) if match else None


def import_source(connection, path: Path, root: Path, run_id, stats: ImportStats):
    """Import one file. Existing occurrences are matched, not duplicated."""
    data = path.read_bytes()
    try:
        parsed = parse_bytes(data)
    except UnicodeDecodeError as error:
        stats.failures.append({'path': path.name, 'error': 'invalid_utf8', 'detail': str(error)})
        connection.execute(
            """INSERT OR IGNORE INTO parse_issues (import_run_id, source_id, byte_start,
                   issue_type, raw_excerpt_hash, description)
               VALUES (?, NULL, 0, 'invalid_utf8', ?, ?)""",
            (run_id, _digest(_sha256(data)), f'{path.name}: strict UTF-8 decode failed'))
        stats.issues += 1
        return

    source_id, is_new = _register_source(connection, path, root, data, parsed)
    stats.sources += 1
    covered, total = coverage(parsed, data)
    stats.bytes_read += total
    stats.bytes_covered += covered
    kind = classify_source(path, root)
    secondary = kind != CANONICAL

    for message in parsed.messages:
        existing = connection.execute(
            """SELECT message_id FROM message_origins
               WHERE source_id = ? AND byte_start = ? AND raw_hash = ?""",
            (source_id, message.byte_start, _digest(message.raw_hash))).fetchone()
        if existing:
            stats.matched += 1
            continue
        if secondary:
            # A secondary file must attach to the canonical record it duplicates.
            # Only an exact (timestamp, actor, content) match is safe to link;
            # anything else is left for review rather than invented as new history.
            twin = connection.execute(
                """SELECT id FROM messages
                   WHERE raw_timestamp = ? AND raw_actor = ? AND content = ?
                     AND source_kind = ? ORDER BY id LIMIT 1""",
                (message.raw_timestamp, message.raw_actor, message.content, CANONICAL)
            ).fetchone()
            if twin is None:
                stats.secondary_ambiguous += 1
                connection.execute(
                    """INSERT OR IGNORE INTO parse_issues (import_run_id, source_id, byte_start,
                           byte_end, line_start, line_end, issue_type, raw_excerpt_hash,
                           description)
                       VALUES (?,?,?,?,?,?, 'secondary_without_canonical_match', ?, ?)""",
                    (run_id, source_id, message.byte_start, message.byte_end,
                     message.line_start, message.line_end, _digest(message.raw_hash),
                     'secondary source record has no exact canonical counterpart; '
                     'kept as provenance only, no new canonical message created'))
                stats.issues += 1
                continue
            _link_origin(connection, twin['id'], source_id, message,
                         'exact_secondary_match', 'high')
            stats.secondary_exact += 1
            continue

        message_id = connection.execute(
            """INSERT INTO messages (event_uuid, source_kind, message_type, raw_actor,
                   content, raw_timestamp, event_time, recorded_at, local_date,
                   created_at)
               VALUES (?,?,?,?,?,?,NULL,?,?,?)""",
            (str(uuid.uuid4()), kind, classify_legacy(message.raw_actor, message.content),
             message.raw_actor, message.content, message.raw_timestamp,
             # The legacy header is when the bot wrote the line, not when the event
             # happened, so it fills recorded_at and event_time stays NULL.
             message.raw_timestamp.replace(' ', 'T') if message.raw_timestamp else None,
             _local_date(message.raw_timestamp, path),
             datetime.now().isoformat(timespec='seconds'))).lastrowid
        _link_origin(connection, message_id, source_id, message, 'byte_range', 'high')
        stats.inserted += 1
        if message.is_multiline:
            stats.multiline += 1
        if message.content == '':
            stats.empty_content += 1

    for issue in parsed.issues:
        connection.execute(
            """INSERT OR IGNORE INTO parse_issues (import_run_id, source_id, byte_start,
                   byte_end, line_start, line_end, issue_type, raw_excerpt_hash, description)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (run_id, source_id, issue.byte_start, issue.byte_end, issue.line_start,
             issue.line_end, issue.issue_type, _digest(issue.raw_excerpt_hash), issue.description))
        stats.issues += 1
    if not is_new:
        stats.skipped_sources += 1


def _link_origin(connection, message_id, source_id, message, method, confidence):
    connection.execute(
        """INSERT OR IGNORE INTO message_origins (message_id, source_id, byte_start,
               byte_end, line_start, line_end, raw_hash, match_method, confidence)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (message_id, source_id, message.byte_start, message.byte_end,
         message.line_start, message.line_end, _digest(message.raw_hash), method, confidence))


def run_migration(connection, root, *, mode='import', sources=None):
    """Import every discovered source inside one run record."""
    root = Path(root)
    stats = ImportStats()
    started = datetime.now().isoformat(timespec='seconds')
    run_id = connection.execute(
        """INSERT INTO import_runs (started_at, parser_version, mode, status)
           VALUES (?,?,?, 'running')""", (started, PARSER_VERSION, mode)).lastrowid
    paths = list(sources) if sources is not None else discover_sources(root)
    try:
        for path in paths:
            # One transaction per file: an interrupted run keeps whole files and
            # resumes cleanly, because finished files match on the next pass.
            with connection:
                import_source(connection, Path(path), root, run_id, stats)
    finally:
        with connection:
            connection.execute(
                """UPDATE import_runs SET finished_at = ?, source_count = ?,
                       inserted_count = ?, matched_count = ?, skipped_count = ?,
                       issue_count = ?, status = ? WHERE id = ?""",
                (datetime.now().isoformat(timespec='seconds'), stats.sources,
                 stats.inserted, stats.matched, stats.skipped_sources, stats.issues,
                 'failed' if stats.failures else 'completed', run_id))
    return run_id, stats
