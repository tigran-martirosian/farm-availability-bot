#!/usr/bin/env python3
# Interactive Telegram bot for farm shop availability.

import asyncio
import json
import logging
import os
import re
from datetime import time as dtime, datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# --- scraper (shop_notifier.py) ---
from shop_notifier import fetch, parse_catalog, build_message, SHOP_URL, SHOP_NAME
import stock_watch

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("availability-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHOP = os.getenv("SHOP_URL", SHOP_URL)
DATA_FILE = Path(os.getenv("SCHEDULES_FILE", "schedules.json"))
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "15"))

WELCOME_TEXT = (
    "👋 *Welcome to the Farm Availability Notifier!*\n\n"
    "Use the buttons below:\n"
    "• *▶️ Run once now* — sends today's stock report.\n"
    "• *⏰ Set daily time* — send a time like `08:30` (24-hour) and I'll DM the report every day.\n"
    "• `/watch <product>` — alert me when a matching product changes stock (also `/unwatch`, `/watches`).\n\n"
    "You can also type `/status` anytime to see your schedule."
)

# ---------- persistence ----------
def load_schedules() -> Dict[str, str]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("Failed to read schedules.json: %s", e)
            return {}
    return {}

def save_schedules(d: Dict[str, str]) -> None:
    DATA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")

# ---------- report ----------
def build_report() -> str:
    html = fetch(SHOP)
    in_stock, out_stock = parse_catalog(html, SHOP)
    return build_message(in_stock, out_stock, SHOP)

# ---------- time utils (server local tz) ----------
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

def parse_hhmm(text: str) -> Tuple[int, int] | None:
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, m2 = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= m2 <= 59:
        return h, m2
    return None

def next_run_dt(hour: int, minute: int) -> datetime:
    now = datetime.now().astimezone()
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run

def scheduled_time(hour: int, minute: int) -> dtime:
    """Daily job time carrying the server's local tzinfo (a naive time is treated as UTC)."""
    return dtime(hour=hour, minute=minute, tzinfo=datetime.now().astimezone().tzinfo)

def describe_next_run(hour: int, minute: int) -> str:
    return next_run_dt(hour, minute).strftime("%a %Y-%m-%d %H:%M %Z")

# ---------- UI ----------
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ Run once now", callback_data="run_now")],
    [InlineKeyboardButton("⏰ Set daily time", callback_data="set_time")],
])

ASK_TIME = (
    "Send a time in *24-hour* format, e.g. `08:30` or `21:05`.\n"
    "_I’ll send the report every day at that time (server local time)._"
)

AWAIT_FLAG = "await_time"  # context.user_data flag

# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    schedules = load_schedules()
    t = schedules.get(str(chat_id))

    if t:
        h, m = [int(x) for x in t.split(":")]
        status = f"Current schedule: *{t}* daily\nNext send: *{describe_next_run(h, m)}*"
        text = f"{WELCOME_TEXT}\n\n{status}"
    else:
        text = f"{WELCOME_TEXT}\n\n_No schedule set yet._ Tap *Set daily time*."

    await update.message.reply_text(
        text, reply_markup=MAIN_KB, parse_mode="Markdown", disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME_TEXT, reply_markup=MAIN_KB, parse_mode="Markdown"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    schedules = load_schedules()
    t = schedules.get(chat_id)
    if not t:
        await update.message.reply_text("No schedule set.", reply_markup=MAIN_KB)
        return
    h, m = [int(x) for x in t.split(":")]
    await update.message.reply_text(
        f"Current schedule: *{t}* daily\nNext send: *{describe_next_run(h, m)}*",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    log.info("Button pressed by %s: %s", chat_id, q.data)

    if q.data == "run_now":
        try:
            text = await asyncio.to_thread(build_report)
            await q.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            log.exception("run_now failed: %s", e)
            await q.message.reply_text("❌ Failed to fetch/send report. See logs.")
        return

    if q.data == "set_time":
        context.user_data[AWAIT_FLAG] = True
        await q.message.reply_text(ASK_TIME, parse_mode="Markdown")
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user's reply when we're awaiting a time string."""
    if not context.user_data.get(AWAIT_FLAG):
        return  # ignore unrelated text

    chat_id = update.effective_chat.id
    user_text = (update.message.text or "").strip()
    parsed = parse_hhmm(user_text)
    if not parsed:
        await update.message.reply_text(
            "Please send time like `08:30` or `7:45` (24-hour).",
            parse_mode="Markdown"
        )
        return

    hour, minute = parsed

    # Save schedule
    schedules = load_schedules()
    schedules[str(chat_id)] = f"{hour:02d}:{minute:02d}"
    save_schedules(schedules)

    # Clear flag
    context.user_data[AWAIT_FLAG] = False

    # (Re)register job
    await register_daily_job(context.application, chat_id, hour, minute)

    # Confirmation message
    await update.message.reply_text(
        f"✅ *Your time is set to {hour:02d}:{minute:02d} now.*\n"
        f"Next send: *{describe_next_run(hour, minute)}*",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

# ---- jobs ----
async def run_daily_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        text = await asyncio.to_thread(build_report)
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        log.exception("Job send failed for %s: %s", chat_id, e)

async def register_daily_job(app: Application, chat_id: int, hour: int, minute: int):
    name = f"daily_{chat_id}"
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    app.job_queue.run_daily(
        run_daily_job,
        time=scheduled_time(hour, minute),
        name=name,
        chat_id=chat_id,
    )
    log.info("Registered daily job for %s at %02d:%02d", chat_id, hour, minute)

async def load_all_jobs(app: Application):
    app.job_queue.run_repeating(poll_stock_job, interval=POLL_MINUTES * 60, first=10, name="stock_poll")
    schedules = load_schedules()
    for chat_id_str, hm in schedules.items():
        try:
            h, m = [int(x) for x in hm.split(":")]
            await register_daily_job(app, int(chat_id_str), h, m)
        except Exception as e:
            log.error("Failed to register job for %s: %s", chat_id_str, e)

# ---- stock watches ----
async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args).strip()
    if not term:
        await update.message.reply_text("Usage: /watch <product name or part of it>")
        return
    watches = stock_watch.load_watches()
    terms = watches.setdefault(str(update.effective_chat.id), [])
    if term.lower() not in [t.lower() for t in terms]:
        terms.append(term)
        stock_watch.save_watches(watches)
    await update.message.reply_text(f"Watching for products matching: {term}")

async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args).strip().lower()
    watches = stock_watch.load_watches()
    chat_id = str(update.effective_chat.id)
    terms = watches.get(chat_id, [])
    kept = [t for t in terms if t.lower() != term]
    if not term or len(kept) == len(terms):
        await update.message.reply_text("Usage: /unwatch <term exactly as listed by /watches>")
        return
    watches[chat_id] = kept
    stock_watch.save_watches(watches)
    await update.message.reply_text(f"Stopped watching: {term}")

async def watches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    terms = stock_watch.load_watches().get(str(update.effective_chat.id), [])
    if terms:
        text = "Watching:\n" + "\n".join(f"• {t}" for t in terms)
    else:
        text = "No watches set. Use /watch <product>."
    await update.message.reply_text(text)

async def poll_stock_job(context: ContextTypes.DEFAULT_TYPE):
    """Read the catalog, alert watching chats about changes, then store the new state."""
    try:
        html = await asyncio.to_thread(fetch, SHOP)
        in_stock, out_stock = parse_catalog(html, SHOP)
    except Exception as e:
        log.error("Stock poll failed: %s", e)
        return
    cur = stock_watch.snapshot(in_stock, out_stock)
    if not cur:
        log.warning("Stock poll found no products, skipping this check.")
        return
    state, alerts = stock_watch.process_poll(stock_watch.load_state(), cur, stock_watch.load_watches())
    for chat_id, changes in alerts.items():
        for c in changes:
            try:
                await context.bot.send_message(
                    chat_id=int(chat_id), text=stock_watch.build_alert(c, SHOP_NAME), parse_mode="Markdown"
                )
            except Exception as e:
                log.error("Alert send failed for %s: %s", chat_id, e)
    stock_watch.save_state(state)

# ---- errors ----
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", context.error)

def main():
    token = BOT_TOKEN
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (see .env.example).")
    app = Application.builder().token(token).build()
    app.post_init = load_all_jobs

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watches", watches_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    print("Bot running. Use /start in Telegram.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
