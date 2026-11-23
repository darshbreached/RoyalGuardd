"""
main.py
-------
Royal Guard bot entrypoint. Loads environment variables, connects to
MongoDB, registers persistent views, loads all cogs, and syncs slash
commands.

Run locally:   python main.py
Run on Railway: this is your "worker" process (see Procfile)
"""

import os
import asyncio
import logging

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("RoyalGuard")

from config import settings
from database.mongodb import db

INTENTS = discord.Intents.default()
INTENTS.members = True
INTENTS.message_content = True
INTENTS.invites = True

COGS = [
    "cogs.adminlevels",
    "cogs.groupbinds",
    "cogs.grouprole",
    "cogs.rankbinds",
    "cogs.update",
    "cogs.verification",
    "cogs.tickets",
    "cogs.panels",
    "cogs.bgcheck",
    "cogs.blcheck",
    "cogs.moderation",
    "cogs.setrank",
    "cogs.rankrequest",
    "cogs.reactionroles",
    "cogs.invites",
    "cogs.automod",
    "cogs.antinuke",
    "cogs.antiraid",
    "cogs.setup",
    "cogs.bmt",
    "cogs.nuke",
    "cogs.activitycheck",
    "cogs.acceptrequest",
    "cogs.warnings",
    "cogs.ssu",
    "cogs.devtools",
    "cogs.errorhandler",
    "cogs.globalban",
    "cogs.economy",
    "cogs.boosterroles",
    "cogs.loa",
    "cogs.dmannounce",
    "cogs.redeem",
    "cogs.ticketverify",
    "cogs.emailverify",
    "cogs.modulegate",
    "cogs.rowifigroups",
    "cogs.adminjobs",
]


class RoyalGuardBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=settings.COMMAND_PREFIX, intents=INTENTS, help_command=None)

    async def setup_hook(self):
        await db.ensure_indexes()
        log.info("MongoDB indexes ensured.")

        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.exception(f"Failed to load cog {cog}: {e}")

        from cogs.verification import VerificationView
        from cogs.tickets import ReportPanelView, OtherPanelView, CloseTicketView
        from cogs.rankrequest import RankRequestView
        from cogs.registerpanel import RegisterPanelView, DMInstructionsView

        self.add_view(VerificationView())
        self.add_view(ReportPanelView())
        self.add_view(OtherPanelView())
        self.add_view(CloseTicketView())
        self.add_view(RegisterPanelView())
        self.add_view(DMInstructionsView())

        pending_requests = await db.get_pending_rank_requests()
        for req in pending_requests:
            self.add_view(RankRequestView(req["_id"]))
        log.info(f"Re-registered {len(pending_requests)} pending rank request views.")

        synced = await self.tree.sync()
        log.info(f"Synced {len(synced)} global slash commands. Note: global command updates can take up to an hour to appear in all servers (Discord-side caching).")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Currently in {len(self.guilds)} server(s).")


async def main():
    env_token = os.getenv("DISCORD_TOKEN")

    async def saved_token():
        # A token saved from the website's admin page (encrypted in MongoDB).
        try:
            from utils.token_crypto import decrypt_token
            doc = await db.db["bot_config"].find_one({"_id": "main_token"})
            return decrypt_token(doc["encrypted_token"]) if doc else None
        except Exception as e:
            log.error(f"Could not read the saved bot token, using DISCORD_TOKEN instead: {type(e).__name__}")
            return None

    saved = await saved_token()
    tokens = []
    for t in (saved, env_token):
        if t and t not in tokens:
            tokens.append(t)
    if not tokens:
        raise RuntimeError("DISCORD_TOKEN is not set in the environment (.env)")

    try:
        from cogs.tenant_manager import TenantManager
        asyncio.create_task(TenantManager().run_forever())
        log.info("Tenant manager started.")
    except Exception as e:
        log.error(f"Tenant manager not started: {e}")

    for token in tokens:
        bot = RoyalGuardBot()
        try:
            async with bot:
                await bot.start(token)
            return
        except discord.LoginFailure:
            log.error("Discord rejected a bot token" + (" (the saved one), trying DISCORD_TOKEN" if token == saved and env_token and token != env_token else "") + ".")
    raise RuntimeError("Every bot token was rejected by Discord.")


if __name__ == "__main__":
    asyncio.run(main())