"""
utils/updateembeds.py
Shared embeds for /update and the verification panel's "Update Roles" button,
so both show exactly the same thing.
"""

import discord

from utils import embeds


def not_verified_embed(member: discord.abc.User) -> discord.Embed:
    embed = embeds.warning_embed("Update Roles", "This user is not verified on our ROBLOX database.")
    embed.set_author(name=member.name, icon_url=member.display_avatar.url)
    embed.color = discord.Color(0xFFA500)
    embed.add_field(name="Nickname", value="None", inline=False)
    embed.add_field(name="Roles Added", value="None", inline=False)
    embed.add_field(name="Roles Removed", value="None", inline=False)
    return embed


def roles_update_embed(member: discord.Member, added: list, removed: list, timezone_label: str = None) -> discord.Embed:
    embed = embeds.success_embed("Roles Update", "Succesfully updated user roles")
    embed.add_field(name="Nickname", value=member.nick or member.name, inline=False)
    embed.add_field(name="Roles Added", value=", ".join(added) if added else "None", inline=False)
    embed.add_field(name="Roles Removed", value=", ".join(removed) if removed else "None", inline=False)
    if timezone_label:
        embed.add_field(name="Timezone", value=timezone_label, inline=False)
    return embed