# Tether

A personal scheduling agent that converts natural language tasks into structured focus blocks on Google Calendar. Controlled via Discord, runs continuously in Docker.

---

## Overview

Tether uses Google Gemini's function calling API to read your existing calendar, decompose tasks into appropriately-sized work blocks, and schedule them around your commitments. It respects hard deadlines, avoids conflicts, and never modifies events you created manually.

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

## Features

- Natural language task input via Discord
- Automatic task decomposition into 60–90 min focus blocks
- Conflict-aware scheduling against existing calendar events
- Hard and soft deadline handling
- Overflow scheduling into evenings and weekends when needed
- Emoji-prefix system to distinguish agent-managed events from user events
- Automatic model fallback on API unavailability

---

## Project Structure

```
Tether/
├── main.py              # Agent logic and Discord bot
├── agent.md             # Scheduling rules and AI instructions
├── requirements.txt     # Python dependencies
├── dockerfile
├── docker-compose.yml
├── .env                 # API keys — do not commit
├── credentials.json     # Google OAuth credentials — do not commit
├── token.json           # Google auth token — do not commit
├── start.bat            # Windows start script
└── stop.bat             # Windows stop script
```

---

## Setup

### Prerequisites

- Docker Desktop
- Google Cloud project with Calendar API enabled
- Discord bot token from [discord.com/developers](https://discord.com/developers/applications)
- Gemini API key from [Google AI Studio](https://aistudio.google.com)

### Google Calendar Authentication

Run locally once to generate `token.json`:

```bash
pip install -r requirements.txt
python main.py
```

### Environment Variables

```env
GEMINI_API_KEY=your_gemini_api_key
DISCORD_BOT_TOKEN=your_discord_bot_token
```

### Discord Bot Configuration

1. Create a new application at [discord.com/developers](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under Bot settings
3. Grant permissions: `Send Messages`, `Read Message History`, `View Channels`
4. Create a channel named `tether` in your server

### Running

```bash
docker-compose up --build
```

---

## Usage

Send tasks naturally in the `#tether` channel:

```
Study for Circuits exam by Friday
Finish lab report by tomorrow
I'm busy Wednesday
Build a Discord bot this week
```

Tether will confirm what it scheduled and flag any conflicts or warnings.

---

## Scheduling Behavior

- **Primary hours:** 9 AM – 6 PM weekdays
- **Overflow:** 6:30 PM – 10 PM weekdays, 10 AM – 6 PM weekends
- **Buffer:** 30 minutes between every focus block
- **Hard deadlines:** Blocks scheduled backwards from the due date
- **Soft deadlines:** Auto-assigned based on task complexity
- **Fixed events** (no emoji prefix): never rescheduled or modified

Agent-created events use emoji prefixes (`📚`, `⚙️`, `🔁`) to distinguish them from user events.

---

## Model Fallback

On a 503 error, Tether tries models in this order:

```
1. gemini-3-flash-preview
2. gemini-3.1-pro-preview
3. gemini-3.1-flash-lite-preview
4. gemini-2.5-pro
5. gemini-2.5-flash
6. gemini-2.5-flash-lite
7. gemini-2.0-flash
```

---
