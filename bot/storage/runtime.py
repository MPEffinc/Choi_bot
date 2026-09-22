"""Process-wide handle to the log store.

Opening is lazy: importing choi_bot must not touch the filesystem, so the
connection is created on first use, not at import.

Writes never raise into the bot. The TXT mirror is written first and stays the
recovery path, so a database failure degrades to "TXT has it, the database does
not" — which is logged loudly enough to be reconciled later, and never causes an
already-generated LLM answer to be thrown away or regenerated.
"""
import logging
import threading

from .database import connect, initialize
from .repository import LogRepository

logger = logging.getLogger(__name__)


class LogStore:
    def __init__(self, path=None, user_map=None):
        self.path = path
        self.user_map = user_map if user_map is not None else {}
        self._repository = None
        self._lock = threading.Lock()
        self._failed = False
        self.write_failures = 0
        self.last_error = None

    def repository(self):
        """Return the repository, opening the database on first use.

        Returns None if the database cannot be opened; callers fall back to TXT.
        """
        if self._repository is not None:
            return self._repository
        with self._lock:
            if self._repository is None and not self._failed:
                try:
                    connection = initialize(connect(self.path))
                    self._repository = LogRepository(connection, self.user_map)
                except Exception as error:
                    # One failed open must not retry on every message.
                    self._failed = True
                    self.last_error = error
                    logger.error('Log database unavailable (%s); TXT mirror only: %s',
                                 type(error).__name__, error)
        return self._repository

    def available(self):
        return self.repository() is not None

    def record(self, **fields):
        """Store one event. Returns the row id, or None when it was not stored."""
        repository = self.repository()
        if repository is None:
            self.write_failures += 1
            return None
        try:
            with repository.connection:
                return repository.record_message(**fields)
        except Exception as error:
            self.write_failures += 1
            self.last_error = error
            # TXT already holds this event, so the two stores now disagree.
            logger.error('Log DB write failed (%s); TXT mirror holds this event, '
                         'database does not: %s', type(error).__name__, error)
            return None

    def refresh_user_map(self, user_map):
        self.user_map = user_map
        if self._repository is not None:
            self._repository.user_map = dict(user_map)

    def close(self):
        with self._lock:
            if self._repository is not None:
                try:
                    self._repository.connection.close()
                finally:
                    self._repository = None
