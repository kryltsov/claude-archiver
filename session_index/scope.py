"""Scope: which sessions a command looks at. Project groups + date ranges.

Every command filters through here, so they all agree on what "in scope" means.

Groups (config "project_groups": {name: [folder, ...]}):
  a session matches a member folder when its project_root, or any cwd it used
  (session_cwds), is that folder or sits under it. Paths compare after
  folders.normalize_path, so '\\' vs '/' and drive-letter case don't matter.
  A member that is a repo's main worktree also matches every git worktree of that
  repo, wherever it lives: sessions whose git_common_dir is '<member>/.git'. A
  linked worktree or a subfolder listed as a member matches by path only, which is
  how to narrow a group to one worktree. No git runs at query time, so a worktree
  deleted after its sessions were indexed still matches.
  Several --group flags are a union; --project (upstream's LIKE match) then
  narrows inside it.

Dates (--from / --to YYYY-MM-DD, local time, both inclusive, either optional):
  a session matches when it overlaps the range:
      start_time < end_of(to)  AND  COALESCE(end_time, start_time) >= start_of(from)
  Transcript timestamps are UTC ('...Z'), so bounds are converted to UTC first.
  The shortcuts --date D / --week / --days N / --month map onto the same range;
  mixing a shortcut with --from/--to is an error.
"""

import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Optional

try:
    from .folders import normalize_path
except ImportError:
    from folders import normalize_path


class ScopeError(ValueError):
    """Bad --group / --from / --to input; the message is meant for the user."""


def _utc_iso(dt: datetime) -> str:
    """Local-aware datetime -> transcript timestamp shape: 2026-09-17T11:38:21.918Z."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _parse_day(value: str, flag: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ScopeError(f"{flag} expects YYYY-MM-DD, got {value!r}")


def _local_midnight(d: date) -> datetime:
    return datetime.combine(d, time.min).astimezone()  # local tz, DST-correct


@dataclass
class DateRange:
    start: Optional[str] = None   # inclusive, UTC ISO
    end: Optional[str] = None     # exclusive, UTC ISO
    label: str = 'all time'

    @property
    def active(self) -> bool:
        return bool(self.start or self.end)


def date_range(date_from: str = None, date_to: str = None, *, on: str = None,
               week: bool = False, days: int = None, month: bool = False,
               now: datetime = None) -> DateRange:
    """Build the range from --from/--to or from the legacy shortcuts."""
    shortcuts = [n for n, v in (('--date', on), ('--week', week), ('--days', days),
                                ('--month', month)) if v]
    if shortcuts and (date_from or date_to):
        raise ScopeError(f"{', '.join(shortcuts)} can't be combined with --from/--to")

    now = (now or datetime.now()).astimezone()
    starts, ends, labels = [], [], []

    if date_from or date_to:
        d_from = _parse_day(date_from, '--from') if date_from else None
        d_to = _parse_day(date_to, '--to') if date_to else None
        if d_from and d_to and d_from > d_to:
            raise ScopeError(f"--from {d_from} is after --to {d_to}")
        if d_from:
            starts.append(_utc_iso(_local_midnight(d_from)))
        if d_to:
            ends.append(_utc_iso(_local_midnight(d_to + timedelta(days=1))))
        labels.append(f"{d_from or '…'} → {d_to or '…'}")
    # Shortcuts intersect, as upstream's AND-ed filters did.
    if on:
        d = _parse_day(on, '--date')
        starts.append(_utc_iso(_local_midnight(d)))
        ends.append(_utc_iso(_local_midnight(d + timedelta(days=1))))
        labels.append(str(d))
    if week:
        starts.append(_utc_iso(now - timedelta(days=7)))
        labels.append('last 7 days')
    if days:
        if days < 1:
            raise ScopeError(f"--days expects a positive number, got {days}")
        starts.append(_utc_iso(now - timedelta(days=days)))
        labels.append(f'last {days} days')
    if month:
        starts.append(_utc_iso(now - timedelta(days=30)))
        labels.append('last 30 days')

    if not starts and not ends:
        return DateRange()
    return DateRange(start=max(starts) if starts else None,
                     end=min(ends) if ends else None,
                     label=' ∩ '.join(labels))


def resolve_groups(names: Iterable[str], groups: dict) -> list[str]:
    """Group names -> normalised member folders (union, de-duplicated, in order)."""
    folders: list[str] = []
    for name in names or []:
        if name not in groups:
            known = ', '.join(sorted(groups)) or '(none configured)'
            raise ScopeError(f"Unknown group {name!r}. Known groups: {known}")
        for member in groups[name] or []:
            n = normalize_path(member)
            if n and n not in folders:
                folders.append(n)
    return folders


def register(conn):
    """Make norm_path() available in SQL on this connection (idempotent).

    SQLite refuses to create a function while the connection has work in flight, so
    a caller that registers mid-transaction would blow up on a function it already
    has. Re-registering is skipped, and a failure is only raised if the function
    turns out to be unusable.
    """
    if getattr(conn, '_norm_path_registered', False):
        return
    try:
        conn.create_function('norm_path', 1, normalize_path, deterministic=True)
    except Exception:
        conn.execute("SELECT norm_path('x')").fetchone()  # unusable: let it raise
    try:
        conn._norm_path_registered = True
    except AttributeError:  # a Connection subclass without a __dict__
        pass


def _folder_match(expr: str, folder: str) -> tuple[str, list]:
    """SQL: expr is folder, or under it. Prefix compare, not LIKE ('_' is a wildcard)."""
    prefix = folder if folder.endswith('/') else folder + '/'
    return (f"({expr} = ? OR substr({expr}, 1, ?) = ?)",
            [folder, len(prefix), prefix])


def common_dir_of(folder: str) -> str:
    """The git common dir of the repo whose main worktree is folder."""
    return folder.rstrip('/') + '/.git'


def folder_sql(folders: list[str], alias: str = 's') -> tuple[str, list]:
    """Sessions whose project_root or any used cwd is in/under one of folders, or
    that ran in a worktree of a repo whose main worktree is one of folders."""
    parts, params = [], []
    for f in folders:
        parts.append(f"{alias}.git_common_dir = ?")
        params.append(common_dir_of(f))
        sql, p = _folder_match(f"{alias}.project_root", f)
        parts.append(sql)
        params += p
        sql, p = _folder_match("norm_path(c.cwd)", f)
        parts.append(f"EXISTS (SELECT 1 FROM session_cwds c "
                     f"WHERE c.session_id = {alias}.session_id AND {sql})")
        params += p
    return '(' + ' OR '.join(parts) + ')', params


def date_sql(rng: DateRange, alias: str = 's') -> tuple[str, list]:
    """Overlap rule. Sessions with no timestamps never match an active range."""
    parts, params = [], []
    if rng.start:
        parts.append(f"COALESCE({alias}.end_time, {alias}.start_time) >= ?")
        params.append(rng.start)
    if rng.end:
        parts.append(f"{alias}.start_time < ?")
        params.append(rng.end)
    return ' AND '.join(parts), params


@dataclass
class Scope:
    groups: list[str] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    project: Optional[str] = None
    dates: DateRange = field(default_factory=DateRange)

    @property
    def active(self) -> bool:
        return bool(self.folders or self.groups or self.project or self.dates.active)

    def label(self) -> str:
        parts = []
        if self.groups:
            parts.append('group ' + ' + '.join(self.groups))
        if self.project:
            parts.append(f'project ~{self.project}')
        if self.dates.active:
            parts.append(self.dates.label)
        return ' · '.join(parts)

    def version_where(self, alias: str = 'v') -> tuple[str, list]:
        """plan_versions captured inside the date range ('1=1' without dates)."""
        parts, params = [], []
        if self.dates.start:
            parts.append(f"{alias}.captured_at >= ?")
            params.append(self.dates.start)
        if self.dates.end:
            parts.append(f"{alias}.captured_at < ?")
            params.append(self.dates.end)
        return (' AND '.join(parts) if parts else '1=1'), params

    def plan_where(self, alias: str = 'p') -> tuple[str, list]:
        """Plans in scope: linked to an in-scope session (groups/project), and with
        at least one version captured in the date range."""
        parts, params = [], []
        if self.groups or self.folders or self.project:
            sw, sp = self.where('s', dates=False)
            parts.append(f"{alias}.slug IN (SELECT ps.slug FROM plan_sessions ps "
                         f"JOIN sessions s ON s.session_id = ps.session_id WHERE {sw})")
            params += sp
        if self.dates.active:
            vw, vp = self.version_where('v')
            parts.append(f"EXISTS (SELECT 1 FROM plan_versions v "
                         f"WHERE v.slug = {alias}.slug AND {vw})")
            params += vp
        return (' AND '.join(parts) if parts else '1=1'), params

    def where(self, alias: str = 's', dates: bool = True) -> tuple[str, list]:
        """SQL condition (never empty; '1=1' when unscoped) and its params."""
        parts, params = [], []
        if self.groups or self.folders:
            if not self.folders:
                parts.append('0')  # groups given but all empty: match nothing
            else:
                sql, p = folder_sql(self.folders, alias)
                parts.append(sql)
                params += p
        if self.project:
            parts.append(f"({alias}.project_name LIKE ? OR {alias}.project LIKE ?)")
            params += [f"%{self.project}%", f"%{self.project}%"]
        if dates and self.dates.active:
            sql, p = date_sql(self.dates, alias)
            parts.append(sql)
            params += p
        return (' AND '.join(parts) if parts else '1=1'), params


def group_report(conn, group_config: dict) -> list[dict]:
    """Per group: each member folder's sessions, plans and activity window, plus totals.

    A member matching no sessions is flagged; that's usually a typo in the path.
    A member's 'worktrees' are the folders outside it that it matched through its
    repo's common dir, each with its own counts (already included in the member's).
    """
    register(conn)
    has_plans = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='plan_sessions'").fetchone()

    def summarise(folders: list[str]) -> dict:
        return summarise_where(*folder_sql(folders, 's'))

    def worktrees(folder: str) -> list[dict]:
        common = common_dir_of(folder)
        inside, inside_params = _folder_match('s.project_root', folder)
        roots = [r[0] for r in conn.execute(f"""
            SELECT DISTINCT s.project_root FROM sessions s
            WHERE s.git_common_dir = ? AND s.project_root IS NOT NULL AND NOT {inside}
            ORDER BY s.project_root
        """, [common, *inside_params])]
        out = []
        for root in roots:
            m = summarise_where("s.git_common_dir = ? AND s.project_root = ?", [common, root])
            m.update(folder=root, exists=os.path.isdir(root))
            out.append(m)
        return out

    def summarise_where(where: str, params: list) -> dict:
        row = conn.execute(f"""
            SELECT COUNT(*), MIN(s.start_time), MAX(COALESCE(s.end_time, s.start_time))
            FROM sessions s WHERE {where}
        """, params).fetchone()
        plans = 0
        if has_plans:
            plans = conn.execute(f"""
                SELECT COUNT(DISTINCT ps.slug) FROM plan_sessions ps
                JOIN sessions s ON s.session_id = ps.session_id WHERE {where}
            """, params).fetchone()[0]
        return {'sessions': row[0], 'plans': plans, 'first': row[1], 'last': row[2]}

    report = []
    for name in sorted(group_config):
        members = []
        for raw in group_config[name] or []:
            folder = normalize_path(raw)
            if not folder:
                continue
            m = summarise([folder])
            m.update(folder=folder, raw=raw, no_match=(m['sessions'] == 0),
                     exists=os.path.isdir(raw), worktrees=worktrees(folder))
            members.append(m)
        folders = [m['folder'] for m in members]
        total = summarise(folders) if folders else {'sessions': 0, 'plans': 0,
                                                     'first': None, 'last': None}
        report.append({'name': name, 'members': members, **total})
    return report


def build(groups: Iterable[str] = None, project: str = None,
          date_from: str = None, date_to: str = None, *, on: str = None,
          week: bool = False, days: int = None, month: bool = False,
          group_config: dict = None, now: datetime = None) -> Scope:
    """One call from CLI flags to a Scope. Raises ScopeError on bad input."""
    if group_config is None:
        try:
            from . import config
        except ImportError:
            import config
        group_config = config.get_project_groups()
    names = list(dict.fromkeys(groups or []))
    return Scope(groups=names, folders=resolve_groups(names, group_config),
                 project=project or None,
                 dates=date_range(date_from, date_to, on=on, week=week, days=days,
                                  month=month, now=now))
