cat > cogs/devtools.py << 'PYEOF'
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
/dev guilds    - list every server the bot is in, with an invite link for each
/dev shutdown  - gracefully stop the bot (with a confirm button)

Prefix commands (all owner-only):
!leave [id]            - make the bot leave a server
!eval <code>           - execute arbitrary async Python. A single expression
                         is auto-returned, so `!eval len(bot.guilds)` works.
                         Long output is sent as a file.
!again                 - re-run the last !eval / !timeit code
!timeit <code>         - run code once and report how long it took
!inspect <expr>        - evaluate an expression, show its type, repr and
                         public attributes
!sh <command>          - run a shell command on the host (60s timeout)
!mongo <coll> [filter] - read-only find() on a collection, first 10 docs.
                         Filter is JSON, e.g. !mongo warnings {"guild_id": 123}
!source <command>      - send the source code of a prefix or slash command
!load / !unload <cog>  - load or unload an extension, e.g. cogs.rankbinds
!reload <cog>          - prefix version of /dev reload

!eval and !sh are genuinely dangerous - owner-only, but if the bot token or
your Discord account is ever compromised, they become a full remote-code-
execution backdoor into wherever the bot is hosted (reads env vars/secrets,
touches the filesystem, etc). Keep this bot's token and your own account
secured accordingly (2FA, no sharing the token, no pasting it anywhere
public). Never run !eval / !sh / !mongo output through a channel other
people can read - it can contain secrets and PII.
"""

import asyncio
import contextlib
import inspect
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


INVITE_TTL_SECONDS = 3600
_invite_cache = {}  # guild_id -> (url, expires_at)

MAX_INLINE = 1900
SHELL_TIMEOUT = 60


def _clean_code(body: str) -> str:
    body = body.strip()
    if body.startswith("```") and body.endswith("```"):
        body = "\n".join(body.split("\n")[1:-1])
    return body.strip("`\n ")


def _defang(text: str) -> str:
    """Stop output containing ``` from breaking out of our code fence."""
    return text.replace("```", "`\u200b``")


async def _send_output(ctx: commands.Context, text: str, lang: str = "py", filename: str = "output.txt"):
    """Send as a code block, or as a file if it won't fit in one message."""
    if not text:
        text = "(no output)"
    if len(text) <= MAX_INLINE:
        return await ctx.send(f"```{lang}\n{_defang(text)}\n```")
    file = discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)
    return await ctx.send(f"Output was {len(text)} chars, sent as a file.", file=file)


async def _guild_invite(guild: discord.Guild) -> str:
    """An invite URL for the guild, or a short note saying why there isn't one.

    Reuses a permanent invite when the bot may list invites; otherwise makes a
    short-lived single-use one (and remembers it so re-running /dev guilds
    doesn't spam the server's audit log with new invites)."""
    cached = _invite_cache.get(guild.id)
    if cached and cached[1] - time.time() > 300:
        return cached[0]

    me = guild.me
    if me is None:
        return "(bot member not cached)"
    if "INVITES_DISABLED" in guild.features:
        return "(invites are paused in this server)"

    if me.guild_permissions.manage_guild:
        try:
            for inv in await guild.invites():
                if inv.max_age == 0 and inv.max_uses == 0 and not inv.temporary:
                    return inv.url
        except discord.HTTPException:
            pass

    tried = 0
    for channel in [guild.system_channel] + list(guild.text_channels):
        if channel is None or not channel.permissions_for(me).create_instant_invite:
            continue
        if tried >= 5:
            break
        tried += 1
        try:
            invite = await channel.create_invite(
                max_age=INVITE_TTL_SECONDS, max_uses=1, unique=True,
                reason="Bot owner requested via /dev guilds",
            )
        except discord.HTTPException:
            continue
        _invite_cache[guild.id] = (invite.url, time.time() + INVITE_TTL_SECONDS)
        return invite.url
    return "(no invite: the bot lacks Create Invite permission here)"


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
        self._last_code = None
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
        await interaction.response.defer(ephemeral=True)
        guilds = sorted(interaction.client.guilds, key=lambda g: g.member_count or 0, reverse=True)
        if not guilds:
            return await interaction.followup.send(
                embed=embeds.info_embed("Servers (0)", "Not in any servers."), ephemeral=True
            )

        sem = asyncio.Semaphore(4)

        async def fetch(g):
            async with sem:
                try:
                    return await _guild_invite(g)
                except Exception as e:
                    return f"(invite lookup failed: {type(e).__name__})"

        links = await asyncio.gather(*(fetch(g) for g in guilds))

        entries = []
        for g, link in zip(guilds, links):
            owner = f"<@{g.owner_id}>" if g.owner_id else "unknown"
            entries.append(
                f"**{discord.utils.escape_markdown(g.name)}** · `{g.id}` · {g.member_count} members\n"
                f"Owner: {owner}\n{link}"
            )

        pages, current = [], ""
        for entry in entries:
            if current and len(current) + len(entry) + 2 > 3800:
                pages.append(current)
                current = ""
            current += ("\n\n" if current else "") + entry
        if current:
            pages.append(current)

        for i, page in enumerate(pages, start=1):
            title = f"Servers ({len(guilds)})" + (f" - page {i}/{len(pages)}" if len(pages) > 1 else "")
            embed = embeds.info_embed(title, page)
            if i == len(pages):
                embed.set_footer(text="To make the bot leave a server: !leave <server id>")
            await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.check(_is_owner_check)
    async def dev_shutdown(self, interaction: discord.Interaction):
        embed = embeds.warning_embed(
            "Confirm Shutdown",
            "This will disconnect the bot completely. It will only come back online if Railway restarts the service automatically. Are you sure?"
        )
        await interaction.response.send_message(embed=embed, view=ShutdownConfirmView(interaction.user.id), ephemeral=True)

    # ------------------------------------------------------------------
    # Prefix commands
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Eval family
    # ------------------------------------------------------------------

    def _eval_env(self, ctx: commands.Context) -> dict:
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
        return env

    async def _execute(self, ctx: commands.Context, body: str):
        """Compile and run `body` as an async function.

        Returns (status, ret, stdout, error_text) where status is one of
        "compile_error", "runtime_error", "ok". A single-line body is first
        tried as `return (<body>)` so bare expressions auto-return."""
        env = self._eval_env(ctx)
        func = None

        if "\n" not in body:
            try:
                exec("async def __eval_func__():\n    return (" + body + "\n    )", env)
                func = env["__eval_func__"]
            except SyntaxError:
                func = None

        if func is None:
            to_compile = "async def __eval_func__():\n" + textwrap.indent(body, "    ")
            try:
                exec(to_compile, env)
            except Exception as e:
                return "compile_error", None, "", f"{e.__class__.__name__}: {e}"
            func = env["__eval_func__"]

        stdout = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout):
                ret = await func()
        except Exception:
            return "runtime_error", None, stdout.getvalue(), traceback.format_exc()
        return "ok", ret, stdout.getvalue(), ""

    async def _run_and_report(self, ctx: commands.Context, body: str, *, timed: bool = False):
        self._last_code = body
        start = time.perf_counter()
        status, ret, out, err = await self._execute(ctx, body)
        elapsed_ms = (time.perf_counter() - start) * 1000

        if status == "compile_error":
            return await _send_output(ctx, err, filename="error.txt")
        if status == "runtime_error":
            return await _send_output(ctx, f"{out}{err}", filename="error.txt")

        if ret is not None:
            self._last_eval_result = ret

        if timed:
            text = f"{out}{ret if ret is not None else ''}".rstrip()
            footer = f"[ran in {elapsed_ms:.2f}ms]"
            return await _send_output(ctx, f"{text}\n{footer}" if text else footer, filename="output.txt")

        if ret is None:
            if out:
                return await _send_output(ctx, out, filename="output.txt")
            return await ctx.message.add_reaction("\u2705")
        return await _send_output(ctx, f"{out}{ret}", filename="output.txt")

    @commands.command(name="eval")
    @commands.is_owner()
    async def eval_cmd(self, ctx: commands.Context, *, body: str):
        """!eval <code> - owner-only. Executes arbitrary async Python.
        `body` is wrapped in an async function so top-level `await` works,
        e.g. `!eval await ctx.guild.leave()`. A single expression is
        returned automatically (`!eval len(bot.guilds)`); for multi-line
        code use `return` yourself. Has access to ctx, bot, discord,
        commands, guild, channel, author, and `_` (the last eval's return
        value)."""
        await self._run_and_report(ctx, _clean_code(body))

    @commands.command(name="again", aliases=["reeval"])
    @commands.is_owner()
    async def again_cmd(self, ctx: commands.Context):
        """!again - re-runs the last !eval / !timeit code."""
        if not self._last_code:
            return await ctx.send("Nothing to re-run yet.")
        await self._run_and_report(ctx, self._last_code)

    @commands.command(name="timeit")
    @commands.is_owner()
    async def timeit_cmd(self, ctx: commands.Context, *, body: str):
        """!timeit <code> - runs the code once and reports how long it took."""
        await self._run_and_report(ctx, _clean_code(body), timed=True)

    @commands.command(name="inspect", aliases=["ins"])
    @commands.is_owner()
    async def inspect_cmd(self, ctx: commands.Context, *, expr: str):
        """!inspect <expr> - evaluates an expression and shows its type,
        repr and public attributes. e.g. `!inspect ctx.guild`."""
        expr = _clean_code(expr)
        status, ret, out, err = await self._execute(ctx, expr)
        if status != "ok":
            return await _send_output(ctx, f"{out}{err}", filename="error.txt")

        self._last_eval_result = ret
        attrs = sorted(a for a in dir(ret) if not a.startswith("_"))
        lines = [
            f"type : {type(ret).__module__}.{type(ret).__qualname__}",
            f"repr : {repr(ret)[:500]}",
        ]
        doc = inspect.getdoc(ret)
        if doc:
            lines.append(f"doc  : {doc.splitlines()[0][:200]}")
        lines.append(f"attrs ({len(attrs)}):")
        lines.append(textwrap.fill(", ".join(attrs), width=100) if attrs else "(none)")
        await _send_output(ctx, "\n".join(lines), lang="yaml", filename="inspect.txt")

    @commands.command(name="sh", aliases=["shell"])
    @commands.is_owner()
    async def sh_cmd(self, ctx: commands.Context, *, command: str):
        """!sh <command> - runs a shell command on the host and returns
        stdout + stderr. Killed after 60 seconds."""
        command = _clean_code(command)
        async with ctx.typing():
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return await ctx.send(f"Timed out after {SHELL_TIMEOUT}s and was killed.")

        text = raw.decode("utf-8", errors="replace").rstrip()
        footer = f"[exit {proc.returncode}]"
        await _send_output(ctx, f"{text}\n{footer}" if text else footer, lang="sh", filename="shell.txt")

    @commands.command(name="mongo")
    @commands.is_owner()
    async def mongo_cmd(self, ctx: commands.Context, collection: str, *, query: str = "{}"):
        """!mongo <collection> [json filter] - read-only find(), returns the
        first 10 matching documents plus a total count. e.g.
        `!mongo warnings {"guild_id": 123456789}`. This can contain PII -
        only run it somewhere private."""
        try:
            filt = json.loads(_clean_code(query) or "{}")
            if not isinstance(filt, dict):
                raise ValueError("filter must be a JSON object")
        except Exception as e:
            return await ctx.send(f"Bad filter: `{type(e).__name__}: {e}`")

        try:
            coll = db.db[collection]
            total = await coll.count_documents(filt)
            docs = await coll.find(filt).limit(10).to_list(length=10)
        except Exception as e:
            return await _send_output(ctx, f"{type(e).__name__}: {e}", filename="error.txt")

        text = json.dumps(docs, indent=2, default=str)
        header = f"// {total} match(es), showing {len(docs)}\n"
        await _send_output(ctx, header + text, lang="json", filename=f"{collection}_query.json")

    @commands.command(name="source", aliases=["src"])
    @commands.is_owner()
    async def source_cmd(self, ctx: commands.Context, *, name: str):
        """!source <command> - shows the source of a prefix command
        (`!source eval`) or slash command (`!source dev sync`)."""
        name = name.strip().lstrip("!/")
        cmd = self.bot.get_command(name)
        callback = cmd.callback if cmd else None

        if callback is None:
            parts = name.split()
            slash = self.bot.tree.get_command(parts[0])
            for part in parts[1:]:
                slash = slash.get_command(part) if slash and hasattr(slash, "get_command") else None
            callback = getattr(slash, "callback", None)

        if callback is None:
            return await ctx.send(f"No command named `{name}`.")

        try:
            src = inspect.getsource(callback)
        except (OSError, TypeError) as e:
            return await ctx.send(f"Couldn't get source: `{type(e).__name__}: {e}`")
        await _send_output(ctx, textwrap.dedent(src), filename=f"{name.replace(' ', '_')}.py")

    # ------------------------------------------------------------------
    # Extension management
    # ------------------------------------------------------------------

    async def _ext_action(self, ctx: commands.Context, action: str, cog: str):
        fn = getattr(self.bot, f"{action}_extension")
        try:
            await fn(cog)
        except Exception as e:
            return await _send_output(ctx, f"{type(e).__name__}: {e}", filename="error.txt")
        await ctx.message.add_reaction("\u2705")

    @commands.command(name="load")
    @commands.is_owner()
    async def load_cmd(self, ctx: commands.Context, cog: str):
        """!load <cog> - load an extension, e.g. `!load cogs.rankbinds`."""
        await self._ext_action(ctx, "load", cog)

    @commands.command(name="unload")
    @commands.is_owner()
    async def unload_cmd(self, ctx: commands.Context, cog: str):
        """!unload <cog> - unload an extension. Don't unload cogs.devtools
        itself unless you can reload it some other way."""
        await self._ext_action(ctx, "unload", cog)

    @commands.command(name="reload")
    @commands.is_owner()
    async def reload_cmd(self, ctx: commands.Context, cog: str):
        """!reload <cog> - prefix version of /dev reload."""
        await self._ext_action(ctx, "reload", cog)


async def setup(bot: commands.Bot):
    await bot.add_cog(DevTools(bot))
PYEOF