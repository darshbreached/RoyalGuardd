"""grenadier/main.py — Grenadier Bot entry point.

Run from the repo root:   python -m grenadier.main
Needs the GRENADIER_TOKEN environment variable (a separate Discord application
from Royal Guard — never reuse DISCORD_TOKEN here).
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("Grenadier")

COGS = [
    "grenadier.music",
]


class GrenadierBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()  # includes voice_states; no privileged intents needed
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            activity=discord.Activity(type=discord.ActivityType.listening, name="/play"),
        )

    async def setup_hook(self):
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info(f"Loaded cog: {cog}")
            except Exception as e:
                log.exception(f"Failed to load cog {cog}: {e}")

        synced = await self.tree.sync()
        log.info(f"Synced {len(synced)} global slash commands (can take up to an hour to show everywhere).")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} (ID: {self.user.id})")
        log.info(f"Currently in {len(self.guilds)} server(s).")


async def main():
    token = os.getenv("GRENADIER_TOKEN")
    if not token:
        raise RuntimeError("GRENADIER_TOKEN is not set in the environment")
    bot = GrenadierBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
