"""
website/routes/appeal.py
--------------------------
Public ban-appeal form. Submissions are posted as a Discord embed to a
webhook (APPEAL_WEBHOOK_URL env var) rather than written to MongoDB -
staff triage appeals directly in Discord, no separate admin panel needed.

No Discord OAuth required to submit an appeal, since a banned user cannot
complete this bot's own OAuth flow through the guild they're banned from -
they self-report their Discord ID/username instead. This trades a small
amount of spoofing risk for actually being usable by a banned user; staff
reviewing the appeal in Discord can verify identity manually before acting.
"""

import os
from datetime import datetime, timezone

import requests
from flask import Blueprint, render_template, request

appeal_bp = Blueprint("appeal", __name__)

APPEAL_WEBHOOK_URL = os.getenv("APPEAL_WEBHOOK_URL")


@appeal_bp.route("/appeal", methods=["GET"])
def appeal_form():
    return render_template("appeal.html", submitted=False, error=None)


@appeal_bp.route("/appeal", methods=["POST"])
def appeal_submit():
    discord_id = request.form.get("discord_id", "").strip()
    discord_username = request.form.get("discord_username", "").strip()
    roblox_username = request.form.get("roblox_username", "").strip()
    ban_reason = request.form.get("ban_reason", "").strip()
    appeal_message = request.form.get("appeal_message", "").strip()

    if not discord_id or not discord_id.isdigit():
        return render_template("appeal.html", submitted=False, error="Please enter a valid Discord ID (numbers only - right-click your name in Discord with Developer Mode on and choose 'Copy User ID').")

    if not appeal_message:
        return render_template("appeal.html", submitted=False, error="Please explain why your ban should be reconsidered.")

    if not APPEAL_WEBHOOK_URL:
        return render_template("appeal.html", submitted=False, error="Appeals are temporarily unavailable. Please contact staff directly.")

    embed = {
        "title": "New Ban Appeal",
        "color": 0xE74C3C,
        "fields": [
            {
                "name": "Discord User",
                "value": f"<@{discord_id}> (`{discord_id}`)" + (f"\n{discord_username}" if discord_username else ""),
                "inline": False,
            },
            {"name": "Roblox Username", "value": roblox_username or "Not provided", "inline": False},
            {"name": "Original Ban Reason", "value": ban_reason[:1024] or "Not provided", "inline": False},
            {"name": "Appeal Message", "value": appeal_message[:1024], "inline": False},
        ],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        resp = requests.post(APPEAL_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if resp.status_code not in (200, 204):
            return render_template(
                "appeal.html", submitted=False,
                error="Something went wrong submitting your appeal. Please try again or contact staff."
            )
    except requests.RequestException:
        return render_template(
            "appeal.html", submitted=False,
            error="Something went wrong submitting your appeal. Please try again or contact staff."
        )

    return render_template("appeal.html", submitted=True, error=None)
