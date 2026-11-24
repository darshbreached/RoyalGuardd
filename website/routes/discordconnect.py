"""
website/routes/discordconnect.py
---------------------------------
"Verify via Discord Connection": reads the Roblox account the member linked in
Discord (User Settings -> Connections -> Roblox) using Discord OAuth2 with the
`connections` scope, and saves it as their verification.

Flow:
  1. Bot creates a pending state "dcconn:<token>:<unix_time>" in oauth_states
     (same helper as the other methods) and gives the member a link to
     /connect-verify?state=...
  2. /connect-verify sends them to Discord to authorise (identify + connections).
  3. /connect-verify/callback exchanges the code, checks the Discord account
     matches the one that pressed the button, reads the "roblox" connection,
     double-checks the Roblox ID against the Roblox API, then saves it.
  4. Member returns to Discord and presses Done.

Safety: if the connection's ID doesn't resolve to a Roblox user whose name or
display name matches the connection name, verification is REFUSED (no guessing
by username - display names can collide with other people's usernames).
No IP/location is logged here.
"""

import logging
import os
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, request, session, redirect, render_template

from website.routes.gameverify import _db, get_discord_account_age, get_roblox_account_age

log = logging.getLogger("RoyalGuard")

discordconnect_bp = Blueprint("discordconnect", __name__)

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"
DISCORD_CONNECTIONS_URL = "https://discord.com/api/users/@me/connections"
ROBLOX_USER_API = "https://users.roblox.com/v1/users"

REQUEST_TTL_SECONDS = 10 * 60
FALLBACK_VERIFICATION_WEBHOOK = os.getenv("DISCORD_VERIFICATION_WEBHOOK")


def _redirect_uri() -> str:
    base = os.getenv("WEBSITE_BASE_URL", "").rstrip("/")
    return f"{base}/connect-verify/callback"


def _err(message: str, code: int = 400):
    return render_template("error.html", message=message), code


def _load_request(state: str):
    """Returns the pending oauth_states doc if state is a fresh dcconn state, else None."""
    if not state or not state.startswith("dcconn:"):
        return None
    parts = state.split(":")
    if len(parts) != 3:
        return None
    try:
        ts = int(parts[2])
    except ValueError:
        return None
    if time.time() - ts > REQUEST_TTL_SECONDS:
        return None
    return _db["oauth_states"].find_one({"state": state})


def _resolve_roblox(conn: dict):
    """(roblox_id, roblox_username) if the connection's ID is a real Roblox user
    whose name/displayName matches the connection name, else None."""
    cid = str(conn.get("id") or "").strip()
    cname = str(conn.get("name") or "").strip().lower()
    if not cid.isdigit() or not cname:
        return None
    try:
        r = requests.get(f"{ROBLOX_USER_API}/{cid}", timeout=5)
        if r.status_code != 200:
            return None
        u = r.json()
        names = {str(u.get("name") or "").lower(), str(u.get("displayName") or "").lower()}
        if cname in names and u.get("id"):
            return str(u["id"]), u.get("name")
    except Exception:
        return None
    return None


def _post_log(discord_id, roblox_username, roblox_id, webhook_url, discord_age, roblox_age, prior_discord_id=None):
    if not webhook_url:
        return
    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"
    description = (
        f"Discord: <@{discord_id}> | `{discord_id}`\n"
        f"ROBLOX: {roblox_username} | {profile_url}\n"
        f"Method: **Discord Connection**\n\n"
        f"Discord Account: {discord_age}\n"
        f"Roblox Account: {roblox_age}\n\n"
        f"(No IP/location data is logged for this method)"
    )
    if prior_discord_id:
        description += f"\n\n⚠️ This Roblox account was previously linked to <@{prior_discord_id}> (`{prior_discord_id}`)"
    embed = {
        "title": "Verification Logs",
        "description": description,
        "color": 0xE67E22 if prior_discord_id else 0x5865F2,
    }
    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


@discordconnect_bp.route("/connect-verify")
def connect_start():
    state = request.args.get("state", "")
    if not _load_request(state):
        return _err("This verification link has expired. Go back to Discord and press the button again.")

    client_id = os.getenv("DISCORD_CLIENT_ID")
    if not client_id:
        log.error("[DC-CONNECT] DISCORD_CLIENT_ID is not set on the web service.")
        return _err("Verification isn't configured right now.", 500)

    session["dcconn_state"] = state
    params = urlencode({
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": "identify connections",
        "state": state,
    })
    return redirect(f"{DISCORD_AUTHORIZE_URL}?{params}")


@discordconnect_bp.route("/connect-verify/callback")
def connect_callback():
    if request.args.get("error"):
        return _err("Discord login was cancelled. Go back to Discord and press the button again.")

    code = request.args.get("code")
    state = request.args.get("state", "")
    if not code or not state or session.get("dcconn_state") != state:
        return _err("This verification session isn't valid. Go back to Discord and press the button again.")

    doc = _load_request(state)
    if not doc:
        return _err("This verification link has expired. Go back to Discord and press the button again.")

    client_id = os.getenv("DISCORD_CLIENT_ID")
    client_secret = os.getenv("DISCORD_CLIENT_SECRET")
    if not client_id or not client_secret:
        log.error("[DC-CONNECT] DISCORD_CLIENT_ID / DISCORD_CLIENT_SECRET missing on the web service.")
        return _err("Verification isn't configured right now.", 500)

    try:
        tok = requests.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
        if tok.status_code != 200:
            log.error("[DC-CONNECT] token exchange failed: %s %s", tok.status_code, tok.text[:200])
            return _err("Discord didn't accept the login. Go back to Discord and try again.")
        access = tok.json().get("access_token")
        headers = {"Authorization": f"Bearer {access}"}

        me = requests.get(DISCORD_USER_URL, headers=headers, timeout=10)
        conns = requests.get(DISCORD_CONNECTIONS_URL, headers=headers, timeout=10)
    except Exception as e:
        log.error("[DC-CONNECT] Discord request failed: %s", e)
        return _err("Couldn't reach Discord. Please try again in a minute.", 502)

    if me.status_code != 200 or conns.status_code != 200:
        log.error("[DC-CONNECT] me/connections failed: %s / %s", me.status_code, conns.status_code)
        return _err("Couldn't read your Discord connections. Please try again.", 502)

    discord_id = str(doc["discord_id"])
    if str(me.json().get("id")) != discord_id:
        return _err("You logged into a different Discord account than the one that pressed the button.")

    roblox_conns = [c for c in conns.json() if c.get("type") == "roblox"]
    if not roblox_conns:
        return _err(
            "No Roblox account is linked to your Discord. In Discord go to User Settings > "
            "Connections > Roblox, link your account, then press the button again."
        )

    conn = roblox_conns[0]
    log.info("[DC-CONNECT] roblox connection: id=%r name=%r verified=%r", conn.get("id"), conn.get("name"), conn.get("verified"))

    if conn.get("verified") is False:
        return _err("Your Roblox connection in Discord isn't verified. Re-link it in Discord and try again.")

    resolved = _resolve_roblox(conn)
    if not resolved:
        log.error("[DC-CONNECT] could not resolve connection to a Roblox user: %r", {k: conn.get(k) for k in ("id", "name", "type", "verified")})
        return _err("Couldn't match your Discord Roblox connection to a Roblox account. Use another verification method.")

    rid, roblox_username = resolved
    guild_id = doc.get("guild_id")

    existing_link = _db["verifications"].find_one({"roblox_id": rid})
    prior_discord_id = None
    if existing_link and str(existing_link.get("discord_id")) != discord_id:
        prior_discord_id = existing_link["discord_id"]

    discord_age = get_discord_account_age(discord_id)
    roblox_age = get_roblox_account_age(rid)

    _db["verifications"].update_one(
        {"discord_id": doc["discord_id"]},
        {"$set": {
            "discord_id": doc["discord_id"],
            "roblox_id": rid,
            "roblox_username": roblox_username,
            "verified_at": time.time(),
            "verification_method": "discord_connection",
        }},
        upsert=True,
    )

    webhook_url = FALLBACK_VERIFICATION_WEBHOOK
    if guild_id:
        guild_config = _db["guild_config"].find_one({"guild_id": guild_id})
        if guild_config and guild_config.get("verification_webhook_url"):
            webhook_url = guild_config["verification_webhook_url"]

        _db["verification_events"].insert_one({
            "discord_id": doc["discord_id"],
            "roblox_id": rid,
            "roblox_username": roblox_username,
            "guild_id": guild_id,
            "ip": None,
            "user_agent": "discord_connection",
            "timestamp": time.time(),
        })

    _post_log(discord_id, roblox_username, rid, webhook_url, discord_age, roblox_age, prior_discord_id)

    _db["oauth_states"].delete_many({"state": state})
    session.pop("dcconn_state", None)

    return (
        "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Verified</title></head>"
        "<body style='font-family:sans-serif;background:#111;color:#eee;display:flex;align-items:center;"
        "justify-content:center;height:100vh;margin:0;text-align:center'>"
        f"<div><h1>✅ Linked to {roblox_username}</h1>"
        "<p>You can close this tab and press <b>Done</b> in Discord.</p></div></body></html>"
    )
