"""website/routes/site.py: public Royal Guard marketing pages."""
import os
from flask import Blueprint, render_template
from utils.modules import MODULES, CATEGORIES

site_bp = Blueprint("site", __name__)


def invite_url():
    cid = os.getenv("DISCORD_CLIENT_ID", "")
    if not cid:
        return "#"
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={cid}&permissions=8&scope=bot%20applications.commands"
    )


@site_bp.route("/")
def home():
    visible = [m for m in MODULES if not m["hidden"]]
    grouped = {c: [m for m in visible if m["category"] == c] for c in CATEGORIES}
    return render_template("rg_home.html", grouped=grouped, total=len(visible), invite_url=invite_url())
