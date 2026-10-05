---
name: archiver
description: Search, analyze, and synthesize across all your Claude Code sessions and plans — by product (project group) and date range. Ask "what did I try last time?" or "show me the plan we approved" and get answers with resume links.
version: 0.3.1+archiver
author: Lee Fuhr (claude-session-index); plans, groups and dates by Igor Kryltsov
tags:
  - session-search
  - session-history
  - plans
  - analytics
  - memory
  - productivity
requires:
  - python3
  - pip
---

# Archiver skill

You have access to the archiver — a SQLite database with FTS5 full-text search across all Claude Code sessions **and every version of every plan**. Use it whenever the user asks about past sessions, previous conversations, earlier plans, or what they've tried before.

**The user's interface is this conversation.** They ask naturally ("Didn't we discuss browser control recently?"), you translate to CLI commands, and present results conversationally. They should never need to know the CLI syntax.

## How to handle session queries

### 1. Extract keywords and scope from the question

The user says: "Didn't we discuss browser control recently?"
You search: `archiver "browser control" --from <a week or two ago>`

The user says: "What approaches have I tried for form automation on myapp?"
You search: `archiver "form automation" --group myapp` (run `archiver groups` first if you don't know the group names)

The user says: "What did the rebalancing plan say before I changed it?"
You run: `archiver "rebalancing" --in plans`, then `archiver plan <slug> --versions`

The user says: "How much time did I spend on Acme this week?"
You run: `archiver analytics --client "Acme" --week`

### 2. Run the right command via Bash

**Search** — sessions and plans by topic:
```bash
archiver "relevant keywords"
archiver "relevant keywords" --context             # includes conversation / plan excerpts
archiver "keywords" --group NAME                   # one product: sessions AND plans
archiver "keywords" --in plans                     # plans only (sessions | plans | all)
archiver "keywords" --from 2026-09-01 --to 2026-09-15
```

**Plans** — what was planned, what was approved:
```bash
archiver plans --group NAME                        # plans of a product, newest first
archiver plan <slug-or-prefix>                     # latest version
archiver plan <slug> --versions                    # history: approved / rejected per version
archiver plan <slug> --approved                    # the version the user approved
archiver plan <slug> --v 3 --raw                   # one version's plain text
archiver plans-for <session_id>                    # plans a session wrote
```

**Context** — read the actual conversation from a session:
```bash
archiver context <session_id> "search term"        # exchanges matching a term
archiver context <session_id>                      # all exchanges
```

**Analytics** — effort, time, tool usage:
```bash
archiver analytics                                 # overall
archiver analytics --group NAME --from 2026-09-01  # one product, since a date
archiver analytics --client "Acme"                 # per client
archiver analytics --week                          # last 7 days (--month: last 30)
```

**Filter** — find sessions by metadata:
```bash
archiver groups                                    # the project groups and their folders
archiver recent 20 --group NAME                    # last N sessions of a product
archiver find --client "Acme"                      # by client
archiver find --tool Task --week                   # by tool + date
archiver find --project myapp                      # by project name (substring)
archiver list --group NAME --json                  # sessions with transcript paths + folders
```

Scope flags (`--group` repeatable, `--project`, `--from`, `--to`) work the same on search, find, recent, analytics, list and plans. Dates are the user's local days, inclusive.

### 3. For synthesis ("what worked?", "what have I tried?")

This is the most valuable capability. When the user asks a question that spans multiple sessions:

1. Search: `archiver "topic" -n 10` (add `--group` / `--from` when the question names a product or a period)
2. For the top 3-5 results, extract context: `archiver context <id> "topic" -n 3`
3. If plans matched, read the approved one: `archiver plan <slug> --approved --raw`
4. Spawn a Task with `model="haiku"` to synthesize:
   - What approaches were tried?
   - What was planned vs. what was approved?
   - What worked / what failed?
   - Recurring patterns? Current state?
5. Present the synthesis conversationally with `claude --resume <id>` links for each source session

This uses an in-session Haiku subagent — no external API key needed.

### 4. Present results conversationally

Don't dump raw CLI output. Summarize:
- "You discussed browser control in 3 sessions last week..."
- "The plan went through 5 versions; you rejected the first two and approved v5..."
- Include `claude --resume <session_id>` links so they can jump back in
- If context is relevant, quote key exchanges or the relevant plan section

## Installation

From a clone of the repo, in its own venv:

```bash
python -m venv .venv && .venv/Scripts/pip install -e .    # macOS/Linux: .venv/bin/pip
archiver init --from-upstream   # optional: copy an existing claude-session-index DB
archiver hooks install --apply  # keep plans and sessions indexed while you work
```

First run of any command auto-indexes all existing sessions and plans.

## Data location

- **Database:** `~/.archiver/sessions.db`
- **Config:** `~/.archiver/config.json` (project groups live here)
- **Hook log:** `~/.archiver/hooks.log`
