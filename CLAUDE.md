# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run locally (dev/auth):**
```bash
pip install -r requirements.txt
python main.py
```

**Run in Docker (production):**
```bash
docker-compose up --build -d
docker-compose logs -f tether
```

**Stop:**
```bash
docker-compose down
```

**TEST_MODE** (fires all scheduled jobs 2 minutes after startup):
```
TEST_MODE=true  # set in .env
```

## Architecture

Core bot in `main.py`; task lifecycle history in `ledger.py` (SQLite at `LEDGER_DB`, default `data/ledger.db`); Belki task import in `belki_import.py`. Google Calendar remains the scheduling source of truth — the ledger only records history (created/completed/pushed/kept/deleted/nag-ignored) for retrospective stats.

**Belki import** (`belki_import.py`, `@Tether sync belki [project]` + auto-sync at startup and 09:00):
Reads monthly task files from `BELKI_PATH/Data/YYYY-MM.md` (Obsidian vault mount, read-only). Tasks are checkbox blocks with indented `key:: value` fields; only tasks with `project::` are importable. `estimate::` (integer evenings) drives capacity packing: each week holds `EVENINGS_PER_WEEK` (default 4) evenings, tasks pack onto Sundays in file order, a task without an estimate fills its whole week, a future `due::` is kept as a fixed date. Exactly one project is active at a time (`active_project` in ledger state) — set explicitly via `sync belki <name>`, never auto-picked. Sync is bidirectional: a task marked `- [x]` in Belki for the active project auto-completes and deletes its matching ⏰ calendar event (name match), so finishing work in Belki is enough — no separate `@Tether completed` needed.

**Request flow:**
1. Discord `on_message` fires when bot is `@mentioned`
2. `parse_intent_with_fallback()` sends the message to Gemini to produce a structured JSON command (schema defined in `INTENT_PARSER_PROMPT`)
3. High-confidence intents with known actions (`push`, `complete`, `keep`, `next`, `query`, `list`) are handled directly in Python
4. Everything else passes to a stateful Gemini chat session (`get_scheduler_chat`) which has tool access to the calendar functions

**Two separate Gemini roles:**
- **Intent parser** — stateless, one-shot call, returns structured JSON. Uses `INTENT_PARSER_PROMPT` (defined inline in `main.py`).
- **Scheduler session** — stateful multi-turn chat, has function-calling tools, uses `agent.md` as its system prompt.

**State in Calendar event descriptions:**
Tasks store metadata in a `[TETHER_META]` block embedded in the Google Calendar event description. `parse_meta()` / `build_meta()` handle serialization. Fields: `pushes`, `created_at`, `last_modified`, `origin`, `nag_ignored`, `last_push_reason`.

**Nag loop** (`send_overdue_nag`, fires every 30 min):
- Tracks `unacknowledged_overdue` (in-memory set of event IDs) across the session
- `midnight_nag_persist()` writes `nag_ignored` counts back into calendar metadata at midnight and resets the counter

**Priority engine** (`priority_score`, `rank_deadlines`):
Scores tasks by `days_until - (complexity * 2) - (pushes * 1.5)`. Complexity is keyword-inferred from the task title using three keyword lists.

**Model fallback chain:** `gemini-2.5-pro → gemini-2.5-flash → gemini-2.5-flash-lite → gemini-2.0-flash`

## Environment Variables

```env
GEMINI_API_KEY=
DISCORD_BOT_TOKEN=
DISCORD_USER_ID=
MORNING_BRIEFING_ENABLED=true   # optional, defaults true
TEST_MODE=false                 # optional, defaults false
EVENINGS_PER_WEEK=4             # optional, weekly capacity for Belki packing
BELKI_PATH=/app/belki           # optional, Belki vault mount
LEDGER_DB=data/ledger.db        # optional, SQLite ledger path
```

## Required Files (not in git)

- `.env` — environment variables
- `credentials.json` — Google OAuth2 desktop app credentials
- `token.json` — OAuth2 token (generated on first local run; mounted into Docker)

## Scheduled Jobs (production schedule, America/Toronto)

| Job | Schedule |
|-----|----------|
| `send_overdue_nag` | Every 30 min |
| `midnight_nag_persist` | 00:00 daily |
| `run_morning_jobs` | 09:00 daily |
| `eod_sweep` | 22:00 daily |
| `inactivity_check` | 17:00 daily |
| `weekly_queue_summary` | Sunday 09:00 |

## Key Invariants

- Tether-managed events always have the `⏰` prefix (`DEADLINE_PREFIX`). Fixed user events must never be touched.
- Pushes require a reason (`push_reason` must be non-null) — `validate_push_command` enforces this.
- Midnight `T00:00:00` times are hard-overridden to `T23:59:00` in `create_calendar_event` to prevent Gemini from assigning midnight deadlines.
- `pending_clarifications` dict tracks users mid-clarification flow; their next message bypasses the intent parser and routes directly to the scheduler session.
