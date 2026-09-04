"""
website/routes/oauth.py
------------------------
Handles the Roblox OAuth2 authorization + callback flow.

/authorize?state=<state>   -> redirects the user to Roblox's consent screen
/callback?code=...&state=... -> exchanges the code for a token, fetches the
                                  Roblox profile, geolocates the requester's
                                  IP, writes everything to MongoDB, and posts
                                  a "Verification Logs" embed to Discord via
                                  webhook.
"""

import os
import time
import logging
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
VERIFICATION_WEBHOOK = os.getenv("DISCORD_VERIFICATION_WEBHOOK")

_client = MongoClient(os.getenv("MONGODB_URI"))
_db = _client[os.getenv("MONGODB_DB_NAME", "royalguard")]


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr


def get_geolocation(ip_address: str):
    fallback = {
        "ip": ip_address, "country": "Unknown", "country_code": "Unknown",
        "region": "Unknown", "latitude": None, "longitude": None, "isp": "Unknown",
    }
    try:
        response = requests.get(f"https://ipinfo.io/{ip_address}/json", timeout=5)
        if response.status_code != 200:
            return fallback
        data = response.json()
        lat, lon = None, None
        loc = data.get("loc")
        if loc and "," in loc:
            lat_str, lon_str = loc.split(",")
            lat, lon = float(lat_str), float(lon_str)
        return {
            "ip": ip_address,
            "country": data.get("country", "Unknown"),
            "country_code": data.get("country", "Unknown"),
            "region": data.get("region", "Unknown"),
            "latitude": lat,
            "longitude": lon,
            "isp": data.get("org", "Unknown"),
        }
    except Exception:
        return fallback


def post_verification_log(discord_id: str, roblox_username: str, roblox_id: str, geo: dict):
    if not VERIFICATION_WEBHOOK:
        return

    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

    description = (
        f"Discord: <@{discord_id}> | `{discord_id}`\n"
        f"ROBLOX: {roblox_username} | {profile_url}\n"
        f"Method: OAuth2\n\n"
        f"IP: {geo['ip']}\n"
        f"Country: {geo['country']}\n"
        f"Country Code: {geo['country_code']}\n"
        f"Region: {geo['region']}\n"
        f"Latitude: {geo['latitude']}\n"
        f"Longitude: {geo['longitude']}\n"
        f"Internet Service Provider: {geo['isp']}"
    )

    embed = {
        "title": "Verification Logs",
        "description": description,
        "color": 0x3498DB,
    }

    try:
        requests.post(VERIFICATION_WEBHOOK, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


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
    roblox_id = profile.get("sub")
    roblox_username = profile.get("preferred_username") or profile.get("nickname")

    client_ip = get_client_ip()
    geo = get_geolocation(client_ip)

    _db["verifications"].update_one(
        {"discord_id": discord_id},
        {"$set": {
            "discord_id": discord_id,
            "roblox_id": str(roblox_id),
            "roblox_username": roblox_username,
            "verified_at": time.time(),
            "verification_ip": geo["ip"],
            "verification_country": geo["country"],
            "verification_country_code": geo["country_code"],
            "verification_region": geo["region"],
            "verification_latitude": geo["latitude"],
            "verification_longitude": geo["longitude"],
            "verification_isp": geo["isp"],
        }},
        upsert=True,
    )

    post_verification_log(discord_id, roblox_username, str(roblox_id), geo)

    _db["oauth_states"].delete_one({"state": state})

    return render_template("success.html", roblox_username=roblox_username)
