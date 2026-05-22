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

# Eto ang koneksyon para sa permanenteng VIP Keys
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
# HOME
# ======================
@app.route("/")
def home():
    return "KAZE SERVER ONLINE"

# ======================
# TOKEN
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
# GENERATE KEY
# ======================
@app.route("/getkey")
def getkey():
    token_id = request.args.get("token")
    source = request.args.get("src", "site")
    duration = request.args.get("duration", "12h")
    now = time.time()

    if not token_id:
        return jsonify({"status": "error", "message": "Missing token"}), 403

    if token_id not in db_cache["tokens"]:
        return jsonify({"status": "error", "message": "Token expired. Please generate again."}), 403

    token_data = db_cache["tokens"][token_id]
    ip = token_data["ip"]

    if ip in db_cache["ip_limit"]:
        wait = int(KEY_LIMIT - (now - db_cache["ip_limit"][ip]))
        if wait > 0:
            return jsonify({"status": "wait", "message": "Bypass detected! Try again in main page"}), 403

    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"
    key = prefix + ''.join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    # 💾 DIREKTANG PAG-SAVE SA SUPABASE
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time)
            VALUES (%s, %s, NULL, FALSE, NULL);
        """, (key, now + expiry_seconds))
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
        "expires_in": expiry_seconds
    })
    
# ======================
# VERIFY KEY
# ======================
@app.route("/verify")
def verify():
    cleanup()
    key = request.args.get("key")
    device = request.args.get("device")
    
    if not key or not device: 
        return "invalid"

    # 🔍 PAG-BASA MULA SA SUPABASE
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

# ======================
# REVOKE KEY
# ======================
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

# ======================
# LIST ACTIVE KEYS
# ======================
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

# ======================
# STATS
# ======================
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
    
# ======================
# RESET DEVICE KEY
# ======================
@app.route("/reset")
def reset_device():
    key = request.args.get("key")
    if not key: 
        return jsonify({"status": "error", "message": "Key missing"}), 400
    
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Gagawing NULL ang device para pwedeng gamitin sa ibang cellphone
        cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
        conn.commit()
        count = cur.rowcount
        cur.close()
        conn.close()
        
        if count == 0:
            return jsonify({"status": "error", "message": "Key not found"}), 404
            
        send_telegram_alert(f"🔄 *Key Device Reset*\nKey: `{key}`")
        return jsonify({"status": "success", "message": f"Device reset successful for {key}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ======================
# GENERATE CUSTOM KEY
# ======================
@app.route("/customkey")
def custom_key():
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "12h")
    now = time.time()

    if not custom_name:
        return jsonify({"status": "error", "message": "Custom key name is missing"}), 400

    # Linisin ang input (alisin ang mga spaces para hindi magka-error sa URL)
    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # Check muna kung may kaparehas na susing buhay pa sa database
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        exist = cur.fetchone()
        
        if exist:
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key name already exists!"}), 409

        # I-save sa Supabase
        cur.execute("""
            INSERT INTO keys (key_code, expiry, device, revoked, login_time)
            VALUES (%s, %s, NULL, FALSE, NULL);
        """, (key, now + expiry_seconds))
        conn.commit()
        cur.close()
        conn.close()
        
        send_telegram_alert(f"🎁 *Custom Key Created*\nKey: `{key}`\nDuration: `{duration}`")
        return jsonify({"status": "success", "key": key, "expires_in": expiry_seconds})
        
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
        
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
