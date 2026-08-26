"""
cogs/warnings.py
------------------
/warn            - issue a warning to a member, DM them a notice, log it
/warnings        - view a member's warning history
/clearwarnings   - wipe a member's entire warning history

Warnings are stored directly in a "warnings" MongoDB collection (via
db.warnings, same direct-collection-access pattern already used for
db.rankbinds in cogs/rankbinds.py) rather than through a dedicated helper
method - no changes to database/mongodb.py needed.

Each warning document: {guild_id, user_id, moderator_id, reason, timestamp}
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timezone

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level


async def _log_action(guild: discord.Guild, log_type: str, embed: discord.Embed):
    channel_id = await db.get_log_channel(guild.id, log_type)
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)


class Warnings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="warn", description="Issue a warning to a member.")
    @app_commands.describe(user="The user to warn", reason="Reason for the warning")
    @require_level(20)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)

        warning_doc = {
            "guild_id": str(interaction.guild.id),
            "user_id": str(user.id),
            "moderator_id": str(interaction.user.id),
            "reason": reason,
            "timestamp": datetime.now(timezone.utc),
        }
        await db.warnings.insert_one(warning_doc)

        warning_count = await db.warnings.count_documents({
            "guild_id": str(interaction.guild.id),
            "user_id": str(user.id),
        })

        dm_embed = embeds.warning_embed("Warning Notice", None)
        dm_embed.add_field(name="Origin", value=interaction.guild.name, inline=False)
        dm_embed.add_field(name="Reason", value=reason, inline=False)
        dm_embed.add_field(name="Warning Count", value=f"This is warning **#{warning_count}** on record.", inline=False)
        try:
            await user.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

        embed = embeds.success_embed(
            "User Warned",
            f"Successfully warned {user.mention} with reason: {reason}\nThis is their **#{warning_count}** warning."
        )
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="warnings", description="View a member's warning history.")
    @app_commands.describe(user="The user to check")
    @require_level(10)
    async def warnings_view(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)

        docs = await db.warnings.find({
            "guild_id": str(interaction.guild.id),
            "user_id": str(user.id),
        }).sort("timestamp", -1).to_list(length=25)

        if not docs:
            return await interaction.followup.send(
                embed=embeds.info_embed("No Warnings", f"{user.mention} has no warnings on record.")
            )

        lines = []
        for i, w in enumerate(docs, start=1):
            ts = w["timestamp"].strftime("%Y-%m-%d %H:%M UTC") if isinstance(w["timestamp"], datetime) else str(w["timestamp"])
            lines.append(f"**#{i}** - <@{w['moderator_id']}> - {ts}\n{w['reason']}")

        embed = embeds.info_embed(f"Warnings for {user}", "\n\n".join(lines))
        embed.set_footer(text=f"Showing up to 25 most recent warnings")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="clearwarnings", description="Clear a member's entire warning history.")
    @app_commands.describe(user="The user whose warnings to clear")
    @require_level(30)
    async def clearwarnings(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)

        result = await db.warnings.delete_many({
            "guild_id": str(interaction.guild.id),
            "user_id": str(user.id),
        })

        embed = embeds.success_embed(
            "Warnings Cleared", f"Removed **{result.deleted_count}** warning(s) for {user.mention}."
        )
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Warnings(bot))
