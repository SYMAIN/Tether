import os
import datetime
import discord
from google import genai
from google.genai import types
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# --- CONFIG ---
SCOPES = ["https://www.googleapis.com/auth/calendar"]
ALLOWED_CHANNEL = "tether"  # bot only responds in a channel named "tether"


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


def list_upcoming_events(max_results: int = 20):
    """Returns upcoming events to find free focus blocks."""
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


# --- GEMINI SETUP ---
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

with open("agent.md", "r") as f:
    instructions = f.read()


MODELS = [
    "gemini-3-flash-preview",  # Best — Pro-level reasoning at Flash speed
    "gemini-3.1-pro-preview",  # Strongest reasoning, use as backup
    "gemini-3.1-flash-lite-preview",  # Cost-efficient, good for high volume
    "gemini-2.5-pro",  # Stable fallback
    "gemini-2.5-flash",  # Stable, well tested
    "gemini-2.5-flash-lite",  # Lightest stable option
    "gemini-2.0-flash",  # Last resort (shuts down June 1, 2026)
]


def get_chat(model=None):
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))
    date_context = f"\n\n## Current Date & Time\nToday is {today.strftime('%A, %B %d, %Y')}. Current time is {today.strftime('%I:%M %p')} EDT."
    return client.chats.create(
        model=model or MODELS[0],
        config=types.GenerateContentConfig(
            system_instruction=instructions + date_context,
            tools=[create_calendar_event, list_upcoming_events],
        ),
    )


def send_with_fallback(chat, user_id, message_content):
    current_model_index = user_model_index.get(user_id, 0)

    for i in range(current_model_index, len(MODELS)):
        try:
            # If we need to switch models, create a new chat
            if i != current_model_index:
                user_sessions[user_id] = get_chat(MODELS[i])
                user_model_index[user_id] = i
                chat = user_sessions[user_id]

            response = chat.send_message(message_content)

            # Reset to primary model after success if we had switched
            if i != 0:
                user_model_index[user_id] = 0
                user_sessions[user_id] = get_chat(MODELS[0])

            return response, MODELS[i]

        except Exception as e:
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                print(f"Model {MODELS[i]} unavailable, trying next...")
                continue
            raise e

    raise Exception("All models are currently unavailable. Please try again later.")


# One chat session per Discord user
user_sessions = {}
user_model_index = {}

# --- DISCORD BOT ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


@bot.event
async def on_ready():
    print(f"Tether is online as {bot.user}")


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

            text_parts = []
            for part in response.candidates[0].content.parts:
                if hasattr(part, "text") and part.text:
                    text_parts.append(part.text)

            if text_parts:
                reply = "".join(text_parts)
            else:
                follow_up = user_sessions[user_id].send_message(
                    "Confirm what you just scheduled in a brief summary."
                )
                reply = follow_up.text or "Done."

            # Notify if fallback model was used
            if model_used != MODELS[0]:
                reply = (
                    f"⚠️ Primary model unavailable, using `{model_used}`.\n\n" + reply
                )

            if len(reply) > 2000:
                for i in range(0, len(reply), 2000):
                    await message.channel.send(reply[i : i + 2000])
            else:
                await message.channel.send(reply)

        except Exception as e:
            await message.channel.send(f"⚠️ {str(e)}")


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
