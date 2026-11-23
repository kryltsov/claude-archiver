# archiver

**Every Claude Code session and every plan you've ever had, searchable in under a second — by product, by date.**

> **A fork of [lee-fuhr/claude-session-index](https://github.com/lee-fuhr/claude-session-index)** by Lee Fuhr (MIT, © 2026 Lee Fuhr), taken at commit [`ad2f2b2`](https://github.com/lee-fuhr/claude-session-index/commit/ad2f2b2) (v0.3.1). Everything that project does, this does too. See [What this fork changes](#what-this-fork-changes) for exactly what was kept, added and changed, and [NOTICE](NOTICE) for the attribution record.

You've built things across hundreds of sessions. Solved problems, hit walls, found workarounds, wrote plans and rewrote them. But sessions disappear into `~/.claude/projects/` as unlabeled JSONL files, and `~/.claude/plans/` only ever holds the *last* version of each plan. This tool indexes all of it into a fast SQLite database with full-text search, conversation retrieval, analytics, plan history and cross-session synthesis.

Ask "what did I try last time I debugged webhooks?" or "show me every version of the plan we approved for the rebalancer" and get an actual answer.

---

## Quick start

Not on PyPI yet. Install from a clone, in its own virtual environment:

```bash
git clone <this repo> archiver && cd archiver
python -m venv .venv
.venv/Scripts/pip install -e .        # macOS/Linux: .venv/bin/pip install -e .
```

Then, once:

```bash
archiver init --from-upstream         # only if you already use claude-session-index: copy its DB
archiver index                        # index sessions + plans (or just run any command)
archiver hooks install                # shows the settings.json diff; add --apply to write it
```

The first command you run auto-indexes everything if the database is empty. The hooks keep plans and sessions current while you work (see [Hooks](#hooks)).

Commands live in the venv (`.venv/Scripts/archiver`, or `.venv/bin/archiver`). Activate the venv or put that folder on your `PATH`.

Install the Claude Code skill so Claude can answer questions about your history:

```bash
cp skills/archiver/SKILL.md ~/.claude/skills/archiver.md
```

---

## How it works (for you)

The interface is your Claude Code conversation. You ask questions naturally, Claude translates them into searches, and gives you conversational answers.

### "Didn't we discuss browser control recently?"

Claude searches your sessions, finds the matches, and tells you:

> You discussed browser control in 3 sessions last week. The most relevant was on Jan 18 where you figured out that synthetic MouseEvent dispatch bypasses the framework's event handling. The key insight was that the submit button was a `<div>`, not a `<button>`.
>
> → `claude --resume a5b111c6-dca0-4ee9-b237-74b75baf13cd`

### "What have I tried for form automation? What actually worked?"

Claude searches across sessions, extracts the relevant conversations, and synthesizes:

> **Approaches tried:** element.click() → failed (framework intercepts). Coordinate-based clicking → failed (dynamic elements). Synthetic MouseEvent dispatch → success.
>
> **What worked:** Native OS-level clicking for all button interaction. Persistent browser profiles for session continuity.
>
> Sources: 5 sessions spanning Jan 10–Feb 1
> → `claude --resume abc123...`

### "What did the plan for phase 1 look like before I pushed back on it?"

> The plan went through 13 versions in one session on Sep 17. You rejected it three times (v1, v5, v11) and approved v13. The first version put commits straight on `main`; by v13 it had a `claude_plans/` folder and a traceability rule.
>
> → `archiver plan ship-the-retry-queue --v 1`

### "How did I spend my week on Acme?"

> 24 sessions since Monday across acme-web, acme-api and acme-infra. 41 hours total. 6 plans written, 5 approved.

Every answer includes `claude --resume` links so you can jump straight back into any session.

---

## What it does under the hood

1. **Indexes** all your Claude Code sessions into SQLite with FTS5 full-text search
2. **Indexes plans** — every version of every plan in `~/.claude/plans`, rebuilt from the transcripts (Write, Edit, ExitPlanMode) and the plans folder, with approved/rejected and a `transcript:line` citation for each version. Plans survive being overwritten or deleted
3. **Knows where each session ran** — its start folder, every folder it `cd`'d into, the git root, branch and worktree
4. **Searches** by content, client, project, **project group**, tool, agent, tag, or **date range** — sessions and plans, results in milliseconds
5. **Retrieves context** — actual conversation exchanges (user + assistant), not just metadata
6. **Analyzes** your usage — time per client, tool trends, session frequency, topic patterns
7. **Synthesizes** across sessions — "What approaches have I tried for X?" via in-session Haiku subagent (no extra API cost)
8. **Hands off** to other tools — `list --json` and `plans export` give scripts (e.g. writer) a product's sessions and plans across all its folders

---

## The CLI

The skill handles the conversational interface. From a terminal, everything goes through `archiver`:

```bash
# Search — just type what you're looking for
archiver "webhook debugging"
archiver "webhook" --context              # with conversation excerpts
archiver "webhook" --group myapp          # sessions AND plans of one product
archiver "webhook" --in plans             # plans only (sessions | plans | all)
archiver "webhook" --from 2026-09-01 --to 2026-09-15

# Plans
archiver plans --group myapp              # plans of a product, newest first
archiver plan <slug>                      # latest version (a unique slug prefix is enough)
archiver plan <slug> --versions           # every version: source, approved/rejected, citation
archiver plan <slug> --approved           # latest approved version
archiver plan <slug> --v 3 --raw          # one version's text, nothing else
archiver plans-for <session_id>           # plans a session wrote or touched
archiver plans export ./out --group myapp # <slug>/v<n>.md + index.json

# Project groups
archiver groups                           # each group: folders, sessions, plans, activity
archiver list --group myapp --json        # sessions + transcript paths + folders

# Browse a conversation
archiver context <id> "term"              # exchanges matching a term
archiver context <id>                     # all exchanges

# Analytics
archiver analytics                        # overall stats
archiver analytics --group myapp --from 2026-09-01
archiver analytics --client "Acme"        # per-client
archiver analytics --week                 # last 7 days (--month: last 30)

# Synthesis (requires anthropic package + API key for standalone use)
archiver synthesize "topic"               # cross-session intelligence

# Browse & filter
archiver recent 20 --group writer      # last N sessions
archiver find --client "Acme"             # filter by client
archiver find --tool Task --week          # filter by tool + date
archiver topics <session_id>              # topic timeline
archiver tools                            # top tools across sessions
archiver stats                            # database overview

# Indexing & setup
archiver index                            # index new/modified sessions and plans
archiver index --backfill                 # re-index everything
archiver init --from-upstream [--force]   # one-time copy of claude-session-index's DB
archiver hooks status | install | uninstall [--apply]
```

Plain text defaults to search — `archiver "webhook debugging"` just works, no subcommand needed.

**Scope flags** work the same on `search`, `find`, `recent`, `analytics`, `list` and `plans`:

| Flag | Meaning |
|---|---|
| `--group NAME` | Sessions in a project group's folders. Repeat for a union. |
| `--project X` | Substring match on the project name (unchanged from upstream). Combined with `--group`, it narrows inside the group. |
| `--from YYYY-MM-DD` | Sessions overlapping this local day or later. Plans with a version captured then or later. |
| `--to YYYY-MM-DD` | Same, this day or earlier. Both ends inclusive; either can be left out. |

`--date`, `--week`, `--days N` (on `find`) and `--week` / `--month` (on `analytics`) still work as shortcuts for a range. Mixing them with `--from` / `--to` is an error.

### CLI output

Search with a group shows sessions, then plans:

```
🔍 2 sessions for "retry budget" · group myapp

  ◆ 9521bc10 · harden the queue worker
    2026-08-09 · app · 327 exchanges · 133min
    "...the retry budget is shared across workers, so a hot shard can starve the others..."
    → claude --resume 9521bc10-2d14-4bc2-9f7c-1d616398934b

📝 2 plans for "retry budget" · group myapp

  ◇ calm-rolling-maple · Hold the retry budget on every path
    2 versions (2 matching) · ✓ approved · last 2026-08-09 · sessions 0118aa1f
    "# Hold the retry budget on every path  ## Context  `docs/retries.md` claims the budget is..."
    → archiver plan calm-rolling-maple
```

A plan's history:

```
╭─── Retry queue — phase 1 ───
│ ship-the-retry-queue-brave-otter · 13 versions · ✓ approved
│ C:\Users\me\.claude\plans\ship-the-retry-queue-brave-otter.md
│ session 4f1c8ab2 (author) · e:/work/writer · 2026-09-17
╰────────────────────────────────────────────────

  v1   2026-09-17 11:47  write          rejected  4f1c8ab2  13732B  4f1c8ab2-….jsonl:220
  v2   2026-09-17 11:49  edit           —         4f1c8ab2  15146B  4f1c8ab2-….jsonl:249
  …
  v13  2026-09-17 11:56  edit           approved  4f1c8ab2  17114B  4f1c8ab2-….jsonl:336
```

Conversation context shows the actual chat:

```
╭─── Build automation debugging ─────────────────
│ 2026-01-20 · my-project · 96 exchanges · 7min
│ → claude --resume a5b111c6-dca0-4ee9-b237-74b75baf13cd
╰────────────────────────────────────────────────

  ┌─ Jan 20, 19:14 ──────────────────────────────
  │
  │  🧑 Breakthrough session. Successfully submitted forms #32 and #33
  │     using synthetic MouseEvent dispatch to bypass the framework's
  │     event handling.
  │
  │  🤖 I'll process these findings. Let me search for existing patterns...
  │     [Grep: framework|zone\.js|MouseEvent|click]
  │     [Read: /path/to/automation/docs.md]
  │
  └────────────────────────────────────────────────
```

Tool calls get collapsed into readable one-liners — `[Read: path]`, `[Edit: path]`, `[Bash: command]`, `[Task: "description" → agent]` — so you can follow the conversation without drowning in JSON.

---

## What this fork changes

[claude-session-index](https://github.com/lee-fuhr/claude-session-index) is the original: a SQLite + FTS5 index over your Claude Code transcripts, with search, conversation retrieval, analytics and live topic capture. This fork starts from it at `ad2f2b2` (v0.3.1) and keeps all of that intact.

**Kept from the original, unchanged in behaviour**

| | |
|---|---|
| The index and its schema | `sessions`, `session_content` (FTS5), `session_topics`, `session_tools`, `session_agents` |
| Transcript indexing | titles (including auto-titles from compaction summaries), tools, agents, topics, durations |
| Search and retrieval | full-text search, `context` for real conversation exchanges, `tools`, `topics`, `stats` |
| Analytics and synthesis | time per client, tool trends, session frequency, the Haiku-subagent `synthesize` |
| Live topic capture | the `UserPromptSubmit` / `PreCompact` / `SessionEnd` hook, and the macOS LaunchAgent |
| Configuration layering | CLI flags, then environment variables, then a config file, then defaults |
| `--project` and `project_name` | same substring match, same derivation |
| The default search | with no scope flags, output is identical to the original's |

**Added by this fork**

| | |
|---|---|
| **Plan indexing** | every version of every plan in `~/.claude/plans`, rebuilt from transcripts (`Write`, `Edit`/`MultiEdit`, `ExitPlanMode`) and from the plans folder, with approved/rejected status and a `transcript:line` citation per version, linked to the sessions that wrote or touched them. Plans survive being overwritten or deleted |
| **Folder awareness** | each session's `cwd`, repository root, git common dir (worktrees) and branch, plus every folder a session worked in (`session_cwds`) |
| **Project groups** | one product spanning several folders, filtered with `--group` |
| **Date ranges** | `--from` / `--to` on every listing and searching command, matched by overlap rather than start time |
| **One search over both** | `--in sessions \| plans \| all`, with plans ranked in their own FTS table |
| **New commands** | `groups`, `list --json`, `plans`, `plan`, `plans-for`, `plans export`, `hooks install \| uninstall \| status` |
| **Side-by-side install** | its own command names, data home and hook entries, a one-time DB copy (`init --from-upstream`) and a read-only topic import, so the original keeps working untouched |
| **Tests** | a stdlib `unittest` suite; the original has none |

**Changed from the original**

| | |
|---|---|
| **The date shortcuts** | `--date`, `--week`, `--days N` and `analytics --week`/`--month` described a UTC comparison against a session's *start time*. They now describe a local day range matched by **overlap**, so they return more sessions: ones that started before the UTC day rolled over, and long ones that ran into the window. On one real index, `find --date <day>` went from 3 sessions to 6 and `find --week` from 20 to 23 |
| **Command and data names** | `sessions` becomes `archiver`, and `~/.session-index/` becomes `~/.archiver/` (`ARCHIVER_HOME`), so both can run at once |
| **An unreadable config** | was ignored silently, dropping every configured setting; now warns on stderr |
| **Database locking** | indexing waits for a lock instead of failing, so hooks and manual runs can overlap |
| **The skill** | renamed to `archiver` (`skills/archiver/SKILL.md`) so it can sit beside the original's skill |

Parts of this work have been offered back upstream: [claude-session-index#4](https://github.com/lee-fuhr/claude-session-index/pull/4).

## Plans

Claude Code writes each plan to `~/.claude/plans/<slug>.md` and overwrites it on every edit. The file only ever has the last version, and it can be deleted. The archiver keeps all of them:

- **From transcripts** (including subagent transcripts): each `Write` to a plan file is a version; each `Edit` / `MultiEdit` is rebuilt from the text before it; each `ExitPlanMode` records the submitted plan and whether you **approved or rejected** it. If you edit the plan in the approval dialog, the edited text is what's stored as approved.
- **From the plans folder**: a file whose text differs from every known version (e.g. edited by hand) is stored too.
- **Links**: every plan knows the sessions that wrote it (`author`, or `subagent`) or only touched it (`referenced`), and through them its folders and groups.
- Identical texts are stored once. Each version cites the transcript and line it came from — the `<file>:<line>` shape other tools can quote.

Version numbers (`v1`, `v2`, …) count over a plan's whole history, so a date filter or a repeat export never renumbers them.

## Project groups

One product is often several folders. Name them once in `~/.archiver/config.json`:

```json
{
  "project_groups": {
    "myapp":     ["E:/work/app", "E:/work/app-cli", "E:/work/shared-infra"],
    "writer": ["E:/work/writer", "E:/work/blog_site"],
    "blog":       ["E:/work/blog", "E:/work/blog_web", "E:/work/shared-infra"]
  }
}
```

- A session belongs to a member folder when its git root, **or any folder it worked in**, is that folder or sits under it. `\` vs `/` and drive-letter case don't matter.
- **Git worktrees come along automatically.** List a repo's main folder and the group also covers every `git worktree` of that repo, wherever it lives (`E:/work/app` covers a worktree at `E:/work/app-feature`). This still works after a worktree is removed: sessions are matched by the repo link recorded when they were indexed. To cover just one worktree, list that worktree's folder and not the main repo. A subfolder of a repo listed as a member covers only that subfolder.
- A folder can be in several groups. `--group a --group b` returns each session once.
- A plan belongs to the groups of the sessions linked to it.
- `archiver groups` shows each group's folders with session and plan counts and first/last activity, plus the worktrees each repo folder picked up. It flags a folder that doesn't exist (typo?) and one that exists but has no sessions yet.
- The config file must be valid JSON. If it isn't, every command warns and runs without groups.

## Dates

`--from` / `--to` take local calendar days, both inclusive. A session matches if it **overlaps** the range, so one that runs past midnight or over several days is never lost. A plan matches if any of its versions was captured in the range; `plan <slug> --versions --from D` lists just those versions.

Transcript timestamps are UTC; the archiver converts your local days to UTC before comparing. (The original matched a UTC date prefix on the start time, which at UTC+10 misses sessions started before 10:00 local and long sessions that ran into the day. See the CHANGELOG.)

## Handing off to other tools

The database is plain SQLite (`~/.archiver/sessions.db`). Other tools can read the tables directly (Node ≥ 22 has `node:sqlite` built in), or use the two file handoffs:

```bash
archiver list --group writer --json        # session_id, transcript_path, cwd, cwds,
                                              # project_root, git_common_dir, git_branch,
                                              # start_time, end_time, title
archiver plans export ./plans-out --group writer --from 2026-09-01
                                              # ./plans-out/<slug>/v<n>.md + index.json
```

Each `v<n>.md` opens with frontmatter carrying the same provenance — version number, capture time, `approval: approved | rejected | unknown`, session id, `cite: <transcript>:<line>`, sha256 — so a tool that only reads markdown still sees it. `--plain` writes the plan text alone. Frontmatter holds only facts about that version, so a version file never changes once written; the plan's current title and version count live in `index.json`. `index.json` repeats it all in one file, plus the scope and `exported_at`.

`--check` writes nothing and exits 1 when the folder is out of date (versions to write, stale files, or no `index.json`), so a script can refresh sources before reading them:

```bash
archiver plans export "$DIR" --group writer-lore --check ||   archiver plans export "$DIR" --group writer-lore --force
```

Each file's **modification time is the version's capture time**, not the time you ran the export, and a file whose text is already correct is left alone. A consumer whose watermark is a modification time (writer's `docs` source, for one) therefore sees only versions captured since it last looked, and re-exporting doesn't make the whole folder look new.

---

## Hooks

Three hook entries keep the index current while you work:

| Event | Command | What it does |
|---|---|---|
| `PostToolUse` (`Write\|Edit\|MultiEdit\|ExitPlanMode`) | `archiver-plan-capture` | Stores the plan version a tool call just produced. Any other file returns immediately. |
| `PreCompact`, `SessionEnd` | `archiver-index --hook <event>` | Re-reads that session (messages, folders, plans, subagents) and imports new topics. |

Don't hand-edit these into `settings.json`. Let the archiver do it:

```bash
archiver hooks install            # prints the diff against ~/.claude/settings.json
archiver hooks install --apply    # backs up settings.json, then writes
archiver hooks status             # checks what's installed; exit 1 on problems
archiver hooks uninstall --apply  # removes only the archiver's entries
```

- The commands use this install's absolute paths, **with forward slashes**. Claude Code runs hook commands through Git Bash on Windows, which strips backslashes: `E:\venv\Scripts\x.exe` becomes `E:venvScriptsx.exe: command not found`. Paths with spaces are quoted.
- Only the archiver's own entries are added, replaced or removed. The original's topic hooks and any others are left exactly as they are.
- Every archiver hook exits 0, logs to `~/.archiver/hooks.log`, and times out after 10 s (plan capture) or 60 s (index), so it can't block a session.
- Sessions already open keep the hooks they started with. New sessions pick up the change.

`hooks/settings-snippet.json` shows what gets written.

## Running next to claude-session-index

The archiver never writes to anything the original owns:

| | claude-session-index | archiver |
|---|---|---|
| commands | `sessions`, `session-*` | `archiver`, `archiver-*` |
| data | `~/.session-index/` | `~/.archiver/` (`ARCHIVER_HOME`) |
| hooks | its 3 topic entries | its own 3 entries, next to them |

- `archiver init --from-upstream` copies the original's database once (SQLite backup API, source opened read-only). After that, the archiver indexes transcripts itself.
- **Live topics** come from the original's `UserPromptSubmit` hook and can't be rebuilt from transcripts. Every `archiver index` and session-end hook imports new ones from the original's DB, read-only. Set `"upstream_db": ""` in the config to turn that off.
- The archiver has no topic hook of its own yet. Without the original installed, sessions still get titles and compaction-summary topics, but no live topic timeline.

### Background indexing (macOS)

```bash
cp launchagent/com.archiver-indexer.plist ~/Library/LaunchAgents/
# Edit the plist to point to the archiver venv's Python
launchctl load ~/Library/LaunchAgents/com.archiver-indexer.plist
```

Runs `archiver-index --incremental` every 30 minutes. Only processes new or modified sessions.

---

## Configuration

Works out of the box with sensible defaults. All paths are configurable.

### Priority order

1. CLI flags (`--db-path`, `--projects-dir`, `--settings`)
2. Environment variables (`ARCHIVER_HOME`, `ARCHIVER_DB`, `ARCHIVER_PROJECTS`, `ARCHIVER_TOPICS`, `ARCHIVER_PLANS`)
3. Config file (`~/.archiver/config.json`)
4. Defaults

### Default paths

| What | Default location |
|------|-----------------|
| Data home | `~/.archiver/` (env `ARCHIVER_HOME`) |
| Database | `~/.archiver/sessions.db` |
| Config | `~/.archiver/config.json` |
| Hook log | `~/.archiver/hooks.log` |
| Sessions | `~/.claude/projects/` |
| Plans | `~/.claude/plans/` |
| Topics | `~/.archiver/session-topics/` |
| Original's DB (topic import) | `~/.session-index/sessions.db` |

### Optional config file

```json
{
  "projects_dir": "~/.claude/projects",
  "plans_dir": "~/.claude/plans",
  "db_path": "~/.archiver/sessions.db",
  "upstream_db": "~/.session-index/sessions.db",
  "clients": ["Acme Corp", "Internal"],
  "project_names": {
    "-Users-me-projects-myapp": "My App"
  },
  "project_groups": {
    "myapp": ["~/projects/myapp", "~/projects/myapp-api"]
  }
}
```

- **`clients`** — Optional. If provided, sessions are auto-tagged with matching client names. If empty, client detection is skipped.
- **`project_names`** — Optional. Maps Claude's directory-based project names to friendly labels. If empty, auto-generates from directory names.
- **`project_groups`** — Optional. Named sets of folders; see [Project groups](#project-groups).
- **`upstream_db`** — The original's DB for the topic import. `""` turns it off.

---

## How it works (technically)

```
~/.claude/projects/          archiver                  Your conversation
  ├── -project-a/              ┌──────────┐
  │   ├── abc123.jsonl ──────▶│ SQLite   │◀──── "Didn't we discuss X?"
  │   └── abc123/subagents/ ─▶│ + FTS5   │◀──── "How'd I spend my week on myapp?"
  ├── -project-b/              └──────────┘◀──── "Show me the approved plan"
  │   └── ghi789.jsonl ──────▶     ▲   │
~/.claude/plans/*.md ─────────────┘   ▼
                            sessions.db
                          ┌─────────────────┐
                          │ sessions        │  metadata, timestamps, tools, cwd,
                          │                 │  project_root, git dir, branch
                          │ session_cwds    │  every folder a session worked in
                          │ messages        │  every prompt and reply, one-line tool calls
                          │ session_content │  FTS5 full-text index over messages
                          │ session_topics  │  live topic timeline
                          │ session_tools   │  tool usage per session
                          │ session_agents  │  agent invocations
                          │ plans           │  one row per plan slug
                          │ plan_versions   │  every version, citation, approval
                          │ plan_sessions   │  plan ↔ session (author/subagent/referenced)
                          │ plan_content    │  FTS5 over every plan version
                          │ plan_folders    │  view: a plan's folders via its sessions
                          └─────────────────┘
```

The indexer parses JSONL files once, extracts metadata (timestamps, tools, agents, topics, folders, plan signals), and stores everything in SQLite. Plan scans resume each transcript where the last one stopped. FTS5 handles the full-text search. Context retrieval reads the stored messages, and falls back to the JSONL only for a session that hasn't been indexed yet.

**Compaction loses nothing.** Claude Code appends a compaction boundary and a summary to the same transcript, leaving every earlier line in place. The `messages` table works the same way: every prompt and reply is kept in full, with a one-line stub per tool call (`[Edit: path]`, `[Bash: command]`). Rows are only ever inserted, so a message survives even if its transcript is later rewritten or deleted. Each row records which compaction segment it belongs to (`compact_epoch`). Each summary is stored as an extra row and as a `compaction_summary` topic. Tool results and full tool inputs stay in the JSONL only; `line_no` points back to them.

## Tech stack

- **Python 3.10+** — stdlib only for core features (no dependencies)
- **SQLite + FTS5** — fast full-text search, no server needed
- **Anthropic SDK** — optional, only for standalone `synthesize` command

Tests: `python -m unittest discover -s tests` (stdlib `unittest`, no extra packages).

---

## Requirements

- Python 3.10+
- Claude Code (the sessions to index)
- `git` on `PATH` (optional; used to find each session's repository root)
- That's it. No server, no database setup, no API keys for core features.

---

## Credits and license

The original is [claude-session-index](https://github.com/lee-fuhr/claude-session-index) by **[Lee Fuhr](https://leefuhr.com)** — the index, the search, the analytics and the conversational skill are his design and his code. This fork adds plan indexing, folder awareness, project groups, date ranges, the unified search and the hooks tooling.

MIT, both copyrights, in one file: [LICENSE](LICENSE). The attribution record, listing what was taken and what was added, is in [NOTICE](NOTICE). Upstream's commit history is preserved in this repository, so authorship stays visible in `git log`.
