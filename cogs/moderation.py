"""
cogs/moderation.py
-------------------
Ban, kick, mute (Discord timeout), and unban commands. Every action posts
a confirmation embed publicly in the channel the command was used in, and
mirrors it to the configured "mod" log channel if one's been set via
/setlogchannel.

Deferred non-ephemeral on purpose: once an interaction is deferred with
ephemeral=True, Discord forces EVERY followup on that interaction to be
ephemeral too, regardless of what you pass to followup.send() afterward -
there's no way to override that per-followup. So these defer public, and
error paths explicitly pass ephemeral=True on their own followup instead.

/ban and /mute also DM the target a notice BEFORE the action executes -
DMing after a ban fails silently since Discord blocks DMs once you no
longer share a server with someone. The DM includes the real server name
(not a hardcoded one), an optional evidence link, and a link to the ban
appeal form (WEBSITE_BASE_URL + /appeal). If the user has DMs disabled,
the notice is skipped silently and the ban/mute still proceeds - a failed
DM is never a reason to abort the action.

NOTE: the appeal DM only covers THIS server. It does not claim a ban
applies to "all associated servers" - that would require a real
cross-server ban-sync system, which doesn't exist here. Ask if you want
that built as a separate feature.
"""

import os
import discord
from discord import app_commands
from discord.ext import commands
from datetime import timedelta

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level

WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://your-railway-app.up.railway.app")


async def _log_action(guild: discord.Guild, log_type: str, embed: discord.Embed):
    channel_id = await db.get_log_channel(guild.id, log_type)
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            await channel.send(embed=embed)


async def _send_action_dm(
    user: discord.Member,
    title: str,
    guild_name: str,
    reason: str,
    evidence: str = None,
    duration_text: str = None,
    include_appeal: bool = True,
):
    """Sends a DM notice before a moderation action executes. Never raises -
    if the user has DMs off or has blocked the bot, this fails silently and
    the calling command proceeds with the action regardless."""
    embed = embeds.error_embed(title, None) if "Ban" in title else embeds.warning_embed(title, None)
    embed.add_field(name="Origin", value=guild_name, inline=False)
    if duration_text:
        embed.add_field(name="Duration", value=duration_text, inline=False)

    reason_value = reason
    if evidence:
        reason_value += f"\n[Evidence]({evidence})"
    embed.add_field(name="Reason", value=reason_value, inline=False)

    if include_appeal:
        appeal_url = f"{WEBSITE_BASE_URL}/appeal"
        embed.add_field(name="Appeal", value=f"[Submit an appeal]({appeal_url})", inline=False)

    try:
        await user.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException):
        pass


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
    @app_commands.describe(user="The user to ban", reason="Reason for the ban", evidence="Optional link to evidence (screenshot, clip, etc.)")
    @require_level(50)
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", evidence: str = None):
        await interaction.response.defer()

        await _send_action_dm(
            user, "Ban Notice", interaction.guild.name, reason, evidence=evidence, include_appeal=True
        )

        try:
            await user.ban(reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to ban this user."), ephemeral=True
            )

        embed = embeds.success_embed("User Banned", f"Successfully banned user {user.mention} with reason: {reason}")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="The user to kick", reason="Reason for the kick")
    @require_level(20)
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
        await interaction.response.defer()
        try:
            await user.kick(reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to kick this user."), ephemeral=True
            )

        embed = embeds.success_embed("User Kicked", f"Successfully kicked user {user.mention} with reason: {reason}")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="mute", description="Timeout a member for a duration.")
    @app_commands.describe(user="The user to mute", minutes="Duration in minutes", reason="Reason for the mute", evidence="Optional link to evidence (screenshot, clip, etc.)")
    @require_level(20)
    async def mute(self, interaction: discord.Interaction, user: discord.Member, minutes: int, reason: str = "No reason provided", evidence: str = None):
        await interaction.response.defer()

        await _send_action_dm(
            user, "Mute Notice", interaction.guild.name, reason, evidence=evidence,
            duration_text=f"{minutes} minutes", include_appeal=True
        )

        try:
            await user.timeout(timedelta(minutes=minutes), reason=f"{reason} | By {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to mute this user."), ephemeral=True
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
        await interaction.response.defer()
        try:
            await user.timeout(None, reason=f"Unmuted by {interaction.user}")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to unmute this user."), ephemeral=True
            )

        embed = embeds.success_embed("User Unmuted", f"Successfully unmuted {user.mention}.")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)

    @app_commands.command(name="unban", description="Unban a user by their Discord ID.")
    @app_commands.describe(user_id="The Discord user ID to unban", reason="Reason for the unban")
    @require_level(50)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
        await interaction.response.defer()
        try:
            user_obj = discord.Object(id=int(user_id))
            await interaction.guild.unban(user_obj, reason=f"{reason} | By {interaction.user}")
        except (discord.NotFound, ValueError):
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "That user is not banned, or the ID is invalid."), ephemeral=True
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Failed", "I don't have permission to unban."), ephemeral=True
            )

        embed = embeds.success_embed("User Unbanned", f"Successfully unbanned user <@{user_id}>.")
        await interaction.followup.send(embed=embed)
        await _log_action(interaction.guild, "mod", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
