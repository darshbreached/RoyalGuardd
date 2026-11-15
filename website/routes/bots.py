"""
website/routes/bots.py
"My bots": logged-in dashboard users register their own bot token. Writes to the
same `tenants` collection tenant_manager.py already reads (status="active").
The token is validated against Discord, encrypted, and never shown again.
"""
import os
import time
import logging

import requests
from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, render_template, request, jsonify, redirect

from website.routes.dashboard import current_user, _db, DEMO
from website.routes.site import invite_url

log = logging.getLogger("RoyalGuard")
bots_bp = Blueprint("bots", __name__, url_prefix="/dashboard/bots")

MAX_BOTS = int(os.getenv("MAX_TENANT_BOTS", "3"))
API = "https://discord.com/api/v10"
MEMBERS_INTENT = (1 << 14) | (1 << 15)
CONTENT_INTENT = (1 << 18) | (1 << 19)
_DEMO_BOTS = []


def bot_invite(bot_id):
    if not bot_id:
        return ""
    return f"https://discord.com/oauth2/authorize?client_id={bot_id}&permissions=8&scope=bot%20applications.commands"


def list_bots(uid):
    if DEMO:
        return list(_DEMO_BOTS)
    out = []
    for d in _db["tenants"].find({"owner_discord_id": uid}).sort("created_at", -1):
        created = d.get("created_at")
        out.append({
            "id": str(d["_id"]),
            "name": d.get("bot_name") or "Unnamed bot",
            "status": d.get("status", "unknown"),
            "error": d.get("last_error"),
            "created": time.strftime("%d %b %Y", time.gmtime(created)) if created else "",
            "invite": bot_invite(d.get("bot_user_id")),
        })
    return out


def check_token(token):
    """Returns (info, error). Never logs or echoes the token."""
    if DEMO:
        return {"id": "999", "name": "Demo Bot"}, None
    h = {"Authorization": f"Bot {token}"}
    try:
        me = requests.get(f"{API}/users/@me", headers=h, timeout=8)
    except Exception:
        return None, "Could not reach Discord. Please try again."
    if me.status_code == 401:
        return None, "That token is not valid. Copy it again from the Developer Portal."
    if me.status_code != 200:
        return None, "Discord rejected that token."
    info = me.json()
    try:
        app = requests.get(f"{API}/applications/@me", headers=h, timeout=8)
        if app.status_code == 200:
            flags = int(app.json().get("flags", 0))
            missing = []
            if not flags & MEMBERS_INTENT:
                missing.append("Server Members Intent")
            if not flags & CONTENT_INTENT:
                missing.append("Message Content Intent")
            if missing:
                return None, "Turn on " + " and ".join(missing) + " (Developer Portal, Bot tab), then try again."
    except Exception:
        pass
    return {"id": info["id"], "name": info.get("username", "Bot")}, None


@bots_bp.route("/")
def page():
    user = current_user()
    if not user:
        return redirect("/dashboard/")
    return render_template("rg_bots.html", bots=list_bots(int(user["discord_id"])), max_bots=MAX_BOTS,
                           user=user, demo=DEMO, invite_url=invite_url())


@bots_bp.route("/api/add", methods=["POST"])
def add():
    user = current_user()
    if not user:
        return jsonify(ok=False, error="Please log in again"), 401
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Bad request"), 400
    token = str(data.get("token", "")).strip()
    if not 50 <= len(token) <= 120 or " " in token:
        return jsonify(ok=False, error="That doesn't look like a bot token."), 400
    uid = int(user["discord_id"])
    if len(list_bots(uid)) >= MAX_BOTS:
        return jsonify(ok=False, error=f"You can host up to {MAX_BOTS} bots."), 400
    info, err = check_token(token)
    if err:
        return jsonify(ok=False, error=err), 400
    name = str(data.get("name", "")).strip()[:100] or info["name"]
    if DEMO:
        _DEMO_BOTS.append({"id": str(len(_DEMO_BOTS)), "name": name, "status": "active", "error": None,
                           "created": time.strftime("%d %b %Y"), "invite": bot_invite(info["id"])})
        return jsonify(ok=True)
    if _db["tenants"].find_one({"bot_user_id": info["id"]}):
        return jsonify(ok=False, error="That bot is already registered."), 400
    try:
        from utils.token_crypto import encrypt_token
        encrypted = encrypt_token(token)
    except Exception as e:
        log.error(f"Token encryption unavailable: {type(e).__name__}")
        return jsonify(ok=False, error="Bot hosting is not configured yet."), 500
    _db["tenants"].insert_one({
        "owner_discord_id": uid, "encrypted_token": encrypted, "bot_name": name,
        "bot_user_id": info["id"], "status": "active", "last_error": None,
        "created_at": time.time(), "source": "dashboard",
    })
    return jsonify(ok=True)


@bots_bp.route("/api/remove", methods=["POST"])
def remove():
    user = current_user()
    if not user:
        return jsonify(ok=False, error="Please log in again"), 401
    data = request.get_json(silent=True) or {}
    if DEMO:
        _DEMO_BOTS[:] = [b for b in _DEMO_BOTS if b["id"] != str(data.get("id"))]
        return jsonify(ok=True)
    try:
        oid = ObjectId(str(data.get("id", "")))
    except InvalidId:
        return jsonify(ok=False, error="Bad request"), 400
    res = _db["tenants"].delete_one({"_id": oid, "owner_discord_id": int(user["discord_id"])})
    return jsonify(ok=bool(res.deleted_count), error=None if res.deleted_count else "Bot not found")
