"""
website/app.py
---------------
Flask backend that hosts the Roblox OAuth2 verification flow. Deployed
as a separate Railway service (or the "web" process type) alongside the
bot's "worker" process. Both share the same MongoDB Atlas cluster, so
verification data written here is instantly visible to the bot.
"""

import os
from flask import Flask, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder="templates")
app.secret_key = os.getenv("WEBSITE_SECRET_KEY", "dev-secret-change-me")

from website.routes.oauth import oauth_bp
app.register_blueprint(oauth_bp)

from website.routes.tenant import tenant_bp
app.register_blueprint(tenant_bp)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    return {"status": "healthy"}


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", message="That page doesn't exist."), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", message="Something went wrong on our end. Please try again."), 500


if __name__ == "__main__":
    port = int(os.getenv("WEBSITE_PORT", 8080))
    app.run(host="0.0.0.0", port=port)