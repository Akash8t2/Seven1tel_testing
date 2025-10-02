"""
OTP Bot with Web Admin Panel
Single-file example using Flask + background thread + optional MongoDB
Features:
 - Web admin UI (login with ADMIN_PASSWORD) to add/remove groups
 - Per-group button text + URL
 - Persist settings in MongoDB (if MONGO_URI set) or local JSON fallback
 - Background worker polls an external SMS API and forwards OTPs to configured groups

Notes:
 - For production, use MongoDB Atlas (set MONGO_URI) or another persistent DB. Heroku's filesystem is ephemeral.
 - Secure ADMIN_PASSWORD and BOT_TOKEN using environment variables.
"""

from flask import Flask, request, redirect, url_for, render_template_string, session, flash, jsonify
import os
import threading
import time
import requests
import re
import json
from datetime import datetime
try:
    from pymongo import MongoClient
    MONGO_AVAILABLE = True
except Exception:
    MONGO_AVAILABLE = False

# -----------------------
# Configuration (env)
# -----------------------
BOT_TOKEN = os.getenv('BOT_TOKEN') or ''
API_TOKEN = os.getenv('API_TOKEN') or ''
API_URL = os.getenv('API_URL') or 'http://147.135.212.197/crapi/s1t/viewstats'
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'adminpass')
MONGO_URI = os.getenv('MONGO_URI')  # if provided, will be used
POLL_INTERVAL = float(os.getenv('POLL_INTERVAL', '1'))

# JSON fallback file (not persistent on ephemeral hosts)
DATA_FILE = os.getenv('DATA_FILE', 'bot_data.json')

# Flask setup
app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET', 'supersecretkey')

# -----------------------
# Storage layer
# -----------------------
if MONGO_URI and MONGO_AVAILABLE:
    client = MongoClient(MONGO_URI)
    db = client.get_default_database()
    groups_col = db.get_collection('groups')
    meta_col = db.get_collection('meta')
    def add_group_to_db(group):
        groups_col.update_one({'chat_id': group['chat_id']}, {'$set': group}, upsert=True)
    def remove_group_from_db(chat_id):
        groups_col.delete_one({'chat_id': chat_id})
    def get_groups_from_db():
        return list(groups_col.find({}, {'_id': 0}))
    def save_meta(key, value):
        meta_col.update_one({'k': key}, {'$set': {'v': value}}, upsert=True)
    def load_meta(key, default=None):
        doc = meta_col.find_one({'k': key})
        return doc['v'] if doc else default
    storage = 'mongo'
else:
    # Fallback JSON storage
    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'w') as f:
            json.dump({'groups': [], 'meta': {}}, f)
    def _read_data():
        with open(DATA_FILE, 'r') as f:
            return json.load(f)
    def _write_data(d):
        with open(DATA_FILE, 'w') as f:
            json.dump(d, f, indent=2)
    def add_group_to_db(group):
        d = _read_data()
        groups = [g for g in d['groups'] if g['chat_id'] != group['chat_id']]
        groups.append(group)
        d['groups'] = groups
        _write_data(d)
    def remove_group_from_db(chat_id):
        d = _read_data()
        d['groups'] = [g for g in d['groups'] if g['chat_id'] != chat_id]
        _write_data(d)
    def get_groups_from_db():
        return _read_data().get('groups', [])
    def save_meta(key, value):
        d = _read_data()
        d.setdefault('meta', {})[key] = value
        _write_data(d)
    def load_meta(key, default=None):
        return _read_data().get('meta', {}).get(key, default)
    storage = 'json_fallback'

# -----------------------
# Helpers (from user's original script)
# -----------------------
last_msg_id = None

def extract_otp(message):
    message = message.replace("–", "-").replace("—", "-")
    possible_codes = re.findall(r'\d{3,4}[- ]?\d{3,4}', message)
    if possible_codes:
        return possible_codes[0].replace("-", "").replace(" ", "")
    fallback = re.search(r'\d{4,8}', message)
    return fallback.group(0) if fallback else "N/A"

def mask_number(number):
    return number[:3] + "***" + number[-5:] if len(number) >= 10 else number

# Format message (HTML)
def format_message(sms):
    number = sms.get("num", "")
    msg = sms.get("message", "")
    time_sent = sms.get("dt") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    otp = extract_otp(msg)
    masked = mask_number(number)

    return f"""<b> ✅ New OTP Received Successfully... </b>\n\n🕰️ <b>Time:</b> {time_sent}\n📞 <b>Number:</b> {masked}\n🔑 <b>OTP Code:</b> <code>{otp}</code>\n❤️ <b>Full Message:</b>\n<pre>{msg}</pre>\n"""

# -----------------------
# Telegram send helper
# -----------------------
def send_telegram_to_group(chat_id, text, button_text=None, button_url=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': chat_id,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    if button_text and button_url:
        payload['reply_markup'] = {
            'inline_keyboard': [[{'text': button_text, 'url': button_url}]]
        }
    try:
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code, r.text
    except Exception as e:
        return None, str(e)

# -----------------------
# Background worker: polls API and forwards to configured groups
# -----------------------

def fetch_latest_sms():
    try:
        res = requests.get(API_URL, params={'token': API_TOKEN, 'records': 1}, timeout=8)
        if res.status_code == 200:
            data = res.json()
            if data.get('status') == 'success':
                return data.get('data', [])[0]
    except Exception as e:
        print('API Error:', e)
    return None


def worker_loop():
    global last_msg_id
    print('Worker started, storage=', storage)
    while True:
        sms = fetch_latest_sms()
        if sms:
            msg_id = f"{sms.get('num')}_{sms.get('dt')}"
            msg_text = sms.get('message', '').lower()
            # basic filtering
            if msg_id != last_msg_id and any(k in msg_text for k in ['otp', 'code', 'verify', 'كود', 'رمز', 'password']):
                formatted = format_message(sms)
                groups = get_groups_from_db()
                for g in groups:
                    if not g.get('enabled', True):
                        continue
                    chat_id = g['chat_id']
                    btn_text = g.get('button_text')
                    btn_url = g.get('button_url')
                    status, resp = send_telegram_to_group(chat_id, formatted, btn_text, btn_url)
                    print('Sent to', chat_id, 'status=', status)
                last_msg_id = msg_id
        time.sleep(POLL_INTERVAL)

# Start worker in a separate thread
worker = threading.Thread(target=worker_loop, daemon=True)
worker.start()

# -----------------------
# Admin web UI (simple)
# -----------------------

ADMIN_TEMPLATE = '''
<!doctype html>
<title>OTP Bot Admin</title>
<h2>OTP Bot Admin Panel</h2>
{% with messages = get_flashed_messages() %}
  {% if messages %}
    <ul>
    {% for message in messages %}
      <li>{{ message }}</li>
    {% endfor %}
    </ul>
  {% endif %}
{% endwith %}

<p><a href="{{ url_for('logout') }}">Logout</a></p>

<h3>Add / Update Group</h3>
<form method="post" action="{{ url_for('add_group') }}">
  <label>Chat ID: <input name="chat_id"></label><br>
  <label>Button Text: <input name="button_text"></label><br>
  <label>Button URL: <input name="button_url"></label><br>
  <label>Enabled: <input type="checkbox" name="enabled" checked></label><br>
  <button type="submit">Add / Update</button>
</form>

<h3>Configured Groups</h3>
<ul>
{% for g in groups %}
  <li>
    <b>{{ g.chat_id }}</b> — {{ g.button_text or 'No button' }} — <a href="#" onclick="fetch('/admin/remove/{{ g.chat_id }}', {method:'POST'}).then(()=>location.reload())">Remove</a>
    <form method="post" action="{{ url_for('edit_group', chat_id=g.chat_id) }}">
      <input type="hidden" name="chat_id" value="{{ g.chat_id }}">
      <label>Button Text: <input name="button_text" value="{{ g.button_text or '' }}"></label>
      <label>Button URL: <input name="button_url" value="{{ g.button_url or '' }}"></label>
      <label>Enabled: <input type="checkbox" name="enabled" {% if g.enabled %}checked{% endif %}></label>
      <button type="submit">Save</button>
    </form>
  </li>
{% endfor %}
</ul>

'''

LOGIN_TEMPLATE = '''
<!doctype html>
<title>Login</title>
<h2>Login</h2>
<form method="post">
  <label>Password: <input type="password" name="password"></label>
  <button type="submit">Login</button>
</form>
'''

@app.route('/')
def index():
    if not session.get('admin'):
        return redirect(url_for('login'))
    groups = get_groups_from_db()
    return render_template_string(ADMIN_TEMPLATE, groups=groups)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pw = request.form.get('password', '')
        if pw == ADMIN_PASSWORD:
            session['admin'] = True
            flash('Logged in')
            return redirect(url_for('index'))
        flash('Wrong password')
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.pop('admin', None)
    return redirect(url_for('login'))

@app.route('/admin/add', methods=['POST'])
def add_group():
    if not session.get('admin'):
        return redirect(url_for('login'))
    chat_id = request.form.get('chat_id')
    button_text = request.form.get('button_text') or None
    button_url = request.form.get('button_url') or None
    enabled = bool(request.form.get('enabled'))
    if not chat_id:
        flash('chat_id required')
        return redirect(url_for('index'))
    group = {'chat_id': chat_id, 'button_text': button_text, 'button_url': button_url, 'enabled': enabled}
    add_group_to_db(group)
    flash('Group saved')
    return redirect(url_for('index'))

@app.route('/admin/edit/<chat_id>', methods=['POST'])
def edit_group(chat_id):
    if not session.get('admin'):
        return redirect(url_for('login'))
    button_text = request.form.get('button_text') or None
    button_url = request.form.get('button_url') or None
    enabled = bool(request.form.get('enabled'))
    group = {'chat_id': chat_id, 'button_text': button_text, 'button_url': button_url, 'enabled': enabled}
    add_group_to_db(group)
    flash('Group updated')
    return redirect(url_for('index'))

@app.route('/admin/remove/<chat_id>', methods=['POST'])
def remove_group(chat_id):
    if not session.get('admin'):
        return redirect(url_for('login'))
    remove_group_from_db(chat_id)
    return ('', 204)

# Small API for clients (optional)
@app.route('/api/groups')
def api_groups():
    # simple read-only
    return jsonify(get_groups_from_db())

# -----------------------
# Run app
# -----------------------
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
