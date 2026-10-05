import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from fixtures import Transcript  # noqa: E402

from session_index import plans  # noqa: E402
from session_index.plans import PlanIndexer, apply_edits, plan_title  # noqa: E402

SID = '11111111-1111-1111-1111-111111111111'
SID2 = '22222222-2222-2222-2222-222222222222'

V1 = "# Demo plan\n\n## Context\n\nFirst draft.\n"
V2 = "# Demo plan\n\n## Context\n\nSecond draft.\n"


class PlanTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.projects = self.root / 'projects'
        self.plans_dir = self.root / 'plans'
        self.plans_dir.mkdir()
        self.proj = self.projects / '-Users-me-work-demo'
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        plans.create_schema(self.conn)
        self.ix = PlanIndexer(self.conn, self.plans_dir, self.projects)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def plan_path(self, slug='demo-plan'):
        return str(self.plans_dir / f'{slug}.md')

    def versions(self, slug='demo-plan'):
        return self.conn.execute(
            "SELECT * FROM plan_versions WHERE slug=? ORDER BY captured_at, id", (slug,)
        ).fetchall()

    def links(self, slug='demo-plan'):
        return {(r['session_id'], r['role']): (r['first_line'], r['last_line'])
                for r in self.conn.execute("SELECT * FROM plan_sessions WHERE slug=?", (slug,))}

    def save(self, t: Transcript, sid=None) -> Path:
        return t.write_to(self.proj / f'{sid or t.session_id}.jsonl')


class TestHelpers(unittest.TestCase):
    def test_apply_edits_first_occurrence_and_replace_all(self):
        self.assertEqual(apply_edits('a b a', [{'old_string': 'a', 'new_string': 'x'}]), 'x b a')
        self.assertEqual(apply_edits('a b a', [{'old_string': 'a', 'new_string': 'x',
                                                'replace_all': True}]), 'x b x')

    def test_apply_edits_missing_base_or_text(self):
        self.assertIsNone(apply_edits(None, [{'old_string': 'a', 'new_string': 'b'}]))
        self.assertIsNone(apply_edits('abc', [{'old_string': 'zzz', 'new_string': 'b'}]))
        self.assertEqual(apply_edits(None, [{'old_string': '', 'new_string': 'new'}]), 'new')

    def test_plan_title(self):
        self.assertEqual(plan_title(V1), 'Demo plan')
        self.assertEqual(plan_title('\nno heading here\nmore'), 'no heading here')
        self.assertIsNone(plan_title('   \n'))


class TestSlugForPath(PlanTestCase):
    def test_backslash_forward_slash_and_drive_case(self):
        p = self.plan_path()
        self.assertEqual(self.ix.slug_for_path(p), 'demo-plan')
        self.assertEqual(self.ix.slug_for_path(p.replace('\\', '/')), 'demo-plan')
        if len(p) > 1 and p[1] == ':':
            flipped = (p[0].lower() if p[0].isupper() else p[0].upper()) + p[1:]
            self.assertEqual(self.ix.slug_for_path(flipped), 'demo-plan')

    def test_rejects_other_folders_and_files(self):
        self.assertIsNone(self.ix.slug_for_path(str(self.root / 'elsewhere' / 'x.md')))
        self.assertIsNone(self.ix.slug_for_path(str(self.plans_dir / 'sub' / 'x.md')))
        self.assertIsNone(self.ix.slug_for_path(str(self.plans_dir / 'x.txt')))
        self.assertIsNone(self.ix.slug_for_path(None))


class TestTranscriptSignals(PlanTestCase):
    def test_write_creates_version_plan_and_author_link(self):
        t = Transcript(SID, slug='demo-plan').user('plan something please')
        t.plan_mode(self.plan_path()).write('w1', self.plan_path(), V1)
        self.save(t)
        self.ix.backfill()

        v = self.versions()
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]['source'], 'write')
        self.assertEqual(v[0]['content'], V1)
        self.assertEqual(v[0]['session_id'], SID)
        self.assertEqual(v[0]['line_no'], 3)  # the tool_use line
        self.assertIsNone(v[0]['approved'])
        plan = self.conn.execute("SELECT * FROM plans WHERE slug='demo-plan'").fetchone()
        self.assertEqual(plan['title'], 'Demo plan')
        self.assertEqual(plan['first_seen'], '2026-09-01T10:00:01.000Z')
        # 'referenced' (slug on every line) is folded into 'author', keeping the full range
        self.assertEqual(self.links(), {(SID, 'author'): (1, 4)})

    def test_rejected_write_adds_nothing(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1, ok=False)
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.versions(), [])
        self.assertEqual(self.links(), {(SID, 'referenced'): (1, 2)})

    def test_edit_rebuilt_from_previous_write(self):
        t = Transcript(SID, slug='demo-plan')
        t.write('w1', self.plan_path(), V1)
        t.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        self.save(t)
        self.ix.backfill()
        v = self.versions()
        self.assertEqual([x['source'] for x in v], ['write', 'edit'])
        self.assertEqual(v[1]['content'], V2)

    def test_edit_after_revert_uses_the_text_in_hand(self):
        """V1 -> V2 -> back to V1 (dedups onto the early V1 row) -> edit. The base is V1,
        not V2, even though V2 is the newest row in the DB."""
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        t.edit('e2', self.plan_path(), 'Second draft.', 'First draft.')
        t.edit('e3', self.plan_path(), 'First draft.', 'Third draft.')
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.versions()[-1]['content'], V1.replace('First', 'Third'))

    def test_multiedit_applies_in_order(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.tool_use('m1', 'MultiEdit', {'file_path': self.plan_path(), 'edits': [
            {'old_string': 'First', 'new_string': 'Second'},
            {'old_string': 'Context', 'new_string': 'Background'}]})
        t.tool_result('m1', True, {'filePath': self.plan_path()})
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.versions()[-1]['content'],
                         "# Demo plan\n\n## Background\n\nSecond draft.\n")

    def test_edit_uses_tool_use_result_over_input(self):
        """The user can change an edit in the permission dialog; the result has what landed."""
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.tool_use('e1', 'Edit', {'file_path': self.plan_path(), 'old_string': 'First draft.',
                                  'new_string': 'What Claude proposed.'})
        t.tool_result('e1', True, {'filePath': self.plan_path(), 'oldString': 'First draft.',
                                   'newString': 'Second draft.', 'userModified': True})
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.versions()[-1]['content'], V2)

    def test_edit_base_from_an_earlier_session(self):
        a = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        b = Transcript(SID2, slug='demo-plan')
        b._n = 100  # later timestamps than session A
        b.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        self.save(a)
        self.save(b)
        self.ix.backfill()
        v = self.versions()
        self.assertEqual([(x['source'], x['session_id']) for x in v],
                         [('write', SID), ('edit', SID2)])
        self.assertEqual(v[1]['content'], V2)
        self.assertIn((SID2, 'author'), self.links())

    def test_edit_without_known_base_is_counted_not_stored(self):
        t = Transcript(SID, slug='demo-plan').edit('e1', self.plan_path(), 'x', 'y')
        self.save(t)
        stats = self.ix.backfill()
        self.assertEqual(self.versions(), [])
        self.assertEqual(stats['edits_unresolved'], 1)
        self.assertIn((SID, 'author'), self.links())

    def test_failed_edit_is_not_applied(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.edit('e1', self.plan_path(), 'First draft.', 'Nope.', ok=False)
        t.edit('e2', self.plan_path(), 'First draft.', 'Second draft.')
        self.save(t)
        self.ix.backfill()
        self.assertEqual([x['content'] for x in self.versions()], [V1, V2])

    def test_exit_plan_mode_rejected_then_approved(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.exit_plan('x1', self.plan_path(), V1, approved=False)
        t.write('w2', self.plan_path(), V2)
        t.exit_plan('x2', self.plan_path(), V2, approved=True)
        self.save(t)
        self.ix.backfill()
        v = self.versions()
        # Same text as the Write dedups into one row; approval is merged onto it.
        self.assertEqual([(x['content'], x['source'], x['approved']) for x in v],
                         [(V1, 'write', 0), (V2, 'write', 1)])

    def test_exit_plan_mode_alone_is_a_version(self):
        t = Transcript(SID, slug='demo-plan').exit_plan('x1', self.plan_path(), V1)
        self.save(t)
        self.ix.backfill()
        v = self.versions()
        self.assertEqual([(x['source'], x['approved']) for x in v], [('exit_plan_mode', 1)])

    def test_rejected_then_same_text_approved_is_approved(self):
        t = Transcript(SID, slug='demo-plan')
        t.exit_plan('x1', self.plan_path(), V1, approved=False)
        t.exit_plan('x2', self.plan_path(), V1, approved=True)
        self.save(t)
        self.ix.backfill()
        self.assertEqual([x['approved'] for x in self.versions()], [1])

    def test_plan_edited_in_approval_dialog(self):
        t = Transcript(SID, slug='demo-plan')
        t.exit_plan('x1', self.plan_path(), V1, approved=True, final_plan=V2)
        self.save(t)
        self.ix.backfill()
        v = {x['content']: x['approved'] for x in self.versions()}
        self.assertEqual(v, {V1: None, V2: 1})

    def test_plan_mode_attachment_and_slug_only_session_are_referenced(self):
        t = Transcript(SID, slug='demo-plan').plan_mode(self.plan_path()).user('hello there!!')
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.links(), {(SID, 'referenced'): (1, 2)})
        plan = self.conn.execute("SELECT * FROM plans WHERE slug='demo-plan'").fetchone()
        self.assertEqual(plan['path'], self.plan_path())
        self.assertEqual(plan['on_disk'], 0)

    def test_subagent_write_links_parent_session(self):
        t = Transcript(SID, slug='demo-plan', sidechain=True)
        t.write('w1', self.plan_path(), V1)
        t.write_to(self.proj / SID / 'subagents' / 'agent-abc.jsonl')
        self.ix.backfill()
        self.assertEqual(self.links(), {(SID, 'subagent'): (1, 1)})
        self.assertEqual(self.versions()[0]['session_id'], SID)

    def test_windows_crlf_text_is_normalised(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1.replace('\n', '\r\n'))
        self.save(t)
        self.ix.backfill()
        self.assertEqual(self.versions()[0]['content'], V1)

    def test_fts_finds_every_version(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        self.save(t)
        self.ix.backfill()
        hits = self.conn.execute(
            "SELECT rowid, slug FROM plan_content WHERE plan_content MATCH 'first'").fetchall()
        self.assertEqual([h['slug'] for h in hits], ['demo-plan'])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM plan_content WHERE plan_content MATCH 'draft'").fetchone()[0], 2)


class TestDiskSnapshot(PlanTestCase):
    def test_disk_matching_latest_adds_no_version(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        self.save(t)
        Path(self.plan_path()).write_bytes(V1.replace('\n', '\r\n').encode())  # CRLF on disk
        stats = self.ix.backfill()
        self.assertEqual(len(self.versions()), 1)
        self.assertEqual(stats['on_disk'], 1)
        self.assertEqual(self.conn.execute(
            "SELECT on_disk FROM plans WHERE slug='demo-plan'").fetchone()[0], 1)

    def test_disk_edited_by_hand_is_a_new_version(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        self.save(t)
        Path(self.plan_path()).write_text(V1 + "\nHand-written note.\n", encoding='utf-8')
        self.ix.backfill()
        v = self.versions()
        self.assertEqual([x['source'] for x in v], ['write', 'disk'])
        self.assertIsNone(v[1]['session_id'])
        self.assertIsNone(v[1]['transcript_path'])

    def test_disk_only_plan_and_deleted_plan(self):
        Path(self.plan_path('orphan')).write_text('# Orphan\n', encoding='utf-8')
        t = Transcript(SID, slug='gone-plan').write('w1', self.plan_path('gone-plan'), V1)
        self.save(t)
        self.ix.backfill()
        rows = {r['slug']: r['on_disk'] for r in self.conn.execute("SELECT * FROM plans")}
        self.assertEqual(rows, {'orphan': 1, 'gone-plan': 0})
        self.assertEqual(self.versions('gone-plan')[0]['content'], V1)
        self.assertEqual(self.conn.execute(
            "SELECT title FROM plans WHERE slug='orphan'").fetchone()[0], 'Orphan')

    def test_disk_snapshot_never_displaces_transcript_citation(self):
        Path(self.plan_path()).write_text(V1, encoding='utf-8')
        os.utime(self.plan_path(), (0, 0))  # mtime far earlier than the transcript
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        self.save(t)
        self.ix.backfill()
        self.ix.backfill()
        v = self.versions()
        self.assertEqual(len(v), 1)
        self.assertEqual(v[0]['source'], 'write')


class TestIncremental(PlanTestCase):
    def counts(self):
        return tuple(self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                     for t in ('plans', 'plan_versions', 'plan_sessions', 'plan_content'))

    def test_backfill_is_idempotent(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        t.exit_plan('x1', self.plan_path(), V2)
        self.save(t)
        self.ix.backfill()
        first = self.counts()
        self.ix.backfill()
        self.assertEqual(self.counts(), first)

    def test_unchanged_transcript_is_skipped(self):
        self.save(Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1))
        self.ix.index_incremental()
        stats = self.ix.index_incremental()
        self.assertEqual((stats['transcripts'], stats['skipped']), (0, 1))

    def test_resume_after_append_rebuilds_edit_from_db(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        path = self.save(t)
        self.ix.index_incremental()
        self.assertEqual(self.conn.execute(
            "SELECT last_line FROM plan_scan").fetchone()[0], 2)
        t.edit('e1', self.plan_path(), 'First draft.', 'Second draft.')
        t.write_to(path)
        self.ix.index_incremental()
        self.assertEqual([x['content'] for x in self.versions()], [V1, V2])

    def test_watermark_waits_for_pending_result(self):
        t = Transcript(SID, slug='demo-plan').write('w1', self.plan_path(), V1)
        t.exit_plan('x1', self.plan_path(), V2, approved=None)  # result not written yet
        path = self.save(t)
        self.ix.index_incremental()
        self.assertEqual(self.conn.execute("SELECT last_line FROM plan_scan").fetchone()[0], 2)
        self.assertEqual(self.versions()[-1]['approved'], None)

        t.tool_result('x1', True, {'plan': V2, 'isAgent': False}, 'User has approved your plan.')
        t.write_to(path)
        self.ix.index_incremental()
        self.assertEqual(self.versions()[-1]['approved'], 1)
        self.assertEqual(self.conn.execute("SELECT last_line FROM plan_scan").fetchone()[0], 4)

    def test_pending_write_is_picked_up_later(self):
        t = Transcript(SID, slug='demo-plan')
        t.tool_use('w1', 'Write', {'file_path': self.plan_path(), 'content': V1})
        path = self.save(t)
        self.ix.index_incremental()
        self.assertEqual(self.versions(), [])
        t.tool_result('w1', True, {'type': 'create', 'content': V1})
        t.write_to(path)
        self.ix.index_incremental()
        self.assertEqual([x['source'] for x in self.versions()], ['write'])

    def test_earlier_occurrence_takes_the_citation(self):
        self.ix.record_version('demo-plan', V1, 'write', session_id=SID2,
                               captured_at='2026-09-02T00:00:00.000Z')
        self.ix.record_version('demo-plan', V1, 'exit_plan_mode', session_id=SID,
                               captured_at='2026-09-01T00:00:00.000Z', approved=0)
        v = self.versions()
        self.assertEqual(len(v), 1)
        self.assertEqual((v[0]['session_id'], v[0]['source'], v[0]['approved']),
                         (SID, 'exit_plan_mode', 0))


if __name__ == '__main__':
    unittest.main()
