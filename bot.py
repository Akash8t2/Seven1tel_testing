#!/usr/bin/env python3
"""
Final OTP Bot (single file) — updated country detection

Env vars expected:
 - BOT_TOKEN    (required) Telegram bot token
 - OWNER_ID     (required) Your Telegram numeric user id
 - MONGO_URI    (optional) MongoDB connection string; if absent, local state.json used
 - API_TOKEN    (optional) OTP provider token
 - API_URL      (optional) OTP API URL (default uses example URL)
 - POLL_INTERVAL(optional) seconds between API polls (default 2)

Dependencies (requirements.txt):
 python-telegram-bot==20.5
 pymongo==4.7.1
 requests==2.31.0
 phonenumbers==8.14.6
 pycountry==22.3.5
"""

import os
import json
import re
import time
import logging
import asyncio
from datetime import datetime, timezone

import requests
import phonenumbers
import pycountry
from pymongo import MongoClient

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ChatInviteLink, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MONGO_URI = os.getenv("MONGO_URI", "").strip() or None
API_TOKEN = os.getenv("API_TOKEN", "").strip() or None
API_URL = os.getenv("API_URL", "http://147.135.212.197/crapi/s1t/viewstats")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "2"))

STATE_FILE = "state.json"

if not BOT_TOKEN or not OWNER_ID:
    raise SystemExit("BOT_TOKEN and OWNER_ID must be set as environment variables")

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("otp-bot")

# ---------------- Storage (Mongo or JSON) ----------------
db = None
if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        # Trigger connection check
        client.server_info()
        db = client["otpbot"]
        logger.info("Connected to MongoDB")
    except Exception as e:
        logger.warning("Could not connect to MongoDB, falling back to local JSON state. Error: %s", e)
        db = None

# In-memory state (source of truth during runtime; persisted to DB or JSON)
state = {
    "groups": {},   # chat_id (string) -> { "title": str, "button_text": str, "button_url": str, "messages": int }
    "admins": set(),# set of int user ids
    "owner": OWNER_ID,
    "start_time": None,
}

# Initialize admins with owner
state["admins"].add(OWNER_ID)

# ---------------- Helpers ----------------
def _load_state_from_db():
    global state
    if db is None:
        return
    try:
        doc = db.settings.find_one({"_id": "state"})
        if doc and "data" in doc:
            data = doc["data"]
            # groups
            state["groups"] = data.get("groups", {})
            # convert admin list to set
            state["admins"] = set(data.get("admins", []))
            state["admins"].add(OWNER_ID)
            logger.info("Loaded state from MongoDB")
    except Exception as e:
        logger.error("Error loading state from DB: %s", e)

def _save_state_to_db():
    if db is None:
        return
    try:
        data = {
            "groups": state["groups"],
            "admins": list(state["admins"]),
        }
        db.settings.update_one({"_id": "state"}, {"$set": {"data": data}}, upsert=True)
    except Exception as e:
        logger.error("Error saving state to DB: %s", e)

def _load_state_from_file():
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                state["groups"] = data.get("groups", {})
                state["admins"] = set(data.get("admins", []))
                state["admins"].add(OWNER_ID)
                logger.info("Loaded state from local file")
        except Exception as e:
            logger.error("Error loading local state file: %s", e)

def _save_state_to_file():
    try:
        data = {"groups": state["groups"], "admins": list(state["admins"])}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        logger.error("Error saving local state file: %s", e)

def load_state():
    if db is not None:
        _load_state_from_db()
    else:
        _load_state_from_file()

def save_state():
    if db is not None:
        _save_state_to_db()
    else:
        _save_state_to_file()

def is_admin(user_id: int) -> bool:
    return (user_id in state["admins"]) or (user_id == state["owner"])

# ---------- Message formatting / parsing ----------
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
    """
    Robust country detection:
    - Remove non-digit chars (handles masked numbers like 228***29936)
    - Try parsing with '+' + cleaned digits
    - If that fails, try parsing with default region 'US' as fallback
    - Return (country_name, flag_emoji) or ("Unknown", "🌍")
    """
    if not number:
        return "Unknown", "🌍"
    # keep digits only
    cleaned = re.sub(r'\D', '', str(number))
    if not cleaned:
        return "Unknown", "🌍"

    # Try parse with leading +
    try:
        to_parse = "+" + cleaned
        parsed = phonenumbers.parse(to_parse, None)
        region = phonenumbers.region_code_for_number(parsed)
        if region:
            country = pycountry.countries.get(alpha_2=region)
            country_name = country.name if country else region
            flag = ''.join([chr(ord(c) + 127397) for c in region.upper()])
            return country_name, flag
    except Exception:
        pass

    # Fallback: try parse as national number with US region (best-effort)
    try:
        parsed = phonenumbers.parse(cleaned, "US")
        region = phonenumbers.region_code_for_number(parsed)
        if region:
            country = pycountry.countries.get(alpha_2=region)
            country_name = country.name if country else region
            flag = ''.join([chr(ord(c) + 127397) for c in region.upper()])
            return country_name, flag
    except Exception:
        pass

    return "Unknown", "🌍"

def mask_number(number: str) -> str:
    s = str(number)
    if len(s) >= 10:
        return s[:3] + "***" + s[-5:]
    return s

def format_message(sms: dict) -> str:
    # Use raw values where possible (don't mask before detecting country)
    number = sms.get("num", "") or sms.get("number", "") or ""
    msg = sms.get("message", "") or sms.get("text", "") or ""
    time_sent = sms.get("dt") or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    country, flag = detect_country_flag(number)
    otp = extract_otp(msg)
    masked = mask_number(number)
    return (
        f"<b>✅ New OTP Received</b>\n\n"
        f"🕰️ <b>Time:</b> {time_sent}\n"
        f"📞 <b>Number:</b> {masked}\n"
        f"🔑 <b>OTP Code:</b> <code>{otp}</code>\n"
        f"🌍 <b>Country:</b> {flag} {country}\n\n"
        f"❤️ <b>Full Message:</b>\n<pre>{msg}</pre>"
    )

# ---------------- API fetch ----------------
def fetch_latest_sms():
    """Return a single SMS dict or None. Handles a few common response shapes."""
    try:
        params = {"token": API_TOKEN, "records": 1} if API_TOKEN else {"records": 1}
        resp = requests.get(API_URL, params=params, timeout=10)
        if resp.status_code != 200:
            logger.debug("API not 200: %s", resp.status_code)
            return None
        data = resp.json()
        # Attempt common shapes:
        if isinstance(data, dict):
            # example: {status: "success", data: [ {...} ]}
            if data.get("status") == "success" and isinstance(data.get("data"), list) and data["data"]:
                return data["data"][0]
            # some APIs return messages list
            if "messages" in data and isinstance(data["messages"], list) and data["messages"]:
                return data["messages"][0]
        elif isinstance(data, list) and data:
            return data[0]
    except Exception as e:
        logger.debug("fetch_latest_sms error: %s", e)
    return None

# ---------------- Sending to groups ----------------
async def send_to_all_groups(app, text: str):
    """Sends a message with per-group button and increments counters."""
    groups = state["groups"]
    for cid_str, g in groups.items():
        # chat id may be numeric (int) or string - try int first
        try:
            chat_id = int(cid_str)
        except Exception:
            chat_id = cid_str
        btn_text = g.get("button_text", "Open")
        btn_url = g.get("button_url", "https://t.me/")
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
        try:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML",
                                       disable_web_page_preview=True, reply_markup=keyboard)
            # increment counter
            g["messages"] = g.get("messages", 0) + 1
            # persist after each successful send (keeps counts safe)
            save_state()
            logger.info("Sent to %s (total %s)", cid_str, g["messages"])
        except Exception as e:
            logger.warning("Failed to send to %s: %s", cid_str, e)

# ---------------- Bot Commands ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("👋 Hello — this bot forwards OTPs. Contact owner to get admin rights.")
        return
    text = (
        "<b>🔐 Admin Commands</b>\n\n"
        "/addgroup &lt;chat_id&gt; &lt;button_text&gt; &lt;button_url&gt;\n"
        "/removegroup &lt;chat_id&gt;\n"
        "/listgroups\n"
        "/setbutton &lt;chat_id&gt; &lt;button_text&gt; &lt;button_url&gt;\n"
        "/addadmin &lt;user_id&gt;  (owner only)\n"
        "/removeadmin &lt;user_id&gt;  (owner only)\n"
        "/status\n"
        "/stats\n"
        "/help\n"
    )
    await update.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)

async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ You are not admin")
    if len(context.args) < 3:
        return await update.message.reply_text("Usage: /addgroup <chat_id> <button_text> <button_url>")
    chat_id = context.args[0]
    btn_text = context.args[1]
    btn_url = context.args[2]
    state["groups"][str(chat_id)] = {"title": str(chat_id), "button_text": btn_text, "button_url": btn_url, "messages": 0}
    save_state()
    await update.message.reply_text(f"✅ Group {chat_id} added with button '{btn_text}'")

async def cmd_removegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ You are not admin")
    if len(context.args) < 1:
        return await update.message.reply_text("Usage: /removegroup <chat_id>")
    chat_id = str(context.args[0])
    if chat_id in state["groups"]:
        state["groups"].pop(chat_id, None)
        save_state()
        return await update.message.reply_text(f"❌ Group {chat_id} removed")
    return await update.message.reply_text("Group not found")

async def cmd_listgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ You are not admin")
    if not state["groups"]:
        return await update.message.reply_text("No groups configured yet")
    lines = []
    for cid, g in state["groups"].items():
        lines.append(f"{cid} — btn:'{g.get('button_text')}' url:{g.get('button_url')} msgs:{g.get('messages',0)}")
    await update.message.reply_text("\n".join(lines))

async def cmd_setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ You are not admin")
    if len(context.args) < 3:
        return await update.message.reply_text("Usage: /setbutton <chat_id> <button_text> <button_url>")
    cid = str(context.args[0])
    if cid not in state["groups"]:
        return await update.message.reply_text("Group not found")
    state["groups"][cid]["button_text"] = context.args[1]
    state["groups"][cid]["button_url"] = context.args[2]
    save_state()
    await update.message.reply_text(f"✅ Button updated for {cid}")

async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Only owner can add admins")
    if len(context.args) < 1:
        return await update.message.reply_text("Usage: /addadmin <user_id>")
    try:
        uid = int(context.args[0])
        state["admins"].add(uid)
        save_state()
        await update.message.reply_text(f"✅ Admin added: {uid}")
    except Exception:
        await update.message.reply_text("Invalid user id")

async def cmd_removeadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Only owner can remove admins")
    if len(context.args) < 1:
        return await update.message.reply_text("Usage: /removeadmin <user_id>")
    try:
        uid = int(context.args[0])
        if uid == OWNER_ID:
            return await update.message.reply_text("❌ Cannot remove owner")
        state["admins"].discard(uid)
        save_state()
        await update.message.reply_text(f"❌ Admin removed: {uid}")
    except Exception:
        await update.message.reply_text("Invalid user id")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ You are not admin")
    groups_count = len(state["groups"])
    admins_count = len(state["admins"])
    start_time = state.get("start_time")
    uptime = "Unknown"
    if start_time:
        uptime = str(datetime.now(timezone.utc) - start_time).split(".")[0]
    total_msgs = sum([g.get("messages", 0) for g in state["groups"].values()])
    text = (
        f"📊 <b>Bot Status</b>\n\n"
        f"• Groups configured: <b>{groups_count}</b>\n"
        f"• Admins: <b>{admins_count}</b>\n"
        f"• Uptime: <b>{uptime}</b>\n"
        f"• Total messages sent: <b>{total_msgs}</b>\n"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Only owner can view /stats")
    if not state["groups"]:
        return await update.message.reply_text("No groups configured")
    parts = []
    for cid, g in state["groups"].items():
        parts.append(f"{cid}: {g.get('messages',0)} msgs")
    await update.message.reply_text("📊 Message counts:\n" + "\n".join(parts))

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return await update.message.reply_text("⛔ Only owner can broadcast")
    msg = " ".join(context.args) or (update.message.reply_to_message.text if update.message.reply_to_message else None)
    if not msg:
        return await update.message.reply_text("Usage: /broadcast <text> or reply to a message with /broadcast")
    await send_to_all_groups(context.application, msg)
    await update.message.reply_text("✅ Broadcast sent")

# ---------------- CallbackQuery (placeholder) ----------------
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Action not implemented")

# ---------------- Detect when bot added to group ----------------
async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        cm = update.my_chat_member
        if not cm:
            return
        # notify owner on statuses where bot is added
        new_status = cm.new_chat_member.status
        if new_status in ("member", "administrator"):
            chat = update.effective_chat
            if chat and chat.type in ("group", "supergroup"):
                cid = str(chat.id)
                # add default group entry if not exists
                if cid not in state["groups"]:
                    state["groups"][cid] = {
                        "title": chat.title or cid,
                        "button_text": "Visit",
                        "button_url": "https://t.me/",
                        "messages": 0,
                    }
                    save_state()
                # try to create invite link (may fail if bot lacks perms)
                link = "N/A"
                try:
                    inv: ChatInviteLink = await context.bot.create_chat_invite_link(chat.id)
                    link = inv.invite_link
                except Exception:
                    link = "No invite link / insufficient perms"
                text = (
                    f"✅ <b>Bot added to a new group</b>\n\n"
                    f"📛 <b>Group:</b> {chat.title or 'no title'}\n"
                    f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
                    f"🔗 <b>Invite Link:</b> {link}"
                )
                try:
                    await context.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
                except Exception as e:
                    logger.warning("Couldn't notify owner: %s", e)
    except Exception as e:
        logger.exception("Error in my_chat_member handler: %s", e)

# ---------------- OTP worker ----------------
async def otp_worker(app):
    """Continuously poll API and forward detected OTPs to groups."""
    last_msg_id = None
    logger.info("OTP worker started, polling every %s seconds", POLL_INTERVAL)
    while True:
        try:
            sms = await asyncio.to_thread(fetch_latest_sms)
            if sms:
                # generate a message id that likely is unique per SMS
                msg_id = f"{sms.get('num')}_{sms.get('dt')}"
                content = (sms.get("message") or sms.get("text") or "").lower()
                keywords = ["otp", "code", "verify", "رمز", "password", "كود"]
                if msg_id != last_msg_id and any(k in content for k in keywords):
                    formatted = format_message(sms)
                    await send_to_all_groups(app, formatted)
                    last_msg_id = msg_id
        except Exception as e:
            logger.exception("otp_worker error: %s", e)
        await asyncio.sleep(POLL_INTERVAL)

# ---------------- Startup ----------------
async def on_startup(app):
    # mark start time
    state["start_time"] = datetime.now(timezone.utc)
    load_state()
    # start background worker
    # use create_task so run_polling can proceed
    asyncio.create_task(otp_worker(app))
    logger.info("Bot startup complete. Owner: %s", OWNER_ID)

# ---------------- Main ----------------
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(on_startup).build()

    # Commands
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("addgroup", cmd_addgroup))
    app.add_handler(CommandHandler("removegroup", cmd_removegroup))
    app.add_handler(CommandHandler("listgroups", cmd_listgroups))
    app.add_handler(CommandHandler("setbutton", cmd_setbutton))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("removeadmin", cmd_removeadmin))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Chat member handler to detect when bot is added to groups
    app.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    logger.info("Starting bot polling...")
    app.run_polling()

if __name__ == "__main__":
    main()
