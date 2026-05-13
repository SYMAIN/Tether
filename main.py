import os
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


# --- Meta Data ---
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
    today = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
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
    original_due = None

    # Extract original due date from description
    desc = event.get("description", "") or ""
    match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
    if match:
        original_due = match.group(1)

    meta["pushes"] = pushes
    meta["last_modified"] = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d")
    new_due = event["start"]["dateTime"][:10]

    description = build_meta(**meta)
    if original_due and original_due != new_due:
        description += f"\nOriginally due: {original_due}"
    elif original_due:
        description += f"\nOriginally due: {original_due}"

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
    events_result = (
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
    return events_result.get("items", [])


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
    """Returns Tether deadlines that ended before now."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    now_local = datetime.datetime.now(TORONTO_TZ)
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
        end = datetime.datetime.fromisoformat(end_str)
        if end.tzinfo is None:
            end = TORONTO_TZ.localize(end)
        # Only flag if it ended today
        if end.date() == now_local.date():
            overdue.append(e)
    return overdue


# --- GEMINI SETUP ---
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

with open("agent.md", "r") as f:
    instructions = f.read()

MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]


def get_next_sunday(from_date):
    days_ahead = (6 - from_date.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return from_date + datetime.timedelta(days=days_ahead)


def get_chat(model=None):
    today = datetime.datetime.now(TORONTO_TZ)
    next_sunday = get_next_sunday(today)
    date_context = (
        f"## CURRENT DATE (MANDATORY — USE THIS, IGNORE TRAINING DATA)\n"
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Next Sunday: {next_sunday.strftime('%B %d, %Y')}\n"
        f"Default deadline ISO: {next_sunday.strftime('%Y-%m-%d')}T23:59:00\n\n"
    )
    return client.chats.create(
        model=model or MODELS[0],
        config=types.GenerateContentConfig(
            system_instruction=date_context + instructions,  # date FIRST
            tools=[
                create_calendar_event,
                delete_calendar_event,
                list_upcoming_events,
                update_event_meta,
            ],
        ),
    )


def send_with_fallback(chat, user_id, message_content):
    current_model_index = user_model_index.get(user_id, 0)
    for i in range(current_model_index, len(MODELS)):
        try:
            chat = get_chat(MODELS[i])
            user_sessions[user_id] = chat
            user_model_index[user_id] = i
            response = chat.send_message(message_content)
            if i != 0:
                user_model_index[user_id] = 0
                user_sessions[user_id] = get_chat(MODELS[0])
            return response, MODELS[i]
        except Exception as e:
            if (
                "503" in str(e)
                or "UNAVAILABLE" in str(e)
                or "429" in str(e)
                or "RESOURCE_EXHAUSTED" in str(e)
                or "RemoteProtocolError" in str(e)
                or "incomplete chunked read" in str(e)
            ):
                continue
            raise e
    raise Exception("All models are currently unavailable. Please try again later.")


user_sessions = {}
user_model_index = {}

# --- DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


async def dm_user(message: str):
    user = await bot.fetch_user(DISCORD_USER_ID)
    await user.send(message)


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
    lines = ["📋 **Weekly Queue:**\n"]
    for e in deadlines:
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
        lines.append(f"• {name} — due {date_str}{suffix}")
    await dm_user("\n".join(lines))


async def inactivity_check():
    deadlines = get_tether_deadlines()
    if not deadlines:
        return
    today = datetime.datetime.now(TORONTO_TZ).date()
    stale_threshold = 5
    all_stale = all(
        (
            today
            - datetime.date.fromisoformat(
                parse_meta(e).get("last_modified", str(today))
            )
        ).days
        >= stale_threshold
        for e in deadlines
    )
    if all_stale:
        await dm_user(
            "📋 No queue changes in 5 days. Still accurate? Reply in #tether."
        )


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

    # Run missed jobs on startup
    now = datetime.datetime.now(TORONTO_TZ)
    if now.hour >= 9:
        await morning_briefing()
        await deadline_warning()
        await inactivity_check()
    if now.hour >= 22:
        await eod_sweep()
    if now.weekday() == 6 and now.hour >= 9:  # Sunday
        await weekly_queue_summary()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if bot.user not in message.mentions:
        return

    content = message.content.replace(f"<@{bot.user.id}>", "").strip()
    content = content.replace(f"<@!{bot.user.id}>", "").strip()

    if not content:
        return

    user_id = message.author.id
    if user_id not in user_sessions:
        user_sessions[user_id] = get_chat(MODELS[0])
        user_model_index[user_id] = 0

    chat = user_sessions[user_id]

    async with message.channel.typing():
        try:
            response, model_used = send_with_fallback(chat, user_id, content)

            text_parts = [
                part.text
                for part in response.candidates[0].content.parts
                if hasattr(part, "text") and part.text
            ]

            if text_parts:
                reply = "".join(text_parts)
            else:
                follow_up = user_sessions[user_id].send_message(
                    "Confirm what you just did in one line."
                )
                reply = follow_up.text or "Done."

            for i in range(0, len(reply), 2000):
                await message.reply(reply[i : i + 2000], mention_author=False)

        except Exception as e:
            print(f"ERROR: {e}")
            await message.reply(
                f"⚠️ Tether hit an upstream connection issue.", mention_author=False
            )


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
