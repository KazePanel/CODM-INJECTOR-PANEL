from flask import Flask, request, jsonify
from flask_cors import CORS
import uuid
import time
import json
import os
import random
import string
import requests  # telegram alerts

app = Flask(__name__)
CORS(app)

# ======================
# CONSTANTS
# ======================
TOKEN_EXPIRY = 5       # seconds for token expiry
COOLDOWN = 120            # anti-spam cooldown
KEY_LIMIT = 120         # seconds before same IP can generate another key
DATA_FILE = "database.json"

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")  # int chat_id of owner

# ======================
# LOAD DB
# ======================
if os.path.exists(DATA_FILE):
    with open(DATA_FILE, "r") as f:
        db = json.load(f)
else:
    db = {
        "keys": {},
        "tokens": {},
        "ip_limit": {},
        "cooldowns": {}
    }

def save_db():
    with open(DATA_FILE, "w") as f:
        json.dump(db, f, indent=4)

# ======================
# CLEANUP
# ======================
def cleanup():
    now = time.time()
    for t in list(db["tokens"].keys()):
        if now - db["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db["tokens"][t]
    for ip in list(db["ip_limit"].keys()):
        if now - db["ip_limit"][ip] > KEY_LIMIT:
            del db["ip_limit"][ip]

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
    if duration.endswith("m"):
        return int(duration[:-1]) * 60
    if duration.endswith("h"):
        return int(duration[:-1]) * 3600
    if duration.endswith("d"):
        return int(duration[:-1]) * 86400
    if duration == "lifetime":
        return 999999999
    return 1800  # default 30 minutes

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

    # CHECK COOLDOWN ONLY IF IP ALREADY HAS ONE
    if source != "bot":
        if ip in db["cooldowns"]:
            elapsed = now - db["cooldowns"][ip]
            if elapsed < COOLDOWN:
                return jsonify({
                    "status":"cooldown",
                    "redirect":"https://kazehayamodz-main-page-pgp5.onrender.com"
                })

    # GENERATE TOKEN
    token_id = str(uuid.uuid4())
    db["tokens"][token_id] = {"ip": ip, "time": now}

    save_db()
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

    # â— STRICT TOKEN CHECK
    if not token_id:
        return jsonify({
            "status": "error",
            "message": "Missing token"
        }), 403

    if token_id not in db["tokens"]:
        return jsonify({
            "status": "error",
            "message": "Token expired. Please generate again."
        }), 403

    token_data = db["tokens"][token_id]
    ip = token_data["ip"]

    # ðŸ”’ Anti spam check
    if ip in db["ip_limit"]:
        wait = int(KEY_LIMIT - (now - db["ip_limit"][ip]))
        if wait > 0:
            return jsonify({
                "status": "wait",
                "message": f"Bypass detected! Try again in main page"
            }), 403

    # ðŸ”‘ KEY PREFIX
    prefix = "Kaze-" if source == "bot" else "KazeFreeKey-"

    key = prefix + ''.join(
        random.choices(string.ascii_letters + string.digits, k=12)
    )

    expiry_seconds = convert_duration(duration)

    db["keys"][key] = {
        "expiry": now + expiry_seconds,
        "device": None,
        "revoked": False,
        "login_time": None
    }

    # set IP cooldown
    db["ip_limit"][ip] = now

    # remove used token
    del db["tokens"][token_id]

    save_db()

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
    if not key or key not in db["keys"]:
        return "invalid"
    data = db["keys"][key]
    if data.get("revoked"):
        send_telegram_alert(f"*Key Revoked*\nKey: `{key}`\nDevice: `{device}`")
        return "revoked"
    if time.time() > data["expiry"]:
        send_telegram_alert(f"*Key Expired*\nKey: `{key}`\nDevice: `{device}`")
        return "expired"
    if data["device"] is None:
        data["device"] = device
        data["login_time"] = time.time()
        save_db()
        remaining = int(data["expiry"] - time.time())
        send_telegram_alert(f"*Key Used*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{remaining}s`")
        return "valid"
    if data["device"] == device:
        remaining = int(data["expiry"] - time.time())
        send_telegram_alert(f"*Key Used*\nKey: `{key}`\nDevice: `{device}`\nExpires in: `{remaining}s`")
        return "valid"
    send_telegram_alert(f"*Key Locked - Device Mismatch*\nKey: `{key}`\nDevice Attempt: `{device}`\nAssigned Device: `{data['device']}`")
    return "locked"

# ======================
# REVOKE KEY
# ======================
@app.route("/revoke")
def revoke():
    key = request.args.get("key")
    if not key or key not in db["keys"]:
        return jsonify({"status": "error", "message": "Key not found"}), 404
    db["keys"][key]["revoked"] = True
    save_db()
    send_telegram_alert(f" *Key Revoked*\nKey: `{key}`")
    return jsonify({"status": "success", "message": f"{key} revoked"})

# ======================
# LIST ACTIVE KEYS
# ======================
@app.route("/list")
def list_keys():
    cleanup()
    result = []
    for key, data in db["keys"].items():
        if data.get("revoked"):
            continue
        if time.time() > data["expiry"]:
            continue
        result.append({
            "key": key,
            "device": data["device"],
            "expire_in": int(data["expiry"] - time.time())
        })
    return jsonify(result)

# ======================
# STATS
# ======================
@app.route("/stats")
def stats():
    cleanup()
    total = len(db["keys"])
    active = len([k for k in db["keys"] if not db["keys"][k].get("revoked") and time.time() < db["keys"][k]["expiry"]])
    expired = total - active
    return jsonify({
        "total_keys": total,
        "active_keys": active,
        "expired_keys": expired
    })

# ======================
# RUN SERVER
# ======================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
