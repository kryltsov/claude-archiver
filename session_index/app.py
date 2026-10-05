"""App identity: the name, command name and default data home, in one place.

This install runs as "archiver", in parallel with upstream claude-session-index.
For an upstream PR, only this module and [project.scripts] change back:
    APP_NAME = "claude-session-index", CLI_NAME = "sessions",
    ENV_PREFIX = "SESSION_INDEX", DEFAULT_HOME = ~/.session-index,
    and the two hook script names below.
"""

import os
from pathlib import Path

APP_NAME = "archiver"
CLI_NAME = "archiver"
ENV_PREFIX = "ARCHIVER"
DEFAULT_HOME = Path.home() / ".archiver"

# Console scripts the Claude Code hooks call (see [project.scripts]). `hooks install`
# recognises its own settings.json entries by these names.
PLAN_CAPTURE_SCRIPT = "archiver-plan-capture"
INDEX_SCRIPT = "archiver-index"

# Upstream's DB, read-only source for `init --from-upstream`.
UPSTREAM_DB = Path.home() / ".session-index" / "sessions.db"


def data_home() -> Path:
    """Data home: env <PREFIX>_HOME, else DEFAULT_HOME. Holds DB, config, state, logs."""
    val = os.environ.get(f"{ENV_PREFIX}_HOME")
    return Path(val).expanduser() if val else DEFAULT_HOME
