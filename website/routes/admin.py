"""
website/routes/admin.py
Private, owner-only control panel at /admin, protected by a password.

Needs these on the WEB service (Railway -> Variables):
  ADMIN_PASSWORD      a long password (the panel stays switched off without it)
  WEBSITE_SECRET_KEY  a long random string (the panel refuses to run on the default)

The site never talks to Discord's gateway. It queues "jobs" in MongoDB and
cogs/adminjobs.py (inside the bot) carries them out and reports back. The bot
token is checked against Discord, encrypted, and never shown again.
"""
import re
import time
import hmac
import secrets
import logging
import functools

import os
import requests
from flask import Blueprint, render_template, request, jsonify, redirect, session, current_app

from website.routes.dashboard import _db

log = logging.getLogger("RoyalGuard")
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")

SESSION_SECONDS = 2 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
_fails = {}  # in memory only (client -> [count, first_time]); never stored or logged
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


def client_key():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[-1].strip() or request.remote_addr or "?")


def admin_only(api):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not configured():
                return "Not found", 404
            if not logged_in():
                return (jsonify(ok=False, error="Please log in again"), 401) if api else redirect("/admin/login")
            if api and request.method != "GET":
                sent, real = request.headers.get("X-CSRF-Token", ""), session.get("admin_csrf", "")
                if not real or not hmac.compare_digest(sent.encode(), real.encode()):
                    return jsonify(ok=False, error="Bad request"), 400
            return fn(*args, **kwargs)
        return wrapper
    return deco


def get_status():
    s = _db["bot_status"].find_one({"_id": "main"}) or {}
    age = int(time.time() - s["updated_at"]) if s.get("updated_at") else None
    return {"online": age is not None and age < 90, "age": age, "user": s.get("user"),
            "latency": s.get("latency_ms"), "guilds": s.get("guilds", [])}


def get_override():
    d = _db["bot_config"].find_one({"_id": "main_token"})
    if not d:
        return None
    return {"name": d.get("bot_name"), "set_at": time.strftime("%d %b %Y %H:%M UTC", time.gmtime(d.get("set_at", 0)))}


def recent_jobs():
    out = []
    for j in _db["admin_jobs"].find().sort("created_at", -1).limit(15):
        p = j.get("params") or {}
        label = {"post_panel": f"Post {p.get('panel')} panel", "send_message": "Send message",
                 "restart": "Restart bot"}.get(j.get("type"), str(j.get("type")))
        if p.get("channel_id"):
            label += f" to {p['channel_id']}"
        out.append({"label": label, "status": j.get("status"), "result": j.get("result") or "",
                    "when": time.strftime("%H:%M:%S", time.gmtime(j.get("created_at", 0)))})
    return out


def snapshot():
    st = get_status()
    return {"online": st["online"], "age": st["age"], "user": st["user"], "latency": st["latency"],
            "override": get_override(), "jobs": recent_jobs()}


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    if not configured():
        return "Not found", 404
    key, now = client_key(), time.time()
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
    return render_template("rg_admin_login.html", error=error)


@admin_bp.route("/logout")
def logout():
    session.pop("admin_at", None)
    session.pop("admin_csrf", None)
    return redirect("/admin/login")


@admin_bp.route("/")
@admin_only(False)
def home():
    return render_template("rg_admin.html", csrf=session["admin_csrf"], guilds=get_status()["guilds"], panels=PANELS)


@admin_bp.route("/api/status")
@admin_only(True)
def api_status():
    return jsonify(ok=True, **snapshot())


@admin_bp.route("/api/job", methods=["POST"])
@admin_only(True)
def api_job():
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify(ok=False, error="Bad request"), 400
    kind = d.get("type")
    params = {}
    if kind in ("post_panel", "send_message"):
        cid = str(d.get("channel_id", "")).strip()
        if not CHANNEL_RE.fullmatch(cid):
            return jsonify(ok=False, error="Enter a valid channel ID."), 400
        params["channel_id"] = cid
    if kind == "post_panel":
        if d.get("panel") not in PANELS:
            return jsonify(ok=False, error="Pick a panel."), 400
        params["panel"] = d["panel"]
    elif kind == "send_message":
        desc, title = str(d.get("description", "")).strip(), str(d.get("title", "")).strip()
        if not 1 <= len(desc) <= 4000 or len(title) > 256:
            return jsonify(ok=False, error="Message must be 1-4000 characters (title up to 256)."), 400
        params.update(description=desc, title=title)
    elif kind != "restart":
        return jsonify(ok=False, error="Bad request"), 400
    _db["admin_jobs"].insert_one({"type": kind, "params": params, "status": "pending", "created_at": time.time()})
    return jsonify(ok=True)


@admin_bp.route("/api/token", methods=["POST"])
@admin_only(True)
def api_token():
    d = request.get_json(silent=True) or {}
    if d.get("clear"):
        _db["bot_config"].delete_one({"_id": "main_token"})
        return jsonify(ok=True)
    token = str(d.get("token", "")).strip()
    if not 50 <= len(token) <= 120 or " " in token:
        return jsonify(ok=False, error="That doesn't look like a bot token."), 400
    try:
        me = requests.get("https://discord.com/api/v10/users/@me", headers={"Authorization": f"Bot {token}"}, timeout=8)
    except Exception:
        return jsonify(ok=False, error="Could not reach Discord. Try again."), 502
    if me.status_code != 200:
        return jsonify(ok=False, error="Discord says that token is not valid."), 400
    try:
        from utils.token_crypto import encrypt_token
        encrypted = encrypt_token(token)
    except Exception as e:
        log.error(f"Token encryption unavailable: {type(e).__name__}")
        return jsonify(ok=False, error="Token storage is not configured (TENANT_ENCRYPTION_KEY)."), 500
    _db["bot_config"].update_one({"_id": "main_token"}, {"$set": {
        "encrypted_token": encrypted, "bot_name": me.json().get("username"), "set_at": time.time()}}, upsert=True)
    return jsonify(ok=True, name=me.json().get("username"))