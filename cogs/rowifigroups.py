"""
cogs/rowifigroups.py
---------------------
Copy RoWifi's GROUP binds (a Discord role for anyone in a Roblox group, at any
rank) into this bot. Works like /rowifiimport does for rankbinds: start
listening in a channel, run RoWifi's group binds view there, click through the
pages (each page arrives as a message or a message edit), then finish to see a
preview file. Nothing is written until you press Import.

Each group is bound (if it isn't already) and its roles are added to the
"role_ids" list on the group's groupbinds document, exactly like
/groupbind add. cogs/update.py applies them to members. Sessions live in
memory only and expire after 30 minutes.
"""

import io
import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level

ROWIFI_BOT_ID = 508968886998269962
SESSION_TTL = 30 * 60

ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
RANK_RE = re.compile(r"Rank(?:\s*Id)?\s*[:\-]?\s*(\d+)", re.IGNORECASE)
GROUP_RE = re.compile(r"Group(?:\s*Id)?\s*[:\-]?\s*`?(\d{4,})`?", re.IGNORECASE)
BARE_ID_RE = re.compile(r"(?<!\d)(\d{4,10})(?!\d)")


def _is_rowifi(user) -> bool:
    return user.id == ROWIFI_BOT_ID or "rowifi" in user.name.lower()


def _chunks(embed: discord.Embed):
    """Yield the text of each separate entry in an embed (fields, then description blocks)."""
    for f in embed.fields:
        yield f"{f.name}\n{f.value}"
    for block in re.split(r"\n\s*\n", embed.description or ""):
        lines = block.split("\n")
        hits = [l for l in lines if GROUP_RE.search(l) and ROLE_MENTION_RE.search(l)]
        if len(hits) >= 2:
            for line in hits:
                yield line
        elif block.strip():
            yield block


def _parse(embed: discord.Embed):
    """Returns (found, skipped). found = [(group_id, [role_ids])]; skipped is raw
    text that couldn't be read, shown in the preview so the parser can be tuned."""
    found, skipped = [], []
    for chunk in _chunks(embed):
        roles = [int(r) for r in ROLE_MENTION_RE.findall(chunk)]
        if not roles or RANK_RE.search(chunk):
            skipped.append(chunk[:200])
            continue
        m = GROUP_RE.search(chunk)
        gid = int(m.group(1)) if m else None
        if gid is None:
            m = BARE_ID_RE.search(ROLE_MENTION_RE.sub("", chunk))
            gid = int(m.group(1)) if m else None
        if gid is None:
            skipped.append(chunk[:200])
            continue
        found.append((gid, roles))
    return found, skipped


class GroupImportGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="rowifigroupimport", description="Copy your RoWifi group binds into this bot.")


class ConfirmView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, invoker_id: int, binds: dict):
        super().__init__(timeout=300)
        self.cog, self.guild, self.invoker_id, self.binds = cog, guild, invoker_id, binds

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /rowifigroupimport finish can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        try:
            added, missing, invalid, high = await self.cog._run_import(self.guild, self.binds)
        except Exception as e:
            self.stop()
            return await interaction.followup.send(
                embed=embeds.error_embed("Import Failed", f"Something went wrong partway through: {e}"), ephemeral=True
            )
        self.cog.sessions.pop(self.guild.id, None)
        text = f"Added **{added}** group role(s) across **{len(self.binds)}** group(s). Run `/updateall` to apply them to members."
        if missing:
            text += f"\n\n**{missing}** role(s) from RoWifi no longer exist in this server and were skipped."
        if invalid:
            text += f"\n\n**{invalid}** role(s) can't be assigned (@everyone or bot/integration roles) and were skipped."
        if high:
            text += f"\n\n**{high}** role(s) are above my highest role, so I can't assign them until you move my role higher."
        await interaction.followup.send(embed=embeds.success_embed("RoWifi Group Import Complete", text), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.cog.sessions.pop(self.guild.id, None)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Import cancelled. Nothing was written.", ephemeral=True)
        self.stop()


class RowifiGroups(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.sessions: dict[int, dict] = {}

        self.group = GroupImportGroup()
        self.group.add_command(app_commands.Command(
            name="start", description="Start collecting RoWifi's group binds view in this channel.", callback=self.gi_start))
        self.group.add_command(app_commands.Command(
            name="status", description="See how much has been collected so far.", callback=self.gi_status))
        self.group.add_command(app_commands.Command(
            name="finish", description="Preview what was collected and import it.", callback=self.gi_finish))
        bot.tree.add_command(self.group)

    def _get(self, guild_id: int):
        s = self.sessions.get(guild_id)
        if s and time.time() - s["started_at"] > SESSION_TTL:
            self.sessions.pop(guild_id, None)
            return None
        return s

    def _ingest(self, session: dict, message: discord.Message):
        for embed in message.embeds:
            found, skipped = _parse(embed)
            session["seen"].add(hash(str(embed.to_dict())))
            for gid, roles in found:
                session["binds"].setdefault(gid, set()).update(roles)
            for s in skipped:
                if len(session["skipped"]) < 10 and s not in session["skipped"]:
                    session["skipped"].append(s)

    async def _run_import(self, guild: discord.Guild, binds: dict):
        added = missing = invalid = high = 0
        for gid, roles in sorted(binds.items()):
            info = await roblox.get_group_info(gid)
            name = info.get("name", f"Group {gid}") if info else f"Group {gid}"
            await db.add_groupbind(guild.id, gid, name)
            for rid in sorted(roles):
                role = guild.get_role(rid)
                if role is None:
                    missing += 1
                    continue
                if role.is_default() or role.managed:
                    invalid += 1
                    continue
                await db.groupbinds.update_one(
                    {"guild_id": str(guild.id), "group_id": str(gid)},
                    {"$addToSet": {"role_ids": str(rid)}},
                )
                added += 1
                if role >= guild.me.top_role:
                    high += 1
        return added, missing, invalid, high

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        s = self._get(message.guild.id)
        if not s or message.channel.id != s["channel_id"]:
            return
        if message.embeds and _is_rowifi(message.author):
            self._ingest(s, message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        s = self._get(payload.guild_id)
        if not s or payload.channel_id != s["channel_id"]:
            return
        author = payload.data.get("author") or {}
        if author and int(author.get("id", 0)) != ROWIFI_BOT_ID and "rowifi" not in str(author.get("username", "")).lower():
            return
        guild = self.bot.get_guild(payload.guild_id)
        channel = guild.get_channel_or_thread(payload.channel_id) if guild else None
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        if message.embeds and _is_rowifi(message.author):
            self._ingest(s, message)

    @require_level(10)
    async def gi_start(self, interaction: discord.Interaction):
        self.sessions[interaction.guild.id] = {
            "channel_id": interaction.channel.id, "binds": {}, "seen": set(), "skipped": [],
            "started_by": interaction.user.id, "started_at": time.time(),
        }
        await interaction.response.send_message(
            embed=embeds.info_embed(
                "RoWifi Group Import Started",
                "1. In **this channel**, run RoWifi's `/groupbinds view` (if your RoWifi lists group binds under "
                "`/binds view`, use that). The reply must be public.\n"
                "2. Click **Next Page** until you reach the last page. I collect each page as it loads.\n"
                "3. Run `/rowifigroupimport status` any time, then `/rowifigroupimport finish`.\n\n"
                "Nothing is written until you confirm on the preview. This session expires in 30 minutes."
            ),
            ephemeral=True,
        )

    @require_level(10)
    async def gi_status(self, interaction: discord.Interaction):
        s = self._get(interaction.guild.id)
        if not s:
            return await interaction.response.send_message(
                embed=embeds.error_embed("No Import Running", "Run `/rowifigroupimport start` first."), ephemeral=True)
        roles = sum(len(r) for r in s["binds"].values())
        await interaction.response.send_message(
            embed=embeds.info_embed(
                "RoWifi Group Import Status",
                f"Collected **{len(s['binds'])}** group(s) with **{roles}** role link(s) from **{len(s['seen'])}** page view(s) so far."),
            ephemeral=True)

    @require_level(10)
    async def gi_finish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        s = self._get(interaction.guild.id)
        if not s:
            return await interaction.followup.send(
                embed=embeds.error_embed("No Import Running", "Run `/rowifigroupimport start` first."), ephemeral=True)

        lines = ["group_id,role_ids"]
        for gid, roles in sorted(s["binds"].items()):
            lines.append(f"{gid},{' '.join(str(r) for r in sorted(roles))}")
        if s["skipped"]:
            lines += ["", "# Text I couldn't read (first few):"]
            lines += ["# " + t.replace("\n", " | ") for t in s["skipped"]]
        report = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="rowifi_groupbinds_preview.txt")

        if not s["binds"]:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "Nothing To Import",
                    "Nothing usable was captured. Make sure RoWifi's group binds view was run in the same channel as "
                    "`/rowifigroupimport start`, that its reply is public, and that you clicked through the pages. "
                    "If the attached file shows text I couldn't read, send it to the developer."),
                file=report, ephemeral=True)

        links = sum(len(r) for r in s["binds"].values())
        view = ConfirmView(self, interaction.guild, interaction.user.id, s["binds"])
        await interaction.followup.send(
            embed=embeds.info_embed(
                "RoWifi Group Import Preview",
                f"Captured **{len(s['binds'])}** group(s) with **{links}** role link(s) from **{len(s['seen'])}** page view(s).\n\n"
                f"Check the attached file. If the groups and roles look right, press **Import**."),
            file=report, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RowifiGroups(bot))