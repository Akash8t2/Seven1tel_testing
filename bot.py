import os
import json
import logging
import asyncio
import requests
import phonenumbers
import pycountry
from datetime import datetime
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatInviteLink
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler,
    ChatMemberHandler,
)

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MONGO_URI = os.getenv("MONGO_URI", "")

API_TOKEN = os.getenv("API_TOKEN", "")
API_URL = os.getenv("API_URL", "")

# Mongo setup
db = None
if MONGO_URI:
    try:
        mongo = MongoClient(MONGO_URI)
        db = mongo["otpbot"]
    except Exception as e:
        print("Mongo connect failed:", e)

# Local state
groups = {}        # {chat_id: {"title":..., "button_text":..., "button_url":..., "messages": int}}
state_file = "state.json"

# Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================== STATE HANDLING ==================
def load_state():
    global groups
    try:
        if db is not None:
            saved = db.settings.find_one({"_id": "groups"})
            if saved:
                groups = saved["data"]
        else:
            if os.path.exists(state_file):
                with open(state_file, "r") as f:
                    groups = json.load(f)
    except Exception as e:
        logger.error(f"Error loading state: {e}")

def save_state():
    try:
        if db is not None:
            db.settings.update_one({"_id": "groups"}, {"$set": {"data": groups}}, upsert=True)
        else:
            with open(state_file, "w") as f:
                json.dump(groups, f)
    except Exception as e:
        logger.error(f"Error saving state: {e}")

# ================== BOT COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text = (
        "🤖 *Admin Panel Commands*\n\n"
        "/add `<chat_id>` `<button_text>` `<button_url>`\n"
        "/remove `<chat_id>`\n"
        "/groups – Show all groups\n"
        "/stats – Show message counts\n"
        "/broadcast `<msg>` – Send message to all groups"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def add_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    try:
        chat_id = str(context.args[0])
        button_text = context.args[1]
        button_url = context.args[2]
        groups[chat_id] = {
            "title": chat_id,
            "button_text": button_text,
            "button_url": button_url,
            "messages": 0,
        }
        save_state()
        await update.message.reply_text(f"✅ Group {chat_id} added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def remove_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    chat_id = str(context.args[0])
    if chat_id in groups:
        del groups[chat_id]
        save_state()
        await update.message.reply_text(f"❌ Group {chat_id} removed.")
    else:
        await update.message.reply_text("Group not found.")

async def list_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not groups:
        await update.message.reply_text("No groups configured.")
        return
    text = "📋 *Configured Groups:*\n\n"
    for cid, info in groups.items():
        text += f"- `{cid}` → {info['title']} (Messages: {info.get('messages',0)})\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    text = "📊 *Message Stats:*\n\n"
    for cid, info in groups.items():
        text += f"- {info['title']} → {info.get('messages',0)} messages\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    msg = " ".join(context.args)
    for cid in groups.keys():
        try:
            await context.bot.send_message(cid, msg)
        except Exception as e:
            logger.error(f"Broadcast failed to {cid}: {e}")
    await update.message.reply_text("✅ Broadcast sent.")

# ================== NEW GROUP DETECT ==================
async def group_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type in ["group", "supergroup"]:
        groups[str(chat.id)] = {
            "title": chat.title,
            "button_text": "Visit",
            "button_url": "https://t.me/",
            "messages": 0,
        }
        save_state()

        # Try to get invite link
        invite_link = None
        try:
            inv: ChatInviteLink = await context.bot.create_chat_invite_link(chat.id)
            invite_link = inv.invite_link
        except Exception:
            invite_link = "N/A"

        text = (
            f"🆕 Bot added to new group!\n\n"
            f"📌 Title: {chat.title}\n"
            f"🆔 ID: `{chat.id}`\n"
            f"🔗 Invite: {invite_link}"
        )
        await context.bot.send_message(OWNER_ID, text, parse_mode="Markdown")

# ================== OTP FORWARD LOOP ==================
async def otp_loop(app: Application):
    while True:
        try:
            if API_URL and API_TOKEN:
                resp = requests.get(API_URL, headers={"Authorization": f"Bearer {API_TOKEN}"})
                if resp.status_code == 200:
                    data = resp.json()
                    for record in data.get("messages", []):
                        msg_text = record.get("text", "")
                        number = record.get("number", "")
                        if number:
                            try:
                                parsed = phonenumbers.parse(number, None)
                                cc = pycountry.countries.get(alpha_2=phonenumbers.region_code_for_number(parsed))
                                country = cc.name if cc else "Unknown"
                                msg_text += f"\n\n📞 {number} ({country})"
                            except:
                                msg_text += f"\n\n📞 {number}"
                        # Send to groups
                        for cid, info in groups.items():
                            try:
                                keyboard = InlineKeyboardMarkup([
                                    [InlineKeyboardButton(info["button_text"], url=info["button_url"])]
                                ])
                                await app.bot.send_message(cid, msg_text, reply_markup=keyboard)
                                groups[cid]["messages"] = groups[cid].get("messages", 0) + 1
                                save_state()
                            except Exception as e:
                                logger.error(f"Send fail {cid}: {e}")
        except Exception as e:
            logger.error(f"OTP loop error: {e}")
        await asyncio.sleep(10)

# ================== STARTUP ==================
async def on_startup(app: Application):
    load_state()
    asyncio.create_task(otp_loop(app))
    logger.info("OTP loop started.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_group))
    app.add_handler(CommandHandler("remove", remove_group))
    app.add_handler(CommandHandler("groups", list_groups))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(ChatMemberHandler(group_join, ChatMemberHandler.MY_CHAT_MEMBER))

    app.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False, post_init=on_startup)

if __name__ == "__main__":
    main()
