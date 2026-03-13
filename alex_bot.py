"""
Alex — Your Personal Telegram Assistant (Powered by Google Gemini FREE)
========================================================================
Features:
  ✅ Chat about anything
  ✅ Remembers conversation history
  ✅ Set reminders: "Remind me in 2 hours to call John"
  ✅ Recurring reminders: "Every Monday 8am remind me about standup"
  ✅ Flight alerts: "Track flight EK203 remind me 6hrs before landing"

Setup:
  1. pip install python-telegram-bot apscheduler requests
  2. Set TELEGRAM_TOKEN and GEMINI_API_KEY environment variables
  3. python alex_bot.py
"""

import asyncio
import json
import logging
import os
import re
import requests
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from telegram import Bot, Update
from telegram.ext import (ApplicationBuilder, CommandHandler, ContextTypes,
                           MessageHandler, filters)

# ═══════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL   = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-4-scout:free")

BOT_NAME    = "Alex"
MAX_HISTORY = 30

PERSONALITY = f"""You are {BOT_NAME}, a smart and friendly personal assistant on Telegram.
You are concise, warm, helpful, and proactive.

== REMINDERS ==
When the user asks you to set a reminder, include this block ANYWHERE in your reply:

<reminder>
{{
  "type": "once",
  "datetime": "YYYY-MM-DD HH:MM",
  "message": "what to remind them about"
}}
</reminder>

For recurring reminders:
<reminder>
{{
  "type": "recurring",
  "cron": "0 8 * * 1",
  "message": "what to remind them about"
}}
</reminder>

cron format: minute hour day month weekday  (0=Sunday, 1=Monday ... 6=Saturday)
Examples:
  Every day 8am      ->  0 8 * * *
  Every Monday 9am   ->  0 9 * * 1
  Every weekday 7am  ->  0 7 * * 1-5

Always tell the user in plain text what reminder you just set.

Today's date/time: {datetime.now().strftime("%A %B %d %Y %H:%M")}
"""

# ═══════════════════════════════════════════════════════════
#  SETUP
# ═══════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s  %(levelname)s  %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

scheduler    = AsyncIOScheduler()
bot_instance: Bot | None = None

histories: dict[int, list] = {}
reminders: dict[str, dict] = {}

# ═══════════════════════════════════════════════════════════
#  GEMINI via direct HTTP (most reliable, no SDK issues)
# ═══════════════════════════════════════════════════════════

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

def ask_gemini(history: list, user_text: str) -> str:
    """Call OpenRouter API with retry on rate limit."""
    import time

    # Convert history to OpenAI format
    messages = [{"role": "system", "content": PERSONALITY}]
    for msg in history:
        role = "assistant" if msg["role"] == "model" else msg["role"]
        text = msg["parts"][0] if isinstance(msg["parts"][0], str) else msg["parts"][0].get("text", "")
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": user_text})

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": 1024
    }

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://telegram-bot.app",
        "X-Title": "Alex Telegram Bot"
    }

    for attempt in range(3):
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers=headers,
                json=payload,
                timeout=30
            )

            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                log.warning(f"Rate limited (429), retrying in {wait}s...")
                time.sleep(wait)
                continue

            if resp.status_code != 200:
                log.error(f"OpenRouter API error {resp.status_code}: {resp.text}")
                return "Sorry, I had trouble getting a response. Please try again in a moment."

            data  = resp.json()
            reply = data["choices"][0]["message"]["content"]

            history.append({"role": "user",  "parts": [user_text]})
            history.append({"role": "model", "parts": [reply]})
            while len(history) > MAX_HISTORY:
                history.pop(0)

            return reply

        except Exception as e:
            log.error(f"OpenRouter request failed: {e}")
            if attempt == 2:
                return "Sorry, I couldn't reach my AI backend. Please try again."
            time.sleep(3)

    return "I'm getting too many requests right now. Please wait a moment and try again."


# ═══════════════════════════════════════════════════════════
#  REMINDER HELPERS
# ═══════════════════════════════════════════════════════════

def get_history(chat_id: int) -> list:
    if chat_id not in histories:
        histories[chat_id] = []
    return histories[chat_id]


def extract_reminder(text: str) -> dict | None:
    match = re.search(r"<reminder>\s*(\{.*?\})\s*</reminder>", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning("Could not parse reminder JSON")
    return None


def strip_reminder_block(text: str) -> str:
    return re.sub(r"<reminder>.*?</reminder>", "", text, flags=re.DOTALL).strip()


async def fire_reminder(chat_id: int, message: str, job_id: str):
    if bot_instance:
        try:
            await bot_instance.send_message(
                chat_id=chat_id,
                text=f"🔔 *Reminder:* {message}",
                parse_mode="Markdown"
            )
        except Exception as e:
            log.error(f"Could not send reminder: {e}")
    reminders.pop(job_id, None)


def schedule(chat_id: int, reminder: dict) -> str:
    job_id = f"{chat_id}_{datetime.now().timestamp()}"

    if reminder["type"] == "once":
        try:
            run_at = datetime.strptime(reminder["datetime"], "%Y-%m-%d %H:%M")
        except ValueError:
            return "⚠️ I couldn't parse the reminder time. Please try again."
        scheduler.add_job(fire_reminder, trigger=DateTrigger(run_date=run_at),
                          args=[chat_id, reminder["message"], job_id], id=job_id)
        reminders[job_id] = {**reminder, "chat_id": chat_id}
        return f"⏰ Reminder set for *{run_at.strftime('%B %d at %H:%M')}*"

    elif reminder["type"] == "recurring":
        parts = reminder.get("cron", "").split()
        if len(parts) != 5:
            return "⚠️ Invalid recurring schedule. Please try again."
        scheduler.add_job(fire_reminder,
                          trigger=CronTrigger(minute=parts[0], hour=parts[1],
                                              day=parts[2], month=parts[3], day_of_week=parts[4]),
                          args=[chat_id, reminder["message"], job_id], id=job_id)
        reminders[job_id] = {**reminder, "chat_id": chat_id}
        return "🔁 Recurring reminder scheduled!"

    return "⚠️ Unknown reminder type."


# ═══════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    histories[chat_id] = []
    await update.message.reply_text(
        f"👋 Hey! I'm *{BOT_NAME}*, your personal assistant.\n\n"
        "I can:\n"
        "💬 Chat about anything\n"
        "⏰ Set reminders that ping you automatically\n"
        "✈️ Help track flights & alert you before they land\n"
        "🔁 Set recurring reminders (daily, weekly, etc.)\n\n"
        "*Try:*\n"
        "• _Remind me tomorrow at 9am to send the report_\n"
        "• _Remind me every Monday at 8am about standup_\n\n"
        "/reminders — see your active reminders\n"
        "/clear — reset our conversation",
        parse_mode="Markdown"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    histories[update.effective_chat.id] = []
    await update.message.reply_text("🗑️ Conversation cleared!")


async def cmd_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine    = {k: v for k, v in reminders.items() if v.get("chat_id") == chat_id}
    if not mine:
        await update.message.reply_text("📭 No active reminders.")
        return
    lines = ["📋 *Active reminders:*\n"]
    for i, r in enumerate(mine.values(), 1):
        if r["type"] == "once":
            lines.append(f"{i}. 🕐 {r['datetime']} — {r['message']}")
        else:
            lines.append(f"{i}. 🔁 {r.get('cron')} — {r['message']}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id   = update.effective_chat.id
    user_text = update.message.text

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    history = get_history(chat_id)

    try:
        reply        = await asyncio.to_thread(ask_gemini, history, user_text)
        reminder     = extract_reminder(reply)
        visible_text = strip_reminder_block(reply)

        if visible_text:
            await update.message.reply_text(visible_text)
        if reminder:
            confirmation = schedule(chat_id, reminder)
            await update.message.reply_text(confirmation, parse_mode="Markdown")

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        await update.message.reply_text("⚠️ Something went wrong. Please try again.")


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

def main():
    global bot_instance

    if not TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN is not set!")
        return
    if not OPENROUTER_API_KEY:
        log.error("OPENROUTER_API_KEY is not set!")
        return

    log.info(f"OPENROUTER_API_KEY loaded: {OPENROUTER_API_KEY[:8]}...")
    log.info(f"Model: {OPENROUTER_MODEL}")
    log.info(f"TELEGRAM_TOKEN loaded: {TELEGRAM_TOKEN[:8]}...")

    app          = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    bot_instance = app.bot

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("clear",     cmd_clear))
    app.add_handler(CommandHandler("reminders", cmd_reminders))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler.start()
    log.info(f"✅ {BOT_NAME} is running! Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
