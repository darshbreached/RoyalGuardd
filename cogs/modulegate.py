"""
cogs/modulegate.py
Enforces per-server module toggles set from the website dashboard.
A disabled module's slash and prefix commands are blocked in that server.
Fails open: any error means the command is allowed.
"""
import time
import logging

import discord
from discord.ext import commands

from database.mongodb import db
from utils.modules import COG_TO_KEY, BY_KEY

log = logging.getLogger("RoyalGuard")
TTL = 30
_CACHE = {}


async def _disabled(guild_id: int) -> set:
    hit = _CACHE.get(guild_id)
    now = time.time()
    if hit and hit[0] > now:
        return hit[1]
    cfg = await db.get_guild_config(guild_id)
    off = {k for k, v in (cfg.get("modules") or {}).items() if v is False}
    _CACHE[guild_id] = (now + TTL, off)
    return off


async def _blocked_name(guild, command):
    if guild is None or command is None:
        return None
    key = COG_TO_KEY.get(getattr(getattr(command, "callback", None), "__module__", "") or "")
    if not key:
        return None
    try:
        return BY_KEY[key]["name"] if key in await _disabled(guild.id) else None
    except Exception as e:
        log.warning(f"modulegate lookup failed, allowing command: {e}")
        return None


async def tree_check(interaction: discord.Interaction) -> bool:
    if interaction.type is not discord.InteractionType.application_command:
        return True
    name = await _blocked_name(interaction.guild, interaction.command)
    if not name:
        return True
    msg = f"**{name}** is turned off in this server."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass
    return False


async def prefix_check(ctx: commands.Context) -> bool:
    name = await _blocked_name(ctx.guild, ctx.command)
    if not name:
        return True
    try:
        await ctx.send(f"**{name}** is turned off in this server.")
    except Exception:
        pass
    return False


class ModuleGate(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_check(prefix_check)
        bot.tree.interaction_check = tree_check

    async def cog_unload(self):
        self.bot.remove_check(prefix_check)


async def setup(bot: commands.Bot):
    await bot.add_cog(ModuleGate(bot))
