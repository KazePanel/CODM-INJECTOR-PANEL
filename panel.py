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

TOKEN_EXPIRY = 5
COOLDOWN = 120
KEY_LIMIT = 120

db_cache = {
    "tokens": {},
    "ip_limit": {},
    "cooldowns": {}
}

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL variable is missing!")
    return psycopg2.connect(DATABASE_URL)

def cleanup():
    now = time.time()
    for t in list(db_cache["tokens"].keys()):
        if now - db_cache["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db_cache["tokens"][t]
    for ip in list(db_cache["ip_limit"].keys()):
        if now - db_cache["ip_limit"][ip] > KEY_LIMIT:
            del db_cache["ip_limit"][ip]

def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not OWNER_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try: requests.post(url, data={"chat_id": OWNER_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def convert_duration(duration: str):
    duration = duration.lower()
    if duration.endswith("m"): return int(duration[:-1]) * 60
    if duration.endswith("h"): return int(duration[:-1]) * 3600
    if duration.endswith("d"): return int(duration[:-1]) * 86400
    if duration == "lifetime": return 999999999
    return 1800

@app.route("/")
def home(): return "KAZE SERVER ONLINE"

@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    source = request.args.get("src", "site")

    if source != "bot" and ip in db_cache["cooldowns"]:
        if now - db_cache["cooldowns"][ip] < COOLDOWN:
            return jsonify({"status":"cooldown", "redirect":"https://kazehayamodz-main-page-90wu.onrender.com"})

    token_id = str(uuid.uuid4())
    db_cache["tokens"][token_id] = {"ip": ip, "time": now}
    return jsonify({"status":"success", "token": token_id})

# ======================
# GENERATE STANDARD KEY (May "&max=" Control na)
# ======================
@app.route("/getkey")
def getkey():
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1") # Default ay 1 device
    now = time.time()

    if not token_id or token_id not in db_cache["tokens"]:
        return jsonify({"status": "error", "message": "Token expired"}), 403

    ip = db_cache["tokens"][token_id]["ip"]
    if ip in db_cache["ip_limit"] and int(KEY_LIMIT - (now - db_cache["ip_limit"][ip])) > 0:
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
        cur.close() conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    db_cache["ip_limit"][ip] = now
    del db_cache["tokens"][token_id]
    return jsonify({"status": "success", "key": key, "expires_in": expiry_seconds, "max_devices": max_dev})

# ======================
# GENERATE CUSTOM KEY (May "&max=" Control na)
# ======================
@app.route("/customkey")
def custom_key():
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "12h")
    max_dev = request.args.get("max", "1") # Default ay 1 device
    now = time.time()

    if not custom_name: return jsonify({"status": "error", "message": "Missing name"}), 400
    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if cur.fetchone():
            cur.close(); conn.close()
            return jsonify({"status": "error", "message": "Key exists!"}), 409

        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
        """, (key, now + expiry_seconds, int(max_dev)))
        conn.commit()
        cur.close(); conn.close()
        send_telegram_alert(f"🎁 *Custom Key Created*\nKey: `{key}`\nDevices: `{max_dev}`")
        return jsonify({"status": "success", "key": key, "max_devices": max_dev})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

# ======================
# VERIFY KEY (MULTI-DEVICE LOGIC)
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
        return "revoked"

    if time.time() > data["expiry"]:
        cur.close(); conn.close()
        return "expired"

    # I-load ang mga rehistradong devices (naka-save bilang comma-separated text)
    current_devices = data["device"].split(",") if data["device"] else []
    max_allowed = data.get("max_devices", 1)

    # Case 1: Ang device na ito ay naka-login na dati pa
    if device in current_devices:
        cur.close(); conn.close()
        return "valid"

    # Case 2: May bakante pang slot para sa bagong device
    if len(current_devices) < max_allowed:
        current_devices.append(device)
        new_device_string = ",".join(current_devices)
        
        cur.execute("UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;", (new_device_string, time.time(), key))
        conn.commit()
        cur.close(); conn.close()
        
        send_telegram_alert(f"📱 *New Device Linked ({len(current_devices)}/{max_allowed})*\nKey: `{key}`\nDevice: `{device}`")
        return "valid"

    # Case 3: Puno na ang slots (Device Mismatch / Locked)
    cur.close(); conn.close()
    send_telegram_alert(f"🔒 *Max Device Limit Reached*\nKey: `{key}`\nAttempt Device: `{device}`")
    return "locked"

@app.route("/revoke")
def revoke():
    key = request.args.get("key")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE keys SET revoked = TRUE WHERE key_code = %s;", (key,))
    conn.commit()
    count = cur.rowcount
    cur.close(); conn.close()
    if count == 0: return jsonify({"status": "error"}), 404
    return jsonify({"status": "success"})

@app.route("/reset")
def reset_device():
    key = request.args.get("key")
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
    conn.commit()
    cur.close(); conn.close()
    return jsonify({"status": "success"})

@app.route("/list")
def list_keys():
    now = time.time()
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    result = [{"key": r["key_code"], "device": r["device"], "max": r["max_devices"]} for r in rows]
    return jsonify(result)

@app.route("/stats")
def stats():
    now = time.time()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM keys;")
    total = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
    active = cur.fetchone()[0]
    cur.close(); conn.close()
    return jsonify({"total_keys": total, "active_keys": active, "expired_keys": total - active})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
