"""Connection handling for the log store: pragmas, versioning, safe backup."""
import os
from pathlib import Path
import sqlite3

from .schema import DDL, SCHEMA_VERSION

DEFAULT_DB_PATH = 'logs/db/choi_bot.sqlite3'
BUSY_TIMEOUT_MS = 5000


def database_path(explicit=None):
    """Resolve the DB path: explicit argument, then CHOI_DB_PATH, then default.

    The default lives under logs/, which the container already bind-mounts, so
    the file persists on the host without touching the Docker volume layout.
    """
    return Path(explicit or os.environ.get('CHOI_DB_PATH') or DEFAULT_DB_PATH)


def connect(path=None, *, create=True):
    target = database_path(path)
    if create:
        target.parent.mkdir(parents=True, exist_ok=True)
    elif not target.exists():
        raise FileNotFoundError(f'database not found: {target}')
    connection = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    connection.execute(f'PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}')
    # WAL lets the bot append while a migration or query reads. It is a no-op on
    # filesystems that refuse it, so the result is read back rather than assumed.
    connection.execute('PRAGMA journal_mode = WAL')
    connection.execute('PRAGMA synchronous = NORMAL')
    return connection


def journal_mode(connection):
    """Report the mode actually in force; WAL can be refused by the filesystem."""
    return connection.execute('PRAGMA journal_mode').fetchone()[0]


def initialize(connection):
    """Create the schema if absent and record its version. Safe to re-run."""
    with connection:
        connection.executescript(DDL)
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),))
        elif int(row['value']) > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{row['value']} is newer than this code (v{SCHEMA_VERSION})")
    return connection


def schema_version(connection):
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
    return int(row['value']) if row else None


def backup(connection, destination):
    """Copy a live database with the SQLite backup API, never a file copy.

    Copying the file while a writer holds it can capture a torn page or lose the
    WAL tail; the backup API takes a consistent snapshot instead.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(destination))
    try:
        with target:
            connection.backup(target)
    finally:
        target.close()
    return destination


def integrity_report(connection):
    integrity = [dict(r) for r in connection.execute('PRAGMA integrity_check')]
    foreign_keys = [dict(r) for r in connection.execute('PRAGMA foreign_key_check')]
    return {
        'integrity_check': [list(r.values())[0] for r in integrity],
        'foreign_key_violations': foreign_keys,
        'ok': integrity == [{'integrity_check': 'ok'}] and not foreign_keys,
    }
