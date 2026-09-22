"""Phase 2: legacy parsing, migration, the log store and the DB-backed commands.

Fixtures are synthetic. Real archive content is never committed; provenance
against the actual logs is checked locally by scripts/verify_log_migration.py.
"""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import choi_bot as bot
from bot.storage import repository as log_types
from bot.storage.database import (backup, connect, initialize, integrity_report,
                                  journal_mode, schema_version)
from bot.storage.legacy_parser import PARSER_VERSION, coverage, parse_bytes
from bot.storage.migration import discover_sources, run_migration
from bot.storage.repository import LogRepository
from bot.storage.runtime import LogStore
from tests.fakes import FakeChannel, FakeInteraction, temp_log_store

SIMPLE = (
    '[2026-01-01 09:00:00] alice: 안녕하세요\n'
    '[2026-01-01 09:00:01] bob: ㅇㅋ\n'
)

AWKWARD = (
    '[2026-01-02 10:00:00] alice: 첫 줄\n'
    '이어지는 줄\n'
    '\t들여쓴 줄\n'
    '[2026-01-02 10:00:01] bob: \n'
    '[2026-01-02 10:00:02] alice: ```py\nprint("hi")\n```\n'
    '[2026-01-02 10:00:03] bob: 🙂 이모지\n'
    '[2026-01-02 10:00:03] bob: 🙂 이모지\n'
    '[2026-01-02 10:00:04] alice: ' + '가' * 3000 + '\n'
)


def write(directory, name, text, *, encoding='utf-8', newline='\n', bom=False):
    path = Path(directory) / 'logs' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.replace('\n', newline).encode(encoding)
    path.write_bytes((b'\xef\xbb\xbf' if bom else b'') + data)
    return path


class LegacyParserTests(unittest.TestCase):
    def test_multiline_body_is_one_message_with_newlines_kept(self):
        parsed = parse_bytes(AWKWARD.encode())
        first = parsed.messages[0]
        self.assertEqual(first.content, '첫 줄\n이어지는 줄\n\t들여쓴 줄')
        self.assertTrue(first.is_multiline)
        self.assertEqual(first.raw_actor, 'alice')
        self.assertEqual(first.line_start, 1)
        self.assertEqual(first.line_end, 3)

    def test_awkward_bodies_survive_intact(self):
        messages = parse_bytes(AWKWARD.encode()).messages
        self.assertEqual(messages[1].content, '')                      # empty body
        self.assertIn('print("hi")', messages[2].content)               # code block
        self.assertEqual(messages[3].content, '🙂 이모지')
        self.assertEqual(len(messages[5].content), 3000)                # very long
        self.assertEqual(len(messages), 6)

    def test_identical_messages_in_the_same_second_stay_separate(self):
        messages = parse_bytes(AWKWARD.encode()).messages
        twins = [m for m in messages if m.content == '🙂 이모지']
        self.assertEqual(len(twins), 2)
        self.assertNotEqual(twins[0].byte_start, twins[1].byte_start)
        self.assertEqual(twins[0].raw_hash, twins[1].raw_hash)  # identity is the offset

    def test_every_byte_is_accounted_for(self):
        for text, kwargs in ((SIMPLE, {}), (AWKWARD, {}), (SIMPLE, {'newline': '\r\n'})):
            data = text.replace('\n', kwargs.get('newline', '\n')).encode()
            parsed = parse_bytes(data)
            self.assertEqual(coverage(parsed, data), (len(data), len(data)))

    def test_crlf_and_bom_are_recorded_not_silently_normalised(self):
        crlf = parse_bytes(SIMPLE.replace('\n', '\r\n').encode())
        self.assertEqual(crlf.newline_style, 'crlf')
        self.assertEqual(crlf.messages[0].content, '안녕하세요')  # no stray \r
        bom = parse_bytes(b'\xef\xbb\xbf' + SIMPLE.encode())
        self.assertTrue(bom.has_bom)
        self.assertEqual(len(bom.messages), 2)
        self.assertEqual(bom.messages[0].byte_start, 3)

    def test_invalid_utf8_raises_instead_of_being_mangled(self):
        with self.assertRaises(UnicodeDecodeError):
            parse_bytes(b'[2026-01-01 09:00:00] a: \xff\xfe bad\n')

    def test_text_before_any_header_is_reported_as_orphan(self):
        parsed = parse_bytes(('선행 텍스트\n' + SIMPLE).encode())
        self.assertEqual([i.issue_type for i in parsed.issues], ['orphan_text'])
        self.assertEqual(len(parsed.messages), 2)

    def test_backward_timestamp_is_flagged_but_still_kept(self):
        text = ('[2026-01-01 09:00:05] alice: 인용함\n'
                '[2026-01-01 09:00:00] bob: 과거\n')
        parsed = parse_bytes(text.encode())
        self.assertEqual(len(parsed.messages), 2)  # nothing discarded
        self.assertEqual([i.issue_type for i in parsed.issues],
                         ['ambiguous_header_timestamp_regression'])


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.connection = initialize(connect(self.root / 'db.sqlite3'))
        self.addCleanup(self.connection.close)

    def migrate(self):
        return run_migration(self.connection, self.root)

    def counts(self):
        return {name: self.connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
                for name in ('messages', 'message_origins', 'log_sources', 'parse_issues')}

    def test_rerun_inserts_nothing(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        write(self.root, '2026-01-02.txt', AWKWARD)
        _, first = self.migrate()
        before = self.counts()
        _, second = self.migrate()
        self.assertEqual(second.inserted, 0)
        self.assertEqual(second.matched, first.inserted)
        self.assertEqual(self.counts(), before)  # issues do not accumulate either

    def test_appending_to_a_source_imports_only_the_new_tail(self):
        path = write(self.root, '2026-01-01.txt', SIMPLE)
        _, first = self.migrate()
        with path.open('a', encoding='utf-8') as handle:
            handle.write('[2026-01-01 09:00:02] carol: 나중에\n')
        _, second = self.migrate()
        self.assertEqual(second.inserted, 1)
        self.assertEqual(second.matched, first.inserted)
        # An append extends the same source row, so repeated catch-up runs do not
        # accumulate one snapshot row per run.
        self.assertEqual(self.counts()['log_sources'], 1)
        source = self.connection.execute('SELECT file_size FROM log_sources').fetchone()
        self.assertEqual(source['file_size'], path.stat().st_size)

    def test_rewritten_source_gets_a_new_row_and_keeps_the_old_record(self):
        path = write(self.root, '2026-01-01.txt', SIMPLE)
        self.migrate()
        original = self.connection.execute('SELECT sha256 FROM log_sources').fetchone()['sha256']
        # logs_bak/clear.py rewrote files in place; such a change is not an append.
        path.write_text('[2026-01-01 09:00:09] carol: 교체됨\n', encoding='utf-8')
        self.migrate()
        rows = [r['sha256'] for r in self.connection.execute(
            'SELECT sha256 FROM log_sources ORDER BY id')]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], original)  # the pre-rewrite snapshot is still recorded

    def test_resumes_after_an_interrupted_run(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        write(self.root, '2026-01-02.txt', AWKWARD)
        sources = discover_sources(self.root)
        run_migration(self.connection, self.root, sources=sources[:1])
        partial = self.counts()['messages']
        _, resumed = self.migrate()
        self.assertEqual(resumed.matched, partial)
        self.assertEqual(self.counts()['messages'], partial + 6)

    def test_raw_actor_is_never_rewritten_by_user_map(self):
        write(self.root, '2026-01-01.txt', '[2026-01-01 09:00:00] jhy.jng: 테스트\n')
        self.migrate()
        row = self.connection.execute('SELECT raw_actor FROM messages').fetchone()
        self.assertEqual(row['raw_actor'], 'jhy.jng')
        repository = LogRepository(self.connection, {'jhy.jng': '주효중'})
        self.assertEqual(repository.render_summary_view(
            repository.messages_for_date('2026-01-01')), ['[09:00] 주효중: 테스트'])
        # Storage still holds the original after a display mapping change.
        self.assertEqual(self.connection.execute(
            'SELECT raw_actor FROM messages').fetchone()['raw_actor'], 'jhy.jng')

    def test_secondary_source_links_instead_of_duplicating(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        write(self.root, 'alice_all.txt', SIMPLE)     # merged per-user export
        _, stats = self.migrate()
        self.assertEqual(stats.inserted, 2)           # canonical only
        self.assertEqual(stats.secondary_exact, 2)    # export attached as provenance
        self.assertEqual(self.counts()['messages'], 2)
        self.assertEqual(self.counts()['message_origins'], 4)

    def test_secondary_without_a_canonical_twin_is_flagged_not_invented(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        write(self.root, 'alice_all.txt',
              '[2026-01-01 09:00:00] alice: 다른 내용\n')
        _, stats = self.migrate()
        self.assertEqual(stats.secondary_ambiguous, 1)
        self.assertEqual(self.counts()['messages'], 2)  # no third message invented
        issue = self.connection.execute(
            "SELECT issue_type FROM parse_issues").fetchone()
        self.assertEqual(issue['issue_type'], 'secondary_without_canonical_match')

    def test_source_snapshot_metadata_is_recorded(self):
        write(self.root, '2026-01-01.txt', SIMPLE, newline='\r\n', bom=True)
        self.migrate()
        source = self.connection.execute('SELECT * FROM log_sources').fetchone()
        self.assertEqual(source['newline_style'], 'crlf')
        self.assertEqual(source['has_bom'], 1)
        self.assertEqual(source['parser_version'], PARSER_VERSION)
        self.assertEqual(len(source['sha256']), 64)
        self.assertIn('Asia/Seoul', source['timezone_assumption'])

    def test_legacy_rows_record_when_written_not_when_it_happened(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        self.migrate()
        row = self.connection.execute('SELECT * FROM messages').fetchone()
        self.assertIsNone(row['event_time'])          # unknowable for legacy TXT
        self.assertEqual(row['recorded_at'], '2026-01-01T09:00:00')
        self.assertEqual(row['local_date'], '2026-01-01')

    def test_legacy_rows_are_classified_without_over_claiming(self):
        write(self.root, '2026-01-01.txt',
              '[2026-01-01 09:00:00] alice: 사람\n'
              '[2026-01-01 09:00:01] 최씨 봇: 답변\n'
              '[2026-01-01 09:00:02] 최씨 봇: 00100, 의미 없음\n'
              '[2026-01-01 09:00:03] USER: 명령\n'
              '[2026-01-01 09:00:04] Console: [DEBUG] x\n')
        self.migrate()
        types = [r['message_type'] for r in self.connection.execute(
            'SELECT message_type FROM messages ORDER BY id')]
        self.assertEqual(types, [log_types.HUMAN, log_types.BOT,
                                 log_types.SUSPECTED_CONTROL, log_types.COMMAND_INPUT,
                                 log_types.LEGACY_UNKNOWN])

    def test_integrity_and_foreign_keys_hold_after_migration(self):
        write(self.root, '2026-01-02.txt', AWKWARD)
        self.migrate()
        report = integrity_report(self.connection)
        self.assertTrue(report['ok'], report)
        self.assertEqual(schema_version(self.connection), 1)

    def test_backup_restores_to_an_identical_database(self):
        write(self.root, '2026-01-02.txt', AWKWARD)
        self.migrate()
        destination = self.root / 'backup.sqlite3'
        backup(self.connection, destination)
        restored = connect(destination, create=False)
        self.addCleanup(restored.close)
        for table in ('messages', 'message_origins', 'log_sources'):
            self.assertEqual(
                restored.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0],
                self.connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0])
        self.assertEqual(
            [r['content'] for r in restored.execute('SELECT content FROM messages ORDER BY id')],
            [r['content'] for r in self.connection.execute('SELECT content FROM messages ORDER BY id')])
        self.assertTrue(integrity_report(restored)['ok'])

    def test_opening_an_existing_database_again_is_safe(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        self.migrate()
        reopened = initialize(connect(self.root / 'db.sqlite3'))
        self.addCleanup(reopened.close)
        self.assertEqual(schema_version(reopened), 1)
        self.assertEqual(reopened.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 2)

    def test_newer_schema_version_is_refused(self):
        self.connection.execute(
            "UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
        self.connection.commit()
        with self.assertRaisesRegex(RuntimeError, 'newer than this code'):
            initialize(connect(self.root / 'db.sqlite3'))

    def test_constraint_violation_does_not_corrupt_the_run(self):
        write(self.root, '2026-01-01.txt', SIMPLE)
        self.migrate()
        message_id = self.connection.execute('SELECT id FROM messages LIMIT 1').fetchone()['id']
        with self.assertRaises(sqlite3.IntegrityError):
            with self.connection:
                self.connection.execute(
                    """INSERT INTO message_origins (message_id, source_id, byte_start,
                           byte_end, raw_hash, match_method, confidence)
                       SELECT ?, source_id, byte_start, byte_end, raw_hash, 'x', 'low'
                       FROM message_origins LIMIT 1""", (message_id,))
        self.assertTrue(integrity_report(self.connection)['ok'])


class RepositoryQueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        write(root, '2026-01-01.txt', SIMPLE)
        write(root, '2026-01-02.txt', AWKWARD)
        self.connection = initialize(connect(root / 'db.sqlite3'))
        self.addCleanup(self.connection.close)
        run_migration(self.connection, root)
        self.repository = LogRepository(self.connection, {'alice': '앨리스'})

    def test_date_and_actor_queries(self):
        self.assertEqual(len(self.repository.messages_for_date('2026-01-01')), 2)
        self.assertEqual(len(self.repository.messages_for_date('2026-01-02')), 6)
        self.assertEqual(self.repository.count_messages('2026-01-02'), 6)
        self.assertEqual(self.repository.count_messages(), 8)
        self.assertEqual(
            len(self.repository.messages_for_date_and_actor('2026-01-02', 'bob')), 3)
        self.assertEqual(self.repository.date_range(), ('2026-01-01', '2026-01-02'))

    def test_ordering_is_stable_for_the_same_second(self):
        rows = self.repository.messages_for_date('2026-01-02')
        stamps = [r['raw_timestamp'] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        twins = [r['id'] for r in rows if r['content'] == '🙂 이모지']
        self.assertEqual(twins, sorted(twins))  # byte order breaks the tie

    def test_latest_messages_returns_logical_messages_oldest_first(self):
        rows = self.repository.latest_messages(3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['content'][:3], '가가가')  # the very long one is last
        self.assertEqual([r['raw_timestamp'] for r in rows],
                         sorted(r['raw_timestamp'] for r in rows))

    def test_render_helpers_match_the_legacy_shapes(self):
        rows = self.repository.messages_for_date('2026-01-01')
        self.assertEqual(self.repository.render_legacy_view(rows),
                         ['[2026-01-01 09:00:00] alice: 안녕하세요',
                          '[2026-01-01 09:00:01] bob: ㅇㅋ'])
        self.assertEqual(self.repository.render_summary_view(rows),
                         ['[09:00] 앨리스: 안녕하세요', '[09:00] bob: ㅇㅋ'])


class LogStoreTests(unittest.TestCase):
    def test_store_opens_lazily_and_records(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LogStore(Path(directory) / 'sub' / 'db.sqlite3')
            self.assertFalse((Path(directory) / 'sub').exists())  # nothing until used
            row_id = store.record(content='hi', raw_actor='alice',
                                  message_type=log_types.HUMAN)
            self.assertIsNotNone(row_id)
            self.assertEqual(journal_mode(store.repository().connection), 'wal')
            store.close()

    def test_unusable_database_degrades_instead_of_raising(self):
        store = LogStore('/proc/definitely/not/writable.sqlite3')
        self.assertIsNone(store.record(content='x', raw_actor='a',
                                       message_type=log_types.HUMAN))
        self.assertEqual(store.write_failures, 1)
        self.assertFalse(store.available())

    def test_replayed_discord_message_is_not_stored_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LogStore(Path(directory) / 'db.sqlite3')
            first = store.record(content='hi', raw_actor='a', message_type=log_types.HUMAN,
                                 discord_message_id='123')
            second = store.record(content='hi', raw_actor='a', message_type=log_types.HUMAN,
                                  discord_message_id='123')
            self.assertEqual(first, second)
            self.assertEqual(store.repository().count_messages(), 1)
            store.close()


class RuntimeLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = temp_log_store(self)
        patcher = patch.object(bot, 'log_store', self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        return [dict(r) for r in self.store.repository().connection.execute(
            'SELECT * FROM messages ORDER BY id')]

    def test_event_is_written_to_both_the_database_and_the_txt_mirror(self):
        with patch.object(bot, 'save__logs') as mirror:
            bot.record_event('안녕', raw_actor='alice', message_type=log_types.HUMAN)
        mirror.assert_called_once_with('alice', '안녕')   # legacy format unchanged
        row = self.rows()[0]
        self.assertEqual((row['raw_actor'], row['content']), ('alice', '안녕'))
        self.assertEqual(row['source_kind'], 'live_txt')

    def test_mirror_failure_does_not_reach_the_caller_of_a_generated_answer(self):
        with patch.object(bot, 'save__logs', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                bot.record_event('x', raw_actor='a', message_type=log_types.HUMAN)
        # record_bot_reply owns the existing swallow so a reply is never regenerated.
        with patch.object(bot, 'record_event', side_effect=OSError('disk full')):
            bot.record_bot_reply('이미 만든 답변')

    def test_database_failure_keeps_the_txt_mirror(self):
        with patch.object(bot, 'log_store', LogStore('/proc/nope/db.sqlite3')) as _:
            with patch.object(bot, 'save__logs') as mirror:
                bot.record_event('x', raw_actor='a', message_type=log_types.HUMAN)
        mirror.assert_called_once()

    async def test_discord_message_metadata_is_captured(self):
        message = SimpleNamespace(
            content='안녕', author=SimpleNamespace(name='alice', id=42),
            channel=FakeChannel(7), guild=SimpleNamespace(id=9), id=1234,
            created_at=SimpleNamespace(isoformat=lambda timespec=None: '2026-01-01T09:00:00'))
        with patch.object(bot, 'save__logs'):
            bot.record_event(message.content, raw_actor=str(message.author.name),
                             message_type=log_types.HUMAN,
                             discord_user_id=message.author.id,
                             guild_id=message.guild.id, channel_id=message.channel.id,
                             discord_message_id=message.id,
                             event_time=bot._discord_created_at(message))
        row = self.rows()[0]
        self.assertEqual(row['discord_user_id'], '42')      # stored as TEXT
        self.assertEqual(row['discord_message_id'], '1234')
        self.assertEqual(row['channel_id'], '7')
        self.assertEqual(row['event_time'], '2026-01-01T09:00:00')
        self.assertIsNotNone(row['recorded_at'])

    def test_command_input_keeps_the_legacy_label_but_stores_the_real_caller(self):
        interaction = FakeInteraction()
        interaction.user.name = 'jhy.jng'
        interaction.user.id = 77
        with patch.object(bot, 'save__logs') as mirror:
            bot.record_command_input(interaction, '질문 내용')
        mirror.assert_called_once_with('USER', '질문 내용')   # TXT compatibility
        row = self.rows()[0]
        self.assertEqual(row['raw_actor'], 'jhy.jng')         # real identity in the DB
        self.assertEqual(row['discord_user_id'], '77')
        self.assertEqual(row['message_type'], log_types.COMMAND_INPUT)

    def test_suppressed_control_signal_is_recorded_as_not_delivered(self):
        with patch.object(bot, 'save__logs'):
            bot.record_bot_reply('00100, 의미 없음', delivered=False)
            bot.record_bot_reply('진짜 답변', delivered=True)
        suppressed, sent = self.rows()
        self.assertEqual(suppressed['message_type'], log_types.SUSPECTED_CONTROL)
        self.assertEqual(suppressed['delivery_state'], 'suppressed')
        self.assertEqual(sent['message_type'], log_types.BOT)
        self.assertEqual(sent['delivery_state'], 'sent')


class CommandSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = temp_log_store(self)
        patcher = patch.object(bot, 'log_store', self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        with patch.object(bot, 'save__logs'):
            for actor, text, stamp in (
                    ('alice', '첫 줄\n이어지는 줄', '2026-03-01 09:00:00'),
                    ('bob', '', '2026-03-01 09:00:01'),
                    ('alice', '마지막', '2026-03-01 09:00:02')):
                self.store.record(content=text, raw_actor=actor,
                                  message_type=log_types.HUMAN, raw_timestamp=stamp,
                                  recorded_at=stamp.replace(' ', 'T'),
                                  local_date='2026-03-01')

    def test_log_command_reads_logical_messages_from_the_database(self):
        label, lines = bot.get_recent_log_view(3)
        self.assertEqual(label, '2026-03-01')
        self.assertEqual(lines[0], '[2026-03-01 09:00:00] alice: 첫 줄\n이어지는 줄')
        self.assertEqual(len(lines), 3)   # three messages, not four physical lines
        self.assertEqual(lines[-1], '[2026-03-01 09:00:02] alice: 마지막')

    def test_summary_source_renders_full_multiline_and_skips_empty_bodies(self):
        with patch.dict(bot.USER_MAP, {'alice': '앨리스'}):
            lines = bot.get_day_summary_lines('2026-03-01')
        self.assertEqual(lines, ['[09:00] 앨리스: 첫 줄\n이어지는 줄',
                                 '[09:00] 앨리스: 마지막'])

    def test_unknown_date_falls_back_to_the_txt_reader(self):
        self.assertIsNone(bot.get_day_summary_lines('2020-01-01'))

    def test_empty_database_falls_back_to_the_txt_reader(self):
        with patch.object(bot, 'log_store', temp_log_store(self)), \
             patch.object(bot, 'get_latest_log_lines', return_value=('x.txt', ['line'])):
            self.assertEqual(bot.get_recent_log_view(5), ('x.txt', ['line']))


if __name__ == '__main__':
    unittest.main()
