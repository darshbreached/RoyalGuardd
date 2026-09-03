"""
cogs/adminlevels.py
--------------------
Slash commands to manage the staff hierarchy admin level system.
Levels are scoped per-guild - a level set in one server has no effect
in any other server the bot is installed in.

Admin levels can be granted to an individual user OR to a Discord role.
When checking a member's effective level, the higher of "their own
explicit level" and "the level of any admin-role they hold" is used
(see database/mongodb.py: get_effective_admin_level).

Only BOT_OWNER_ID (set in the environment) is automatically Owner level,
everywhere.
"""

import os
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level
from config import settings

BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")


class AdminLevels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if BOT_OWNER_ID and str(member.id) == str(BOT_OWNER_ID):
            return
        await db.set_admin_level(member.guild.id, member.id, 0)

    @app_commands.command(name="setadmin", description="Set a user's or role's admin level.")
    @app_commands.describe(
        level="Admin level (0-100, or 999999 for Owner)",
        user="The user to set (leave blank if setting a role instead)",
        role="The role to set (leave blank if setting a user instead)",
    )
    @require_level(90)
    async def setadmin(
        self,
        interaction: discord.Interaction,
        level: int,
        user: discord.Member = None,
        role: discord.Role = None,
    ):
        if not user and not role:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Missing Target", "Provide either a `user` or a `role`."),
                ephemeral=True,
            )
        if user and role:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Too Many Targets", "Provide a `user` **or** a `role`, not both."),
                ephemeral=True,
            )

        if level != settings.OWNER_LEVEL and (level < 0 or level > 100):
            return await interaction.response.send_message(
                embed=embeds.error_embed("Invalid Level", "Level must be between 0-100, or 999999 for Owner."),
                ephemeral=True,
            )

        # Effective caller level - accounts for any admin role the caller holds,
        # not just a level set directly on them.
        caller_role_ids = [r.id for r in interaction.user.roles]
        caller_level = await db.get_effective_admin_level(interaction.guild.id, interaction.user.id, caller_role_ids)
        is_bot_owner = BOT_OWNER_ID and str(interaction.user.id) == str(BOT_OWNER_ID)

        if not is_bot_owner and level >= caller_level:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "You cannot assign a level equal to or higher than your own."),
                ephemeral=True,
            )

        if role:
            await db.set_role_admin_level(interaction.guild.id, role.id, level, role_name=role.name)
            target_mention = role.mention
        else:
            await db.set_admin_level(interaction.guild.id, user.id, level)
            target_mention = user.mention

        await interaction.response.send_message(
            embed=embeds.success_embed("Admin Level Updated", f"{target_mention} is now admin level **{level}** in this server.")
        )

    @app_commands.command(name="removeadmin", description="Remove a user's or role's admin level (resets to 0).")
    @app_commands.describe(
        user="The user to reset (leave blank if resetting a role instead)",
        role="The role to reset (leave blank if resetting a user instead)",
    )
    @require_level(90)
    async def removeadmin(
        self,
        interaction: discord.Interaction,
        user: discord.Member = None,
        role: discord.Role = None,
    ):
        if not user and not role:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Missing Target", "Provide either a `user` or a `role`."),
                ephemeral=True,
            )
        if user and role:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Too Many Targets", "Provide a `user` **or** a `role`, not both."),
                ephemeral=True,
            )

        if role:
            await db.remove_role_admin_level(interaction.guild.id, role.id)
            target_mention = role.mention
        else:
            await db.remove_admin_level(interaction.guild.id, user.id)
            target_mention = user.mention

        await interaction.response.send_message(
            embed=embeds.success_embed("Admin Level Removed", f"{target_mention} has been reset to level **0** in this server.")
        )

    @app_commands.command(name="adminlevel", description="Check a user's effective admin level.")
    @app_commands.describe(user="The user to check (defaults to yourself)")
    async def adminlevel(self, interaction: discord.Interaction, user: discord.Member = None):
        target = user or interaction.user
        is_bot_owner = BOT_OWNER_ID and str(target.id) == str(BOT_OWNER_ID)

        role_ids = [r.id for r in target.roles]
        level = await db.get_effective_admin_level(interaction.guild.id, target.id, role_ids)
        display_level = settings.OWNER_LEVEL if is_bot_owner else level
        label = "Owner (Infinite)" if display_level == settings.OWNER_LEVEL else str(display_level)

        await interaction.response.send_message(
            embed=embeds.info_embed("Admin Level", f"{target.mention}'s admin level in this server: **{label}**")
        )

    admins_group = app_commands.Group(name="admins", description="View server admins.")

    @admins_group.command(name="view", description="List every user and role with an admin level in this server.")
    async def admins_view(self, interaction: discord.Interaction):
        docs = await db.get_all_admin_levels(interaction.guild.id)
        docs = [d for d in docs if d.get("level", 0) > 0]

        if not docs:
            return await interaction.response.send_message(
                embed=embeds.info_embed("Admins List", "No admins have been configured for this server yet."),
            )

        by_level = {}
        for doc in docs:
            lvl = doc["level"]
            mention = f"<@&{doc['discord_id']}>" if doc.get("type") == "role" else f"<@{doc['discord_id']}>"
            by_level.setdefault(lvl, []).append(mention)

        embed = discord.Embed(
            title=f"{interaction.guild.name} | Admins List",
            description=f"Listing server level admins for the server {interaction.guild.name}",
            color=settings.INFO_COLOR,
        )

        for lvl in sorted(by_level.keys()):
            label = "Owner" if lvl == settings.OWNER_LEVEL else str(lvl)
            value = "\n".join(f"- {m}" for m in by_level[lvl])
            if len(value) > 1024:
                value = value[:1000] + "\n...(truncated)"
            embed.add_field(name=f"Admin Level {label}", value=value, inline=True)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminLevels(bot))
