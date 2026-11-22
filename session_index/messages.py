"""The conversation itself: one row per user or assistant entry, kept for good.

Compacting doesn't remove anything from a transcript. Claude Code appends a
`system/compact_boundary` entry and a user entry flagged `isCompactSummary` to the same
`.jsonl`, and everything earlier stays in the file. This table follows the same rule:
reindexing only ever inserts rows, never updates or deletes them. A compaction adds a
`compact_summary` row and increments `compact_epoch` for the messages after it.

Stored: prompts, replies, and a one-line summary per tool call (`[Edit: path]`,
`[Bash: cmd]`). Tool results and full tool inputs stay in the transcript only;
`line_no` points back to them.
"""

import re
import sqlite3
from typing import Optional

SCHEMA = """
    CREATE TABLE IF NOT EXISTS messages (
        session_id TEXT NOT NULL,
        uuid TEXT NOT NULL,            -- the entry's uuid; 'line:<n>' if it has none
        parent_uuid TEXT,
        line_no INTEGER NOT NULL,      -- 1-based line in the transcript at first insert
        timestamp TEXT,
        role TEXT NOT NULL,            -- user | assistant
        kind TEXT NOT NULL,            -- prompt | meta | compact_summary | reply | tool
        text TEXT NOT NULL,
        is_sidechain INTEGER DEFAULT 0,
        compact_epoch INTEGER DEFAULT 0,  -- compactions before this message
        PRIMARY KEY (session_id, uuid)
    );
    CREATE INDEX IF NOT EXISTS idx_messages_order ON messages(session_id, line_no);
"""

# Columns added to the sessions table.
SESSION_COLUMNS = {
    'compaction_count': 'INTEGER DEFAULT 0',
    # 1 once this indexer has stored the session's messages. Rows from before the
    # messages table start at 0, so the next index run re-reads them.
    'messages_scanned': 'INTEGER DEFAULT 0',
}

TOOL_STUB_MAX = 200

# User entries that are slash-command echoes or output, not something the user typed.
_COMMAND_TAGS = ('<command-name>', '<command-message>', '<local-command-stdout>',
                 '<local-command-stderr>', '<local-command-caveat>')


def create_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)


def _text_blocks(content) -> list:
    if isinstance(content, str):
        return [content]
    out = []
    for item in content or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict) and item.get('type') == 'text':
            t = item.get('text', '')
            if t and t != '(no content)':
                out.append(t)
    return out


def _tool_stubs(content) -> list:
    try:
        from .analyzer import _summarize_tool_call
    except ImportError:
        from analyzer import _summarize_tool_call
    if not isinstance(content, list):
        return []
    return [_summarize_tool_call(item)[:TOOL_STUB_MAX] for item in content
            if isinstance(item, dict) and item.get('type') == 'tool_use']


def is_compact_boundary(entry: dict) -> bool:
    return entry.get('type') == 'system' and entry.get('subtype') == 'compact_boundary'


def message_row(entry: dict, line_no: int, epoch: int) -> Optional[dict]:
    """The row to store for one transcript entry, or None (tool results, system lines)."""
    etype = entry.get('type')
    if etype not in ('user', 'assistant'):
        return None
    content = (entry.get('message') or {}).get('content', '')

    if etype == 'user':
        text = '\n'.join(t for t in _text_blocks(content) if t.strip())
        if not text:
            return None  # tool results only
        if entry.get('isCompactSummary'):
            kind = 'compact_summary'
        elif entry.get('isMeta') or text.lstrip().startswith(_COMMAND_TAGS):
            kind = 'meta'
        else:
            kind = 'prompt'
    else:
        texts = _text_blocks(content)
        stubs = _tool_stubs(content)
        text = '\n'.join(texts + stubs)
        if not text:
            return None  # thinking-only entries: their text isn't in the transcript
        kind = 'reply' if texts else 'tool'

    return {
        'uuid': entry.get('uuid') or f'line:{line_no}',
        'parent_uuid': entry.get('parentUuid'),
        'line_no': line_no,
        'timestamp': entry.get('timestamp'),
        'role': etype,
        'kind': kind,
        'text': text,
        'is_sidechain': 1 if entry.get('isSidechain') else 0,
        'compact_epoch': epoch,
    }


_PREAMBLE = re.compile(r'^This session is being continued from a previous conversation.*?$',
                       re.MULTILINE)


def compact_topic(summary: str, n: int) -> str:
    """A one-line topic for the n-th compaction: the summary's first real line."""
    body = _PREAMBLE.sub('', summary)
    m = re.search(r'Primary Request and Intent:?\s*(.+)', body)
    lines = [m.group(1)] if m else []
    lines += body.splitlines()
    for line in lines:
        line = re.sub(r'^[\s\-*#\d.]+', '', line).strip().strip('*').strip()
        if len(line) > 10 and not line.lower().startswith('summary'):
            return f"Compaction {n}: {line}"[:120]
    return f"Compaction {n}"


def store(conn: sqlite3.Connection, session_id: str, rows: list) -> int:
    """Insert rows not already stored; returns how many were new."""
    before = conn.total_changes
    conn.executemany("""
        INSERT OR IGNORE INTO messages (session_id, uuid, parent_uuid, line_no, timestamp,
            role, kind, text, is_sidechain, compact_epoch)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(session_id, r['uuid'], r['parent_uuid'], r['line_no'], r['timestamp'], r['role'],
           r['kind'], r['text'], r['is_sidechain'], r['compact_epoch']) for r in rows])
    return conn.total_changes - before


def search_text(conn: sqlite3.Connection, session_id: str) -> str:
    """Everything stored for a session, in order: the full-text search content."""
    rows = conn.execute(
        "SELECT text FROM messages WHERE session_id=? ORDER BY line_no", (session_id,)
    ).fetchall()
    return '\n'.join(r[0] for r in rows)


def entries(conn: sqlite3.Connection, session_id: str) -> list:
    """Stored messages as {type, text, timestamp} entries, in order; [] if none stored."""
    try:
        rows = conn.execute(
            "SELECT role, text, timestamp FROM messages WHERE session_id=? ORDER BY line_no",
            (session_id,)
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # DB from before the messages table
    return [{'type': r[0], 'text': r[1], 'timestamp': r[2] or ''} for r in rows]
