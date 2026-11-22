"""The conversation is stored whole and survives compaction and rewritten transcripts."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import Transcript  # noqa: E402

import session_index.config as config  # noqa: E402
from session_index.analyzer import get_context  # noqa: E402
from session_index.indexer import SessionIndexer  # noqa: E402
from session_index.messages import compact_topic  # noqa: E402

SID = '33333333-3333-3333-3333-333333333333'

SUMMARY = ("This session is being continued from a previous conversation that ran out of "
           "context. The summary below covers the earlier portion of the conversation.\n\n"
           "Summary:\n1. Primary Request and Intent:\n   - The user asked to wire up the "
           "webhook retry queue.\n2. Key Technical Concepts: ...")


def long_session() -> Transcript:
    t = Transcript(SID)
    t.user('please design the zebrafish webhook retry queue')
    t.reply('Here is a design for the retry queue with exponential backoff.')
    t.edit('t1', 'E:\\work\\demo\\queue.py', 'old', 'new')
    t.compact(SUMMARY)
    t.user('now add the narwhal dead-letter table')
    t.reply('Added the dead-letter table.')
    t.compact(SUMMARY.replace('webhook retry queue', 'dead-letter table'))
    t.user('final question about pelican metrics')
    t.reply('The pelican metrics are exported every minute.')
    return t


class MessagesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / 'projects'
        self.path = self.projects / 'E--work-demo' / f'{SID}.jsonl'
        self.db = root / 'sessions.db'
        self.plans = root / 'plans'
        self._saved = (config.get_plans_dir, config.get_upstream_db)
        config.get_plans_dir = lambda override=None: self.plans
        config.get_upstream_db = lambda: None

    def tearDown(self):
        config.get_plans_dir, config.get_upstream_db = self._saved
        self._tmp.cleanup()

    def index(self, incremental=False):
        ix = SessionIndexer(db_path=self.db, projects_dir=self.projects)
        ix.connect()
        try:
            if incremental:
                return ix.index_incremental()
            self.assertTrue(ix.index_session(file_path=str(self.path)))
        finally:
            ix.close()

    def q(self, sql, *args):
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def append(self, *lines):
        with open(self.path, 'a', encoding='utf-8', newline='\n') as f:
            for d in lines:
                f.write(json.dumps(d) + '\n')


class TestCompaction(MessagesTestCase):
    def setUp(self):
        super().setUp()
        long_session().write_to(self.path)
        self.index()

    def test_every_message_is_stored_with_its_compaction_epoch(self):
        rows = self.q("SELECT role, kind, compact_epoch FROM messages "
                      "WHERE session_id=? ORDER BY line_no", SID)
        self.assertEqual(rows, [
            ('user', 'prompt', 0), ('assistant', 'reply', 0), ('assistant', 'tool', 0),
            ('user', 'compact_summary', 1),
            ('user', 'prompt', 1), ('assistant', 'reply', 1),
            ('user', 'compact_summary', 2),
            ('user', 'prompt', 2), ('assistant', 'reply', 2),
        ])

    def test_tool_calls_are_one_line_stubs_and_results_are_not_stored(self):
        tools = self.q("SELECT text FROM messages WHERE kind='tool'")
        self.assertEqual(tools, [('[Edit: E:\\work\\demo\\queue.py (old...)]',)])
        self.assertFalse(self.q("SELECT 1 FROM messages WHERE text='ok'"))

    def test_compaction_is_detected(self):
        self.assertEqual(self.q("SELECT has_compaction, compaction_count FROM sessions"),
                         [(1, 2)])
        topics = [r[0] for r in self.q(
            "SELECT topic FROM session_topics WHERE source='compaction_summary' ORDER BY id")]
        self.assertEqual(topics, [
            'Compaction 1: The user asked to wire up the webhook retry queue.',
            'Compaction 2: The user asked to wire up the dead-letter table.',
        ])

    def test_search_covers_before_and_after_every_compaction(self):
        for word in ('zebrafish', 'narwhal', 'pelican', 'backoff', 'queue.py'):
            hits = self.q("SELECT session_id FROM session_content WHERE session_content MATCH ?",
                          f'"{word}"')
            self.assertEqual(hits, [(SID,)], word)

    def test_reindexing_adds_nothing(self):
        self.index()
        self.assertEqual(self.q("SELECT COUNT(*) FROM messages")[0][0], 9)
        self.assertEqual(self.q("SELECT COUNT(*) FROM session_topics")[0][0], 2)

    def test_appended_lines_are_added(self):
        more = Transcript(SID)
        more._n = 100
        more.user('one more thing about otters').reply('Otters noted.')
        self.append(*more.lines)
        self.index()
        self.assertEqual(self.q("SELECT COUNT(*) FROM messages")[0][0], 11)
        self.assertEqual(self.q("SELECT compact_epoch FROM messages WHERE text LIKE '%otters%'"),
                         [(2,), (2,)])

    def test_messages_survive_a_rewritten_transcript(self):
        # Keep only the last two lines: nothing already stored may disappear.
        lines = self.path.read_text(encoding='utf-8').splitlines()
        self.path.write_text('\n'.join(lines[-2:]) + '\n', encoding='utf-8')
        self.index()
        self.assertEqual(self.q("SELECT COUNT(*) FROM messages")[0][0], 9)
        hits = self.q("SELECT 1 FROM session_content WHERE session_content MATCH 'zebrafish'")
        self.assertTrue(hits)

    def test_context_reads_the_db_when_the_transcript_is_gone(self):
        self.path.unlink()
        ctx = get_context(SID, query='narwhal', db_path=self.db)
        self.assertEqual([ex['user'] for ex in ctx['exchanges']],
                         ['now add the narwhal dead-letter table'])
        self.assertEqual(ctx['exchanges'][0]['assistant'], 'Added the dead-letter table.')


class TestMigration(MessagesTestCase):
    def test_sessions_indexed_before_messages_are_read_again(self):
        long_session().write_to(self.path)
        self.index(incremental=True)
        conn = sqlite3.connect(self.db)
        conn.execute("DROP TABLE messages")
        conn.execute("ALTER TABLE sessions DROP COLUMN messages_scanned")
        conn.execute("ALTER TABLE sessions DROP COLUMN compaction_count")
        conn.commit()
        conn.close()
        self.assertEqual(self.index(incremental=True)['indexed'], 1)
        self.assertEqual(self.q("SELECT COUNT(*) FROM messages")[0][0], 9)
        self.assertEqual(self.index(incremental=True)['indexed'], 0)


class TestCompactTopic(unittest.TestCase):
    def test_falls_back_to_first_real_line(self):
        self.assertEqual(compact_topic('Summary:\n- Fixed the flaky login test', 3),
                         'Compaction 3: Fixed the flaky login test')
        self.assertEqual(compact_topic('', 1), 'Compaction 1')


if __name__ == '__main__':
    unittest.main()
