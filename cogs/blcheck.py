"""
cogs/blcheck.py
----------------
!blcheck - admin level 10+ only.

Scans every verified member of this server and lists who is in a blacklisted
Roblox group, with their role/rank in that group. Uses the same lists as
!bgcheck (set via /setup -> Background Check):
  - blacklisted_groups: comma-separated Roblox group IDs
  - blacklisted_names:  comma-separated Roblox group NAMES (case-insensitive)
  - whitelisted_groups: group IDs that override a blacklist match
"""

import asyncio

import discord
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import has_level, insufficient_permissions_embed

BLCHECK_MIN_LEVEL = 10
CONCURRENCY = 4          # simultaneous Roblox lookups (keep low to avoid rate limits)
PROGRESS_EVERY = 25      # edit the progress message every N members
MAX_EMBED_CHARS = 3800
MAX_RESULT_MESSAGES = 20


def _cfg_list(guild_config: dict, key: str) -> list:
    raw = (guild_config or {}).get(key, "") or ""
    return [v.strip() for v in raw.split(",") if v.strip()]


class BLCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _scan_member(self, member, sem, bl_ids, bl_names, wl_ids):
        """Returns (status, roblox_username, hits). status: ok / unverified / error."""
        async with sem:
            try:
                verification = await db.get_verification(member.id)
                if not verification:
                    return "unverified", None, []
                roblox_id = int(verification["roblox_id"])
                username = verification.get("roblox_username") or str(roblox_id)
                groups = await roblox.get_user_groups(roblox_id)
            except Exception:
                return "error", None, []

        hits = []
        for entry in groups or []:
            try:
                group_id = str(entry["group"]["id"])
                group_name = entry["group"]["name"]
                role_name = entry["role"]["name"]
                rank = entry["role"].get("rank")
            except (KeyError, TypeError):
                continue
            blacklisted = group_id in bl_ids or group_name.lower() in bl_names
            if blacklisted and group_id not in wl_ids:
                hits.append((group_id, group_name, role_name, rank))
        return "ok", username, hits

    @commands.command(name="blcheck")
    @commands.guild_only()
    async def blcheck(self, ctx: commands.Context):
        if not await has_level(ctx.author.id, ctx.guild, BLCHECK_MIN_LEVEL):
            return await ctx.send(
                embed=insufficient_permissions_embed(
                    ctx.author,
                    f"This command is limited to the admin level **{BLCHECK_MIN_LEVEL}**!",
                )
            )

        guild_config = await db.get_guild_config(ctx.guild.id) or {}
        bl_ids = set(_cfg_list(guild_config, "blacklisted_groups"))
        bl_names = {n.lower() for n in _cfg_list(guild_config, "blacklisted_names")}
        wl_ids = set(_cfg_list(guild_config, "whitelisted_groups"))

        if not bl_ids and not bl_names:
            return await ctx.send(
                embed=embeds.error_embed(
                    "No Blacklist Set",
                    "This server has no blacklisted groups. Add some in `/setup` -> Background Check.",
                )
            )

        try:
            if not ctx.guild.chunked:
                await ctx.guild.chunk()
        except Exception:
            pass

        members = [m for m in ctx.guild.members if not m.bot]
        status_msg = await ctx.send(
            embed=embeds.info_embed("Blacklist Check", f"Scanning **{len(members)}** members... this can take a while.")
        )

        sem = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            asyncio.create_task(self._scan_member(m, sem, bl_ids, bl_names, wl_ids))
            for m in members
        ]

        verified = unverified = errors = 0
        done = 0
        for finished in asyncio.as_completed(tasks):
            status, _username, _hits = await finished
            done += 1
            if status == "ok":
                verified += 1
            elif status == "unverified":
                unverified += 1
            else:
                errors += 1
            if done % PROGRESS_EVERY == 0:
                try:
                    await status_msg.edit(
                        embed=embeds.info_embed("Blacklist Check", f"Scanned **{done}/{len(members)}** members...")
                    )
                except Exception:
                    pass

        # group results: group_id -> {"name": str, "rows": [(member, username, role, rank)]}
        by_group = {}
        flagged_users = set()
        for task, member in zip(tasks, members):
            try:
                status, username, hits = task.result()
            except Exception:
                continue
            for group_id, group_name, role_name, rank in hits:
                bucket = by_group.setdefault(group_id, {"name": group_name, "rows": []})
                bucket["rows"].append((member, username, role_name, rank))
                flagged_users.add(member.id)

        summary = embeds.info_embed(
            "Blacklist Check - Summary",
            f"**Members scanned:** {len(members)}\n"
            f"**Verified:** {verified}\n"
            f"**Not verified (skipped):** {unverified}\n"
            f"**Lookup errors:** {errors}\n\n"
            f"**Users in blacklisted groups:** {len(flagged_users)}\n"
            f"**Blacklisted groups found:** {len(by_group)}",
        )
        try:
            await status_msg.edit(embed=summary)
        except Exception:
            await ctx.send(embed=summary)

        if not by_group:
            return

        sent = 0
        for group_id, data in sorted(by_group.items(), key=lambda kv: -len(kv[1]["rows"])):
            rows = sorted(data["rows"], key=lambda r: (r[3] is None, -(r[3] or 0)))
            lines = [
                f"{m.mention} (`{uname}`) - {role}" + (f" (Rank {rank})" if rank is not None else "")
                for m, uname, role, rank in rows
            ]

            chunks, current = [], ""
            for line in lines:
                if len(current) + len(line) + 1 > MAX_EMBED_CHARS:
                    chunks.append(current)
                    current = ""
                current += line + "\n"
            if current:
                chunks.append(current)

            for i, chunk in enumerate(chunks):
                if sent >= MAX_RESULT_MESSAGES:
                    return await ctx.send("Too many results to show - narrow your blacklist or check fewer groups.")
                title = f"{data['name']} ({group_id}) - {len(rows)} member(s)"
                if len(chunks) > 1:
                    title += f" [{i + 1}/{len(chunks)}]"
                await ctx.send(embed=embeds.info_embed(title[:256], chunk))
                sent += 1


async def setup(bot: commands.Bot):
    await bot.add_cog(BLCheck(bot))
