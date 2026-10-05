import contextlib
import os
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_scope import DBTestCase, GROUPS, local  # noqa: E402

from session_index import config  # noqa: E402
from session_index import plan_views as PV  # noqa: E402
from session_index import scope as S  # noqa: E402
from session_index.search import SessionSearch  # noqa: E402

A1 = "# Alpha plan\n\nDeploy the webhook retry queue.\n"
A2 = "# Alpha plan\n\nDeploy the webhook retry queue.\n\nAlso add backoff.\n"
A3 = "# Alpha plan\n\nShip the queue.\n\nAlso add backoff.\n"
B1 = "# Blog plan\n\nWrite about the webhook launch.\n"


class PlanDB(DBTestCase):
    """Two sessions in two groups, three plans (one with no stored text)."""

    def setUp(self):
        super().setUp()
        self.add('s-etf', root='e:/work/app', start=local('2026-09-01', 9),
                 end=local('2026-09-05', 9), cwds=['E:\\work\\app'])
        self.add('s-blog', root='e:/work/blog_site', start=local('2026-09-10', 9),
                 end=local('2026-09-10', 10))
        v = self.plans.record_version
        v('alpha', A1, 'write', session_id='s-etf', transcript_path='t1.jsonl', line_no=10,
          captured_at=local('2026-09-01', 10), approved=0)
        v('alpha', A2, 'edit', session_id='s-etf', transcript_path='t1.jsonl', line_no=20,
          captured_at=local('2026-09-03', 10), approved=1)
        v('alpha', A3, 'edit', session_id='s-etf', transcript_path='t1.jsonl', line_no=30,
          captured_at=local('2026-09-05', 8))
        v('blog', B1, 'write', session_id='s-blog', transcript_path='t2.jsonl', line_no=5,
          captured_at=local('2026-09-10', 9, 30))
        self.plans._link('alpha', 's-etf', 'author', 1, 40)
        self.plans._link('blog', 's-blog', 'author', 1, 9)
        self.plans._touch_plan('empty-slug', None, local('2026-09-02'))
        self.plans._link('empty-slug', 's-etf', 'referenced', 3, 4)
        self.plans._finish()


class TestVersionsAndLists(PlanDB):
    def test_version_numbers_survive_date_filter(self):
        all_v = PV.versions(self.conn, 'alpha')
        self.assertEqual([x['n'] for x in all_v], [1, 2, 3])
        dated = PV.versions(self.conn, 'alpha', self.scope(date_from='2026-09-03'))
        self.assertEqual([x['n'] for x in dated], [2, 3])

    def test_list_plans_by_group_and_hides_empty(self):
        self.assertEqual([p['slug'] for p in PV.list_plans(self.conn, self.scope(['myapp']))],
                         ['alpha'])
        self.assertEqual([p['slug'] for p in PV.list_plans(self.conn, self.scope(['blog']))],
                         ['blog'])
        allp = PV.list_plans(self.conn, include_empty=True)
        self.assertEqual({p['slug'] for p in allp}, {'alpha', 'blog', 'empty-slug'})
        self.assertNotIn('empty-slug', [p['slug'] for p in PV.list_plans(self.conn)])

    def test_list_plans_by_date_counts_in_range(self):
        rows = {p['slug']: p for p in PV.list_plans(
            self.conn, self.scope(date_from='2026-09-03', date_to='2026-09-04'))}
        self.assertEqual(set(rows), {'alpha'})
        self.assertEqual((rows['alpha']['versions'], rows['alpha']['in_range']), (3, 1))
        self.assertEqual(rows['alpha']['approved'], 1)

    def test_resolve_slug(self):
        self.assertEqual(PV.resolve_slug(self.conn, 'alpha'), 'alpha')
        self.assertEqual(PV.resolve_slug(self.conn, 'bl'), 'blog')
        with self.assertRaisesRegex(LookupError, 'No plan'):
            PV.resolve_slug(self.conn, 'zzz')
        self.plans._touch_plan('alpine', None, None)
        with self.assertRaisesRegex(LookupError, 'matches 2 plans'):
            PV.resolve_slug(self.conn, 'alp')

    def test_plans_for_session(self):
        sid, rows = PV.plans_for_session(self.conn, 's-e')
        self.assertEqual(sid, 's-etf')
        got = {(r['slug'], r['role'], r['from_this_session']) for r in rows}
        self.assertEqual(got, {('alpha', 'author', 3), ('empty-slug', 'referenced', 0)})
        self.assertEqual(PV.plans_for_session(self.conn, 'nope'), (None, []))
        with self.assertRaises(LookupError):
            PV.plans_for_session(self.conn, 's-')


class TestPlanSearch(PlanDB):
    def test_best_version_per_plan_and_match_count(self):
        hits = {h['slug']: h for h in PV.search_plans(self.conn, 'webhook')}
        self.assertEqual(set(hits), {'alpha', 'blog'})
        self.assertEqual(hits['alpha']['matching_versions'], 2)  # A3 dropped the word
        self.assertEqual(hits['alpha']['versions'], 3)
        self.assertEqual(hits['alpha']['sessions'], ['s-etf'])
        self.assertIn('>>>', hits['alpha']['snippet'])

    def test_group_and_dates_limit_plan_hits(self):
        self.assertEqual([h['slug'] for h in PV.search_plans(
            self.conn, 'webhook', self.scope(['blog']))], ['blog'])
        # Only versions captured in range can match: A3 (Sept 5) has no "webhook".
        self.assertEqual([h['slug'] for h in PV.search_plans(
            self.conn, 'webhook', self.scope(date_from='2026-09-05', date_to='2026-09-05'))], [])
        self.assertEqual([h['slug'] for h in PV.search_plans(
            self.conn, 'backoff', self.scope(date_from='2026-09-05', date_to='2026-09-05'))],
            ['alpha'])

    def test_context_prefers_latest_then_best_match(self):
        hit = next(h for h in PV.search_plans(self.conn, 'backoff') if h['slug'] == 'alpha')
        ctx = PV.context_for_hit(self.conn, hit, 'backoff')
        self.assertEqual((ctx['n'], ctx['latest']), (3, True))
        hit = next(h for h in PV.search_plans(self.conn, 'webhook') if h['slug'] == 'alpha')
        ctx = PV.context_for_hit(self.conn, hit, 'webhook')
        self.assertFalse(ctx['latest'])
        self.assertIn('webhook', ctx['excerpts'][0])

    def test_excerpts_merge_overlaps_and_cap(self):
        text = '\n'.join(['a', 'hit one', 'hit two', 'b', 'c', 'd', 'hit three', 'e', 'f',
                          'g', 'hit four'])
        ex = PV.excerpts(text, 'hit', max_hits=2)
        self.assertEqual(ex, ['a\nhit one\nhit two\nb', 'd\nhit three\ne'])
        self.assertEqual(PV.query_terms('"retry queue" AND NOT backoff*'),
                         ['retry', 'queue', 'backoff'])


class TestExportAndList(PlanDB):
    def test_export_writes_versions_and_index(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            r = PV.export_plans(self.conn, out, self.scope(['myapp'], date_from='2026-09-03'))
            self.assertEqual((r['plans'], r['files']), (1, 2))
            self.assertEqual(sorted(p.name for p in (out / 'alpha').iterdir()), ['v2.md', 'v3.md'])
            self.assertTrue((out / 'alpha' / 'v2.md').read_text(encoding='utf-8').endswith(A2))
            idx = json.loads((out / 'index.json').read_text(encoding='utf-8'))
            self.assertEqual(idx['scope']['groups'], ['myapp'])
            v = idx['plans'][0]['versions'][0]
            self.assertEqual((v['n'], v['file'], v['approved'], v['line_no']),
                             (2, 'alpha/v2.md', 1, 20))
            self.assertEqual(idx['plans'][0]['sessions'][0]['session_id'], 's-etf')
            with self.assertRaises(FileExistsError):
                PV.export_plans(self.conn, out)
            self.assertEqual(PV.export_plans(self.conn, out, force=True)['plans'], 2)

    def test_files_are_stamped_with_capture_time_not_export_time(self):
        """A consumer whose watermark is a modification time (writer's docs source)
        must see only versions captured since it last looked."""
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out)
            mtimes = {p: p.stat().st_mtime for p in sorted((out / 'alpha').iterdir())}
            self.assertEqual([round(m) for m in mtimes.values()],
                             [round(PV._epoch(local('2026-09-01', 10))),
                              round(PV._epoch(local('2026-09-03', 10))),
                              round(PV._epoch(local('2026-09-05', 8)))])
            r = PV.export_plans(self.conn, out, force=True)
            self.assertEqual((r['written'], r['unchanged']), (0, 4))
            self.assertEqual({p: p.stat().st_mtime for p in sorted((out / 'alpha').iterdir())},
                             mtimes)

    def test_frontmatter_carries_provenance(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out)
            head, _, body = (out / 'alpha' / 'v1.md').read_text(encoding='utf-8').partition('---\n\n')
            self.assertEqual(body, A1)
            fields = dict(line.split(': ', 1) for line in head.splitlines()
                          if ': ' in line and not line.startswith('---'))
            self.assertEqual(fields['plan'], '"alpha"')
            self.assertEqual(fields['version'], '1')
            self.assertEqual(fields['approval'], '"rejected"')
            self.assertEqual(fields['captured_by'], '"write"')
            self.assertEqual(fields['session_id'], '"s-etf"')
            self.assertEqual(fields['cite'], '"t1.jsonl:10"')
            for n, expected in ((2, '"approved"'), (3, '"unknown"')):
                text = (out / 'alpha' / f'v{n}.md').read_text(encoding='utf-8')
                self.assertIn(f'approval: {expected}', text)

    def test_plain_export_is_bare_text(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out, plain=True)
            self.assertEqual((out / 'alpha' / 'v1.md').read_text(encoding='utf-8'), A1)
            idx = json.loads((out / 'index.json').read_text(encoding='utf-8'))
            self.assertFalse(idx['frontmatter'])

    def test_changed_version_is_rewritten(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out)
            (out / 'alpha' / 'v1.md').write_text('tampered\n', encoding='utf-8')
            r = PV.export_plans(self.conn, out, force=True)
            self.assertEqual(r['written'], 1)
            self.assertIn(A1, (out / 'alpha' / 'v1.md').read_text(encoding='utf-8'))

    def test_check_reports_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            r = PV.export_plans(self.conn, out, check=True)
            self.assertFalse(r['current'])
            self.assertEqual(r['written'], 4)
            self.assertFalse(out.exists())  # check never creates anything

            PV.export_plans(self.conn, out)
            r = PV.export_plans(self.conn, out, check=True)
            self.assertTrue(r['current'])
            self.assertEqual((r['written'], r['stale']), (0, []))

            # A new version makes it out of date again.
            self.plans.record_version('alpha', A3 + '\nMore.\n', 'edit', session_id='s-etf',
                                      captured_at=local('2026-09-06', 9))
            r = PV.export_plans(self.conn, out, check=True)
            self.assertEqual((r['current'], r['written']), (False, 1))

    def test_check_notices_stale_files_and_missing_index(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out)
            (out / 'alpha' / 'v9.md').write_text('left over\n', encoding='utf-8')
            r = PV.export_plans(self.conn, out, check=True)
            self.assertEqual((r['current'], r['stale']), (False, ['alpha\\v9.md'.replace('\\', os.sep)]))
            (out / 'alpha' / 'v9.md').unlink()
            (out / 'index.json').unlink()
            self.assertFalse(PV.export_plans(self.conn, out, check=True)['current'])

    def test_export_leaves_stale_files_alone_but_reports_them(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'exp'
            PV.export_plans(self.conn, out)
            stale = out / 'alpha' / 'v9.md'
            stale.write_text('left over\n', encoding='utf-8')
            r = PV.export_plans(self.conn, out, force=True)
            self.assertEqual(len(r['stale']), 1)
            self.assertTrue(stale.exists())

    def test_list_sessions_handoff(self):
        s = SessionSearch(db_path=self.db_path)
        s.connect()
        try:
            rows = s.list_sessions(self.scope(['myapp']))
        finally:
            s.close()
        self.assertEqual([r['session_id'] for r in rows], ['s-etf'])
        r = rows[0]
        self.assertEqual(r['cwds'], ['E:\\work\\app'])
        self.assertEqual(r['transcript_path'], 's-etf.jsonl')
        for key in ('cwd', 'project_root', 'git_branch', 'git_common_dir', 'start_time', 'end_time'):
            self.assertIn(key, r)


class TestGroupFlagsAndConfig(DBTestCase):
    def test_group_report_says_whether_folder_exists(self):
        with tempfile.TemporaryDirectory() as d:
            rep = S.group_report(self.conn, {'g': [d, d + '/missing']})
            flags = [(m['exists'], m['no_match']) for m in rep[0]['members']]
            self.assertEqual(flags, [(True, True), (False, True)])

    def test_invalid_config_json_warns(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / 'config.json'
            bad.write_text('{"project_groups": {"a": [],}}', encoding='utf-8')
            old = config.CONFIG_FILE
            config.CONFIG_FILE = bad
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    self.assertEqual(config._load_config_file(), {})
            finally:
                config.CONFIG_FILE = old
            self.assertIn('warning: ignoring', err.getvalue())


if __name__ == '__main__':
    unittest.main()
