"""
cogs/globalban.py
------------------
Opt-in cross-server ban enforcement.

/globalban    - bans a user from every server currently subscribed to the
                global ban list. Owner level only (BOT_OWNER_ID), since a
                global ban affects every subscribed server at once.
/globalunban  - reverses one.
/globalbanconfig subscribe|unsubscribe
              - lets each server's own high-level admin opt that specific
                server in or out. A server that never subscribes is never
                affected by any global ban, full stop.

Subscribing backfills every existing global ban into that guild immediately,
so a newly-opted-in server doesn't have to wait for the next new ban to
benefit from the list.
"""

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level, require_owner


class GlobalBan(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _ban_in_subscribed_guilds(self, discord_id: int, reason: str, exclude_guild_id: int = None):
        subscribed_ids = await db.list_subscribed_guild_ids()
        results = {"banned": [], "failed": []}
        for guild_id_str in subscribed_ids:
            guild_id = int(guild_id_str)
            if guild_id == exclude_guild_id:
                continue
            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            try:
                await guild.ban(discord.Object(id=discord_id), reason=reason, delete_message_days=0)
                results["banned"].append(guild.name)
            except Exception:
                results["failed"].append(guild.name)
        return results

    async def _unban_in_subscribed_guilds(self, discord_id: int):
        subscribed_ids = await db.list_subscribed_guild_ids()
        for guild_id_str in subscribed_ids:
            guild = self.bot.get_guild(int(guild_id_str))
            if not guild:
                continue
            try:
                await guild.unban(discord.Object(id=discord_id), reason="Global ban lifted")
            except Exception:
                pass

    @app_commands.command(name="globalban", description="Ban a user from every server subscribed to the global ban list.")
    @app_commands.describe(user="The user to ban", reason="Why this user is being globally banned")
    @require_owner()
    async def globalban(self, interaction: discord.Interaction, user: discord.User, reason: str):
        await interaction.response.defer()

        await db.add_global_ban(
            discord_id=user.id,
            reason=reason,
            banned_by=interaction.user.id,
            source_guild_id=interaction.guild.id,
            source_guild_name=interaction.guild.name,
        )

        # Ban in the current guild too if not already subscribed - the
        # action was initiated here, so it applies here regardless.
        try:
            await interaction.guild.ban(user, reason=reason, delete_message_days=0)
        except Exception:
            pass

        results = await self._ban_in_subscribed_guilds(user.id, reason, exclude_guild_id=interaction.guild.id)

        summary = f"Banned in {len(results['banned'])} subscribed server(s)."
        if results["failed"]:
            summary += f" Failed in {len(results['failed'])}: {', '.join(results['failed'])}"

        await interaction.followup.send(
            embed=embeds.success_embed("Global Ban Issued", f"{user.mention} has been globally banned.\nReason: {reason}\n\n{summary}")
        )

    @app_commands.command(name="globalunban", description="Lift a global ban, unbanning the user from every subscribed server.")
    @app_commands.describe(user_id="The Discord user ID to unban")
    @require_owner()
    async def globalunban(self, interaction: discord.Interaction, user_id: str):
        await interaction.response.defer()

        existing = await db.get_global_ban(int(user_id))
        if not existing:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Found", "That user doesn't have an active global ban.")
            )

        await db.remove_global_ban(int(user_id))
        await self._unban_in_subscribed_guilds(int(user_id))

        await interaction.followup.send(
            embed=embeds.success_embed("Global Ban Lifted", f"<@{user_id}> has been unbanned from every subscribed server.")
        )

    globalbanconfig_group = app_commands.Group(name="globalbanconfig", description="Opt this server in or out of the global ban list.")

    @globalbanconfig_group.command(name="subscribe", description="Subscribe this server to the global ban list.")
    @require_level(90)
    async def subscribe(self, interaction: discord.Interaction):
        already = await db.is_guild_subscribed_to_global_bans(interaction.guild.id)
        if already:
            return await interaction.response.send_message(
                embed=embeds.info_embed("Already Subscribed", "This server is already subscribed to the global ban list.")
            )

        await interaction.response.defer()
        await db.subscribe_guild_to_global_bans(interaction.guild.id)

        # Backfill: apply every existing global ban to this guild immediately.
        global_bans = await db.list_global_bans()
        banned_count = 0
        for ban in global_bans:
            try:
                await interaction.guild.ban(
                    discord.Object(id=int(ban["discord_id"])),
                    reason=f"Global ban backfill: {ban['reason']}",
                    delete_message_days=0,
                )
                banned_count += 1
            except Exception:
                pass

        await interaction.followup.send(
            embed=embeds.success_embed(
                "Subscribed",
                f"This server now enforces the global ban list.\n{banned_count} existing global ban(s) applied."
            )
        )

    @globalbanconfig_group.command(name="unsubscribe", description="Unsubscribe this server from the global ban list.")
    @require_level(90)
    async def unsubscribe(self, interaction: discord.Interaction):
        await db.unsubscribe_guild_from_global_bans(interaction.guild.id)
        await interaction.response.send_message(
            embed=embeds.success_embed("Unsubscribed", "This server no longer enforces the global ban list. Existing bans already applied are not automatically lifted.")
        )

    @globalbanconfig_group.command(name="status", description="Check whether this server is subscribed to the global ban list.")
    async def status(self, interaction: discord.Interaction):
        subscribed = await db.is_guild_subscribed_to_global_bans(interaction.guild.id)
        label = "subscribed" if subscribed else "not subscribed"
        await interaction.response.send_message(
            embed=embeds.info_embed("Global Ban Status", f"This server is **{label}** to the global ban list.")
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalBan(bot))
