"""
website/routes/gameverify.py
------------------------------
Endpoint the Roblox game's server-side Script calls to complete
in-game verification. Protected by a shared secret header (never a
LocalScript - that source is exploit-readable) since this endpoint can
link any Discord ID to any Roblox account if called by anyone else.

The game never sees or needs the Discord ID - it's already stored on the
oauth_states "gamecode:<code>" record created when the player pressed
"Verify via ROBLOX Game" in Discord. This endpoint just looks that up by
the code the player typed in-game.

NOTE ON LOCATION DATA: unlike website/routes/oauth.py, this endpoint does
NOT log IP/country/ISP/VPN data. The connecting IP here is Roblox's own
game-server infrastructure, not the player's real IP - logging it would
show every single player as being wherever Roblox's servers are, which is
actively wrong information, not just imprecise.
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
from pymongo import MongoClient

log = logging.getLogger("RoyalGuard")

gameverify_bp = Blueprint("gameverify", __name__)

GAME_API_KEY = os.getenv("ROBLOX_GAME_API_KEY")
FALLBACK_VERIFICATION_WEBHOOK = os.getenv("DISCORD_VERIFICATION_WEBHOOK")
ROBLOX_USER_API = "https://users.roblox.com/v1/users"

DISCORD_EPOCH_MS = 1420070400000

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


@gameverify_bp.route("/api/game-verify", methods=["POST"])
def game_verify():
    if not GAME_API_KEY:
        log.error("ROBLOX_GAME_API_KEY is not configured - refusing all game-verify requests.")
        return jsonify({"success": False, "error": "Not configured"}), 500

    provided_key = request.headers.get("X-API-Key")
    if provided_key != GAME_API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401

    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip().upper()
    roblox_id = data.get("roblox_id")
    roblox_username = data.get("roblox_username")

    if not code or not roblox_id or not roblox_username:
        return jsonify({"success": False, "error": "Missing code, roblox_id, or roblox_username"}), 400

    state_doc = _db["oauth_states"].find_one({"state": f"gamecode:{code}"})
    if not state_doc:
        return jsonify({"success": False, "error": "Invalid or expired code"}), 404

    discord_id = state_doc["discord_id"]
    guild_id = state_doc.get("guild_id")

    existing_link = _db["verifications"].find_one({"roblox_id": str(roblox_id)})
    prior_discord_id = None
    if existing_link and existing_link.get("discord_id") != discord_id:
        prior_discord_id = existing_link["discord_id"]

    discord_age = get_discord_account_age(discord_id)
    roblox_age = get_roblox_account_age(str(roblox_id))

    _db["verifications"].update_one(
        {"discord_id": discord_id},
        {"$set": {
            "discord_id": discord_id,
            "roblox_id": str(roblox_id),
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
            "roblox_id": str(roblox_id),
            "roblox_username": roblox_username,
            "guild_id": guild_id,
            "ip": None,
            "user_agent": "roblox_game",
            "timestamp": time.time(),
        })

    post_game_verification_log(
        discord_id, roblox_username, str(roblox_id), webhook_url,
        discord_age, roblox_age, prior_discord_id=prior_discord_id,
    )

    _db["oauth_states"].delete_one({"state": f"gamecode:{code}"})

    return jsonify({"success": True, "roblox_username": roblox_username})
