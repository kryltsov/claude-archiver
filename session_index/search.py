#!/usr/bin/env python3
"""
Session search — CLI + importable module.

Search and query the session index database.

Usage:
    python3 -m session_index.search search "query"
    python3 -m session_index.search find --client "Connection Lab" --tag "hook"
    python3 -m session_index.search topics <session_id>
    python3 -m session_index.search recent [N]
    python3 -m session_index.search stats
    python3 -m session_index.search tools <tool_name>
"""

import json
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

try:
    from . import config
    from . import scope as scope_mod
except ImportError:
    import config
    import scope as scope_mod


def _escape_fts_query(query: str) -> str:
    """Escape a user query for FTS5 MATCH safety.

    Wraps each token in double quotes so periods, hyphens, and
    reserved words (AND, OR, NOT, NEAR) are treated as literals.
    If the user already quoted a phrase, leave it intact.
    """
    if not query or not query.strip():
        return ""

    tokens = []
    i = 0
    chars = query.strip()

    while i < len(chars):
        # Skip whitespace
        if chars[i].isspace():
            i += 1
            continue

        # Already-quoted phrase — preserve it verbatim
        if chars[i] == '"':
            end = chars.find('"', i + 1)
            if end == -1:
                # Unterminated quote — wrap the rest as a single token
                tokens.append(f'"{chars[i + 1:]}"')
                break
            tokens.append(chars[i:end + 1])
            i = end + 1
        else:
            # Unquoted token — collect until next whitespace or quote
            start = i
            while i < len(chars) and not chars[i].isspace() and chars[i] != '"':
                i += 1
            token = chars[start:i]
            tokens.append(f'"{token}"')

    return " ".join(tokens)


class SessionSearch:
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or config.get_db_path()
        self.conn = None

    def connect(self):
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        scope_mod.register(self.conn)

    def close(self):
        if self.conn:
            self.conn.close()

    def search(self, query: str, limit: int = 20,
               scope: 'scope_mod.Scope' = None) -> list[dict]:
        """Full-text search across all session content, optionally scoped."""
        scope_where, scope_params = (scope or scope_mod.Scope()).where('s')
        rows = self.conn.execute(f"""
            SELECT s.session_id, s.project_name, s.title, s.title_display,
                   s.client, s.tags, s.exchange_count, s.start_time,
                   s.duration_minutes, s.has_compaction,
                   snippet(session_content, 1, '>>>', '<<<', '...', 40) as snippet
            FROM session_content
            JOIN sessions s ON s.session_id = session_content.session_id
            WHERE session_content MATCH ? AND {scope_where}
            ORDER BY rank
            LIMIT ?
        """, (_escape_fts_query(query), *scope_params, limit)).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            r['topics'] = self._get_topics(row['session_id'])
            results.append(r)
        return results

    def find(self, client: str = None, tag: str = None, tool: str = None,
             agent: str = None, date: str = None, week: bool = False,
             days: int = None, project: str = None,
             exclude_project: str = None, has_compaction: bool = None,
             limit: int = 20, scope: 'scope_mod.Scope' = None) -> list[dict]:
        """Filter sessions by various criteria.

        project/date/week/days are the upstream filters; they're folded into a
        Scope (dates use the overlap rule). Pass scope= to use groups/--from/--to.
        """
        if scope is None:
            scope = scope_mod.build(project=project, on=date, week=week, days=days,
                                    group_config={})
        conditions = []
        params = []

        if client:
            conditions.append("s.client LIKE ?")
            params.append(f"%{client}%")

        if tag:
            conditions.append("s.tags LIKE ?")
            params.append(f"%{tag}%")

        if tool:
            conditions.append("s.session_id IN (SELECT session_id FROM session_tools WHERE tool_name LIKE ?)")
            params.append(f"%{tool}%")

        if agent:
            conditions.append("s.session_id IN (SELECT session_id FROM session_agents WHERE agent_name LIKE ?)")
            params.append(f"%{agent}%")

        scope_where, scope_params = scope.where('s')
        conditions.append(scope_where)
        params.extend(scope_params)

        if exclude_project:
            conditions.append("NOT (s.project_name LIKE ? OR s.project LIKE ?)")
            params.extend([f"%{exclude_project}%", f"%{exclude_project}%"])

        if has_compaction is not None:
            conditions.append("s.has_compaction = ?")
            params.append(1 if has_compaction else 0)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)

        rows = self.conn.execute(f"""
            SELECT s.session_id, s.project_name, s.title, s.title_display,
                   s.client, s.tags, s.exchange_count, s.start_time,
                   s.duration_minutes, s.has_compaction
            FROM sessions s
            WHERE {where}
            ORDER BY s.start_time DESC
            LIMIT ?
        """, params).fetchall()

        results = []
        for row in rows:
            r = dict(row)
            r['topics'] = self._get_topics(row['session_id'])
            results.append(r)
        return results

    def topics(self, session_id: str) -> list[dict]:
        """Get topic timeline for a session."""
        rows = self.conn.execute("""
            SELECT topic, captured_at, exchange_number, source
            FROM session_topics
            WHERE session_id = ?
            ORDER BY captured_at
        """, (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, scope: 'scope_mod.Scope' = None, limit: int = None) -> list[dict]:
        """Sessions in scope with where they ran: the writer handoff (list --json).

        Oldest first; every field needed to open the transcript and place it in a repo.
        """
        scope_where, params = (scope or scope_mod.Scope()).where('s')
        sql = f"""
            SELECT s.session_id, s.file_path AS transcript_path, s.cwd, s.project_root,
                   s.git_common_dir, s.git_branch, s.start_time, s.end_time,
                   s.title_display AS title, s.project, s.exchange_count
            FROM sessions s WHERE {scope_where}
            ORDER BY s.start_time
        """
        if limit:
            sql += " LIMIT ?"
            params = [*params, limit]
        rows = [dict(r) for r in self.conn.execute(sql, params)]
        has_cwds = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_cwds'").fetchone()
        for r in rows:
            r['cwds'] = [c[0] for c in self.conn.execute(
                "SELECT cwd FROM session_cwds WHERE session_id=?", (r['session_id'],))] \
                if has_cwds else []
        return rows

    def recent(self, n: int = 10, scope: 'scope_mod.Scope' = None) -> list[dict]:
        """Get N most recent sessions."""
        return self.find(limit=n, scope=scope)

    def stats(self) -> dict:
        """Get overall statistics."""
        try:
            from session_index.indexer import SessionIndexer
        except ImportError:
            try:
                from .indexer import SessionIndexer
            except ImportError:
                try:
                    from session_indexer import SessionIndexer
                except ImportError:
                    return {"error": "SessionIndexer not available"}
        indexer = SessionIndexer(self.db_path)
        indexer.conn = self.conn
        return indexer.get_stats()

    def tools_usage(self, tool_name: str = None, limit: int = 20) -> list[dict]:
        """Find sessions using a specific tool, or show top tools."""
        if tool_name:
            rows = self.conn.execute("""
                SELECT s.session_id, s.title, s.title_display, s.start_time,
                       st.tool_name, st.use_count
                FROM session_tools st
                JOIN sessions s ON s.session_id = st.session_id
                WHERE st.tool_name LIKE ?
                ORDER BY st.use_count DESC
                LIMIT ?
            """, (f"%{tool_name}%", limit)).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT tool_name, SUM(use_count) as total,
                       COUNT(DISTINCT session_id) as session_count
                FROM session_tools
                GROUP BY tool_name
                ORDER BY total DESC
                LIMIT ?
            """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _get_topics(self, session_id: str) -> list[dict]:
        """Get topics for a session (compact)."""
        rows = self.conn.execute("""
            SELECT topic, source
            FROM session_topics
            WHERE session_id = ?
            ORDER BY captured_at
            LIMIT 10
        """, (session_id,)).fetchall()
        return [dict(r) for r in rows]


def format_result(r: dict, show_topics: bool = True) -> str:
    """Format a single search result for CLI output."""
    lines = []

    # Title line with bullet
    sid = r['session_id'][:8]
    title = r.get('title_display') or r.get('title') or '(unnamed)'
    if len(title) > 70:
        title = title[:67] + '...'
    lines.append(f"  ◆ {sid} · {title}")

    # Metadata line
    meta = []
    if r.get('start_time'):
        meta.append(r['start_time'][:10])
    if r.get('project_name'):
        meta.append(r['project_name'])
    if r.get('client'):
        meta.append(r['client'])
    if r.get('exchange_count'):
        meta.append(f"{r['exchange_count']} exchanges")
    if r.get('duration_minutes'):
        meta.append(f"{r['duration_minutes']}min")
    if r.get('has_compaction'):
        meta.append("compacted")
    if meta:
        lines.append(f"    {' · '.join(meta)}")

    # Snippet (from FTS search)
    if r.get('snippet'):
        snippet = r['snippet'].replace('>>>', '').replace('<<<', '').replace('\n', ' ')[:120]
        lines.append(f"    \"{snippet}\"")

    # Topics
    if show_topics and r.get('topics'):
        topic_strs = [t['topic'] for t in r['topics'][:5]]
        if topic_strs:
            lines.append(f"    topics: {' → '.join(topic_strs)}")

    # Tags
    if r.get('tags'):
        lines.append(f"    [{r['tags']}]")

    # Resume command — visually distinct as an action
    lines.append(f"    → claude --resume {r['session_id']}")

    return '\n'.join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session search")
    parser.add_argument('--db-path', type=str, default=None,
                        help='Path to sessions.db (overrides config)')
    subparsers = parser.add_subparsers(dest='command')

    # search
    sp = subparsers.add_parser('search', help='Full-text search')
    sp.add_argument('query', help='Search query')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.add_argument('--context', action='store_true',
                    help='Show conversation exchanges inline with results')

    # find
    sp = subparsers.add_parser('find', help='Filter sessions')
    sp.add_argument('--client', help='Filter by client name')
    sp.add_argument('--tag', help='Filter by tag')
    sp.add_argument('--tool', help='Filter by tool used')
    sp.add_argument('--agent', help='Filter by agent used')
    sp.add_argument('--date', help='Filter by date (YYYY-MM-DD)')
    sp.add_argument('--week', action='store_true', help='Last 7 days')
    sp.add_argument('--project', help='Filter by project')
    sp.add_argument('--compacted', action='store_true', help='Only compacted sessions')
    sp.add_argument('-n', '--limit', type=int, default=20)

    # topics
    sp = subparsers.add_parser('topics', help='Topic timeline for a session')
    sp.add_argument('session_id', help='Session ID (full or prefix)')

    # recent
    sp = subparsers.add_parser('recent', help='Recent sessions')
    sp.add_argument('n', nargs='?', type=int, default=10)

    # stats
    subparsers.add_parser('stats', help='Database statistics')

    # tools
    sp = subparsers.add_parser('tools', help='Tool usage')
    sp.add_argument('tool_name', nargs='?', help='Specific tool to search for')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    db_path = Path(args.db_path) if args.db_path else config.get_db_path()
    config.ensure_indexed(db_path)
    searcher = SessionSearch(db_path=db_path)
    searcher.connect()

    try:
        if args.command == 'search':
            results = searcher.search(args.query, args.limit)
            if not results:
                print(f"No results for: {args.query}")
                return
            print(f"\n🔍 {len(results)} results for \"{args.query}\"\n")
            for r in results:
                print(format_result(r))
                if args.context:
                    try:
                        from session_index.analyzer import get_context
                    except ImportError:
                        try:
                            from .analyzer import get_context
                        except ImportError:
                            try:
                                from session_analyzer import get_context
                            except ImportError:
                                print("    (context not available — analyzer module not found)")
                                print()
                                continue
                    ctx = get_context(r['session_id'], query=args.query, limit=3)
                    if ctx.get('exchanges'):
                        for ex in ctx['exchanges']:
                            ts = ex['timestamp'][:16] if ex['timestamp'] else ''
                            try:
                                dt = datetime.fromisoformat(ts)
                                ts_display = dt.strftime("%b %d, %H:%M")
                            except (ValueError, TypeError):
                                ts_display = ts
                            print(f"    ┌─ {ts_display} {'─' * max(1, 36 - len(ts_display))}")
                            user_preview = ex['user'][:250].replace('\n', ' ')
                            asst_preview = ex['assistant'][:250].replace('\n', ' ')
                            print(f"    │ 🧑 {user_preview}")
                            print(f"    │ 🤖 {asst_preview}")
                            print(f"    └{'─' * 42}")
                print()

        elif args.command == 'find':
            results = searcher.find(
                client=args.client, tag=args.tag, tool=args.tool,
                agent=args.agent, date=args.date, week=args.week,
                project=args.project,
                has_compaction=True if args.compacted else None,
                limit=args.limit,
            )
            if not results:
                print("No sessions match those filters.")
                return
            print(f"\n📋 {len(results)} sessions\n")
            for r in results:
                print(format_result(r))
                print()

        elif args.command == 'topics':
            # Support partial session ID
            sid = args.session_id
            if len(sid) < 36:
                row = searcher.conn.execute(
                    "SELECT session_id FROM sessions WHERE session_id LIKE ?", (f"{sid}%",)
                ).fetchone()
                if row:
                    sid = row['session_id']
                else:
                    print(f"No session found matching: {sid}")
                    return

            topics = searcher.topics(sid)
            if not topics:
                print(f"No topics recorded for session {sid[:8]}")
                return

            # Get session info
            session = searcher.conn.execute(
                "SELECT title_display, start_time, project_name FROM sessions WHERE session_id=?",
                (sid,)
            ).fetchone()

            if session:
                title = session['title_display'] or '(unnamed)'
                print(f"\n╭─── {title} {'─' * max(1, 44 - len(title))}")
                meta = []
                if session['start_time']:
                    meta.append(session['start_time'][:16])
                if session['project_name']:
                    meta.append(session['project_name'])
                print(f"│ {' · '.join(meta)}")
                print(f"╰{'─' * 48}")

            print(f"\n💬 Topic timeline ({len(topics)} entries)\n")
            for t in topics:
                ts = t['captured_at'][:16] if t['captured_at'] else ''
                ex = f" (exchange {t['exchange_number']})" if t['exchange_number'] else ''
                src = t['source']
                print(f"  [{src:20s}] {ts}{ex}")
                print(f"                       {t['topic']}")
                print()

        elif args.command == 'recent':
            results = searcher.recent(args.n)
            print(f"\n📋 Last {len(results)} sessions\n")
            for r in results:
                print(format_result(r))
                print()

        elif args.command == 'stats':
            stats = searcher.stats()
            print(f"\n📊 Database overview")
            print(f"{'═' * 40}")
            print(f"  Sessions:  {stats.get('total_sessions', 0)}")
            print(f"  Topics:    {stats.get('total_topics', 0)}")
            print(f"  Tools:     {stats.get('total_tools', 0)} distinct")
            print(f"  Agents:    {stats.get('total_agents', 0)} distinct")
            dr = stats.get('date_range', {})
            if dr.get('earliest'):
                print(f"  Range:     {dr['earliest']} → {dr['latest']}")
            if stats.get('by_project'):
                print(f"\n  📁 By project")
                print(f"  {'─' * 36}")
                for name, cnt in list(stats['by_project'].items())[:10]:
                    print(f"  {name:25s}  {cnt:>5d}")
            if stats.get('top_tools'):
                print(f"\n  🔧 Top tools")
                print(f"  {'─' * 36}")
                for name, cnt in list(stats['top_tools'].items())[:10]:
                    print(f"  {name:25s}  {cnt:>5d}")
            print()

        elif args.command == 'tools':
            results = searcher.tools_usage(args.tool_name)
            if args.tool_name:
                print(f"\n🔧 Sessions using '{args.tool_name}'\n")
                for r in results:
                    sid = r['session_id'][:8]
                    title = r.get('title_display') or r.get('title') or '(unnamed)'
                    print(f"  ◆ {sid} · {r['tool_name']} ×{r['use_count']}  {title}")
            else:
                print(f"\n🔧 Top tools across all sessions\n")
                for r in results:
                    print(f"  {r['tool_name']:25s}  {r['total']:>6d} uses  ({r['session_count']} sessions)")

    finally:
        searcher.close()


if __name__ == "__main__":
    main()
