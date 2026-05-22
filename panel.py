from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid
import time
import os
import random
import string
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)
CORS(app)

# ======================
# CONSTANTS & LOCAL MEMORY (RAM)
# ======================
TOKEN_EXPIRY = 5       # seconds for token expiry
COOLDOWN = 120         # anti-spam cooldown
KEY_LIMIT = 120        # seconds before same IP can generate another key

# Temporary storage sa RAM (Para sa tokens, ip_limit, at cooldowns)
db_cache = {
    "tokens": {},
    "ip_limit": {},
    "cooldowns": {}
}

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

# Koneksyon para sa permanenteng VIP Keys sa Supabase
def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable is missing sa Render!")
    return psycopg2.connect(DATABASE_URL)

# ======================
# CLEANUP
# ======================
def cleanup():
    now = time.time()
    for t in list(db_cache["tokens"].keys()):
        if now - db_cache["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db_cache["tokens"][t]
    for ip in list(db_cache["ip_limit"].keys()):
        if now - db_cache["ip_limit"][ip] > KEY_LIMIT:
            del db_cache["ip_limit"][ip]

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
# HOME ROUTE
# ======================
@app.route("/")
def home():
    return "KAZE SERVER ONLINE"

# ======================
# TOKEN ROUTE
# ======================
@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    source = request.args.get("src", "site")

    if source != "bot":
        if ip in db_cache["cooldowns"]:
            elapsed = now - db_cache["cooldowns"][ip]
            if elapsed < COOLDOWN:
                return jsonify({
                    "status":"cooldown",
                    "redirect":"https://kazehayamodz-main-page-90wu.onrender.com"
                })

    token_id = str(uuid.uuid4())
    db_cache["tokens"][token_id] = {"ip": ip, "time": now}

    return jsonify({
        "status":"success",
        "token": token_id
    })

# ======================
# GENERATE STANDARD KEY
# ======================
@app.route("/getkey")
def getkey():
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1")  # Default ay 1 device limit
    now = time.time()

    if not token_id or token_id not in db_cache["tokens"]:
        return jsonify({"status": "error", "message": "Token expired"}), 403

    token_data = db_cache["tokens"][token_id]
    ip = token_data["ip"]

    if ip in db_cache["ip_limit"]:
        wait = int(KEY_LIMIT - (now - db_cache["ip_limit"][ip]))
        if wait > 0:
            return jsonify({"status": "wait", "message": "Bypass detected!"}), 403

    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"
    key = prefix + ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
        """, (key, now + expiry_seconds, int(max_dev)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500

    db_cache["ip_limit"][ip] = now
    del db_cache["tokens"][token_id]

    return jsonify({
        "status": "success",
        "key": key,
        "expires_in": expiry_seconds,
        "max_devices": max_dev
    })
    
# ======================
# GENERATE CUSTOM KEY
# ======================
@app.route("/customkey")
def custom_key():
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1")  # Default ay 1 device limit
    now = time.time()

    if not custom_name:
        return jsonify({"status": "error", "message": "Custom key name is missing"}), 400

    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key name already exists!"}), 409

        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
        """, (key, now + expiry_seconds, int(max_dev)))
        conn.commit()
        cur.close()
        conn.close()
        
        send_telegram_alert(f"🎁 *Custom Key Created*\nKey: `{key}`\nDuration: `{duration}`\nMax Devices: `{max_dev}`")
        return jsonify({"status": "success", "key": key, "expires_in": expiry_seconds, "max_devices": max_dev})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ======================
# TIME FORMATTER HELPER (Para sa Telegram Notif)
# ======================
def format_remaining_time(seconds: int) -> str:
    if seconds <= 0:
        return "Expired"
    if seconds >= 900000000: # Para sa Lifetime keys
        return "Lifetime"
        
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    
    parts = []
    if days > 0: parts.append(f"{int(days)}d")
    if hours > 0: parts.append(f"{int(hours)}h")
    if minutes > 0: parts.append(f"{int(minutes)}m")
    
    # Kung mas mababa sa isang minuto ang natitira
    if not parts: return "Less than 1m"
        
    return " ".join(parts)

# ======================
# VERIFY KEY (UPDATED TELEGRAM NOTIF)
# ======================
@app.route("/verify")
def verify():
    cleanup()
    key = request.args.get("key")
    device = request.args.get("device")
    if not key or not device: return "invalid"

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM keys WHERE key_code = %s;", (key,))
    data = cur.fetchone()

    if not data:
        cur.close(); conn.close()
        return "invalid"

    if data["revoked"]:
        cur.close(); conn.close()
        send_telegram_alert(f"❌ *Key Revoked Attempt*\nKey: `{key}`\nDevice: `{device}`")
        return "revoked"

    if time.time() > data["expiry"]:
        cur.close(); conn.close()
        send_telegram_alert(f"❌ *Key Expired Attempt*\nKey: `{key}`\nDevice: `{device}`")
        return "expired"

    current_devices = data["device"].split(",") if data["device"] else []
    max_allowed = data.get("max_devices", 1)
    remaining_seconds = int(data["expiry"] - time.time())
    time_left_str = format_remaining_time(remaining_seconds)

    # Case 1: Ang device na ito ay naka-login na dati pa (Re-login)
    if device in current_devices:
        cur.close(); conn.close()
        device_index = current_devices.index(device) + 1
        
        # Pagandahin ang notif depende kung multi-device o single device
        counter_str = f" ({device_index}/{max_allowed})" if max_allowed > 1 else ""
        send_telegram_alert(f"✓ *Key Used{counter_str}*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{time_left_str}`")
        return "valid"

    # Case 2: May bakante pang slot para sa bagong device
    if len(current_devices) < max_allowed:
        current_devices.append(device)
        new_device_string = ",".join(current_devices)
        
        cur.execute("UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;", (new_device_string, time.time(), key))
        conn.commit()
        cur.close(); conn.close()
        
        counter_str = f" ({len(current_devices)}/{max_allowed})" if max_allowed > 1 else ""
        send_telegram_alert(f"✓ *Key Used{counter_str}*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{time_left_str}`")
        return "valid"

    # Case 3: Puno na ang slots (Device Mismatch / Locked)
    cur.close(); conn.close()
    send_telegram_alert(f"🔒 *Max Device Limit Reached*\nKey: `{key}`\nAttempt Device: `{device}`\nSlots: `{len(current_devices)}/{max_allowed}`")
    return "locked"

# ======================
# REVOKE KEY
# ======================
@app.route("/revoke")
def revoke():
    key = request.args.get("key")
    if not key: return jsonify({"status": "error"}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE keys SET revoked = TRUE WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close()
    conn.close()
    
    if count == 0: return jsonify({"status": "error"}), 404
    send_telegram_alert(f"🚫 *Key Revoked*\nKey: `{key}`")
    return jsonify({"status": "success"})

# ======================
# RESET DEVICE KEY
# ======================
@app.route("/reset")
def reset_device():
    key = request.args.get("key")
    if not key: return jsonify({"status": "error"}), 400
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close()
    conn.close()
    
    if count == 0: return jsonify({"status": "error"}), 404
    send_telegram_alert(f"🔄 *Key Device Reset*\nKey: `{key}`")
    return jsonify({"status": "success"})

# ======================
# LIST ACTIVE KEYS
# ======================
@app.route("/list")
def list_keys():
    now = time.time()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    result = [{"key": r["key_code"], "device": r["device"], "max": r["max_devices"]} for r in rows]
    return jsonify(result)

# ======================
# STATS
# ======================
@app.route("/stats")
def stats():
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
