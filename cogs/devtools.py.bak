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
/dev dbdump    - dump one or every MongoDB collection as a JSON file,
                 sent as an ephemeral response so it's only visible to
                 whoever ran the command, not posted in-channel. Read-only.
                 This includes real PII across every guild the bot is in
                 (verification records, IPs/countries logged at
                 verification, tenant tokens - encrypted, not plaintext -
                 admin levels, warnings, tickets). Delete the file locally
                 once you're done with it, and never re-post it somewhere
                 else. Discord's per-message file size cap (tied to the
                 server's boost level) applies - dump a single collection
                 with the `collection` option if a full dump is too large.
/dev guilds    - list every server the bot is currently in
/dev shutdown  - gracefully stop the bot (with a confirm button)
!leave         - prefix command that makes the bot leave a server
!eval          - prefix command that executes arbitrary async Python.
                 Owner-only, but genuinely dangerous - if the bot token or
                 your Discord account is ever compromised, this becomes a
                 full remote-code-execution backdoor into wherever the bot
                 is hosted (reads env vars/secrets, touches the filesystem,
                 etc). Keep this bot's token and your own account secured
                 accordingly (2FA, no sharing the token, no pasting it
                 anywhere public).
"""

import contextlib
import io
import json
import textwrap
import time
import traceback

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
        self._last_eval_result = None
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
            app_commands.Command(name="dbdump", description="Dump one or all MongoDB collections as a JSON file (owner only, ephemeral).",
                                  callback=self.dev_dbdump)
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

    async def collection_autocomplete(self, interaction: discord.Interaction, current: str):
        try:
            names = sorted(await db.db.list_collection_names())
        except Exception:
            return []
        matches = [c for c in names if current.lower() in c.lower()]
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
    @app_commands.describe(
        collection="Leave empty to dump every collection. Autocompletes from what's actually in the database.",
    )
    @app_commands.autocomplete(collection=collection_autocomplete)
    async def dev_dbdump(self, interaction: discord.Interaction, collection: str = None):
        """Read-only. Sent as an ephemeral response so only the command
        invoker can see it - nothing is posted visibly in the channel."""
        await interaction.response.defer(ephemeral=True)

        try:
            if collection:
                names = [collection]
            else:
                names = sorted(await db.db.list_collection_names())

            dump = {}
            for name in names:
                dump[name] = await db.db[name].find({}).to_list(length=None)

            text = json.dumps(dump, indent=2, default=str)
            filename = f"{collection}_dump.json" if collection else "full_db_dump.json"
            file = discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)
        except Exception as e:
            return await interaction.followup.send(
                embed=embeds.error_embed("Dump Failed", f"```{type(e).__name__}: {e}```")
            )

        total_docs = sum(len(v) for v in dump.values())
        description = (
            f"Dumped **{len(dump)}** collection(s), **{total_docs}** document(s) total. "
            f"This is only visible to you and won't be posted in-channel - please delete it locally once "
            f"you're done, and don't re-share it (it contains real user PII across every server I'm in)."
        )
        await interaction.followup.send(
            embed=embeds.success_embed("Database Dump", description), file=file, ephemeral=True
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

    @commands.command(name="leave")
    @commands.is_owner()
    async def leave(self, ctx: commands.Context, guild_id: int = None):
        """!leave [guild_id] - makes the bot leave a server. Leaves the
        current server if no ID is given, otherwise leaves the server with
        that ID (handy for leaving one remotely without joining a channel
        there first)."""
        guild = self.bot.get_guild(guild_id) if guild_id else ctx.guild
        if guild is None:
            return await ctx.send(f"I'm not in a server with ID `{guild_id}`.")

        farewell_channel = guild.system_channel
        if farewell_channel is None or not farewell_channel.permissions_for(guild.me).send_messages:
            farewell_channel = next(
                (c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None
            )

        if farewell_channel:
            try:
                embed = discord.Embed(
                    title="Standing Down",
                    description=(
                        f"Royal Guard has been ordered to withdraw from **{guild.name}**. "
                        f"It's been an honor serving with you all. Farewell, soldiers."
                    ),
                    color=discord.Color.dark_red(),
                )
                await farewell_channel.send(embed=embed)
            except discord.Forbidden:
                pass

        name, gid = guild.name, guild.id
        await guild.leave()
        await ctx.send(f"Left **{name}** (`{gid}`).")

    @commands.command(name="eval")
    @commands.is_owner()
    async def eval_cmd(self, ctx: commands.Context, *, body: str):
        """!eval <code> - owner-only. Executes arbitrary async Python.
        `body` is wrapped in an async function so top-level `await` works,
        e.g. `!eval await ctx.guild.leave()`. Has access to ctx, bot,
        discord, commands, guild, channel, author, and `_` (the last
        eval's return value)."""
        env = {
            "bot": self.bot,
            "ctx": ctx,
            "discord": discord,
            "commands": commands,
            "guild": ctx.guild,
            "channel": ctx.channel,
            "author": ctx.author,
            "_": self._last_eval_result,
        }
        env.update(globals())

        body = body.strip()
        if body.startswith("```") and body.endswith("```"):
            body = "\n".join(body.split("\n")[1:-1])
        body = body.strip("`\n ")

        stdout = io.StringIO()
        to_compile = "async def __eval_func__():\n" + textwrap.indent(body, "    ")

        try:
            exec(to_compile, env)
        except Exception as e:
            return await ctx.send(f"```py\n{e.__class__.__name__}: {e}\n```")

        func = env["__eval_func__"]
        try:
            with contextlib.redirect_stdout(stdout):
                ret = await func()
        except Exception:
            value = stdout.getvalue()
            await ctx.send(f"```py\n{value}{traceback.format_exc()}\n```"[:2000])
        else:
            value = stdout.getvalue()
            if ret is None:
                if value:
                    await ctx.send(f"```py\n{value}\n```"[:2000])
                else:
                    await ctx.message.add_reaction("\u2705")
            else:
                self._last_eval_result = ret
                await ctx.send(f"```py\n{value}{ret}\n```"[:2000])


async def setup(bot: commands.Bot):
    await bot.add_cog(DevTools(bot))
