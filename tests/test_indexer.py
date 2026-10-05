import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import Transcript  # noqa: E402

from session_index.indexer import SessionIndexer  # noqa: E402

SID = '11111111-1111-1111-1111-111111111111'
STUB = '22222222-2222-2222-2222-222222222222'


class TestIncrementalSkips(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.projects = root / 'projects'
        proj = self.projects / '-Users-me-work-demo'
        self.db = root / 'sessions.db'
        self.plans = root / 'plans'
        Transcript(SID).user('a real question about webhooks').write_to(proj / f'{SID}.jsonl')
        # A message-less stub, like Claude Code leaves behind: no cwd anywhere.
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f'{STUB}.jsonl').write_text(
            json.dumps({'type': 'custom-title', 'customTitle': 'x', 'sessionId': STUB}) + '\n'
            + json.dumps({'type': 'permission-mode', 'permissionMode': 'default',
                          'sessionId': STUB}) + '\n', encoding='utf-8')

    def tearDown(self):
        self._tmp.cleanup()

    def run_incremental(self):
        ix = SessionIndexer(db_path=self.db, projects_dir=self.projects)
        ix.connect()
        try:
            import session_index.config as config
            old = config.get_plans_dir
            config.get_plans_dir = lambda override=None: self.plans
            config_upstream = config.get_upstream_db
            config.get_upstream_db = lambda: None
            try:
                return ix.index_incremental()
            finally:
                config.get_plans_dir = old
                config.get_upstream_db = config_upstream
        finally:
            ix.close()

    def test_stub_without_cwd_is_not_reindexed_every_run(self):
        first = self.run_incremental()
        self.assertEqual(first['indexed'], 2)
        second = self.run_incremental()
        self.assertEqual((second['indexed'], second['unchanged']), (0, 2))

    def test_row_copied_from_upstream_is_read_once_for_folders(self):
        self.run_incremental()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE sessions SET cwd=NULL, project_root=NULL, folders_scanned=0")
        conn.commit()
        conn.close()
        again = self.run_incremental()
        self.assertEqual(again['indexed'], 2)
        conn = sqlite3.connect(self.db)
        cwd = conn.execute("SELECT cwd FROM sessions WHERE session_id=?", (SID,)).fetchone()[0]
        conn.close()
        self.assertEqual(cwd, 'E:\\work\\demo')
        self.assertEqual(self.run_incremental()['indexed'], 0)

    def test_existing_db_without_the_flag_is_migrated(self):
        self.run_incremental()
        conn = sqlite3.connect(self.db)
        conn.execute("ALTER TABLE sessions DROP COLUMN folders_scanned")
        conn.commit()
        conn.close()
        # Only the stub (no cwd) is read again; the real session keeps its folders.
        again = self.run_incremental()
        self.assertEqual((again['indexed'], again['unchanged']), (1, 1))
        self.assertEqual(self.run_incremental()['indexed'], 0)


if __name__ == '__main__':
    unittest.main()
