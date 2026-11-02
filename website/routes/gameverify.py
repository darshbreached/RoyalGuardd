"""
website/routes/gameverify.py
------------------------------
Endpoints the Roblox verification game's server-side Script calls.
Protected by a shared secret header (never a LocalScript - that source is
exploit-readable) since these endpoints can link any Discord ID to a Roblox
account if called by anyone else.

Flow:
  1. In Discord the player presses "Verify via ROBLOX Game" and types their
     Roblox username. The bot stores a pending request in oauth_states:
         state = "gamereq:<roblox_id>:<discord_username>:<unix_time>"
  2. The player joins the game. The game calls /lookup with the joining
     player's own UserId. If a fresh request exists it shows
     "IS THIS YOU? <discord_username>  [YES] [NO]".
  3. YES -> /confirm links the accounts. NO -> /cancel deletes the request.
  4. The player presses Done in Discord and the bot gives roles.

NOTE ON LOCATION DATA: unlike website/routes/oauth.py, these endpoints do
NOT log IP/country/ISP/VPN data. The connecting IP here is Roblox's own
game-server infrastructure, not the player's real IP.
"""

import hmac
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, request, jsonify
from pymongo import MongoClient

log = logging.getLogger("RoyalGuard")

gameverify_bp = Blueprint("gameverify", __name__)

GAME_API_KEY = os.getenv("ROBLOX_GAME_API_KEY")
FALLBACK_VERIFICATION_WEBHOOK = os.getenv("DISCORD_VERIFICATION_WEBHOOK")
ROBLOX_USER_API = "https://users.roblox.com/v1/users"

DISCORD_EPOCH_MS = 1420070400000
REQUEST_TTL_SECONDS = 10 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

_client = MongoClient(os.getenv("MONGODB_URI"))
_db = _client[os.getenv("MONGODB_DB_NAME", "royalguard")]


def _format_age(created_dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    days = (now - created_dt).days
    years, _ = divmod(days, 365)
    if years >= 1:
        return f"{years}y old (joined {created_dt.strftime('%Y-%m-%d')})"
    return f"{days}d old (joined {created_dt.strftime('%Y-%m-%d')})"


def get_discord_account_age(discord_id: str) -> str:
    try:
        snowflake = int(discord_id)
        timestamp_ms = (snowflake >> 22) + DISCORD_EPOCH_MS
        created_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
        return _format_age(created_dt)
    except Exception:
        return "Unknown"


def get_roblox_account_age(roblox_id: str) -> str:
    try:
        response = requests.get(f"{ROBLOX_USER_API}/{roblox_id}", timeout=5)
        if response.status_code != 200:
            return "Unknown"
        created_str = response.json().get("created")
        if not created_str:
            return "Unknown"
        created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        return _format_age(created_dt)
    except Exception:
        return "Unknown"


def post_game_verification_log(discord_id: str, roblox_username: str, roblox_id: str,
                                webhook_url: str, discord_age: str, roblox_age: str,
                                prior_discord_id: str = None):
    if not webhook_url:
        return

    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

    flags = []
    if prior_discord_id:
        flags.append(f"⚠️ This Roblox account was previously linked to <@{prior_discord_id}> (`{prior_discord_id}`)")

    description = (
        f"Discord: <@{discord_id}> | `{discord_id}`\n"
        f"ROBLOX: {roblox_username} | {profile_url}\n"
        f"Method: **Roblox Game**\n\n"
        f"Discord Account: {discord_age}\n"
        f"Roblox Account: {roblox_age}\n\n"
        f"(No IP/location data - in-game verification connects via Roblox's own servers, not the player's real IP)"
    )

    if flags:
        description += "\n\n" + "\n".join(flags)

    embed = {
        "title": "Verification Logs",
        "description": description,
        "color": 0xE67E22 if flags else 0x3498DB,
    }

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #

def _auth_error():
    """Returns a (response, status) tuple if the request is not allowed, else None."""
    if not GAME_API_KEY:
        log.error("ROBLOX_GAME_API_KEY is not configured - refusing all game-verify requests.")
        return jsonify({"success": False, "error": "Not configured"}), 500
    provided = request.headers.get("X-API-Key") or ""
    if not hmac.compare_digest(provided.encode(), GAME_API_KEY.encode()):
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    return None


def _roblox_id(data: dict):
    try:
        rid = int(data.get("roblox_id"))
    except (TypeError, ValueError):
        return None
    return str(rid) if rid > 0 else None


def _pending_pattern(rid: str) -> str:
    return f"^gamereq:{rid}:"


def _newest_request(rid: str):
    """Newest unexpired request for this Roblox ID: (timestamp, discord_name, doc) or None."""
    now = time.time()
    best = None
    for doc in _db["oauth_states"].find({"state": {"$regex": _pending_pattern(rid)}}):
        parts = str(doc.get("state", "")).split(":", 3)
        if len(parts) != 4:
            continue
        try:
            ts = int(parts[3])
        except ValueError:
            continue
        if now - ts > REQUEST_TTL_SECONDS:
            continue
        if best is None or ts > best[0]:
            best = (ts, parts[2], doc)
    return best


def _clear_requests(rid: str):
    _db["oauth_states"].delete_many({"state": {"$regex": _pending_pattern(rid)}})


# ---------------------------------------------------------------------- #
# endpoints
# ---------------------------------------------------------------------- #

@gameverify_bp.route("/api/game-verify/lookup", methods=["POST"])
def game_lookup():
    err = _auth_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    rid = _roblox_id(data)
    if not rid:
        return jsonify({"success": False, "error": "Missing roblox_id"}), 400

    found = _newest_request(rid)
    if not found:
        return jsonify({"success": True, "found": False})
    return jsonify({"success": True, "found": True, "discord_name": found[1]})


@gameverify_bp.route("/api/game-verify/cancel", methods=["POST"])
def game_cancel():
    err = _auth_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    rid = _roblox_id(data)
    if not rid:
        return jsonify({"success": False, "error": "Missing roblox_id"}), 400

    _clear_requests(rid)
    return jsonify({"success": True})


@gameverify_bp.route("/api/game-verify/confirm", methods=["POST"])
def game_confirm():
    err = _auth_error()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    rid = _roblox_id(data)
    roblox_username = str(data.get("roblox_username") or "").strip()
    if not rid or not USERNAME_RE.match(roblox_username):
        return jsonify({"success": False, "error": "Missing or invalid roblox_id / roblox_username"}), 400

    found = _newest_request(rid)
    if not found:
        return jsonify({"success": False, "error": "No pending request (it may have expired)"}), 404

    doc = found[2]
    discord_id = doc["discord_id"]
    guild_id = doc.get("guild_id")

    existing_link = _db["verifications"].find_one({"roblox_id": rid})
    prior_discord_id = None
    if existing_link and existing_link.get("discord_id") != discord_id:
        prior_discord_id = existing_link["discord_id"]

    discord_age = get_discord_account_age(discord_id)
    roblox_age = get_roblox_account_age(rid)

    _db["verifications"].update_one(
        {"discord_id": discord_id},
        {"$set": {
            "discord_id": discord_id,
            "roblox_id": rid,
            "roblox_username": roblox_username,
            "verified_at": time.time(),
            "verification_method": "roblox_game",
        }},
        upsert=True,
    )

    webhook_url = FALLBACK_VERIFICATION_WEBHOOK
    if guild_id:
        guild_config = _db["guild_config"].find_one({"guild_id": guild_id})
        if guild_config and guild_config.get("verification_webhook_url"):
            webhook_url = guild_config["verification_webhook_url"]

        _db["verification_events"].insert_one({
            "discord_id": discord_id,
            "roblox_id": rid,
            "roblox_username": roblox_username,
            "guild_id": guild_id,
            "ip": None,
            "user_agent": "roblox_game",
            "timestamp": time.time(),
        })

    post_game_verification_log(
        discord_id, roblox_username, rid, webhook_url,
        discord_age, roblox_age, prior_discord_id=prior_discord_id,
    )

    _clear_requests(rid)

    return jsonify({"success": True, "roblox_username": roblox_username})