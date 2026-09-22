"""Multiline-aware parser for the legacy `[YYYY-MM-DD HH:MM:SS] actor: content` logs.

save__logs() writes one header line per event and never escapes content, so a
message body containing a newline becomes several physical lines and a body line
can look exactly like a header. The parser therefore works on bytes, keeps every
offset, and reports what it cannot decide instead of guessing silently.

Rules:
  * A line matching HEADER starts a new record; every following line up to the
    next header belongs to the current record's content, newlines preserved.
  * Lines before the first header are orphan bytes, reported as an issue.
  * Nothing is dropped: each byte of the file is inside a record, a trailing
    newline, or an issue range.
"""
from dataclasses import dataclass, field
import hashlib
import re

PARSER_VERSION = '2.0.0'

# Non-greedy actor so the first ': ' separates actor from content, matching the
# writer's f"[{ts}] {user}: {msg}". Content may be empty.
HEADER = re.compile(r'^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.*?): (.*)$', re.S)

BOM = b'\xef\xbb\xbf'


@dataclass
class ParsedMessage:
    raw_timestamp: str
    raw_actor: str
    content: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    raw_hash: str
    is_multiline: bool


@dataclass
class ParseIssue:
    issue_type: str
    byte_start: int
    byte_end: int
    line_start: int
    line_end: int
    description: str
    raw_excerpt_hash: str


@dataclass
class ParsedFile:
    encoding: str
    newline_style: str
    has_bom: bool
    byte_size: int
    messages: list = field(default_factory=list)
    issues: list = field(default_factory=list)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def detect_newline_style(data: bytes) -> str:
    crlf = data.count(b'\r\n')
    lf = data.count(b'\n') - crlf
    if crlf and lf:
        return 'mixed'
    if crlf:
        return 'crlf'
    if lf:
        return 'lf'
    return 'none'


def _physical_lines(data: bytes):
    """Yield (byte_start, byte_end_including_newline, text_without_newline).

    Splitting on b'\\n' keeps byte offsets exact for CRLF too; the trailing \\r
    is stripped from the text but stays inside the byte range.
    """
    start = 0
    index = 0
    line_number = 1
    while index < len(data):
        end = data.find(b'\n', index)
        if end == -1:
            yield line_number, start, len(data), data[start:]
            return
        yield line_number, start, end + 1, data[start:end]
        line_number += 1
        start = index = end + 1


def parse_bytes(data: bytes) -> ParsedFile:
    """Parse one legacy log file. Raises UnicodeDecodeError on invalid UTF-8.

    Decoding is strict on purpose: replacing undecodable bytes would silently
    corrupt the archive this migration exists to preserve.
    """
    has_bom = data.startswith(BOM)
    body = data[len(BOM):] if has_bom else data
    offset = len(BOM) if has_bom else 0
    body.decode('utf-8')  # Strict check; caller records the failure per source.

    result = ParsedFile(encoding='utf-8', newline_style=detect_newline_style(body),
                        has_bom=has_bom, byte_size=len(data))
    current = None          # (timestamp, actor, [content lines], byte_start, line_start)
    current_end = None
    current_line_end = None
    orphan = None

    def flush():
        nonlocal current, current_end, current_line_end
        if current is None:
            return
        timestamp, actor, parts, byte_start, line_start = current
        raw = data[byte_start:current_end]
        content = '\n'.join(parts)
        result.messages.append(ParsedMessage(
            raw_timestamp=timestamp, raw_actor=actor, content=content,
            byte_start=byte_start, byte_end=current_end,
            line_start=line_start, line_end=current_line_end,
            raw_hash=_hash(raw), is_multiline=len(parts) > 1))
        current = current_end = current_line_end = None

    for number, line_start_byte, line_end_byte, raw_line in _physical_lines(body):
        text = raw_line.decode('utf-8')
        if text.endswith('\r'):
            text = text[:-1]
        match = HEADER.match(text)
        absolute_start = line_start_byte + offset
        absolute_end = line_end_byte + offset
        if match:
            if orphan is not None:
                start, end, first, last = orphan
                result.issues.append(ParseIssue(
                    'orphan_text', start, end, first, last,
                    'content before the first header line',
                    _hash(data[start:end])))
                orphan = None
            flush()
            current = (match.group(1), match.group(2), [match.group(3)],
                       absolute_start, number)
            current_end = absolute_end
            current_line_end = number
        elif current is not None:
            current[2].append(text)
            current_end = absolute_end
            current_line_end = number
        else:
            if orphan is None:
                orphan = [absolute_start, absolute_end, number, number]
            else:
                orphan[1], orphan[3] = absolute_end, number
    flush()
    if orphan is not None:
        start, end, first, last = orphan
        result.issues.append(ParseIssue(
            'orphan_text', start, end, first, last,
            'content with no header line in the file', _hash(data[start:end])))

    _flag_backward_timestamps(result)
    return result


def _flag_backward_timestamps(result: ParsedFile) -> None:
    """Flag headers whose timestamp moves backwards.

    save__logs() appends in real time, so a header timestamp earlier than the
    previous one is a hint that a quoted line was read as a header. It stays a
    message — discarding it would lose real content — but it is reported so the
    original bytes can be reviewed.
    """
    previous = None
    for message in result.messages:
        if previous is not None and message.raw_timestamp < previous:
            result.issues.append(ParseIssue(
                'ambiguous_header_timestamp_regression',
                message.byte_start, message.byte_end,
                message.line_start, message.line_end,
                'header timestamp earlier than the previous record; may be quoted content',
                message.raw_hash))
        previous = max(previous, message.raw_timestamp) if previous else message.raw_timestamp


def coverage(parsed: ParsedFile, data: bytes):
    """Return (covered_bytes, total_bytes) to prove nothing was dropped."""
    covered = len(BOM) if parsed.has_bom else 0
    for message in parsed.messages:
        covered += message.byte_end - message.byte_start
    for issue in parsed.issues:
        if issue.issue_type == 'orphan_text':
            covered += issue.byte_end - issue.byte_start
    return covered, len(data)
