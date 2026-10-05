"""Folder identity for sessions: path normalisation and git derivation.

A session's project_root is the git toplevel of its cwd (else the cwd itself),
normalised so paths compare equal regardless of '\\' vs '/' and drive case.
Group matching (scope.py) normalises member paths with the same function.
"""

import os
import subprocess
from typing import Optional

GIT_TIMEOUT = 5  # seconds per git call


def normalize_path(path: Optional[str]) -> Optional[str]:
    """'E:\\work\\Foo\\' → 'e:/work/Foo'. Only the drive letter is lower-cased."""
    if not path:
        return None
    p = path.replace('\\', '/')
    if len(p) >= 2 and p[1] == ':':
        p = p[0].lower() + p[1:]
    stripped = p.rstrip('/')
    if not stripped:
        return '/'
    if len(stripped) == 2 and stripped[1] == ':':
        return stripped + '/'  # bare drive keeps its slash: 'e:/'
    return stripped


def _git(cwd: str, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ['git', *args], cwd=cwd, capture_output=True, text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def derive_roots(cwd: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Return (project_root, git_common_dir) for a cwd, both normalised.

    Falls back to (cwd, None) when the folder is gone or isn't in a repo.
    """
    if not cwd:
        return None, None
    if not os.path.isdir(cwd):
        return normalize_path(cwd), None
    out = _git(cwd, 'rev-parse', '--show-toplevel', '--git-common-dir')
    if not out:
        return normalize_path(cwd), None
    lines = out.splitlines()
    if len(lines) < 2:
        return normalize_path(cwd), None
    toplevel, common = lines[0], lines[1]
    if not os.path.isabs(common):
        common = os.path.normpath(os.path.join(cwd, common))
    return normalize_path(toplevel), normalize_path(common)
