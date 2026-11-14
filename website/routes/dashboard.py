"""
website/routes/dashboard.py
Server dashboard: Discord login, server list, per-server settings.
Settings live in the same guild_config collection the bot already uses.
Set DASHBOARD_DEMO=1 to preview locally without Discord login or Mongo.
"""
import os
import re
import time
import secrets
import logging
from urllib.parse import urlparse, quote

import requests
from flask import Blueprint, render_template, request, jsonify, abort, session, redirect
from pymongo import MongoClient

from utils.modules import MODULES, CATEGORIES
from website.routes.site import invite_url

log = logging.getLogger("RoyalGuard")
dash_bp = Blueprint("dash", __name__, url_prefix="/dashboard")

_db = MongoClient(os.getenv("MONGODB_URI"))[os.getenv("MONGODB_DB_NAME", "royalguard")]

DEMO = os.getenv("DASHBOARD_DEMO") == "1"
TOGGLEABLE = {m["key"]: m for m in MODULES if not m["hidden"]}
# Modules that also run background listeners/tasks. The dashboard toggle only
# blocks their commands until each cog is updated to check the toggle.
BACKGROUND = {"automod", "antinuke", "antiraid", "invites", "reactionroles", "globalban", "boosterroles", "loa"}
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SESSION_MAX_AGE = 3600

DEMO_USER = {"discord_id": "0", "username": "Demo", "updated_at": 0, "guilds": [
    {"id": "1", "name": "Royal Guard HQ"}, {"id": "2", "name": "Test Battalion"}]}
_STORE = {}
_BOT_CACHE = {}


def redirect_uri():
    explicit = os.getenv("DASHBOARD_REDIRECT_URI")
    if explicit:
        return explicit
    p = urlparse(os.getenv("DISCORD_REDIRECT_URI", ""))
    return f"{p.scheme}://{p.netloc}/dashboard/callback" if p.netloc else ""


def configured():
    return bool(os.getenv("DISCORD_CLIENT_ID") and os.getenv("DISCORD_CLIENT_SECRET") and redirect_uri())


def manageable(g):
    p = int(g.get("permissions", 0) or 0)
    return bool(g.get("owner")) or bool(p & 0x8) or bool(p & 0x20)


def bot_in_guild(gid):
    token = os.getenv("DISCORD_TOKEN")
    if DEMO or not token:
        return True
    hit = _BOT_CACHE.get(gid)
    if hit and hit[0] > time.time():
        return hit[1]
    ok = True
    try:
        r = requests.get(f"https://discord.com/api/v10/guilds/{gid}", headers={"Authorization": f"Bot {token}"}, timeout=5)
        if r.status_code in (403, 404):
            ok = False
    except Exception:
        pass
    _BOT_CACHE[gid] = (time.time() + 60, ok)
    return ok


def current_user():
    if DEMO:
        return DEMO_USER
    uid = session.get("dash_id")
    if not uid:
        return None
    user = _db["dashboard_users"].find_one({"discord_id": uid})
    if not user or time.time() - user.get("updated_at", 0) > SESSION_MAX_AGE:
        return None
    return user


def load_settings(gid):
    cfg = _STORE.get(gid, {}) if DEMO else (_db["guild_config"].find_one({"guild_id": str(gid)}) or {})
    mods = {k: True for k in TOGGLEABLE}
    mods.update({k: bool(v) for k, v in (cfg.get("modules") or {}).items() if k in TOGGLEABLE})
    return {"modules": mods, "branding": {
        "army_name": cfg.get("army_name", ""), "currency_name": cfg.get("currency_name", ""),
        "embed_color": cfg.get("embed_color", "#b30000")}}


def save_settings(gid, s):
    b = s["branding"]
    if DEMO:
        _STORE[gid] = {"modules": s["modules"], **b}
        return
    sets = {"guild_id": str(gid), "modules": s["modules"], "embed_color": b["embed_color"]}
    unsets = {}
    for k in ("army_name", "currency_name"):
        if b[k]:
            sets[k] = b[k]
        else:
            unsets[k] = ""
    upd = {"$set": sets}
    if unsets:
        upd["$unset"] = unsets
    _db["guild_config"].update_one({"guild_id": str(gid)}, upd, upsert=True)


def clean(p):
    mods = p.get("modules") if isinstance(p.get("modules"), dict) else {}
    br = p.get("branding") if isinstance(p.get("branding"), dict) else {}
    color = str(br.get("embed_color", ""))
    return {
        "modules": {k: bool(v) for k, v in mods.items() if k in TOGGLEABLE},
        "branding": {
            "army_name": str(br.get("army_name", "")).strip()[:28],
            "currency_name": str(br.get("currency_name", "")).strip()[:16],
            "embed_color": color if COLOR_RE.match(color) else "#b30000",
        },
    }


@dash_bp.route("/login")
def login():
    if not configured():
        return render_template("error.html", message="Dashboard login is not configured yet."), 500
    state = secrets.token_urlsafe(24)
    session["dash_state"] = state
    q = (f"client_id={quote(os.getenv('DISCORD_CLIENT_ID'))}&redirect_uri={quote(redirect_uri(), safe='')}"
         f"&response_type=code&scope=identify%20guilds&state={state}")
    return redirect(f"https://discord.com/api/oauth2/authorize?{q}")


@dash_bp.route("/callback")
def callback():
    code, state = request.args.get("code"), request.args.get("state")
    if not code or not state or state != session.pop("dash_state", None):
        return render_template("error.html", message="This login link is invalid or has expired. Please try again."), 400
    try:
        t = requests.post("https://discord.com/api/oauth2/token", data={
            "client_id": os.getenv("DISCORD_CLIENT_ID"), "client_secret": os.getenv("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}, timeout=10)
        if t.status_code != 200:
            raise RuntimeError(t.text)
        h = {"Authorization": f"Bearer {t.json()['access_token']}"}
        me = requests.get("https://discord.com/api/users/@me", headers=h, timeout=10).json()
        gs = requests.get("https://discord.com/api/users/@me/guilds", headers=h, timeout=10).json()
        if "id" not in me or not isinstance(gs, list):
            raise RuntimeError("bad Discord response")
    except Exception as e:
        log.error(f"Dashboard login failed: {e}")
        return render_template("error.html", message="Failed to log in with Discord. Please try again."), 400
    _db["dashboard_users"].update_one({"discord_id": me["id"]}, {"$set": {
        "discord_id": me["id"], "username": me.get("username", "Unknown"), "updated_at": time.time(),
        "guilds": [{"id": g["id"], "name": g["name"]} for g in gs if manageable(g)]}}, upsert=True)
    session["dash_id"] = me["id"]
    return redirect("/dashboard/")


@dash_bp.route("/logout")
def logout():
    session.pop("dash_id", None)
    return redirect("/dashboard/")


@dash_bp.route("/")
def servers():
    user = current_user()
    if not user:
        return render_template("rg_dash.html", view="login", configured=configured(), invite_url=invite_url())
    inv = invite_url()
    guilds = []
    for g in user["guilds"]:
        has = bot_in_guild(g["id"])
        link = inv if inv == "#" else f"{inv}&guild_id={g['id']}&disable_guild_select=true"
        guilds.append({**g, "bot": has, "invite": link})
    return render_template("rg_dash.html", view="list", guilds=guilds, user=user, demo=DEMO, invite_url=inv)


@dash_bp.route("/<gid>")
def server(gid):
    user = current_user()
    if not user:
        return redirect("/dashboard/")
    guild = next((g for g in user["guilds"] if g["id"] == gid), None)
    if not guild or not bot_in_guild(gid):
        abort(404)
    grouped = {c: [m for m in TOGGLEABLE.values() if m["category"] == c] for c in CATEGORIES}
    return render_template("rg_dash.html", view="server", guild=guild, grouped=grouped, s=load_settings(gid),
                           bg=BACKGROUND, invite_url=invite_url())


@dash_bp.route("/<gid>/api", methods=["POST"])
def api(gid):
    user = current_user()
    if not user:
        return jsonify(ok=False, error="Please log in again"), 401
    if not any(g["id"] == gid for g in user["guilds"]):
        return jsonify(ok=False, error="No access to that server"), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Bad request"), 400
    save_settings(gid, clean(data))
    return jsonify(ok=True)
