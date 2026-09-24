"""
cogs/boosterroles.py
----------------------
When a member starts boosting the server, creates a private channel
(visible only to them + staff), posts the list of colour roles from
guild_config's "colour_roles" (comma-separated role NAMES, set via
/setup -> Roles -> Colour Roles), lets them pick one via a dropdown,
assigns it, and closes the channel shortly after.

Colour roles are mutually exclusive - picking a new one removes any
other colour role from the same configured set that the member already
has, so re-picking (or boosting again after a prior pick) swaps cleanly
instead of stacking roles.

Private channels are created under guild_config's
"booster_channel_category_id" if set (via /setup -> Channels -> Booster
Colour-Select Category), otherwise at the top level of the server.
"""

import asyncio
import discord
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from config import settings

CHANNEL_AUTO_CLOSE_AFTER_PICK_SECONDS = 8
CHANNEL_TIMEOUT_SECONDS = 600  # auto-close if nobody picks


def _get_colour_role_names(guild_config: dict) -> list:
    raw = guild_config.get("colour_roles", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


class ColourSelect(discord.ui.Select):
    def __init__(self, guild: discord.Guild, role_names: list, member: discord.Member):
        self.member = member
        self.role_names = role_names

        options = [discord.SelectOption(label=name) for name in role_names[:25]]
        super().__init__(placeholder="Choose your colour...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.member.id:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not For You", "This colour picker isn't yours."),
                ephemeral=True,
            )

        await interaction.response.defer()

        chosen_name = self.values[0]
        guild = interaction.guild
        chosen_role = discord.utils.get(guild.roles, name=chosen_name)

        if chosen_role is None:
            return await interaction.followup.send(
                embed=embeds.error_embed("Role Missing", f"The **{chosen_name}** role no longer exists in this server. Contact staff."),
            )

        other_colour_roles = [
            discord.utils.get(guild.roles, name=name)
            for name in self.role_names
            if name != chosen_name
        ]
        other_colour_roles = [r for r in other_colour_roles if r and r in self.member.roles]

        try:
            if other_colour_roles:
                await self.member.remove_roles(*other_colour_roles, reason="Swapping booster colour role")
            await self.member.add_roles(chosen_role, reason="Booster colour role selected")
        except discord.Forbidden:
            return await interaction.followup.send(
                embed=embeds.error_embed("Missing Permissions", "The bot doesn't have permission to manage that role - check role hierarchy.")
            )

        for item in self.view.children:
            item.disabled = True
        await interaction.message.edit(view=self.view)

        await interaction.followup.send(
            embed=embeds.success_embed("Colour Set", f"You're now **{chosen_role.mention}**. This channel will close shortly.")
        )

        await asyncio.sleep(CHANNEL_AUTO_CLOSE_AFTER_PICK_SECONDS)
        try:
            await interaction.channel.delete(reason="Booster colour selection complete")
        except Exception:
            pass


class ColourSelectView(discord.ui.View):
    def __init__(self, guild: discord.Guild, role_names: list, member: discord.Member):
        super().__init__(timeout=CHANNEL_TIMEOUT_SECONDS)
        self.channel = None
        self.add_item(ColourSelect(guild, role_names, member))

    async def on_timeout(self):
        if self.channel:
            try:
                await self.channel.delete(reason="Booster colour selection timed out")
            except Exception:
                pass


class BoosterRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is None and after.premium_since is not None:
            await self._start_colour_selection(after)

    async def _start_colour_selection(self, member: discord.Member):
        guild = member.guild
        guild_config = await db.get_guild_config(guild.id)

        role_names = _get_colour_role_names(guild_config)
        if not role_names:
            return  # no colour roles configured for this server - nothing to offer

        category = None
        category_id = guild_config.get("booster_channel_category_id")
        if category_id:
            category = guild.get_channel(int(category_id))

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }

        try:
            channel = await guild.create_text_channel(
                name=f"colour-{member.name}"[:100],
                category=category,
                overwrites=overwrites,
                reason="Booster colour role selection",
            )
        except discord.Forbidden:
            return  # bot lacks Manage Channels - silently skip rather than error into nowhere

        bullet_list = "\n".join(f"• {n and discord.utils.get(guild.roles, name=n).mention if discord.utils.get(guild.roles, name=n) else n}" for n in role_names)

        embed = discord.Embed(
            description=f"Hello {member.mention}\n\n**These are the colour roles you can choose from:**\n{bullet_list}",
            color=settings.EMBED_COLOR,
        )
        embed.set_author(name=settings.BOT_NAME, icon_url=settings.BOT_ICON_URL)

        view = ColourSelectView(guild, role_names, member)
        message = await channel.send(embed=embed, view=view)
        view.channel = channel


async def setup(bot: commands.Bot):
    await bot.add_cog(BoosterRoles(bot))
