import requests
import os
import time
import re
from datetime import datetime
import phonenumbers
import pycountry

# === CONFIG ===
API_TOKEN = os.getenv("API_TOKEN")
API_URL = "http://147.135.212.197/crapi/s1t/viewstats"
BOT_TOKEN = os.getenv("BOT_TOKEN")

# 🔹 Multiple Chat IDs (comma separated string ya direct list)
CHAT_IDS = os.getenv("CHAT_IDS", "123456789,987654321").split(",")

MAX_RECORDS = 1
last_msg_id = None

# ✅ OTP extractor
def extract_otp(message):
    message = message.replace("–", "-").replace("—", "-")
    possible_codes = re.findall(r'\d{3,4}[- ]?\d{3,4}', message)
    if possible_codes:
        return possible_codes[0].replace("-", "").replace(" ", "")
    fallback = re.search(r'\d{4,8}', message)
    return fallback.group(0) if fallback else "N/A"

# ✅ Country detector
def detect_country_flag(number):
    try:
        parsed = phonenumbers.parse("+" + number, None)
        region = phonenumbers.region_code_for_number(parsed)
        country = pycountry.countries.get(alpha_2=region).name
        flag = ''.join([chr(ord(c) + 127397) for c in region.upper()])
        return country, flag
    except:
        return "Unknown", "🌍"

# ✅ Service detector
def detect_service(msg):
    services = {
        "whatsapp": "WhatsApp",
        "telegram": "Telegram",
        "facebook": "Facebook",
        "instagram": "Instagram",
        "gmail": "Gmail",
        "google": "Google",
        "imo": "IMO",
        "signal": "Signal",
        "twitter": "Twitter",
        "microsoft": "Microsoft",
        "yahoo": "Yahoo",
        "tiktok": "TikTok"
    }
    msg = msg.lower()
    for key in services:
        if key in msg:
            return services[key]
    return "Unknown"

# ✅ Number mask
def mask_number(number):
    return number[:3] + "***" + number[-5:] if len(number) >= 10 else number

# ✅ Message format
def format_message(sms):
    number = sms.get("num", "")
    msg = sms.get("message", "")
    time_sent = sms.get("dt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    country, flag = detect_country_flag(number)
    otp = extract_otp(msg)
    service = detect_service(msg)
    masked = mask_number(number)

    return f"""<b> ✅ New OTP Received Successfully... </b>

🕰️ <b>Time:</b> {time_sent}
📞 <b>Number:</b> {masked}
🔑 <b>OTP Code:</b> <code>{otp}</code>
🌍 <b>Country:</b> {flag} {country}
📱 <b>Service:</b> {service}
❤️ <b>Full Message:</b>
<pre>{msg}</pre>

👨‍💻 <b>:POWERED BY</b>
<a href="https://t.me/Fahim_Fsm"> Rohan Fahim 🌼🍀</a>
"""

# ✅ Send to Telegram (multiple chat ids)
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for chat_id in CHAT_IDS:
        chat_id = chat_id.strip()
        if not chat_id:
            continue
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {
                "inline_keyboard": [
                    [
                        {"text": "📞 ALL NUMBER 📞", "url": "https://t.me/+hJ8Ms2Dr3Zw4MTQ1"},
                    ]
                ]
            }
        }
        r = requests.post(url, json=data)
        print(f"📤 Sent to {chat_id}:", r.status_code, r.text)

# ✅ Fetch OTP from API
def fetch_latest_sms():
    try:
        res = requests.get(API_URL, params={"token": API_TOKEN, "records": MAX_RECORDS})
        if res.status_code == 200:
            data = res.json()
            if data.get("status") == "success":
                return data.get("data", [])[0]
    except Exception as e:
        print("❌ API Error:", e)
    return None

# ✅ Main loop
def main():
    global last_msg_id
    print("🚀 OTP BOT LIVE...")
    while True:
        sms = fetch_latest_sms()
        if sms:
            msg_id = f"{sms.get('num')}_{sms.get('dt')}"
            msg_text = sms.get("message", "").lower()
            if msg_id != last_msg_id and any(k in msg_text for k in ["otp", "code", "verify", "كود", "رمز", "password"]):
                formatted = format_message(sms)
                send_telegram(formatted)
                last_msg_id = msg_id
                print("✅ OTP Sent:", formatted.splitlines()[2])
        time.sleep(1)

if __name__ == "__main__":
    main()
