import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import Transcript  # noqa: E402

from session_index import plans  # noqa: E402
from session_index.hooks import capture_plan  # noqa: E402
from session_index.plans import PlanIndexer  # noqa: E402
from session_index.upstream import import_topics  # noqa: E402

SID = '11111111-1111-1111-1111-111111111111'
V1 = "# Demo plan\n\nFirst draft.\n"
V2 = "# Demo plan\n\nSecond draft.\n"

TOPICS_DDL = """
    CREATE TABLE session_topics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, topic TEXT NOT NULL,
        captured_at TEXT NOT NULL, exchange_number INTEGER, source TEXT NOT NULL)
"""


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.plans_dir = self.root / 'plans'
        self.plans_dir.mkdir()
        self.plan = self.plans_dir / 'demo-plan.md'
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        plans.create_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def versions(self):
        return self.conn.execute(
            "SELECT * FROM plan_versions WHERE slug='demo-plan' ORDER BY id").fetchall()

    def payload(self, tool, inp, resp=None, **extra):
        d = {'session_id': SID, 'transcript_path': str(self.root / 't.jsonl'),
             'hook_event_name': 'PostToolUse', 'tool_name': tool, 'tool_input': inp}
        if resp is not None:
            d['tool_response'] = resp
        d.update(extra)
        return d


class TestPlanCapture(HookTestCase):
    def test_write_and_edit_read_the_file_after_the_tool(self):
        self.plan.write_text(V1, encoding='utf-8')
        r = capture_plan(self.payload('Write', {'file_path': str(self.plan), 'content': V1}),
                         self.conn, self.plans_dir)
        self.assertEqual(r, 'write demo-plan')
        self.plan.write_text(V2, encoding='utf-8')
        capture_plan(self.payload('Edit', {'file_path': str(self.plan), 'old_string': 'First',
                                           'new_string': 'Second'}), self.conn, self.plans_dir)
        v = self.versions()
        self.assertEqual([(x['source'], x['content'], x['session_id']) for x in v],
                         [('write', V1, SID), ('edit', V2, SID)])
        self.assertIsNone(v[0]['line_no'])
        links = self.conn.execute("SELECT session_id, role FROM plan_sessions").fetchall()
        self.assertEqual([tuple(x) for x in links], [(SID, 'author')])

    def test_exit_plan_mode_is_approved_and_prefers_response_text(self):
        r = capture_plan(self.payload('ExitPlanMode',
                                      {'plan': V1, 'planFilePath': str(self.plan)},
                                      {'plan': V2, 'filePath': str(self.plan)}),
                         self.conn, self.plans_dir)
        self.assertEqual(r, 'exit_plan_mode demo-plan')
        self.assertEqual([(x['content'], x['approved']) for x in self.versions()], [(V2, 1)])

    def test_non_plan_files_are_skipped(self):
        other = self.root / 'README.md'
        other.write_text('x', encoding='utf-8')
        r = capture_plan(self.payload('Write', {'file_path': str(other), 'content': 'x'}),
                         self.conn, self.plans_dir)
        self.assertEqual(r, 'skip')
        self.assertEqual(capture_plan(self.payload('Bash', {'command': 'ls'}),
                                      self.conn, self.plans_dir), 'skip')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)

    def test_subagent_call_links_as_subagent(self):
        self.plan.write_text(V1, encoding='utf-8')
        capture_plan(self.payload('Write', {'file_path': str(self.plan), 'content': V1},
                                  agent_id='abc123'), self.conn, self.plans_dir)
        role = self.conn.execute("SELECT role FROM plan_sessions").fetchone()[0]
        self.assertEqual(role, 'subagent')

    def test_hook_capture_then_scan_keeps_one_row_with_line_citation(self):
        """The hook stores the version first (no line); the later scan adds the citation."""
        self.plan.write_text(V1, encoding='utf-8')
        capture_plan(self.payload('Write', {'file_path': str(self.plan), 'content': V1}),
                     self.conn, self.plans_dir)
        projects = self.root / 'projects'
        t = Transcript(SID, slug='demo-plan').user('plan it please').write('w1', str(self.plan), V1)
        t.write_to(projects / '-Users-me-work-demo' / f'{SID}.jsonl')
        PlanIndexer(self.conn, self.plans_dir, projects).index_incremental()
        v = self.versions()
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]['line_no'], 2)
        self.assertEqual(tuple(self.conn.execute(
            "SELECT role, first_line, last_line FROM plan_sessions").fetchone()),
            ('author', 1, 3))


class TestTopicImport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.up_path = Path(self._tmp.name) / 'upstream.db'
        up = sqlite3.connect(self.up_path)
        up.execute(TOPICS_DDL)
        up.executemany(
            "INSERT INTO session_topics (session_id, topic, captured_at, exchange_number, source) "
            "VALUES (?, ?, ?, ?, ?)", [
                ('s1', 'Already copied', '2026-09-01T10:00:00', 10, 'hook_periodic'),
                ('s1', 'New topic', '2026-09-01T11:00:00', 20, 'hook_periodic'),
                ('s1', 'A summary', '2026-09-01T12:00:00', None, 'compaction_summary'),
                ('s2', 'Bye', '2026-09-01T13:00:00', 5, 'hook_session_end'),
            ])
        up.commit()
        up.close()
        self.conn = sqlite3.connect(':memory:')
        self.conn.execute(TOPICS_DDL)
        self.conn.execute(
            "INSERT INTO session_topics (session_id, topic, captured_at, exchange_number, source) "
            "VALUES ('s1', 'Already copied', '2026-09-01T10:00:00', 10, 'hook_periodic')")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def topics(self):
        return [tuple(r) for r in self.conn.execute(
            "SELECT session_id, topic, source FROM session_topics ORDER BY captured_at")]

    def test_imports_new_hook_topics_only_once(self):
        before = self.up_path.read_bytes()
        self.assertEqual(import_topics(self.conn, self.up_path), 2)
        self.assertEqual(self.topics(), [('s1', 'Already copied', 'hook_periodic'),
                                         ('s1', 'New topic', 'hook_periodic'),
                                         ('s2', 'Bye', 'hook_session_end')])
        self.assertEqual(import_topics(self.conn, self.up_path), 0)
        self.assertEqual(self.up_path.read_bytes(), before)  # upstream never written

    def test_only_rows_after_the_watermark_are_read(self):
        import_topics(self.conn, self.up_path)
        up = sqlite3.connect(self.up_path)
        up.execute("INSERT INTO session_topics (session_id, topic, captured_at, exchange_number, "
                   "source) VALUES ('s3', 'Later', '2026-09-02T09:00:00', 1, 'hook_precompact')")
        up.commit()
        up.close()
        self.assertEqual(import_topics(self.conn, self.up_path), 1)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM archiver_meta").fetchone()[0], '5')

    def test_disabled_or_missing_upstream(self):
        self.assertEqual(import_topics(self.conn, None), 0)
        self.assertEqual(import_topics(self.conn, ''), 0)
        self.assertEqual(import_topics(self.conn, Path(self._tmp.name) / 'nope.db'), 0)


if __name__ == '__main__':
    unittest.main()
