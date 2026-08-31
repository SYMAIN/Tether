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

**Deploy to a cloud VM (Oracle Always Free):** see `DEPLOYMENT.md`.

**TEST_MODE** (fires all scheduled jobs 2 minutes after startup):
```
TEST_MODE=true  # set in .env
```

## Architecture

Core bot in `main.py`; task lifecycle history in `ledger.py` (SQLite at `LEDGER_DB`, default `data/ledger.db`); Belki task import in `belki_import.py`. Google Calendar remains the scheduling source of truth — the ledger only records history (created/completed/pushed/kept/deleted/nag-ignored) for retrospective stats.

**Belki import** (`belki_import.py`, `@Tether sync belki [project]` + auto-sync at startup, every 30 min via the nag cycle, and 09:00):
Reads monthly task files from `BELKI_PATH/Data/YYYY-MM.md` (Obsidian vault mount, read-only). Tasks are checkbox blocks with indented `key:: value` fields; only tasks with `project::` are importable. `estimate::` is integer evenings; a task without one fills its whole week, and a future `due::` is kept as a fixed date. Sync is bidirectional: a task marked `- [x]` in Belki auto-completes and deletes its matching ⏰ calendar event (name match) — for **every** project in the queue — so finishing work in Belki is enough, no separate `@Tether completed` needed.

**Project-deadline scheduling** (replaced the single sticky `active_project` on 2026-07-30):
Several projects run at once. `BELKI_PATH/projects.md` is a registry of `- <name>` bullets with `due::` and `status::` fields (an entry only counts if it carries at least one — the file's own header uses markdown bullets to document the fields). Each week's `EVENINGS_PER_WEEK` evenings are split across projects by **deadline pressure**: a project's required rate is its open evenings over the weeks left until `due::`, apportioned by largest-remainder, capped at what it actually has left, with the remainder going round-robin to undeadlined projects longest-untouched-first. A slice smaller than the project's next task is rounded up to a spendable size — otherwise that project imports nothing every week while still consuming its share.

The split is **computed once and pinned** for the week (`allocation:<sunday>` in ledger state, holding both `alloc` and `spent`). Spend is recorded at import, never re-derived from the calendar: completing a task deletes its event, so a queue-derived figure would drop and immediately hand the freed evening to the next task. That treadmill is what made per-task due dates meaningless. Finishing early leaves the week clear; a new week (or an explicit `sync belki <name>`, which hands the whole week to one project) recomputes.

Forecasting uses **measured output** — `ledger.velocity(project)` over completed rows — not the sum of open estimates. Tasks churn every session, so a burndown never converges and every project reads as doomed. The deadline sets the stakes; completion history sets the rate. `weekly_queue_summary` shows required vs observed per project and flags AT RISK.

Every imported event is stamped with the Belki task's `id::` as `belki_id` in `[TETHER_META]`. On each sync, any active-project task whose `id::` already matches a tracked event is reconciled by id (not name): a rename, a `description::` edit, or a due date that's newly gone fixed updates that event in place (`update_deadline_content`) instead of leaving the old event orphaned and importing a duplicate under the new name. A tracked `belki_id` that no longer appears anywhere in Belki (deleted outright, not checked off) is never auto-removed — just flagged in the sync reply for manual review, since a missing line is too ambiguous a signal to delete on.

Events created before `belki_id` tracking existed (pre-2026-07-07) carry no id at all, so the id lookup can never find them. Before the id-reconcile pass, a backfill step name-matches any such legacy event against a currently-untracked task's current title and stamps its `belki_id`/`estimate` in place — closing the gap prospectively for the *next* edit. It cannot recover an event that's already orphaned (title no longer matches anything current) — those need manual cleanup.

**Request flow:**
1. Discord `on_message` fires when bot is `@mentioned`
2. `parse_intent_with_fallback()` sends the message to Gemini to produce a structured JSON command (schema defined in `INTENT_PARSER_PROMPT`)
3. High-confidence intents with known actions (`push`, `complete`, `keep`, `next`, `query`, `list`) are handled directly in Python
4. Everything else passes to a stateful Gemini chat session (`get_scheduler_chat`) which has tool access to the calendar functions

**Two separate Gemini roles:**
- **Intent parser** — stateless, one-shot call, returns structured JSON. Uses `INTENT_PARSER_PROMPT` (defined inline in `main.py`).
- **Scheduler session** — stateful multi-turn chat, has function-calling tools, uses `agent.md` as its system prompt.

**State in Calendar event descriptions:**
Tasks store metadata in a `[TETHER_META]` block embedded in the Google Calendar event description. `parse_meta()` / `build_meta()` handle serialization. Fields: `pushes`, `created_at`, `last_modified`, `origin`, `nag_ignored`, `last_push_reason`, plus `estimate`, `belki_id`, `project` and `priority` on Belki-imported events. Events predating a field carry none; the reconcile pass in `belki_import.sync()` stamps `belki_id`/`project`/`priority` onto them in place (`needs_id` / `needs_project` / `priority_changed`) the first time their title still matches a live Belki task.

**Nag loop** (`send_overdue_nag`, fires every 30 min):
- Tracks `unacknowledged_overdue` (in-memory set of event IDs) across the session
- `midnight_nag_persist()` writes `nag_ignored` counts back into calendar metadata at midnight and resets the counter
- Nag pressure itself is priority-blind: every overdue event nags at the same cadence regardless of `priority_score` — that score only affects ranking (e.g. the morning briefing's "next up" pick), never whether/how loud something nags.
- Nothing in the nag/sync path calls Gemini — `run_belki_sync()` is pure Python (file parsing + calendar CRUD). Only reactive `@Tether` messages and the scheduler chat touch the Gemini API, so nag frequency cannot exhaust its quota.
- **Quiet hours:** the job still fires every 30 min year-round (Belki reconciliation runs every tick), but between `NIGHT_START_HOUR`/`NIGHT_END_HOUR` (23:00-08:00 Toronto, `main.py`) the nag DM itself only sends on the top of the hour (`_in_quiet_hours` + `now.minute >= 30` check) — added 2026-08-12 so overdue-task pressure doesn't buzz the phone every 30 min overnight.
- **`keep` actually suppresses nagging now.** Before 2026-08-11, `handle_keep()` discarded the event from `unacknowledged_overdue`, but `send_overdue_nag()` unconditionally re-added every currently-overdue event on its very next cycle — so the discard was wiped out within 30 minutes and push/complete were the only things that ever stopped the nagging, despite `keep` existing specifically to be a third option. Fixed with `kept_today` (in-memory set): `send_overdue_nag()` skips re-adding any event in it, `handle_keep()` adds to it, and `midnight_nag_persist()` (plus `on_ready()` on a fresh process start) clears it — so a kept task goes quiet for the rest of the day and resumes normal pressure tomorrow if still unresolved, rather than either nagging forever or going silent permanently.

**Priority engine** (`priority_score`, `rank_deadlines`, `priority_reason`):
Scores tasks by `days_until - (weight * 2) - (pushes * 1.5)`. For Belki-imported tasks, `weight` comes from the task's own `priority::` tag (`ledger.belki_priority_weight`, P1=4 .. P4=1) — the user's explicit call on urgency, which takes precedence. Only ad-hoc (non-Belki) tasks, which carry no `priority::`, fall back to `weight = infer_complexity(title)` (keyword-guessed from three keyword lists in `ledger.py`). Before 2026-08-11, Belki's `priority::` was parsed on import and silently discarded — every task's urgency was keyword-guessed from its title regardless of its actual tag, so e.g. a P4 task whose title happened to contain "build"/"project"/"app"/etc. outranked a P2 task that didn't.

**Model fallback chain:** `gemini-2.5-flash → gemini-2.5-flash-lite → gemini-2.0-flash`. `gemini-2.5-pro` was dropped 2026-08-11 after Google returned `404 NOT_FOUND` ("no longer available to new users") for it — since it led the list, every call hit that 404 on the very first attempt. `parse_intent_with_fallback`/`send_with_fallback` used to only retry on a fixed allowlist of error substrings (`503`/`429`/`RESOURCE_EXHAUSTED`/etc.), which didn't include `404`/`NOT_FOUND`, so the failure was fatal on the spot instead of falling through to a working model — this is what broke `@Tether` commands (including pushes) until fixed. Both now retry on any exception across the whole model list, matching the pattern already used by the push-date-picker Gemini call.

## Environment Variables

```env
GEMINI_API_KEY=
DISCORD_BOT_TOKEN=
DISCORD_USER_ID=
MORNING_BRIEFING_ENABLED=true   # optional, defaults true
TEST_MODE=false                 # optional, defaults false
EVENINGS_PER_WEEK=6             # optional, weekly capacity split across projects
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
- Pushes require a reason (`push_reason` must be non-null) — `validate_push_command` enforces this. The reason's *content* is never judged (deliberately — see `apply_push_runway_cap`'s docstring for why text-similarity/AI-graded excuse checks were rejected). Instead, repeated pushes on the same task buy less runway each time, content-blind: push 1 is unrestricted, push 2 caps at `today+3` days, push 3+ caps at `today+1` day (`PUSH_RUNWAY_CAP_DAYS`/`PUSH_RUNWAY_FLOOR_DAYS`, applied in `handle_push_with_reason` after `resolve_push_date`). Pushing is never blocked outright — only `complete`/`delete` are hard actions — consistent with "missed deadlines are never auto-moved."
- Midnight `T00:00:00` times are hard-overridden to `T23:59:00` in `create_calendar_event` to prevent Gemini from assigning midnight deadlines.
- `pending_clarifications` dict tracks users mid-clarification flow; their next message bypasses the intent parser and routes directly to the scheduler session.
