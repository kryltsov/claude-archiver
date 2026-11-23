import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from session_index import scope as S
from session_index.analyzer import analytics
from session_index.indexer import SessionIndexer
from session_index.plans import PlanIndexer
from session_index.search import SessionSearch

GROUPS = {
    'myapp': ['E:/work/app', 'E:\\work\\app-cli\\'],
    'blog': ['e:/work/blog_site'],
    'typo': ['E:/work/does-not-exist'],
    'empty': [],
}


def local(day: str, hh: int = 0, mm: int = 0) -> str:
    """A local wall-clock time as a transcript-style UTC timestamp."""
    dt = datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}:00").astimezone()
    return S._utc_iso(dt)


class TestDateRange(unittest.TestCase):
    def test_from_to_are_local_days_to_is_exclusive_next_midnight(self):
        r = S.date_range('2026-09-01', '2026-09-03')
        self.assertEqual((r.start, r.end), (local('2026-09-01'), local('2026-09-04')))
        self.assertTrue(r.start.endswith('Z'))
        self.assertEqual(r.label, '2026-09-01 → 2026-09-03')

    def test_open_ended(self):
        self.assertIsNone(S.date_range('2026-09-01').end)
        self.assertIsNone(S.date_range(None, '2026-09-01').start)
        self.assertFalse(S.date_range().active)

    def test_date_shortcut_is_one_local_day(self):
        r = S.date_range(on='2026-09-02')
        self.assertEqual((r.start, r.end), (local('2026-09-02'), local('2026-09-03')))

    def test_week_days_month_count_back_from_now(self):
        now = datetime(2026, 9, 22, 12, 0).astimezone()
        self.assertEqual(S.date_range(week=True, now=now).start, S._utc_iso(now - timedelta(days=7)))
        self.assertEqual(S.date_range(days=3, now=now).start, S._utc_iso(now - timedelta(days=3)))
        self.assertEqual(S.date_range(month=True, now=now).start, S._utc_iso(now - timedelta(days=30)))

    def test_shortcuts_intersect(self):
        now = datetime(2026, 9, 22, 12, 0).astimezone()
        r = S.date_range(on='2026-09-10', week=True, now=now)
        self.assertEqual(r.start, S._utc_iso(now - timedelta(days=7)))  # the later start wins
        self.assertEqual(r.end, local('2026-09-11'))

    def test_errors(self):
        with self.assertRaisesRegex(S.ScopeError, "can't be combined"):
            S.date_range('2026-09-01', week=True)
        with self.assertRaisesRegex(S.ScopeError, 'after --to'):
            S.date_range('2026-09-05', '2026-09-01')
        with self.assertRaisesRegex(S.ScopeError, 'YYYY-MM-DD'):
            S.date_range('01/09/2026')
        with self.assertRaisesRegex(S.ScopeError, 'positive'):
            S.date_range(days=-1)


class TestResolveGroups(unittest.TestCase):
    def test_union_normalised_and_deduplicated(self):
        self.assertEqual(S.resolve_groups(['myapp', 'myapp'], GROUPS),
                         ['e:/work/app', 'e:/work/app-cli'])

    def test_unknown_group_lists_known(self):
        with self.assertRaises(S.ScopeError) as cm:
            S.resolve_groups(['nope'], GROUPS)
        self.assertIn('blog, empty, myapp, typo', str(cm.exception))


class DBTestCase(unittest.TestCase):
    """Real schema (SessionIndexer) with hand-made rows."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / 'projects').mkdir()
        self.db_path = root / 'sessions.db'
        ix = SessionIndexer(db_path=self.db_path, projects_dir=root / 'projects')
        ix.connect()
        self.conn = ix.conn
        self._ix = ix
        self.plans = PlanIndexer(self.conn, root / 'plans')

    def tearDown(self):
        self._ix.close()
        self._tmp.cleanup()

    def add(self, sid, root=None, start=None, end=None, cwds=(), project='-Users-me-work-x',
            name='x', text='hello world', common=None):
        self.conn.execute("""
            INSERT INTO sessions (session_id, project, project_name, title_display, file_path,
                indexed_at, start_time, end_time, project_root, exchange_count, duration_minutes,
                git_common_dir)
            VALUES (?, ?, ?, ?, ?, 'now', ?, ?, ?, 2, 5, ?)
        """, (sid, project, name, f'title {sid}', f'{sid}.jsonl', start, end, root, common))
        for c in cwds:
            self.conn.execute("INSERT INTO session_cwds VALUES (?, ?)", (sid, c))
        self.conn.execute("INSERT INTO session_content (session_id, content) VALUES (?, ?)",
                          (sid, text))
        self.conn.commit()

    def ids(self, scope, dates=True):
        S.register(self.conn)
        where, params = scope.where('s', dates=dates)
        return sorted(r[0] for r in self.conn.execute(
            f"SELECT s.session_id FROM sessions s WHERE {where}", params))

    def scope(self, groups=None, **kw):
        return S.build(groups=groups, group_config=GROUPS, **kw)


class TestGroupMatching(DBTestCase):
    def setUp(self):
        super().setUp()
        self.add('root', root='e:/work/app')
        self.add('sub', root='e:/work/app/scripts/data-pipeline')
        self.add('sibling', root='e:/work/app-old')    # prefix, not a subfolder
        self.add('cli', root='e:/work/app-cli')
        self.add('cwd-only', root='e:/work', cwds=['E:\\work\\app\\docs'])
        self.add('blog', root='e:/work/blog_site')
        self.add('wildcard', root='e:/work/blogXsite/drafts')     # '_' must not act as LIKE '_'
        self.add('no-folder', root=None)

    def test_folder_or_under_it_via_root_or_any_cwd(self):
        self.assertEqual(self.ids(self.scope(['myapp'])), ['cli', 'cwd-only', 'root', 'sub'])

    def test_underscore_is_literal(self):
        self.assertEqual(self.ids(self.scope(['blog'])), ['blog'])

    def test_repeat_group_is_union(self):
        self.assertEqual(self.ids(self.scope(['myapp', 'blog'])),
                         ['blog', 'cli', 'cwd-only', 'root', 'sub'])

    def test_folder_shared_by_two_groups(self):
        shared = {'a': ['E:/work/app', 'E:/work/blog_site'],
                  'b': ['E:/work/blog_site', 'E:/work/app-cli']}
        ids = lambda names: self.ids(S.build(groups=names, group_config=shared))
        self.assertIn('blog', ids(['a']))
        self.assertIn('blog', ids(['b']))
        both = ids(['a', 'b'])
        self.assertEqual(both.count('blog'), 1)  # union, not double-counted
        self.assertEqual(both, ['blog', 'cli', 'cwd-only', 'root', 'sub'])
        rep = {g['name']: g for g in S.group_report(self.conn, shared)}
        self.assertEqual((rep['a']['sessions'], rep['b']['sessions']), (4, 2))

    def test_member_paths_with_backslashes_and_upper_drive(self):
        s = S.build(groups=['g'], group_config={'g': ['E:\\work\\blog_site\\']})
        self.assertEqual(self.ids(s), ['blog'])

    def test_empty_or_unmatched_group_matches_nothing(self):
        self.assertEqual(self.ids(self.scope(['empty'])), [])
        self.assertEqual(self.ids(self.scope(['typo'])), [])

    def test_project_narrows_inside_group(self):
        self.conn.execute("UPDATE sessions SET project_name='cli thing' WHERE session_id='cli'")
        self.assertEqual(self.ids(self.scope(['myapp'], project='cli thing')), ['cli'])

    def test_unscoped_is_everything(self):
        self.assertEqual(len(self.ids(S.Scope())), 8)


WT_GROUPS = {
    'app': ['E:/work/app'],                     # a repo's main worktree
    'feature': ['E:\\work\\app-feature'],       # one linked worktree of it
    'mono-app': ['E:/work/mono/packages/app'],  # a subfolder of another repo
}
APP_GIT = 'e:/work/app/.git'
MONO_GIT = 'e:/work/mono/.git'


class TestWorktreeMatching(DBTestCase):
    """Sessions in a repo's worktrees share its git_common_dir; folders need not exist."""

    def setUp(self):
        super().setUp()
        self.add('main', root='e:/work/app', common=APP_GIT)
        self.add('nested-wt', root='e:/work/app/.claude/worktrees/x', common=APP_GIT)
        self.add('wt', root='e:/work/app-feature', common=APP_GIT)
        self.add('wt-gone', root='e:/work/app-old-branch', common=APP_GIT)
        self.add('other', root='e:/work/other', common='e:/work/other/.git')
        self.add('no-git', root='e:/work/app-notes')            # sibling name, not a worktree
        self.add('mono-pkg', root='e:/work/mono', common=MONO_GIT,
                 cwds=['E:\\work\\mono\\packages\\app'])
        self.add('mono-root', root='e:/work/mono', common=MONO_GIT)
        self.add('mono-wt', root='e:/work/mono-wt', common=MONO_GIT)

    def scope(self, groups=None, **kw):
        return S.build(groups=groups, group_config=WT_GROUPS, **kw)

    def test_main_repo_member_matches_its_worktrees_anywhere(self):
        self.assertEqual(self.ids(self.scope(['app'])),
                         ['main', 'nested-wt', 'wt', 'wt-gone'])

    def test_linked_worktree_member_matches_only_itself(self):
        self.assertEqual(self.ids(self.scope(['feature'])), ['wt'])

    def test_subfolder_member_does_not_pull_in_the_repo(self):
        self.assertEqual(self.ids(self.scope(['mono-app'])), ['mono-pkg'])

    def test_union_counts_each_session_once(self):
        self.assertEqual(self.ids(self.scope(['app', 'feature'])),
                         ['main', 'nested-wt', 'wt', 'wt-gone'])

    def test_plans_of_worktree_sessions_are_in_scope(self):
        self.plans.record_version('wt-plan', '# WT\n', 'write', session_id='wt',
                                  captured_at='2026-09-10T00:00:00.000Z')
        self.plans._link('wt-plan', 'wt', 'author', 1, 2)
        S.register(self.conn)
        where, params = self.scope(['app']).plan_where('p')
        slugs = [r[0] for r in self.conn.execute(
            f"SELECT p.slug FROM plans p WHERE {where}", params)]
        self.assertEqual(slugs, ['wt-plan'])

    def test_group_report_lists_picked_up_worktrees(self):
        rep = {g['name']: g for g in S.group_report(self.conn, WT_GROUPS)}
        app = rep['app']['members'][0]
        self.assertEqual(app['sessions'], 4)
        self.assertFalse(app['no_match'])
        # nested-wt sits under the member, so it isn't listed as an outside worktree.
        self.assertEqual([(w['folder'], w['sessions'], w['exists']) for w in app['worktrees']],
                         [('e:/work/app-feature', 1, False), ('e:/work/app-old-branch', 1, False)])
        self.assertEqual(rep['feature']['members'][0]['worktrees'], [])
        self.assertEqual(rep['mono-app']['members'][0]['worktrees'], [])


class TestDateOverlap(DBTestCase):
    def setUp(self):
        super().setUp()
        D = '2026-09-10'
        self.add('inside', start=local(D, 10), end=local(D, 11))
        self.add('before', start=local('2026-09-09', 10), end=local('2026-09-09', 23, 59))
        self.add('after', start=local('2026-09-11', 0), end=local('2026-09-11', 1))
        self.add('across-midnight-in', start=local('2026-09-09', 23), end=local(D, 1))
        self.add('across-midnight-out', start=local(D, 23), end=local('2026-09-11', 2))
        self.add('spans-days', start=local('2026-09-01', 9), end=local('2026-09-20', 9))
        self.add('no-end', start=local(D, 12), end=None)
        self.add('no-times')

    def test_one_day_catches_everything_touching_it(self):
        self.assertEqual(self.ids(self.scope(date_from='2026-09-10', date_to='2026-09-10')),
                         ['across-midnight-in', 'across-midnight-out', 'inside', 'no-end',
                          'spans-days'])

    def test_from_only_and_to_only(self):
        self.assertNotIn('before', self.ids(self.scope(date_from='2026-09-10')))
        self.assertIn('after', self.ids(self.scope(date_from='2026-09-10')))
        self.assertNotIn('after', self.ids(self.scope(date_to='2026-09-10')))
        self.assertIn('before', self.ids(self.scope(date_to='2026-09-10')))

    def test_session_without_timestamps_only_matches_unfiltered(self):
        self.assertNotIn('no-times', self.ids(self.scope(date_from='2000-01-01')))
        self.assertIn('no-times', self.ids(S.Scope()))

    def test_date_shortcut_now_uses_overlap(self):
        self.assertIn('across-midnight-in', self.ids(self.scope(on='2026-09-10')))


class TestCommands(DBTestCase):
    def setUp(self):
        super().setUp()
        self.add('a1', root='e:/work/app', start=local('2026-09-10', 9),
                 end=local('2026-09-10', 10), text='webhook retry logic', name='etf integrate')
        self.add('a2', root='e:/work/app', start=local('2026-08-01', 9),
                 end=local('2026-08-01', 10), text='webhook signature', name='etf integrate')
        self.add('b1', root='e:/work/blog_site', start=local('2026-09-10', 9),
                 end=local('2026-09-10', 10), text='webhook in a blog post', name='slp blog')
        self.s = SessionSearch(db_path=self.db_path)
        self.s.connect()

    def tearDown(self):
        self.s.close()
        super().tearDown()

    def test_search_scoped_by_group_and_dates(self):
        ids = lambda sc: sorted(r['session_id'] for r in self.s.search('webhook', 20, scope=sc))
        self.assertEqual(ids(None), ['a1', 'a2', 'b1'])
        self.assertEqual(ids(self.scope(['myapp'])), ['a1', 'a2'])
        self.assertEqual(ids(self.scope(['myapp'], date_from='2026-09-01')), ['a1'])

    def test_find_and_recent(self):
        self.assertEqual([r['session_id'] for r in self.s.recent(10, scope=self.scope(['blog']))],
                         ['b1'])
        self.assertEqual([r['session_id'] for r in self.s.find(scope=self.scope(date_to='2026-08-31'))],
                         ['a2'])

    def test_find_legacy_params_still_work(self):
        self.assertEqual([r['session_id'] for r in self.s.find(project='slp')], ['b1'])
        self.assertEqual(sorted(r['session_id'] for r in self.s.find(date='2026-09-10')),
                         ['a1', 'b1'])

    def test_analytics_scoped(self):
        res = analytics(db_path=self.db_path, scope=self.scope(['myapp'], date_from='2026-09-01'))
        self.assertEqual(res['overview']['total_sessions'], 1)
        self.assertIn('group myapp', res['period'])
        self.assertEqual(analytics(db_path=self.db_path)['overview']['total_sessions'], 3)

    def test_group_report(self):
        self.plans.record_version('p1', '# P1\n', 'write', session_id='a1',
                                  captured_at='2026-09-10T00:00:00.000Z')
        self.plans._link('p1', 'a1', 'author', 1, 2)
        rep = {g['name']: g for g in S.group_report(self.conn, GROUPS)}
        y = rep['myapp']
        self.assertEqual((y['sessions'], y['plans']), (2, 1))
        self.assertEqual([(m['folder'], m['sessions'], m['no_match']) for m in y['members']],
                         [('e:/work/app', 2, False), ('e:/work/app-cli', 0, True)])
        self.assertTrue(rep['typo']['members'][0]['no_match'])
        self.assertEqual(rep['empty']['sessions'], 0)
        self.assertEqual(y['first'][:10], local('2026-08-01', 9)[:10])


if __name__ == '__main__':
    unittest.main()
