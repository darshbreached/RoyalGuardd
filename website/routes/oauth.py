"""
website/routes/oauth.py
------------------------
Handles the Roblox OAuth2 authorization + callback flow.

/authorize?state=<state>   -> redirects the user to Roblox's consent screen
/callback?code=...&state=... -> exchanges the code for a token, fetches the
                                  Roblox profile, links it to the Discord
                                  account, and posts a "Verification Logs"
                                  embed to Discord.

PRIVACY: no IP address, location, address, coordinates or ISP is read, looked
up, stored or logged anywhere in this flow. Only the Discord ID, Roblox ID,
Roblox username and verification time are saved.

Alt protection without IPs:
  * a Roblox account that is already linked to a DIFFERENT Discord account is
    refused (staff can /unverify the old link first)
  * Roblox accounts younger than MIN_ACCOUNT_AGE_DAYS (env var, default 7) are
    flagged in the verification log (not blocked)

The oauth_states record (created by cogs/verification.py) carries which
guild the /verify flow was started in, so the log gets posted to that
guild's own verification-logs webhook (set via /setup ->
Verification Logs Channel) if one exists. Falls back to the global
DISCORD_VERIFICATION_WEBHOOK env var for guilds that haven't set one up,
or for states created before this field existed.
"""

import os
import time
import logging
from datetime import datetime, timezone

import requests
from flask import Blueprint, request, redirect, render_template
from pymongo import MongoClient

log = logging.getLogger("RoyalGuard")

oauth_bp = Blueprint("oauth", __name__)

ROBLOX_AUTHORIZE_URL = "https://apis.roblox.com/oauth/v1/authorize"
ROBLOX_TOKEN_URL = "https://apis.roblox.com/oauth/v1/token"
ROBLOX_USERINFO_URL = "https://apis.roblox.com/oauth/v1/userinfo"

CLIENT_ID = os.getenv("ROBLOX_CLIENT_ID")
CLIENT_SECRET = os.getenv("ROBLOX_CLIENT_SECRET")
REDIRECT_URI = os.getenv("ROBLOX_REDIRECT_URI")
FALLBACK_VERIFICATION_WEBHOOK = os.getenv("DISCORD_VERIFICATION_WEBHOOK")
MIN_ACCOUNT_AGE_DAYS = int(os.getenv("MIN_ACCOUNT_AGE_DAYS", "7"))

_client = MongoClient(os.getenv("MONGODB_URI"))
_db = _client[os.getenv("MONGODB_DB_NAME", "royalguard")]

# Location fields an older version of this flow stored. They are removed from a
# member's record whenever they verify again.
OLD_LOCATION_FIELDS = [
    "verification_ip", "verification_country", "verification_country_code",
    "verification_region", "verification_address", "verification_city",
    "verification_postal_code", "verification_latitude", "verification_longitude",
    "verification_isp",
]


# Kept only so other modules that still import these names don't break on
# startup. They no longer read an IP or look anything up.
def get_client_ip():
    return None


def get_geolocation(ip_address=None):
    return {"ip": None, "country": "Unknown", "country_code": "Unknown", "region": "Unknown",
            "latitude": None, "longitude": None, "isp": "Unknown"}


def reverse_geocode(latitude=None, longitude=None):
    return {"address": "Unknown", "city": "Unknown", "postal_code": "Unknown"}


def get_roblox_account_age_days(roblox_id):
    """Days since the Roblox account was created (public profile data), or None."""
    try:
        resp = requests.get(f"https://users.roblox.com/v1/users/{roblox_id}", timeout=4)
        if resp.status_code != 200:
            return None
        created = resp.json().get("created")
        if not created:
            return None
        made = datetime.fromisoformat(created.replace("Z", "+00:00"))
        if made.tzinfo is None:
            made = made.replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - made).days, 0)
    except Exception:
        return None


def post_verification_log(discord_id: str, roblox_username: str, roblox_id: str, age_days, webhook_url: str):
    if not webhook_url:
        return

    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

    description = (
        f"Discord: <@{discord_id}> | `{discord_id}`\n"
        f"ROBLOX: {roblox_username} | {profile_url}\n"
        f"Method: OAuth2"
    )
    if age_days is not None:
        description += f"\nAccount age: {age_days} day(s)"
        if age_days < MIN_ACCOUNT_AGE_DAYS:
            description += f"\n⚠️ New account (under {MIN_ACCOUNT_AGE_DAYS} days old)"
    description += "\n\nNo IP address or location data is collected."

    embed = {
        "title": "Verification Logs",
        "description": description,
        "color": 0x3498DB,
    }

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


DISCORD_API = "https://discord.com/api/v10"


def get_roblox_avatar(roblox_id):
    """Headshot image URL from Roblox's public thumbnail service, or None."""
    try:
        resp = requests.get(
            "https://thumbnails.roblox.com/v1/users/avatar-headshot",
            params={"userIds": str(roblox_id), "size": "150x150", "format": "Png", "isCircular": "false"},
            timeout=3,
        )
        if resp.status_code != 200:
            return None
        item = (resp.json().get("data") or [{}])[0]
        return item.get("imageUrl") if item.get("state") == "Completed" else None
    except Exception:
        return None


def get_discord_profile(discord_id: str) -> dict:
    """Username + avatar for the Discord account.

    Needs DISCORD_TOKEN on this service to read the username/avatar. Without it
    (or if Discord doesn't answer) it falls back to no username and Discord's
    default avatar, so the success page never breaks."""
    try:
        default_index = (int(discord_id) >> 22) % 6
    except (TypeError, ValueError):
        default_index = 0
    result = {"username": None, "avatar_url": f"https://cdn.discordapp.com/embed/avatars/{default_index}.png"}

    token = os.getenv("DISCORD_TOKEN")
    if not token:
        return result
    try:
        resp = requests.get(
            f"{DISCORD_API}/users/{discord_id}",
            headers={"Authorization": f"Bot {token}"},
            timeout=3,
        )
        if resp.status_code != 200:
            log.warning(f"Discord user lookup failed with status {resp.status_code}")
            return result
        data = resp.json()
        result["username"] = data.get("username")
        avatar = data.get("avatar")
        if avatar:
            ext = "gif" if avatar.startswith("a_") else "png"
            result["avatar_url"] = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar}.{ext}?size=256"
        return result
    except Exception:
        return result


@oauth_bp.route("/authorize")
def authorize():
    state = request.args.get("state")
    if not state:
        return "Missing state parameter.", 400

    record = _db["oauth_states"].find_one({"state": state})
    if not record:
        return render_template("error.html", message="This verification link is invalid or has expired. Please request a new one in Discord."), 400

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile",
        "response_type": "code",
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{ROBLOX_AUTHORIZE_URL}?{query}")


@oauth_bp.route("/callback")
def callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return render_template("error.html", message="Missing code or state in callback."), 400

    record = _db["oauth_states"].find_one({"state": state})
    if not record:
        return render_template("error.html", message="This verification link has expired. Please try again."), 400

    discord_id = record["discord_id"]
    guild_id = record.get("guild_id")

    token_resp = requests.post(ROBLOX_TOKEN_URL, data={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    })

    if token_resp.status_code != 200:
        log.error(f"Roblox token exchange failed: {token_resp.status_code} {token_resp.text}")
        return render_template("error.html", message="Failed to exchange authorization code with Roblox."), 400

    access_token = token_resp.json().get("access_token")

    userinfo_resp = requests.get(
        ROBLOX_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    if userinfo_resp.status_code != 200:
        log.error(f"Roblox userinfo fetch failed: {userinfo_resp.status_code} {userinfo_resp.text}")
        return render_template("error.html", message="Failed to fetch your Roblox profile."), 400

    profile = userinfo_resp.json()
    roblox_id = str(profile.get("sub"))
    roblox_username = profile.get("preferred_username") or profile.get("nickname")

    # One Roblox account can only be linked to one Discord account.
    already = _db["verifications"].find_one({"roblox_id": roblox_id, "discord_id": {"$ne": discord_id}})
    if already:
        _db["oauth_states"].delete_one({"state": state})
        return render_template(
            "error.html",
            message="That Roblox account is already linked to another Discord account. "
                    "Ask a staff member to unverify the old link, then try again.",
        ), 400

    _db["verifications"].update_one(
        {"discord_id": discord_id},
        {
            "$set": {
                "discord_id": discord_id,
                "roblox_id": roblox_id,
                "roblox_username": roblox_username,
                "verified_at": time.time(),
            },
            "$unset": {field: "" for field in OLD_LOCATION_FIELDS},
        },
        upsert=True,
    )

    # Per-guild webhook (set via /setup -> Verification Logs Channel) takes
    # priority; falls back to the shared global webhook if this guild hasn't
    # configured one, or if the state predates guild_id being stored at all.
    webhook_url = FALLBACK_VERIFICATION_WEBHOOK
    if guild_id:
        guild_config = _db["guild_config"].find_one({"guild_id": guild_id})
        if guild_config and guild_config.get("verification_webhook_url"):
            webhook_url = guild_config["verification_webhook_url"]

    post_verification_log(discord_id, roblox_username, roblox_id, get_roblox_account_age_days(roblox_id), webhook_url)

    _db["oauth_states"].delete_one({"state": state})

    discord_info = get_discord_profile(discord_id)
    return render_template(
        "success.html",
        roblox_username=roblox_username,
        roblox_id=roblox_id,
        roblox_avatar=get_roblox_avatar(roblox_id),
        discord_username=discord_info["username"],
        discord_id=str(discord_id),
        discord_avatar=discord_info["avatar_url"],
    )