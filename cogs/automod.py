"""
cogs/automod.py
-----------------
Lightweight automod: spam detection (message rate), mass mentions,
invite link filtering, a basic bad-word filter, and ping protection.
Configurable per guild via /automod config. Staff (admin level 10+) are
exempt from the filters below - EXCEPT ping protection, which deliberately
runs before that staff bypass. Ping protection exists to stop unwanted
pings on one specific protected user regardless of who's pinging them;
only the configured exempt role skips it, not admin level.
"""

import time
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level, has_level

INVITE_REGEX = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\S+", re.IGNORECASE)

# Simple in-memory spam tracker: {(guild_id, user_id): [timestamps]}
_message_windows = {}


class AutoModGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="automod", description="Configure automod.")


class PingProtectGroup(app_commands.Group):
    def __init__(self):
        super().__init__(
            name="pingprotect",
            description="Mute anyone who pings a protected user without an exempt role.",
        )


class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = AutoModGroup()

        self.group.add_command(
            app_commands.Command(name="config", description="Configure automod settings.",
                                  callback=self.automod_config)
        )
        self.group.add_command(
            app_commands.Command(name="enable", description="Enable automod.",
                                  callback=self.automod_enable)
        )
        self.group.add_command(
            app_commands.Command(name="disable", description="Disable automod.",
                                  callback=self.automod_disable)
        )
        self.group.add_command(
            app_commands.Command(name="addword", description="Add a word to the filtered word list.",
                                  callback=self.automod_addword)
        )
        self.group.add_command(
            app_commands.Command(name="removeword", description="Remove a word from the filtered word list.",
                                  callback=self.automod_removeword)
        )

        self.pingprotect_group = PingProtectGroup()
        self.pingprotect_group.add_command(
            app_commands.Command(name="enable", description="Turn on ping protection for a user.",
                                  callback=self.pingprotect_enable)
        )
        self.pingprotect_group.add_command(
            app_commands.Command(name="disable", description="Turn off ping protection.",
                                  callback=self.pingprotect_disable)
        )
        self.pingprotect_group.add_command(
            app_commands.Command(name="status", description="Show the current ping protection settings.",
                                  callback=self.pingprotect_status)
        )
        self.group.add_command(self.pingprotect_group)

        bot.tree.add_command(self.group)

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        config = await db.get_automod_config(guild.id)
        log_channel_id = config.get("log_channel_id")
        if log_channel_id:
            channel = guild.get_channel(int(log_channel_id))
            if channel:
                await channel.send(embed=embed)

    async def _check_pingprotect(self, message: discord.Message, config: dict):
        """Deletes the message and mutes the sender if they pinged the
        protected user without the exempt role. Independent of the general
        automod on/off toggle and the level-10 staff bypass in on_message -
        only the configured exempt role skips this."""
        target_id = config.get("pingprotect_target_id")
        if not target_id or str(message.author.id) == str(target_id):
            return
        if not any(str(m.id) == str(target_id) for m in message.mentions):
            return

        exempt_role_id = config.get("pingprotect_exempt_role_id")
        if exempt_role_id:
            exempt_role = message.guild.get_role(int(exempt_role_id))
            if exempt_role and exempt_role in message.author.roles:
                return

        duration_minutes = config.get("pingprotect_duration_minutes", 60)

        try:
            await message.delete()
        except discord.Forbidden:
            pass

        try:
            await message.author.timeout(
                discord.utils.utcnow() + timedelta(minutes=duration_minutes),
                reason="Automod: ping protection",
            )
            muted = True
        except discord.Forbidden:
            muted = False

        target_mention = f"<@{target_id}>"
        if muted:
            description = (
                f"{message.author.mention} pinged {target_mention} without the exempt role "
                f"and was muted for **{duration_minutes} minutes**."
            )
        else:
            description = (
                f"{message.author.mention} pinged {target_mention} without the exempt role. "
                f"Their message was deleted, but I couldn't mute them - check my role position/permissions."
            )

        embed = embeds.warning_embed("Ping Protection", description)
        await self._log(message.guild, embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        config = await db.get_automod_config(message.guild.id)

        # Ping protection runs regardless of the general automod enabled
        # toggle and BEFORE the staff bypass below - see the module
        # docstring for why.
        if config.get("pingprotect_enabled") and message.mentions:
            await self._check_pingprotect(message, config)

        # Staff (level 10+) bypass everything below this point.
        if await has_level(message.author.id, message.guild, 10):
            return

        if not config.get("enabled", False):
            return

        # ---- Mass mention filter ----
        max_mentions = config.get("max_mentions", 5)
        if len(message.mentions) + len(message.role_mentions) > max_mentions:
            await self._delete_and_warn(message, "Mass mentions")
            return

        # ---- Invite link filter ----
        if config.get("block_invites", True) and INVITE_REGEX.search(message.content):
            await self._delete_and_warn(message, "Posting an invite link")
            return

        # ---- Bad word filter ----
        banned_words = config.get("banned_words", [])
        if banned_words:
            content_lower = message.content.lower()
            for word in banned_words:
                if word.lower() in content_lower:
                    await self._delete_and_warn(message, "Filtered word")
                    return

        # ---- Spam / message rate filter ----
        max_messages = config.get("spam_max_messages", 5)
        spam_window = config.get("spam_window_seconds", 5)

        key = (message.guild.id, message.author.id)
        now = time.time()
        timestamps = _message_windows.get(key, [])
        timestamps = [t for t in timestamps if now - t < spam_window]
        timestamps.append(now)
        _message_windows[key] = timestamps

        if len(timestamps) > max_messages:
            _message_windows[key] = []  # reset so we don't spam-punish repeatedly
            try:
                await message.author.timeout(discord.utils.utcnow() + timedelta(minutes=5),
                                              reason="Automod: message spam")
            except discord.Forbidden:
                pass

            embed = embeds.warning_embed(
                "Automod Action",
                f"{message.author.mention} was muted for 5 minutes for spamming."
            )
            await self._log(message.guild, embed)

    async def _delete_and_warn(self, message: discord.Message, reason: str):
        try:
            await message.delete()
        except discord.Forbidden:
            pass

        embed = embeds.warning_embed(
            "Automod Action",
            f"A message from {message.author.mention} was removed.\n**Reason:** {reason}"
        )
        await self._log(message.guild, embed)

    # ============================================================
    # COMMANDS
    # ============================================================
    @require_level(50)
    @app_commands.describe(
        log_channel="Channel to post automod actions to",
        max_mentions="Max mentions per message before deletion",
        block_invites="Whether to auto-delete Discord invite links",
        spam_max_messages="Messages allowed within the spam window",
        spam_window_seconds="Spam window length in seconds",
    )
    async def automod_config(
        self,
        interaction: discord.Interaction,
        log_channel: discord.TextChannel = None,
        max_mentions: int = None,
        block_invites: bool = None,
        spam_max_messages: int = None,
        spam_window_seconds: int = None,
    ):
        update = {}
        if log_channel:
            update["log_channel_id"] = str(log_channel.id)
        if max_mentions is not None:
            update["max_mentions"] = max_mentions
        if block_invites is not None:
            update["block_invites"] = block_invites
        if spam_max_messages is not None:
            update["spam_max_messages"] = spam_max_messages
        if spam_window_seconds is not None:
            update["spam_window_seconds"] = spam_window_seconds

        if update:
            await db.set_automod_config(interaction.guild.id, **update)

        await interaction.response.send_message(
            embed=embeds.success_embed("Automod Configured", "Settings updated."), ephemeral=True
        )

    @require_level(50)
    async def automod_enable(self, interaction: discord.Interaction):
        await db.set_automod_config(interaction.guild.id, enabled=True)
        await interaction.response.send_message(
            embed=embeds.success_embed("Automod Enabled", "Automod is now active."), ephemeral=True
        )

    @require_level(50)
    async def automod_disable(self, interaction: discord.Interaction):
        await db.set_automod_config(interaction.guild.id, enabled=False)
        await interaction.response.send_message(
            embed=embeds.warning_embed("Automod Disabled", "Automod is now off."), ephemeral=True
        )

    @require_level(50)
    @app_commands.describe(word="The word or phrase to filter")
    async def automod_addword(self, interaction: discord.Interaction, word: str):
        config = await db.get_automod_config(interaction.guild.id)
        words = config.get("banned_words", [])
        if word.lower() not in [w.lower() for w in words]:
            words.append(word)
            await db.set_automod_config(interaction.guild.id, banned_words=words)
        await interaction.response.send_message(
            embed=embeds.success_embed("Word Added", f"`{word}` added to the filter list."), ephemeral=True
        )

    @require_level(50)
    @app_commands.describe(word="The word or phrase to remove from the filter")
    async def automod_removeword(self, interaction: discord.Interaction, word: str):
        config = await db.get_automod_config(interaction.guild.id)
        words = [w for w in config.get("banned_words", []) if w.lower() != word.lower()]
        await db.set_automod_config(interaction.guild.id, banned_words=words)
        await interaction.response.send_message(
            embed=embeds.success_embed("Word Removed", f"`{word}` removed from the filter list."), ephemeral=True
        )

    # ------------------------------------------------------------
    # /automod pingprotect
    # ------------------------------------------------------------
    @require_level(50)
    @app_commands.describe(
        target="The user to protect from unwanted pings",
        exempt_role="Role that's allowed to ping the target freely",
        duration_minutes="How long to mute someone who pings without the exempt role (default 60)",
    )
    async def pingprotect_enable(
        self,
        interaction: discord.Interaction,
        target: discord.Member,
        exempt_role: discord.Role,
        duration_minutes: int = 60,
    ):
        await db.set_automod_config(
            interaction.guild.id,
            pingprotect_enabled=True,
            pingprotect_target_id=str(target.id),
            pingprotect_exempt_role_id=str(exempt_role.id),
            pingprotect_duration_minutes=duration_minutes,
        )
        await interaction.response.send_message(
            embed=embeds.success_embed(
                "Ping Protection Enabled",
                f"Anyone who pings {target.mention} without {exempt_role.mention} will have their "
                f"message deleted and be muted for **{duration_minutes} minutes**."
            ),
            ephemeral=True,
        )

    @require_level(50)
    async def pingprotect_disable(self, interaction: discord.Interaction):
        await db.set_automod_config(interaction.guild.id, pingprotect_enabled=False)
        await interaction.response.send_message(
            embed=embeds.warning_embed("Ping Protection Disabled", "Ping protection is now off."), ephemeral=True
        )

    @require_level(50)
    async def pingprotect_status(self, interaction: discord.Interaction):
        config = await db.get_automod_config(interaction.guild.id)
        if not config.get("pingprotect_enabled"):
            return await interaction.response.send_message(
                embed=embeds.info_embed("Ping Protection", "Ping protection is currently off in this server."),
                ephemeral=True,
            )

        target_id = config.get("pingprotect_target_id")
        exempt_role_id = config.get("pingprotect_exempt_role_id")
        duration = config.get("pingprotect_duration_minutes", 60)
        description = (
            f"Protecting <@{target_id}>.\n"
            f"Exempt role: <@&{exempt_role_id}>.\n"
            f"Mute duration: **{duration} minutes**."
        )
        await interaction.response.send_message(embed=embeds.info_embed("Ping Protection", description), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
