import os
import time
import re
import logging
import requests
from datetime import datetime
import phonenumbers
import pycountry
from pymongo import MongoClient

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes, ChatMemberHandler
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_TOKEN = os.getenv("API_TOKEN")
API_URL = "http://147.135.212.197/crapi/s1t/viewstats"

OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))

MONGO_URI = os.getenv("MONGO_URI", "")
mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
db = mongo_client["otpbot"] if mongo_client else None

MAX_RECORDS = 1
last_msg_id = None

# Local memory fallback
groups = {}   # {chat_id: {"button_text":..., "button_url":..., "messages": count}}
admins = set([OWNER_ID])

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("OTP-Bot")

# -------------- UTILS -----------------

def save_state():
    if db:
        db.groups.delete_many({})
        for gid, data in groups.items():
            db.groups.insert_one({"_id": gid, **data})
        db.admins.delete_many({})
        for aid in admins:
            db.admins.insert_one({"_id": aid})

def load_state():
    global groups, admins
    if db:
        groups = {}
        for g in db.groups.find():
            gid = g["_id"]
            g.pop("_id")
            groups[gid] = g
        admins.clear()
        for a in db.admins.find():
            admins.add(a["_id"])

def extract_otp(message: str):
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
    except:
        return "Unknown", "🌍"

def detect_service(msg: str):
    services = {
        "whatsapp": "WhatsApp", "telegram": "Telegram", "facebook": "Facebook",
        "instagram": "Instagram", "gmail": "Gmail", "google": "Google",
        "imo": "IMO", "signal": "Signal", "twitter": "Twitter",
        "microsoft": "Microsoft", "yahoo": "Yahoo", "tiktok": "TikTok"
    }
    msg = msg.lower()
    for key in services:
        if key in msg:
            return services[key]
    return "Unknown"

def mask_number(number: str):
    return number[:3] + "***" + number[-5:] if len(number) >= 10 else number

def format_message(sms):
    number = sms.get("num", "")
    msg = sms.get("message", "")
    time_sent = sms.get("dt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    country, flag = detect_country_flag(number)
    otp = extract_otp(msg)
    service = detect_service(msg)
    masked = mask_number(number)

    return f"""<b> ✅ New OTP Received Successfully </b>

🕰️ <b>Time:</b> {time_sent}
📞 <b>Number:</b> {masked}
🔑 <b>OTP Code:</b> <code>{otp}</code>
🌍 <b>Country:</b> {flag} {country}
📱 <b>Service:</b> {service}
❤️ <b>Full Message:</b>
<pre>{msg}</pre>
"""

def fetch_latest_sms():
    try:
        res = requests.get(API_URL, params={"token": API_TOKEN, "records": MAX_RECORDS})
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                return data.get("data", [])[0]
    except Exception as e:
        logger.error(f"API Error: {e}")
    return None

async def send_telegram(app: Application, text: str):
    for chat_id, data in groups.items():
        btn_text = data.get("button_text", "📞 ALL NUMBERS 📞")
        btn_url = data.get("button_url", "https://t.me/")
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn_text, url=btn_url)]])
            )
            groups[chat_id]["messages"] = groups[chat_id].get("messages", 0) + 1
            save_state()
        except Exception as e:
            logger.error(f"Send error {chat_id}: {e}")

# --------------- HANDLERS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in admins:
        return await update.message.reply_text("⛔ You are not authorized.")
    commands = """
<b>🤖 Admin Commands:</b>

/addgroup <chat_id> <btn_text> <btn_url>
/removegroup <chat_id>
/setbutton <chat_id> <btn_text> <btn_url>
/status
/addadmin <user_id>
/removeadmin <user_id>
/admins
"""
    await update.message.reply_text(commands, parse_mode="HTML")

async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins:
        return
    try:
        chat_id = int(context.args[0])
        btn_text, btn_url = context.args[1], context.args[2]
        groups[chat_id] = {"button_text": btn_text, "button_url": btn_url, "messages": 0}
        save_state()
        await update.message.reply_text(f"✅ Group {chat_id} added.")
    except:
        await update.message.reply_text("Usage: /addgroup <chat_id> <btn_text> <btn_url>")

async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins:
        return
    try:
        chat_id = int(context.args[0])
        groups.pop(chat_id, None)
        save_state()
        await update.message.reply_text(f"❌ Group {chat_id} removed.")
    except:
        await update.message.reply_text("Usage: /removegroup <chat_id>")

async def set_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins:
        return
    try:
        chat_id = int(context.args[0])
        btn_text, btn_url = context.args[1], context.args[2]
        if chat_id in groups:
            groups[chat_id]["button_text"] = btn_text
            groups[chat_id]["button_url"] = btn_url
            save_state()
            await update.message.reply_text(f"✅ Button updated for {chat_id}.")
        else:
            await update.message.reply_text("❌ Group not found.")
    except:
        await update.message.reply_text("Usage: /setbutton <chat_id> <btn_text> <btn_url>")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins:
        return
    text = "📊 <b>Bot Status</b>\n\n"
    for gid, data in groups.items():
        text += f"👥 <b>{gid}</b> → {data.get('messages',0)} msgs\n"
    await update.message.reply_text(text, parse_mode="HTML")

async def add_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        uid = int(context.args[0])
        admins.add(uid)
        save_state()
        await update.message.reply_text(f"✅ Admin {uid} added.")
    except:
        await update.message.reply_text("Usage: /addadmin <user_id>")

async def remove_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        uid = int(context.args[0])
        admins.discard(uid)
        save_state()
        await update.message.reply_text(f"❌ Admin {uid} removed.")
    except:
        await update.message.reply_text("Usage: /removeadmin <user_id>")

async def list_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in admins:
        return
    text = "👮 <b>Admins:</b>\n" + "\n".join([str(a) for a in admins])
    await update.message.reply_text(text, parse_mode="HTML")

# --- Alert when bot added to new group ---
async def new_group_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        try:
            try:
                link = await context.bot.create_chat_invite_link(chat.id)
                link = link.invite_link
            except:
                link = "❌ No invite link available"

            text = (
                f"✅ <b>Bot added to a new group</b>\n\n"
                f"📛 <b>Group:</b> {chat.title}\n"
                f"🆔 <b>ID:</b> <code>{chat.id}</code>\n"
                f"🔗 <b>Invite Link:</b> {link}"
            )
            await context.bot.send_message(chat_id=OWNER_ID, text=text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Group alert error: {e}")

# --------------- STARTUP ----------------
async def on_startup(app: Application):
    load_state()
    logger.info("Bot started!")

async def otp_loop(app: Application):
    global last_msg_id
    while True:
        sms = fetch_latest_sms()
        if sms:
            msg_id = f"{sms.get('num')}_{sms.get('dt')}"
            msg_text = sms.get("message", "").lower()
            if msg_id != last_msg_id and any(k in msg_text for k in ["otp", "code", "verify", "رمز", "password"]):
                formatted = format_message(sms)
                await send_telegram(app, formatted)
                last_msg_id = msg_id
        await asyncio.sleep(2)

import asyncio
def main():
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addgroup", add_group))
    app.add_handler(CommandHandler("removegroup", remove_group))
    app.add_handler(CommandHandler("setbutton", set_button))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("admins", list_admins))
    app.add_handler(CommandHandler("addadmin", add_admin))
    app.add_handler(CommandHandler("removeadmin", remove_admin))

    app.add_handler(ChatMemberHandler(new_group_alert, ChatMemberHandler.MY_CHAT_MEMBER))

    # Run polling + background loop
    loop = asyncio.get_event_loop()
    loop.create_task(otp_loop(app))
    app.run_polling()

if __name__ == "__main__":
    main()
