"""
website/routes/admin.py
Private, owner-only page at /admin, protected by a password.

Set these on the WEB service (Railway -> Variables) or the page stays off:
  ADMIN_PASSWORD      a long password
  WEBSITE_SECRET_KEY  a long random string (the page refuses to run on the default)

The site never talks to Discord directly. It queues "jobs" in MongoDB and
cogs/adminjobs.py (inside the bot) carries them out. A bot token entered here
is checked against Discord, encrypted, and never shown again.
"""
import os
import re
import time
import hmac
import secrets
import logging
import functools

import requests
from flask import Blueprint, render_template, request, redirect, session, current_app
from pymongo import MongoClient

log = logging.getLogger("RoyalGuard")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

_db = MongoClient(os.getenv("MONGODB_URI"))[os.getenv("MONGODB_DB_NAME", "royalguard")]

SESSION_SECONDS = 2 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails = {}  # in memory only (visitor -> [count, first_time]); never stored or logged
PANELS = {
    "verification": "Verification panel",
    "tickets_report": "Report tickets panel",
    "tickets_other": "Other tickets panel",
}
CHANNEL_RE = re.compile(r"\d{15,22}")


def configured():
    return bool(os.getenv("ADMIN_PASSWORD")) and current_app.secret_key not in ("", None, "dev-secret-change-me")


def logged_in():
    at = session.get("admin_at")
    return bool(at) and time.time() - at < SESSION_SECONDS


def visitor_key():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[-1].strip() or request.remote_addr or "?"


def admin_only(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not configured():
            return "Not found", 404
        if not logged_in():
            return redirect("/admin/login")
        if request.method == "POST":
            sent, real = request.form.get("csrf", ""), session.get("admin_csrf", "")
            if not real or not hmac.compare_digest(sent.encode(), real.encode()):
                return "Bad request", 400
        return fn(*args, **kwargs)
    return wrapper


def notify(text, bad=False):
    session["admin_msg"] = [text, bad]


def queue_job(kind, params):
    _db["admin_jobs"].insert_one({"type": kind, "params": params, "status": "pending", "created_at": time.time()})


def recent_jobs():
    names = {"post_panel": "Post panel", "send_message": "Send message", "restart": "Restart bot"}
    out = []
    for j in _db["admin_jobs"].find().sort("created_at", -1).limit(10):
        p = j.get("params") or {}
        label = names.get(j.get("type"), str(j.get("type")))
        if p.get("panel"):
            label += f" ({p['panel']})"
        if p.get("channel_id"):
            label += f" to {p['channel_id']}"
        out.append({"label": label, "status": j.get("status"), "result": j.get("result") or "",
                    "when": time.strftime("%H:%M:%S", time.gmtime(j.get("created_at", 0)))})
    return out


def get_override():
    d = _db["bot_config"].find_one({"_id": "main_token"})
    return {"name": d.get("bot_name")} if d else None


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if not configured():
        return "Not found", 404
    key, now = visitor_key(), time.time()
    rec = _fails.get(key)
    if rec and now - rec[1] > LOCK_SECONDS:
        _fails.pop(key, None)
        rec = None
    error = None
    if request.method == "POST":
        if rec and rec[0] >= MAX_FAILS:
            error = "Too many attempts. Try again in 15 minutes."
        else:
            given = request.form.get("password", "")
            if hmac.compare_digest(given.encode(), os.getenv("ADMIN_PASSWORD").encode()):
                _fails.pop(key, None)
                session["admin_at"] = now
                session["admin_csrf"] = secrets.token_urlsafe(24)
                return redirect("/admin/")
            _fails.setdefault(key, [0, now])[0] += 1
            error = "Wrong password."
            time.sleep(1)
    return render_template("admin_login.html", error=error)


@admin_bp.route("/logout")
def logout():
    session.pop("admin_at", None)
    session.pop("admin_csrf", None)
    return redirect("/admin/login")


@admin_bp.route("/")
@admin_only
def home():
    jobs = recent_jobs()
    return render_template("admin.html", csrf=session["admin_csrf"], msg=session.pop("admin_msg", None),
                           panels=PANELS, override=get_override(), jobs=jobs,
                           busy=any(j["status"] in ("pending", "running") for j in jobs))


@admin_bp.route("/panel", methods=["POST"])
@admin_only
def post_panel():
    cid, kind = request.form.get("channel_id", "").strip(), request.form.get("panel")
    if not CHANNEL_RE.fullmatch(cid):
        notify("Enter a valid channel ID (numbers only).", True)
    elif kind not in PANELS:
        notify("Pick a panel.", True)
    else:
        queue_job("post_panel", {"channel_id": cid, "panel": kind})
        notify("Queued. The bot posts it within a few seconds.")
    return redirect("/admin/")


@admin_bp.route("/message", methods=["POST"])
@admin_only
def send_message():
    cid = request.form.get("channel_id", "").strip()
    title, text = request.form.get("title", "").strip(), request.form.get("description", "").strip()
    if not CHANNEL_RE.fullmatch(cid):
        notify("Enter a valid channel ID (numbers only).", True)
    elif not 1 <= len(text) <= 4000 or len(title) > 256:
        notify("Message must be 1-4000 characters (title up to 256).", True)
    else:
        queue_job("send_message", {"channel_id": cid, "title": title, "description": text})
        notify("Queued. The bot sends it within a few seconds.")
    return redirect("/admin/")


@admin_bp.route("/token", methods=["POST"])
@admin_only
def save_token():
    token = request.form.get("token", "").strip()
    if not 50 <= len(token) <= 120 or " " in token:
        notify("That doesn't look like a bot token.", True)
        return redirect("/admin/")
    try:
        me = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}, timeout=8)
    except Exception:
        notify("Could not reach Discord. Try again.", True)
        return redirect("/admin/")
    if me.status_code != 200:
        notify("Discord says that token is not valid.", True)
        return redirect("/admin/")
    try:
        from utils.token_crypto import encrypt_token
        encrypted = encrypt_token(token)
    except Exception as e:
        log.error(f"Token encryption unavailable: {type(e).__name__}")
        notify("Token storage is not configured (TENANT_ENCRYPTION_KEY).", True)
        return redirect("/admin/")
    name = me.json().get("username")
    _db["bot_config"].update_one({"_id": "main_token"}, {"$set": {
        "encrypted_token": encrypted, "bot_name": name, "set_at": time.time()}}, upsert=True)
    notify(f'Saved the token for "{name}". Press Restart bot to use it.')
    return redirect("/admin/")


@admin_bp.route("/token/clear", methods=["POST"])
@admin_only
def clear_token():
    _db["bot_config"].delete_one({"_id": "main_token"})
    notify("Saved token removed. The bot goes back to DISCORD_TOKEN on its next start.")
    return redirect("/admin/")


@admin_bp.route("/restart", methods=["POST"])
@admin_only
def restart():
    queue_job("restart", {})
    notify("Restart queued.")
    return redirect("/admin/")