import os
import json
import datetime
import discord
import pytz
import re
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- CONFIG ---
SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEADLINE_PREFIX = "⏰"
TORONTO_TZ = pytz.timezone("America/Toronto")
DISCORD_USER_ID = int(os.environ.get("DISCORD_USER_ID"))
LOG_FILE = "tether.log"

MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]

CONFIDENCE_THRESHOLD = 0.7


# --- META ---
def parse_meta(event: dict) -> dict:
    desc = event.get("description", "") or ""
    meta = {"pushes": 0, "created_at": "", "last_modified": "", "origin": "normal"}
    match = re.search(r"\[TETHER_META\](.*?)(\[|$)", desc, re.DOTALL)
    if not match:
        return meta
    for line in match.group(1).strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    meta["pushes"] = int(meta.get("pushes", 0))
    return meta


def build_meta(pushes=0, created_at=None, last_modified=None, origin="normal") -> str:
    today = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    return (
        f"[TETHER_META]\n"
        f"pushes={pushes}\n"
        f"created_at={created_at or today}\n"
        f"last_modified={last_modified or today}\n"
        f"origin={origin}\n"
    )


def log(entry: str):
    timestamp = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {entry}\n")


# --- CALENDAR SETUP ---
def get_calendar_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open("token.json", "w") as token:
                token.write(creds.to_json())
        else:
            raise Exception("No valid credentials.")
    return build("calendar", "v3", credentials=creds)


service = get_calendar_service()


# --- CALENDAR TOOLS ---
def create_calendar_event(
    summary: str, start_time: str, end_time: str, origin: str = "normal"
):
    """Creates a calendar event. Times must be ISO format (YYYY-MM-DDTHH:MM:SS)."""
    meta = build_meta(origin=origin)
    due_date = start_time[:10]
    description = f"{meta}\nOriginally due: {due_date}"
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time, "timeZone": "America/Toronto"},
        "end": {"dateTime": end_time, "timeZone": "America/Toronto"},
        "colorId": "5",
    }
    return service.events().insert(calendarId="primary", body=event).execute()


def update_event_meta(event_id: str, pushes: int):
    """Increments push count and updates last_modified on a Tether event."""
    event = service.events().get(calendarId="primary", eventId=event_id).execute()
    meta = parse_meta(event)
    desc = event.get("description", "") or ""
    orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
    original_due = orig_match.group(1) if orig_match else None

    meta["pushes"] = pushes
    meta["last_modified"] = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    new_due = event["start"]["dateTime"][:10]

    description = build_meta(**meta)
    description += f"\nOriginally due: {original_due or new_due}"

    event["description"] = description
    service.events().update(
        calendarId="primary", eventId=event_id, body=event
    ).execute()
    return {"status": "updated", "pushes": pushes}


def delete_calendar_event(event_id: str):
    """Deletes a calendar event by its event ID."""
    service.events().delete(calendarId="primary", eventId=event_id).execute()
    return {"status": "deleted", "event_id": event_id}


def list_upcoming_events(max_results: int = 20):
    """Returns upcoming events to check the queue and find free slots."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
            timeZone="America/Toronto",
        )
        .execute()
    )
    return result.get("items", [])


def get_tether_deadlines():
    """Returns only Tether-managed ⏰ deadline events."""
    return [
        e
        for e in list_upcoming_events(50)
        if e.get("summary", "").startswith(DEADLINE_PREFIX)
    ]


def get_events_in_window(hours_from_now: int):
    """Returns Tether deadlines within the next N hours."""
    now = datetime.datetime.now(TORONTO_TZ)
    cutoff = now + datetime.timedelta(hours=hours_from_now)
    deadlines = []
    for e in get_tether_deadlines():
        start_str = e.get("start", {}).get("dateTime")
        if not start_str:
            continue
        start = datetime.datetime.fromisoformat(start_str)
        if start.tzinfo is None:
            start = TORONTO_TZ.localize(start)
        if now <= start <= cutoff:
            deadlines.append(e)
    return deadlines


def get_overdue_tether_events():
    """Returns all past-due Tether deadlines (not just today's)."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    events_result = (
        service.events()
        .list(
            calendarId="primary",
            timeMax=now_utc,
            maxResults=50,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    overdue = []
    for e in events_result.get("items", []):
        if not e.get("summary", "").startswith(DEADLINE_PREFIX):
            continue
        end_str = e.get("end", {}).get("dateTime")
        if not end_str:
            continue
        overdue.append(e)
    return overdue


def complete_task(event_id: str):
    """Marks a Tether deadline as done and signals the queue to advance."""
    service.events().delete(calendarId="primary", eventId=event_id).execute()
    remaining = get_tether_deadlines()
    return {
        "status": "completed",
        "event_id": event_id,
        "remaining_queue": [
            {"id": e["id"], "summary": e["summary"], "due": e["start"]["dateTime"]}
            for e in remaining
        ],
    }


# --- PRIORITY ENGINE ---
COMPLEXITY_HIGH = [
    "exam",
    "final",
    "midterm",
    "thesis",
    "dissertation",
    "project",
    "build",
    "app",
    "bot",
    "feature",
    "system",
    "redesign",
    "research",
]
COMPLEXITY_MED = [
    "report",
    "essay",
    "assignment",
    "lab",
    "presentation",
    "study",
    "review",
    "analysis",
    "proposal",
]
COMPLEXITY_LOW = [
    "email",
    "reply",
    "read",
    "watch",
    "call",
    "meeting",
    "form",
    "submit",
]


def infer_complexity(task_title: str) -> int:
    title = task_title.lower()
    if any(w in title for w in COMPLEXITY_HIGH):
        return 3
    if any(w in title for w in COMPLEXITY_MED):
        return 2
    return 1


def priority_score(event: dict) -> float:
    today = datetime.datetime.now(TORONTO_TZ).date()
    meta = parse_meta(event)
    pushes = meta["pushes"]

    due_str = event.get("start", {}).get("dateTime", "")[:10]
    try:
        due_date = datetime.date.fromisoformat(due_str)
        days_until = max((due_date - today).days, 0)
    except ValueError:
        days_until = 999

    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    complexity = infer_complexity(name)

    # Lower score = higher priority
    return days_until - (complexity * 2) - (pushes * 1.5)


def rank_deadlines(deadlines: list) -> list:
    return sorted(deadlines, key=priority_score)


def complexity_label(score: int) -> str:
    return {3: "high complexity", 2: "medium complexity", 1: "quick task"}.get(
        score, "unknown"
    )


def priority_reason(event: dict) -> str:
    today = datetime.datetime.now(TORONTO_TZ).date()
    meta = parse_meta(event)
    pushes = meta["pushes"]
    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    due_str = event.get("start", {}).get("dateTime", "")[:10]

    try:
        due_date = datetime.date.fromisoformat(due_str)
        days_until = (due_date - today).days
    except ValueError:
        days_until = 999

    complexity = infer_complexity(name)
    parts = []
    if days_until <= 2:
        parts.append("due very soon")
    elif days_until <= 7:
        parts.append(f"due in {days_until} days")
    else:
        parts.append(f"due {due_str}")
    parts.append(complexity_label(complexity))
    if pushes > 0:
        parts.append(f"pushed {pushes}×")
    return ", ".join(parts)


# --- INTENT PARSER ---
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

with open("agent.md", "r") as f:
    AGENT_INSTRUCTIONS = f.read()

INTENT_PARSER_PROMPT = f"""You are an intent parser for a personal scheduling agent called Tether.
Convert the user's message into a structured JSON command. Output ONLY the JSON object — no preamble, no markdown fences.

Schema:
{{
  "action": "schedule" | "push" | "complete" | "delete" | "list" | "next" | "query" | "clarify",
  "task_title": string | null,
  "urgency": "normal" | "asap",
  "target_date": "YYYY-MM-DD" | null,
  "delta_days": integer | null,
  "confidence": float (0.0–1.0),
  "needs_clarification": boolean,
  "clarification_question": string | null
}}

Rules:
- action "schedule": user wants to add a task with a deadline
- action "push": user wants to delay a task (delta_days = number of days, or 7 for "one week / next week")
- action "complete": user says they finished something
- action "delete": user wants to remove a task without marking it complete
- action "query": user asks about an existing task's deadline (e.g. "when is X due", "what's the deadline for X")
- action "next": user asks what to work on next / what's most important / what should I do
- action "list": user wants to see the queue
- action "clarify": ONLY when you cannot determine action or task with confidence >= {CONFIDENCE_THRESHOLD}
- urgency "asap": only if user says urgent / ASAP / emergency / as soon as possible
- target_date: nearest future date matching what the user said (e.g. "by Friday" → next Friday as YYYY-MM-DD); null if not given
- delta_days: for push only; null otherwise
- needs_clarification: true if confidence < {CONFIDENCE_THRESHOLD} OR action is ambiguous
- clarification_question: single concise question to resolve ambiguity; null if needs_clarification is false
- "I'm busy [day]" → action "push", task_title null, delta_days null (scheduler will handle day-specific logic)
- Never invent a deadline the user did not provide
- Current date: {{date}}
"""


def build_intent_prompt() -> str:
    today = datetime.datetime.now(TORONTO_TZ).strftime("%A, %B %d, %Y")
    return INTENT_PARSER_PROMPT.replace("{date}", today)


def parse_intent(user_message: str, model: str) -> dict:
    response = gemini_client.models.generate_content(
        model=model,
        config=types.GenerateContentConfig(system_instruction=build_intent_prompt()),
        contents=user_message,
    )
    raw = (
        response.text.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )
    return json.loads(raw)


def parse_intent_with_fallback(user_message: str) -> tuple[dict, str]:
    for model in MODELS:
        try:
            return parse_intent(user_message, model), model
        except Exception as e:
            if any(
                code in str(e)
                for code in [
                    "503",
                    "UNAVAILABLE",
                    "429",
                    "RESOURCE_EXHAUSTED",
                    "RemoteProtocolError",
                    "incomplete chunked read",
                ]
            ):
                continue
            raise
    raise Exception("All models are currently unavailable. Please try again later.")


# --- SCHEDULER SESSION ---
def get_next_sunday(from_date):
    days_ahead = (6 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + datetime.timedelta(days=days_ahead)


def get_scheduler_chat(model=None):
    today = datetime.datetime.now(TORONTO_TZ)
    next_sunday = get_next_sunday(today)
    date_context = (
        f"## CURRENT DATE (MANDATORY — USE THIS, IGNORE TRAINING DATA)\n"
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Next Sunday: {next_sunday.strftime('%B %d, %Y')}\n"
        f"Default deadline ISO: {next_sunday.strftime('%Y-%m-%d')}T23:59:00\n\n"
    )
    return gemini_client.chats.create(
        model=model or MODELS[0],
        config=types.GenerateContentConfig(
            system_instruction=date_context + AGENT_INSTRUCTIONS,
            tools=[
                create_calendar_event,
                delete_calendar_event,
                list_upcoming_events,
                update_event_meta,
                complete_task,
            ],
        ),
    )


user_sessions: dict = {}
user_model_index: dict = {}
pending_clarifications: dict[int, str] = {}  # user_id → message awaiting clarification


def get_or_create_session(user_id: int):
    if user_id not in user_sessions:
        user_sessions[user_id] = get_scheduler_chat(MODELS[0])
        user_model_index[user_id] = 0
    return user_sessions[user_id]


def send_with_fallback(chat, user_id: int, message_content: str):
    current_index = user_model_index.get(user_id, 0)
    for i in range(current_index, len(MODELS)):
        try:
            if i != current_index:
                chat = get_scheduler_chat(MODELS[i])
                user_sessions[user_id] = chat
                user_model_index[user_id] = i
            response = chat.send_message(message_content)
            if i != 0:
                user_model_index[user_id] = 0
                user_sessions[user_id] = get_scheduler_chat(MODELS[0])
            return response, MODELS[i]
        except Exception as e:
            if any(
                code in str(e)
                for code in [
                    "503",
                    "UNAVAILABLE",
                    "429",
                    "RESOURCE_EXHAUSTED",
                    "RemoteProtocolError",
                    "incomplete chunked read",
                ]
            ):
                continue
            raise
    raise Exception("All models are currently unavailable. Please try again later.")


def extract_reply(response, chat) -> str:
    text_parts = [
        part.text
        for part in response.candidates[0].content.parts
        if hasattr(part, "text") and part.text
    ]
    if text_parts:
        return "".join(text_parts)
    follow_up = chat.send_message("Confirm what you just did in one line.")
    return follow_up.text or "Done."


# --- DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


async def dm_user(message: str):
    user = await bot.fetch_user(DISCORD_USER_ID)
    await user.send(message)
    log(f"[DM] {message[:200]}")


# --- SCHEDULED JOBS ---
async def morning_briefing():
    today = datetime.datetime.now(TORONTO_TZ).date()
    events = list_upcoming_events(20)
    today_events = [
        e
        for e in events
        if e.get("start", {}).get("dateTime", "").startswith(str(today))
    ]
    if not today_events:
        await dm_user("📅 **Morning briefing** — Nothing scheduled today.")
        return
    lines = ["📅 **Morning briefing — Today's schedule:**\n"]
    for e in today_events:
        start = e["start"]["dateTime"][11:16]
        end = e["end"]["dateTime"][11:16]
        lines.append(f"• {start}–{end} {e['summary']}")
    await dm_user("\n".join(lines))


async def eod_sweep():
    overdue = get_overdue_tether_events()
    for e in overdue:
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        meta = parse_meta(e)
        pushes = meta["pushes"]
        origin = meta.get("origin", "normal")
        desc = e.get("description", "") or ""
        orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
        orig_str = (
            f" Originally due {orig_match.group(1)}."
            if orig_match and pushes > 0
            else ""
        )

        if origin == "asap_insert" and pushes == 0:
            msg = f"⚠️ **{name}** — you marked this urgent. It wasn't completed. Keep or push back?"
        elif pushes >= 4:
            msg = f"⚠️ **{name}** — deferred {pushes} times.{orig_str} Do you still intend to complete this?"
        elif pushes >= 2:
            msg = (
                f"⚠️ **{name}** — slipped {pushes} times.{orig_str} Keep or push back?"
            )
        else:
            msg = f"⚠️ **{name}** wasn't completed. Keep or push back?"

        await dm_user(msg)


async def deadline_warning():
    upcoming = get_events_in_window(hours_from_now=24)
    for e in upcoming:
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        due = e["start"]["dateTime"][11:16]
        await dm_user(f"⏰ **Heads up:** {name} is due tomorrow at {due}.")


async def weekly_queue_summary():
    deadlines = get_tether_deadlines()
    if not deadlines:
        await dm_user("📋 **Weekly Queue** — No deadlines in the queue. You're clear.")
        return
    ranked = rank_deadlines(deadlines)
    lines = ["📋 **Weekly Queue** _(by priority)_:\n"]
    for i, e in enumerate(ranked):
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        date_str = e["start"]["dateTime"][:10]
        meta = parse_meta(e)
        pushes = meta["pushes"]
        desc = e.get("description", "") or ""
        orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
        suffix = ""
        if pushes > 0:
            suffix = f" _(pushed {pushes}×"
            if orig_match:
                suffix += f", originally {orig_match.group(1)}"
            suffix += ")_"
        prefix = "🔴" if i == 0 else "•"
        lines.append(f"{prefix} {name} — due {date_str}{suffix}")
    await dm_user("\n".join(lines))


async def inactivity_check():
    deadlines = get_tether_deadlines()
    if not deadlines:
        return
    today = datetime.datetime.now(TORONTO_TZ).date()
    stale_threshold = 5
    all_stale = all(
        min(
            (
                today
                - datetime.date.fromisoformat(
                    parse_meta(e).get("last_modified", str(today))
                )
            ).days,
            (
                today
                - datetime.date.fromisoformat(
                    parse_meta(e).get("created_at", str(today))
                )
            ).days,
        )
        >= stale_threshold
        for e in deadlines
    )
    if all_stale:
        await dm_user(
            "📋 No queue changes in 5 days. Still accurate? Reply in #tether."
        )


async def already_dmed_today(keyword: str) -> bool:
    user = await bot.fetch_user(DISCORD_USER_ID)
    dm = await user.create_dm()
    today = datetime.datetime.now(TORONTO_TZ).date()
    async for msg in dm.history(limit=50):
        msg_date = msg.created_at.astimezone(TORONTO_TZ).date()
        if msg_date < today:
            break
        if msg.author == bot.user and keyword in msg.content:
            return True
    return False


@bot.event
async def on_ready():
    print(f"Tether is online as {bot.user}")
    scheduler = AsyncIOScheduler(timezone=TORONTO_TZ)
    scheduler.add_job(
        morning_briefing,
        CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ),
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.add_job(
        eod_sweep,
        CronTrigger(hour=22, minute=0, timezone=TORONTO_TZ),
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.add_job(
        deadline_warning,
        CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ),
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.add_job(
        inactivity_check,
        CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ),
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.add_job(
        weekly_queue_summary,
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=TORONTO_TZ),
        misfire_grace_time=None,
        coalesce=True,
    )
    scheduler.start()

    now = datetime.datetime.now(TORONTO_TZ)
    if now.hour >= 9 and not await already_dmed_today("Morning briefing"):
        await morning_briefing()
        await deadline_warning()
        await inactivity_check()
    if now.hour >= 22 and not await already_dmed_today("wasn't completed"):
        await eod_sweep()
    if (
        now.weekday() == 6
        and now.hour >= 9
        and not await already_dmed_today("Weekly Queue")
    ):
        await weekly_queue_summary()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if bot.user not in message.mentions:
        return

    content = (
        message.content.replace(f"<@{bot.user.id}>", "")
        .replace(f"<@!{bot.user.id}>", "")
        .strip()
    )
    if not content:
        return

    user_id = message.author.id

    async with message.channel.typing():
        try:
            # Resolve pending clarification
            if user_id in pending_clarifications:
                original = pending_clarifications.pop(user_id)
                content = f"{original} — clarification: {content}"

            command, _ = parse_intent_with_fallback(content)
            log(f"[INTENT] {message.author}: {json.dumps(command)}")

            if command.get("needs_clarification"):
                pending_clarifications[user_id] = content
                question = command.get("clarification_question", "Could you clarify?")
                await message.reply(f"❓ {question}", mention_author=False)
                return

            # Handle priority query directly — no calendar writes needed
            if command.get("action") == "next":
                deadlines = get_tether_deadlines()
                if not deadlines:
                    await message.reply(
                        "Queue is empty. Nothing to work on.", mention_author=False
                    )
                    return
                top = rank_deadlines(deadlines)[0]
                name = (
                    top["summary"]
                    .replace(DEADLINE_PREFIX, "")
                    .replace("— DUE", "")
                    .strip()
                )
                reason = priority_reason(top)
                await message.reply(f"🔴 **{name}** — {reason}.", mention_author=False)
                return

            # Handle completion directly — no scheduler session needed
            if command.get("action") == "complete":
                task_title = command.get("task_title") or ""
                deadlines = get_tether_deadlines()
                matches = [
                    e
                    for e in deadlines
                    if task_title.lower() in e.get("summary", "").lower()
                ]
                if not matches:
                    await message.reply(
                        f"No scheduled task matching **{task_title}**.",
                        mention_author=False,
                    )
                    return
                if len(matches) > 1:
                    names = ", ".join(
                        e["summary"]
                        .replace(DEADLINE_PREFIX, "")
                        .replace("— DUE", "")
                        .strip()
                        for e in matches
                    )
                    await message.reply(
                        f"Multiple matches: {names}. Be more specific.",
                        mention_author=False,
                    )
                    return
                e = matches[0]
                name = (
                    e["summary"]
                    .replace(DEADLINE_PREFIX, "")
                    .replace("— DUE", "")
                    .strip()
                )
                service.events().delete(calendarId="primary", eventId=e["id"]).execute()
                remaining = get_tether_deadlines()
                if remaining:
                    ranked = rank_deadlines(remaining)
                    next_name = (
                        ranked[0]["summary"]
                        .replace(DEADLINE_PREFIX, "")
                        .replace("— DUE", "")
                        .strip()
                    )
                    next_due = ranked[0]["start"]["dateTime"][:10]
                    await message.reply(
                        f"✅ **{name}** done. Up next: **{next_name}** — due {next_due}.",
                        mention_author=False,
                    )
                else:
                    await message.reply(
                        f"✅ **{name}** done. Queue is clear.", mention_author=False
                    )
                log(f"[COMPLETE] {name}")
                return
            if command.get("action") == "query":
                task_title = command.get("task_title") or ""
                deadlines = get_tether_deadlines()
                matches = [
                    e
                    for e in deadlines
                    if task_title.lower() in e.get("summary", "").lower()
                ]
                if not matches:
                    await message.reply(
                        f"No scheduled task matching **{task_title}**.",
                        mention_author=False,
                    )
                else:
                    e = matches[0]
                    name = (
                        e["summary"]
                        .replace(DEADLINE_PREFIX, "")
                        .replace("— DUE", "")
                        .strip()
                    )
                    due = e["start"]["dateTime"][:10]
                    await message.reply(
                        f"**{name}** is due {due}.", mention_author=False
                    )
                return

            # Pass structured intent to scheduler session
            chat = get_or_create_session(user_id)
            scheduler_prompt = (
                f"[PARSED INTENT] {json.dumps(command)}\n\nOriginal message: {content}"
            )
            response, _ = send_with_fallback(chat, user_id, scheduler_prompt)
            reply = extract_reply(response, chat)

            log(f"[REPLY to {message.author}] {reply[:200]}")
            for i in range(0, len(reply), 2000):
                await message.reply(reply[i : i + 2000], mention_author=False)

        except Exception as e:
            print(f"ERROR: {e}")
            await message.reply(
                "⚠️ Tether hit an upstream connection issue.", mention_author=False
            )


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
