#!/usr/bin/env python3
"""
Unified CLI for claude-session-index.

Plain text defaults to search — no subcommands needed for the common case.

Usage:
    sessions "webhook debugging"                # search (default)
    sessions "webhook" --context                # search with conversation excerpts
    sessions context a5b111c6 "failure"         # conversation around matches
    sessions context a5b111c6                   # all exchanges
    sessions analytics --week                   # this week's stats
    sessions analytics --client "Acme"          # per-client stats
    sessions synthesize "form automation"       # cross-session synthesis
    sessions recent                             # last 10 sessions
    sessions recent 20                          # last 20 sessions
    sessions find --tool Task --week            # filter sessions
    sessions tools                              # top tools across sessions
    sessions tools "Bash"                       # sessions using specific tool
    sessions topics <session_id>                # topic timeline
    sessions stats                              # database overview
    sessions index                              # re-index new/modified
    sessions index --backfill                   # re-index everything
    sessions init --from-upstream [--force]     # one-time copy of upstream's DB
    sessions groups                             # project groups, per-folder counts
    sessions "query" --group myapp --from 2026-09-01   # scoped search
    sessions "query" --in plans                 # search plans (sessions | plans | all)
    sessions list --group myapp --json          # sessions + transcript paths + folders
    sessions plans [--group G] [--from D]       # plans in scope
    sessions plans export DIR [--group G]       # <slug>/v<n>.md + index.json
    sessions plan <slug> [--versions | --approved | --v N] [--raw]
    sessions plans-for <session_id>             # plans a session wrote or touched
    sessions hooks status                       # are the Claude Code hooks healthy?
    sessions hooks install [--apply]            # diff, then write ~/.claude/settings.json
    sessions hooks uninstall [--apply]          # remove only this tool's hook entries

Scope flags on search / find / recent / analytics / list / plans:
    --group NAME (repeatable, union)  --project X  --from YYYY-MM-DD  --to YYYY-MM-DD
Search defaults to --in all when --group or --project is given, else sessions only.

The command name comes from app.CLI_NAME ("archiver" in this install).
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timedelta

try:
    from . import app, config
    from . import scope as scope_mod
    from . import plan_views
    from .search import SessionSearch, format_result
    from .analyzer import (
        get_context, format_context,
        analytics, format_analytics,
        synthesize, format_synthesis,
    )
except ImportError:
    import app, config
    import scope as scope_mod
    import plan_views
    from search import SessionSearch, format_result
    from analyzer import (
        get_context, format_context,
        analytics, format_analytics,
        synthesize, format_synthesis,
    )


SUBCOMMANDS = {
    'context', 'analytics', 'synthesize', 'recent', 'find',
    'tools', 'topics', 'stats', 'index', 'search', 'init', 'groups',
    'list', 'plans', 'plan', 'plans-for', 'hooks',
}

APPROVAL = {1: '✓ approved', 0: '✗ never approved', None: '– not submitted'}
VERSION_APPROVAL = {1: 'approved', 0: 'rejected', None: '—'}


def _n(count: int, noun: str) -> str:
    """'1 version', '3 versions'."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _short(ts: str, n: int = 16) -> str:
    return ts[:n].replace('T', ' ') if ts else '—'


def format_plan_hit(h: dict) -> str:
    lines = [f"  ◇ {h['slug']} · {h.get('title') or '(untitled)'}"]
    sess = ', '.join(x[:8] for x in h.get('sessions') or []) or 'no linked session'
    match = f" ({h['matching_versions']} matching)" if h.get('matching_versions') else ''
    lines.append(f"    {_n(h['versions'], 'version')}{match} · {APPROVAL.get(h.get('approved'))} · "
                 f"last {_short(h.get('last'), 10)} · sessions {sess}")
    if h.get('snippet'):
        snip = h['snippet'].replace('>>>', '').replace('<<<', '').replace('\n', ' ')[:140]
        lines.append(f'    "{snip}"')
    lines.append(f"    → {app.CLI_NAME} plan {h['slug']}")
    return '\n'.join(lines)


def format_plan_row(p: dict, dated: bool) -> str:
    in_range = f" ({p['in_range']} in range)" if dated else ''
    gone = '' if p['on_disk'] else ' · file gone'
    n_sess = len(p['sessions'])
    return (f"  ◇ {p['slug']}\n"
            f"    {p.get('title') or '(untitled)'}\n"
            f"    {_n(p['versions'], 'version')}{in_range} · {APPROVAL.get(p['approved'])} · "
            f"{_short(p['first'], 10)} → {_short(p['last'], 10)} · "
            f"{_n(n_sess, 'session')}{gone}")


def add_scope_args(sp, project: bool = True):
    """--group / --from / --to (and --project where the command lacks it)."""
    sp.add_argument('--group', action='append', metavar='NAME',
                    help='Project group from config (repeatable; union)')
    if project:
        sp.add_argument('--project', help='Filter by project (substring match)')
    sp.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD',
                    help='Sessions overlapping this day or later (local time)')
    sp.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD',
                    help='Sessions overlapping this day or earlier (local time)')


def scope_from_args(args) -> 'scope_mod.Scope':
    return scope_mod.build(
        groups=getattr(args, 'group', None), project=getattr(args, 'project', None),
        date_from=getattr(args, 'date_from', None), date_to=getattr(args, 'date_to', None),
        on=getattr(args, 'date', None), week=getattr(args, 'week', False),
        days=getattr(args, 'days', None), month=getattr(args, 'month', False),
    )


def scope_suffix(scope) -> str:
    label = scope.label()
    return f" · {label}" if label else ""


def show_plan(conn, args, scope):
    """archiver plan <slug>: header + one version, or the version list."""
    try:
        slug = plan_views.resolve_slug(conn, args.slug)
    except LookupError as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    everything = plan_views.versions(conn, slug, content=True)
    in_range = plan_views.versions(conn, slug, scope, content=True)
    plan = conn.execute("SELECT * FROM plans WHERE slug=?", (slug,)).fetchone()

    if args.version is not None:
        chosen = next((v for v in everything if v['n'] == args.version), None)
        if not chosen:
            print(f"{slug} has no v{args.version} (it has {len(everything)} versions)",
                  file=sys.stderr)
            sys.exit(1)
    elif args.approved:
        chosen = next((v for v in reversed(in_range) if v['approved'] == 1), None)
        if not chosen:
            print(f"No approved version of {slug}{scope_suffix(scope)}", file=sys.stderr)
            sys.exit(1)
    else:
        chosen = in_range[-1] if in_range else None

    if args.raw and not args.versions:
        if not chosen:
            sys.exit(1)
        sys.stdout.write(chosen['content'])
        return

    title = plan['title'] or '(untitled)'
    print(f"\n╭─── {title} {'─' * max(1, 44 - len(title))}")
    where = plan['path'] if plan['on_disk'] else 'file no longer on disk'
    approved = max((v['approved'] for v in everything if v['approved'] is not None), default=None)
    print(f"│ {slug} · {_n(len(everything), 'version')} · {APPROVAL.get(approved)}")
    print(f"│ {where}")
    for l in plan_views.plan_links(conn, slug):
        print(f"│ session {l['session_id'][:8]} ({l['role']}) · {l['project_root'] or '—'} · "
              f"{_short(l['start_time'], 10)}")
    print(f"╰{'─' * 48}")

    if args.versions:
        print(f"\n  {len(in_range)} of {len(everything)} versions{scope_suffix(scope)}\n")
        for v in in_range:
            cite = (f"{Path(v['transcript_path']).name}:{v['line_no']}" if v['line_no']
                    else ('plan file' if v['source'] == 'disk' else 'hook capture'))
            sid = (v['session_id'] or '')[:8] or '—'
            print(f"  v{v['n']:<3} {_short(v['captured_at'])}  {v['source']:14s} "
                  f"{VERSION_APPROVAL.get(v['approved']):9s} {sid:8s} {v['size']:>6d}B  {cite}")
        print()
        return
    if not chosen:
        print(f"\n  No version of {slug}{scope_suffix(scope)}")
        return
    sid = (chosen['session_id'] or '')[:8] or '—'
    cite = (f" · {chosen['transcript_path']}:{chosen['line_no']}" if chosen['line_no'] else '')
    print(f"\n── v{chosen['n']}/{len(everything)} · {_short(chosen['captured_at'])} · "
          f"{chosen['source']} · {VERSION_APPROVAL.get(chosen['approved'])} · session {sid}{cite}\n")
    print(chosen['content'])


def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog=app.CLI_NAME,
        description='Search and analyze your Claude Code sessions',
        usage=f'{app.CLI_NAME} "query" | {app.CLI_NAME} <command> [options]',
    )
    parser.add_argument('--db-path', type=str, default=None,
                        help='Path to sessions.db (overrides config)')

    subparsers = parser.add_subparsers(dest='command')

    # search (also the default when no subcommand given)
    sp = subparsers.add_parser('search', help='Full-text search')
    sp.add_argument('query', help='Search query')
    sp.add_argument('-n', '--limit', type=int, default=20)
    sp.add_argument('--context', action='store_true',
                    help='Show conversation exchanges inline')
    add_scope_args(sp)
    sp.add_argument('--in', dest='in_', choices=['sessions', 'plans', 'all'],
                    help='What to search (default: all with --group/--project, else sessions)')

    # context
    sp = subparsers.add_parser('context', help='Conversation context for a session')
    sp.add_argument('session_id', help='Session ID (full or prefix)')
    sp.add_argument('query', nargs='?', default=None, help='Filter to matching exchanges')
    sp.add_argument('-n', '--limit', type=int, default=10, help='Max exchanges')

    # analytics
    sp = subparsers.add_parser('analytics', help='Session analytics')
    sp.add_argument('--client', help='Filter by client')
    sp.add_argument('--week', action='store_true', help='Last 7 days (shortcut)')
    sp.add_argument('--month', action='store_true', help='Last 30 days (shortcut)')
    add_scope_args(sp)

    # synthesize
    sp = subparsers.add_parser('synthesize', help='Cross-session synthesis')
    sp.add_argument('query', help='Topic to synthesize across sessions')
    sp.add_argument('--limit', type=int, default=10, help='Max sessions to analyze')

    # recent
    sp = subparsers.add_parser('recent', help='Recent sessions')
    sp.add_argument('n', nargs='?', type=int, default=10)
    add_scope_args(sp)

    # find
    sp = subparsers.add_parser('find', help='Filter sessions')
    sp.add_argument('--client', help='Filter by client')
    sp.add_argument('--tag', help='Filter by tag')
    sp.add_argument('--tool', help='Filter by tool used')
    sp.add_argument('--agent', help='Filter by agent used')
    sp.add_argument('--date', help='One day, local time (shortcut for --from D --to D)')
    sp.add_argument('--week', action='store_true', help='Last 7 days (shortcut)')
    sp.add_argument('--days', type=int, help='Last N days (shortcut)')
    add_scope_args(sp)
    sp.add_argument('--exclude-project', help='Exclude a project from results')
    sp.add_argument('--compacted', action='store_true', help='Only compacted sessions')
    sp.add_argument('-n', '--limit', type=int, default=20)

    # tools
    sp = subparsers.add_parser('tools', help='Tool usage')
    sp.add_argument('tool_name', nargs='?', help='Specific tool')

    # topics
    sp = subparsers.add_parser('topics', help='Topic timeline for a session')
    sp.add_argument('session_id', help='Session ID (full or prefix)')

    # stats
    subparsers.add_parser('stats', help='Database overview')

    # groups
    subparsers.add_parser('groups', help='Project groups: folders, sessions, plans, activity')

    # list
    sp = subparsers.add_parser('list', help='Sessions in scope with transcript paths and folders')
    add_scope_args(sp)
    sp.add_argument('--json', action='store_true', help='Machine-readable output')
    sp.add_argument('-n', '--limit', type=int, default=None)

    # plans
    sp = subparsers.add_parser('plans', help='List plans in scope, or: plans export DIR')
    sp.add_argument('action', nargs='?', choices=['export'], help='export: write plans to DIR')
    sp.add_argument('dir', nargs='?', help='Export directory')
    add_scope_args(sp)
    sp.add_argument('--all', action='store_true', help='Include plans with no stored text')
    sp.add_argument('--json', action='store_true', help='Machine-readable output')
    sp.add_argument('--force', action='store_true', help='export: write into a non-empty DIR')
    sp.add_argument('--plain', action='store_true',
                    help='export: plan text only, without the provenance frontmatter')
    sp.add_argument('--check', action='store_true',
                    help='export: write nothing; exit 1 if the export is out of date')

    # plan
    sp = subparsers.add_parser('plan', help='Show one plan (latest version by default)')
    sp.add_argument('slug', help='Plan slug (or a unique prefix)')
    g = sp.add_mutually_exclusive_group()
    g.add_argument('--versions', action='store_true', help='List versions instead')
    g.add_argument('--approved', action='store_true', help='Latest approved version')
    g.add_argument('--v', type=int, dest='version', metavar='N', help='Version N')
    sp.add_argument('--raw', action='store_true', help='Print only the plan text')
    sp.add_argument('--from', dest='date_from', metavar='YYYY-MM-DD',
                    help='Only versions captured on/after this day')
    sp.add_argument('--to', dest='date_to', metavar='YYYY-MM-DD',
                    help='Only versions captured on/before this day')

    # plans-for
    sp = subparsers.add_parser('plans-for', help='Plans a session wrote or touched')
    sp.add_argument('session_id', help='Session ID (full or prefix)')

    # hooks
    sp = subparsers.add_parser('hooks', help='Install / check the Claude Code hook entries')
    sp.add_argument('action', choices=['install', 'uninstall', 'status'])
    sp.add_argument('--apply', action='store_true',
                    help='Write the change (default: show the diff only)')
    sp.add_argument('--settings', metavar='PATH',
                    help='settings.json to use (default: ~/.claude/settings.json)')

    # index
    sp = subparsers.add_parser('index', help='Index sessions')
    sp.add_argument('--backfill', action='store_true', help='Re-index everything')
    sp.add_argument('--session', metavar='ID', help='Index a single session')

    # init
    sp = subparsers.add_parser('init', help='Initialise the DB')
    sp.add_argument('--from-upstream', action='store_true', required=True,
                    help="Copy upstream's sessions.db (read-only, backup API)")
    sp.add_argument('--force', action='store_true', help='Replace an existing DB')

    # --- Default to search if first arg isn't a subcommand ---
    # Intercept before argparse: if the first real arg isn't a known
    # subcommand or flag, treat the whole thing as a search query.
    raw_args = sys.argv[1:]
    if raw_args and raw_args[0] not in SUBCOMMANDS and not raw_args[0].startswith('-'):
        # Bare text = search. Rebuild as: search "the query" [flags]
        # Once we hit a flag (starts with -), everything after is flags/values.
        query_parts = []
        flags = []
        in_flags = False
        for arg in raw_args:
            if arg.startswith('-'):
                in_flags = True
            if in_flags:
                flags.append(arg)
            else:
                query_parts.append(arg)
        sys.argv = [sys.argv[0], 'search', ' '.join(query_parts)] + flags

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # Resolve paths
    db_path = Path(args.db_path) if args.db_path else config.get_db_path()

    if args.command == 'hooks':
        try:
            from .hooks import hooks_command
        except ImportError:
            from hooks import hooks_command
        sys.exit(hooks_command(args.action, apply=args.apply, settings=args.settings))

    if args.command == 'init':
        try:
            from .upstream import copy_from_upstream
        except ImportError:
            from upstream import copy_from_upstream
        src = config.get_upstream_db()
        if src is None:
            print("upstream_db is not configured", file=sys.stderr)
            sys.exit(1)
        try:
            r = copy_from_upstream(src, db_path, force=args.force)
        except (FileNotFoundError, FileExistsError, ValueError) as e:
            print(e, file=sys.stderr)
            sys.exit(1)
        print(f"Copied {r['sessions']} sessions: {r['src']} → {r['dest']}")
        return

    config.ensure_indexed(db_path)

    try:
        scope = scope_from_args(args)
    except scope_mod.ScopeError as e:
        print(f"{app.CLI_NAME}: {e}", file=sys.stderr)
        sys.exit(2)

    # --- Dispatch ---

    if args.command == 'search':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
            in_ = args.in_ or ('all' if (scope.groups or scope.project) else 'sessions')
            want_sessions, want_plans = in_ in ('sessions', 'all'), in_ in ('plans', 'all')
            results = searcher.search(args.query, args.limit, scope=scope) if want_sessions else []
            plan_hits = plan_views.search_plans(searcher.conn, args.query, scope, args.limit) \
                if want_plans else []
            if not results and not plan_hits:
                print(f"No results for: {args.query}{scope_suffix(scope)}")
                return
            if want_sessions:
                noun = 'results' if in_ == 'sessions' else 'sessions'
                print(f"\n🔍 {len(results)} {noun} for \"{args.query}\"{scope_suffix(scope)}\n")
            for r in results:
                print(format_result(r))
                if args.context:
                    ctx = get_context(r['session_id'], query=args.query, limit=3, db_path=db_path)
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
            if want_plans:
                print(f"\n📝 {_n(len(plan_hits), 'plan')} for \"{args.query}\"{scope_suffix(scope)}\n")
                for h in plan_hits:
                    print(format_plan_hit(h))
                    if args.context:
                        ctx = plan_views.context_for_hit(searcher.conn, h, args.query, scope)
                        where = 'latest version' if ctx['latest'] else 'best match'
                        for ex in ctx['excerpts']:
                            print(f"    ┌─ v{ctx['n']} ({where}) {'─' * 20}")
                            for line in ex.splitlines():
                                print(f"    │ {line[:160]}")
                            print(f"    └{'─' * 42}")
                    print()
        finally:
            searcher.close()

    elif args.command == 'context':
        result = get_context(args.session_id, query=args.query,
                             limit=args.limit, db_path=db_path)
        print(format_context(result))

    elif args.command == 'analytics':
        result = analytics(client=args.client, db_path=db_path, scope=scope)
        print(format_analytics(result))

    elif args.command == 'synthesize':
        result = synthesize(args.query, limit=args.limit, db_path=db_path)
        print(format_synthesis(result))

    elif args.command == 'recent':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
            results = searcher.recent(args.n, scope=scope)
            print(f"\n📋 Last {len(results)} sessions{scope_suffix(scope)}\n")
            for r in results:
                print(format_result(r))
                print()
        finally:
            searcher.close()

    elif args.command == 'find':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
            results = searcher.find(
                client=args.client, tag=args.tag, tool=args.tool,
                agent=args.agent,
                exclude_project=getattr(args, 'exclude_project', None),
                has_compaction=True if args.compacted else None,
                limit=args.limit, scope=scope,
            )
            if not results:
                print(f"No sessions match those filters{scope_suffix(scope)}.")
                return
            print(f"\n📋 {len(results)} sessions{scope_suffix(scope)}\n")
            for r in results:
                print(format_result(r))
                print()
        finally:
            searcher.close()

    elif args.command == 'tools':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
            tool_name = getattr(args, 'tool_name', None)
            results = searcher.tools_usage(tool_name)
            if tool_name:
                print(f"\n🔧 Sessions using '{tool_name}'\n")
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

    elif args.command == 'topics':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
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
        finally:
            searcher.close()

    elif args.command == 'stats':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
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
        finally:
            searcher.close()

    elif args.command == 'groups':
        import sqlite3
        groups = config.get_project_groups()
        if not groups:
            print(f"No project groups configured. Add them to {config.CONFIG_FILE}:")
            print('  "project_groups": {"myproduct": ["E:/work/app", "E:/work/app-api"]}')
            return
        conn = sqlite3.connect(str(db_path))
        try:
            report = scope_mod.group_report(conn, groups)
        finally:
            conn.close()

        def day(ts):
            return ts[:10] if ts else '—'

        print(f"\n🗂  {len(report)} project groups  ({config.CONFIG_FILE})")
        for g in report:
            print(f"\n  {g['name']}  ·  {g['sessions']} sessions · {g['plans']} plans · "
                  f"{day(g['first'])} → {day(g['last'])}")
            for m in g['members']:
                flag = ''
                if not m['exists']:
                    flag = '   ⚠ folder not found (typo?)'
                elif m['no_match']:
                    flag = '   · no sessions yet'
                print(f"    {m['folder']:36s} {m['sessions']:>4d} sessions {m['plans']:>4d} plans  "
                      f"{day(m['first'])} → {day(m['last'])}{flag}")
        print()

    elif args.command == 'list':
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        try:
            rows = searcher.list_sessions(scope, limit=args.limit)
        finally:
            searcher.close()
        if args.json:
            print(json.dumps(rows, indent=2, ensure_ascii=False))
            return
        print(f"\n📋 {len(rows)} sessions{scope_suffix(scope)}\n")
        for r in rows:
            print(f"  {_short(r['start_time'])}  {r['session_id'][:8]}  "
                  f"{(r['project_root'] or '—'):32s} {(r['git_branch'] or ''):14s} "
                  f"{(r['title'] or '')[:50]}")
        print()

    elif args.command in ('plans', 'plan', 'plans-for'):
        searcher = SessionSearch(db_path=db_path)
        searcher.connect()
        conn = searcher.conn
        try:
            if not plan_views.has_plans(conn):
                print(f"No plan index in {db_path}. Run: {app.CLI_NAME} index")
                sys.exit(1)
            if args.command == 'plans' and args.action == 'export':
                if not args.dir:
                    print(f"usage: {app.CLI_NAME} plans export DIR [--group G] [--from D] [--to D]",
                          file=sys.stderr)
                    sys.exit(2)
                try:
                    r = plan_views.export_plans(conn, Path(args.dir), scope,
                                                force=args.force, plain=args.plain,
                                                check=args.check)
                except FileExistsError as e:
                    print(e, file=sys.stderr)
                    sys.exit(1)
                if args.check:
                    if r['current']:
                        print(f"Up to date: {r['plans']} plans, {_n(r['files'], 'version file')}"
                              f" in {r['dir']}{scope_suffix(scope)}")
                        return
                    parts = []
                    if r['written']:
                        parts.append(f"{_n(r['written'], 'version file')} to write")
                    if r['stale']:
                        parts.append(f"{_n(len(r['stale']), 'stale file')}: "
                                     f"{', '.join(r['stale'][:5])}")
                    if not parts:
                        parts.append('no index.json')
                    print(f"Out of date ({'; '.join(parts)}): {args.dir}{scope_suffix(scope)}",
                          file=sys.stderr)
                    sys.exit(1)
                stale = f", {_n(len(r['stale']), 'stale file')} left in place" if r['stale'] else ''
                print(f"Exported {r['plans']} plans, {r['files']} version files "
                      f"({r['written']} written, {r['unchanged']} already current){stale} "
                      f"→ {r['dir']}{scope_suffix(scope)}")
            elif args.command == 'plans':
                rows = plan_views.list_plans(conn, scope, include_empty=args.all)
                if args.json:
                    print(json.dumps(rows, indent=2, ensure_ascii=False))
                    return
                print(f"\n📝 {_n(len(rows), 'plan')}{scope_suffix(scope)}\n")
                for p in rows:
                    print(format_plan_row(p, scope.dates.active))
                    print()
            elif args.command == 'plans-for':
                try:
                    sid, rows = plan_views.plans_for_session(conn, args.session_id)
                except LookupError as e:
                    print(e, file=sys.stderr)
                    sys.exit(1)
                if not sid:
                    print(f"No session found matching: {args.session_id}")
                    return
                print(f"\n📝 {_n(len(rows), 'plan')} linked to session {sid[:8]}\n")
                for p in rows:
                    print(f"  ◇ {p['slug']} · {p['title'] or '(untitled)'}")
                    lines = f"lines {p['first_line']}–{p['last_line']}" if p['first_line'] else ''
                    print(f"    {p['role']} · {p['from_this_session']} of {_n(p['versions'], 'version')} "
                          f"from this session · {APPROVAL.get(p['approved'])}"
                          f"{' · ' + lines if lines else ''}")
                    print(f"    → {app.CLI_NAME} plan {p['slug']}\n")
            else:
                show_plan(conn, args, scope)
        finally:
            searcher.close()

    elif args.command == 'index':
        try:
            from session_index.indexer import SessionIndexer, format_plan_stats
        except ImportError:
            try:
                from .indexer import SessionIndexer, format_plan_stats
            except ImportError:
                from indexer import SessionIndexer, format_plan_stats

        indexer = SessionIndexer(db_path=db_path)
        indexer.connect()
        try:
            if args.backfill:
                indexer.backfill_all()
            elif args.session:
                if indexer.index_session(session_id=args.session):
                    print(f"Indexed: {args.session}")
                else:
                    print(f"Failed to index: {args.session}", file=sys.stderr)
                    sys.exit(1)
            else:
                stats = indexer.index_incremental()
                print(f"Incremental: {stats['indexed']} new/updated, "
                      f"{stats['unchanged']} unchanged, {stats['errors']} errors")
                print(format_plan_stats(stats['plans']))
                print(f"Upstream topics imported: {stats['topics_imported']}")
        finally:
            indexer.close()


if __name__ == "__main__":
    main()
