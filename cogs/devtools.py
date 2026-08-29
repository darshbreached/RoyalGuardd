"""
cogs/devtools.py
------------------
Owner-only developer utilities. Gated via bot.is_owner(), which discord.py
resolves against the Discord application's actual owner (or team members)
fetched from Discord itself - no separate config needed, and this is
intentionally NOT tied to the per-guild admin_levels system, since these
are bot-wide operations that shouldn't be grantable by a server admin.

/dev sync      - resync slash commands, either globally (up to 1hr to
                 propagate) or instantly to the current server (for testing)
/dev reload    - hot-reload a single already-loaded cog
/dev cogs      - list every currently loaded cog
/dev dbstatus  - ping MongoDB and report latency
/dev guilds    - list every server the bot is currently in
/dev shutdown  - gracefully stop the bot (with a confirm button)

Deliberately does NOT include an eval/exec command - arbitrary code
execution is a serious liability even gated to the owner (e.g. if the
token or account were ever compromised via another route). Add only if
you explicitly want that risk.
"""

import time
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds


async def _is_owner_check(interaction: discord.Interaction) -> bool:
    is_owner = await interaction.client.is_owner(interaction.user)
    if not is_owner:
        raise app_commands.CheckFailure("Developer tools are restricted to the bot owner.")
    return True


class ShutdownConfirmView(discord.ui.View):
    def __init__(self, executor_id: int):
        super().__init__(timeout=30)
        self.executor_id = executor_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.executor_id

    @discord.ui.button(label="Confirm Shutdown", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=embeds.warning_embed("Shutting Down", "Bot is shutting down now."), view=None
        )
        await interaction.client.close()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=embeds.info_embed("Cancelled", "Shutdown cancelled."), view=None
        )


class DevGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="dev", description="Owner-only developer tools.")


class DevTools(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = DevGroup()

        self.group.add_command(
            app_commands.Command(name="sync", description="Resync slash commands.", callback=self.dev_sync)
        )
        self.group.add_command(
            app_commands.Command(name="reload", description="Hot-reload a single cog.", callback=self.dev_reload)
        )
        self.group.add_command(
            app_commands.Command(name="cogs", description="List currently loaded cogs.", callback=self.dev_cogs)
        )
        self.group.add_command(
            app_commands.Command(name="dbstatus", description="Check MongoDB connectivity.", callback=self.dev_dbstatus)
        )
        self.group.add_command(
            app_commands.Command(name="guilds", description="List every server the bot is in.", callback=self.dev_guilds)
        )
        self.group.add_command(
            app_commands.Command(name="shutdown", description="Gracefully stop the bot.", callback=self.dev_shutdown)
        )
        bot.tree.add_command(self.group)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=embeds.error_embed("Not Allowed", "Developer tools are restricted to the bot owner."),
                    ephemeral=True,
                )
            return
        raise error

    async def reload_autocomplete(self, interaction: discord.Interaction, current: str):
        loaded = sorted(interaction.client.extensions.keys())
        matches = [c for c in loaded if current.lower() in c.lower()]
        return [app_commands.Choice(name=c, value=c) for c in matches[:25]]

    @app_commands.check(_is_owner_check)
    @app_commands.describe(scope="Global takes up to an hour to propagate everywhere. Guild-only is instant but only applies here.")
    @app_commands.choices(scope=[
        app_commands.Choice(name="Global (slow, up to 1 hour)", value="global"),
        app_commands.Choice(name="This server only (instant, for testing)", value="guild"),
    ])
    async def dev_sync(self, interaction: discord.Interaction, scope: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        if scope.value == "guild":
            interaction.client.tree.copy_global_to(guild=interaction.guild)
            synced = await interaction.client.tree.sync(guild=interaction.guild)
            await interaction.followup.send(
                embed=embeds.success_embed("Synced", f"Synced **{len(synced)}** command(s) to this server instantly.")
            )
        else:
            synced = await interaction.client.tree.sync()
            await interaction.followup.send(
                embed=embeds.success_embed(
                    "Synced",
                    f"Synced **{len(synced)}** command(s) globally. Can take up to an hour to appear everywhere."
                )
            )

    @app_commands.check(_is_owner_check)
    @app_commands.describe(cog="The cog to reload, e.g. cogs.rankbinds")
    @app_commands.autocomplete(cog=reload_autocomplete)
    async def dev_reload(self, interaction: discord.Interaction, cog: str):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.client.reload_extension(cog)
        except Exception as e:
            return await interaction.followup.send(
                embed=embeds.error_embed("Reload Failed", f"```{type(e).__name__}: {e}```")
            )
        await interaction.followup.send(embed=embeds.success_embed("Reloaded", f"`{cog}` reloaded successfully."))

    @app_commands.check(_is_owner_check)
    async def dev_cogs(self, interaction: discord.Interaction):
        loaded = sorted(interaction.client.extensions.keys())
        text = "\n".join(f"- {c}" for c in loaded) if loaded else "No cogs loaded."
        embed = embeds.info_embed(f"Loaded Cogs ({len(loaded)})", text[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.check(_is_owner_check)
    async def dev_dbstatus(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        start = time.monotonic()
        try:
            await db.rankbinds.database.command("ping")
            latency_ms = round((time.monotonic() - start) * 1000, 1)
            await interaction.followup.send(
                embed=embeds.success_embed("MongoDB Healthy", f"Ping successful - **{latency_ms}ms**.")
            )
        except Exception as e:
            await interaction.followup.send(
                embed=embeds.error_embed("MongoDB Unreachable", f"```{type(e).__name__}: {e}```")
            )

    @app_commands.check(_is_owner_check)
    async def dev_guilds(self, interaction: discord.Interaction):
        guilds = interaction.client.guilds
        lines = [f"**{g.name}** (`{g.id}`) - {g.member_count} members" for g in guilds]
        text = "\n".join(lines) if lines else "Not in any servers."
        embed = embeds.info_embed(f"Servers ({len(guilds)})", text[:4000])
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.check(_is_owner_check)
    async def dev_shutdown(self, interaction: discord.Interaction):
        embed = embeds.warning_embed(
            "Confirm Shutdown",
            "This will disconnect the bot completely. It will only come back online if Railway restarts the service automatically. Are you sure?"
        )
        await interaction.response.send_message(embed=embed, view=ShutdownConfirmView(interaction.user.id), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevTools(bot))
