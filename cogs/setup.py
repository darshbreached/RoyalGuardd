"""cogs/setup.py — /setup wizard: cascading dropdown configuration UI.

Category -> Option -> (channel picker / role picker / modal), saved per-guild
into guild_config via database/mongodb.py's existing get_guild_config/set_guild_config.

IMPORTANT — scope of this cog: it only WRITES configuration into guild_config.
Other cogs (setrank.py, bgcheck.py, verification.py, tickets.py, etc.) do NOT
automatically read these values yet — most currently rely on env vars or
hardcoded IDs. Wiring each cog to prefer its guild_config value over its
current behavior is a separate follow-up task per cog, not included here.
The one exception worth prioritizing: setrank.py's ROBLOX_SECURITY_COOKIE
env var vs. this cog's "ranking.roblox_cookie" guild_config key — these are
NOT the same thing yet until setrank.py is updated to check guild_config first.

Selecting the Verification Logs Channel is special-cased: it creates (or
replaces) a "Darsh Industries" webhook in that channel, using the company
logo as its avatar, and stores the webhook URL so
website/routes/oauth.py can post verification logs (IP, location, etc.)
there per-guild instead of to one shared global webhook.
"""

import os
import discord
import aiohttp
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level

WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://your-railway-app.up.railway.app")
LOGO_URL = f"{WEBSITE_BASE_URL}/static/images/logo-mark.png"


CATEGORIES = {
    "channels": {
        "label": "Channels",
        "description": "Configure channel settings",
        "options": {
            "mod_logs_channel_id": {"label": "Moderation Logs Channel", "description": "Set the channel for moderation logs", "type": "channel"},
            "bot_logs_channel_id": {"label": "Bot Logs Channel", "description": "Set the channel for bot logs", "type": "channel"},
            "verification_logs_channel_id": {"label": "Verification Logs Channel", "description": "Set the channel for verification logs", "type": "channel"},
            "ssu_channel_id": {"label": "SSU Channel", "description": "Server Startup channel", "type": "channel"},
            "bmt_logs_channel_id": {"label": "BMT Logs Channel", "description": "Channel for BMT before/after training logs", "type": "channel"},
        },
    },
    "roles": {
        "label": "Roles",
        "description": "Configure role settings",
        "options": {
            "extra_roles": {"label": "Extra Roles", "description": "Comma-separated role names, auto-added to every rankbind", "type": "list"},
            "verified_roles": {"label": "Verified Roles", "description": "Comma-separated role names, auto-added to every rankbind", "type": "list"},
            "nitro_booster_role_id": {"label": "Nitro Booster Role", "description": "Nitro booster role", "type": "role"},
            "flex_role_id": {"label": "Flex Role", "description": "Flex role for nitro boosters", "type": "role"},
            "non_verified_role_id": {"label": "Non-Verified Role", "description": "Role for non-verified users", "type": "role"},
            "non_ba_role_id": {"label": "Non-BA Role", "description": "Role for users not in BA group", "type": "role"},
            "ranks_role_id": {"label": "Ranks Role", "description": "Role for ranked users", "type": "role"},
            "awards_role_id": {"label": "Awards Role", "description": "Role for awards", "type": "role"},
            "timezone_role_id": {"label": "Timezone Role", "description": "Role for timezone", "type": "role"},
            "level_role_id": {"label": "Level Role", "description": "Role for levels", "type": "role"},
            "colour_roles": {"label": "Colour Roles", "description": "Comma-separated role names", "type": "list"},
        },
    },
    "verification": {
        "label": "Verification",
        "description": "Configure verification settings",
        "options": {
            "main_group_id": {"label": "Main Group ID", "description": "Main Roblox group ID for BA rank", "type": "number"},
        },
    },
    "background_check": {
        "label": "Background Check",
        "description": "Configure background check settings",
        "options": {
            "blacklisted_groups": {"label": "Blacklisted Groups", "description": "Comma-separated group IDs", "type": "list"},
            "whitelisted_groups": {"label": "Whitelisted Groups", "description": "Comma-separated group IDs", "type": "list"},
            "blacklisted_names": {"label": "Blacklisted Names", "description": "Comma-separated group names", "type": "list"},
            "regiment_groups": {"label": "Regiment Groups", "description": "Comma-separated group IDs", "type": "list"},
        },
    },
    "server_startup": {
        "label": "Server Startup",
        "description": "Configure SSU settings",
        "options": {
            "ssu_ping_role_id": {"label": "SSU Ping Role", "description": "Role to ping for server startups", "type": "role"},
            "ssu_min_attendees": {"label": "Minimum Attendees", "description": "Minimum users required to start", "type": "number"},
        },
    },
    "ranking": {
        "label": "Ranking",
        "description": "Configure ranking settings",
        "options": {
            "roblox_cookie": {"label": "ROBLOX Cookie", "description": "Roblox .ROBLOSECURITY cookie for ranking", "type": "secret"},
            "ranking_logs_channel_id": {"label": "Ranking Logs Channel", "description": "Channel for ranking logs", "type": "channel"},
            "setrank_max_rank_id": {"label": "SetRank Max Rank ID", "description": "Maximum rank ID users can be ranked to", "type": "number"},
            "setrank_main_group": {"label": "SetRank Main Group", "description": "The main group the max rank limit applies to", "type": "number"},
            "bmt_graduate_rank_id": {"label": "BMT Graduate Rank", "description": "Rank ID BMT graduates are promoted to", "type": "number"},
        },
    },
}


def _find_option(option_key: str):
    for cat_key, cat in CATEGORIES.items():
        if option_key in cat["options"]:
            return cat_key, cat["options"][option_key]
    return None, None


def _category_embed(category_key: str) -> discord.Embed:
    cat = CATEGORIES[category_key]
    embed = embeds.base_embed()
    embed.title = f"Setup - {cat['label']}"
    embed.description = f"Select an option to configure from the {cat['label'].lower()} category."
    return embed


def _root_embed() -> discord.Embed:
    embed = embeds.base_embed()
    embed.title = "Setup"
    embed.description = "Select a category to configure."
    return embed


async def _create_verification_webhook(channel: discord.TextChannel) -> str:
    """Creates (or replaces) a 'Darsh Industries' webhook in the given
    channel, using the company logo as its avatar, and returns its URL.
    Re-running /setup on this channel replaces the old webhook instead of
    piling up duplicates."""
    avatar_bytes = None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(LOGO_URL) as resp:
                if resp.status == 200:
                    avatar_bytes = await resp.read()
    except Exception:
        pass  # fine to create the webhook without an avatar if the logo fetch fails

    existing = await channel.webhooks()
    for wh in existing:
        if wh.name == "Darsh Industries":
            try:
                await wh.delete(reason="Replacing with a fresh verification logging webhook")
            except Exception:
                pass

    webhook = await channel.create_webhook(name="Darsh Industries", avatar=avatar_bytes)
    return webhook.url


class BackButton(discord.ui.Button):
    """Goes back to the category list (to_root=True) or the option list within a category."""

    def __init__(self, category_key: str, to_root: bool = False):
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, emoji="◀️")
        self.category_key = category_key
        self.to_root = to_root

    async def callback(self, interaction: discord.Interaction):
        if self.to_root:
            return await interaction.response.edit_message(embed=_root_embed(), view=CategorySelectView())
        await interaction.response.edit_message(embed=_category_embed(self.category_key), view=OptionSelectView(self.category_key))


class ResultView(discord.ui.View):
    """Shown after a setting is saved — Back button returns to that category's option list."""

    def __init__(self, category_key: str):
        super().__init__(timeout=180)
        self.add_item(BackButton(category_key))


class ChannelPicker(discord.ui.ChannelSelect):
    def __init__(self, option_key: str, label: str):
        super().__init__(placeholder=f"Select {label}...", channel_types=[discord.ChannelType.text], min_values=1, max_values=1)
        self.option_key = option_key

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await db.set_guild_config(interaction.guild.id, **{self.option_key: str(channel.id)})
        cat_key, meta = _find_option(self.option_key)

        extra_note = ""
        if self.option_key == "verification_logs_channel_id":
            await interaction.response.defer()
            try:
                webhook_url = await _create_verification_webhook(channel)
                await db.set_guild_config(interaction.guild.id, verification_webhook_url=webhook_url)
                extra_note = "\n\nA **Darsh Industries** webhook was created in that channel — verification logs (IP, location, ISP, etc.) will post there automatically from now on."
            except Exception as e:
                extra_note = f"\n\n⚠️ Channel saved, but creating the logging webhook failed: {e}"

            embed = embeds.success_embed("Setting Saved", f"**{meta['label']}** set to {channel.mention}.{extra_note}")
            return await interaction.edit_original_response(embed=embed, view=ResultView(cat_key))

        embed = embeds.success_embed("Setting Saved", f"**{meta['label']}** set to {channel.mention}.")
        await interaction.response.edit_message(embed=embed, view=ResultView(cat_key))


class RolePicker(discord.ui.RoleSelect):
    def __init__(self, option_key: str, label: str):
        super().__init__(placeholder=f"Select {label}...", min_values=1, max_values=1)
        self.option_key = option_key

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        await db.set_guild_config(interaction.guild.id, **{self.option_key: str(role.id)})
        cat_key, meta = _find_option(self.option_key)
        embed = embeds.success_embed("Setting Saved", f"**{meta['label']}** set to {role.mention}.")
        await interaction.response.edit_message(embed=embed, view=ResultView(cat_key))


class PickerView(discord.ui.View):
    def __init__(self, option_key: str, meta: dict, picker_type: str):
        super().__init__(timeout=180)
        if picker_type == "channel":
            self.add_item(ChannelPicker(option_key, meta["label"]))
        else:
            self.add_item(RolePicker(option_key, meta["label"]))
        cat_key, _ = _find_option(option_key)
        self.add_item(BackButton(cat_key))


class SettingModal(discord.ui.Modal):
    def __init__(self, option_key: str, meta: dict):
        super().__init__(title=meta["label"][:45])
        self.option_key = option_key
        self.meta = meta
        style = discord.TextStyle.paragraph if meta["type"] in ("list", "secret") else discord.TextStyle.short
        if meta["type"] == "list":
            max_len = 400
        elif meta["type"] == "secret":
            max_len = 2000  # Roblox .ROBLOSECURITY cookies typically run 800+ chars - 200 was truncating them silently
        else:
            max_len = 200
        self.value_input = discord.ui.TextInput(
            label=meta["label"][:45],
            style=style,
            placeholder=meta["description"][:100],
            required=True,
            max_length=max_len,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.value_input.value).strip()

        if self.meta["type"] == "number" and not raw.isdigit():
            return await interaction.response.send_message(
                embed=embeds.error_embed("Invalid Value", f"**{self.meta['label']}** must be a number."),
                ephemeral=True,
            )

        await db.set_guild_config(interaction.guild.id, **{self.option_key: raw})

        display_value = f"||`{raw}`||" if self.meta["type"] == "secret" else f"`{raw}`"
        cat_key, _ = _find_option(self.option_key)
        embed = embeds.success_embed("Setting Saved", f"**{self.meta['label']}** set to {display_value}.")
        await interaction.response.edit_message(embed=embed, view=ResultView(cat_key))


class OptionSelect(discord.ui.Select):
    def __init__(self, category_key: str):
        self.category_key = category_key
        cat = CATEGORIES[category_key]
        options = [
            discord.SelectOption(label=meta["label"], description=meta["description"][:100], value=key)
            for key, meta in cat["options"].items()
        ][:25]
        super().__init__(placeholder="Select Setup Option", options=options)

    async def callback(self, interaction: discord.Interaction):
        option_key = self.values[0]
        cat_key, meta = _find_option(option_key)

        if meta["type"] == "channel":
            embed = _category_embed(cat_key)
            embed.description = f"Select a channel for **{meta['label']}**."
            return await interaction.response.edit_message(embed=embed, view=PickerView(option_key, meta, "channel"))

        if meta["type"] == "role":
            embed = _category_embed(cat_key)
            embed.description = f"Select a role for **{meta['label']}**."
            return await interaction.response.edit_message(embed=embed, view=PickerView(option_key, meta, "role"))

        await interaction.response.send_modal(SettingModal(option_key, meta))


class OptionSelectView(discord.ui.View):
    def __init__(self, category_key: str):
        super().__init__(timeout=180)
        self.add_item(OptionSelect(category_key))
        self.add_item(BackButton(category_key, to_root=True))


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=cat["label"], description=cat["description"], value=key)
            for key, cat in CATEGORIES.items()
        ]
        super().__init__(placeholder="Select Setup Category", options=options)

    async def callback(self, interaction: discord.Interaction):
        category_key = self.values[0]
        await interaction.response.edit_message(embed=_category_embed(category_key), view=OptionSelectView(category_key))


class CategorySelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(CategorySelect())


class Setup(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure server settings through an interactive menu.")
    @require_level(10)
    async def setup_command(self, interaction: discord.Interaction):
        embed = _root_embed()
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed, view=CategorySelectView(), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Setup(bot))
