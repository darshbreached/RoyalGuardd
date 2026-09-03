"""
Darsh Industries - tenant registration site.

Standalone Flask app, separate from the main Royal Guard website. Lets a
regiment server owner submit their own Discord bot token to be hosted as a
tenant of Royal Guard Services. Submissions are auto-approved: they're
written directly into the shared `tenants` collection (same one
tenant_manager.py already reads from) as status="active".

NOTE: this site only registers a tenant into MongoDB. It does not itself
start a bot process. tenant_manager.py (currently paused per the Royal
Guard tenant retirement) still has to actually be running for a registered
tenant's bot to come online.
"""

import os
import re
import time
from datetime import datetime

from flask import Flask, render_template, request
from pymongo import MongoClient
from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "royalguard")
TENANT_ENCRYPTION_KEY = os.getenv("TENANT_ENCRYPTION_KEY")

if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI is not set in the environment (.env)")
if not TENANT_ENCRYPTION_KEY:
    raise RuntimeError("TENANT_ENCRYPTION_KEY is not set in the environment (.env)")

client = MongoClient(MONGODB_URI)
db = client[MONGODB_DB_NAME]
tenants = db["tenants"]

fernet = Fernet(TENANT_ENCRYPTION_KEY.encode())

DISCORD_ID_RE = re.compile(r"^\d{15,20}$")


def looks_like_bot_token(token: str) -> bool:
    """Loose sanity check only - not a real validation against Discord's API.
    Bot tokens are long, dot-delimited base64-ish strings. This just catches
    obvious mistakes (pasting the wrong thing, empty field, etc)."""
    token = token.strip()
    return len(token) >= 50 and token.count(".") == 2


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", success=None, error=None, form={})


@app.route("/register", methods=["POST"])
def register():
    server_name = request.form.get("server_name", "").strip()
    owner_discord_id = request.form.get("owner_discord_id", "").strip()
    bot_token = request.form.get("bot_token", "").strip()
    agree = request.form.get("agree")

    form_echo = {"server_name": server_name, "owner_discord_id": owner_discord_id}

    if not server_name or not owner_discord_id or not bot_token:
        return render_template("index.html", success=None,
                                error="Every field is required.", form=form_echo)

    if not DISCORD_ID_RE.match(owner_discord_id):
        return render_template("index.html", success=None,
                                error="That doesn't look like a valid Discord user ID (15-20 digits).",
                                form=form_echo)

    if not looks_like_bot_token(bot_token):
        return render_template("index.html", success=None,
                                error="That doesn't look like a valid Discord bot token. Double-check you copied the full token from the Developer Portal.",
                                form=form_echo)

    if not agree:
        return render_template("index.html", success=None,
                                error="You need to confirm you own this bot token and understand it will be hosted by Darsh Industries.",
                                form=form_echo)

    encrypted_token = fernet.encrypt(bot_token.encode()).decode()

    doc = {
        "owner_discord_id": owner_discord_id,
        "encrypted_token": encrypted_token,
        "bot_name": server_name,
        "status": "active",
        "last_error": None,
        "created_at": time.time(),
        "registered_via": "darsh-industries-site",
    }
    result = tenants.insert_one(doc)

    return render_template("index.html", success=str(result.inserted_id), error=None, form={})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5050)))
