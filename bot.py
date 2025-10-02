#!/usr/bin/env python3
"""
Final OTP Bot single-file.

Env vars required:
 - BOT_TOKEN (Telegram Bot token)
 - API_TOKEN (OTP API token)
 - MONGO_URI (MongoDB connection string)
 - OWNER_ID (owner's numeric Telegram user id)

Optional:
 - API_URL (defaults to http://147.135.212.197/crapi/s1t/viewstats)
 - POLL_INTERVAL (seconds, default 2)

Dependencies (requirements.txt):
 python-telegram-bot==20.5
 pymongo==4.7.1
 requests==2.31.0
 phonenumbers==8.14.6
 pycountry==22.3.5
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timezone
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
    ChatMemberHandler,
    filters,
)

# ---------------- Config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN", "")
API_URL = os.getenv("API_URL", "http://147.135.212.197/crapi/s1t/viewstats")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2"))

# ---------------- Logging ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- Mongo ----------------
mongo = MongoClient(MONGO_URI)
db = mongo["otpbot"]
groups_col = db["groups"]   # { chat_id: str, button_text: str, button_url: str }
admins_col = db["admins"]   # { admin_id: int }
stats_col = db["stats"]     # { chat_id: str, messages: int }

# ---------------- Utilities ----------------
def is_admin(user_id: int) -> bool:
    if OWNER_ID and user_id == OWNER_ID:
        return True
    return admins_col.find_one({"admin_id": user_id}) is not None

def extract_otp(message: str) -> str:
    if not message:
        return "N/A"
    m = message.replace("–", "-").replace("—", "-")
    possible = re.findall(r'\d{3,4}[- ]?\d{3,4}', m)
    if possible:
        return possible[0].replace("-", "").replace(" ", "")
    fallback = re.search(r'\d{4,8}', m)
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
    if not number:
        return "N/A"
    number = str(number)
    if len(number) >= 10:
        return number[:3] + "***" + number[-5:]
    return number

def format_message(sms: dict) -> str:
    number = sms.get("num", "") or ""
    msg = sms.get("message", "") or ""
    time_sent = sms.get("dt") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
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

# ---------------- API Fetch ----------------
def fetch_latest_sms():
    """Synchronous HTTP fetch. Returns dict or None."""
    try:
        res = requests.get(API_URL, params={"token": API_TOKEN, "records": 1}, timeout=8)
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("status") == "success" and data.get("data"):
                return data["data"][0]
    except Exception as e:
        logger.debug("API fetch error: %s", e)
    return None

# ---------------- Sending & Stats ----------------
async def send_otp_to_groups(application, text: str):
    """Send formatted text to every group stored in Mongo. Increment stats on success."""
    groups = list(groups_col.find({}))
    for g in groups:
        cid = g.get("chat_id")
        if not cid:
            continue
        # try to convert to int for send_message, otherwise keep string
        try:
            send_to = int(cid)
        except Exception:
            send_to = cid
        btn_text = g.get("button_text", "Open")
        btn_url = g.get("button_url", "https://t.me/")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
        try:
            await application.bot.send_message(
                chat_id=send_to,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=keyboard
            )
            # increment counter
            stats_col.update_one({"chat_id": str(cid)}, {"$inc": {"messages": 1}}, upsert=True)
            logger.info("Sent OTP to %s", cid)
        except Exception as e:
            logger.warning("Failed to send to %s: %s", cid, e)

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
        "/status — Show bot status (uptime, group counts, per-group messages)\n"
        "/help — Show this message\n\n"
        "<i>Owner-only commands are marked above.</i>"
    )
    await update.message.reply_text(commands_text, parse_mode="HTML", disable_web_page_preview=True)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    groups_count = groups_col.count_documents({})
    admins_count = admins_col.count_documents({}) + (1 if OWNER_ID else 0)
    start_time = context.bot_data.get("start_time")
    uptime = "Unknown"
    if start_time:
        uptime_delta = datetime.now(timezone.utc) - start_time
        uptime = str(uptime_delta).split('.')[0]  # trim microseconds

    # per-group stats
    stats = list(stats_col.find({}))
    stats_lines = []
    total_messages = 0
    for s in stats:
        msg_count = s.get("messages", 0)
        total_messages += msg_count
        stats_lines.append(f"• {s.get('chat_id')}: {msg_count} msgs")
    stats_text = "\n".join(stats_lines) if stats_lines else "No messages yet."

    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"• Groups configured: <b>{groups_count}</b>\n"
        f"• Admins (incl owner): <b>{admins_count}</b>\n"
        f"• Uptime: <b>{uptime}</b>\n"
        f"• Total messages sent: <b>{total_messages}</b>\n\n"
        f"<b>Messages per group:</b>\n{stats_text}"
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
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("🚫 You are not admin")
        return
    raw = update.message.text or ""
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

# ---------------- Callback Handler ----------------
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

# ---------------- Chat Member (New Group) Handler ----------------
async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Detect when bot is added to a group and notify owner."""
    try:
        cm = update.my_chat_member
        if not cm:
            return
        new_status = cm.new_chat_member.status  # e.g., 'member' or 'administrator'
        # When bot is added as 'member' or 'administrator', notify owner
        if new_status in ("member", "administrator"):
            chat = update.effective_chat
            if chat and chat.type in ("group", "supergroup"):
                # try to create invite link (may fail if bot lacks permission)
                link = None
                try:
                    invite = await context.bot.create_chat_invite_link(chat.id)
                    link = invite.invite_link
                except Exception:
                    link = "❌ No invite link available / insufficient permissions"
                text = (
                    f"✅ <b>Bot added to a new group</b>\n\n"
                    f"📛 <b>Group:</b> {chat.title or 'No title'}\n"
                    f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
                    f"🔗 <b>Invite Link:</b> {link}"
                )
                # send to owner (if valid)
                if OWNER_ID:
                    try:
                        await context.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
                    except Exception as e:
                        logger.warning("Failed to notify owner about new group: %s", e)
    except Exception as e:
        logger.exception("Error in my_chat_member_handler: %s", e)

# ---------------- OTP Worker ----------------
async def otp_worker(application):
    """Background OTP poller and forwarder."""
    last_msg_id = None
    logger.info("Starting OTP worker, polling every %s seconds", POLL_INTERVAL)
    while True:
        sms = await asyncio.to_thread(fetch_latest_sms)
        if sms:
            msg_id = f"{sms.get('num')}_{sms.get('dt')}"
            msg_text = (sms.get("message") or "").lower()
            # naive keyword detection
            keywords = ["otp", "code", "verify", "رمز", "password", "كود"]
            if msg_id != last_msg_id and any(k in msg_text for k in keywords):
                formatted = format_message(sms)
                try:
                    await send_otp_to_groups(application, formatted)
                except Exception as e:
                    logger.exception("Error sending OTP to groups: %s", e)
                last_msg_id = msg_id
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Startup hook ----------------
async def on_startup(application):
    application.bot_data["start_time"] = datetime.now(timezone.utc)
    # start otp worker task
    application.create_task(otp_worker(application))
    logger.info("Bot started. Owner ID: %s", OWNER_ID)

# ---------------- Main ----------------
def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN not set. Exiting.")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Register handlers
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("addgroup", addgroup_cmd))
    app.add_handler(CommandHandler("removegroup", removegroup_cmd))
    app.add_handler(CommandHandler("listgroups", listgroups_cmd))
    app.add_handler(CommandHandler("setbutton", setbutton_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))

    # post init startup
    app.post_init.append(on_startup)

    logger.info("Launching bot (polling)...")
    app.run_polling()

if __name__ == "__main__":
    main()
