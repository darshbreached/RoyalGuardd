"""
utils/permissions.py
---------------------
Admin level permission system, scoped per-guild so a level granted in
one server has no effect in another.

Level 0        = regular user
Level 1-100    = staff hierarchy (higher = more powerful)
Level 999999   = Owner (infinite / bypasses everything)

Only the Discord user ID in BOT_OWNER_ID gets automatic Owner level,
in every server. Everyone else is checked against MongoDB, scoped to
the current guild - and now that includes any admin level granted to
a Discord role the member holds, not just levels set directly on them.
"""

import os
import discord
from discord import app_commands
from database.mongodb import db
from config import settings

BOT_OWNER_ID = os.getenv("BOT_OWNER_ID")


async def has_level(user_id: int, guild: discord.Guild, required: int) -> bool:
    if BOT_OWNER_ID and str(user_id) == str(BOT_OWNER_ID):
        return True

    if guild is None:
        return False

    # Pull the member's roles (if cached/fetchable) so role-granted admin
    # levels are considered alongside any level set directly on the user.
    member = guild.get_member(user_id)
    role_ids = [r.id for r in member.roles] if member else []

    level = await db.get_effective_admin_level(guild.id, user_id, role_ids)
    return level >= required


def require_level(required: int):
    """App command check decorator - use as @require_level(10)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        ok = await has_level(interaction.user.id, interaction.guild, required)
        if not ok:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Insufficient Permissions",
                    description=f"You need admin level **{required}+** to use this command.",
                    color=settings.ERROR_COLOR,
                ),
                ephemeral=True,
            )
        return ok

    return app_commands.check(predicate)


def require_owner():
    """Restricts a command to the bot owner only."""
    return require_level(settings.OWNER_LEVEL)
