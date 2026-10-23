"""
cogs/setrank.py
----------------
Manually sets a member's Roblox group rank (writes back to Roblox via the
service account cookie configured in ROBLOX_SECURITY_COOKIE), then syncs
their Discord roles/nickname to match, and posts a promotion log matching
the "Archive Promotion Logs" style.

The rank_id parameter uses autocomplete: once group_id is filled in, typing
in rank_id shows a live dropdown of that group's actual rank names pulled
from Roblox, so staff never need to know raw rank numbers.
"""

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from cogs.update import sync_member_roles


class SetRank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def rank_autocomplete(self, interaction: discord.Interaction, current: str):
        """Populates rank_id choices from the group_id the staff member already typed."""
        group_id = interaction.namespace.group_id
        if not group_id:
            return [app_commands.Choice(name="Type a group_id first", value=0)]

        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return [app_commands.Choice(name="Invalid group_id", value=0)]

        roles = await roblox.get_group_roles(group_id)
        if not roles:
            return [app_commands.Choice(name="No ranks found for this group", value=0)]

        current_lower = (current or "").lower()
        matches = [
            r for r in roles
            if current_lower in r["name"].lower()
        ]

        # Discord allows a max of 25 autocomplete choices
        return [
            app_commands.Choice(name=f"{r['name']} (Rank {r['rank']})", value=r["rank"])
            for r in matches[:25]
        ]

    @app_commands.command(name="setrank", description="Set a user's Roblox group rank.")
    @app_commands.describe(
        user="Discord user to rank",
        group_id="Roblox group ID",
        rank_id="The rank to set them to",
    )
    @app_commands.autocomplete(rank_id=rank_autocomplete)
    @require_level(30)
    async def setrank(self, interaction: discord.Interaction, user: discord.Member, group_id: int, rank_id: int):
        await interaction.response.defer()

        verification = await db.get_verification(user.id)
        if not verification:
            return await interaction.followup.send(
                embed=embeds.error_embed("Warning - Not Verified", f"{user.mention} has not verified their Roblox account.")
            )

        roblox_id = int(verification["roblox_id"])
        group_info = await roblox.get_group_info(group_id)
        group_name = group_info.get("name", "Unknown Group") if group_info else "Unknown Group"

        roles = await roblox.get_group_roles(group_id)
        rank_name = next((r["name"] for r in roles if r["rank"] == rank_id), f"Rank {rank_id}")
        rank_role_data = next((r for r in roles if r["rank"] == rank_id), None)

        success = False
        error_message = None
        if rank_role_data:
            try:
                success = await roblox.set_group_rank(group_id, roblox_id, rank_role_data["id"], guild_id=interaction.guild.id)
            except RuntimeError as e:
                success = False
                error_message = str(e)

        if not success:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "Rank Change Failed",
                    error_message or "Could not update the rank in Roblox. Make sure ROBLOX_SECURITY_COOKIE is configured and the service account outranks the target rank."
                )
            )

        await sync_member_roles(interaction.guild, user, roblox_id)

        embed = embeds.success_embed(
            "Rank Updated",
            f"Successfully set the rank of **{verification['roblox_username']}** to **{rank_name}** in **{group_name}**."
        )
        embed.set_footer(
            text=f"Set by {interaction.user}",
            icon_url=interaction.user.display_avatar.url,
        )
        await interaction.followup.send(embed=embed)

        log_embed = embeds.info_embed(
            "Archive Promotion Logs",
            f"**{interaction.user}** successfully set the rank of **{verification['roblox_username']}** "
            f"to **{rank_name}** in **{group_name}**."
        )
        channel_id = await db.get_log_channel(interaction.guild.id, "rank")
        if channel_id:
            channel = interaction.guild.get_channel(int(channel_id))
            if channel:
                await channel.send(embed=log_embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetRank(bot))
