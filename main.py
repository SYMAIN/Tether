import os
import time
import asyncio
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

import belki_import
import ledger
from ledger import clean_name, infer_complexity, complexity_label

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
LOG_RETENTION_DAYS = 30
MORNING_BRIEFING_ENABLED = (
    os.environ.get("MORNING_BRIEFING_ENABLED", "true").lower() == "true"
)
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# --- NAG STATE (in-memory, per session) ---
unacknowledged_overdue: set[str] = set()
nag_count: int = 0
task_nag_counts: dict[str, int] = {}
_started: bool = False


# --- META ---
def parse_meta(event: dict) -> dict:
    desc = event.get("description", "") or ""
    meta = {
        "pushes": 0,
        "created_at": "",
        "last_modified": "",
        "origin": "normal",
        "nag_ignored": 0,
        "last_push_reason": "",
    }
    match = re.search(r"\[TETHER_META\](.*?)(\[|$)", desc, re.DOTALL)
    if not match:
        return meta
    for line in match.group(1).strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    meta["pushes"] = int(meta.get("pushes", 0))
    meta["nag_ignored"] = int(meta.get("nag_ignored", 0))
    return meta


def build_meta(
    pushes=0,
    created_at=None,
    last_modified=None,
    origin="normal",
    nag_ignored=0,
    last_push_reason="",
    estimate="",
) -> str:
    today = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    meta = (
        f"[TETHER_META]\n"
        f"pushes={pushes}\n"
        f"created_at={created_at or today}\n"
        f"last_modified={last_modified or today}\n"
        f"origin={origin}\n"
        f"nag_ignored={nag_ignored}\n"
        f"last_push_reason={last_push_reason}\n"
    )
    if estimate not in ("", None):
        meta += f"estimate={estimate}\n"
    return meta


def log(entry: str):
    timestamp = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d %H:%M")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{timestamp}] {entry}\n")


def trim_log():
    if not os.path.exists(LOG_FILE):
        return
    cutoff = (
        datetime.datetime.now(TORONTO_TZ) - datetime.timedelta(days=LOG_RETENTION_DAYS)
    ).strftime("%Y-%m-%d")
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    kept = [l for l in lines if not l.startswith("[") or l[1:11] >= cutoff]
    with open(LOG_FILE, "w") as f:
        f.writelines(kept)


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


_service = None


def get_service():
    # Lazy init: module-level init crashed the bot before Discord connected
    # when credentials were missing, and cached a service that outlived token
    # expiry (see 462dded).
    global _service
    if _service is None:
        _service = get_calendar_service()
    return _service


ledger.init_db()


# --- CALENDAR TOOLS ---
def _insert_deadline(
    summary: str,
    start_time: str,
    end_time: str,
    origin: str = "normal",
    estimate: int | None = None,
    body_text: str | None = None,
    project: str | None = None,
):
    """Internal insert used by both the Gemini tool and the Belki import."""
    # Hard override: Gemini sometimes passes midnight — force to 23:59
    if "T00:00:00" in start_time:
        date_part = start_time[:10]
        start_time = f"{date_part}T23:59:00"
        end_time = f"{date_part}T23:59:00"
    meta = build_meta(origin=origin, estimate=estimate if estimate is not None else "")
    due_date = start_time[:10]
    description = f"{meta}\nOriginally due: {due_date}"
    if body_text:
        description += f"\n\n{body_text}"
    event = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_time, "timeZone": "America/Toronto"},
        "end": {"dateTime": end_time, "timeZone": "America/Toronto"},
        "colorId": "5",
    }
    created = get_service().events().insert(calendarId="primary", body=event).execute()
    ledger.record_created(created, origin=origin, estimate=estimate, project=project)
    return created


def create_calendar_event(
    summary: str, start_time: str, end_time: str, origin: str = "normal"
):
    """Creates a calendar event. Times must be ISO format (YYYY-MM-DDTHH:MM:SS)."""
    return _insert_deadline(summary, start_time, end_time, origin=origin)


def update_event_meta(event_id: str, pushes: int):
    """Increments push count and updates last_modified on a Tether event."""
    event = get_service().events().get(calendarId="primary", eventId=event_id).execute()
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
    get_service().events().update(
        calendarId="primary", eventId=event_id, body=event
    ).execute()
    return {"status": "updated", "pushes": pushes}


def delete_calendar_event(event_id: str):
    """Deletes a calendar event by its event ID."""
    try:
        event = get_service().events().get(calendarId="primary", eventId=event_id).execute()
    except Exception:
        event = None
    get_service().events().delete(calendarId="primary", eventId=event_id).execute()
    if event:
        ledger.record_deleted(event)
    return {"status": "deleted", "event_id": event_id}


def list_upcoming_events(max_results: int = 20):
    """Returns upcoming events to check the queue and find free slots."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = (
        get_service().events()
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
    """Returns all past-due Tether deadlines using pagination to avoid maxResults crowding."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    forty_five_days_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)
    ).isoformat()
    all_items = []
    page_token = None
    while True:
        params = dict(
            calendarId="primary",
            timeMin=forty_five_days_ago,
            timeMax=now_utc,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        if page_token:
            params["pageToken"] = page_token
        result = get_service().events().list(**params).execute()
        all_items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    overdue = []
    for e in all_items:
        if not e.get("summary", "").startswith(DEADLINE_PREFIX):
            continue
        if e.get("end", {}).get("dateTime"):
            overdue.append(e)
    return overdue


def complete_task(event_id: str):
    """Marks a Tether deadline as done and signals the queue to advance."""
    try:
        event = get_service().events().get(calendarId="primary", eventId=event_id).execute()
    except Exception:
        event = None
    get_service().events().delete(calendarId="primary", eventId=event_id).execute()
    if event:
        ledger.record_completed(event)
    remaining = get_tether_deadlines()
    return {
        "status": "completed",
        "event_id": event_id,
        "remaining_queue": [
            {"id": e["id"], "summary": e["summary"], "due": e["start"]["dateTime"]}
            for e in remaining
        ],
    }


def task_matches(query: str, event: dict) -> bool:
    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
        .lower()
    )
    q = query.lower().strip()
    if not q:
        # An empty query must never match — all() over zero words is
        # vacuously true and would match every event.
        return False
    return q == name or all(
        re.search(r"\b" + re.escape(word) + r"\b", name) for word in q.split()
    )


# --- PRIORITY ENGINE ---
# Complexity keyword lists and infer_complexity/complexity_label/clean_name
# live in ledger.py (shared with test_jobs.py).
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
    return days_until - (complexity * 2) - (pushes * 1.5)


def rank_deadlines(deadlines: list) -> list:
    return sorted(deadlines, key=priority_score)


def priority_reason(event: dict) -> str:
    today = datetime.datetime.now(TORONTO_TZ).date()
    meta = parse_meta(event)
    pushes = meta["pushes"]
    due_str = event.get("start", {}).get("dateTime", "")[:10]
    try:
        due_date = datetime.date.fromisoformat(due_str)
        days_until = (due_date - today).days
    except ValueError:
        days_until = 999
    complexity = infer_complexity(
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
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


# --- NAG WORDING ENGINE ---
def nag_message(event: dict, session_nag_count: int) -> str:
    meta = parse_meta(event)
    pushes = meta["pushes"]
    nag_ignored = meta.get("nag_ignored", 0)
    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    desc = event.get("description", "") or ""
    orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
    orig_str = (
        f" (originally due {orig_match.group(1)})" if orig_match and pushes > 0 else ""
    )
    last_reason = meta.get("last_push_reason", "")
    reason_str = f' Last reason: "{last_reason}".' if last_reason else ""
    total_pressure = pushes + nag_ignored + session_nag_count

    if total_pressure == 0:
        return f"⚠️ **{name}** wasn't completed{orig_str}. Keep or push back? (`@Tether push {name} because <reason>` — I'll pick the date)"
    elif total_pressure <= 2:
        return f"⚠️ **{name}** is still sitting there{orig_str}.{reason_str} You haven't responded. Keep or push back?"
    elif total_pressure <= 4:
        return f"🔴 **{name}** — this has slipped {pushes} time(s) and you've ignored {nag_ignored + session_nag_count} reminder(s){orig_str}.{reason_str} What's actually blocking you? Keep or push back."
    elif total_pressure <= 6:
        return f"🔴 **{name}** — still here. Still overdue. You've deferred this {pushes} times{orig_str}.{reason_str} Either commit to a date or delete it. Don't ghost me."
    else:
        return f"🚨 **{name}** — {pushes} pushes. {nag_ignored + session_nag_count} ignored reminders{orig_str}.{reason_str} This task has been rotting. You either do it tonight or you tell me why. I'm not stopping."


def nag_summary_header(nag_count: int) -> str:
    if nag_count == 0:
        return "📋 **Overdue tasks — respond to each one.**\n"
    elif nag_count == 1:
        return "📋 **Still waiting on these. 30 minutes and no response.**\n"
    elif nag_count == 2:
        return "📋 **An hour in. These are still unacknowledged.**\n"
    else:
        return f"📋 **Reminder #{nag_count + 1}. You can't outlast me.**\n"


# --- INTENT PARSER ---
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

with open("agent.md", "r") as f:
    AGENT_INSTRUCTIONS = f.read()

INTENT_PARSER_PROMPT = f"""You are an intent parser for a personal scheduling agent called Tether.
Convert the user's message into a structured JSON command. Output ONLY the JSON object — no preamble, no markdown fences.

Schema:
{{
  "action": "schedule" | "push" | "complete" | "delete" | "list" | "next" | "query" | "keep" | "sync" | "clarify",
  "task_title": string | null,
  "urgency": "normal" | "asap",
  "target_date": "YYYY-MM-DD" | null,
  "delta_days": integer | null,
  "push_reason": string | null,
  "confidence": float (0.0–1.0),
  "needs_clarification": boolean,
  "clarification_question": string | null
}}

Rules:
- action "schedule": user wants to add a task with a deadline
- action "push": user wants to delay a task. push_reason is the key field. target_date is optional — capture it if the user provides one, otherwise leave null and Tether will pick a date automatically
- action "complete": user says they finished something
- action "delete": user wants to remove a task without marking it complete
- action "query": user asks about an existing task's deadline
- action "next": user asks what to work on next
- action "list": user wants to see the queue
- action "keep": user says keep, it's staying, I'll do it — acknowledges an overdue task without pushing
- action "sync": user asks to sync/import tasks from Belki (e.g. "sync belki"). task_title = the project name if the user names one, else null
- action "clarify": ONLY when you cannot determine action or task with confidence >= {CONFIDENCE_THRESHOLD}
- push_reason: extract any reason or explanation the user gives for pushing — from "because <reason>", "since <reason>", or any explanation in the message. null if no reason given
- urgency "asap": only if user says urgent / ASAP / emergency
- target_date: nearest future date matching what the user said (e.g. "by Friday" → next Friday as YYYY-MM-DD, "before July" → last day of June as YYYY-MM-DD, "before [month]" → last day of the prior month); null if not given
- delta_days: for push only; null otherwise
- needs_clarification: true if confidence < {CONFIDENCE_THRESHOLD} OR action is ambiguous
- clarification_question: single concise question to resolve ambiguity
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
user_fallback_time: dict[int, float] = {}
pending_clarifications: dict[int, str] = {}
FALLBACK_COOLDOWN = 300


def get_or_create_session(user_id: int):
    if user_id not in user_sessions:
        user_sessions[user_id] = get_scheduler_chat(MODELS[0])
        user_model_index[user_id] = 0
    return user_sessions[user_id]


def send_with_fallback(chat, user_id: int, message_content: str):
    current_index = user_model_index.get(user_id, 0)
    if current_index > 0:
        elapsed = time.time() - user_fallback_time.get(user_id, 0)
        if elapsed > FALLBACK_COOLDOWN:
            current_index = 0
            user_model_index[user_id] = 0
            user_fallback_time.pop(user_id, None)
    for i in range(current_index, len(MODELS)):
        try:
            if i != current_index:
                chat = get_scheduler_chat(MODELS[i])
                user_sessions[user_id] = chat
                user_model_index[user_id] = i
                if user_id not in user_fallback_time:
                    user_fallback_time[user_id] = time.time()
            response = chat.send_message(message_content)
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
    return "".join(text_parts) if text_parts else "Done."


# --- DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


async def dm_user(message: str):
    user = await bot.fetch_user(DISCORD_USER_ID)
    await user.send(message)
    log(f"[DM] {message[:200]}")


# --- NAG LOOP ---
async def send_overdue_nag():
    global nag_count, unacknowledged_overdue, task_nag_counts
    # Reconcile against Belki first: without this, a task marked done there
    # keeps getting nagged for up to a day (until the next scheduled/manual
    # sync) because this loop only ever looked at the calendar directly.
    # Running it every cycle caps that staleness at 30 minutes.
    try:
        text, imported, completed = run_belki_sync()
        if imported or completed:
            await dm_user(text)
    except Exception as e:
        log(f"[SYNC] nag-cycle Belki sync failed: {e}")

    current_overdue = get_overdue_tether_events()
    current_ids = {e["id"] for e in current_overdue}
    unacknowledged_overdue = unacknowledged_overdue & current_ids
    for e in current_overdue:
        unacknowledged_overdue.add(e["id"])
    if not unacknowledged_overdue:
        return
    event_map = {e["id"]: e for e in current_overdue}
    pending = [event_map[eid] for eid in unacknowledged_overdue if eid in event_map]
    if not pending:
        return
    lines = [nag_summary_header(nag_count)]
    for e in pending:
        eid = e["id"]
        lines.append(nag_message(e, task_nag_counts.get(eid, 0)))
        task_nag_counts[eid] = task_nag_counts.get(eid, 0) + 1
    await dm_user("\n".join(lines))
    nag_count += 1
    log(f"[NAG #{nag_count}] {len(pending)} unacknowledged tasks")


async def midnight_nag_persist():
    global nag_count, unacknowledged_overdue, task_nag_counts
    trim_log()
    if not unacknowledged_overdue:
        nag_count = 0
        task_nag_counts.clear()
        return
    current_overdue = get_overdue_tether_events()
    event_map = {e["id"]: e for e in current_overdue}
    for eid in list(unacknowledged_overdue):
        if eid not in event_map:
            continue
        event = get_service().events().get(calendarId="primary", eventId=eid).execute()
        meta = parse_meta(event)
        desc = event.get("description", "") or ""
        orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
        original_due = (
            orig_match.group(1) if orig_match else event["start"]["dateTime"][:10]
        )
        meta["nag_ignored"] = meta.get("nag_ignored", 0) + 1
        meta["last_modified"] = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
        new_desc = build_meta(**meta) + f"\nOriginally due: {original_due}"
        event["description"] = new_desc
        get_service().events().update(calendarId="primary", eventId=eid, body=event).execute()
        ledger.record_nag_ignored(event, nag_count)
        log(f"[MIDNIGHT] incremented nag_ignored on {event.get('summary')}")
    nag_count = 0
    task_nag_counts.clear()
    await dm_user(
        f"🌙 Midnight check-in: {len(unacknowledged_overdue)} task(s) still unacknowledged. "
        f"They'll be waiting when you're back."
    )


# --- SCHEDULED JOBS ---
async def morning_briefing():
    today = datetime.datetime.now(TORONTO_TZ).date()
    overdue = get_overdue_tether_events()
    overdue_lines = []
    for e in overdue:
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        overdue_lines.append(f"• ⚠️ {name} — OVERDUE")

    all_events = list_upcoming_events(20)
    today_events = [
        e
        for e in all_events
        if e.get("start", {}).get("dateTime", "").startswith(str(today))
    ]
    cal_lines = []
    for e in today_events:
        start = e["start"]["dateTime"][11:16]
        end = e["end"]["dateTime"][11:16]
        cal_lines.append(f"• {start}–{end} {e['summary']}")

    deadlines = get_tether_deadlines()
    next_line = ""
    if deadlines:
        top = rank_deadlines(deadlines)[0]
        name = top["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        reason = priority_reason(top)
        next_line = f"**Next up:** {name} — {reason}."

    lines = ["📅 **Morning briefing**\n"]
    if overdue_lines:
        lines.append("**Overdue:**")
        lines.extend(overdue_lines)
        lines.append("")
    if cal_lines:
        lines.append("**Today's schedule:**")
        lines.extend(cal_lines)
        lines.append("")
    else:
        lines.append("**Today's schedule:** Nothing on the calendar.")
        lines.append("")
    if next_line:
        lines.append(next_line)

    await dm_user("\n".join(lines))


async def eod_sweep():
    """EOD sweep — nag loop handles primary pressure; this just logs."""
    overdue = get_overdue_tether_events()
    if not overdue:
        await dm_user("✅ EOD — no overdue tasks. Queue is clean.")
    log(f"[EOD] {len(overdue)} overdue at EOD")


async def deadline_warning():
    upcoming = get_events_in_window(hours_from_now=24)
    today = datetime.datetime.now(TORONTO_TZ).date()
    for e in upcoming:
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        start = datetime.datetime.fromisoformat(e["start"]["dateTime"])
        if start.tzinfo is None:
            start = TORONTO_TZ.localize(start)
        due = e["start"]["dateTime"][11:16]
        when = "today" if start.date() == today else "tomorrow"
        await dm_user(f"⏰ **Heads up:** {name} is due {when} at {due}.")


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
    retro = ledger.retro_lines()
    if retro:
        lines.append("")
        lines.extend(retro)
    await dm_user("\n".join(lines))


async def inactivity_check():
    deadlines = get_tether_deadlines()
    if not deadlines:
        return
    today = datetime.datetime.now(TORONTO_TZ).date()
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
        >= 5
        for e in deadlines
    )
    if all_stale:
        await dm_user(
            "📋 No queue changes in 5 days. Still accurate? Reply in #tether."
        )


def already_ran_today(keyword: str) -> bool:
    today = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    if not os.path.exists(LOG_FILE):
        return False
    with open(LOG_FILE, "r") as f:
        for line in f:
            if today in line and keyword in line:
                return True
    return False


def run_belki_sync(project_override: str = None) -> tuple[str, int, int]:
    # Overdue events are included so a task finished late in Belki still
    # gets auto-completed and cleared, not just on-time ones.
    all_events = get_tether_deadlines() + get_overdue_tether_events()
    return belki_import.sync(
        all_events,
        _insert_deadline,
        complete_deadline=complete_task,
        project_override=project_override,
    )


async def run_morning_jobs():
    if already_ran_today("Morning briefing"):
        return
    await morning_briefing()
    await deadline_warning()
    try:
        text, imported, completed = run_belki_sync()
        if imported or completed:
            await dm_user(text)
    except Exception as e:
        log(f"[SYNC] morning Belki sync failed: {e}")


async def run_sunday_morning():
    await run_morning_jobs()
    if not already_ran_today("Weekly Queue"):
        await weekly_queue_summary()


# --- PUSH VALIDATION ---
def validate_push_command(command: dict) -> tuple[bool, str]:
    if not command.get("push_reason"):
        return (
            False,
            "❌ Push rejected — you need to include a reason. Example: `@Tether push youtube video because I need to film first`",
        )
    return True, ""


def pick_push_date(
    task_name: str, push_reason: str, current_queue: list, original_message: str = ""
) -> tuple[str, bool]:
    today = datetime.datetime.now(TORONTO_TZ).strftime("%A, %B %d, %Y")
    queue_summary = (
        "\n".join(
            f"- {e.get('summary','').replace(DEADLINE_PREFIX,'').replace('— DUE','').strip()} "
            f"due {e.get('start',{}).get('dateTime','')[:10]}"
            for e in current_queue[:10]
        )
        or "Queue is empty."
    )
    message_context = (
        f"Full user message: {original_message}\n" if original_message else ""
    )
    prompt = (
        f"Today is {today}.\n"
        f"Task: {task_name}\n"
        f"Reason for pushing: {push_reason}\n"
        f"{message_context}"
        f"Current queue:\n{queue_summary}\n\n"
        f"Pick a realistic new deadline for this task. "
        f"IMPORTANT: if the user's message mentions a timeframe (e.g. 'a week or 2', 'a few days', 'next month'), "
        f"honour the longer end of that range. Do not pick a date shorter than what they implied. "
        f"Do not pile deadlines on top of each other unless unavoidable. "
        f"Respond with ONLY a date in YYYY-MM-DD format. No explanation."
    )
    for model in MODELS:
        try:
            response = gemini_client.models.generate_content(
                model=model, contents=prompt
            )
            raw = response.text.strip()
            datetime.date.fromisoformat(raw)
            return raw, False
        except Exception:
            continue
    fallback = datetime.datetime.now(TORONTO_TZ) + datetime.timedelta(days=7)
    return fallback.strftime("%Y-%m-%d"), True


def next_sunday_with_capacity(from_date, deadlines, need, exclude_event_id=None) -> str:
    """First Sunday on/after from_date whose week can absorb `need` more evenings."""
    others = [e for e in deadlines if e.get("id") != exclude_event_id]
    usage = belki_import.week_usage(others)
    d = from_date + datetime.timedelta(days=(6 - from_date.weekday()) % 7)
    while True:
        used = usage.get(d.isoformat(), 0)
        if used == 0 or used + need <= belki_import.EVENINGS_PER_WEEK:
            return d.isoformat()
        d += datetime.timedelta(days=7)


def resolve_push_date(
    command: dict, event: dict, meta: dict, name: str, push_reason: str,
    current_queue: list, content: str,
) -> tuple[str, bool]:
    """Deterministic push-date precedence: explicit timeframe > estimate arithmetic > AI fallback.

    Returns (date, ai_defaulted) matching pick_push_date's contract; the flag is
    only True when every model failed and the hardcoded 7-day fallback was used.
    """
    today = datetime.datetime.now(TORONTO_TZ).date()

    target = command.get("target_date")
    if target:
        try:
            if datetime.date.fromisoformat(target) > today:
                return target, False
        except ValueError:
            pass
    delta = command.get("delta_days")
    if delta:
        try:
            delta = int(delta)
            if delta > 0:
                return (today + datetime.timedelta(days=delta)).isoformat(), False
        except (TypeError, ValueError):
            pass

    # Belki estimates are integer evenings; 1 evening ≈ 1 calendar day of runway
    estimate = meta.get("estimate")
    if estimate:
        try:
            est = int(estimate)
            candidate = today + datetime.timedelta(days=est)
            return next_sunday_with_capacity(
                candidate, current_queue, est, exclude_event_id=event.get("id")
            ), False
        except (TypeError, ValueError):
            pass

    return pick_push_date(name, push_reason, current_queue, original_message=content)


async def handle_push_with_reason(command: dict, content: str, message):
    task_title = command.get("task_title") or ""
    push_reason = command.get("push_reason", "")
    all_tasks = get_overdue_tether_events() + get_tether_deadlines()
    matches = [
        e for e in all_tasks if task_matches(task_title, e)
    ]
    if not matches:
        await message.reply(
            f"No task matching **{task_title}** found.", mention_author=False
        )
        return
    if len(matches) > 1:
        names = ", ".join(
            e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
            for e in matches
        )
        await message.reply(
            f"Multiple matches: {names}. Be more specific.", mention_author=False
        )
        return
    e = matches[0]
    event_id = e["id"]
    meta = parse_meta(e)
    desc = e.get("description", "") or ""
    orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
    original_due = orig_match.group(1) if orig_match else e["start"]["dateTime"][:10]
    old_due = e["start"]["dateTime"][:10]
    name = clean_name(e["summary"])

    current_queue = get_tether_deadlines()
    target_date, date_fallback = resolve_push_date(
        command, e, meta, name, push_reason, current_queue, content
    )

    new_start = f"{target_date}T23:59:00"
    e["start"] = {"dateTime": new_start, "timeZone": "America/Toronto"}
    e["end"] = {"dateTime": new_start, "timeZone": "America/Toronto"}

    meta["pushes"] = meta["pushes"] + 1
    meta["last_modified"] = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    meta["last_push_reason"] = push_reason
    new_desc = build_meta(**meta) + f"\nOriginally due: {original_due}"
    e["description"] = new_desc

    get_service().events().update(calendarId="primary", eventId=event_id, body=e).execute()
    ledger.record_pushed(e, target_date, push_reason, "user_push", old_due=old_due)
    unacknowledged_overdue.discard(event_id)
    task_nag_counts.pop(event_id, None)

    fallback_note = " _(AI unavailable — defaulted to 7 days)_" if date_fallback else ""
    await message.reply(
        f"📅 **{name}** pushed to **{target_date}**. Reason logged: _{push_reason}_. "
        f"Push #{meta['pushes']}.{fallback_note}",
        mention_author=False,
    )
    log(
        f"[PUSH] {name} → {target_date} | reason: {push_reason} | pushes: {meta['pushes']}"
    )


async def handle_keep(command: dict, message):
    task_title = command.get("task_title") or ""
    all_tasks = get_overdue_tether_events() + get_tether_deadlines()
    matches = [
        e for e in all_tasks if task_matches(task_title, e)
    ]
    if not matches:
        await message.reply(
            f"No task matching **{task_title}** found.", mention_author=False
        )
        return
    e = matches[0]
    event_id = e["id"]
    name = clean_name(e["summary"])
    unacknowledged_overdue.discard(event_id)
    ledger.record_kept(e)
    await message.reply(
        f"✅ Got it — **{name}** stays. I'll keep watching it.",
        mention_author=False,
    )
    log(f"[KEEP] {name} acknowledged")


@bot.event
async def on_ready():
    global nag_count, unacknowledged_overdue, task_nag_counts, _started
    if _started:
        # on_ready fires on every gateway reconnect, not just the first connect.
        # Without this guard, each reconnect stacks a new AsyncIOScheduler on top
        # of the still-running one, so jobs fire multiple times per cron tick.
        return
    _started = True

    nag_count = 0
    unacknowledged_overdue = set()
    task_nag_counts.clear()

    await asyncio.sleep(3)  # Give Discord state time to settle

    print(f"Tether is online as {bot.user}")

    try:
        backfilled = ledger.backfill_from_events(
            get_tether_deadlines() + get_overdue_tether_events(), parse_meta
        )
        if backfilled:
            log(f"[LEDGER] backfilled {backfilled} existing task(s)")
    except Exception as e:
        log(f"[LEDGER] backfill failed: {e}")

    # Startup Belki sync: pick up tasks written to the vault since the last
    # run, and clear anything marked done there. Silent unless something
    # actually happened — restarts must not DM "no active project" /
    # "nothing to import" noise.
    try:
        text, imported, completed = run_belki_sync()
        log(f"[SYNC] startup: imported={imported} completed={completed}")
        if imported or completed:
            await dm_user(text)
    except Exception as e:
        log(f"[SYNC] startup Belki sync failed: {e}")

    scheduler = AsyncIOScheduler(timezone=TORONTO_TZ)

    if TEST_MODE:
        print("TEST MODE — all jobs fire in 2 minutes")
        fire_at = datetime.datetime.now(TORONTO_TZ) + datetime.timedelta(minutes=2)
        jobs = [send_overdue_nag, eod_sweep, inactivity_check, weekly_queue_summary]
        if MORNING_BRIEFING_ENABLED:
            jobs.append(run_morning_jobs)
        for job in jobs:
            scheduler.add_job(job, "date", run_date=fire_at, misfire_grace_time=60)
    else:
        scheduler.add_job(
            send_overdue_nag,
            CronTrigger(minute="*/30", timezone=TORONTO_TZ),
            misfire_grace_time=None,
            coalesce=True,
        )
        scheduler.add_job(
            midnight_nag_persist,
            CronTrigger(hour=0, minute=0, timezone=TORONTO_TZ),
            misfire_grace_time=None,
            coalesce=True,
        )
        if MORNING_BRIEFING_ENABLED:
            scheduler.add_job(
                run_morning_jobs,
                CronTrigger(day_of_week="mon-sat", hour=9, minute=0, timezone=TORONTO_TZ),
                misfire_grace_time=None,
                coalesce=True,
            )
            scheduler.add_job(
                run_sunday_morning,
                CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=TORONTO_TZ),
                misfire_grace_time=None,
                coalesce=True,
            )
        else:
            scheduler.add_job(
                weekly_queue_summary,
                CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=TORONTO_TZ),
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
            inactivity_check,
            CronTrigger(hour=17, minute=0, timezone=TORONTO_TZ),
            misfire_grace_time=None,
            coalesce=True,
        )

        await send_overdue_nag()

        now = datetime.datetime.now(TORONTO_TZ)
        if now.hour >= 9:
            is_sunday = now.weekday() == 6
            if MORNING_BRIEFING_ENABLED:
                if is_sunday:
                    await run_sunday_morning()
                else:
                    await run_morning_jobs()
            elif is_sunday and not already_ran_today("Weekly Queue"):
                await weekly_queue_summary()
        if now.hour >= 17 and not already_ran_today("No queue changes"):
            await inactivity_check()
        if now.hour >= 22 and not already_ran_today("EOD"):
            await eod_sweep()

    scheduler.start()


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
            # If user is responding to a clarification, route directly to scheduler session
            if user_id in pending_clarifications:
                pending_clarifications.pop(user_id)
                chat = get_or_create_session(user_id)
                today = datetime.datetime.now(TORONTO_TZ)
                next_sunday = get_next_sunday(today)
                follow_up_prompt = (
                    f"[DATE] Today is {today.strftime('%A, %B %d, %Y')}. "
                    f"Next Sunday is {next_sunday.strftime('%B %d, %Y')} — use {next_sunday.strftime('%Y-%m-%d')}T23:59:00 as the default deadline.\n\n"
                    f"User follow-up: {content}"
                )
                response, _ = send_with_fallback(chat, user_id, follow_up_prompt)
                reply = extract_reply(response, chat)
                log(f"[CLARIFICATION REPLY to {message.author}] {reply[:200]}")
                for i in range(0, len(reply), 2000):
                    await message.reply(reply[i : i + 2000], mention_author=False)
                return

            command, _ = parse_intent_with_fallback(content)
            log(f"[INTENT] {message.author}: {json.dumps(command)}")

            if command.get("needs_clarification"):
                pending_clarifications[user_id] = content
                question = command.get("clarification_question", "Could you clarify?")
                await message.reply(f"❓ {question}", mention_author=False)
                return

            # --- KEEP ---
            if command.get("action") == "keep":
                await handle_keep(command, message)
                return

            # --- SYNC (Belki import) ---
            if command.get("action") == "sync":
                text, imported, completed = run_belki_sync(command.get("task_title"))
                log(f"[SYNC] imported={imported} completed={completed} | {text[:200]}")
                for i in range(0, len(text), 2000):
                    await message.reply(text[i : i + 2000], mention_author=False)
                return

            # --- PUSH ---
            if command.get("action") == "push":
                is_valid, error_msg = validate_push_command(command)
                if not is_valid:
                    await message.reply(error_msg, mention_author=False)
                    return
                await handle_push_with_reason(command, content, message)
                return

            # --- NEXT ---
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

            # --- COMPLETE ---
            if command.get("action") == "complete":
                task_title = command.get("task_title") or ""
                deadlines = get_tether_deadlines()
                overdue = get_overdue_tether_events()
                all_tasks = overdue + deadlines
                matches = [e for e in all_tasks if task_matches(task_title, e)]
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
                get_service().events().delete(calendarId="primary", eventId=e["id"]).execute()
                ledger.record_completed(e)
                unacknowledged_overdue.discard(e["id"])
                task_nag_counts.pop(e["id"], None)
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

            # --- QUERY ---
            if command.get("action") == "query":
                task_title = command.get("task_title") or ""
                deadlines = get_tether_deadlines()
                matches = [e for e in deadlines if task_matches(task_title, e)]
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

            # --- PASS TO SCHEDULER SESSION ---
            chat = get_or_create_session(user_id)
            today = datetime.datetime.now(TORONTO_TZ)
            next_sunday = get_next_sunday(today)
            scheduler_prompt = (
                f"[DATE] Today is {today.strftime('%A, %B %d, %Y')}. "
                f"Next Sunday is {next_sunday.strftime('%B %d, %Y')} — use {next_sunday.strftime('%Y-%m-%d')}T23:59:00 as the default deadline.\n\n"
                f"[PARSED INTENT] {json.dumps(command)}\n\nOriginal message: {content}"
            )
            response, _ = send_with_fallback(chat, user_id, scheduler_prompt)
            reply = extract_reply(response, chat)

            # If scheduler responded with a question, flag user as pending clarification
            if reply.strip().endswith("?"):
                pending_clarifications[user_id] = content

            log(f"[REPLY to {message.author}] {reply[:200]}")
            for i in range(0, len(reply), 2000):
                await message.reply(reply[i : i + 2000], mention_author=False)

        except Exception as e:
            print(f"ERROR: {e}")
            await message.reply(
                "⚠️ Tether hit an upstream connection issue.", mention_author=False
            )


if __name__ == "__main__":
    bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
