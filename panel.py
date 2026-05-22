from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid
import time
import json
import os
import random
import string
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

# ======================
# CONSTANTS
# ======================
TOKEN_EXPIRY = 5       # seconds for token expiry
COOLDOWN = 120         # anti-spam cooldown
KEY_LIMIT = 120        # seconds before same IP can generate another key

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing sa Render!")
    return psycopg2.connect(DATABASE_URL)

# ======================
# SYSTEM CACHE (Para sa tokens/cooldowns gamit ang Supabase)
# ======================
def get_cache(cache_name):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT cache_value FROM system_cache WHERE cache_key = %s;", (cache_name,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            return json.loads(row[0])
    except:
        pass
    return {}

def save_cache(cache_name, data):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO system_cache (cache_key, cache_value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (cache_key) 
            DO UPDATE SET cache_value = EXCLUDED.cache_value, updated_at = CURRENT_TIMESTAMP;
        """, (cache_name, json.dumps(data)))
        conn.commit()
        cur.close()
        conn.close()
    except:
        pass

# ======================
# CLEANUP
# ======================
def cleanup():
    now = time.time()
    tokens = get_cache("tokens")
    ip_limit = get_cache("ip_limit")
    
    tokens_changed = False
    for t in list(tokens.keys()):
        if now - tokens[t]["time"] > TOKEN_EXPIRY:
            del tokens[t]
            tokens_changed = True
            
    ip_limit_changed = False
    for ip in list(ip_limit.keys()):
        if now - ip_limit[ip] > KEY_LIMIT:
            del ip_limit[ip]
            ip_limit_changed = True
            
    if tokens_changed: save_cache("tokens", tokens)
    if ip_limit_changed: save_cache("ip_limit", ip_limit)

# ======================
# TELEGRAM ALERT
# ======================
def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not OWNER_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload, timeout=5)
    except:
        pass

# ======================
# DURATION CONVERTER
# ======================
def convert_duration(duration: str):
    duration = duration.lower()
    if duration.endswith("m"): return int(duration[:-1]) * 60
    if duration.endswith("h"): return int(duration[:-1]) * 3600
    if duration.endswith("d"): return int(duration[:-1]) * 86400
    if duration == "lifetime": return 999999999
    return 1800

# ======================
# API ENDPOINTS
# ======================
@app.route("/")
def home():
    return "KAZE SERVER ONLINE (SUPABASE STABLE)"

@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    source = request.args.get("src", "site")

    cooldowns = get_cache("cooldowns")
    tokens = get_cache("tokens")

    if source != "bot" and ip in cooldowns:
        if now - cooldowns[ip] < COOLDOWN:
            return jsonify({
                "status":"cooldown",
                "redirect":"https://kazehayamodz-main-page-90wu.onrender.com"
            })

    token_id = str(uuid.uuid4())
    tokens[token_id] = {"ip": ip, "time": now}
    save_cache("tokens", tokens)
    
    return jsonify({"status":"success", "token": token_id})

@app.route("/getkey")
def getkey():
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "12h")
    now = time.time()

    if not token_id:
        return jsonify({"status": "error", "message": "Missing token"}), 403

    tokens = get_cache("tokens")
    ip_limit = get_cache("ip_limit")

    if token_id not in tokens:
        return jsonify({"status": "error", "message": "Token expired. Please generate again."}), 403

    ip = tokens[token_id]["ip"]

    if ip in ip_limit and int(KEY_LIMIT - (now - ip_limit[ip])) > 0:
        return jsonify({"status": "wait", "message": "Bypass detected! Try again in main page"}), 403

    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"
    key = prefix + ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    # I-save direkta sa Supabase Cloud Tables
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO keys (key_code, expiry, device, revoked, login_time)
        VALUES (%s, %s, NULL, FALSE, NULL);
    """, (key, now + expiry_seconds))
    conn.commit()
    cur.close()
    conn.close()

    ip_limit[ip] = now
    del tokens[token_id]
    save_cache("tokens", tokens)
    save_cache("ip_limit", ip_limit)

    return jsonify({"status": "success", "key": key, "expires_in": expiry_seconds})
    
@app.route("/verify")
def verify():
    cleanup()
    key = request.args.get("key")
    device = request.args.get("device")
    
    if not key or not device: 
        return "invalid"

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM keys WHERE key_code = %s;", (key,))
    data = cur.fetchone()

    if not data:
        cur.close()
        conn.close()
        return "invalid"

    if data["revoked"]:
        cur.close()
        conn.close()
        send_telegram_alert(f"*Key Revoked*\nKey: `{key}`\nDevice: `{device}`")
        return "revoked"

    if time.time() > data["expiry"]:
        cur.close()
        conn.close()
        send_telegram_alert(f"*Key Expired*\nKey: `{key}`\nDevice: `{device}`")
        return "expired"

    if data["device"] is None:
        cur.execute("UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;", (device, time.time(), key))
        conn.commit()
        cur.close()
        conn.close()
        remaining = int(data["expiry"] - time.time())
        send_telegram_alert(f"*Key Used*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{remaining}s`")
        return "valid"

    if data["device"] == device:
        cur.close()
        conn.close()
        remaining = int(data["expiry"] - time.time())
        send_telegram_alert(f"*Key Used*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{remaining}s`")
        return "valid"

    cur.close()
    conn.close()
    send_telegram_alert(f"*Key Locked - Device Mismatch*\nKey: `{key}`\nDevice Attempt: `{device}`\nAssigned Device: `{data['device']}`")
    return "locked"

@app.route("/revoke")
def revoke():
    key = request.args.get("key")
    if not key: 
        return jsonify({"status": "error", "message": "Key missing"}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE keys SET revoked = TRUE WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close()
    conn.close()
    
    if count == 0:
        return jsonify({"status": "error", "message": "Key not found"}), 404
        
    send_telegram_alert(f"🚫 *Key Revoked*\nKey: `{key}`")
    return jsonify({"status": "success", "message": f"{key} revoked"})

@app.route("/list")
def list_keys():
    cleanup()
    now = time.time()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT key_code, device, expiry FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = [{"key": r["key_code"], "device": r["device"], "expire_in": int(r["expiry"] - now)} for r in rows]
    return jsonify(result)

@app.route("/stats")
def stats():
    cleanup()
    now = time.time()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM keys;")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
    active = cur.fetchone()[0]
    cur.close()
    conn.close()
    
    return jsonify({"total_keys": total, "active_keys": active, "expired_keys": total - active})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
