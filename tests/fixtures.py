"""Builders for fixture transcripts, shaped like real Claude Code JSONL lines."""

import json
from pathlib import Path


class Transcript:
    """Accumulates JSONL lines for one session; write() puts them on disk."""

    def __init__(self, session_id: str, slug: str = None, cwd: str = 'E:\\work\\demo',
                 sidechain: bool = False):
        self.session_id = session_id
        self.slug = slug
        self.cwd = cwd
        self.sidechain = sidechain
        self.lines: list[dict] = []
        self._n = 0

    def _ts(self) -> str:
        self._n += 1
        return f"2026-09-01T10:{self._n // 60:02d}:{self._n % 60:02d}.000Z"

    def _base(self, type_: str) -> dict:
        d = {'type': type_, 'sessionId': self.session_id, 'isSidechain': self.sidechain,
             'cwd': self.cwd, 'timestamp': self._ts()}
        d['uuid'] = f"{self.session_id[:8]}-{self._n:04d}"
        if self.slug:
            d['slug'] = self.slug
        return d

    def user(self, text: str) -> 'Transcript':
        d = self._base('user')
        d['message'] = {'role': 'user', 'content': text}
        self.lines.append(d)
        return self

    def reply(self, text: str) -> 'Transcript':
        d = self._base('assistant')
        d['message'] = {'role': 'assistant', 'model': 'claude-test',
                        'content': [{'type': 'text', 'text': text}]}
        self.lines.append(d)
        return self

    def compact(self, summary: str) -> 'Transcript':
        """A compaction as Claude Code records it: a boundary, then the summary."""
        d = self._base('system')
        d.update({'subtype': 'compact_boundary', 'content': 'Conversation compacted',
                  'compactMetadata': {'trigger': 'manual'}})
        self.lines.append(d)
        d = self._base('user')
        d.update({'isCompactSummary': True, 'isVisibleInTranscriptOnly': True,
                  'message': {'role': 'user', 'content': summary}})
        self.lines.append(d)
        return self

    def plan_mode(self, plan_path: str) -> 'Transcript':
        d = self._base('attachment')
        d['attachment'] = {'type': 'plan_mode', 'reminderType': 'full', 'isSubAgent': False,
                           'planFilePath': plan_path, 'planExists': False}
        self.lines.append(d)
        return self

    def tool_use(self, tool_id: str, name: str, inp: dict) -> 'Transcript':
        d = self._base('assistant')
        d['message'] = {'role': 'assistant', 'model': 'claude-test', 'content': [
            {'type': 'tool_use', 'id': tool_id, 'name': name, 'input': inp}]}
        self.lines.append(d)
        return self

    def tool_result(self, tool_id: str, ok: bool = True, tur=None,
                    text: str = 'ok') -> 'Transcript':
        d = self._base('user')
        item = {'type': 'tool_result', 'tool_use_id': tool_id, 'content': text}
        if not ok:
            item['is_error'] = True
        d['message'] = {'role': 'user', 'content': [item]}
        if tur is not None:
            d['toolUseResult'] = tur
        self.lines.append(d)
        return self

    # Convenience: a tool call plus its result, as Claude Code records them.

    def write(self, tool_id, path, content, ok=True):
        self.tool_use(tool_id, 'Write', {'file_path': path, 'content': content})
        tur = {'type': 'create', 'filePath': path, 'content': content,
               'structuredPatch': [], 'originalFile': None} if ok else 'Error: rejected'
        return self.tool_result(tool_id, ok, tur)

    def edit(self, tool_id, path, old, new, replace_all=False, ok=True):
        self.tool_use(tool_id, 'Edit', {'file_path': path, 'old_string': old,
                                        'new_string': new, 'replace_all': replace_all})
        tur = {'filePath': path, 'oldString': old, 'newString': new, 'originalFile': None,
               'replaceAll': replace_all} if ok else 'Error: rejected'
        return self.tool_result(tool_id, ok, tur)

    def exit_plan(self, tool_id, path, plan, approved=True, final_plan=None):
        self.tool_use(tool_id, 'ExitPlanMode', {'plan': plan, 'planFilePath': path})
        if approved is None:
            return self  # no result yet
        if approved:
            tur = {'plan': final_plan if final_plan is not None else plan,
                   'isAgent': False, 'filePath': path}
            return self.tool_result(tool_id, True, tur, 'User has approved your plan.')
        return self.tool_result(tool_id, False, "Error: The user doesn't want to proceed",
                                "The user doesn't want to proceed with this tool use.")

    def write_to(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            for d in self.lines:
                f.write(json.dumps(d) + '\n')
        return path
