"""
Orange Carrier Telegram Bot
===========================

🚀 A Python bot that logs into **multiple Orange Carrier accounts**, fetches
**CDR records**, and sends them directly to a Telegram group/channel.

-----------------------------------
✨ Features
-----------------------------------
- ✅ Multi-account support (parallel login & CDR fetch)
- ✅ Sends new call records (CLI, To, Time, Duration, Type) to Telegram
- ✅ Prevents duplicate messages
- ✅ `/start` command support
- ✅ Hourly heartbeat message ("Bot active hai...")
- ✅ Heroku-ready (Procfile, runtime.txt, app.json included)

-----------------------------------
🛠 Deployment
-----------------------------------
1. Clone Repo:
    git clone https://github.com/Akash8t2/Orangecarrier.git
    cd Orangecarrier

2. Set Environment Variables:
   - BOT_TOKEN → Your Telegram Bot Token
   - CHAT_ID   → Telegram Group/Chat ID (e.g., -100123456789)
   - ACCOUNTS  → JSON list of Orange Carrier accounts

   Example ACCOUNTS:
   [
     {"email": "user1@example.com", "password": "pass1"},
     {"email": "user2@example.com", "password": "pass2"}
   ]

3. Deploy to Heroku:
   Use the Deploy Button:

   <h2 align="center">🚀 Deploy to Heroku</h2>

<p align="center">
  <a href="https://heroku.com/deploy?template=https://github.com/Akash8t2/Seven1tel_testing">
    <img src="https://img.shields.io/badge/Deploy%20On%20Heroku-430098?style=for-the-badge&logo=heroku&logoColor=white" width="270" height="60"/>
  </a>
</p>

<p align="center">
  Click the button above to instantly deploy this bot to <b>Heroku</b> and get it running in minutes!
</p>

-----------------------------------
📂 Project Structure
-----------------------------------
Orangecarrier/
│── orange_bot.py       # Main bot script
│── requirements.txt    # Python dependencies
│── Procfile            # Heroku process definition
│── runtime.txt         # Python runtime version
│── app.json            # Heroku deploy config

-----------------------------------
⚡ Tech Stack
-----------------------------------
- Python 3.10+
- httpx (HTTP client)
- BeautifulSoup4 (HTML parsing)
- python-telegram-bot (Telegram API)

-----------------------------------
📬 Contact
-----------------------------------
👤 Author: Akash
💬 Telegram: @botcasx
📦 GitHub:  https://github.com/Akash8t2

-----------------------------------
📜 License
-----------------------------------
MIT License © 2025 Akash
"""
