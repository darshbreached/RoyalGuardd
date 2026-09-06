"""
cogs/rankrequest.py
--------------------
/rankrequestconfig    - sets which role can approve/deny, and which
                        channel requests get posted to
/rank request         - lets a verified member request a rank change;
                        posts an Approve/Deny embed to the configured
                        channel, gated to members with the approver role
"""

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from cogs.update import sync_member_roles


async def _has_approver_role(interaction: discord.Interaction) -> bool:
    config = await db.get_rank_request_config(interaction.guild.id)
    role_id = config.get("approver_role_id")
    if not role_id:
        return False
    role = interaction.guild.get_role(int(role_id))
    return role is not None and role in interaction.user.roles


class RankRequestView(discord.ui.View):
    """One instance per pending request; custom_id encodes the request id
    so these remain clickable even after a bot restart, once re-registered
    in main.py's setup_hook from the pending requests stored in Mongo."""

    def __init__(self, request_id: str):
        super().__init__(timeout=None)
        self.request_id = request_id

        approve_button = discord.ui.Button(
            label="Approve", style=discord.ButtonStyle.success,
            custom_id=f"royalguard:rankreq_approve:{request_id}"
        )
        approve_button.callback = self.approve
        self.add_item(approve_button)

        deny_button = discord.ui.Button(
            label="Deny", style=discord.ButtonStyle.danger,
            custom_id=f"royalguard:rankreq_deny:{request_id}"
        )
        deny_button.callback = self.deny
        self.add_item(deny_button)

    async def approve(self, interaction: discord.Interaction):
        if not await _has_approver_role(interaction):
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "You don't have the approver role for rank requests in this server."),
                ephemeral=True,
            )

        request = await db.get_rank_request(self.request_id)
        if not request or request["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error_embed("Already Resolved", "This request has already been approved, denied, or no longer exists."),
                ephemeral=True,
            )

        await interaction.response.defer()

        verification = await db.get_verification(int(request["requester_id"]))
        if not verification:
            await db.update_rank_request_status(self.request_id, "error", resolved_by=interaction.user.id)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Verified", f"<@{request['requester_id']}> is no longer verified - cannot rank them."),
            )

        # rank_id stored on the request is the Roblox *rank number* (1-255),
        # but Roblox's ranking endpoint needs the role's unique *id* - these
        # are different fields on the same group role, so resolve one to
        # the other before calling set_group_rank.
        try:
            group_roles = await roblox.get_group_roles(int(request["group_id"]))
        except Exception as e:
            await db.update_rank_request_status(self.request_id, "error", resolved_by=interaction.user.id)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.followup.send(
                embed=embeds.error_embed("Roblox Lookup Failed", str(e)),
            )

        matching_role = next((r for r in group_roles if r.get("rank") == request["rank_id"]), None)
        if not matching_role:
            await db.update_rank_request_status(self.request_id, "error", resolved_by=interaction.user.id)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.followup.send(
                embed=embeds.error_embed("Rank Not Found", f"Rank **{request['rank_name']}** no longer exists in this group - it may have been renamed or removed."),
            )

        try:
            await roblox.set_group_rank(
                group_id=int(request["group_id"]),
                roblox_user_id=int(verification["roblox_id"]),
                role_id=matching_role["id"],
                guild_id=interaction.guild.id,
            )
        except RuntimeError as e:
            await db.update_rank_request_status(self.request_id, "error", resolved_by=interaction.user.id)
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
            return await interaction.followup.send(
                embed=embeds.error_embed("Rank Change Failed", str(e)),
            )

        await db.update_rank_request_status(self.request_id, "approved", resolved_by=interaction.user.id)

        member = interaction.guild.get_member(int(request["requester_id"]))
        if member:
            try:
                await sync_member_roles(interaction.guild, member)
            except Exception:
                pass

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        await interaction.followup.send(
            embed=embeds.success_embed(
                "Rank Request Approved",
                f"<@{request['requester_id']}> has been ranked to **{request['rank_name']}** in **{request['group_name']}**."
            )
        )

    async def deny(self, interaction: discord.Interaction):
        if not await _has_approver_role(interaction):
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "You don't have the approver role for rank requests in this server."),
                ephemeral=True,
            )

        request = await db.get_rank_request(self.request_id)
        if not request or request["status"] != "pending":
            return await interaction.response.send_message(
                embed=embeds.error_embed("Already Resolved", "This request has already been approved, denied, or no longer exists."),
                ephemeral=True,
            )

        await db.update_rank_request_status(self.request_id, "denied", resolved_by=interaction.user.id)

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send(
            embed=embeds.info_embed(
                "Rank Request Denied",
                f"<@{request['requester_id']}>'s request for **{request['rank_name']}** in **{request['group_name']}** was denied."
            )
        )


class RankRequest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="rankrequestconfig", description="Set the approver role and channel for rank requests.")
    @app_commands.describe(approver_role="Role allowed to approve/deny requests", channel="Channel requests get posted to")
    @require_level(70)
    async def rankrequestconfig(
        self,
        interaction: discord.Interaction,
        approver_role: discord.Role,
        channel: discord.TextChannel,
    ):
        await db.set_rank_request_config(
            interaction.guild.id,
            approver_role_id=approver_role.id,
            requests_channel_id=channel.id,
        )
        await interaction.response.send_message(
            embed=embeds.success_embed(
                "Rank Request Config Updated",
                f"Approver role: {approver_role.mention}\nRequests channel: {channel.mention}"
            )
        )

    rank_group = app_commands.Group(name="rank", description="Rank request commands.")

    @rank_group.command(name="request", description="Request a rank change in a bound Roblox group.")
    @app_commands.describe(group_id="The Roblox group ID", rank_id="The target rank number (1-255)", rank_name="The target rank's display name")
    async def request(
        self,
        interaction: discord.Interaction,
        group_id: str,
        rank_id: int,
        rank_name: str,
    ):
        config = await db.get_rank_request_config(interaction.guild.id)
        channel_id = config.get("requests_channel_id")
        if not channel_id:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Configured", "Rank requests haven't been set up in this server yet. Ask an admin to run /rankrequestconfig."),
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(int(channel_id))
        if not channel:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Channel Missing", "The configured rank request channel no longer exists. Ask an admin to run /rankrequestconfig again."),
                ephemeral=True,
            )

        groupbinds = await db.list_groupbinds(interaction.guild.id)
        group_name = next((g["group_name"] for g in groupbinds if g["group_id"] == group_id), group_id)

        request = await db.create_rank_request(
            guild_id=interaction.guild.id,
            requester_id=interaction.user.id,
            group_id=int(group_id),
            rank_id=rank_id,
            rank_name=rank_name,
            group_name=group_name,
        )

        embed = embeds.info_embed(
            "New Rank Request",
            f"Requester: {interaction.user.mention}\nGroup: {group_name}\nRequested rank: **{rank_name}**"
        )
        view = RankRequestView(request["_id"])
        await channel.send(embed=embed, view=view)

        await interaction.response.send_message(
            embed=embeds.success_embed("Request Submitted", f"Your rank request for **{rank_name}** has been sent for approval."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RankRequest(bot))
