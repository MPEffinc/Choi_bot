"""Query and write API over the log store. Commands never write SQL themselves."""
from datetime import datetime
import uuid

LEGACY_TIMEZONE_ASSUMPTION = 'Asia/Seoul (container TZ at migration time; not proven for the whole archive)'

# Message types. Legacy rows cannot always be classified, so they say so.
HUMAN, BOT, COMMAND_INPUT, COMMAND_OUTPUT = (
    'human_message', 'bot_message', 'command_input', 'command_output')
LEGACY_UNKNOWN, SUSPECTED_CONTROL = 'legacy_unknown', 'suspected_control'

# Actors the legacy writer used for its own records.
BOT_ACTOR = '최씨 봇'
COMMAND_ACTOR = 'USER'
CONSOLE_ACTOR = 'Console'


def classify_legacy(raw_actor, content):
    """Best-effort type for a legacy row, admitting what cannot be known.

    The TXT format records only an actor string, so a bot line that was actually
    a suppressed 00100 control signal looks like any other. Those are marked
    suspected_control rather than asserted either way.
    """
    if raw_actor == BOT_ACTOR:
        return SUSPECTED_CONTROL if '00100' in content else BOT
    if raw_actor == COMMAND_ACTOR:
        return COMMAND_INPUT
    if raw_actor == CONSOLE_ACTOR:
        return LEGACY_UNKNOWN
    return HUMAN


def _now():
    return datetime.now().isoformat(timespec='seconds')


class LogRepository:
    def __init__(self, connection, user_map=None):
        self.connection = connection
        # Display names are resolved at render time so a later USER_MAP change
        # never rewrites stored history.
        self.user_map = dict(user_map or {})

    # ---------------------------------------------------------------- writing

    def record_message(self, *, content, raw_actor, message_type, recorded_at=None,
                       event_time=None, local_date=None, source_kind='live_txt',
                       discord_user_id=None, guild_id=None, channel_id=None,
                       thread_id=None, discord_message_id=None, command_name=None,
                       delivery_state=None, control_kind=None, raw_timestamp=None,
                       display_actor=None, event_uuid=None):
        """Insert one live event. Returns the row id, or the existing id if the
        same discord_message_id was already stored (replayed event)."""
        recorded_at = recorded_at or _now()
        local_date = local_date or (event_time or recorded_at)[:10]
        if discord_message_id is not None:
            existing = self.connection.execute(
                'SELECT id FROM messages WHERE discord_message_id = ?',
                (str(discord_message_id),)).fetchone()
            if existing:
                return existing['id']
        cursor = self.connection.execute(
            """INSERT INTO messages (event_uuid, source_kind, message_type, raw_actor,
                   display_actor, discord_user_id, guild_id, channel_id, thread_id,
                   discord_message_id, command_name, content, raw_timestamp, event_time,
                   recorded_at, local_date, delivery_state, control_kind, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_uuid or str(uuid.uuid4()), source_kind, message_type, raw_actor,
             display_actor, _text(discord_user_id), _text(guild_id), _text(channel_id),
             _text(thread_id), _text(discord_message_id), command_name, content,
             raw_timestamp, event_time, recorded_at, local_date, delivery_state,
             control_kind, _now()))
        return cursor.lastrowid

    # ---------------------------------------------------------------- reading

    def latest_messages(self, count):
        """Most recent `count` logical messages, oldest first.

        Ordered by recorded_at then id: legacy rows share a one-second
        resolution, so the insertion order breaks ties deterministically.
        """
        if count <= 0:
            return []
        rows = self.connection.execute(
            """SELECT * FROM messages ORDER BY recorded_at DESC, id DESC LIMIT ?""",
            (count,)).fetchall()
        return list(reversed([dict(r) for r in rows]))

    def messages_for_date(self, local_date):
        rows = self.connection.execute(
            """SELECT * FROM messages WHERE local_date = ?
               ORDER BY COALESCE(event_time, recorded_at), id""", (local_date,))
        return [dict(r) for r in rows]

    def messages_for_date_and_actor(self, local_date, raw_actor):
        rows = self.connection.execute(
            """SELECT * FROM messages WHERE local_date = ? AND raw_actor = ?
               ORDER BY COALESCE(event_time, recorded_at), id""", (local_date, raw_actor))
        return [dict(r) for r in rows]

    def count_messages(self, local_date=None):
        if local_date is None:
            return self.connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        return self.connection.execute(
            'SELECT COUNT(*) FROM messages WHERE local_date = ?', (local_date,)).fetchone()[0]

    def latest_date(self):
        row = self.connection.execute(
            'SELECT MAX(local_date) AS d FROM messages').fetchone()
        return row['d'] if row else None

    def date_range(self):
        row = self.connection.execute(
            'SELECT MIN(local_date) AS lo, MAX(local_date) AS hi FROM messages').fetchone()
        return (row['lo'], row['hi']) if row else (None, None)

    # --------------------------------------------------------------- rendering

    def display_name(self, raw_actor):
        """Resolve a stored raw actor for display only; storage keeps the original."""
        return self.user_map.get(raw_actor, raw_actor)

    def render_legacy_view(self, rows):
        """Render as the old `/로그` output: `[YYYY-MM-DD HH:MM:SS] actor: content`.

        Uses raw_actor, matching what the TXT file showed.
        """
        return [f"[{r['raw_timestamp'] or (r['recorded_at'] or '').replace('T', ' ')}] "
                f"{r['raw_actor']}: {r['content']}" for r in rows]

    def render_summary_view(self, rows):
        """Render as the old summary input: `[HH:MM] display_name: content`."""
        lines = []
        for row in rows:
            stamp = row['raw_timestamp'] or (row['recorded_at'] or '').replace('T', ' ')
            lines.append(f"[{stamp[11:16]}] {self.display_name(row['raw_actor'])}: {row['content']}")
        return lines


def _text(value):
    return None if value is None else str(value)
