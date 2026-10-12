"""cogs/bgcheck.py — !bgcheck @user, paginated Discord/Roblox/Regiment info embed.
Pagination via message reactions: ⬅️ (previous), ➡️ (next), 🏁 (jump to last page), 🗑️ (delete).
Auto-deletes after 60s of inactivity. Bot requires 'Add Reactions' and 'Manage Messages' permissions.

Regiment/blacklist matching (page 3) reads from guild_config, set via
/setup -> Background Check:
  - regiment_groups:    comma-separated Roblox group IDs. Any of the user's
                         groups matching one of these IDs is shown as a regiment.
  - blacklisted_groups: comma-separated Roblox group IDs.
  - blacklisted_names:  comma-separated Roblox group NAMES (case-insensitive).
                         A group matches the blacklist if its ID is in
                         blacklisted_groups OR its name is in blacklisted_names.
  - whitelisted_groups: comma-separated Roblox group IDs that override a
                         blacklist match (checked last, takes priority).
All four are optional - an unset/empty list just means no matches for that check.
"""

import asyncio
import discord
from discord.ext import commands
from datetime import datetime, timezone

from database.mongodb import db
from utils import embeds, roblox, robloxinventory
from config import settings

PREV_EMOJI = "⬅️"
NEXT_EMOJI = "➡️"
LAST_EMOJI = "🏁"
DELETE_EMOJI = "🗑️"

TIMEOUT_SECONDS = 60.0

DATE_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"


def _cfg_list(guild_config: dict, key: str) -> list[str]:
    raw = guild_config.get(key, "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def _build_alerts(member: discord.Member, extra_alerts: list[str] = None) -> str:
    alerts = []
    if (datetime.now(timezone.utc) - member.joined_at).days < 7:
        alerts.append("* [This user is new to our discord server.]")
    if extra_alerts:
        alerts.extend(extra_alerts)
    if not alerts:
        alerts.append("* [No alerts.]")
    return "\n".join(alerts)


class BGCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _build_pages(self, ctx: commands.Context, member: discord.Member):
        verification = await db.get_verification(member.id)

        roblox_id = None
        groups = []
        if verification:
            roblox_id = int(verification["roblox_id"])
            groups = await roblox.get_user_groups(roblox_id)

        # Work out regiment/blacklist matches once, up front, so both the
        # regiment page and the shared Alerts field can use them.
        guild_config = await db.get_guild_config(ctx.guild.id)
        regiment_group_ids = set(_cfg_list(guild_config, "regiment_groups"))
        blacklisted_group_ids = set(_cfg_list(guild_config, "blacklisted_groups"))
        whitelisted_group_ids = set(_cfg_list(guild_config, "whitelisted_groups"))
        blacklisted_names = {n.lower() for n in _cfg_list(guild_config, "blacklisted_names")}

        regiment_matches = []
        blacklist_matches = []

        for entry in groups:
            group_id = str(entry["group"]["id"])
            group_name = entry["group"]["name"]
            role_name = entry["role"]["name"]

            if group_id in regiment_group_ids:
                regiment_matches.append(f"**{group_name}** — {role_name}")

            is_blacklisted = group_id in blacklisted_group_ids or group_name.lower() in blacklisted_names
            if is_blacklisted and group_id not in whitelisted_group_ids:
                blacklist_matches.append(f"**{group_name}** ({role_name})")

        extra_alerts = []
        if blacklist_matches:
            extra_alerts.append(f"* [This user is in {len(blacklist_matches)} blacklisted group(s) — see Page 3.]")

        alerts_text = _build_alerts(member, extra_alerts)

        # Page 1/3 — User Discord Account Details
        discord_embed = embeds.base_embed()
        discord_embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        discord_embed.title = "User Discord Account Details"
        discord_embed.add_field(name="Joined Date", value=member.joined_at.strftime(DATE_FORMAT), inline=False)
        discord_embed.add_field(name="Registered Date", value=member.created_at.strftime(DATE_FORMAT), inline=False)
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        discord_embed.add_field(name=f"User Roles [{len(roles)}]", value=" ".join(roles) if roles else "None", inline=False)
        discord_embed.add_field(name="Alerts", value=alerts_text, inline=False)

        # Page 2/3 — User ROBLOX Account Details
        roblox_embed = embeds.base_embed()
        roblox_embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        roblox_embed.title = "User ROBLOX Account Details"

        if verification:
            roblox_user = await roblox.get_user_by_id(roblox_id)
            avatar_url = await roblox.get_avatar_headshot_url(roblox_id)
            followers = await roblox.get_followers_count(roblox_id)
            following = await roblox.get_following_count(roblox_id)
            friends = await roblox.get_friends_count(roblox_id)

            if avatar_url:
                roblox_embed.set_thumbnail(url=avatar_url)

            if roblox_user and roblox_user.get("created"):
                created_dt = datetime.fromisoformat(roblox_user["created"].replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created_dt).days
                roblox_embed.add_field(name="ROBLOX Account Age", value=f"{age_days} days", inline=False)

            description = (roblox_user.get("description") or "").strip() if roblox_user else ""
            roblox_embed.add_field(name="ROBLOX Account Description", value=description if description else "No description set.", inline=False)
            roblox_embed.add_field(name="ROBLOX Account Groups", value=str(len(groups)), inline=False)
            roblox_embed.add_field(name="ROBLOX Account Friends", value=str(friends), inline=False)
            roblox_embed.add_field(name="ROBLOX Account Followers", value=str(followers), inline=False)
            roblox_embed.add_field(name="ROBLOX Account Following", value=str(following), inline=False)
            inventory = await robloxinventory.get_inventory_counts(roblox_id)
            roblox_embed.add_field(name="ROBLOX Account Gamepasses Owned", value=robloxinventory.format_result(inventory["gamepasses"]), inline=False)
            roblox_embed.add_field(name="ROBLOX Account Badges Owned", value=robloxinventory.format_result(inventory["badges"]), inline=False)
        else:
            roblox_embed.add_field(name="ROBLOX Account Status", value="This user has not verified their Roblox account.", inline=False)

        roblox_embed.add_field(name="Alerts", value=alerts_text, inline=False)

        # Page 3/3 — User Regiment Details
        regiment_embed = embeds.base_embed()
        regiment_embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        regiment_embed.title = "User Regiment Details"

        if not verification:
            regiment_embed.add_field(name="\u200b", value="[This user has not verified their Roblox account.]", inline=False)
        else:
            regiment_value = "\n".join(regiment_matches) if regiment_matches else "[This user is not in any regiments]"
            regiment_embed.add_field(name="Regiments", value=regiment_value, inline=False)

            blacklist_value = "\n".join(blacklist_matches) if blacklist_matches else "[Not in any blacklisted groups]"
            regiment_embed.add_field(name="Blacklisted Groups", value=blacklist_value, inline=False)

        regiment_embed.add_field(name="Alerts", value=alerts_text, inline=False)

        pages = [discord_embed, roblox_embed, regiment_embed]
        total = len(pages)
        for i, page in enumerate(pages, start=1):
            page.set_footer(text=f"Viewing Page {i}/{total}")

        return pages

    @commands.command(name="bgcheck")
    async def bgcheck(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author

        loading_embed = embeds.base_embed()
        loading_embed.title = "Background Checking"
        loading_embed.description = "Please hold on whilst we background check the user."
        message = await ctx.send(content=f"Hello {member.mention}", embed=loading_embed)

        pages = await self._build_pages(ctx, member)
        current = 0
        await message.edit(embed=pages[current])

        for emoji in (PREV_EMOJI, NEXT_EMOJI, LAST_EMOJI, DELETE_EMOJI):
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

        def check(reaction: discord.Reaction, user: discord.User):
            return (
                reaction.message.id == message.id
                and user.id == ctx.author.id
                and str(reaction.emoji) in (PREV_EMOJI, NEXT_EMOJI, LAST_EMOJI, DELETE_EMOJI)
            )

        while True:
            try:
                reaction, user = await self.bot.wait_for("reaction_add", timeout=TIMEOUT_SECONDS, check=check)
            except asyncio.TimeoutError:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                break

            emoji = str(reaction.emoji)

            if emoji == DELETE_EMOJI:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
                break

            try:
                await message.remove_reaction(emoji, user)
            except discord.HTTPException:
                pass

            if emoji == NEXT_EMOJI and current < len(pages) - 1:
                current += 1
                await message.edit(embed=pages[current])
            elif emoji == PREV_EMOJI and current > 0:
                current -= 1
                await message.edit(embed=pages[current])
            elif emoji == LAST_EMOJI:
                current = len(pages) - 1
                await message.edit(embed=pages[current])


async def setup(bot: commands.Bot):
    await bot.add_cog(BGCheck(bot))
