# Tether

Tether is a personal scheduling and accountability agent that manages deadline queues through natural language.  
It operates through Discord, runs continuously in Docker, and uses Google Calendar as the source of truth.

Unlike traditional productivity systems, Tether does not manage focus sessions, task breakdowns, or execution strategy.

Tether owns the **when** — you own the **how**.

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

Tether manages tasks as a Sunday-based deadline queue.

### Standard Tasks

New tasks are assigned to the next available Sunday slot.

### Urgent / ASAP Tasks

Urgent tasks insert at the front of the queue and push existing deadlines back one week each.

### Completing Tasks

Completing a task removes it and pulls all remaining deadlines forward.

### Missed Tasks

Missed deadlines remain in place until explicitly pushed or completed.

### Fixed Events

Events created manually by the user are never modified.

---

## Features

- Natural language scheduling through Discord mentions
- Queue-based deadline management
- Automatic Sunday deadline assignment
- ASAP insertion with cascading queue shifts
- Google Calendar integration
- Metadata tracking for postponed tasks
- Proactive morning and evening reminders
- Deadline escalation behavior
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

No external database is required.

Task metadata is embedded directly into Google Calendar event descriptions.

---

## Project Structure

```txt
Tether/
├── main.py              # Core bot logic
├── agent.md             # System prompt and scheduling rules
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env
├── credentials.json
├── token.json
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
→ Done. Circuits exam now due this Sunday.
```

---

## Proactive Behaviors

Tether can proactively DM:

- Morning schedule briefings
- Upcoming deadline warnings
- Weekly queue summaries
- Overdue task follow-ups
- Queue inactivity checks

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

without requiring a database.

---

## Model Fallback

If Gemini fails or rate limits, Tether automatically retries using fallback models.

Current fallback chain:

```txt
1. gemini-2.5-pro
2. gemini-2.5-flash
3. gemini-2.5-flash-lite
4. gemini-2.0-flash
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
