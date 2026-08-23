"""
cogs/update.py
---------------
/update       - syncs the invoking user's roles against all bound groups/ranks
/updateall    - syncs every verified member in the server (admin level 10+)

sync_member_roles is the shared core: it adds/removes Discord roles based
on the member's live Roblox rank, and sets their nickname to
"<prefix> <roblox_username>" using the highest-priority nickname prefix
from any matched rankbind. If no rankbind for their current rank has a
prefix set, the nickname is left as just their Roblox username with no
prefix.

IMPORTANT #1: every groupbind's live rank is fetched BEFORE any role is
added/removed. If a Roblox API call fails for any group (rate limit, Roblox
outage, etc.), sync_member_roles raises roblox.RobloxAPIError and makes ZERO
role changes for this call - a failed API call must never look identical to
"the user left every group."

IMPORTANT #2: rankbind roles are evaluated per ROLE, not per rankbind ENTRY.
If the same Discord role (e.g. "Roblox Verified") is bound to multiple
different ranks as separate rankbind documents, evaluating entry-by-entry
meant every non-matching rank's entry would independently try to strip that
role - even if a different entry for the user's actual current rank also
grants it - with the final add/remove outcome depending on unpredictable
MongoDB document order (add-then-immediately-remove in the same sync call).
Grouping by role_id first and using the union of every rank it's bound to
fixes this: a shared role is only removed if NONE of its bound ranks match.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from config import settings


async def sync_member_roles(guild: discord.Guild, member: discord.Member, roblox_id: int):
    """Compares the member's current roles against every rankbind, adds/removes
    roles as needed, sets their nickname to "<prefix> <roblox_username>",
    and logs the result. Returns (added, removed, nickname_changed).

    Raises roblox.RobloxAPIError if any group's live rank can't be fetched -
    in that case NO roles are touched at all, to avoid a false de-rank from a
    transient Roblox API failure.
    """
    groupbinds = await db.list_groupbinds(guild.id)
    added, removed = [], []
    best_prefix = None
    best_rank_id = -1
    has_any_rank = False

    # Fetch every group's live rank FIRST, before touching any roles. If any
    # one of these fails, bail out completely - don't remove roles based on
    # partial/unknown data.
    rank_by_group = {}
    for gb in groupbinds:
        group_id = int(gb["group_id"])
        try:
            rank_id, _ = await roblox.get_user_rank_in_group(roblox_id, group_id)
        except roblox.RobloxAPIError as e:
            print(f"Royal Guard: aborting role sync for {member} ({member.id}) in guild {guild.id} "
                  f"- Roblox API failed for group {group_id}, refusing to risk a false de-rank. {e}")
            raise
        rank_by_group[group_id] = rank_id

    for gb in groupbinds:
        group_id = int(gb["group_id"])
        rank_id = rank_by_group[group_id]
        rankbinds = await db.list_rankbinds(guild.id, group_id)

        # Group rankbind entries by role_id so a role bound to multiple ranks
        # is evaluated ONCE using the union of every rank it's bound to -
        # not once per (rank, role) pair, which previously let mismatched
        # entries for a SHARED role strip it right after a matching entry
        # had just granted it, depending on iteration order.
        role_to_ranks: dict[int, set[int]] = {}
        for rb in rankbinds:
            role_id = int(rb["role_id"])
            role_to_ranks.setdefault(role_id, set()).add(int(rb["rank_id"]))

        for role_id, bound_ranks in role_to_ranks.items():
            role = guild.get_role(role_id)
            if role is None:
                continue

            should_have = rank_id in bound_ranks
            if should_have:
                has_any_rank = True
            has_role = role in member.roles

            try:
                if should_have and not has_role:
                    await member.add_roles(role, reason="Royal Guard rank sync")
                    added.append(role.mention)
                elif not should_have and has_role:
                    await member.remove_roles(role, reason="Royal Guard rank sync")
                    removed.append(role.mention)
            except discord.Forbidden:
                continue

        # Nickname prefix: use whichever rankbind entry matches the user's
        # current rank and has a prefix set, preferring the highest rank_id
        # if more than one entry for that rank has a prefix.
        for rb in rankbinds:
            if int(rb["rank_id"]) == rank_id and rb.get("nickname_prefix") and int(rb["rank_id"]) > best_rank_id:
                best_rank_id = int(rb["rank_id"])
                best_prefix = rb["nickname_prefix"]

    # Sticky roles from /setup. Extra Roles and Verified Roles are comma-separated
    # lists (role ID or role name, either works), resolved fresh each sync. Ranks
    # Role and Non-BA are single role IDs. Non-Verified is always removed here
    # since this function only runs for already-verified members.
    guild_config = await db.get_guild_config(guild.id)
    is_verified = await db.get_verification(member.id) is not None

    def _cfg_role(key):
        role_id = guild_config.get(key)
        return guild.get_role(int(role_id)) if role_id else None

    def _cfg_role_list(key):
        raw = guild_config.get(key, "")
        entries = [v.strip() for v in raw.split(",") if v.strip()]
        roles = []
        for entry in entries:
            role = None
            if entry.isdigit():
                role = guild.get_role(int(entry))
            if role is None:
                role = discord.utils.get(guild.roles, name=entry)
            if role:
                roles.append(role)
            else:
                print(f"Royal Guard: could not resolve role '{entry}' (tried as both ID and name) from '{key}' in guild {guild.id} ({guild.name}) - check /setup for a typo or a role that no longer exists.")
        return roles

    sticky_adjustments = []
    for role in _cfg_role_list("extra_roles"):
        sticky_adjustments.append((role, has_any_rank))
    for role in _cfg_role_list("verified_roles"):
        sticky_adjustments.append((role, is_verified))
    sticky_adjustments.append((_cfg_role("ranks_role_id"), has_any_rank))
    sticky_adjustments.append((_cfg_role("non_verified_role_id"), False))
    sticky_adjustments.append((_cfg_role("non_ba_role_id"), not has_any_rank))

    for role, should_have in sticky_adjustments:
        if role is None:
            continue
        has_role = role in member.roles
        try:
            if should_have and not has_role:
                await member.add_roles(role, reason="Royal Guard rank sync (sticky)")
                added.append(role.mention)
            elif not should_have and has_role:
                await member.remove_roles(role, reason="Royal Guard rank sync (sticky)")
                removed.append(role.mention)
        except discord.Forbidden:
            continue

    nickname_changed = False
    verification = await db.get_verification(member.id)
    roblox_username = verification["roblox_username"] if verification else member.name

    if guild.me.guild_permissions.manage_nicknames and member.id != guild.owner_id:
        new_nick = f"{best_prefix} {roblox_username}" if best_prefix else roblox_username
        new_nick = new_nick[:32]  # Discord nickname length limit
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason="Royal Guard rank sync")
                nickname_changed = True
            except discord.Forbidden:
                pass

    if added or removed or nickname_changed:
        log_embed = embeds.info_embed("Roles Update", "Succesfully updated user roles")
        log_embed.add_field(name="Nickname", value=member.nick or member.name, inline=False)
        log_embed.add_field(name="Roles Added", value=", ".join(added) if added else "None", inline=False)
        log_embed.add_field(name="Roles Removed", value=", ".join(removed) if removed else "None", inline=False)

        channel_id = await db.get_log_channel(guild.id, "update")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                await channel.send(embed=log_embed)

    return added, removed, nickname_changed


class Update(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild_config = await db.get_guild_config(member.guild.id)
        role_id = guild_config.get("non_ba_role_id")
        if not role_id:
            return
        role = member.guild.get_role(int(role_id))
        if role is None:
            return
        try:
            await member.add_roles(role, reason="Royal Guard: assigned on join, not yet in BA group")
        except discord.Forbidden:
            pass

    @app_commands.command(name="update", description="Sync your own roles with your current Roblox group ranks.")
    async def update(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        verification = await db.get_verification(interaction.user.id)
        if not verification:
            return await interaction.followup.send(
                embed=embeds.error_embed("Warning - Not Verified", "You must be verified to update your roles.")
            )

        try:
            added, removed, nickname_changed = await sync_member_roles(
                interaction.guild, interaction.user, int(verification["roblox_id"])
            )
        except roblox.RobloxAPIError:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "Roblox Temporarily Unavailable",
                    "Roblox's API didn't respond correctly just now, so no roles were changed. Please try `/update` again in a minute."
                )
            )

        embed = embeds.success_embed("Roles Update", "Succesfully updated user roles")
        embed.add_field(name="Nickname", value=interaction.user.nick or interaction.user.name, inline=False)
        embed.add_field(name="Roles Added", value=", ".join(added) if added else "None", inline=False)
        embed.add_field(name="Roles Removed", value=", ".join(removed) if removed else "None", inline=False)

        await interaction.followup.send(embed=embed)

    @app_commands.command(name="updateall", description="Sync roles for every verified member in the server.")
    @require_level(settings.UPDATEALL_MIN_LEVEL)
    async def updateall(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild = interaction.guild
        updated, failed = 0, 0

        progress_embed = embeds.info_embed("Updating All Members", "This may take a while depending on server size...")
        message = await interaction.followup.send(embed=progress_embed)

        async for member in guild.fetch_members(limit=None):
            if member.bot:
                continue
            verification = await db.get_verification(member.id)
            if not verification:
                continue
            try:
                await sync_member_roles(guild, member, int(verification["roblox_id"]))
                updated += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.5)

        await message.edit(embed=embeds.success_embed(
            "Update Complete", f"Synced **{updated}** members. Failed: **{failed}**."
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(Update(bot))
