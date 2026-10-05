"""cogs/dmannounce.py — Announce to members by direct message.

/dmannounce send [role]  -> modal (title, message, optional image) -> preview
                            -> Send / Cancel -> throttled DM broadcast
/dmannounce cancel       -> stop a broadcast that is currently running

Safeguards (mass-DMing is the fastest way to get a bot flagged by Discord):
  * administrators only
  * preview + explicit confirmation before anything is sent
  * bots are skipped
  * one broadcast per server at a time
  * ~1.5s between DMs, and the run aborts if Discord says we're opening DMs too fast
  * hard cap on recipients per broadcast (target a role for bigger groups)
  * every DM tells the member why they got it
  * every broadcast is logged to Mongo ("dm_announcements")

Requires the Server Members intent (so the bot can see the member list).
"""

import asyncio
import math

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds

LOG_COLLECTION = "dm_announcements"

DELAY_SECONDS = 1.5      # pause between DMs
MAX_RECIPIENTS = 500     # per broadcast (500 * 1.5s ~ 12.5 min, inside the 15-min interaction window)
PROGRESS_EVERY = 20      # update the progress message every N recipients


def _clean_link(value: str):
    value = str(value or "").strip()
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")) and " " not in value:
        return value
    return None


def _is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


def _fmt_minutes(count: int) -> str:
    seconds = count * DELAY_SECONDS
    if seconds < 60:
        return "under a minute"
    return f"about {math.ceil(seconds / 60)} minute(s)"


class Broadcast:
    """In-memory state of a running broadcast."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.cancelled = False


def build_dm_embed(guild: discord.Guild, title: str, message: str, image: str) -> discord.Embed:
    embed = embeds.base_embed()
    embed.title = title
    embed.description = (
        f"{message}\n\n"
        f"*You received this because you are a member of **{guild.name}**.*"
    )
    embed.set_author(name=guild.name, icon_url=guild.icon.url if guild.icon else None)
    if image:
        embed.set_image(url=image)
    embed.timestamp = discord.utils.utcnow()
    return embed


class AnnounceModal(discord.ui.Modal, title="DM Announcement"):
    announcement_title = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        placeholder="e.g. Training event this Saturday",
        required=True,
        max_length=100,
    )
    message = discord.ui.TextInput(
        label="Message",
        style=discord.TextStyle.paragraph,
        placeholder="What do you want to tell your members?",
        required=True,
        max_length=3500,
    )
    image = discord.ui.TextInput(
        label="Image link (optional)",
        style=discord.TextStyle.short,
        placeholder="https://i.imgur.com/...png",
        required=False,
        max_length=500,
    )

    def __init__(self, cog: "DMAnnounce", role):
        super().__init__()
        self.cog = cog
        self.role = role

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        image = _clean_link(self.image.value)
        if image is None:
            return await interaction.followup.send(
                embed=embeds.error_embed("DM Announce", "The image link must start with https:// . Run the command again."),
                ephemeral=True,
            )

        guild = interaction.guild
        if guild.id in self.cog.active:
            return await interaction.followup.send(
                embed=embeds.error_embed("DM Announce", "A broadcast is already running in this server. Wait for it to finish or use `/dmannounce cancel`."),
                ephemeral=True,
            )

        recipients = await self.cog.collect_recipients(guild, self.role)

        if not recipients:
            return await interaction.followup.send(
                embed=embeds.error_embed("DM Announce", "There is nobody to send this to (only bots matched)."),
                ephemeral=True,
            )
        if len(recipients) > MAX_RECIPIENTS:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "DM Announce",
                    f"That would reach **{len(recipients)}** members, and the limit is **{MAX_RECIPIENTS}** per broadcast. "
                    f"Target a smaller role with `/dmannounce send role:`.",
                ),
                ephemeral=True,
            )

        dm_embed = build_dm_embed(guild, str(self.announcement_title.value).strip(), str(self.message.value).strip(), image)
        target = f"members with {self.role.mention}" if self.role and not self.role.is_default() else "**all members**"
        preview = embeds.warning_embed(
            "DM Announce — Preview",
            f"This will be sent to **{len(recipients)}** {target}.\n"
            f"Bots are skipped.\n"
            f"Estimated time: {_fmt_minutes(len(recipients))}.\n\n"
            f"The embed below is exactly what they'll receive. Press **Send** to start.",
        )
        view = ConfirmView(self.cog, interaction.user.id, dm_embed, recipients, str(self.announcement_title.value).strip())
        await interaction.followup.send(embeds=[preview, dm_embed], view=view, ephemeral=True)


class ConfirmView(discord.ui.View):
    def __init__(self, cog: "DMAnnounce", invoker_id: int, dm_embed: discord.Embed, recipients: list, title: str):
        super().__init__(timeout=600)
        self.cog = cog
        self.invoker_id = invoker_id
        self.dm_embed = dm_embed
        self.recipients = recipients
        self.title_text = title

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who started this can use it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Send", style=discord.ButtonStyle.success)
    async def send(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild.id in self.cog.active:
            return await interaction.response.send_message("A broadcast is already running in this server.", ephemeral=True)
        await interaction.response.defer()
        self.stop()
        await self.cog.run_broadcast(interaction, self.dm_embed, self.recipients, self.title_text)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embeds=[embeds.info_embed("DM Announce", "Cancelled. Nothing was sent.")], view=None
        )


class DMAnnounce(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active = {}  # guild_id -> Broadcast

    dm = app_commands.Group(name="dmannounce", description="Announce to members by direct message.", guild_only=True)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #

    @dm.command(name="send", description="Write an announcement and DM it to members.")
    @app_commands.describe(role="Only DM members with this role (leave empty for everyone)")
    async def send(self, interaction: discord.Interaction, role: discord.Role = None):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error_embed("No Permission", "Only server administrators can send DM announcements."), ephemeral=True
            )
        if interaction.guild.id in self.active:
            return await interaction.response.send_message(
                embed=embeds.error_embed("DM Announce", "A broadcast is already running in this server."), ephemeral=True
            )
        await interaction.response.send_modal(AnnounceModal(self, role))

    @dm.command(name="cancel", description="Stop the broadcast that is currently running.")
    async def cancel(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            return await interaction.response.send_message(
                embed=embeds.error_embed("No Permission", "Only server administrators can do this."), ephemeral=True
            )
        state = self.active.get(interaction.guild.id)
        if not state:
            return await interaction.response.send_message(
                embed=embeds.info_embed("DM Announce", "No broadcast is running right now."), ephemeral=True
            )
        state.cancelled = True
        await interaction.response.send_message(
            embed=embeds.info_embed("DM Announce", "Stopping after the current message. You'll get the final report shortly."), ephemeral=True
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    async def collect_recipients(self, guild: discord.Guild, role):
        """Return the non-bot members to DM."""
        if not guild.chunked:
            try:
                await guild.chunk()
            except Exception as e:
                print(f"[DMANNOUNCE DEBUG] chunk failed: {e}")

        pool = guild.members if (role is None or role.is_default()) else role.members
        return [m for m in pool if not m.bot]

    async def _update(self, interaction: discord.Interaction, embed: discord.Embed, view=None):
        try:
            await interaction.edit_original_response(embeds=[embed], view=view)
        except discord.HTTPException:
            pass  # interaction token expired or message gone; the final report has a fallback

    async def run_broadcast(self, interaction: discord.Interaction, dm_embed: discord.Embed, recipients: list, title: str):
        guild = interaction.guild
        state = Broadcast(len(recipients))
        self.active[guild.id] = state

        sent, closed, failed = 0, 0, 0
        abort_reason = None

        await self._update(interaction, embeds.info_embed("DM Announce", f"Sending… 0 / {state.total}"))

        try:
            for member in recipients:
                if state.cancelled:
                    break
                try:
                    await member.send(embed=dm_embed)
                    sent += 1
                except discord.Forbidden:
                    closed += 1  # DMs closed / blocked the bot — normal, not an error
                except discord.HTTPException as e:
                    # 40003 = "You are opening direct messages too fast"; 429 = rate limited.
                    if e.code == 40003 or e.status == 429:
                        abort_reason = "Discord told the bot it was opening DMs too fast, so the broadcast was stopped to protect the bot."
                        break
                    failed += 1
                    print(f"[DMANNOUNCE DEBUG] send to {member.id} failed: {e.status} {e.code} {e.text}")

                state.done += 1
                if state.done % PROGRESS_EVERY == 0:
                    await self._update(
                        interaction, embeds.info_embed("DM Announce", f"Sending… {state.done} / {state.total}")
                    )
                await asyncio.sleep(DELAY_SECONDS)
        except Exception as e:
            abort_reason = f"Unexpected error: {type(e).__name__}: {e}"
            print(f"[DMANNOUNCE DEBUG] broadcast crashed: {type(e).__name__}: {e}")
        finally:
            self.active.pop(guild.id, None)

        not_attempted = state.total - state.done
        lines = [
            f"**Delivered:** {sent}",
            f"**Couldn't deliver (DMs closed):** {closed}",
        ]
        if failed:
            lines.append(f"**Failed (other errors):** {failed}")
        if not_attempted > 0:
            lines.append(f"**Not sent:** {not_attempted}")
        if state.cancelled:
            lines.append("\nThe broadcast was cancelled.")
        if abort_reason:
            lines.append(f"\n⚠️ {abort_reason}")

        report = (embeds.warning_embed if (abort_reason or state.cancelled) else embeds.success_embed)(
            "DM Announce — Finished", "\n".join(lines)
        )

        try:
            await interaction.edit_original_response(embeds=[report], view=None)
        except discord.HTTPException:
            # The 15-minute interaction window expired; DM the sender instead.
            try:
                await interaction.user.send(embed=report)
            except discord.HTTPException:
                pass

        try:
            await db.db[LOG_COLLECTION].insert_one({
                "guild_id": guild.id,
                "sender_id": interaction.user.id,
                "title": title,
                "targeted": state.total,
                "delivered": sent,
                "closed": closed,
                "failed": failed,
                "cancelled": state.cancelled,
                "aborted": abort_reason,
                "created_at": discord.utils.utcnow(),
            })
        except Exception as e:
            print(f"[DMANNOUNCE DEBUG] failed to log broadcast: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(DMAnnounce(bot))
