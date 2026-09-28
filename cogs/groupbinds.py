"""
cogs/groupbinds.py
-------------------
Manage which Roblox groups are bound to this Discord server, and which
Discord roles a member gets just for being in a group (RoWifi-style
groupbind: any rank in the group qualifies).
Multiple groups may be bound at once, and each group can have multiple roles.

Group roles are stored as a "role_ids" list (role IDs as strings, same as
rankbinds) directly on the group's existing "groupbinds" document. They are
applied by cogs/update.py's sync_member_roles.

/rowifiimport copies rankbinds out of RoWifi. RoWifi has no export, and a bot
can't click another bot's buttons, so this works by watching RoWifi's own
/rankbinds view message: /rowifiimport start begins listening in that channel,
every page RoWifi shows (first load and each Next Page click, which arrives as
a message edit) is parsed, and /rowifiimport finish shows a preview file
before anything is written. Confirming creates the rankbinds and also binds
each group (rankbinds only sync for groups in the groupbinds list). The
RoWifi template becomes the nickname prefix (the text before any {placeholder}).
Sessions live in memory only and expire after 30 minutes.
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
IMPORT_SESSION_TTL = 30 * 60

ROLE_MENTION_RE = re.compile(r"<@&(\d+)>")
RANK_RE = re.compile(r"Rank(?:\s*Id)?\s*[:\-]?\s*(\d+)", re.IGNORECASE)
TEMPLATE_RE = re.compile(r"Template\s*:\s*`?([^\n`]*)`?", re.IGNORECASE)
GROUP_RE = re.compile(r"Group(?:\s*Id)?\s*[:\-]?\s*`?(\d{4,})`?", re.IGNORECASE)


def _is_rowifi(user) -> bool:
    return user.id == ROWIFI_BOT_ID or "rowifi" in user.name.lower()


def _prefix_from_template(template: str) -> str:
    return template.split("{")[0].strip()


def _parse_embed(embed: discord.Embed):
    """Returns (found, skipped). found is a list of
    (group_id or None, rank_id, template, [role_ids]); skipped is raw text of
    anything that looked like it might matter but couldn't be read, so the
    preview file can show what the parser is choking on."""
    found = []
    skipped = []
    current_group = None

    for text in (embed.title, embed.description, embed.author.name if embed.author else None):
        m = GROUP_RE.search(text or "")
        if m:
            current_group = int(m.group(1))
            break

    if not embed.fields and embed.description:
        skipped.append("(embed with no fields) " + embed.description[:300])

    for f in embed.fields:
        blob = f"{f.name}\n{f.value}"
        rank_m = RANK_RE.search(blob)
        group_m = GROUP_RE.search(blob)
        if group_m:
            current_group = int(group_m.group(1))
        if not rank_m:
            if not group_m:
                skipped.append(blob[:200])
            continue
        tmpl_m = TEMPLATE_RE.search(blob)
        template = tmpl_m.group(1).strip() if tmpl_m else ""
        roles = [int(r) for r in ROLE_MENTION_RE.findall(blob)]
        found.append((current_group, int(rank_m.group(1)), template, roles))

    return found, skipped


class GroupBindGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="groupbind", description="Manage Roblox group bindings for this server.")


class RowifiImportGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="rowifiimport", description="Copy your RoWifi rankbinds into this bot.")


class ImportConfirmView(discord.ui.View):
    def __init__(self, cog, guild: discord.Guild, invoker_id: int, resolved: dict):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild = guild
        self.invoker_id = invoker_id
        self.resolved = resolved

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Only the person who ran /rowifiimport finish can use these buttons.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Import", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        try:
            imported, missing_roles, group_count = await self.cog._run_import(self.guild, self.resolved)
        except Exception as e:
            self.stop()
            return await interaction.followup.send(
                embed=embeds.error_embed("Import Failed", f"Something went wrong partway through: {e}"),
                ephemeral=True,
            )

        self.cog.import_sessions.pop(self.guild.id, None)
        description = (
            f"Created **{imported}** rank-role binds across **{group_count}** group(s). "
            f"Run `/updateall` to apply them to members."
        )
        if missing_roles:
            description += f"\n\n**{missing_roles}** role(s) from RoWifi no longer exist in this server and were skipped."
        await interaction.followup.send(embed=embeds.success_embed("RoWifi Import Complete", description), ephemeral=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        self.cog.import_sessions.pop(self.guild.id, None)
        await interaction.response.edit_message(view=self)
        await interaction.followup.send("Import cancelled. Nothing was written.", ephemeral=True)
        self.stop()


class GroupBinds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.import_sessions: dict[int, dict] = {}

        self.group = GroupBindGroup()
        self.group.add_command(
            app_commands.Command(name="add", description="Bind a Roblox group (and optionally a role for its members).",
                                  callback=self.groupbind_add)
        )
        self.group.add_command(
            app_commands.Command(name="removerole", description="Remove a role from a bound group's members.",
                                  callback=self.groupbind_removerole)
        )
        self.group.add_command(
            app_commands.Command(name="remove", description="Unbind a Roblox group from this server.",
                                  callback=self.groupbind_remove)
        )
        self.group.add_command(
            app_commands.Command(name="list", description="List all Roblox groups bound to this server.",
                                  callback=self.groupbind_list)
        )
        bot.tree.add_command(self.group)

        self.import_group = RowifiImportGroup()
        self.import_group.add_command(
            app_commands.Command(name="start", description="Start collecting RoWifi's /rankbinds view pages in this channel.",
                                  callback=self.rowifiimport_start)
        )
        self.import_group.add_command(
            app_commands.Command(name="status", description="See how much has been collected so far.",
                                  callback=self.rowifiimport_status)
        )
        self.import_group.add_command(
            app_commands.Command(name="finish", description="Preview what was collected and import it.",
                                  callback=self.rowifiimport_finish)
        )
        bot.tree.add_command(self.import_group)

    # ---------------------------------------------------------------
    # /groupbind
    # ---------------------------------------------------------------
    @require_level(10)
    @app_commands.describe(
        group_id="The Roblox group ID to bind",
        role="Optional: role every member of this group gets, at any rank. Run again to add more roles.",
    )
    async def groupbind_add(self, interaction: discord.Interaction, group_id: int, role: discord.Role = None):
        await interaction.response.defer(ephemeral=True)
        info = await roblox.get_group_info(group_id)
        if not info:
            return await interaction.followup.send(
                embed=embeds.error_embed("Group Not Found", f"Could not find a Roblox group with ID `{group_id}`.")
            )

        if role is not None and (role.is_default() or role.managed):
            return await interaction.followup.send(
                embed=embeds.error_embed("Invalid Role", "That role can't be assigned (it's @everyone or a bot/integration role).")
            )

        group_name = info.get("name", "Unknown")
        await db.add_groupbind(interaction.guild.id, group_id, group_name)

        if role is None:
            return await interaction.followup.send(
                embed=embeds.success_embed("Group Bound", f"**{group_name}** (`{group_id}`) is now bound to this server.")
            )

        await db.groupbinds.update_one(
            {"guild_id": str(interaction.guild.id), "group_id": str(group_id)},
            {"$addToSet": {"role_ids": str(role.id)}},
        )

        description = (
            f"**{group_name}** (`{group_id}`) is bound to this server. "
            f"Members of the group at any rank will get {role.mention}."
        )
        if role >= interaction.guild.me.top_role:
            description += (
                f"\n\nWarning: {role.mention} is above my highest role, so I can't assign it. "
                f"Move my role above it in Server Settings -> Roles."
            )
        await interaction.followup.send(embed=embeds.success_embed("Group Bound", description))

    @require_level(10)
    @app_commands.describe(group_id="The Roblox group ID", role="The role to stop giving to this group's members")
    async def groupbind_removerole(self, interaction: discord.Interaction, group_id: int, role: discord.Role):
        result = await db.groupbinds.update_one(
            {"guild_id": str(interaction.guild.id), "group_id": str(group_id)},
            {"$pull": {"role_ids": str(role.id)}},
        )

        if result.matched_count == 0:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Group Not Bound", f"Group `{group_id}` isn't bound to this server."),
                ephemeral=True,
            )
        if result.modified_count == 0:
            return await interaction.response.send_message(
                embed=embeds.info_embed("Nothing Changed", f"{role.mention} wasn't bound to group `{group_id}`."),
                ephemeral=True,
            )

        await interaction.response.send_message(
            embed=embeds.success_embed(
                "Group Role Removed",
                f"{role.mention} is no longer given to members of group `{group_id}`. "
                f"Members who already have it keep it."
            )
        )

    @require_level(10)
    @app_commands.describe(group_id="The Roblox group ID to unbind")
    async def groupbind_remove(self, interaction: discord.Interaction, group_id: int):
        await db.remove_groupbind(interaction.guild.id, group_id)
        await interaction.response.send_message(
            embed=embeds.success_embed("Group Unbound", f"Group `{group_id}`, its rankbinds, and its group roles have been removed.")
        )

    async def groupbind_list(self, interaction: discord.Interaction):
        binds = await db.list_groupbinds(interaction.guild.id)
        if not binds:
            return await interaction.response.send_message(
                embed=embeds.info_embed("No Groupbinds", "This server has no Roblox groups bound yet.")
            )

        lines = []
        for b in binds:
            line = f"• **{b['group_name']}** — `{b['group_id']}`"
            role_ids = b.get("role_ids") or []
            if role_ids:
                line += "\n   Group roles: " + ", ".join(f"<@&{r}>" for r in role_ids)
            lines.append(line)

        await interaction.response.send_message(embed=embeds.info_embed("Bound Groups", "\n".join(lines)))

    # ---------------------------------------------------------------
    # /rowifiimport
    # ---------------------------------------------------------------
    def _get_session(self, guild_id: int):
        session = self.import_sessions.get(guild_id)
        if session and time.time() - session["started_at"] > IMPORT_SESSION_TTL:
            self.import_sessions.pop(guild_id, None)
            return None
        return session

    def _ingest(self, session: dict, message: discord.Message) -> int:
        added = 0
        for embed in message.embeds:
            found, skipped = _parse_embed(embed)
            session["seen"].add(hash(str(embed.to_dict())))
            for gid, rank_id, template, roles in found:
                session["binds"][(gid, rank_id)] = {
                    "template": template,
                    "prefix": _prefix_from_template(template),
                    "roles": roles,
                }
                added += 1
            for s in skipped:
                if len(session["skipped"]) < 10 and s not in session["skipped"]:
                    session["skipped"].append(s)
        return added

    @staticmethod
    def _resolve(session: dict):
        resolved = {}
        unresolved = 0
        for (gid, rank_id), bind in session["binds"].items():
            gid = gid or session["default_group"]
            if not gid:
                unresolved += 1
                continue
            resolved[(gid, rank_id)] = bind
        return resolved, unresolved

    async def _run_import(self, guild: discord.Guild, resolved: dict):
        rank_names: dict[int, dict[int, str]] = {}
        for gid in sorted({g for g, _ in resolved}):
            info = await roblox.get_group_info(gid)
            group_name = info.get("name", f"Group {gid}") if info else f"Group {gid}"
            await db.add_groupbind(guild.id, gid, group_name)
            try:
                roles = await roblox.get_group_roles(gid)
            except Exception:
                roles = []
            rank_names[gid] = {r["rank"]: r["name"] for r in roles}

        imported = 0
        missing = set()
        for (gid, rank_id), bind in sorted(resolved.items()):
            rank_name = rank_names.get(gid, {}).get(rank_id, f"Rank {rank_id}")
            for role_id in bind["roles"]:
                if guild.get_role(role_id) is None:
                    missing.add(role_id)
                    continue
                await db.add_rankbind(guild.id, gid, rank_id, role_id, rank_name, bind["prefix"])
                imported += 1
        return imported, len(missing), len(rank_names)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return
        session = self._get_session(message.guild.id)
        if not session or message.channel.id != session["channel_id"]:
            return
        if not message.embeds or not _is_rowifi(message.author):
            return
        self._ingest(session, message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.guild_id is None:
            return
        session = self._get_session(payload.guild_id)
        if not session or payload.channel_id != session["channel_id"]:
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
            self._ingest(session, message)

    @require_level(10)
    @app_commands.describe(
        group_id="Optional: Roblox group ID to use for any bind where RoWifi's page doesn't show one"
    )
    async def rowifiimport_start(self, interaction: discord.Interaction, group_id: int = None):
        self.import_sessions[interaction.guild.id] = {
            "channel_id": interaction.channel.id,
            "default_group": group_id,
            "binds": {},
            "seen": set(),
            "skipped": [],
            "started_by": interaction.user.id,
            "started_at": time.time(),
        }
        await interaction.response.send_message(
            embed=embeds.info_embed(
                "RoWifi Import Started",
                "1. In **this channel**, run RoWifi's `/rankbinds view` (the reply must be public).\n"
                "2. Click **Next Page** until you reach the last page. I collect each page as it loads.\n"
                "3. Run `/rowifiimport status` any time to see progress, then `/rowifiimport finish`.\n\n"
                "Nothing is written until you confirm on the preview. This session expires in 30 minutes."
            ),
            ephemeral=True,
        )

    @require_level(10)
    async def rowifiimport_status(self, interaction: discord.Interaction):
        session = self._get_session(interaction.guild.id)
        if not session:
            return await interaction.response.send_message(
                embed=embeds.error_embed("No Import Running", "Run `/rowifiimport start` first."), ephemeral=True
            )
        await interaction.response.send_message(
            embed=embeds.info_embed(
                "RoWifi Import Status",
                f"Collected **{len(session['binds'])}** rank binds from **{len(session['seen'])}** page view(s) so far."
            ),
            ephemeral=True,
        )

    @require_level(10)
    async def rowifiimport_finish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        session = self._get_session(interaction.guild.id)
        if not session:
            return await interaction.followup.send(
                embed=embeds.error_embed("No Import Running", "Run `/rowifiimport start` first."), ephemeral=True
            )

        resolved, unresolved = self._resolve(session)

        lines = ["group_id,rank_id,prefix,role_ids"]
        for (gid, rank_id), b in sorted(resolved.items()):
            lines.append(f"{gid},{rank_id},{b['prefix']},{' '.join(str(r) for r in b['roles'])}")
        if session["skipped"]:
            lines += ["", "# Text I couldn't read (first few):"]
            lines += ["# " + s.replace("\n", " | ") for s in session["skipped"]]
        report = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename="rowifi_import_preview.txt")

        if not resolved:
            description = (
                "Nothing usable was captured. Make sure `/rankbinds view` was run in the same channel as "
                "`/rowifiimport start`, that its reply is public, and that you clicked through the pages."
            )
            if unresolved:
                description = (
                    f"Found **{unresolved}** rank binds but couldn't tell which Roblox group they belong to. "
                    f"Run `/rowifiimport start` again with the group_id option."
                )
            return await interaction.followup.send(
                embed=embeds.error_embed("Nothing To Import", description), file=report, ephemeral=True
            )

        groups = sorted({g for g, _ in resolved})
        role_links = sum(len(b["roles"]) for b in resolved.values())
        description = (
            f"Captured **{len(resolved)}** rank binds ({role_links} role links) from "
            f"**{len(session['seen'])}** page view(s), for group(s): {', '.join(f'`{g}`' for g in groups)}.\n\n"
            f"Check the attached file. If the groups, ranks and roles look right, press **Import**."
        )
        if unresolved:
            description += f"\n\n**{unresolved}** bind(s) had no group ID and are left out."

        view = ImportConfirmView(self, interaction.guild, interaction.user.id, resolved)
        await interaction.followup.send(
            embed=embeds.info_embed("RoWifi Import Preview", description), file=report, view=view, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(GroupBinds(bot))
