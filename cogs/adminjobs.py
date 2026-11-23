"""
cogs/adminjobs.py
Carries out the jobs queued by the owner-only /admin page on the website:
post a panel, send a message, or restart the bot. Only the main bot loads this.
Jobs live in the "admin_jobs" collection.
"""

import os
import time
import asyncio
import logging

import discord
from discord.ext import commands, tasks

from database.mongodb import db
from utils import embeds
from config import settings

log = logging.getLogger("RoyalGuard")


def _panel_verification():
    from cogs.verification import VerificationView
    embed = embeds.base_embed()
    embed.title = settings.VERIFICATION_PANEL_TITLE
    embed.description = settings.VERIFICATION_PANEL_DESCRIPTION
    embed.set_thumbnail(url=settings.BOT_ICON_URL)
    return embed, VerificationView()


def _panel_tickets(kind: str):
    from cogs.tickets import ReportPanelView, OtherPanelView
    embed = embeds.base_embed()
    embed.title = settings.TICKET_PANEL_TITLE
    embed.description = settings.TICKET_PANEL_DESCRIPTION
    embed.set_thumbnail(url=settings.BOT_ICON_URL)
    return embed, (ReportPanelView() if kind == "report" else OtherPanelView())


PANELS = {
    "verification": _panel_verification,
    "tickets_report": lambda: _panel_tickets("report"),
    "tickets_other": lambda: _panel_tickets("other"),
}


class AdminJobs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tick.start()

    async def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=5)
    async def tick(self):
        try:
            while True:
                job = await db.db["admin_jobs"].find_one_and_update(
                    {"status": "pending"},
                    {"$set": {"status": "running", "started_at": time.time()}},
                    sort=[("created_at", 1)],
                )
                if not job:
                    break
                await self._run(job)
        except Exception as e:
            log.error(f"Admin job loop error: {type(e).__name__}: {e}")

    @tick.before_loop
    async def _before_tick(self):
        await self.bot.wait_until_ready()

    async def _channel(self, channel_id):
        channel = self.bot.get_channel(int(channel_id)) or await self.bot.fetch_channel(int(channel_id))
        if not hasattr(channel, "send"):
            raise ValueError("That channel can't receive messages.")
        return channel

    async def _post(self, kind: str, p: dict) -> str:
        channel = await self._channel(p["channel_id"])
        if kind == "post_panel":
            builder = PANELS.get(p.get("panel"))
            if builder is None:
                raise ValueError("Unknown panel.")
            embed, view = builder()
            message = await channel.send(embed=embed, view=view)
        elif kind == "send_message":
            embed = embeds.base_embed()
            embed.title = p.get("title") or None
            embed.description = p["description"]
            message = await channel.send(embed=embed)
        else:
            raise ValueError("Unknown job type.")
        return message.jump_url

    async def _run(self, job: dict):
        kind, params = job.get("type"), job.get("params") or {}
        jobs = db.db["admin_jobs"]
        if kind == "restart":
            await jobs.update_one({"_id": job["_id"]}, {"$set": {"status": "done", "result": "Restarting...", "finished_at": time.time()}})
            log.warning("Restart requested from the admin page.")
            await asyncio.sleep(1)
            os._exit(1)  # non-zero exit so the host restarts the service
        status, result = "done", ""
        try:
            result = await self._post(kind, params)
        except discord.Forbidden:
            status, result = "error", "The bot doesn't have permission to send in that channel."
        except discord.NotFound:
            status, result = "error", "Channel not found. Check the ID and that the bot is in that server."
        except Exception as e:
            status, result = "error", f"{type(e).__name__}: {e}"[:300]
        await jobs.update_one({"_id": job["_id"]}, {"$set": {"status": status, "result": result, "finished_at": time.time()}})


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminJobs(bot))