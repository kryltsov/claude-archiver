"""Plan indexing: every version of every Claude Code plan, linked to its sessions.

Sources, in order of trust:
- transcripts (~/.claude/projects/**/*.jsonl, subagents included):
    Write to a plan file     -> full text            (source 'write')
    Edit/MultiEdit           -> rebuilt from the previous known text (source 'edit')
    ExitPlanMode             -> full text submitted; its tool_result says approved or
                                rejected             (source 'exit_plan_mode')
    top-level "slug", plan_mode attachment -> session <-> plan link only
- the plans dir itself       -> whatever is on disk now (source 'disk'); adds a
                                version only when it differs from every known one

Versions are deduplicated on (slug, sha256), so rescans are idempotent. Text is
stored with '\\n' line endings. A version's citation is its earliest occurrence:
transcript_path + line_no, the <file>:<line> shape writer uses.

Incremental scans resume each transcript from a watermark (plan_scan). The
watermark stops before any tool call still waiting for its result, so that call
is re-read once the result lands.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

try:
    from .folders import normalize_path
except ImportError:
    from folders import normalize_path

SCHEMA = """
    CREATE TABLE IF NOT EXISTS plans (
        slug TEXT PRIMARY KEY,
        path TEXT,
        title TEXT,
        first_seen TEXT,
        last_seen TEXT,
        on_disk INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS plan_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content TEXT NOT NULL,
        source TEXT NOT NULL,          -- disk | write | edit | exit_plan_mode
        session_id TEXT,
        transcript_path TEXT,
        line_no INTEGER,
        captured_at TEXT,
        approved INTEGER,              -- 1 / 0 / NULL (unknown)
        UNIQUE (slug, sha256)
    );

    -- session_id is deliberately not a foreign key: a plan can be seen before
    -- its session is indexed, and must stay joinable after.
    CREATE TABLE IF NOT EXISTS plan_sessions (
        slug TEXT NOT NULL,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,            -- author | subagent | referenced
        first_line INTEGER,
        last_line INTEGER,
        PRIMARY KEY (slug, session_id, role)
    );

    CREATE TABLE IF NOT EXISTS plan_scan (
        transcript_path TEXT PRIMARY KEY,
        size INTEGER,
        mtime REAL,
        last_line INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_plan_versions_slug ON plan_versions(slug, captured_at);
    CREATE INDEX IF NOT EXISTS idx_plan_sessions_session ON plan_sessions(session_id);
"""

EDIT_TOOLS = ('Write', 'Edit', 'MultiEdit')


def create_schema(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plan_content'"
    ).fetchone()
    if not row:
        # One row per version; rowid = plan_versions.id
        conn.execute("""
            CREATE VIRTUAL TABLE plan_content USING fts5(
                slug, title, content, tokenize='porter unicode61'
            )
        """)
    conn.commit()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _clean(text: str) -> str:
    return text.replace('\r\n', '\n')


def _iso_utc(ts: float) -> str:
    """Same shape as transcript timestamps: 2026-09-17T11:47:00.809Z."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def plan_title(text: str) -> Optional[str]:
    """First '# ' heading, else the first non-empty line."""
    first = None
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith('# '):
            return s[2:].strip()[:200]
        if first is None:
            first = s
    return first[:200] if first else None


def apply_edits(base: Optional[str], edits: Iterable[dict]) -> Optional[str]:
    """Apply Edit-style replacements; None when any old_string isn't found."""
    text = base
    for e in edits:
        old = e.get('old_string') or ''
        new = e.get('new_string') or ''
        if text is None:
            if old == '':
                text = new  # Edit creating a new file
                continue
            return None
        if old == '' or old not in text:
            return None
        text = text.replace(old, new) if e.get('replace_all') else text.replace(old, new, 1)
    return text


def slug_for_path(file_path: Optional[str], plans_dir) -> Optional[str]:
    """Plan slug when file_path is a .md directly inside plans_dir."""
    n = normalize_path(file_path)
    if not n or '/' not in n or not n.lower().endswith('.md'):
        return None
    parent, name = n.rsplit('/', 1)
    if parent.lower() != (normalize_path(str(plans_dir)) or '').lower():
        return None
    return name[:-3]


def _edits_from(name: str, inp: dict, tur) -> list[dict]:
    """The edits that actually landed; toolUseResult wins (it has user changes)."""
    if name == 'MultiEdit':
        return [{k: _clean(v) if isinstance(v, str) else v for k, v in e.items()}
                for e in inp.get('edits') or [] if isinstance(e, dict)]
    if isinstance(tur, dict) and isinstance(tur.get('oldString'), str):
        return [{'old_string': _clean(tur['oldString']),
                 'new_string': _clean(tur.get('newString') or ''),
                 'replace_all': bool(tur.get('replaceAll'))}]
    return [{'old_string': _clean(inp.get('old_string') or ''),
             'new_string': _clean(inp.get('new_string') or ''),
             'replace_all': bool(inp.get('replace_all'))}]


class PlanIndexer:
    def __init__(self, conn: sqlite3.Connection, plans_dir: Path, projects_dir: Path = None):
        self.conn = conn
        self.plans_dir = Path(plans_dir)
        self.projects_dir = Path(projects_dir) if projects_dir else None
        self._touched: set[str] = set()
        self.stats = self._new_stats()

    @staticmethod
    def _new_stats() -> dict:
        return {'transcripts': 0, 'skipped': 0, 'versions_added': 0,
                'edits_unresolved': 0}

    # --- paths -------------------------------------------------------------

    def slug_for_path(self, file_path: Optional[str]) -> Optional[str]:
        return slug_for_path(file_path, self.plans_dir)

    def transcripts(self) -> list[Path]:
        """All transcripts incl. subagents, oldest first (so Edits find their base)."""
        if not self.projects_dir or not self.projects_dir.exists():
            return []
        files = list(self.projects_dir.rglob('*.jsonl'))
        return sorted(files, key=lambda p: (self._first_timestamp(p) or '', str(p)))

    @staticmethod
    def _first_timestamp(path: Path) -> Optional[str]:
        try:
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f):
                    if i > 200:
                        break
                    if '"timestamp"' not in line:
                        continue
                    try:
                        ts = json.loads(line).get('timestamp')
                    except json.JSONDecodeError:
                        continue
                    if ts:
                        return ts
        except OSError:
            pass
        return None

    # --- storage -----------------------------------------------------------

    def _touch_plan(self, slug: str, path: Optional[str], ts: Optional[str]):
        self.conn.execute("""
            INSERT INTO plans (slug, path, first_seen, last_seen) VALUES (?, ?, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                path = COALESCE(plans.path, excluded.path),
                first_seen = CASE WHEN plans.first_seen IS NULL
                                    OR excluded.first_seen < plans.first_seen
                             THEN excluded.first_seen ELSE plans.first_seen END,
                last_seen = CASE WHEN plans.last_seen IS NULL
                                   OR excluded.last_seen > plans.last_seen
                            THEN excluded.last_seen ELSE plans.last_seen END
        """, (slug, path, ts, ts))
        self._touched.add(slug)

    def record_version(self, slug: str, text: str, source: str, *,
                       session_id: str = None, transcript_path: str = None,
                       line_no: int = None, captured_at: str = None,
                       approved: Optional[int] = None, path: str = None) -> int:
        """Store one version; returns its id. Merges approval, keeps earliest citation."""
        text = _clean(text)
        sha = _sha(text)
        self._touch_plan(slug, path, captured_at)
        row = self.conn.execute(
            "SELECT id, captured_at, approved FROM plan_versions WHERE slug=? AND sha256=?",
            (slug, sha)
        ).fetchone()
        if row is None:
            cur = self.conn.execute("""
                INSERT INTO plan_versions (slug, sha256, content, source, session_id,
                    transcript_path, line_no, captured_at, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (slug, sha, text, source, session_id, transcript_path, line_no,
                  captured_at, approved))
            vid = cur.lastrowid
            self.conn.execute(
                "INSERT INTO plan_content (rowid, slug, title, content) VALUES (?, ?, ?, ?)",
                (vid, slug, plan_title(text), text)
            )
            self.stats['versions_added'] += 1
            return vid

        vid, old_ts, old_approved = row[0], row[1], row[2]
        merged = old_approved
        if approved is not None:
            merged = approved if old_approved is None else max(old_approved, approved)
        if merged != old_approved:
            self.conn.execute("UPDATE plan_versions SET approved=? WHERE id=?", (merged, vid))
        # Earlier occurrence (e.g. a backfill after a hook capture) takes the citation;
        # a disk snapshot never displaces a transcript citation.
        if (captured_at and source != 'disk' and (old_ts is None or captured_at < old_ts)):
            self.conn.execute("""
                UPDATE plan_versions SET source=?, session_id=?, transcript_path=?,
                    line_no=?, captured_at=? WHERE id=?
            """, (source, session_id, transcript_path, line_no, captured_at, vid))
        return vid

    def set_approved(self, vid: int, approved: int):
        self.conn.execute("""
            UPDATE plan_versions SET approved = CASE
                WHEN approved IS NULL THEN ? ELSE MAX(approved, ?) END
            WHERE id=?
        """, (approved, approved, vid))

    def _link(self, slug: str, session_id: str, role: str, first: int, last: int):
        self.conn.execute("""
            INSERT INTO plan_sessions (slug, session_id, role, first_line, last_line)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug, session_id, role) DO UPDATE SET
                first_line = COALESCE(MIN(plan_sessions.first_line, excluded.first_line),
                                      plan_sessions.first_line, excluded.first_line),
                last_line = COALESCE(MAX(plan_sessions.last_line, excluded.last_line),
                                     plan_sessions.last_line, excluded.last_line)
        """, (slug, session_id, role, first, last))

    def _latest_before(self, slug: str, ts: Optional[str]) -> Optional[str]:
        """Latest stored text of slug captured before ts (the base for an Edit)."""
        if ts:
            row = self.conn.execute("""
                SELECT content FROM plan_versions WHERE slug=? AND captured_at < ?
                ORDER BY captured_at DESC, id DESC LIMIT 1
            """, (slug, ts)).fetchone()
        else:
            row = self.conn.execute(
                "SELECT content FROM plan_versions WHERE slug=? ORDER BY captured_at DESC, id DESC LIMIT 1",
                (slug,)
            ).fetchone()
        return row[0] if row else None

    def _finish(self):
        """Refresh titles of touched plans; fold 'referenced' into 'author'."""
        for slug in self._touched:
            row = self.conn.execute("""
                SELECT content FROM plan_versions WHERE slug=?
                ORDER BY captured_at DESC, id DESC LIMIT 1
            """, (slug,)).fetchone()
            if row:
                self.conn.execute("UPDATE plans SET title=? WHERE slug=?",
                                  (plan_title(row[0]), slug))
        self._touched.clear()
        self.conn.execute("""
            UPDATE plan_sessions AS a SET
                first_line = COALESCE(MIN(a.first_line, r.first_line), a.first_line, r.first_line),
                last_line = COALESCE(MAX(a.last_line, r.last_line), a.last_line, r.last_line)
            FROM plan_sessions AS r
            WHERE a.role = 'author' AND r.role = 'referenced'
              AND r.slug = a.slug AND r.session_id = a.session_id
        """)
        self.conn.execute("""
            DELETE FROM plan_sessions WHERE role = 'referenced' AND EXISTS (
                SELECT 1 FROM plan_sessions a WHERE a.role = 'author'
                  AND a.slug = plan_sessions.slug AND a.session_id = plan_sessions.session_id)
        """)
        self.conn.commit()

    # --- transcript scan ---------------------------------------------------

    def scan_transcript(self, path: Path, start_line: int = 0) -> int:
        """Scan lines after start_line; returns the new watermark (last safe line)."""
        path = Path(path)
        is_subagent = 'subagents' in path.parts
        fallback_sid = (path.parent.parent.name if is_subagent else path.stem)
        writer_role = 'subagent' if is_subagent else 'author'
        tpath = str(path)

        current: dict[str, str] = {}   # slug -> text as of the last applied change
        pending: dict[str, dict] = {}  # tool_use_id -> call waiting for its result
        links: dict[tuple, list] = {}  # (slug, sid, role) -> [first_line, last_line]
        seen: dict[str, list] = {}     # slug -> [first_ts, last_ts, path]
        line_no = start_line

        def link(slug, sid, role, n):
            r = links.setdefault((slug, sid, role), [n, n])
            r[0], r[1] = min(r[0], n), max(r[1], n)

        def saw(slug, ts, plan_path=None):
            r = seen.setdefault(slug, [ts, ts, plan_path])
            if ts:
                r[0] = ts if not r[0] or ts < r[0] else r[0]
                r[1] = ts if not r[1] or ts > r[1] else r[1]
            r[2] = r[2] or plan_path

        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for n, line in enumerate(f, 1):
                line_no = n
                if n <= start_line:
                    continue
                if '"slug"' not in line and 'plan' not in line.lower():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                sid = entry.get('sessionId') or fallback_sid
                ts = entry.get('timestamp')

                if entry.get('slug') and not is_subagent:
                    link(entry['slug'], sid, 'referenced', n)
                    saw(entry['slug'], ts)

                att = entry.get('attachment')
                if isinstance(att, dict) and att.get('type') == 'plan_mode' and not is_subagent:
                    slug = self.slug_for_path(att.get('planFilePath'))
                    if slug:
                        link(slug, sid, 'referenced', n)
                        saw(slug, ts, att.get('planFilePath'))

                content = (entry.get('message') or {}).get('content')
                if not isinstance(content, list):
                    continue
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    kind = item.get('type')
                    if kind == 'tool_use':
                        self._on_tool_use(item, entry, sid, ts, n, tpath,
                                          writer_role, pending, link)
                    elif kind == 'tool_result' and item.get('tool_use_id') in pending:
                        call = pending.pop(item['tool_use_id'])
                        self._on_tool_result(call, item, entry, current, tpath,
                                             writer_role, link)

        for slug, (first_ts, last_ts, plan_path) in seen.items():
            self._touch_plan(slug, plan_path, first_ts)
            if last_ts != first_ts:
                self._touch_plan(slug, None, last_ts)
        for (slug, sid, role), (first, last) in links.items():
            self._link(slug, sid, role, first, last)
        self._finish()
        self.stats['transcripts'] += 1
        if pending:
            return min(c['line_no'] for c in pending.values()) - 1
        return line_no

    def _on_tool_use(self, item, entry, sid, ts, n, tpath, writer_role, pending, link):
        name = item.get('name')
        inp = item.get('input') or {}
        if not isinstance(inp, dict):
            return
        if name in EDIT_TOOLS:
            slug = self.slug_for_path(inp.get('file_path'))
            if not slug:
                return
        elif name == 'ExitPlanMode':
            slug = self.slug_for_path(inp.get('planFilePath')) or entry.get('slug')
            plan = inp.get('plan')
            if not slug or not isinstance(plan, str) or not plan.strip():
                return
            vid = self.record_version(slug, plan, 'exit_plan_mode', session_id=sid,
                                      transcript_path=tpath, line_no=n, captured_at=ts,
                                      path=inp.get('planFilePath'))
            link(slug, sid, writer_role, n)
            pending[item.get('id')] = {'name': name, 'slug': slug, 'vid': vid,
                                       'plan': _clean(plan), 'sid': sid, 'ts': ts,
                                       'line_no': n, 'path': inp.get('planFilePath')}
            return
        else:
            return
        pending[item.get('id')] = {'name': name, 'slug': slug, 'input': inp, 'sid': sid,
                                   'ts': ts, 'line_no': n, 'path': inp.get('file_path')}

    def _on_tool_result(self, call, item, entry, current, tpath, writer_role, link):
        ok = not item.get('is_error')
        tur = entry.get('toolUseResult')
        slug, sid, n = call['slug'], call['sid'], call['line_no']
        where = dict(session_id=sid, transcript_path=tpath, line_no=n,
                     captured_at=call['ts'], path=call['path'])

        if call['name'] == 'ExitPlanMode':
            final = tur.get('plan') if isinstance(tur, dict) else None
            if ok and isinstance(final, str) and _clean(final) != call['plan']:
                # Plan edited in the approval dialog: the edited text is what was approved.
                self.record_version(slug, final, 'exit_plan_mode', approved=1, **where)
            else:
                self.set_approved(call['vid'], 1 if ok else 0)
            return

        if not ok:
            return  # rejected or failed: the file didn't change
        inp = call['input']
        if call['name'] == 'Write':
            text = tur.get('content') if isinstance(tur, dict) else None
            if not isinstance(text, str):
                text = inp.get('content')
            if not isinstance(text, str):
                return
            text = _clean(text)
            self.record_version(slug, text, 'write', **where)
        else:
            edits = _edits_from(call['name'], inp, tur)
            text = apply_edits(current.get(slug), edits)
            if text is None:
                text = apply_edits(self._latest_before(slug, call['ts']), edits)
            if text is None:
                current.pop(slug, None)
                self.stats['edits_unresolved'] += 1
                link(slug, sid, writer_role, n)
                return
            self.record_version(slug, text, 'edit', **where)
        current[slug] = text
        link(slug, sid, writer_role, n)

    # --- disk + drivers ----------------------------------------------------

    def snapshot_plans_dir(self) -> int:
        """Record each plan file as it is now; flag which plans are on disk."""
        on_disk = []
        if self.plans_dir.exists():
            for p in sorted(self.plans_dir.glob('*.md')):
                try:
                    text = p.read_text(encoding='utf-8', errors='replace')
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                self.record_version(p.stem, text, 'disk',
                                    captured_at=_iso_utc(mtime), path=str(p))
                self.conn.execute("UPDATE plans SET path=? WHERE slug=?", (str(p), p.stem))
                on_disk.append(p.stem)
        self.conn.execute("UPDATE plans SET on_disk=0")
        self.conn.executemany("UPDATE plans SET on_disk=1 WHERE slug=?",
                              [(s,) for s in on_disk])
        self._finish()
        return len(on_disk)

    def _scan_with_watermark(self, path: Path, full: bool):
        try:
            st = path.stat()
        except OSError:
            return
        key = str(path)
        row = self.conn.execute(
            "SELECT size, mtime, last_line FROM plan_scan WHERE transcript_path=?", (key,)
        ).fetchone()
        if not full and row and row[0] == st.st_size and row[1] == st.st_mtime:
            self.stats['skipped'] += 1
            return
        start = 0
        if not full and row and st.st_size >= row[0]:
            start = row[2] or 0
        last = self.scan_transcript(path, start)
        self.conn.execute("""
            INSERT INTO plan_scan (transcript_path, size, mtime, last_line) VALUES (?, ?, ?, ?)
            ON CONFLICT(transcript_path) DO UPDATE SET
                size=excluded.size, mtime=excluded.mtime, last_line=excluded.last_line
        """, (key, st.st_size, st.st_mtime, last))
        self.conn.commit()

    def backfill(self) -> dict:
        """Scan every transcript from the start, then snapshot the plans dir."""
        self.stats = self._new_stats()
        for p in self.transcripts():
            self._scan_with_watermark(p, full=True)
        self.stats['on_disk'] = self.snapshot_plans_dir()
        return self.stats

    def index_incremental(self, paths: Iterable[Path] = None) -> dict:
        """Scan only what's new since each transcript's watermark, then the plans dir."""
        self.stats = self._new_stats()
        for p in (paths if paths is not None else self.transcripts()):
            self._scan_with_watermark(Path(p), full=False)
        self.stats['on_disk'] = self.snapshot_plans_dir()
        return self.stats
