"""Claude Code hook handlers for the archiver. Both always exit 0.

- archiver-plan-capture (PostToolUse, matcher Write|Edit|MultiEdit|ExitPlanMode):
  records the plan version a tool call just produced. Anything that isn't a
  plan file returns before sqlite is even imported.
- archiver-index --hook <Event> (PreCompact / SessionEnd): reindexes that one
  session (folders), runs the plan pass on its transcripts, imports upstream topics.

Neither writes topic files or statusline output. Errors go to <data home>/hooks.log.
"""

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import config
except ImportError:
    import config

LOG_MAX_BYTES = 1_000_000


def log(msg: str):
    try:
        path = config.DATA_HOME / 'hooks.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
            path.replace(path.with_suffix('.log.1'))
        with open(path, 'a', encoding='utf-8') as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def read_payload() -> dict:
    try:
        raw = sys.stdin.buffer.read().decode('utf-8', errors='replace')
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _connect():
    import sqlite3
    conn = sqlite3.connect(str(config.get_db_path()), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# --- PostToolUse: plan capture ------------------------------------------------

def capture_plan(payload: dict, conn, plans_dir) -> str:
    """Record the plan version produced by one tool call. Returns what happened."""
    try:
        from .plans import PlanIndexer, slug_for_path, create_schema, EDIT_TOOLS
    except ImportError:
        from plans import PlanIndexer, slug_for_path, create_schema, EDIT_TOOLS

    tool = payload.get('tool_name')
    inp = payload.get('tool_input') or {}
    resp = payload.get('tool_response')
    if not isinstance(inp, dict):
        return 'skip'
    if tool == 'ExitPlanMode':
        path = inp.get('planFilePath') or (resp.get('filePath') if isinstance(resp, dict) else None)
    elif tool in EDIT_TOOLS:
        path = inp.get('file_path')
    else:
        return 'skip'
    slug = slug_for_path(path, plans_dir)
    if not slug and tool == 'ExitPlanMode':
        slug = payload.get('slug')
    if not slug:
        return 'skip'

    if tool == 'ExitPlanMode':
        # PostToolUse only fires when the tool ran, i.e. the plan was approved.
        text = resp.get('plan') if isinstance(resp, dict) else None
        if not isinstance(text, str):
            text = inp.get('plan')
        source, approved = 'exit_plan_mode', 1
    else:
        text = None
        try:
            text = Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError:
            if tool == 'Write':
                text = inp.get('content')
        source, approved = ('write' if tool == 'Write' else 'edit'), None
    if not isinstance(text, str) or not text.strip():
        return 'empty'

    create_schema(conn)
    sid = payload.get('session_id')
    ix = PlanIndexer(conn, plans_dir)
    ix.record_version(slug, text, source, session_id=sid,
                      transcript_path=payload.get('transcript_path'),
                      captured_at=_now_iso(), approved=approved, path=path)
    if sid:
        role = 'subagent' if payload.get('agent_id') else 'author'
        ix._link(slug, sid, role, None, None)
    ix._finish()
    return f'{source} {slug}'


def plan_capture_main() -> int:
    t0 = time.monotonic()
    payload = read_payload()
    try:
        tool = payload.get('tool_name')
        if tool not in ('Write', 'Edit', 'MultiEdit', 'ExitPlanMode'):
            return 0
        conn = _connect()
        try:
            result = capture_plan(payload, conn, config.get_plans_dir())
        finally:
            conn.close()
        if result != 'skip':
            log(f"plan-capture {result} ({time.monotonic() - t0:.2f}s)")
    except Exception:
        log("plan-capture error: " + traceback.format_exc().replace('\n', ' | '))
    return 0


# --- PreCompact / SessionEnd: one-session reindex -----------------------------

def index_hook(event: str) -> int:
    t0 = time.monotonic()
    payload = read_payload()
    try:
        try:
            from .indexer import SessionIndexer
            from .upstream import import_topics
        except ImportError:
            from indexer import SessionIndexer
            from upstream import import_topics

        transcript = payload.get('transcript_path')
        sid = payload.get('session_id')
        indexer = SessionIndexer()
        indexer.connect()
        try:
            path = Path(transcript) if transcript else None
            if path is None and sid:
                path = indexer._find_session_file(sid)
            parts = [event]
            if path and path.exists():
                ok = indexer.index_session(file_path=str(path))
                subagents = sorted((path.parent / path.stem / 'subagents').glob('*.jsonl'))
                st = indexer._plan_indexer().index_incremental(paths=[path, *subagents])
                parts += [f"session {'ok' if ok else 'FAILED'} {path.stem[:8]}",
                          f"plans +{st['versions_added']} ({st['transcripts']} transcripts)"]
            else:
                parts.append(f"no transcript for {sid}")
            added = import_topics(indexer.conn, config.get_upstream_db())
            parts.append(f"topics +{added}")
        finally:
            indexer.close()
        log(f"index-hook {' · '.join(parts)} ({time.monotonic() - t0:.2f}s)")
    except Exception:
        log(f"index-hook {event} error: " + traceback.format_exc().replace('\n', ' | '))
    return 0


# --- settings.json: hooks install / uninstall / status -----------------------
#
# Hook entries are written by an explicit command, never by pip install. Paths
# come from the running install, always with forward slashes (Claude Code runs
# hook commands through Git Bash on Windows, which strips backslashes) and
# quoted when they contain a space. Only the archiver's own entries are touched,
# recognised by script name; everything else in settings.json is left as is.

PLAN_CAPTURE_MATCHER = 'Write|Edit|MultiEdit|ExitPlanMode'
INDEX_EVENTS = ('PreCompact', 'SessionEnd')


def _app():
    try:
        from . import app
    except ImportError:
        import app
    return app


def default_settings_path() -> Path:
    import os
    base = os.environ.get('CLAUDE_CONFIG_DIR')
    return (Path(base).expanduser() if base else Path.home() / '.claude') / 'settings.json'


def scripts_dir() -> Path:
    """Where this install's console scripts live (venv, pipx or global)."""
    import sysconfig
    return Path(sysconfig.get_path('scripts'))


def script_path(name: str, directory: Path = None) -> Path:
    import os
    exe = f"{name}.exe" if os.name == 'nt' else name
    return (directory or scripts_dir()) / exe


def command_for(path: Path, args: str = '') -> str:
    p = path.as_posix()
    if ' ' in p:
        p = f'"{p}"'
    return f"{p} {args}".strip()


def expected_entries(directory: Path = None) -> dict:
    """event -> the one settings.json entry the archiver wants there."""
    app = _app()
    entries = {'PostToolUse': {
        'matcher': PLAN_CAPTURE_MATCHER,
        'hooks': [{'type': 'command', 'timeout': 10,
                   'command': command_for(script_path(app.PLAN_CAPTURE_SCRIPT, directory))}]}}
    for event in INDEX_EVENTS:
        entries[event] = {
            'matcher': '',
            'hooks': [{'type': 'command', 'timeout': 60,
                       'command': command_for(script_path(app.INDEX_SCRIPT, directory),
                                              f'--hook {event}')}]}
    return entries


def is_archiver_entry(entry) -> bool:
    app = _app()
    if not isinstance(entry, dict):
        return False
    return any(isinstance(h, dict) and (app.PLAN_CAPTURE_SCRIPT in str(h.get('command', ''))
                                        or app.INDEX_SCRIPT in str(h.get('command', '')))
               for h in entry.get('hooks') or [])


def load_settings(path: Path) -> dict:
    """{} when the file doesn't exist; ValueError when it isn't a JSON object."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def with_install(data: dict, entries: dict) -> dict:
    """Copy of data with each archiver entry replaced in place (or appended)."""
    new = json.loads(json.dumps(data))
    hooks = new.setdefault('hooks', {})
    for event, entry in entries.items():
        out, placed = [], False
        for e in hooks.get(event) or []:
            if not is_archiver_entry(e):
                out.append(e)
            elif not placed:  # first archiver entry: replaced where it stands
                out.append(entry)
                placed = True
            # later archiver entries are duplicates: dropped
        if not placed:
            out.append(entry)
        hooks[event] = out
    return new


def with_uninstall(data: dict) -> dict:
    """Copy of data without any archiver entry; events left empty are dropped."""
    new = json.loads(json.dumps(data))
    hooks = new.get('hooks')
    if not isinstance(hooks, dict):
        return new
    for event in list(hooks):
        if not isinstance(hooks[event], list):
            continue
        kept = [e for e in hooks[event] if not is_archiver_entry(e)]
        if len(kept) != len(hooks[event]):
            if kept:
                hooks[event] = kept
            else:
                del hooks[event]
    return new


def settings_diff(old: dict, new: dict, name: str = 'settings.json') -> str:
    import difflib
    a = json.dumps(old, indent=2, ensure_ascii=False).splitlines()
    b = json.dumps(new, indent=2, ensure_ascii=False).splitlines()
    return '\n'.join(difflib.unified_diff(a, b, name, f'{name} (proposed)', lineterm=''))


def write_settings(path: Path, new: dict):
    """Back up the existing file (never over an earlier backup), then write."""
    import shutil
    from datetime import date
    backup = None
    if path.exists():
        stem = f"{path.name}.bak-{_app().APP_NAME}-{date.today().isoformat()}"
        backup = path.with_name(stem)
        n = 2
        while backup.exists():
            backup = path.with_name(f"{stem}-{n}")
            n += 1
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return backup


def _command_exe(command: str) -> str:
    """The executable a hook command runs. Unquoted, it ends at our script name (not at
    the first space), so an unquoted path with a space is still read whole."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else command[1:]
    app = _app()
    for name in (app.PLAN_CAPTURE_SCRIPT, app.INDEX_SCRIPT):
        i = command.find(name)
        if i >= 0:
            end = i + len(name)
            if command[end:end + 4].lower() == '.exe':
                end += 4
            return command[:end]
    return command.split()[0] if command else ''


def status_report(data: dict, directory: Path = None) -> tuple[list[dict], list[str]]:
    """(installed archiver hooks, problems). No problems means healthy."""
    import os
    expected = expected_entries(directory)
    hooks = data.get('hooks') if isinstance(data.get('hooks'), dict) else {}
    rows, problems = [], []
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            if not is_archiver_entry(entry):
                continue
            for h in entry.get('hooks') or []:
                cmd = str(h.get('command', ''))
                exe = _command_exe(cmd)
                row = {'event': event, 'matcher': entry.get('matcher'), 'command': cmd,
                       'timeout': h.get('timeout'), 'problems': []}
                if '\\' in cmd:
                    row['problems'].append('backslashes in path (Git Bash strips them)')
                if ' ' in exe and not cmd.lstrip().startswith('"'):
                    row['problems'].append('path with a space is not quoted')
                if exe and not os.path.exists(exe.replace('\\', '/')):
                    row['problems'].append(f'executable not found: {exe}')
                if event in expected and cmd != expected[event]['hooks'][0]['command'] \
                        and not row['problems']:
                    row['problems'].append('points at a different install than this one')
                rows.append(row)
                problems += [f"{event}: {p}" for p in row['problems']]
    for event in expected:
        n = sum(1 for r in rows if r['event'] == event)
        if n == 0:
            problems.append(f"{event}: not installed")
        elif n > 1:
            problems.append(f"{event}: installed {n} times")
    return rows, problems


def hooks_command(action: str, apply: bool = False, settings: str = None) -> int:
    """CLI: <app> hooks install|uninstall|status [--apply] [--settings PATH]."""
    import os
    app = _app()
    path = Path(settings).expanduser() if settings else default_settings_path()
    try:
        data = load_settings(path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Can't read {path}: {e}. Nothing changed.", file=sys.stderr)
        return 1

    if action == 'status':
        rows, problems = status_report(data)
        print(f"\n🪝 {app.APP_NAME} hooks in {path}\n")
        for r in rows:
            print(f"  {r['event']:12s} {r['command']}")
            for p in r['problems']:
                print(f"  {'':12s} ⚠ {p}")
        if not rows:
            print("  (none installed)")
        print()
        if problems:
            print(f"  {len(problems)} problem(s). Fix with: {app.CLI_NAME} hooks install --apply\n")
            return 1
        print("  Healthy.\n")
        return 0

    new = with_install(data, expected_entries()) if action == 'install' else with_uninstall(data)
    if new == data:
        print(f"No change: {path} already has "
              f"{'the current' if action == 'install' else 'no'} {app.APP_NAME} hooks.")
        return 0
    print(settings_diff(data, new, path.name))
    if not apply:
        print(f"\nDry run. Re-run with --apply to write {path} (a backup is made first).")
        return 0
    if action == 'install':
        missing = [h['command'] for e in expected_entries().values() for h in e['hooks']
                   if not os.path.exists(_command_exe(h['command']))]
        if missing:
            print(f"Refusing: these scripts don't exist: {missing}", file=sys.stderr)
            return 1
    backup = write_settings(path, new)
    print(f"\nWritten {path}" + (f" (backup: {backup})" if backup else ''))
    print("Sessions already open keep their old hooks; new sessions pick these up.")
    return 0
