"""Bridge to an upstream claude-session-index DB. Never writes to it.

- copy_from_upstream(): one-time consistent copy via SQLite's online backup API.
- import_topics(): pull new hook-captured topics (upstream's UserPromptSubmit /
  PreCompact / SessionEnd hook) that transcripts can't rebuild.
"""

import os
import sqlite3
from pathlib import Path


def _open_ro(path: Path) -> sqlite3.Connection:
    """Open a DB read-only (no journal/WAL writes to the source)."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)


META_LAST_TOPIC_ID = 'upstream_topics_last_id'


def import_topics(conn: sqlite3.Connection, upstream_db) -> int:
    """Copy upstream hook topics (source 'hook_*') newer than the last import.

    Deduplicated on (session_id, topic, captured_at), so rows already in the
    copied DB aren't doubled. Returns the number of rows added.
    """
    if not upstream_db:
        return 0
    upstream_db = Path(upstream_db).expanduser()
    if not upstream_db.exists():
        return 0
    conn.execute("CREATE TABLE IF NOT EXISTS archiver_meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM archiver_meta WHERE key=?",
                       (META_LAST_TOPIC_ID,)).fetchone()
    last_id = int(row[0]) if row else 0

    src = _open_ro(upstream_db)
    try:
        rows = src.execute("""
            SELECT id, session_id, topic, captured_at, exchange_number, source
            FROM session_topics WHERE id > ? ORDER BY id
        """, (last_id,)).fetchall()
    finally:
        src.close()
    if not rows:
        return 0

    added = 0
    for _id, sid, topic, captured_at, exchange_number, source in rows:
        if not (source or '').startswith('hook_'):
            continue  # compaction summaries: the archiver derives those itself
        exists = conn.execute(
            "SELECT 1 FROM session_topics WHERE session_id=? AND topic=? AND captured_at=?",
            (sid, topic, captured_at)
        ).fetchone()
        if exists:
            continue
        conn.execute("""
            INSERT INTO session_topics (session_id, topic, captured_at, exchange_number, source)
            VALUES (?, ?, ?, ?, ?)
        """, (sid, topic, captured_at, exchange_number, source))
        added += 1
    conn.execute("""
        INSERT INTO archiver_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (META_LAST_TOPIC_ID, str(rows[-1][0])))
    conn.commit()
    return added


def copy_from_upstream(src: Path, dest: Path, force: bool = False) -> dict:
    """Copy src → dest with Connection.backup. Refuses to overwrite dest unless force.

    Writes to a temp file next to dest, then swaps it in, so a failed copy
    never leaves a half-written DB behind.
    """
    src = Path(src).expanduser()
    dest = Path(dest).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"Upstream DB not found: {src}")
    if src.resolve() == dest.resolve():
        raise ValueError(f"Source and destination are the same file: {src}")
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists (use --force to replace it)")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".init-tmp")
    if tmp.exists():
        tmp.unlink()

    src_conn = _open_ro(src)
    try:
        dst_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dst_conn)
            count = dst_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        finally:
            dst_conn.close()
    finally:
        src_conn.close()

    for suffix in ("-wal", "-shm"):
        side = dest.with_name(dest.name + suffix)
        if side.exists():
            side.unlink()
    os.replace(tmp, dest)
    return {"src": str(src), "dest": str(dest), "sessions": count}
