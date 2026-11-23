# Changelog

## Unreleased — archiver (fork of v0.3.1)

A fork of claude-session-index that runs next to it under its own name (`archiver`) and data home (`~/.archiver/`). Everything below is new unless it says otherwise.

### The whole conversation, kept through compaction

- **New `messages` table**: every user prompt and assistant reply in full, plus a one-line stub per tool call (`[Edit: path]`, `[Bash: command]`, max 200 chars). Tool results and full tool inputs are not stored. Rows are insert-only, keyed on `(session_id, uuid)`: reindexing never rewrites or deletes them, so a message survives even if its transcript is later rewritten or removed.
- **Full-text search covers every message.** Before, `session_content` held only the first 50 user prompts (capped at 100,000 chars), so anything said after roughly the first compaction couldn't be found, and assistant replies were never indexed.
- **Compaction is detected again.** Current Claude Code writes `system/compact_boundary` plus a user entry with `isCompactSummary`, not the old `type: "summary"` the indexer looked for, so `has_compaction` was 0 for every session. New `compaction_count` column; each message records its `compact_epoch`; each summary is stored as a `compact_summary` message and a `compaction_summary` topic (`Compaction N: <first line of the request>`).
- `context`, `search --context` and `synthesize` read the stored messages, and fall back to the JSONL only for sessions not indexed yet.
- Upgrading: a new `messages_scanned` flag makes the next `archiver index` re-read every transcript still on disk (once). Sessions whose JSONL is already gone keep their old rows unchanged.

### Plans

- **Every version of every plan** in `~/.claude/plans`, rebuilt from transcripts (including subagents): `Write` stores the text, `Edit` / `MultiEdit` are replayed onto the previous text, `ExitPlanMode` records the submitted plan and whether it was approved or rejected. The plans folder itself is snapshotted too, so hand edits are kept. Plans survive being overwritten or deleted.
- Each version cites the transcript and line it came from; identical texts are stored once.
- Plans are linked to the sessions that wrote (`author` / `subagent`) or touched (`referenced`) them.
- Commands: `plans`, `plan <slug> [--versions | --approved | --v N] [--raw]`, `plans-for <session>`, `plans export DIR`.
- New tables: `plans`, `plan_versions`, `plan_sessions`, `plan_content` (FTS5), `plan_scan`; view `plan_folders`.

### Folders and project groups

- Sessions now record where they ran: `cwd`, `project_root` (git root, else cwd), `git_common_dir` (groups worktrees), `git_branch`, and every folder used (`session_cwds`). The real path, not the lossy encoded directory name (`-Users-me-work-blog-site` → `E:\work\blog_site`).
- **Project groups** in config (`project_groups`): name a set of folders once, filter with `--group`. A folder can be in several groups. `archiver groups` reports each group's folders, sessions, plans and activity, and flags folders that don't exist.
- **Groups include git worktrees.** A member that is a repo's main folder also matches sessions in any worktree of that repo, wherever it lives, by the stored `git_common_dir`. Removed worktrees still match. A linked worktree or a subfolder listed as a member matches by path only. `archiver groups` lists the worktrees each member picked up. Re-indexing a session whose folder has since been deleted now keeps its stored `project_root` / `git_common_dir` instead of wiping the repo link. No re-index is needed to upgrade.
- `--project` and `project_name` are unchanged.

### Dates and search

- **`--from` / `--to`** (local days, inclusive) on `search`, `find`, `recent`, `analytics`, `list`, `plans`, `plan`.
- **One search over sessions and plans**: `--in sessions | plans | all`. It defaults to `all` with `--group` / `--project`, and to `sessions` (the old behaviour and output) otherwise. `search` and `recent` gain `--project`.
- `list [--json]`: sessions in scope with transcript path, folders, branch and times, for handing off to other tools.
- `plans export --check`: writes nothing, reports what an export would do, exits 1 when the folder is out of date (versions to write, stale files, or a missing `index.json`). Stale files are reported but never deleted.
- `plans export` writes provenance frontmatter into each `v<n>.md` (version, capture time, approval, session, `transcript:line`, sha256; `--plain` turns it off), stamps each file with the version's capture time rather than the export time, and leaves files whose text is already correct untouched — so a consumer whose watermark is a modification time sees only what's new.

### Hooks and setup

- `archiver hooks install | uninstall | status`: writes, removes or checks the archiver's own `settings.json` entries (diff first, `--apply` with a backup). Paths use forward slashes and are quoted when they contain a space.
- Hook handlers: `archiver-plan-capture` (PostToolUse) and `archiver-index --hook <event>` (PreCompact / SessionEnd). Both always exit 0 and log to `~/.archiver/hooks.log`.
- `archiver init --from-upstream [--force]`: one-time copy of claude-session-index's DB (backup API, source read-only). New hook topics are imported from it on every index run.
- One data home for DB, config, state and logs (`ARCHIVER_HOME`); env vars use the `ARCHIVER_` prefix.

### Changed behaviour

- **The date shortcuts use local days and the overlap rule.** `--date`, `--week`, `--days` and `analytics --week / --month` used to compare against the session's start time only — `--date` as a UTC date prefix. Now a session counts if it overlaps the range in your local time. More sessions match: on one real index, `find --date 2026-09-17` went from 3 sessions to 6, and `find --week` from 17 to 24. The missed ones had started before 10:00 local time (at UTC+10) or ran into the day from the one before.
- An unreadable `config.json` now prints a warning instead of being silently ignored.
- The indexer waits up to 10 s for a database lock instead of failing, so hooks and manual runs can overlap.

### Tests

- A `tests/` suite (stdlib `unittest`, 101 tests): fixture transcripts for each plan signal, folder changes inside a session, group path matching (subfolders, `\` vs `/`, drive case, `_` in names), date-overlap edges, hook handlers and `settings.json` handling.

## v0.3.1 — Stop titling everything "## Curation Data"

- **Smarter title auto-generation** — skips markdown headers, agent system prompts, and system caveats when picking a title from user messages. Tries up to 5 messages before giving up.
- Previously, 84+ sessions were titled "## Curation Data" and 20+ were titled "You are QA testing...". Now those get proper titles or null instead of garbage.

## v0.3.0 — Sessions have names now

The biggest annoyance is fixed: most sessions showed "(unnamed)" because only manually-titled sessions had display names. Now titles are auto-generated from compaction summaries or the first user message. Re-index with `sessions index --backfill` to see the difference.

- **Auto-generated session titles** — compaction summaries get parsed (including JSON blobs), and untitled sessions fall back to the first user message. No more walls of "(unnamed)".
- **`--days N` filter** — `sessions find --days 14` for arbitrary date ranges, not just `--week`
- **`--exclude-project` filter** — `sessions find --exclude-project "share memory"` to cut the noise
- **FTS5 crash fixes** — queries with periods (`CLAUDE.md`), hyphens (`session-index`), and reserved words (`index`) no longer crash. All search terms get quoted for safe literal matching.
- **CLI flag parsing fix** — `sessions "query" -n 5` now works correctly (the flag value was getting split from the flag)
- **`npx skills add` support** — skill moved to `skills/session-index/SKILL.md` to match the skills.sh registry convention. Install with `npx skills add lee-fuhr/claude-session-index`.
- **Conversational interface as primary UX** — README and skill rewritten to emphasize the natural language experience in Claude Code. The CLI is still there for power users.

## v0.2.0 — It looks good now

The output got a proper makeover. Conversations read like conversations. Analytics have visual hierarchy. And you don't have to set anything up anymore.

- **One command to rule them all** — `sessions` replaces the old `session-search` / `session-analyze` / `session-index` trio. Plain text defaults to search: `sessions "webhook debugging"` just works.
- Chat-like conversation display with 🧑/🤖 markers — you can actually tell who said what
- Box-drawing characters for session cards and exchange blocks
- Section headers with emoji in analytics (📊 📈 🔧 💬) for scannable output
- Cleaner search results with ◆ bullets and → resume commands
- Auto-indexing on first use — no more separate `--backfill` step, just run any command and it handles the rest
- Better stats display with visual structure instead of raw JSON
- The old commands still work if you prefer them

## v0.1.0 — Initial release

- Full-text search across all Claude Code sessions (SQLite + FTS5)
- Filter by client, project, tool, agent, tag, date
- Conversation context retrieval from session JSONL files
- Analytics: time per client, tool trends, session frequency, topic analysis
- Cross-session synthesis via Anthropic API (optional dependency)
- Live topic capture via Claude Code hooks (UserPromptSubmit, PreCompact, SessionEnd)
- Background indexing via macOS LaunchAgent
- Claude Code skill for natural language session queries
- Configurable paths via CLI flags, env vars, or config file
