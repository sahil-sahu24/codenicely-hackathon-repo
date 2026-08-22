# Telegram Reminder Bot

A Telegram-only reminder assistant for a fast hackathon demo. It runs locally with no landing page or hosting account required.

## Features

- AI-powered natural-language reminder understanding, including casual phrasing
- Plain-English one-time reminders: `remind me to call Mom tomorrow at 6pm`
- Relative reminders: `drink water in 20 minutes`
- Repeating reminders: `standup every weekday at 9:30am`, `pay rent every month at 10am`, `gym every Monday at 7am`
- `/list`, `/cancel <number>`, `/timezone <IANA timezone>`, `/help`
- Done and 10-minute-snooze buttons on reminder notifications
- Local SQLite persistence in `data/reminders.db`

## Run it

1. In Telegram, open **@BotFather**, run `/newbot`, and copy the token it gives you.
2. Create an OpenRouter API key (or use your existing OpenRouter key).
2. Copy `.env.example` to a new file named `.env`.
3. Put the token in `.env`:

   ```env
   TELEGRAM_BOT_TOKEN=your-token-here
   OPENROUTER_API_KEY=your-openrouter-key-here
   ```

4. Install the one Windows timezone dependency:

   ```powershell
   python -m pip install -r requirements.txt
   ```

5. From this folder run:

   ```powershell
   python bot.py
   ```

5. Open your new bot in Telegram, press **Start**, and send it a reminder.

Keep the terminal open while demonstrating; the bot must stay running to deliver reminders. Stop it with `Ctrl+C`.

## Fast demo script

1. Send `/start`.
2. Send `remind me to take a demo photo in 2 minutes`.
3. Send `/list` to show it was saved.
4. Wait for the Telegram notification, then press **Snooze 10 min** or **Done**.

## Current MVP limits

The bot asks the LLM for JSON, then validates the title, date/time, and recurrence rule locally before saving anything. The original lightweight parser remains as a fallback if no OpenRouter key is configured.

## Google Calendar

The bot uses only the owner login from `GOOGLE_TOKEN_JSON` or `data/google-token.json`. There is no in-chat `/connect` for other Gmail accounts. On Railway, set the `GOOGLE_TOKEN_JSON` variable to that JSON.

Meetings such as `schedule a meeting with Rahul tomorrow at 4 PM` go on that calendar. Ordinary reminder messages do not create calendar events.
