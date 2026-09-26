"""
cogs/acceptrequest.py
----------------------
/acceptrequest roblox_username:<name>  - accept level 20+ only

Takes a raw ROBLOX USERNAME instead of a Discord member picker, since not
every user being accepted is necessarily in this Discord server (or
verified) yet. Flow:

  1. Look up the Roblox username via roblox.get_user_by_username() to get
     their Roblox ID - fails cleanly if the username doesn't exist.
  2. Reverse-lookup db.get_verification_by_roblox(roblox_id) to see if
     that Roblox account happens to be verified to a Discord member in
     this server.
  3. Show the regiment dropdown, rank them on Roblox regardless of step 2
     (Option A) - a regiment acceptance is a Roblox-side action and
     shouldn't be blocked just because Discord verification hasn't
     happened yet.
  4. If a verified Discord member WAS found (and is still in this guild),
     sync their Discord roles same as before. If not, skip the sync and
     say so plainly in the confirmation - there's no Discord member to
     sync roles onto.
"""

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from cogs.update import sync_member_roles


class RegimentSelect(discord.ui.Select):
    def __init__(self, roblox_id: int, roblox_username: str, member: discord.Member | None, groups_info: list[dict], guild_id: int):
        self.roblox_id = roblox_id
        self.roblox_username = roblox_username
        self.member = member  # None if no verified Discord member was found
        self.guild_id = guild_id
        self.groups_info = {str(g["id"]): g for g in groups_info}

        options = [
            discord.SelectOption(label=g["name"][:100], value=str(g["id"]), description=f"Group ID: {g['id']}")
            for g in groups_info
        ][:25]  # Discord hard cap on select options

        super().__init__(placeholder="Select a regiment to accept this user into...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        group_id = int(self.values[0])
        group_name = self.groups_info[self.values[0]]["name"]

        roles = await roblox.get_group_roles(group_id)
        entry_roles = sorted((r for r in roles if r["rank"] > 0), key=lambda r: r["rank"])
        if not entry_roles:
            return await interaction.followup.send(
                embed=embeds.error_embed("No Entry Rank Found", f"Could not find a valid entry rank in **{group_name}**."),
                ephemeral=True,
            )
        entry_role = entry_roles[0]

        try:
            success = await roblox.set_group_rank(group_id, self.roblox_id, entry_role["id"], guild_id=self.guild_id)
        except RuntimeError as e:
            return await interaction.followup.send(
                embed=embeds.error_embed("Rank Change Failed", str(e)),
                ephemeral=True,
            )

        if not success:
            return await interaction.followup.send(
                embed=embeds.error_embed("Rank Change Failed", "Could not accept the user into this regiment."),
                ephemeral=True,
            )

        target_label = self.member.mention if self.member else f"**{self.roblox_username}** (no verified Discord member found)"

        if self.member:
            await sync_member_roles(interaction.guild, self.member, self.roblox_id)
            description = f"{target_label} has been accepted into **{group_name}** as **{entry_role['name']}**."
        else:
            description = (
                f"**{self.roblox_username}** has been accepted into **{group_name}** as **{entry_role['name']}** "
                f"on Roblox. No verified Discord member was found in this server, so Discord roles were not synced - "
                f"they'll sync automatically once/if they verify."
            )

        await interaction.followup.send(
            embed=embeds.success_embed("Request Accepted", description),
            ephemeral=True,
        )

        channel_id = await db.get_log_channel(interaction.guild.id, "rank")
        if channel_id:
            channel = interaction.guild.get_channel(int(channel_id))
            if channel:
                await channel.send(embed=embeds.info_embed(
                    "Regiment Acceptance",
                    f"**{interaction.user}** accepted {target_label} into **{group_name}** as **{entry_role['name']}**."
                ))


class RegimentSelectView(discord.ui.View):
    def __init__(self, roblox_id: int, roblox_username: str, member: discord.Member | None, groups_info: list[dict], guild_id: int):
        super().__init__(timeout=120)
        self.add_item(RegimentSelect(roblox_id, roblox_username, member, groups_info, guild_id))


class AcceptRequest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="acceptrequest", description="Accept a Roblox user into a regiment group.")
    @app_commands.describe(roblox_username="The ROBLOX username to accept into a regiment")
    @require_level(20)
    async def acceptrequest(self, interaction: discord.Interaction, roblox_username: str):
        await interaction.response.defer(ephemeral=True)

        roblox_user = await roblox.get_user_by_username(roblox_username)
        if not roblox_user:
            return await interaction.followup.send(
                embed=embeds.error_embed("User Not Found", f"No Roblox user named **{roblox_username}** exists.")
            )

        roblox_id = roblox_user["id"]
        # get_user_by_username returns whatever Roblox's real casing is - use
        # that (not the raw input) everywhere from here on for consistency
        roblox_username = roblox_user.get("name", roblox_username)

        member = None
        verification = await db.get_verification_by_roblox(roblox_id)
        if verification:
            discord_id = int(verification["discord_id"])
            member = interaction.guild.get_member(discord_id)
            if member is None:
                try:
                    member = await interaction.guild.fetch_member(discord_id)
                except discord.NotFound:
                    member = None  # verified elsewhere or left this server

        guild_config = await db.get_guild_config(interaction.guild.id)
        raw = guild_config.get("regiment_groups", "")
        regiment_ids = [v.strip() for v in raw.split(",") if v.strip()]

        if not regiment_ids:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "No Regiments Configured",
                    "No regiment groups are set up. Configure them via /setup -> Background Check -> Regiment Groups."
                )
            )

        groups_info = []
        for gid in regiment_ids:
            info = await roblox.get_group_info(int(gid))
            if info:
                groups_info.append({"id": int(gid), "name": info.get("name", f"Group {gid}")})
            else:
                groups_info.append({"id": int(gid), "name": f"Unknown Group ({gid})"})

        embed = embeds.info_embed(
            "Accept Into Regiment",
            f"Select which regiment to accept **{roblox_username}** into."
        )
        view = RegimentSelectView(roblox_id, roblox_username, member, groups_info, interaction.guild.id)
        await interaction.followup.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(AcceptRequest(bot))
