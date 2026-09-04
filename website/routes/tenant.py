"""
website/routes/tenant.py
--------------------------
Self-serve tenant bot registration via the website. Requires Discord OAuth2
login to identify the submitter - otherwise anyone could claim ownership of
any Discord user ID.

Auto-approve: a submission is written directly into the `tenants` collection
as status="active" - the same collection/shape tenant_manager.py already
reads from. There is no review queue; nothing lands in pending_tenants
anymore. If tenant_manager.py is running, a submitted bot goes live as soon
as it picks up the new document.
"""

import os
import time
import logging
import secrets
import requests
from flask import Blueprint, request, redirect, render_template, session, url_for

from pymongo import MongoClient
from utils.token_crypto import encrypt_token

log = logging.getLogger("RoyalGuard")

tenant_bp = Blueprint("tenant", __name__)

_client = MongoClient(os.getenv("MONGODB_URI"))
_db = _client[os.getenv("MONGODB_DB_NAME", "royalguard")]

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_USER_URL = "https://discord.com/api/users/@me"


@tenant_bp.route("/tenant/login")
def tenant_login():
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return render_template("error.html", message="Tenant registration is not configured yet. Contact the server owner."), 500

    state = secrets.token_urlsafe(24)
    session["discord_oauth_state"] = state

    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify",
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
    return redirect(f"{DISCORD_AUTHORIZE_URL}?{query}")


@tenant_bp.route("/tenant/callback")
def tenant_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state or state != session.get("discord_oauth_state"):
        return render_template("error.html", message="This login link is invalid or has expired. Please try again."), 400

    session.pop("discord_oauth_state", None)

    token_resp = requests.post(
        DISCORD_TOKEN_URL,
        data={
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=10,
    )

    if token_resp.status_code != 200:
        log.error(f"Discord token exchange failed: {token_resp.status_code} {token_resp.text}")
        return render_template("error.html", message="Failed to log in with Discord. Please try again."), 400

    access_token = token_resp.json().get("access_token")

    user_resp = requests.get(
        DISCORD_USER_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )

    if user_resp.status_code != 200:
        return render_template("error.html", message="Failed to fetch your Discord account details. Please try again."), 400

    user_data = user_resp.json()
    session["discord_id"] = user_data["id"]
    session["discord_username"] = user_data.get("username", "Unknown")

    return redirect(url_for("tenant.tenant_register"))


@tenant_bp.route("/tenant/logout")
def tenant_logout():
    session.pop("discord_id", None)
    session.pop("discord_username", None)
    return redirect(url_for("tenant.tenant_register"))


@tenant_bp.route("/tenant/register", methods=["GET"])
def tenant_register():
    discord_id = session.get("discord_id")
    discord_username = session.get("discord_username")

    if not discord_id:
        return render_template("tenant_register.html", logged_in=False)

    form_token = secrets.token_urlsafe(24)
    session["tenant_form_token"] = form_token

    return render_template(
        "tenant_register.html",
        logged_in=True,
        discord_username=discord_username,
        form_token=form_token,
    )


@tenant_bp.route("/tenant/submit", methods=["POST"])
def tenant_submit():
    discord_id = session.get("discord_id")
    discord_username = session.get("discord_username")

    if not discord_id:
        return render_template("error.html", message="You must log in with Discord before submitting a bot."), 401

    submitted_token = request.form.get("form_token")
    if not submitted_token or submitted_token != session.get("tenant_form_token"):
        return render_template("error.html", message="This form has expired. Please refresh and try again."), 400
    session.pop("tenant_form_token", None)

    bot_token = (request.form.get("bot_token") or "").strip()
    bot_name = (request.form.get("bot_name") or "").strip()[:100]

    if not bot_token:
        return render_template("error.html", message="A bot token is required."), 400

    encrypted = encrypt_token(bot_token)

    # Auto-approve: straight into `tenants` as active, not the pending queue.
    _db["tenants"].insert_one({
        "owner_discord_id": int(discord_id),
        "encrypted_token": encrypted,
        "bot_name": bot_name,
        "status": "active",
        "last_error": None,
        "created_at": time.time(),
        "source": "website",
    })

    return render_template("tenant_success.html", discord_username=discord_username)
