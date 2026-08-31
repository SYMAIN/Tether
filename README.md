# Tether

Tether is a personal scheduling and accountability agent that manages deadline queues through natural language.  
It operates through Discord, runs continuously in Docker, and uses Google Calendar as the source of truth.

Unlike traditional productivity systems, Tether does not manage focus sessions, task breakdowns, or execution strategy.

Tether owns the **when** — you own the **how**.

**Status: v1.0 — feature-complete as of 2026-08-21.** Maintenance mode: bug fixes only, no new features planned. See [CHANGELOG.md](CHANGELOG.md).

---

## Philosophy

Tether is designed as a lightweight personal secretary:

- Assigns and manages deadlines
- Maintains a structured queue
- Tracks postponed commitments
- Follows up proactively
- Preserves scheduling continuity

It intentionally avoids:

- Pomodoro systems
- Habit tracking
- Task decomposition
- Gamification
- Productivity coaching

The goal is minimal overhead with persistent accountability.

---

## Core Behavior

Tether manages tasks as a Sunday-based deadline queue, per project.

### Standard Tasks

New ad-hoc tasks are assigned to the next available Sunday slot.

Belki-imported subtasks are packed into weeks by estimated evenings
(`EVENINGS_PER_WEEK`, default 6) — several small subtasks share one Sunday
instead of each consuming a week. Subtasks without an estimate fill their
whole week.

### Multi-Project Scheduling

Several Belki projects can run at once. `projects.md` (in the Belki vault)
registers each project's `due::` and `status::`. Each week's evening capacity
is split across active projects by **deadline pressure** — a project's
required rate is its open evenings over the weeks left until its deadline,
apportioned by largest remainder and capped at what it actually has left.
Undeadlined projects split the remainder round-robin, longest-untouched-first.

The split is computed once per week and pinned — spend is tracked at import
time, not re-derived from the calendar, so finishing a task early doesn't
hand its freed evening to the next task in the same week. A new week (or an
explicit `sync belki <project>`, which hands the whole week to one project)
recomputes the split.

Forecasting compares each project's **measured** completion velocity against
its required rate, not the sum of its open estimates, and flags projects
falling behind as AT RISK (`weekly_queue_summary`, Sunday mornings).

### Urgent / ASAP Tasks

Urgent tasks insert at the front of the queue and push existing deadlines back one week each.

### Completing Tasks

Completing a task removes it. Remaining deadlines stay where they are —
finishing early is never punished by making the next deadline arrive sooner.

Belki tasks are bidirectional: checking a task off (`- [x]`) in Belki
auto-completes it in Tether and deletes its calendar event — no separate
`@Tether completed` needed.

### Missed Tasks

Missed deadlines remain in place until explicitly pushed or completed.
Pushing always requires a reason, but is never blocked — only `complete`/
`delete` are hard actions. Repeated pushes on the same task buy less runway
each time (push 1 unrestricted, push 2 caps at +3 days, push 3+ caps at +1
day), independent of what the reason says.

### Fixed Events

Events created manually by the user are never modified — only events with
the `⏰` prefix are Tether-managed.

---

## Features

- Natural language scheduling through Discord mentions
- Queue-based deadline management, split across concurrent projects by deadline pressure
- Automatic Sunday deadline assignment
- ASAP insertion with cascading queue shifts
- Bidirectional Belki sync — checking off a task in Belki completes it and clears its calendar event
- Priority-aware ranking (`priority::` from Belki, keyword-inferred for ad-hoc tasks)
- Google Calendar integration
- Metadata tracking for postponed tasks
- Proactive morning and evening reminders, plus weekly at-risk-project summaries
- Overdue nag loop with quiet hours and per-task `keep` suppression
- Content-blind push runway (repeated pushes buy progressively less runway)
- Model fallback handling for Gemini API failures
- Dockerized deployment with Windows auto-start support

---

## Stack

| Layer     | Tool                    |
| --------- | ----------------------- |
| AI Engine | Google Gemini           |
| Interface | Discord                 |
| Calendar  | Google Calendar API     |
| Runtime   | Docker + Docker Compose |
| Language  | Python 3.11             |

---

## Architecture

Tether is intentionally lightweight:

- Discord acts as the interaction layer
- Google Calendar acts as the persistent state layer
- Gemini handles natural language interpretation and scheduling decisions
- Docker keeps the system continuously running

Google Calendar remains the scheduling source of truth — task metadata is
embedded directly into event descriptions, and nothing about *what's due
when* lives anywhere else.

A local SQLite ledger (`ledger.py`, default `data/ledger.db`) exists
alongside it, but only for retrospective history — created/completed/pushed/
kept/deleted/nag-ignored events — used for velocity forecasting and stats.
It is never consulted to decide what's currently due.

---

## Project Structure

```txt
Tether/
├── main.py              # Core bot logic, nag loop, scheduled jobs
├── belki_import.py      # Belki sync + project-deadline allocation
├── ledger.py            # SQLite task-history ledger (stats only, not scheduling)
├── agent.md             # System prompt and scheduling rules for the Gemini chat
├── requirements.txt
├── dockerfile
├── docker-compose.yml
├── .env                 # not in git
├── credentials.json     # not in git
├── token.json           # not in git
├── data/                # ledger.db, not in git
├── start.bat
└── stop.bat
```

---

## Setup

### Prerequisites

- Docker Desktop
- Python 3.11
- Google Cloud project with Calendar API enabled
- Discord bot token
- Gemini API key

---

## Environment Variables

```env
GEMINI_API_KEY=your_key
DISCORD_BOT_TOKEN=your_token
DISCORD_USER_ID=your_discord_id

# optional
MORNING_BRIEFING_ENABLED=true  # defaults true
TEST_MODE=false                # fires all scheduled jobs 2 min after startup
EVENINGS_PER_WEEK=6            # weekly capacity split across projects
BELKI_PATH=/app/belki          # Belki vault mount (read-only)
LEDGER_DB=data/ledger.db       # SQLite task-history ledger
```

---

## Google Calendar Authentication

Generate `token.json` locally before running Docker:

```bash
pip install -r requirements.txt
python main.py
```

The OAuth client must be configured as:

- Desktop Application
- Google Calendar API enabled
- `http://localhost:8080` added as an authorized redirect URI

---

## Discord Setup

1. Create a bot application in Discord Developer Portal
2. Enable:
    - Message Content Intent
3. Grant permissions:
    - Send Messages
    - Read Message History
    - View Channels

Tether responds when mentioned:

```txt
@Tether schedule circuits exam
@Tether push lab report back one week
@Tether completed deck renovation
@Tether sync belki
@Tether sync belki circuits
```

---

## Running

```bash
docker-compose up --build -d
```

For Windows startup automation:

- Launch Docker Desktop first
- Then execute `start.bat`
- Task Scheduler should wait for Docker readiness before booting containers

---

## Example Interactions

```txt
@Tether schedule circuits exam
→ Added. Circuits exam due Sun May 18.

@Tether this is urgent — finish lab report
→ ⚠️ Lab report moved to front. Bumped: Circuits exam → May 25.

@Tether completed lab report
→ Done. Up next: Circuits exam — due May 25.
```

---

## Proactive Behaviors

Tether can proactively DM:

- Morning schedule briefings
- Upcoming deadline warnings
- Weekly queue summaries (per project, flagging AT RISK ones)
- Overdue task follow-ups
- Queue inactivity checks

### Scheduled Jobs (America/Toronto)

| Job                    | Schedule       |
| ---------------------- | -------------- |
| `send_overdue_nag`     | Every 30 min   |
| `midnight_nag_persist` | 00:00 daily    |
| `run_morning_jobs`     | 09:00 daily    |
| `eod_sweep`            | 22:00 daily    |
| `inactivity_check`     | 17:00 daily    |
| `weekly_queue_summary` | Sunday 09:00   |

`send_overdue_nag` reconciles Belki every 30 min regardless of quiet hours;
between 23:00–08:00 the nag *DM* itself only sends on the top of the hour,
so overdue pressure doesn't buzz the phone all night. A task marked `keep`
goes quiet until midnight, then resumes normal pressure the next day if
still unresolved.

---

## Metadata Tracking

Tether embeds lightweight metadata directly inside calendar event descriptions:

```txt
[TETHER_META]
pushes=3
created_at=2026-05-13
last_modified=2026-05-20
origin=asap_insert
```

This enables:

- procrastination tracking
- escalation behavior
- continuity across sessions

directly on the calendar, with no round-trip to the ledger needed to know what's due.

---

## Model Fallback

If Gemini fails or rate limits, Tether automatically retries using fallback models — on *any* exception, not just a fixed allowlist (a bare 404 from a deprecated model used to be fatal instead of falling through).

Current fallback chain:

```txt
1. gemini-2.5-flash
2. gemini-2.5-flash-lite
3. gemini-2.0-flash
```

---

## Design Principles

- Deadlines over micromanagement
- Queue continuity over optimization
- Minimal friction
- Stateless conversations
- Calendar as source of truth
- Accountability over motivation

---
