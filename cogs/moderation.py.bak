"""
cogs/moderation.py
-------------------
Ban, kick, mute (Discord timeout), and unban commands. Every action posts
a confirmation embed and, if a log channel has been configured for "mod"
via /setlogchannel, mirrors that same embed there.
"""

import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level


async def _log_action(guild: discord.Guild, log_type: str, embed: discord.Embed):
    channel_id = await db.get_log_channel(guild.id, log_type)
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setlogchannel", description="Set a log channel for moderation/rank/update events.")
    @app_commands.describe(log_type="Which type of log this channel is for", channel="The channel to send logs to")
    @app_commands.choices(log_type=[
        app_commands.Choice(name="Moderation", value="mod"),
        app_commands.Choice(name="Rank Changes", value="rank"),
        app_commands.Choice(name="Role Updates", value="update"),
    ])
    @require_level(50)
    async def setlogchannel(self, interaction: discord.Interaction, log_type: app_commands.Choice[str], channel: discord.TextChannel):
        await db.set_log_channel(interaction.guild.id, log_type.value, channel.id)
        await interaction.response.send_message(
            embed=embeds.success_embed("Log Channel Set", f"{log_type.name} logs will now be sent to {channel.mention}."),
            ephemeral=True,
        )

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(user="The user to ban", reason="Reason for the ban")
    @require_level(50)
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            await user.ban(reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to ban this user.")
            )

        embed = embeds.success_embed("User Banned", f"Successfully banned user {user.mention} with reason: {reason}")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="The user to kick", reason="Reason for the kick")
    @require_level(20)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            await user.kick(reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to kick this user.")
            )

        embed = embeds.success_embed("User Kicked", f"Successfully kicked user {user.mention} with reason: {reason}")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="mute", description="Timeout a member for a duration.")
    @app_commands.describe(user="The user to mute", minutes="Duration in minutes", reason="Reason for the mute")
    @require_level(20)
    async def mute(self, interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            await user.timeout(timedelta(minutes=minutes), reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to mute this user.")
            )

        embed = embeds.success_embed(
            "User Muted", f"Successfully muted {user.mention} for **{minutes} minutes** with reason: {reason}"
        )
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="unmute", description="Remove a timeout from a member.")
    @app_commands.describe(user="The user to unmute")
    @require_level(20)
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)
        try:
            await user.timeout(None, reason=f"Unmuted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to unmute this user.")
            )

        embed = embeds.success_embed("User Unmuted", f"Successfully unmuted {user.mention}.")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="unban", description="Unban a user by their Discord ID.")
    @app_commands.describe(user_id="The Discord user ID to unban", reason="Reason for the unban")
    @require_level(50)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        await interaction.response.defer(ephemeral=True)
        try:
            user_obj = discord.Object(id=int(user_id))
            await interaction.guild.unban(user_obj, reason=f"{reason} | By {interaction.user}")
        except (discord.NotFound, ValueError):
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "That user is not banned, or the ID is invalid.")
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to unban.")
            )

        embed = embeds.success_embed("User Unbanned", f"Successfully unbanned user <@{user_id}>.")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))