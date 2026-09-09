"""
utils/embeds.py
----------------
Reusable embed factory functions so every part of the bot has a
consistent look and feel (matches config/settings.py branding).

verification_panel_embed() is per-guild branded: each server can set its
own Army Name and Crest Image URL via /setup -> Branding, so one server
can say "Canadian Army" with its own crest while another says something
else entirely. Falls back to settings.VERIFICATION_PANEL_TITLE /
BOT_ICON_URL for any guild that hasn't configured branding yet.

Everything else (footer text/icon, ticket panels, etc.) stays global
platform branding - "Royal Guard Services" / "Darsh Industries" - since
that's product identity, not the roleplay theme.
"""

import discord
from config import settings
from database.mongodb import db


def powered_by_button() -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.link,
        url="https://discord.gg/UZ7raGDKhV",
        label="Powered by Royal Guard Services",
    )


def base_embed(title: str = None, description: str = None, color: int = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color if color is not None else settings.EMBED_COLOR,
    )
    if settings.FOOTER_TEXT:
        embed.set_footer(text=settings.FOOTER_TEXT, icon_url=settings.FOOTER_ICON)
    return embed


def success_embed(title: str, description: str = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=settings.SUCCESS_COLOR)


def error_embed(title: str, description: str = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=settings.ERROR_COLOR)


def warning_embed(title: str, description: str = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=settings.WARNING_COLOR)


def info_embed(title: str, description: str = None) -> discord.Embed:
    return base_embed(title=title, description=description, color=settings.INFO_COLOR)


async def verification_panel_embed(guild_id: int = None) -> discord.Embed:
    army_name = None
    crest_url = settings.BOT_ICON_URL

    if guild_id is not None:
        config = await db.get_guild_config(guild_id)
        army_name = config.get("army_name")
        crest_url = config.get("crest_url") or settings.BOT_ICON_URL

    title = f"{army_name.upper()} VERIFICATION SYSTEM V2" if army_name else settings.VERIFICATION_PANEL_TITLE

    embed = discord.Embed(
        title=title,
        description=settings.VERIFICATION_PANEL_DESCRIPTION,
        color=settings.EMBED_COLOR,
    )
    embed.set_author(name=settings.BOT_NAME, icon_url=settings.AUTHOR_ICON)
    embed.set_thumbnail(url=crest_url)
    if settings.FOOTER_TEXT:
        embed.set_footer(text=settings.FOOTER_TEXT, icon_url=settings.FOOTER_ICON)
    return embed


def ticket_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=settings.TICKET_PANEL_TITLE,
        description=settings.TICKET_PANEL_DESCRIPTION,
        color=settings.EMBED_COLOR,
    )
    embed.set_author(name=settings.BOT_NAME, icon_url=settings.AUTHOR_ICON)
    if settings.FOOTER_TEXT:
        embed.set_footer(text=settings.FOOTER_TEXT, icon_url=settings.FOOTER_ICON)
    return embed
