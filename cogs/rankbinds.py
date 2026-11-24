"""
cogs/rankbinds.py
------------------
Manage rank -> Discord role bindings per Roblox group, plus an optional
nickname prefix (e.g. "[OF-8]") applied automatically during role sync.

/rankbind add        - bind a rank to any number of roles (up to 25 per
                       selection - Discord's own RoleSelect limit - via an
                       interactive picker after group_id/rank_id are set).
                       Run it again to add more if you ever need more than 25.
/rankbind remove     - unbind a role (or all roles) from a rank
/rankbind removebulk - remove every rankbind for a group at once, or every
                       rankbind tied to a specific Discord role across all
                       ranks/groups
/rankbind view        - pick a group from a dropdown, then see every rank in it
                       (nickname prefix + bound roles) in a RoWifi-style embed
                       with a close button; pages automatically if long
/rankbind list        - list current bindings for a group, paginated by
                       character count (not a fixed line count) so a rank
                       with many bound roles never blows past Discord's
                       4096-char embed description limit
/rankbind findrole    - look up which rank(s) a given Discord role is
                       bound to, by role ID/mention instead of by rank name
/rankbind import      - bulk-create rankbinds from an uploaded text/CSV file,
                       one bind per line: group_id,rank_id,role_id,nickname_prefix
                       (nickname_prefix optional). Meant for migrating from
                       another bot (e.g. RoWifi) whose binds you've copied by
                       hand from its dashboard/commands - RoWifi's public API
                       does not expose an export/list endpoint for rankbinds,
                       so this is the practical path, not automatic scraping.
"""

import io
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level

MAX_CHARS_PER_PAGE = 3800
MAX_ROLES_SHOWN_PER_RANK = 20


def _format_rankbind_lines(binds: list) -> list:
    """Groups rankbind documents by rank and returns one formatted line per rank.
    Caps the number of role mentions shown per rank - a rank can have any
    number of bound roles now (/rankbind add supports unlimited), and showing
    all of them inline for a heavily-bound rank could produce a single line
    long enough to blow past Discord's 4096-char embed description limit on
    its own. Use /rankbind findrole to check whether a specific role is bound."""
    by_rank = {}
    for b in binds:
        by_rank.setdefault(b["rank_id"], {"rank_name": b.get("rank_name", "Rank"), "roles": []})
        by_rank[b["rank_id"]]["roles"].append(b)

    lines = []
    for rank_id, data in sorted(by_rank.items()):
        role_mentions = []
        for b in data["roles"]:
            prefix = f" (`{b['nickname_prefix']}`)" if b.get("nickname_prefix") else ""
            role_mentions.append(f"<@&{b['role_id']}>{prefix}")

        shown = role_mentions[:MAX_ROLES_SHOWN_PER_RANK]
        remainder = len(role_mentions) - len(shown)
        role_text = ", ".join(shown)
        if remainder > 0:
            role_text += f", and {remainder} more (use /rankbind findrole to check a specific one)"

        lines.append(f"**{data['rank_name']}** (`{rank_id}`) -> {role_text}")
    return lines


def _paginate_lines(lines: list) -> list:
    """Splits formatted lines into pages under MAX_CHARS_PER_PAGE, by character
    count rather than a fixed line count - a fixed-count page could still blow
    past Discord's embed description limit if individual lines are long. If a
    single line alone exceeds the cap, it's hard-split across multiple pages."""
    pages = []
    current_lines = []
    current_len = 0

    for line in lines:
        if len(line) > MAX_CHARS_PER_PAGE:
            if current_lines:
                pages.append(chr(10).join(current_lines))
                current_lines = []
                current_len = 0
            for i in range(0, len(line), MAX_CHARS_PER_PAGE):
                pages.append(line[i:i + MAX_CHARS_PER_PAGE])
            continue

        added_len = len(line) + 1
        if current_len + added_len > MAX_CHARS_PER_PAGE:
            pages.append(chr(10).join(current_lines))
            current_lines = [line]
            current_len = added_len
        else:
            current_lines.append(line)
            current_len += added_len

    if current_lines:
        pages.append(chr(10).join(current_lines))

    return pages


def _rank_sort_key(item):
    rank_id = item[0]
    try:
        return (0, int(rank_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(rank_id))


def _build_view_blocks(binds: list) -> tuple:
    """Returns (blocks, total_ranks) for /rankbind view: one text block per rank."""
    by_rank = {}
    for b in binds:
        entry = by_rank.setdefault(
            b["rank_id"], {"rank_name": b.get("rank_name") or "", "prefixes": [], "roles": []}
        )
        entry["roles"].append(b["role_id"])
        prefix = b.get("nickname_prefix")
        if prefix and prefix not in entry["prefixes"]:
            entry["prefixes"].append(prefix)

    blocks = []
    for rank_id, data in sorted(by_rank.items(), key=_rank_sort_key):
        name = data["rank_name"]
        header = f"**Rank {rank_id}**"
        if name and name != f"Rank {rank_id}":
            header += f" ({name})"

        prefix_text = ", ".join(f"`{p}`" for p in data["prefixes"]) if data["prefixes"] else "None"

        mentions = [f"<@&{rid}>" for rid in data["roles"]]
        shown = mentions[:MAX_ROLES_SHOWN_PER_RANK]
        remainder = len(mentions) - len(shown)
        roles_text = ", ".join(shown) if shown else "None"
        if remainder > 0:
            roles_text += f", and {remainder} more"

        blocks.append(
            f"{header}\n"
            f"Nickname Prefix: {prefix_text}\n"
            f"Roles Binded: {roles_text}\n"
        )

    return blocks, len(by_rank)


class RankbindListView(discord.ui.View):
    def __init__(self, title: str, pages: list, executor_id: int):
        super().__init__(timeout=120)
        self.title = title
        self.pages = pages
        self.current = 0
        self.executor_id = executor_id
        self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = self.current == 0
        self.next_page.disabled = self.current == len(self.pages) - 1

    def _embed(self):
        embed = embeds.info_embed(self.title, self.pages[self.current])
        embed.set_footer(text=f"Page {self.current + 1}/{len(self.pages)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.executor_id:
            await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "Only the command executor can page through this list."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)


class RankbindViewPager(discord.ui.View):
    """The 'Viewing Rankbinds for <server>' embed: one block per rank, close button,
    Previous/Next only when the group needs more than one page."""

    def __init__(self, guild_name: str, group_id: int, total_ranks: int, pages: list, executor_id: int, author: discord.abc.User):
        super().__init__(timeout=300)
        self.guild_name = guild_name
        self.group_id = group_id
        self.total_ranks = total_ranks
        self.pages = pages
        self.current = 0
        self.executor_id = executor_id
        self.author = author

        if len(pages) <= 1:
            self.remove_item(self.previous_page)
            self.remove_item(self.next_page)
        else:
            self._update_buttons()

    def _update_buttons(self):
        self.previous_page.disabled = self.current == 0
        self.next_page.disabled = self.current == len(self.pages) - 1

    def _embed(self) -> discord.Embed:
        header = f"Viewing group {self.group_id} | Total {self.total_ranks} Rankbinds"
        embed = embeds.info_embed(
            f"Viewing Rankbinds for {self.guild_name}",
            f"{header}\n\n{self.pages[self.current]}".strip(),
        )
        embed.set_author(name=self.author.display_name, icon_url=self.author.display_avatar.url)
        if len(self.pages) > 1:
            embed.set_footer(text=f"Page {self.current + 1}/{len(self.pages)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.executor_id:
            await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "Only the person who ran this command can use these buttons."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    @discord.ui.button(label="✖", style=discord.ButtonStyle.secondary)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        self.stop()


class RankbindGroupSelect(discord.ui.Select):
    def __init__(self, groups: list):
        options = []
        for g in groups[:25]:
            gid = str(g["group_id"])
            name = str(g.get("group_name") or f"Group {gid}")
            options.append(discord.SelectOption(label=name[:100], value=gid, description=f"Group ID: {gid}"[:100]))
        super().__init__(placeholder="Select Group to View Rankbinds", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        group_id = int(self.values[0])
        binds = await db.list_rankbinds(interaction.guild.id, group_id)

        if not binds:
            return await interaction.response.send_message(
                embed=embeds.info_embed("No Rankbinds", f"No rankbinds found for group `{group_id}`."),
                ephemeral=True,
            )

        blocks, total_ranks = _build_view_blocks(binds)
        pages = _paginate_lines(blocks)

        view = RankbindViewPager(
            interaction.guild.name, group_id, total_ranks, pages, interaction.user.id, interaction.user
        )
        await interaction.response.edit_message(embed=view._embed(), view=view)


class RankbindGroupSelectView(discord.ui.View):
    def __init__(self, groups: list, executor_id: int):
        super().__init__(timeout=300)
        self.executor_id = executor_id
        self.add_item(RankbindGroupSelect(groups))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.executor_id:
            await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "Only the person who ran this command can use this menu."),
                ephemeral=True,
            )
            return False
        return True


class RankbindRoleSelect(discord.ui.RoleSelect):
    """Lets staff pick any number of roles (up to Discord's 25-per-select cap)
    to bind to one rank in a single interaction - replaces the old fixed
    role/role2/.../role5 parameter approach."""

    def __init__(self, group_id: int, rank_id: int, rank_name: str, nickname_prefix: str):
        super().__init__(
            placeholder="Select role(s) to bind to this rank (up to 25 at once)...",
            min_values=1,
            max_values=25,
        )
        self.group_id = group_id
        self.rank_id = rank_id
        self.rank_name = rank_name
        self.nickname_prefix = nickname_prefix

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        for role in self.values:
            await db.add_rankbind(
                interaction.guild.id, self.group_id, self.rank_id, role.id, self.rank_name, self.nickname_prefix
            )

        role_mentions = ", ".join(r.mention for r in self.values)
        extra = f" Nickname prefix: `{self.nickname_prefix}`." if self.nickname_prefix else ""
        await interaction.followup.send(
            embed=embeds.success_embed(
                "Rankbind Added",
                f"Rank **{self.rank_name}** (`{self.rank_id}`) in group `{self.group_id}` now maps to {role_mentions}.{extra}\n\n"
                f"Run `/rankbind add` again on the same rank if you need to bind more than 25 roles at once."
            ),
            ephemeral=True,
        )


class RankbindRoleSelectView(discord.ui.View):
    def __init__(self, group_id: int, rank_id: int, rank_name: str, nickname_prefix: str):
        super().__init__(timeout=180)
        self.add_item(RankbindRoleSelect(group_id, rank_id, rank_name, nickname_prefix))


class RankBindGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="rankbind", description="Manage rank-to-role bindings.")


class RankBinds(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = RankBindGroup()

        self.group.add_command(
            app_commands.Command(name="add", description="Bind a Roblox rank to any number of Discord roles.",
                                  callback=self.rankbind_add)
        )
        self.group.add_command(
            app_commands.Command(name="remove", description="Remove a rankbind.",
                                  callback=self.rankbind_remove)
        )
        self.group.add_command(
            app_commands.Command(name="removebulk", description="Remove multiple rankbinds at once.",
                                  callback=self.rankbind_removebulk)
        )
        self.group.add_command(
            app_commands.Command(name="view", description="View every rankbind for a group.",
                                  callback=self.rankbind_view)
        )
        self.group.add_command(
            app_commands.Command(name="list", description="List rankbinds for a group.",
                                  callback=self.rankbind_list)
        )
        self.group.add_command(
            app_commands.Command(name="findrole", description="Find which rank(s) a Discord role is bound to.",
                                  callback=self.rankbind_findrole)
        )
        self.group.add_command(
            app_commands.Command(
                name="import",
                description="Bulk-import rankbinds from a file (e.g. migrating from RoWifi).",
                callback=self.rankbind_import,
            )
        )
        bot.tree.add_command(self.group)

    async def group_autocomplete(self, interaction: discord.Interaction, current: str):
        binds = await db.list_groupbinds(interaction.guild.id)
        matches = [b for b in binds if current.lower() in b["group_name"].lower()]
        return [
            app_commands.Choice(name=f"{b['group_name']} ({b['group_id']})", value=int(b["group_id"]))
            for b in matches[:25]
        ]

    async def rank_autocomplete(self, interaction: discord.Interaction, current: str):
        group_id = interaction.namespace.group_id
        if not group_id:
            return [app_commands.Choice(name="Select a group first", value=0)]

        try:
            group_id = int(group_id)
        except (TypeError, ValueError):
            return [app_commands.Choice(name="Invalid group", value=0)]

        roles = await roblox.get_group_roles(group_id)
        if not roles:
            return [app_commands.Choice(name="No ranks found for this group", value=0)]

        current_lower = (current or "").lower()
        matches = [r for r in roles if current_lower in r["name"].lower()]
        return [
            app_commands.Choice(name=f"{r['name']} (Rank {r['rank']})", value=r["rank"])
            for r in matches[:25]
        ]

    @require_level(10)
    @app_commands.describe(
        group_id="The Roblox group (start typing to search your bound groups)",
        rank_id="The rank to bind (pick a group first, then search by name)",
        nickname_prefix="Optional nickname prefix, e.g. '[OF-8]' (leave blank for none)",
    )
    @app_commands.autocomplete(group_id=group_autocomplete, rank_id=rank_autocomplete)
    async def rankbind_add(
        self,
        interaction: discord.Interaction,
        group_id: int,
        rank_id: int,
        nickname_prefix: str = "",
    ):
        await interaction.response.defer(ephemeral=True)

        role_info = await roblox.get_group_roles(group_id)
        rank_name = next((r["name"] for r in role_info if r["rank"] == rank_id), f"Rank {rank_id}")

        embed = embeds.info_embed(
            "Select Roles",
            f"Choose the Discord role(s) to bind to **{rank_name}** (`{rank_id}`) in group `{group_id}`.\n"
            f"You can select as many as you need in one go (up to 25)."
        )
        view = RankbindRoleSelectView(group_id, rank_id, rank_name, nickname_prefix)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @require_level(10)
    @app_commands.describe(
        group_id="The Roblox group (start typing to search your bound groups)",
        rank_id="The rank to unbind (pick a group first, then search by name)",
        role="Optional: remove only this specific role from the rank (leave blank to remove all roles bound to this rank)",
    )
    @app_commands.autocomplete(group_id=group_autocomplete, rank_id=rank_autocomplete)
    async def rankbind_remove(self, interaction: discord.Interaction, group_id: int, rank_id: int, role: discord.Role = None):
        await db.remove_rankbind(interaction.guild.id, group_id, rank_id, role.id if role else None)
        if role:
            await interaction.response.send_message(
                embed=embeds.success_embed("Rankbind Removed", f"{role.mention} removed from rank `{rank_id}` in group `{group_id}`.")
            )
        else:
            await interaction.response.send_message(
                embed=embeds.success_embed("Rankbind Removed", f"All roles removed from rank `{rank_id}` in group `{group_id}`.")
            )

    @require_level(10)
    @app_commands.describe(
        group_id="Remove every rankbind for this group (leave blank if using role instead)",
        role="Remove every rankbind using this specific Discord role, across all groups/ranks (leave blank if using group_id instead)",
    )
    @app_commands.autocomplete(group_id=group_autocomplete)
    async def rankbind_removebulk(self, interaction: discord.Interaction, group_id: int = None, role: discord.Role = None):
        if group_id is None and role is None:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Missing Input", "Provide either `group_id` (to clear a whole group) or `role` (to clear one role everywhere)."),
                ephemeral=True,
            )

        await interaction.response.defer(ephemeral=True)

        if group_id is not None:
            binds = await db.list_rankbinds(interaction.guild.id, group_id)
            count = len(binds)
            await db.rankbinds.delete_many({"guild_id": str(interaction.guild.id), "group_id": str(group_id)})
            await interaction.followup.send(
                embed=embeds.success_embed("Rankbinds Cleared", f"Removed **{count}** rankbind(s) for group `{group_id}`.")
            )
        else:
            result = await db.rankbinds.delete_many({"guild_id": str(interaction.guild.id), "role_id": str(role.id)})
            await interaction.followup.send(
                embed=embeds.success_embed("Rankbinds Cleared", f"Removed **{result.deleted_count}** rankbind(s) using {role.mention}.")
            )

    async def rankbind_view(self, interaction: discord.Interaction):
        groups = await db.list_groupbinds(interaction.guild.id)
        if not groups:
            return await interaction.response.send_message(
                embed=embeds.error_embed("No Groups", "This server has no Roblox groups bound yet."),
                ephemeral=True,
            )

        embed = embeds.info_embed(
            f"Viewing Rankbinds For {interaction.guild.name}",
            "Select the group you wish to view rankbinds linked to.",
        )
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, view=RankbindGroupSelectView(groups, interaction.user.id))

    @app_commands.describe(group_id="The Roblox group (start typing to search your bound groups)")
    @app_commands.autocomplete(group_id=group_autocomplete)
    async def rankbind_list(self, interaction: discord.Interaction, group_id: int):
        await interaction.response.defer(ephemeral=True)

        binds = await db.list_rankbinds(interaction.guild.id, group_id)
        if not binds:
            return await interaction.followup.send(
                embed=embeds.info_embed("No Rankbinds", f"No rankbinds found for group `{group_id}`.")
            )

        lines = _format_rankbind_lines(binds)
        pages = _paginate_lines(lines)

        if len(pages) == 1:
            return await interaction.followup.send(embed=embeds.info_embed(f"Rankbinds for {group_id}", pages[0]))

        view = RankbindListView(f"Rankbinds for {group_id}", pages, interaction.user.id)
        await interaction.followup.send(embed=view._embed(), view=view)

    @app_commands.describe(role="The Discord role to search for across all rankbinds")
    async def rankbind_findrole(self, interaction: discord.Interaction, role: discord.Role):
        await interaction.response.defer(ephemeral=True)

        binds = await db.rankbinds.find({
            "guild_id": str(interaction.guild.id),
            "role_id": str(role.id),
        }).to_list(length=100)

        if not binds:
            return await interaction.followup.send(
                embed=embeds.info_embed("No Rankbinds Found", f"{role.mention} is not bound to any rank.")
            )

        lines = []
        for b in binds:
            prefix = f" (`{b['nickname_prefix']}`)" if b.get("nickname_prefix") else ""
            lines.append(f"**{b.get('rank_name', 'Rank')}** (`{b['rank_id']}`) in group `{b['group_id']}`{prefix}")

        await interaction.followup.send(
            embed=embeds.info_embed(f"Rankbinds using {role.name}", chr(10).join(lines))
        )

    @require_level(10)
    @app_commands.describe(
        file="A .txt or .csv file, one bind per line: group_id,rank_id,role_id,nickname_prefix (prefix optional). Lines starting with # are skipped as comments."
    )
    async def rankbind_import(self, interaction: discord.Interaction, file: discord.Attachment):
        await interaction.response.defer(ephemeral=True)

        try:
            raw_bytes = await file.read()
            text = raw_bytes.decode("utf-8")
        except Exception as e:
            return await interaction.followup.send(
                embed=embeds.error_embed("Could Not Read File", f"Failed to read/decode the uploaded file: {e}")
            )

        lines = text.splitlines()
        imported = 0
        errors = []
        rank_name_cache = {}

        for line_num, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                errors.append(f"Line {line_num}: expected at least group_id,rank_id,role_id - got: {line}")
                continue

            group_id_str, rank_id_str, role_id_str = parts[0], parts[1], parts[2]
            nickname_prefix = parts[3] if len(parts) > 3 else ""

            try:
                group_id = int(group_id_str)
                rank_id = int(rank_id_str)
                role_id = int(role_id_str)
            except ValueError:
                errors.append(f"Line {line_num}: group_id, rank_id, and role_id must all be numbers - got: {line}")
                continue

            role = interaction.guild.get_role(role_id)
            if role is None:
                errors.append(f"Line {line_num}: no role with ID {role_id} exists in this server - got: {line}")
                continue

            if group_id not in rank_name_cache:
                try:
                    group_roles = await roblox.get_group_roles(group_id)
                except Exception:
                    group_roles = []
                rank_name_cache[group_id] = {r["rank"]: r["name"] for r in group_roles}

            rank_name = rank_name_cache[group_id].get(rank_id, f"Rank {rank_id}")

            await db.add_rankbind(interaction.guild.id, group_id, rank_id, role_id, rank_name, nickname_prefix)
            imported += 1

        summary = f"Imported **{imported}** rankbind(s)."
        if errors:
            summary += f" **{len(errors)}** line(s) skipped due to errors - see the attached report."

        embed = embeds.success_embed("Rankbind Import Complete", summary) if imported else embeds.error_embed("Rankbind Import Failed", summary)

        if errors:
            report_text = chr(10).join(errors)
            report_file = discord.File(io.BytesIO(report_text.encode("utf-8")), filename="rankbind_import_errors.txt")
            await interaction.followup.send(embed=embed, file=report_file)
        else:
            await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankBinds(bot))
