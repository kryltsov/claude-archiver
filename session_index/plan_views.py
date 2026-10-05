"""Read side of the plan index: list, show, search and export plans.

Version numbers (v1, v2, ...) are a plan's versions in capture order, counted
over all its versions, so a date filter never renumbers them and an export's
<slug>/v<n>.md names stay stable between exports.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from . import scope as scope_mod
    from .plans import plan_title
    from .search import _escape_fts_query
except ImportError:
    import scope as scope_mod
    from plans import plan_title
    from search import _escape_fts_query


def has_plans(conn) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_versions'").fetchone())


def resolve_slug(conn, prefix: str) -> str:
    """Exact slug, or a unique prefix of one. LookupError otherwise."""
    if conn.execute("SELECT 1 FROM plans WHERE slug=?", (prefix,)).fetchone():
        return prefix
    rows = [r[0] for r in conn.execute(
        "SELECT slug FROM plans WHERE substr(slug, 1, ?) = ? ORDER BY slug",
        (len(prefix), prefix))]
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise LookupError(f"No plan matching {prefix!r}")
    raise LookupError(f"{prefix!r} matches {len(rows)} plans: " + ', '.join(rows[:10]))


def versions(conn, slug: str, scope: 'scope_mod.Scope' = None,
             content: bool = False) -> list[dict]:
    """A plan's versions, numbered over all of them, then filtered to scope's dates."""
    cols = "id, sha256, source, session_id, transcript_path, line_no, captured_at, " \
           "approved, length(content) AS size" + (", content" if content else "")
    rows = [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM plan_versions WHERE slug=? ORDER BY captured_at, id", (slug,))]
    for i, r in enumerate(rows, 1):
        r['n'] = i
    rng = scope.dates if scope else None
    if rng and rng.active:
        rows = [r for r in rows if r['captured_at']
                and (not rng.start or r['captured_at'] >= rng.start)
                and (not rng.end or r['captured_at'] < rng.end)]
    return rows


def plan_links(conn, slug: str) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT ps.session_id, ps.role, ps.first_line, ps.last_line,
               s.project_root, s.start_time, s.title_display
        FROM plan_sessions ps LEFT JOIN sessions s ON s.session_id = ps.session_id
        WHERE ps.slug = ?
        ORDER BY s.start_time
    """, (slug,))]


def list_plans(conn, scope: 'scope_mod.Scope' = None,
               include_empty: bool = False) -> list[dict]:
    """Plans in scope, most recently active first."""
    scope = scope or scope_mod.Scope()
    scope_mod.register(conn)
    pw, pp = scope.plan_where('p')
    vw, vp = scope.version_where('v')
    rows = conn.execute(f"""
        SELECT p.slug, p.title, p.path, p.on_disk,
               (SELECT COUNT(*) FROM plan_versions v WHERE v.slug = p.slug) AS versions,
               (SELECT COUNT(*) FROM plan_versions v WHERE v.slug = p.slug AND {vw}) AS in_range,
               (SELECT MAX(approved) FROM plan_versions v WHERE v.slug = p.slug) AS approved,
               COALESCE((SELECT MIN(captured_at) FROM plan_versions v WHERE v.slug = p.slug),
                        p.first_seen) AS first,
               COALESCE((SELECT MAX(captured_at) FROM plan_versions v WHERE v.slug = p.slug),
                        p.last_seen) AS last,
               (SELECT GROUP_CONCAT(session_id) FROM plan_sessions ps
                 WHERE ps.slug = p.slug) AS sessions
        FROM plans p
        WHERE {pw}
        ORDER BY last DESC
    """, (*vp, *pp)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if not include_empty and not d['versions']:
            continue
        d['sessions'] = sorted(set((d['sessions'] or '').split(','))) if d['sessions'] else []
        out.append(d)
    return out


def plans_for_session(conn, session_prefix: str) -> tuple[Optional[str], list[dict]]:
    """(full session id, its plans with role and version count)."""
    rows = [r[0] for r in conn.execute("""
        SELECT DISTINCT session_id FROM (
            SELECT session_id FROM sessions WHERE substr(session_id, 1, ?) = ?
            UNION SELECT session_id FROM plan_sessions WHERE substr(session_id, 1, ?) = ?)
    """, (len(session_prefix), session_prefix, len(session_prefix), session_prefix))]
    if len(rows) != 1:
        if len(rows) > 1:
            raise LookupError(f"{session_prefix!r} matches {len(rows)} sessions")
        return None, []
    sid = rows[0]
    plans = [dict(r) for r in conn.execute("""
        SELECT ps.slug, ps.role, ps.first_line, ps.last_line, p.title,
               (SELECT COUNT(*) FROM plan_versions v WHERE v.slug = ps.slug) AS versions,
               (SELECT COUNT(*) FROM plan_versions v WHERE v.slug = ps.slug
                  AND v.session_id = ps.session_id) AS from_this_session,
               (SELECT MAX(approved) FROM plan_versions v WHERE v.slug = ps.slug) AS approved
        FROM plan_sessions ps JOIN plans p ON p.slug = ps.slug
        WHERE ps.session_id = ?
        ORDER BY ps.first_line
    """, (sid,))]
    return sid, plans


def search_plans(conn, query: str, scope: 'scope_mod.Scope' = None,
                 limit: int = 20) -> list[dict]:
    """Plan hits ranked in plan_content (one FTS row per version), best version per plan.

    Only versions inside the date range count as matches.
    """
    if not has_plans(conn):
        return []
    scope = scope or scope_mod.Scope()
    scope_mod.register(conn)
    pw, pp = scope.plan_where('p')
    vw, vp = scope.version_where('v')
    rows = conn.execute(f"""
        SELECT v.id, v.slug, v.captured_at, bm25(plan_content) AS rank,
               snippet(plan_content, 2, '>>>', '<<<', '...', 30) AS snippet
        FROM plan_content
        JOIN plan_versions v ON v.id = plan_content.rowid
        JOIN plans p ON p.slug = v.slug
        WHERE plan_content MATCH ? AND {pw} AND {vw}
        ORDER BY rank
    """, (_escape_fts_query(query), *pp, *vp)).fetchall()

    best: dict[str, dict] = {}
    for r in rows:
        hit = best.get(r['slug'])
        if hit is None:
            best[r['slug']] = hit = {'slug': r['slug'], 'rank': r['rank'],
                                     'snippet': r['snippet'], 'best_version_id': r['id'],
                                     'matching_versions': 0}
        hit['matching_versions'] += 1
    hits = sorted(best.values(), key=lambda h: h['rank'])[:limit]
    for h in hits:
        p = conn.execute("""
            SELECT p.title, p.on_disk,
                   (SELECT COUNT(*) FROM plan_versions v WHERE v.slug = p.slug) AS versions,
                   (SELECT MAX(approved) FROM plan_versions v WHERE v.slug = p.slug) AS approved,
                   (SELECT MAX(captured_at) FROM plan_versions v WHERE v.slug = p.slug) AS last
            FROM plans p WHERE p.slug = ?
        """, (h['slug'],)).fetchone()
        h.update(dict(p))
        h['sessions'] = [r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM plan_sessions WHERE slug=? ORDER BY session_id",
            (h['slug'],))]
    return hits


_FTS_WORDS = {'AND', 'OR', 'NOT', 'NEAR'}


def query_terms(query: str) -> list[str]:
    """Plain lower-case words from an FTS-ish query (operators and quotes dropped)."""
    words = re.findall(r"[\w][\w.-]*", query)
    return [w.lower() for w in words if w not in _FTS_WORDS and len(w) > 1]


def excerpts(text: str, query: str, max_hits: int = 3, context: int = 1) -> list[str]:
    """Up to max_hits line windows of text around lines that mention a query term."""
    terms = query_terms(query)
    if not terms:
        return []
    lines = text.splitlines()
    windows: list[list[int]] = []  # [lo, hi) line ranges; touching ones are merged
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(t in low for t in terms):
            continue
        lo, hi = max(0, i - context), min(len(lines), i + context + 1)
        if windows and lo <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], hi)
        elif len(windows) < max_hits:
            windows.append([lo, hi])
        else:
            break
    return ['\n'.join(lines[lo:hi]).strip('\n') for lo, hi in windows]


def context_for_hit(conn, hit: dict, query: str, scope: 'scope_mod.Scope' = None) -> dict:
    """Excerpts from the latest in-range version; else from the best-matching one."""
    vs = versions(conn, hit['slug'], scope, content=True)
    if not vs:
        return {'n': None, 'excerpts': [], 'latest': False}
    latest = vs[-1]
    ex = excerpts(latest['content'], query)
    if ex:
        return {'n': latest['n'], 'excerpts': ex, 'latest': True}
    best = next((v for v in vs if v['id'] == hit['best_version_id']), latest)
    return {'n': best['n'], 'excerpts': excerpts(best['content'], query), 'latest': False}


def _epoch(captured_at: Optional[str]) -> Optional[float]:
    if not captured_at:
        return None
    try:
        return datetime.fromisoformat(captured_at.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def version_frontmatter(plan: dict, v: dict) -> str:
    """YAML frontmatter so a plain markdown reader sees the provenance too.

    Without it, approval, session and citation live only in index.json, which a
    tool that reads markdown never opens.

    Only facts about *this* version go in, so a version file never changes once
    written. The plan's current title and version count live in index.json: they
    move as the plan grows, and would otherwise rewrite every file each export.
    """
    approved = {1: 'approved', 0: 'rejected'}.get(v['approved'], 'unknown')
    cite = f"{v['transcript_path']}:{v['line_no']}" if v.get('line_no') else (
        v.get('transcript_path') or '')
    fields = [
        ('plan', plan['slug']),
        ('title', plan_title(v['content']) or plan.get('title') or ''),
        ('version', v['n']),
        ('captured_at', v['captured_at']),
        ('captured_by', v['source']),          # write | edit | exit_plan_mode | disk
        ('approval', approved),                # approved | rejected | unknown
        ('session_id', v.get('session_id') or ''),
        ('cite', cite),
        ('sha256', v['sha256']),
        ('exported_by', 'archiver plans export'),
    ]
    lines = ['---']
    for key, value in fields:
        rendered = value if isinstance(value, int) else json.dumps(str(value), ensure_ascii=False)
        lines.append(f"{key}: {rendered}")
    lines.append('---')
    return '\n'.join(lines) + '\n\n'


def export_plans(conn, out_dir: Path, scope: 'scope_mod.Scope' = None,
                 force: bool = False, plain: bool = False, check: bool = False) -> dict:
    """Write <out>/<slug>/v<n>.md for in-scope versions, plus <out>/index.json.

    Each file carries frontmatter (unless plain) and is stamped with the version's
    capture time, not the export time: a consumer whose watermark is a modification
    time then sees only versions captured since it last looked. Files whose text is
    already correct are left alone.

    check=True writes nothing and reports what an export would do — for a script that
    refreshes the folder before reading it. `current` in the result says whether the
    folder is up to date. Stale files (left by a narrower scope, or by a plan leaving
    it) are reported in both modes and never deleted.
    """
    out_dir = Path(out_dir)
    if not check:
        if out_dir.exists() and any(out_dir.iterdir()) and not force:
            raise FileExistsError(f"{out_dir} is not empty (use --force to write into it)")
        out_dir.mkdir(parents=True, exist_ok=True)
    scope = scope or scope_mod.Scope()
    expected: set[Path] = set()

    index = {
        'exported_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'scope': {'groups': scope.groups, 'folders': scope.folders, 'project': scope.project,
                  'from': scope.dates.start, 'to_exclusive': scope.dates.end,
                  'label': scope.label() or 'everything'},
        'plans': [],
    }
    written = unchanged = 0
    for p in list_plans(conn, scope):
        vs = versions(conn, p['slug'], scope, content=True)
        if not vs:
            continue
        folder = out_dir / p['slug']
        if not check:
            folder.mkdir(exist_ok=True)
        entries = []
        for v in vs:
            name = f"v{v['n']}.md"
            target = folder / name
            expected.add(target)
            text = v['content'] if plain else version_frontmatter(p, v) + v['content']
            current = None
            if target.exists():
                try:
                    current = target.read_text(encoding='utf-8')
                except OSError:
                    current = None
            if current == text:
                unchanged += 1
            else:
                written += 1
                if not check:
                    target.write_text(text, encoding='utf-8', newline='\n')
            if not check:
                stamp = _epoch(v['captured_at'])
                if stamp is not None:
                    os.utime(target, (stamp, stamp))
            entries.append({k: v[k] for k in ('n', 'sha256', 'source', 'captured_at', 'approved',
                                               'session_id', 'transcript_path', 'line_no')}
                           | {'file': f"{p['slug']}/{name}"})
        index['plans'].append({
            'slug': p['slug'], 'title': p['title'], 'path': p['path'],
            'on_disk': bool(p['on_disk']), 'versions_total': p['versions'],
            'approved': p['approved'],
            'sessions': [{k: l[k] for k in ('session_id', 'role', 'project_root', 'start_time')}
                         for l in plan_links(conn, p['slug'])],
            'versions': entries,
        })
    index['frontmatter'] = not plain
    stale = sorted(str(p.relative_to(out_dir)) for p in out_dir.glob('*/v*.md')
                   if p not in expected) if out_dir.exists() else []
    index_missing = not (out_dir / 'index.json').exists()
    if not check:
        (out_dir / 'index.json').write_text(json.dumps(index, indent=2, ensure_ascii=False) + '\n',
                                            encoding='utf-8', newline='\n')
    return {'dir': str(out_dir), 'plans': len(index['plans']),
            'files': written + unchanged, 'written': written, 'unchanged': unchanged,
            'stale': stale, 'check': check,
            'current': not written and not stale and (not index_missing if check else True)}
