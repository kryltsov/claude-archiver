import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from session_index import hooks as H

EXE = '.exe' if os.name == 'nt' else ''

UPSTREAM = {
    'cleanupPeriodDays': 3650,
    'hooks': {
        'UserPromptSubmit': [{'matcher': '', 'hooks': [
            {'type': 'command', 'command': 'session-topic-capture UserPromptSubmit'}]}],
        'PreCompact': [{'matcher': '', 'hooks': [
            {'type': 'command', 'command': 'session-topic-capture PreCompact'}]}],
        'SessionEnd': [{'matcher': '', 'hooks': [
            {'type': 'command', 'command': 'session-topic-capture SessionEnd'}]}],
    },
    'theme': 'dark',
}


def quiet(fn, *a, **kw):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = fn(*a, **kw)
    return rc, out.getvalue()


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.scripts = self.root / 'venv' / 'Scripts'
        self.scripts.mkdir(parents=True)
        for name in ('archiver-plan-capture', 'archiver-index'):
            (self.scripts / f'{name}{EXE}').write_text('')
        self.expected = H.expected_entries(self.scripts)
        self.settings = self.root / 'settings.json'
        # hooks_command() reads the real install's scripts directory; point it at
        # the fixture so the test doesn't depend on this machine.
        self._real_scripts_dir = H.scripts_dir
        H.scripts_dir = lambda: self.scripts

    def tearDown(self):
        H.scripts_dir = self._real_scripts_dir
        self._tmp.cleanup()

    def installed(self, data):
        return H.with_install(data, self.expected)


class TestInstall(SettingsCase):
    def test_into_empty_settings(self):
        new = self.installed({})
        self.assertEqual(set(new['hooks']), {'PostToolUse', 'PreCompact', 'SessionEnd'})
        cmd = new['hooks']['SessionEnd'][0]['hooks'][0]['command']
        self.assertTrue(cmd.endswith(f'archiver-index{EXE} --hook SessionEnd'))
        self.assertNotIn('\\', cmd)
        self.assertEqual(new['hooks']['PostToolUse'][0]['matcher'], H.PLAN_CAPTURE_MATCHER)

    def test_next_to_upstream_entries(self):
        new = self.installed(UPSTREAM)
        for event in ('PreCompact', 'SessionEnd'):
            self.assertEqual(new['hooks'][event][0], UPSTREAM['hooks'][event][0])
            self.assertEqual(new['hooks'][event][1], self.expected[event])
        self.assertEqual(new['hooks']['UserPromptSubmit'], UPSTREAM['hooks']['UserPromptSubmit'])
        self.assertEqual((new['theme'], new['cleanupPeriodDays']), ('dark', 3650))

    def test_already_installed_is_no_change(self):
        once = self.installed(UPSTREAM)
        self.assertEqual(self.installed(once), once)

    def test_backslash_entry_replaced_in_place_and_duplicates_dropped(self):
        data = json.loads(json.dumps(UPSTREAM))
        old = {'matcher': '', 'hooks': [{'type': 'command', 'command':
               'E:\\old\\venv\\Scripts\\archiver-index.exe --hook SessionEnd'}]}
        other = {'matcher': '', 'hooks': [{'type': 'command', 'command': 'someone-else'}]}
        data['hooks']['SessionEnd'] = [old, UPSTREAM['hooks']['SessionEnd'][0], old, other]
        new = self.installed(data)['hooks']['SessionEnd']
        self.assertEqual(new, [self.expected['SessionEnd'],
                               UPSTREAM['hooks']['SessionEnd'][0], other])

    def test_path_with_space_is_quoted_and_parsed_back(self):
        d = Path('C:/Users/Jane Doe/venv/Scripts')
        cmd = H.expected_entries(d)['SessionEnd']['hooks'][0]['command']
        self.assertTrue(cmd.startswith('"C:/Users/Jane Doe/'))
        self.assertTrue(cmd.endswith('" --hook SessionEnd'))
        self.assertEqual(H._command_exe(cmd), f'C:/Users/Jane Doe/venv/Scripts/archiver-index{EXE}')


class TestUninstall(SettingsCase):
    def test_install_then_uninstall_restores_upstream_exactly(self):
        back = H.with_uninstall(self.installed(UPSTREAM))
        self.assertEqual(json.dumps(back), json.dumps(UPSTREAM))  # same content and order

    def test_uninstall_without_entries_is_no_change(self):
        self.assertEqual(H.with_uninstall(UPSTREAM), UPSTREAM)
        self.assertEqual(H.with_uninstall({}), {})


class TestStatus(SettingsCase):
    def test_healthy(self):
        rows, problems = H.status_report(self.installed(UPSTREAM), self.scripts)
        self.assertEqual(len(rows), 3)
        self.assertEqual(problems, [])

    def test_flags_each_problem(self):
        data = self.installed(UPSTREAM)
        hooks = data['hooks']
        del hooks['PostToolUse']
        hooks['PreCompact'][1]['hooks'][0]['command'] = \
            'E:\\work\\x\\.venv\\Scripts\\archiver-index.exe --hook PreCompact'
        hooks['SessionEnd'][1]['hooks'][0]['command'] = \
            f'{self.root.as_posix()}/has space/archiver-index{EXE} --hook SessionEnd'
        hooks['SessionEnd'].append(self.expected['SessionEnd'])
        _, problems = H.status_report(data, self.scripts)
        text = '\n'.join(problems)
        self.assertIn('PostToolUse: not installed', text)
        self.assertIn('PreCompact: backslashes', text)
        self.assertIn('SessionEnd: path with a space is not quoted', text)
        self.assertIn('executable not found', text)
        self.assertIn('SessionEnd: installed 2 times', text)

    def test_flags_other_install(self):
        other = self.root / 'other' / 'Scripts'
        other.mkdir(parents=True)
        for name in ('archiver-plan-capture', 'archiver-index'):
            (other / f'{name}{EXE}').write_text('')
        data = H.with_install(UPSTREAM, H.expected_entries(other))
        _, problems = H.status_report(data, self.scripts)
        self.assertEqual(len(problems), 3)
        self.assertTrue(all('different install' in p for p in problems))


class TestCommandAndFiles(SettingsCase):
    def test_dry_run_writes_nothing_and_apply_backs_up(self):
        self.settings.write_text(json.dumps(UPSTREAM, indent=2), encoding='utf-8')
        before = self.settings.read_bytes()
        rc, out = quiet(H.hooks_command, 'install', settings=str(self.settings))
        self.assertEqual(rc, 0)
        self.assertIn('Dry run', out)
        self.assertEqual(self.settings.read_bytes(), before)

        rc, out = quiet(H.hooks_command, 'install', apply=True, settings=str(self.settings))
        self.assertEqual(rc, 0)
        backups = sorted(self.root.glob('settings.json.bak-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assertEqual(len(json.loads(self.settings.read_text())['hooks']['SessionEnd']), 2)

        rc, out = quiet(H.hooks_command, 'install', apply=True, settings=str(self.settings))
        self.assertIn('No change', out)
        self.assertEqual(len(list(self.root.glob('settings.json.bak-*'))), 1)

    def test_backups_never_overwrite(self):
        self.settings.write_text('{"a": 1}', encoding='utf-8')
        b1 = H.write_settings(self.settings, {'a': 2})
        b2 = H.write_settings(self.settings, {'a': 3})
        self.assertNotEqual(b1, b2)
        self.assertEqual(json.loads(b1.read_text()), {'a': 1})
        self.assertEqual(json.loads(b2.read_text()), {'a': 2})

    def test_invalid_settings_json_is_left_alone(self):
        self.settings.write_text('{"hooks": {,}}', encoding='utf-8')
        rc, out = quiet(H.hooks_command, 'install', apply=True, settings=str(self.settings))
        self.assertEqual(rc, 1)
        self.assertIn('Nothing changed', out)
        self.assertEqual(self.settings.read_text(encoding='utf-8'), '{"hooks": {,}}')


if __name__ == '__main__':
    unittest.main()
