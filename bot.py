"""A lightweight Telegram reminder bot. Uses only Python's standard library."""

from __future__ import annotations

import calendar
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "data" / "reminders.db"
POLL_TIMEOUT_SECONDS = 25
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
SCHEDULE_CHANGED = threading.Event()
GOOGLE_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"


class BotReply(Exception):
    """A conversational response that should be sent directly to the user."""

    def __init__(self, message: str, needs_clarification: bool = False):
        super().__init__(message)
        self.needs_clarification = needs_clarification


class BotAction(Exception):
    """A request that must be handled by local bot functionality."""

    def __init__(self, action: str, query: str = ""):
        super().__init__(action)
        self.action = action
        self.query = query


class GoogleCalendarNotConnected(Exception):
    """Raised when the one-time Google OAuth setup has not been completed."""


def load_env_file() -> None:
    """Load local .env values without a dependency."""
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def connect() -> sqlite3.Connection:
    DATABASE.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def initialize_database() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata'
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                due_at_utc TEXT NOT NULL,
                recurrence TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at_utc TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_due_reminders
                ON reminders(active, due_at_utc);
            CREATE TABLE IF NOT EXISTS pending_clarifications (
                chat_id INTEGER PRIMARY KEY,
                original_message TEXT NOT NULL
            );
            """
        )
        # A clarification only makes sense in the same uninterrupted chat flow.
        # Never carry an unfinished question across a bot restart.
        db.execute("DELETE FROM pending_clarifications")
        reminder_columns = {row[1] for row in db.execute("PRAGMA table_info(reminders)")}
        if "google_event_id" not in reminder_columns:
            db.execute("ALTER TABLE reminders ADD COLUMN google_event_id TEXT")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS google_accounts (
                chat_id INTEGER PRIMARY KEY,
                token_json TEXT NOT NULL,
                email TEXT,
                connected_at_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS google_oauth_pending (
                chat_id INTEGER PRIMARY KEY,
                state TEXT NOT NULL
            );
            """
        )


class Telegram:
    def __init__(self, token: str):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, payload: Optional[dict] = None) -> dict:
        body = urllib.parse.urlencode(payload or {}).encode()
        request = urllib.request.Request(self.base_url + method, data=body, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            details = error.read().decode(errors="replace")
            raise RuntimeError(f"Telegram API error {error.code}: {details}") from error
        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Telegram request failed"))
        return result["result"]

    def send(self, chat_id: int, text: str, keyboard: Optional[dict] = None, parse_mode: Optional[str] = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = json.dumps(keyboard)
        if parse_mode:
            payload["parse_mode"] = parse_mode
        self.call("sendMessage", payload)


def response_text(response: dict) -> str:
    """Extract generated text from an OpenAI-compatible chat response."""
    try:
        return response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("The AI did not return a usable answer.") from error


def parse_reminder_with_ai(message: str, tz: ZoneInfo) -> tuple[str, datetime, Optional[str], bool]:
    """Turn flexible natural language into a reminder or calendar event using OpenRouter."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise ValueError("AI parsing is not configured. Add OPENROUTER_API_KEY to .env.")
    now = datetime.now(tz)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string", "enum": ["reminder", "clarify", "list_reminders", "answer", "delete_calendar"]},
            "title": {"type": "string"},
            "due_local": {"type": ["string", "null"], "description": "ISO-8601 local datetime without timezone, or null"},
            "end_local": {"type": ["string", "null"], "description": "ISO-8601 local end datetime without timezone when the user gives a range, else null"},
            "recurrence": {"type": ["string", "null"], "enum": ["daily", "weekdays", "weekly:0", "weekly:1", "weekly:2", "weekly:3", "weekly:4", "weekly:5", "weekly:6", "monthly", None]},
            "add_to_google_calendar": {"type": "boolean"},
            "reply": {"type": "string"},
        },
        "required": ["kind", "title", "due_local", "end_local", "recurrence", "add_to_google_calendar", "reply"],
    }
    instructions = (
        "You are a friendly Telegram reminder assistant that understands how a normal person talks. "
        "Handle imperfect grammar, spelling mistakes (calander, calender, schdeulde, meet vs meeting), "
        "messy word order (task after the date), casual English, Hindi in Latin script, and Hinglish. "
        "Reply in the same language and conversational style as the user. "
        f"The user's timezone is {tz.key}; the current local date-time is {now.isoformat()}. "
        "Interpret relative dates from that current date-time. Tomorrow is the next calendar date; "
        "day after tomorrow is two dates later. Hindi/Hinglish 'kal' means tomorrow and 'parso' means day after tomorrow. "
        "Do not invent a date or time. "
        "If the date is given but the time is missing, use kind clarify and ask what time. "
        "If a 1-12 clock time has no AM/PM, ask AM or PM. Times written as 6pm, 7pm, 6 pm, or 18:00 already include PM — do not ask. "
        "Ask exactly one short clarification in the user's own language and style. "
        "If the user asks what reminders are saved, pending, upcoming, or in their list, use kind list_reminders. "
        "Never claim that you cannot access the reminder list. "
        "This bot already uses one owner Google Calendar. If the user asks to connect, link, or disconnect a Gmail, use kind answer and say other Google accounts cannot be connected. "
        "If a normal person wants to delete, cancel, or remove a meeting/event from Google Calendar "
        "(including typos and Hinglish like hatao/hatado), use kind delete_calendar. "
        "Put the meeting name or person in title, due_local null, add_to_google_calendar false. "
        "If the message is a question or conversation rather than a reminder, use kind answer, due_local null, "
        "recurrence null, add_to_google_calendar false, and answer concisely in reply. "
        "Set add_to_google_calendar true whenever a normal person wants an event on Google Calendar, including messy English, Hindi, and Hinglish. "
        "Examples that MUST be calendar events: 'schedule my google meet', 'google meet laga do', "
        "'calendar me schedule kro', 'calander pe meeting rakhdo', 'meet schedule kar dena', "
        "'kal 4 baje milna fix karo', 'gmeet book karo', 'appointment daal do calendar me'. "
        "Treat google meet / gmeet / milna / mulaqat / meeting as calendar events when the user is scheduling them. "
        "If they give a date/time but no event name, still schedule it: title 'Google Meet' when they said meet/gmeet, otherwise 'Meeting'. Do not ask for a title. "
        "In that case title should be a short event name such as 'Meeting with Harsh' or 'Google Meet', not the scheduling words. "
        "Ordinary 'remind me' messages with no calendar/meet/meeting intent must keep add_to_google_calendar false. "
        "For recurring reminders use only the recurrence enum supplied. weekly:0 means Monday. "
        "For a successful reminder or calendar event, set kind reminder, a concise title, and due_local as a future ISO-8601 local datetime. "
        "If the user gives a time range such as 'from 4.10 to 4.15 pm', set due_local to the start and end_local to the end. "
        "Do not invent a 30-minute block when they specified an end time. If there is no end time, set end_local null. "
        "The reply should be brief, and must not claim the reminder has already been saved."
    )
    payload = {
        "model": os.getenv("OPENROUTER_MODEL", "google/gemini-3.6-flash"),
        "messages": [{"role": "system", "content": instructions}, {"role": "user", "content": message}],
        "max_tokens": 400,
        "reasoning": {"effort": "minimal", "exclude": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "reminder", "strict": True, "schema": schema},
        },
    }
    request = urllib.request.Request(
        OPENROUTER_CHAT_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            result = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        error.read()
        raise ValueError(f"AI service error ({error.code}). Check your OpenRouter key, model, and credits.") from error
    except urllib.error.URLError as error:
        raise ValueError("AI service error (network). Please try again.") from error
    try:
        parsed = json.loads(response_text(result))
    except (json.JSONDecodeError, ValueError) as error:
        raise ValueError("The AI response was invalid. Please try again.") from error
    if parsed["kind"] == "list_reminders":
        raise BotAction("list_reminders")
    if parsed["kind"] == "delete_calendar":
        raise BotAction("delete_calendar", (parsed.get("title") or "").strip())
    if parsed["kind"] != "reminder" or not parsed["due_local"]:
        needs_clarification = parsed["kind"] == "clarify"
        raise BotReply(
            parsed["reply"] or "Please tell me the date and time for the reminder.",
            needs_clarification=needs_clarification,
        )
    try:
        due = datetime.fromisoformat(parsed["due_local"])
        if due.tzinfo is not None:
            due = due.astimezone(tz).replace(tzinfo=None)
        due = due.replace(tzinfo=tz)
    except (TypeError, ValueError) as error:
        raise ValueError("The AI could not determine a valid date and time. Please try again.") from error
    if due <= now:
        raise ValueError("That time has already passed. Please give me a future time.")
    end = None
    if parsed.get("end_local"):
        try:
            end = datetime.fromisoformat(parsed["end_local"])
            if end.tzinfo is not None:
                end = end.astimezone(tz).replace(tzinfo=None)
            end = end.replace(tzinfo=tz)
            if end <= due:
                end = None
        except (TypeError, ValueError):
            end = None
    return parsed["title"].strip(), due, parsed["recurrence"], bool(parsed.get("add_to_google_calendar")), end


def user_timezone(chat_id: int) -> ZoneInfo:
    with connect() as db:
        row = db.execute("SELECT timezone FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    name = row["timezone"] if row else "Asia/Kolkata"
    return ZoneInfo(name)


def set_timezone(chat_id: int, name: str) -> None:
    ZoneInfo(name)  # Validate before storing.
    with connect() as db:
        db.execute(
            "INSERT INTO users(chat_id, timezone) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET timezone = excluded.timezone",
            (chat_id, name),
        )


def pending_clarification(chat_id: int) -> Optional[str]:
    with connect() as db:
        row = db.execute(
            "SELECT original_message FROM pending_clarifications WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["original_message"] if row else None


def set_pending_clarification(chat_id: int, message: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO pending_clarifications(chat_id, original_message) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET original_message = excluded.original_message",
            (chat_id, message),
        )


def clear_pending_clarification(chat_id: int) -> None:
    with connect() as db:
        db.execute("DELETE FROM pending_clarifications WHERE chat_id = ?", (chat_id,))


def google_file_path(env_name: str, default: str) -> Path:
    configured = Path(os.getenv(env_name, default))
    return configured if configured.is_absolute() else ROOT / configured


def credentials_from_token_json(token_json: str):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as error:
        raise RuntimeError("Google Calendar packages are not installed. Run: python -m pip install -r requirements.txt") from error

    credentials = Credentials.from_authorized_user_info(json.loads(token_json), [GOOGLE_CALENDAR_SCOPE])
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials or not credentials.valid:
        raise GoogleCalendarNotConnected
    return credentials


def owner_google_token_file() -> Path:
    return google_file_path("GOOGLE_TOKEN_FILE", "data/google-token.json")


def google_calendar_credentials(_chat_id: int):
    """Always use the owner's saved Google token. Other Gmail accounts cannot be linked."""
    token_file = owner_google_token_file()
    if not token_file.exists():
        raise GoogleCalendarNotConnected
    credentials = credentials_from_token_json(token_file.read_text(encoding="utf-8"))
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def connected_google_email(_chat_id: int) -> Optional[str]:
    if owner_google_token_file().exists():
        return "Google Calendar"
    return None


def owner_calendar_locked_message() -> str:
    return "This bot uses the owner's Google Calendar only. Other Gmail accounts cannot be connected."


def google_calendar_id() -> str:
    return os.getenv("GOOGLE_CALENDAR_ID", "primary")


def google_calendar_service(chat_id: int):
    from googleapiclient.discovery import build

    credentials = google_calendar_credentials(chat_id)
    return build("calendar", "v3", credentials=credentials, cache_discovery=False)


def create_google_calendar_event(
    chat_id: int, title: str, start: datetime, tz: ZoneInfo, end: Optional[datetime] = None
) -> tuple[str, str]:
    """Create a timed event on this user's calendar; return (html link, event id)."""
    if end is None:
        end = start + timedelta(minutes=max(1, int(os.getenv("GOOGLE_EVENT_DURATION_MINUTES", "30"))))
    event = {
        "summary": title,
        "start": {"dateTime": start.isoformat(), "timeZone": tz.key},
        "end": {"dateTime": end.isoformat(), "timeZone": tz.key},
        "description": "Created from the Telegram reminder bot.",
    }
    created = google_calendar_service(chat_id).events().insert(calendarId=google_calendar_id(), body=event).execute()
    return created.get("htmlLink", ""), created.get("id", "")


def resolve_event_end(start: datetime, text: str, tz: ZoneInfo, end_from_ai: Optional[datetime] = None) -> datetime:
    """Use an explicit end time (4.10 to 4.15 pm) instead of the default 30 minutes."""
    if end_from_ai is not None:
        end = end_from_ai if end_from_ai.tzinfo else end_from_ai.replace(tzinfo=tz)
        if end > start:
            return end
    match = re.search(
        r"\b(?:from\s+)?(\d{1,2}(?:[:.]\d{1,2})?)\s*(a\.?m\.?|p\.?m\.?)?\s*(?:to|-|until|till)\s+(\d{1,2}(?:[:.]\d{1,2})?)\s*(a\.?m\.?|p\.?m\.?)?",
        text,
        re.I,
    )
    if match:
        start_clock, start_suffix, end_clock, end_suffix = match.groups()
        shared = end_suffix or start_suffix or ""
        try:
            end_hour, end_minute = parse_time(f"{end_clock} {shared}".strip())
            end = start.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
            if end.tzinfo is None:
                end = end.replace(tzinfo=tz)
            if end > start:
                return end
        except ValueError:
            pass
    return start + timedelta(minutes=max(1, int(os.getenv("GOOGLE_EVENT_DURATION_MINUTES", "30"))))


def delete_google_calendar_event(chat_id: int, event_id: str) -> bool:
    """Delete one event by id. Missing events count as already gone."""
    if not event_id:
        return False
    try:
        google_calendar_service(chat_id).events().delete(calendarId=google_calendar_id(), eventId=event_id).execute()
        return True
    except GoogleCalendarNotConnected:
        raise
    except Exception:
        return False


def search_google_calendar_events(chat_id: int, query: str) -> list[dict]:
    """Find upcoming events whose title matches a normal-person search phrase."""
    cleaned = re.sub(
        r"\b(?:delete|remove|cancel|hatao|hatado|from|my|google|calendar|calender|calander|meeting|meet|with|the|event|please)\b",
        " ",
        query,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    now = datetime.now(timezone.utc).isoformat()
    result = (
        google_calendar_service(chat_id)
        .events()
        .list(
            calendarId=google_calendar_id(),
            q=cleaned,
            timeMin=now,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def format_local(utc_iso: str, tz: ZoneInfo) -> str:
    instant = datetime.fromisoformat(utc_iso).astimezone(tz)
    return instant.strftime("%a, %d %b %Y at %I:%M:%S %p %Z").replace(" 0", " ")


def format_list_time(instant: datetime, tz: ZoneInfo) -> str:
    local = instant.astimezone(tz)
    return local.strftime("%a, %d %b · %I:%M %p").replace(" 0", " ")


def html_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def list_upcoming_google_events(chat_id: int) -> list[dict]:
    now = datetime.now(timezone.utc)
    result = (
        google_calendar_service(chat_id)
        .events()
        .list(
            calendarId=google_calendar_id(),
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=21)).isoformat(),
            maxResults=15,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )
    return result.get("items", [])


def parse_time(value: str) -> tuple[int, int]:
    value = value.strip().lower()
    # People commonly write 2.45 pm instead of 2:45 pm. Preserve that
    # separator, while still accepting a.m./p.m.
    value = re.sub(r"(?<=\d)\.(?=\d)", ":", value).replace(".", "")
    if value == "noon":
        return 12, 0
    if value == "midnight":
        return 0, 0
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", value)
    if not match:
        raise ValueError("I couldn't understand that time")
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    suffix = match.group(3)
    if minute > 59 or hour > 23 or hour == 0 and suffix:
        raise ValueError("That time isn't valid")
    if suffix == "pm" and hour != 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not suffix and hour > 23:
        raise ValueError("That time isn't valid")
    return hour, minute


def time_needs_am_pm(value: str) -> bool:
    """Return true when a 1-12 clock value has no AM/PM marker."""
    normalized = value.strip().lower().replace(".", "")
    if normalized in {"noon", "midnight"}:
        return False
    # "7pm" has no word boundary between the digit and pm, so \bpm\b misses it.
    if re.search(r"(?:am|pm)\s*$", normalized) or re.search(r"\b(?:am|pm)\b", normalized):
        return False
    match = re.match(r"(\d{1,2})", normalized)
    return bool(match and 1 <= int(match.group(1)) <= 12)


def merge_clarification(pending: Optional[str], clarification: str) -> str:
    """Attach a follow-up like 'pm' or '7pm' onto the original reminder text."""
    extra = clarification.strip()
    if not pending:
        return extra
    match = re.fullmatch(
        r"(?:at\s+)?(?:(\d{1,2}(?:[:.]\d{1,2})?)\s*)?(a\.?m\.?|p\.?m\.?)",
        extra,
        flags=re.I,
    )
    if not match:
        return f"{pending} {extra}".strip()
    suffix = re.sub(r"\.", "", match.group(2).lower())

    def inject(token_match: re.Match[str]) -> str:
        token = token_match.group(0)
        if re.search(r"a\.?m\.?|p\.?m\.?", token, re.I):
            return token
        return f"{token} {suffix}"

    updated, count = re.subn(
        r"\b\d{1,2}(?:[:.]\d{1,2})?(?:\s*(?:a\.?m\.?|p\.?m\.?))?",
        inject,
        pending,
        count=1,
        flags=re.I,
    )
    if count:
        return updated
    clock = match.group(1)
    return f"{pending} {clock + ' ' if clock else ''}{suffix}".strip()


def next_weekday(now: datetime, weekday: int, hour: int, minute: int) -> datetime:
    days = (weekday - now.weekday()) % 7
    candidate = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate + timedelta(days=7) if candidate <= now else candidate


def parse_reminder(message: str, tz: ZoneInfo) -> tuple[str, datetime, Optional[str]]:
    """Parse common, demo-friendly English reminder forms without external services."""
    original = message.strip()
    text = re.sub(
        r"^(?:please\s+|pls\s+|plz\s+)?/?rem(?:i?nd|inder)(?:\s+me)?\s+(?:to\s+)?",
        "",
        original,
        flags=re.I,
    ).strip()
    if not text:
        raise ValueError("Tell me what you want to be reminded about.")
    now = datetime.now(tz)

    every = re.search(r"\bevery\s+(day|daily|weekday|weekdays|week|weekly|month|monthly)\b", text, re.I)
    weekday_match = re.search(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, re.I)
    at = re.search(r"\b(?:at\s+)?(noon|midnight|\d{1,2}(?:[:.]\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\b", text, re.I)
    if every or weekday_match:
        if not at:
            raise BotReply("What time should I remind you? Please include AM or PM.", True)
        if time_needs_am_pm(at.group(1)):
            raise BotReply(f"Should that be {at.group(1).strip()} AM or PM?", True)
        hour, minute = parse_time(at.group(1))
        recurrence = "daily"
        if weekday_match:
            names = list(calendar.day_name)
            weekday = next(i for i, name in enumerate(names) if name.lower() == weekday_match.group(1).lower())
            due = next_weekday(now, weekday, hour, minute)
            recurrence = f"weekly:{weekday}"
        elif every.group(1).lower() in {"weekday", "weekdays"}:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            while due <= now or due.weekday() > 4:
                due += timedelta(days=1)
            recurrence = "weekdays"
        elif every.group(1).lower() in {"week", "weekly"}:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=7)
            recurrence = f"weekly:{due.weekday()}"
        elif every.group(1).lower() in {"month", "monthly"}:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=31)
            recurrence = "monthly"
        else:
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        task = text[: every.start()].strip() or "this"
        task = re.sub(r"\bat\s+$", "", task, flags=re.I).strip()
        return task, due, recurrence

    relative = re.search(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|days?)\b", text, re.I)
    if relative:
        count, unit = int(relative.group(1)), relative.group(2).lower()
        delta = timedelta(minutes=count) if unit.startswith(("minute", "min")) else timedelta(hours=count) if unit.startswith("hour") else timedelta(days=count)
        task = text[: relative.start()].strip() or "this"
        return task, now + delta, None

    day_match = re.search(r"\b(day\s+after\s+tomorrow|today|tomorrow|kal|parso)\b", text, re.I)
    if day_match:
        at = re.search(r"\b(?:at\s+)?(noon|midnight|\d{1,2}(?:[:.]\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\b", text, re.I)
        if not at:
            raise BotReply("Kitne baje remind karun? AM ya PM bhi bata dena.", True)
        if time_needs_am_pm(at.group(1)):
            raise BotReply(f"{at.group(1).strip()} AM ya PM?", True)
        hour, minute = parse_time(at.group(1))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        relative_day = re.sub(r"\s+", " ", day_match.group(1).lower())
        days_ahead = 2 if relative_day in {"day after tomorrow", "parso"} else 1 if relative_day in {"tomorrow", "kal"} else 0
        due += timedelta(days=days_ahead)
        if days_ahead == 0 and due <= now:
            raise BotReply("That time has already passed today. What future time should I use?", True)
        task = reminder_subject(text[: day_match.start()], text[day_match.end():])
        return task, due, None

    # If only a clock time is supplied, choose its next occurrence: today when
    # still upcoming, otherwise tomorrow. This handles natural input such as
    # "remind me drink water at 2.45 pm" without requiring the word "today".
    clock = re.search(
        r"\b(?:at\s+)?(noon|midnight|\d{1,2}(?:[:.]\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*$",
        text,
        re.I,
    )
    if clock:
        if time_needs_am_pm(clock.group(1)):
            raise BotReply(f"{clock.group(1).strip()} AM ya PM?", True)
        hour, minute = parse_time(clock.group(1))
        due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due <= now:
            due += timedelta(days=1)
        task = text[: clock.start()].strip()
        task = re.sub(r"\bat\s*$", "", task, flags=re.I).strip() or "this"
        return task, due, None

    raise ValueError("Try: ‘remind me to call Mom tomorrow at 6pm’ or ‘drink water in 20 minutes’.")


def add_reminder(chat_id: int, task: str, due: datetime, recurrence: Optional[str], google_event_id: Optional[str] = None) -> int:
    with connect() as db:
        result = db.execute(
            "INSERT INTO reminders(chat_id, text, due_at_utc, recurrence, created_at_utc, google_event_id) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, task, due.astimezone(timezone.utc).isoformat(), recurrence, datetime.now(timezone.utc).isoformat(), google_event_id),
        )
        reminder_id = int(result.lastrowid)
    SCHEDULE_CHANGED.set()
    return reminder_id


def list_reminders(chat_id: int) -> list[sqlite3.Row]:
    with connect() as db:
        return db.execute(
            "SELECT * FROM reminders WHERE chat_id = ? AND active = 1 ORDER BY due_at_utc LIMIT 50", (chat_id,)
        ).fetchall()


def is_calendar_list_request(text: str) -> bool:
    """Show Google Calendar when the user asks what is on their calendar."""
    if is_calendar_delete_request(text):
        return False
    normalized = normalize_calendar_words(text)
    tokens = set(normalized.split())
    calendar_word = "calendar" in tokens or "gcal" in tokens
    list_word = any(
        token in {"list", "show", "what", "whats", "which", "kya", "dikhao", "batao", "things", "events", "meetings", "pending", "upcoming"}
        or token.startswith(("what", "dikh", "bata"))
        for token in tokens
    )
    return calendar_word and list_word


def agenda_mode(text: str) -> str:
    calendar_ask = is_calendar_list_request(text)
    reminder_ask = is_list_request(text)
    if calendar_ask and not reminder_ask:
        return "calendar"
    if reminder_ask and not calendar_ask:
        return "reminders"
    return "both"


def is_list_request(text: str) -> bool:
    """Recognize common natural English and Hinglish reminder-list questions."""
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    if re.match(r"^(?:please\s+|pls\s+|plz\s+)?remind\s+me\b", normalized):
        return False
    tokens = normalized.split()
    reminder_words = ("reminder", "reminders", "remind", "task", "tasks", "yaad")
    list_words = ("list", "show", "what", "which", "pending", "upcoming", "dikhao", "batao", "kya")
    has_reminder_word = any(token in reminder_words or token.startswith("remind") for token in tokens)
    return has_reminder_word and any(word in tokens for word in list_words)


def is_abandon_request(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\s]", " ", text.lower()).strip()
    phrases = ("dont do anything", "do not do anything", "leave it", "never mind", "nevermind", "rehne do", "cancel it")
    return any(phrase in normalized for phrase in phrases)


def normalize_calendar_words(text: str) -> str:
    normalized = re.sub(r"[^\w\s]", " ", text.lower(), flags=re.UNICODE)
    hindi_map = {
        "कैलेंडर": "calendar",
        "कैलेंडर": "calendar",
        "कैलेंडर": "calendar",
        "मीटिंग": "meeting",
        "मीट": "meet",
        "मिलना": "meet",
        "मुलाकात": "meet",
        "शेड्यूल": "schedule",
        "गूगल": "google",
        "करो": "karo",
        "करदो": "kardo",
    }
    for hindi, english in hindi_map.items():
        normalized = normalized.replace(hindi, f" {english} ")
    normalized = re.sub(r"\b(?:gmeet|googlemeet|google\s+meets?)\b", "google meet", normalized)
    normalized = re.sub(r"\bcal[a-z]*d[a-z]*rs?\b", "calendar", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def reminder_subject(prefix: str, leftover: str = "") -> str:
    """Prefer the real task when it comes after the date, e.g. 'today that I had to meet harsh'."""
    after = leftover.strip()
    after = re.sub(r"^(?:that\s+)?(?:i\s+)?(?:had|have|need)\s+to\s+", "", after, flags=re.I)
    after = re.sub(r"^that\s+", "", after, flags=re.I).strip()
    before = prefix.strip()
    before = re.sub(
        r"\bat\s+(?:noon|midnight|\d{1,2}(?:[:.]\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*$",
        "",
        before,
        flags=re.I,
    ).strip()
    return after or before or "this"


def is_calendar_delete_request(text: str) -> bool:
    """Detect a normal-person request to remove something from Google Calendar."""
    normalized = normalize_calendar_words(text)
    tokens = normalized.split()
    delete_word = any(token in {"delete", "remove", "hatao", "hatado", "hata"} for token in tokens) or "hata do" in normalized
    cancel_calendar = "cancel" in tokens and any(token in {"calendar", "meeting", "meet", "event", "appointment"} for token in tokens)
    target = any(token in {"calendar", "meeting", "meet", "event", "appointment", "google"} for token in tokens)
    return (delete_word and target) or cancel_calendar


def match_reminders(chat_id: int, query: str) -> list[sqlite3.Row]:
    needles = [
        token
        for token in re.sub(
            r"\b(?:delete|remove|cancel|hatao|hatado|from|my|google|calendar|calender|calander|meeting|meet|with|the|event|please)\b",
            " ",
            query.lower(),
        ).split()
        if token
    ]
    rows = list_reminders(chat_id)
    if not needles:
        return [row for row in rows if row["google_event_id"]]
    exact = [row for row in rows if all(token in row["text"].lower() for token in needles)]
    return exact or [row for row in rows if any(token in row["text"].lower() for token in needles)]


def delete_calendar_items(chat_id: int, query: str) -> str:
    """Cancel matching local reminders and delete the linked Google Calendar events."""
    rows = match_reminders(chat_id, query)
    deleted_titles: list[str] = []
    for row in rows:
        event_id = row["google_event_id"]
        if event_id:
            try:
                delete_google_calendar_event(chat_id, event_id)
            except GoogleCalendarNotConnected:
                return "Google Calendar is not available on this computer."
        cancel_reminder(chat_id, row["id"])
        deleted_titles.append(row["text"])
    extra_titles: list[str] = []
    try:
        for event in search_google_calendar_events(chat_id, query):
            event_id = event.get("id")
            title = event.get("summary") or "event"
            if not event_id or title in deleted_titles:
                continue
            if delete_google_calendar_event(chat_id, event_id):
                extra_titles.append(title)
    except GoogleCalendarNotConnected:
        if not deleted_titles:
            return "Google Calendar is not available on this computer."
    names = deleted_titles + extra_titles
    if not names:
        return "I couldn't find that meeting on Google Calendar. Try /list, then /cancel <number>."
    unique = list(dict.fromkeys(names))
    if len(unique) == 1:
        return f"🗑️ Deleted from Google Calendar: {unique[0]}"
    return "🗑️ Deleted from Google Calendar:\n" + "\n".join(f"• {name}" for name in unique)


def is_connect_request(text: str) -> bool:
    normalized = normalize_calendar_words(text)
    tokens = set(normalized.split())
    connect = bool(tokens & {"connect", "link", "attach", "jodo", "jod", "login", "signin"})
    target = bool(tokens & {"google", "calendar", "gmail", "email", "account"})
    return connect and target


def is_disconnect_request(text: str) -> bool:
    normalized = normalize_calendar_words(text)
    tokens = set(normalized.split())
    return bool(tokens & {"disconnect", "unlink", "logout"}) and bool(tokens & {"google", "calendar", "gmail"})


def is_calendar_request(text: str) -> bool:
    """Detect English, Hindi, and Hinglish requests to create a calendar event."""
    if is_calendar_list_request(text) or is_calendar_delete_request(text):
        return False
    normalized = normalize_calendar_words(text)
    tokens = normalized.split()
    event_word = any(
        token in {
            "meeting",
            "meet",
            "gmeet",
            "calendar",
            "appointment",
            "event",
            "milna",
            "milne",
            "mulaqat",
            "mulakat",
        }
        for token in tokens
    )
    action_word = any(
        token in {
            "add",
            "book",
            "create",
            "schedule",
            "set",
            "put",
            "kro",
            "karo",
            "kar",
            "kardo",
            "karde",
            "rakh",
            "rakho",
            "rakhdo",
            "rakhde",
            "daal",
            "daalo",
            "daaldo",
            "dal",
            "dalo",
            "daldo",
            "laga",
            "lagao",
            "lagado",
            "bana",
            "banao",
            "banado",
            "fix",
            "fixed",
        }
        or "schedul" in token
        or token.startswith(("sched", "shedule", "schd"))
        for token in tokens
    )
    google_event = "google" in tokens and any(token in {"meet", "meeting", "calendar"} for token in tokens)
    calendar_place = any(
        phrase in normalized
        for phrase in ("calendar me", "calendar mein", "calendar pe", "calendar par", "calendar mai")
    )
    return (event_word and action_word) or google_event or (calendar_place and action_word)


def clean_calendar_title(title: str) -> str:
    cleaned = re.sub(r"\bcal[ae]nd[ae]r\b", "calendar", title, flags=re.I)
    cleaned = re.sub(
        r"^(?:please\s+|pls\s+|plz\s+)?(?:add|book|create|schedule|set(?:\s+up)?|shedule|schedual|schdeulde)\s+(?:an?\s+|my\s+)?",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\s+(?:ko\s+)?(?:schedule|schedul|shedule)?\s*(?:kro|karo|kardo|kar\s+do|kar\s+dena|rakhdo|rakh\s+do|daal\s+do|laga\s+do)\b",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+(?:on|in|to|un|me|mein|mai|pe|par)\s+(?:my\s+)?google\s+calendars?\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:my\s+)?google\s+calendars?\s+(?:on|in|me|mein|mai|pe|par)?\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+(?:on|in|me|mein|mai|pe|par)\s+(?:my\s+)?calendars?\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:my\s+)?calendars?\s+(?:on|in|me|mein|mai|pe|par)\b", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\b(?:my\s+)?google\s+meets?\b", "Google Meet", cleaned, flags=re.I)
    cleaned = re.sub(
        r"\s+at\s+(?:noon|midnight|\d{1,2}(?:[:.]\d{1,2})?\s*(?:a\.?m\.?|p\.?m\.?)?)\s*$",
        "",
        cleaned,
        flags=re.I,
    )
    if re.search(r"\bgoogle meet\b", cleaned, flags=re.I) and not re.search(r"\bwith\b", cleaned, flags=re.I):
        return "Google Meet"
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.lower() in {"calendar", "google", "this", "meeting", "meet", "event"}:
        return "Google Meet" if re.search(r"meet", title, flags=re.I) else "Meeting"
    meet = re.search(r"\b(?:meeting\s+with|meet)\s+(.+)$", cleaned, flags=re.I)
    if meet:
        name = meet.group(1).strip(" .")
        if name:
            return name if re.match(r"^meeting\b", name, flags=re.I) else f"Meeting with {name}"
    return cleaned.strip() or title


def send_reminder_list(api: Telegram, chat_id: int, user_text: str = "") -> None:
    send_agenda(api, chat_id, user_text, agenda_mode(user_text) if user_text != "/list" else "both")


def send_agenda(api: Telegram, chat_id: int, user_text: str = "", mode: str = "both") -> None:
    tz = user_timezone(chat_id)
    hinglish = any(word in user_text.lower().split() for word in ("meri", "mera", "dikhao", "batao", "kya", "yaad"))
    blocks: list[str] = []
    if mode in {"both", "calendar"}:
        try:
            events = list_upcoming_google_events(chat_id)
            if events:
                email = html_escape(connected_google_email(chat_id) or "")
                heading = f"📅 <b>Google Calendar</b>" + (f"\n<i>{email}</i>" if email else "")
                lines = [heading]
                for event in events:
                    title = html_escape(event.get("summary") or "Untitled")
                    start = event.get("start") or {}
                    start_raw = start.get("dateTime") or start.get("date")
                    when = ""
                    if start.get("date") and not start.get("dateTime"):
                        when = datetime.fromisoformat(start["date"]).strftime("%a, %d %b · All day")
                    elif start_raw:
                        when = format_list_time(datetime.fromisoformat(start_raw.replace("Z", "+00:00")), tz)
                    lines.append(f"• <b>{title}</b>\n  <i>{html_escape(when)}</i>" if when else f"• <b>{title}</b>")
                blocks.append("\n".join(lines))
            else:
                blocks.append("📅 <b>Google Calendar</b>\n<i>No upcoming events.</i>" if not hinglish else "📅 <b>Google Calendar</b>\n<i>Koi upcoming event nahi hai.</i>")
        except GoogleCalendarNotConnected:
            blocks.append("📅 <b>Google Calendar</b>\n<i>Owner calendar is not available on this computer.</i>")
        except Exception as error:
            blocks.append(f"📅 <b>Google Calendar</b>\n<i>Couldn’t load events: {html_escape(str(error))}</i>")
    if mode in {"both", "reminders"}:
        reminders = [row for row in list_reminders(chat_id) if not row["google_event_id"]]
        if reminders:
            lines = ["🔔 <b>Reminders</b>"]
            for row in reminders:
                repeat = " 🔁" if row["recurrence"] else ""
                when = format_list_time(datetime.fromisoformat(row["due_at_utc"]), tz)
                lines.append(f"• <code>#{row['id']}</code> <b>{html_escape(row['text'])}</b>{repeat}\n  <i>{html_escape(when)}</i>")
            blocks.append("\n".join(lines))
        else:
            blocks.append("🔔 <b>Reminders</b>\n<i>Abhi koi active reminder nahi hai.</i>" if hinglish else "🔔 <b>Reminders</b>\n<i>No active reminders.</i>")
    api.send(chat_id, "\n\n".join(blocks), parse_mode="HTML")


def cancel_reminder(chat_id: int, reminder_id: int) -> bool:
    with connect() as db:
        row = db.execute(
            "SELECT google_event_id FROM reminders WHERE id = ? AND chat_id = ? AND active = 1",
            (reminder_id, chat_id),
        ).fetchone()
        result = db.execute("UPDATE reminders SET active = 0 WHERE id = ? AND chat_id = ? AND active = 1", (reminder_id, chat_id))
        cancelled = result.rowcount > 0
    if cancelled:
        SCHEDULE_CHANGED.set()
        event_id = row["google_event_id"] if row else None
        if event_id:
            try:
                delete_google_calendar_event(chat_id, event_id)
            except GoogleCalendarNotConnected:
                pass
    return cancelled


def next_due(current: datetime, recurrence: str, tz: ZoneInfo) -> datetime:
    local = current.astimezone(tz)
    if recurrence == "daily":
        return local + timedelta(days=1)
    if recurrence == "weekdays":
        candidate = local + timedelta(days=1)
        while candidate.weekday() > 4:
            candidate += timedelta(days=1)
        return candidate
    if recurrence.startswith("weekly:"):
        return local + timedelta(days=7)
    if recurrence == "monthly":
        month = local.month % 12 + 1
        year = local.year + (local.month == 12)
        day = min(local.day, calendar.monthrange(year, month)[1])
        return local.replace(year=year, month=month, day=day)
    return local + timedelta(days=1)


def deliver_due_reminders(api: Telegram) -> None:
    now_utc = datetime.now(timezone.utc)
    with connect() as db:
        due = db.execute("SELECT * FROM reminders WHERE active = 1 AND due_at_utc <= ?", (now_utc.isoformat(),)).fetchall()
    for reminder in due:
        try:
            api.send(reminder["chat_id"], f"🔔 Reminder: {reminder['text']}")
        except RuntimeError as error:
            print(f"Could not send reminder {reminder['id']}: {error}", file=sys.stderr)
            continue
        with connect() as db:
            if reminder["recurrence"]:
                due_local = next_due(datetime.fromisoformat(reminder["due_at_utc"]), reminder["recurrence"], user_timezone(reminder["chat_id"]))
                db.execute("UPDATE reminders SET due_at_utc = ? WHERE id = ?", (due_local.astimezone(timezone.utc).isoformat(), reminder["id"]))
            else:
                db.execute("UPDATE reminders SET active = 0 WHERE id = ?", (reminder["id"],))


def next_active_due_utc() -> Optional[datetime]:
    """Return the next scheduled instant, always as an aware UTC datetime."""
    with connect() as db:
        row = db.execute(
            "SELECT due_at_utc FROM reminders WHERE active = 1 ORDER BY due_at_utc LIMIT 1"
        ).fetchone()
    if not row:
        return None
    value = datetime.fromisoformat(row["due_at_utc"])
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def reminder_scheduler(api: Telegram) -> None:
    """Sleep until the exact next due instant, independently of Telegram polling."""
    while True:
        try:
            next_due_at = next_active_due_utc()
            if next_due_at is None:
                SCHEDULE_CHANGED.wait(60)
            else:
                seconds_until_due = (next_due_at - datetime.now(timezone.utc)).total_seconds()
                if seconds_until_due > 0:
                    SCHEDULE_CHANGED.wait(min(seconds_until_due, 60))
                else:
                    deliver_due_reminders(api)
            SCHEDULE_CHANGED.clear()
        except Exception as error:
            print(f"Scheduler issue: {error}. Retrying in 1 second.", file=sys.stderr, flush=True)
            SCHEDULE_CHANGED.wait(1)
            SCHEDULE_CHANGED.clear()


def handle_message(api: Telegram, message: dict) -> None:
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    if not text:
        api.send(chat_id, "Send a text reminder, for example: remind me to call Mom tomorrow at 6pm.")
        return
    command = text.split()[0].split("@")[0].lower()
    if command == "/start":
        api.send(
            chat_id,
            "👋 I remember things for you.\n\n"
            "Meetings go on the owner's Google Calendar.\n\n"
            "Try: ‘remind me to call Mom tomorrow at 6pm’\n"
            "Or: ‘schedule a meet with Priyanshu at 4pm today’\n\n"
            "Commands: /list, /cancel <number>, /timezone, /help",
        )
        return
    if command == "/help":
        api.send(
            chat_id,
            "Examples:\n"
            "• schedule a meeting with Harsh at 6pm today\n"
            "• remind me to submit the form tomorrow at 5pm\n"
            "• what’s in my google calendar\n"
            "• delete meeting with Harsh from calendar\n\n"
            "/list — reminders + the owner's Google Calendar\n"
            "/cancel 3 — cancel reminder #3\n"
            "/timezone Asia/Kolkata",
        )
        return
    if command in {"/connect", "/disconnect"}:
        api.send(chat_id, owner_calendar_locked_message())
        return
    if command == "/timezone":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            api.send(chat_id, f"Your timezone is {user_timezone(chat_id).key}. Set one with /timezone Asia/Kolkata")
            return
        try:
            set_timezone(chat_id, parts[1].strip())
            api.send(chat_id, f"✅ Timezone set to {parts[1].strip()}")
        except ZoneInfoNotFoundError:
            api.send(chat_id, "I couldn't find that timezone. Use an IANA name, e.g. Asia/Kolkata or America/New_York.")
        return
    if command == "/list":
        clear_pending_clarification(chat_id)
        send_agenda(api, chat_id, text, "both")
        return
    if command == "/cancel":
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].isdigit():
            api.send(chat_id, "Use /cancel <number>. Find numbers with /list.")
            return
        api.send(chat_id, "✅ Reminder cancelled." if cancel_reminder(chat_id, int(parts[1])) else "I couldn't find that active reminder.")
        return
    if is_connect_request(text) or is_disconnect_request(text):
        api.send(chat_id, owner_calendar_locked_message())
        return
    if pending_clarification(chat_id) and is_abandon_request(text):
        clear_pending_clarification(chat_id)
        api.send(chat_id, "Okay.")
        return
    if is_calendar_list_request(text):
        clear_pending_clarification(chat_id)
        send_agenda(api, chat_id, text, "calendar")
        return
    if is_list_request(text):
        clear_pending_clarification(chat_id)
        send_agenda(api, chat_id, text, "reminders")
        return
    if is_calendar_delete_request(text):
        clear_pending_clarification(chat_id)
        api.send(chat_id, delete_calendar_items(chat_id, text))
        return
    try:
        tz = user_timezone(chat_id)
        pending_text = pending_clarification(chat_id)
        combined_text = merge_clarification(pending_text, text)
        calendar_requested = is_calendar_request(combined_text)
        ai_text = (
            f"Original reminder request: {pending_text}\nUser's clarification: {text}"
            if pending_text
            else text
        )
        # Prefer the LLM so messy human phrasing, typos, and calendar intent are understood.
        # The local parser is only a fallback when no OpenRouter key is set, or the LLM fails.
        if os.getenv("OPENROUTER_API_KEY"):
            api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            end = None
            try:
                task, due, recurrence, calendar_from_ai, end = parse_reminder_with_ai(ai_text, tz)
                calendar_requested = calendar_requested or calendar_from_ai
            except ValueError:
                task, due, recurrence = parse_reminder(combined_text, tz)
        else:
            end = None
            task, due, recurrence = parse_reminder(combined_text, tz)
        if calendar_requested:
            task = clean_calendar_title(task)
            if task.lower() in {"in my google calendar", "on my google calendar", "this"}:
                task = clean_calendar_title(combined_text)
        calendar_link = ""
        google_event_id = None
        end = resolve_event_end(due, combined_text, tz, end)
        calendar_email = connected_google_email(chat_id)
        if calendar_requested:
            try:
                calendar_link, google_event_id = create_google_calendar_event(chat_id, task, due, tz, end)
            except GoogleCalendarNotConnected:
                raise BotReply("Google Calendar is not available on this computer.")
            except Exception as error:
                raise BotReply(f"I couldn't add that meeting to Google Calendar: {error}")
        add_reminder(chat_id, task, due, recurrence, google_event_id)
        clear_pending_clarification(chat_id)
        confirmation = f"✅ I’ll remind you to {task} on {format_local(due.astimezone(timezone.utc).isoformat(), tz)}."
        if google_event_id:
            who = calendar_email or "Google Calendar"
            confirmation += f"\n📅 Also added to {who} ({format_list_time(due, tz)} – {format_list_time(end, tz)})."
            if calendar_link:
                confirmation += f"\n{calendar_link}"
        elif calendar_requested:
            confirmation = (
                f"✅ {task} added to Google Calendar for {format_list_time(due, tz)} – {format_list_time(end, tz)}."
            )
            if calendar_link:
                confirmation += f"\n{calendar_link}"
        api.send(chat_id, confirmation)
    except BotReply as reply:
        if reply.needs_clarification:
            set_pending_clarification(chat_id, combined_text)
        else:
            clear_pending_clarification(chat_id)
        api.send(chat_id, str(reply))
    except BotAction as action:
        if action.action == "list_reminders":
            send_agenda(api, chat_id, text, agenda_mode(text))
        elif action.action == "delete_calendar":
            clear_pending_clarification(chat_id)
            api.send(chat_id, delete_calendar_items(chat_id, action.query or text))
        elif action.action in {"connect_calendar", "disconnect_calendar"}:
            api.send(chat_id, owner_calendar_locked_message())
    except ValueError as error:
        api.send(chat_id, f"I couldn't set that reminder: {error}")


def handle_callback(api: Telegram, callback: dict) -> None:
    data = callback.get("data", "")
    chat_id = callback["message"]["chat"]["id"]
    try:
        action, identifier = data.split(":", 1)
        reminder_id = int(identifier)
    except ValueError:
        return
    if action == "done":
        cancel_reminder(chat_id, reminder_id)
        api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Marked as done"})
    elif action == "snooze":
        with connect() as db:
            db.execute("UPDATE reminders SET due_at_utc = ? WHERE id = ? AND chat_id = ?", ((datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(), reminder_id, chat_id))
        SCHEDULE_CHANGED.set()
        api.call("answerCallbackQuery", {"callback_query_id": callback["id"], "text": "Snoozed for 10 minutes"})


def start_health_server() -> None:
    """Railway/Render web services must listen on $PORT or the deploy is killed."""
    raw_port = os.getenv("PORT")
    if not raw_port:
        return
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args) -> None:
            return

    server = HTTPServer(("0.0.0.0", int(raw_port)), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True, name="health-server").start()
    print(f"Health server listening on {raw_port}", flush=True)


def main() -> None:
    load_env_file()
    start_health_server()
    if "--setup-google-calendar" in sys.argv:
        print("This bot uses data/google-token.json only. Other Gmail accounts cannot be connected in chat.")
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    print(
        f"Startup: PORT={os.getenv('PORT') or 'none'} token={'set' if token else 'MISSING'}",
        flush=True,
    )
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN. Add it in Railway Variables and redeploy.")
    initialize_database()
    api = Telegram(token)
    threading.Thread(target=reminder_scheduler, args=(api,), daemon=True, name="reminder-scheduler").start()
    print("Bot is running. Open it in Telegram and send /start. Press Ctrl+C to stop.", flush=True)
    offset: Optional[int] = None
    while True:
        try:
            payload = {"timeout": POLL_TIMEOUT_SECONDS, "allowed_updates": json.dumps(["message", "callback_query"])}
            if offset is not None:
                payload["offset"] = offset
            updates = api.call("getUpdates", payload)
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    handle_message(api, update["message"])
                elif "callback_query" in update:
                    handle_callback(api, update["callback_query"])
        except (RuntimeError, urllib.error.URLError, ConnectionError, TimeoutError, OSError) as error:
            print(f"Connection issue: {error}. Retrying in 1 second.", file=sys.stderr)
            time.sleep(1)


if __name__ == "__main__":
    main()
