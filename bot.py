#!/usr/bin/env python3
"""
OTP Bot with admin commands + per-group button support (MongoDB persistence)

Env vars required:
 - BOT_TOKEN    (Telegram bot token)
 - API_TOKEN    (external OTP API token if needed)
 - API_URL      (optional - defaults to embedded example)
 - MONGO_URI    (mongodb://... , defaults to localhost)
 - OWNER_ID     (your Telegram user id, numeric)

Install:
 pip install python-telegram-bot==20.5 pymongo requests phonenumbers pycountry
"""

import os
import re
import asyncio
import logging
from datetime import datetime
import requests
import phonenumbers
import pycountry
from pymongo import MongoClient

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN", "")
API_URL = os.getenv("API_URL", "http://147.135.212.197/crapi/s1t/viewstats")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # set your Telegram user id here

# OTP poll interval (seconds)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2"))

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- MongoDB ----------------
mongo = MongoClient(MONGO_URI)
db = mongo["otpbot"]
groups_col = db["groups"]      # documents: { "chat_id": "<id>", "button_text": "...", "button_url": "..." }
admins_col = db["admins"]      # documents: { "admin_id": <int> }

# ---------------- Utilities ----------------
def is_admin(user_id: int) -> bool:
    if user_id == OWNER_ID:
        return True
    return admins_col.find_one({"admin_id": user_id}) is not None

def extract_otp(message: str) -> str:
    message = message.replace("–", "-").replace("—", "-")
    possible_codes = re.findall(r'\d{3,4}[- ]?\d{3,4}', message)
    if possible_codes:
        return possible_codes[0].replace("-", "").replace(" ", "")
    fallback = re.search(r'\d{4,8}', message)
    return fallback.group(0) if fallback else "N/A"

def detect_country_flag(number: str):
    try:
        parsed = phonenumbers.parse("+" + number, None)
        region = phonenumbers.region_code_for_number(parsed)
        country = pycountry.countries.get(alpha_2=region).name
        flag = ''.join([chr(ord(c) + 127397) for c in region.upper()])
        return country, flag
    except Exception:
        return "Unknown", "🌍"

def mask_number(number: str) -> str:
    if len(number) >= 10:
        return number[:3] + "***" + number[-5:]
    return number

def format_message(sms: dict) -> str:
    number = sms.get("num", "")
    msg = sms.get("message", "")
    time_sent = sms.get("dt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    country, flag = detect_country_flag(number)
    otp = extract_otp(msg)
    masked = mask_number(number)
    return (
        f"<b>✅ New OTP Received</b>\n\n"
        f"🕰️ <b>Time:</b> {time_sent}\n"
        f"📞 <b>Number:</b> {masked}\n"
        f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
        f"🌍 <b>Country:</b> {flag} {country}\n\n"
        f"❤️ <b>Full Message:</b>\n<pre>{msg}</pre>\n\n"
        f"<i>Powered by your bot</i>"
    )

# ---------------- API Fetch (sync) ----------------
def fetch_latest_sms():
    """Fetch latest record from API (sync). Return dict or None."""
    try:
        res = requests.get(API_URL, params={"token": API_TOKEN, "records": 1}, timeout=8)
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success" and data.get("data"):
                return data["data"][0]
    except Exception as e:
        logger.debug("API fetch error: %s", e)
    return None

# ---------------- Bot Commands ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text(
            "👋 Hello! This bot forwards OTPs to admin-managed groups.\n"
            "If you think you should be an admin, contact the owner."
        )
        return

    commands_text = (
        "<b>🔐 Admin Commands — OTP Bot</b>\n\n"
        "/addgroup &lt;chat_id&gt; — Add group to forward OTPs\n"
        "/removegroup &lt;chat_id&gt; — Remove group from forwarding\n"
        "/listgroups — List all configured groups (with actions)\n"
        "/setbutton &lt;chat_id&gt; \"&lt;text&gt;\" &lt;url&gt; — Set button text and URL for a group\n"
        "/addadmin &lt;user_id&gt; — (Owner only) Add an admin\n"
        "/removeadmin &lt;user_id&gt; — (Owner only) Remove an admin\n"
        "/status — Show bot status (uptime, groups count)\n"
        "/help — Show this message\n\n"
        "<i>Owner-only commands are marked above.</i>"
    )
    await update.message.reply_text(commands_text, parse_mode="HTML", disable_web_page_preview=True)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    groups_count = groups_col.count_documents({})
    admins_count = admins_col.count_documents({})
    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"• Groups configured: <b>{groups_count}</b>\n"
        f"• Admins (incl owner): <b>{admins_count + (1 if OWNER_ID else 0)}</b>\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def addgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addgroup <chat_id>")
        return
    chat_id = context.args[0]
    groups_col.update_one({"chat_id": str(chat_id)}, {"$set": {"chat_id": str(chat_id)}}, upsert=True)
    await update.message.reply_text(f"✅ Group added: {chat_id}")

async def removegroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removegroup <chat_id>")
        return
    chat_id = context.args[0]
    groups_col.delete_one({"chat_id": str(chat_id)})
    await update.message.reply_text(f"❌ Group removed: {chat_id}")

async def listgroups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    groups = list(groups_col.find({}))
    if not groups:
        await update.message.reply_text("❌ No groups configured yet.")
        return

    for g in groups:
        cid = g["chat_id"]
        btn_text = g.get("button_text", "Open")
        btn_url = g.get("button_url", "https://t.me/")
        text = f"• <b>{cid}</b>\nButton: {btn_text}\nURL: {btn_url}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔧 Edit button (use /setbutton)", callback_data=f"editbtn:{cid}")],
            [InlineKeyboardButton("🗑 Remove group", callback_data=f"delgrp:{cid}")]
        ])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)

async def setbutton_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Usage:
     /setbutton <chat_id> "<button text>" <url>
    Example:
     /setbutton -1001234567890 "📞 ALL NUMBERS" https://t.me/MyLink
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return

    raw = update.message.text or ""
    # parse chat_id, quoted text, url
    # pattern: /setbutton <chat_id> "some text" url
    m = re.match(r'^/setbutton\s+(\S+)\s+"([^"]+)"\s+(\S+)', raw)
    if not m:
        await update.message.reply_text('Usage: /setbutton <chat_id> "<button text>" <url>\nExample: /setbutton -1001234567890 "📞 ALL" https://t.me/link')
        return

    chat_id, btn_text, btn_url = m.group(1), m.group(2), m.group(3)
    groups_col.update_one({"chat_id": str(chat_id)}, {"$set": {"button_text": btn_text, "button_url": btn_url}}, upsert=True)
    await update.message.reply_text(f"✅ Button set for {chat_id}\nText: {btn_text}\nURL: {btn_url}")

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 Only owner can add admin")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User id must be a number")
        return
    admins_col.update_one({"admin_id": uid}, {"$set": {"admin_id": uid}}, upsert=True)
    await update.message.reply_text(f"✅ Admin added: {uid}")

async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("🚫 Only owner can remove admin")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeadmin <user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("User id must be a number")
        return
    admins_col.delete_one({"admin_id": uid})
    await update.message.reply_text(f"❌ Admin removed: {uid}")

# ---------------- Callback Query Handler ----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = update.effective_user.id
    if not is_admin(uid):
        await query.edit_message_text("🚫 You are not admin")
        return

    if data.startswith("delgrp:"):
        chat_id = data.split(":", 1)[1]
        groups_col.delete_one({"chat_id": chat_id})
        await query.edit_message_text(f"❌ Group removed: {chat_id}")
    elif data.startswith("editbtn:"):
        chat_id = data.split(":", 1)[1]
        await query.edit_message_text(
            f"To edit button for {chat_id}, use:\n"
            f'/setbutton {chat_id} "📞 ALL NUMBERS" https://t.me/example'
        )
    else:
        await query.edit_message_text("Unknown action")

# ---------------- Send OTP to groups ----------------
async def send_otp_to_groups(application, text: str):
    groups = list(groups_col.find({}))
    for g in groups:
        cid = g["chat_id"]
        btn_text = g.get("button_text", "📞 Open")
        btn_url = g.get("button_url", "https://t.me/")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
        try:
            await application.bot.send_message(chat_id=cid, text=text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard)
        except Exception as e:
            logger.warning("Failed to send to %s: %s", cid, e)

# ---------------- OTP Background Loop ----------------
async def otp_worker(application):
    """Long-running background task that polls the API and forwards OTPs."""
    last_msg_id = None
    logger.info("OTP worker started, polling every %s seconds", POLL_INTERVAL)
    while True:
        # fetch in thread to avoid blocking loop
        sms = await asyncio.to_thread(fetch_latest_sms)
        if sms:
            msg_id = f"{sms.get('num')}_{sms.get('dt')}"
            msg_text = (sms.get("message") or "").lower()
            # naive detection - customize keywords if needed
            if msg_id != last_msg_id and any(k in msg_text for k in ["otp", "code", "verify", "رمز", "password", "كود"]):
                formatted = format_message(sms)
                await send_otp_to_groups(application, formatted)
                last_msg_id = msg_id
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set in env")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("addgroup", addgroup_cmd))
    app.add_handler(CommandHandler("removegroup", removegroup_cmd))
    app.add_handler(CommandHandler("listgroups", listgroups_cmd))
    app.add_handler(CommandHandler("setbutton", setbutton_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))

    # Callback handler for inline actions
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Start background OTP worker after bot starts
    async def _start_worker(_ctx: ContextTypes.DEFAULT_TYPE):
        # run as independent task
        asyncio.create_task(otp_worker(app))

    # schedule one-time job shortly after startup
    app.job_queue.run_once(lambda ctx: asyncio.create_task(otp_worker(app)), when=1)

    logger.info("Starting bot...")
    app.run_polling()

if __name__ == "__main__":
    main()
