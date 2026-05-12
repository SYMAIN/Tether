import os
import datetime
import discord
import pytz
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- CONFIG ---
SCOPES = ["https://www.googleapis.com/auth/calendar"]
ALLOWED_CHANNEL = "tether"
DEADLINE_PREFIX = "⏰"
TORONTO_TZ = pytz.timezone("America/Toronto")
DISCORD_USER_ID = int(os.environ.get("DISCORD_USER_ID"))


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
def create_calendar_event(summary: str, start_time: str, end_time: str):
    """Creates a calendar event. Times must be ISO format (YYYY-MM-DDTHH:MM:SS)."""
    event = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": "America/Toronto"},
        "end": {"dateTime": end_time, "timeZone": "America/Toronto"},
        "colorId": "5",
    }
    return service.events().insert(calendarId="primary", body=event).execute()


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
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]


def get_next_sunday(from_date):
    days_ahead = 6 - from_date.weekday()  # Sunday = 6
    if days_ahead == 0:
        days_ahead = 7
    return from_date + datetime.timedelta(days=days_ahead)


def get_chat(model=None):
    today = datetime.datetime.now(TORONTO_TZ)
    next_sunday = get_next_sunday(today)
    date_context = (
        f"\n\n## CURRENT DATE (DO NOT IGNORE)\n"
        f"Today: {today.strftime('%A, %B %d, %Y')}\n"
        f"Next Sunday: {next_sunday.strftime('%A, %B %d, %Y')}\n"
        f"Default deadline date: {next_sunday.strftime('%B %d, %Y')} at 11:59 PM\n"
        f"Default deadline ISO: {next_sunday.strftime('%Y-%m-%d')}T23:59:00\n\n"
        f"When creating a deadline event, use EXACTLY this start and end time unless the user specifies otherwise:\n"
        f"start_time: {next_sunday.strftime('%Y-%m-%d')}T23:59:00\n"
        f"end_time: {next_sunday.strftime('%Y-%m-%d')}T23:59:00\n"
    )
    return client.chats.create(
        model=model or MODELS[0],
        config=types.GenerateContentConfig(
            system_instruction=instructions + date_context,
            tools=[create_calendar_event, delete_calendar_event, list_upcoming_events],
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
            if "503" in str(e) or "UNAVAILABLE" in str(e):
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
        await dm_user(
            f"⚠️ **{name}** wasn't completed today. Keep it this Sunday or push back one week? Reply in #tether."
        )


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
        lines.append(f"• {name} — due {date_str}")
    await dm_user("\n".join(lines))


@bot.event
async def on_ready():
    print(f"Tether is online as {bot.user}")
    scheduler = AsyncIOScheduler(timezone=TORONTO_TZ)
    scheduler.add_job(
        morning_briefing, CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ)
    )
    scheduler.add_job(eod_sweep, CronTrigger(hour=22, minute=0, timezone=TORONTO_TZ))
    scheduler.add_job(
        deadline_warning, CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ)
    )
    scheduler.add_job(
        weekly_queue_summary,
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=TORONTO_TZ),
    )
    scheduler.start()


@bot.event
async def on_ready():
    print(f"Tether is online as {bot.user}")
    scheduler = AsyncIOScheduler(timezone=TORONTO_TZ)

    test_time = datetime.datetime.now(TORONTO_TZ) + datetime.timedelta(minutes=2)
    trigger = CronTrigger(
        hour=test_time.hour, minute=test_time.minute, timezone=TORONTO_TZ
    )

    scheduler.add_job(
        morning_briefing, CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ)
    )

    scheduler.add_job(eod_sweep, CronTrigger(hour=22, minute=0, timezone=TORONTO_TZ))
    scheduler.add_job(
        deadline_warning, CronTrigger(hour=9, minute=0, timezone=TORONTO_TZ)
    )
    scheduler.add_job(
        weekly_queue_summary,
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=TORONTO_TZ),
    )


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.channel.name != ALLOWED_CHANNEL:
        return

    user_id = message.author.id
    if user_id not in user_sessions:
        user_sessions[user_id] = get_chat(MODELS[0])
        user_model_index[user_id] = 0

    chat = user_sessions[user_id]

    async with message.channel.typing():
        try:
            response, model_used = send_with_fallback(chat, user_id, message.content)

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

            if model_used != MODELS[0]:
                reply = f"⚠️ Using fallback model `{model_used}`.\n\n" + reply

            for i in range(0, len(reply), 2000):
                await message.channel.send(reply[i : i + 2000])

        except Exception as e:
            await message.channel.send(f"⚠️ {str(e)}")


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
