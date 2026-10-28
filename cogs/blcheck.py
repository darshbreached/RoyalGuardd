"""
cogs/blcheck.py
----------------
!blcheck          - admin level 10+. Scans every verified member of this server and
                    lists who is in a blacklisted Roblox group (read-only report).
!blcheck notify   - same report, PLUS:
                    * DMs each flagged user telling them to leave within 24 hours
                    * DMs the CO(s) of any regiment the user is in
                    * saves the warning; a background task re-checks every 10 minutes
                      and, once the 24h are up, posts in the channel where notify was
                      run (pinging the COs) for anyone still in the group.

Uses the same lists as !bgcheck (set via /setup -> Background Check):
  - blacklisted_groups: comma-separated Roblox group IDs
  - blacklisted_names:  comma-separated Roblox group NAMES (case-insensitive)
  - whitelisted_groups: group IDs that override a blacklist match
  - regiment_groups:    comma-separated Roblox group IDs that count as regiments

A regiment's CO = a verified member of this server holding CO_MIN_RANK or higher
in that regiment's Roblox group.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import has_level, insufficient_permissions_embed

BLCHECK_MIN_LEVEL = 10
CO_MIN_RANK = 200        # Roblox rank number in a regiment group that counts as CO
WARN_HOURS = 24
CONCURRENCY = 4          # simultaneous Roblox lookups (keep low to avoid rate limits)
PROGRESS_EVERY = 25      # edit the progress message every N members
DM_DELAY = 1.2           # seconds between DMs, to stay under Discord rate limits
MAX_EMBED_CHARS = 3800
MAX_RESULT_MESSAGES = 20
WARN_KEY = "blcheck_warnings"


def _cfg_list(guild_config: dict, key: str) -> list:
    raw = (guild_config or {}).get(key, "") or ""
    return [v.strip() for v in raw.split(",") if v.strip()]


class BLCheck(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.check_deadlines.start()

    async def cog_unload(self):
        self.check_deadlines.cancel()

    # ------------------------------------------------------------------ #
    # scanning
    # ------------------------------------------------------------------ #

    async def _scan_member(self, member, sem, bl_ids, bl_names, wl_ids, reg_ids):
        async with sem:
            try:
                verification = await db.get_verification(member.id)
                if not verification:
                    return {"status": "unverified"}
                roblox_id = int(verification["roblox_id"])
                username = verification.get("roblox_username") or str(roblox_id)
                groups = await roblox.get_user_groups(roblox_id)
            except Exception:
                return {"status": "error"}

        hits, regs = [], []
        for entry in groups or []:
            try:
                group_id = str(entry["group"]["id"])
                group_name = entry["group"]["name"]
                role_name = entry["role"]["name"]
                rank = entry["role"].get("rank")
            except (KeyError, TypeError):
                continue
            if group_id in reg_ids:
                regs.append((group_id, group_name, role_name, rank))
            blacklisted = group_id in bl_ids or group_name.lower() in bl_names
            if blacklisted and group_id not in wl_ids:
                hits.append((group_id, group_name, role_name, rank))
        return {
            "status": "ok", "member": member, "username": username,
            "roblox_id": roblox_id, "hits": hits, "regs": regs,
        }

    # ------------------------------------------------------------------ #
    # command
    # ------------------------------------------------------------------ #

    @commands.command(name="blcheck")
    @commands.guild_only()
    async def blcheck(self, ctx: commands.Context, mode: str = None):
        if not await has_level(ctx.author.id, ctx.guild, BLCHECK_MIN_LEVEL):
            return await ctx.send(
                embed=insufficient_permissions_embed(
                    ctx.author,
                    f"This command is limited to the admin level **{BLCHECK_MIN_LEVEL}**!",
                )
            )

        notify = (mode or "").lower() == "notify"

        guild_config = await db.get_guild_config(ctx.guild.id) or {}
        bl_ids = set(_cfg_list(guild_config, "blacklisted_groups"))
        bl_names = {n.lower() for n in _cfg_list(guild_config, "blacklisted_names")}
        wl_ids = set(_cfg_list(guild_config, "whitelisted_groups"))
        reg_ids = set(_cfg_list(guild_config, "regiment_groups"))

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
        tasks_ = [
            asyncio.create_task(self._scan_member(m, sem, bl_ids, bl_names, wl_ids, reg_ids))
            for m in members
        ]

        results = []
        verified = unverified = errors = 0
        done = 0
        for fut in asyncio.as_completed(tasks_):
            res = await fut
            results.append(res)
            done += 1
            if res["status"] == "ok":
                verified += 1
            elif res["status"] == "unverified":
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

        ok_results = [r for r in results if r["status"] == "ok"]
        flagged = [r for r in ok_results if r["hits"]]

        # regiment COs, worked out from the same scan (no extra Roblox calls)
        cos_by_reg = {}
        for r in ok_results:
            for gid, _gname, _role, rank in r["regs"]:
                if rank is not None and rank >= CO_MIN_RANK:
                    cos_by_reg.setdefault(gid, []).append(r["member"])

        # group results: group_id -> {"name": str, "rows": [(member, username, role, rank)]}
        by_group = {}
        for r in flagged:
            for group_id, group_name, role_name, rank in r["hits"]:
                bucket = by_group.setdefault(group_id, {"name": group_name, "rows": []})
                bucket["rows"].append((r["member"], r["username"], role_name, rank))

        summary = embeds.info_embed(
            "Blacklist Check - Summary",
            f"**Members scanned:** {len(members)}\n"
            f"**Verified:** {verified}\n"
            f"**Not verified (skipped):** {unverified}\n"
            f"**Lookup errors:** {errors}\n\n"
            f"**Users in blacklisted groups:** {len(flagged)}\n"
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
                    await ctx.send("Too many results to show - narrow your blacklist or check fewer groups.")
                    break
                title = f"{data['name']} ({group_id}) - {len(rows)} member(s)"
                if len(chunks) > 1:
                    title += f" [{i + 1}/{len(chunks)}]"
                await ctx.send(embed=embeds.info_embed(title[:256], chunk))
                sent += 1
            if sent >= MAX_RESULT_MESSAGES:
                break

        if notify:
            await self._notify(ctx, flagged, cos_by_reg, guild_config)

    # ------------------------------------------------------------------ #
    # notify: DM users + COs, save warnings
    # ------------------------------------------------------------------ #

    async def _notify(self, ctx, flagged, cos_by_reg, guild_config):
        guild = ctx.guild
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=WARN_HOURS)
        deadline_text = discord.utils.format_dt(deadline, "F")

        warnings = dict(guild_config.get(WARN_KEY) or {})
        warned, already, dm_failed = [], [], []
        co_notes = {}   # co member id -> (co member, [lines])
        no_co = set()

        await ctx.send(embed=embeds.info_embed("Notifying", f"Sending warnings to **{len(flagged)}** user(s)..."))

        for r in flagged:
            member = r["member"]
            uid = str(member.id)

            existing = warnings.get(uid)
            if existing:
                try:
                    if datetime.fromisoformat(existing["deadline"]) > now:
                        already.append(member)
                        continue
                except (KeyError, ValueError):
                    pass

            group_map = {gid: gname for gid, gname, _role, _rank in r["hits"]}
            group_list = "\n".join(f"- **{n}**" for n in group_map.values())

            dm = embeds.warning_embed(
                "Blacklisted Group Notice",
                f"You are in the following blacklisted Roblox group(s):\n\n{group_list}\n\n"
                f"You must **leave within {WARN_HOURS} hours** (by {deadline_text}).\n"
                f"Staff will check again after that, and your regiment CO will be told if you are still in.\n\n"
                f"Sent from **{guild.name}**.",
            )
            try:
                await member.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                dm_failed.append(member)
            await asyncio.sleep(DM_DELAY)

            co_ids = []
            for gid, gname, _role, _rank in r["regs"]:
                cos = [c for c in cos_by_reg.get(gid, []) if c.id != member.id]
                if not cos:
                    no_co.add(gname)
                    continue
                for co in cos:
                    if co.id not in co_ids:
                        co_ids.append(co.id)
                    entry = co_notes.setdefault(co.id, (co, []))
                    entry[1].append(
                        f"{member.mention} (`{r['username']}`) [{gname}] - in {', '.join(group_map.values())}"
                    )

            warnings[uid] = {
                "roblox_id": r["roblox_id"],
                "username": r["username"],
                "groups": group_map,
                "deadline": deadline.isoformat(),
                "channel_id": ctx.channel.id,
                "co_ids": co_ids,
            }
            warned.append(member)

        try:
            await db.set_guild_config(guild.id, **{WARN_KEY: warnings})
        except Exception as e:
            print(f"[BLCHECK] could not save warnings: {type(e).__name__}")
            await ctx.send(embed=embeds.error_embed(
                "Warnings Not Saved",
                "The DMs went out, but I couldn't save the 24h reminders, so I won't re-check automatically."))

        co_failed = []
        for co, lines in co_notes.values():
            body = "\n".join(lines)[:3500]
            dm = embeds.warning_embed(
                "Regiment Members In Blacklisted Groups",
                f"These members of your regiment are in blacklisted groups. "
                f"They were told to leave by {deadline_text}.\n\n{body}\n\nSent from **{guild.name}**.",
            )
            try:
                await co.send(embed=dm)
            except (discord.Forbidden, discord.HTTPException):
                co_failed.append(co)
            await asyncio.sleep(DM_DELAY)

        report = embeds.success_embed(
            "Notices Sent",
            f"**Warned:** {len(warned)}\n"
            f"**Already warned (still inside 24h):** {len(already)}\n"
            f"**COs notified:** {len(co_notes) - len(co_failed)}\n"
            f"I'll re-check in {WARN_HOURS} hours.",
        )
        if dm_failed:
            report.add_field(
                name="Could not DM (DMs closed) - tell them yourself",
                value=" ".join(m.mention for m in dm_failed)[:1000], inline=False)
        if co_failed:
            report.add_field(
                name="Could not DM these COs",
                value=" ".join(m.mention for m in co_failed)[:1000], inline=False)
        if no_co:
            report.add_field(
                name=f"No CO found (nobody verified at rank {CO_MIN_RANK}+)",
                value=", ".join(sorted(no_co))[:1000], inline=False)
        await ctx.send(embed=report)

    # ------------------------------------------------------------------ #
    # background re-check once the 24h are up
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=10)
    async def check_deadlines(self):
        for guild in self.bot.guilds:
            try:
                await self._process_guild(guild)
            except Exception as e:
                print(f"[BLCHECK] deadline check failed for {guild.id}: {type(e).__name__}")

    @check_deadlines.before_loop
    async def _before_check_deadlines(self):
        await self.bot.wait_until_ready()

    async def _process_guild(self, guild: discord.Guild):
        cfg = await db.get_guild_config(guild.id) or {}
        warnings = dict(cfg.get(WARN_KEY) or {})
        if not warnings:
            return

        now = datetime.now(timezone.utc)
        changed = False

        for uid, w in list(warnings.items()):
            try:
                deadline = datetime.fromisoformat(w["deadline"])
            except (KeyError, ValueError):
                del warnings[uid]
                changed = True
                continue
            if deadline > now:
                continue

            try:
                groups = await roblox.get_user_groups(int(w["roblox_id"]))
            except Exception:
                continue  # try again next loop
            if not groups:
                # empty usually means the lookup failed; retry for 2 days, then give up
                if now > deadline + timedelta(days=2):
                    del warnings[uid]
                    changed = True
                continue

            current_ids = set()
            for entry in groups:
                try:
                    current_ids.add(str(entry["group"]["id"]))
                except (KeyError, TypeError):
                    continue
            still = [name for gid, name in (w.get("groups") or {}).items() if gid in current_ids]

            del warnings[uid]
            changed = True
            if not still:
                continue  # they left - nothing to report

            channel = guild.get_channel(int(w.get("channel_id") or 0))
            if channel is None:
                continue
            co_pings = " ".join(f"<@{c}>" for c in (w.get("co_ids") or []))
            embed = embeds.warning_embed(
                "Still In Blacklisted Group",
                f"<@{uid}> (`{w.get('username', uid)}`) was told to leave {WARN_HOURS} hours ago "
                f"but is still in:\n\n" + "\n".join(f"- **{n}**" for n in still),
            )
            try:
                await channel.send(content=co_pings or None, embed=embed)
            except Exception:
                pass

        if changed:
            await db.set_guild_config(guild.id, **{WARN_KEY: warnings})


async def setup(bot: commands.Bot):
    await bot.add_cog(BLCheck(bot))
