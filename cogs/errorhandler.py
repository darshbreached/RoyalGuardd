"""
cogs/errorhandler.py
---------------------
Global error handler. When anything unexpected crashes, the user gets a short
"Error - Unexpected Error" embed with a 4-character Error Code, and the full
traceback is stored in MongoDB under that code so it can be looked up later.

Covers:
  * slash command errors                (tree.on_error)
  * button / select / modal errors      (discord.ui.View.on_error / Modal.on_error)
  * prefix command errors               (on_command_error)
  * background event errors             (bot.on_error) - logged with a code, no user message

Expected failures (permission checks, cooldowns, bad input) are NOT given codes.
Views that define their own on_error (e.g. registerpanel) keep their own.

/dev error debug [code]   - bot owner only. Shows the stored traceback for a code,
                            or lists the 10 most recent errors when no code is given.

Stored in "error_logs" (kept for 30 days). Tokens, webhook URLs, Mongo URIs and
env-var secrets are replaced with [REDACTED] before anything is saved.

Load this AFTER cogs.devtools in main.py's COGS list (it attaches to /dev).
"""

import asyncio
import collections
import io
import logging
import os
import re
import secrets
import string
import sys
import time
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from pymongo.errors import DuplicateKeyError

from database.mongodb import db
from utils import embeds

log = logging.getLogger("RoyalGuard.errors")

COLLECTION = "error_logs"
RETENTION_DAYS = 30
CODE_LENGTH = 4
MAX_TRACEBACK_CHARS = 20000
DB_TIMEOUT_SECONDS = 5
EVENT_ERROR_COOLDOWN = 60  # store the same event error at most once a minute

_ALPHABET = string.ascii_lowercase + string.digits

# In-memory copy of recent errors, so a code can still be looked up if Mongo is down.
_RECENT = collections.OrderedDict()
_RECENT_MAX = 100
_event_last_seen = {}


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #

_SECRET_HINTS = ("TOKEN", "SECRET", "PASSWORD", "COOKIE", "KEY", "WEBHOOK", "MONGODB_URI", "DSN")
_PATTERNS = [
    (re.compile(r"mongodb(?:\+srv)?://[^\s'\"]+"), "mongodb://[REDACTED]"),
    (re.compile(r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/[^\s'\"]+"), "[REDACTED WEBHOOK]"),
    (re.compile(r"[\w-]{23,28}\.[\w-]{6,7}\.[\w-]{27,}"), "[REDACTED TOKEN]"),
    (re.compile(r"_\|WARNING:-DO-NOT-SHARE-THIS[^\s'\"]+"), "[REDACTED COOKIE]"),
]


def redact(text: str) -> str:
    if not text:
        return text
    values = {
        v for k, v in os.environ.items()
        if len(v) >= 8 and any(hint in k.upper() for hint in _SECRET_HINTS)
    }
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

def _unwrap(error):
    for _ in range(5):
        inner = getattr(error, "original", None)
        if inner is None:
            break
        error = inner
    return error


def _new_code() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


def _remember(code: str, doc: dict):
    _RECENT[code] = doc
    while len(_RECENT) > _RECENT_MAX:
        _RECENT.popitem(last=False)


async def record_error(*, kind: str, source: str, error, bot=None, user=None, guild=None,
                       channel_id=None, extra: dict = None) -> str:
    """Store an error and return its code. Never raises."""
    try:
        err = _unwrap(error)
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        doc = {
            "kind": kind,
            "source": source,
            "error_type": type(err).__name__,
            "error_message": redact(str(err))[:500],
            "traceback": redact(tb)[-MAX_TRACEBACK_CHARS:],
            "user_id": getattr(user, "id", None),
            "user_name": str(user) if user is not None else None,
            "guild_id": getattr(guild, "id", None),
            "guild_name": getattr(guild, "name", None),
            "channel_id": channel_id,
            "bot_id": getattr(getattr(bot, "user", None), "id", None),
            "created_at": datetime.now(timezone.utc),
        }
        if extra:
            doc.update(extra)

        code = None
        for _ in range(8):
            candidate = _new_code()
            if candidate in _RECENT:
                continue
            doc["code"] = candidate
            try:
                await asyncio.wait_for(db.db[COLLECTION].insert_one(dict(doc)), timeout=DB_TIMEOUT_SECONDS)
                code = candidate
                break
            except DuplicateKeyError:
                continue
            except Exception as e:
                print(f"[ERRORHANDLER DEBUG] couldn't save error to Mongo ({type(e).__name__}: {e}); keeping it in memory only")
                code = candidate
                break
        if code is None:
            code = _new_code()
            doc["code"] = code
        _remember(code, doc)
        return code
    except Exception as e:  # last resort: still give the user *a* code
        print(f"[ERRORHANDLER DEBUG] record_error failed: {type(e).__name__}: {e}")
        return _new_code()


async def fetch_error(code: str):
    doc = _RECENT.get(code)
    if doc is not None:
        return doc
    try:
        return await asyncio.wait_for(db.db[COLLECTION].find_one({"code": code}), timeout=DB_TIMEOUT_SECONDS)
    except Exception as e:
        print(f"[ERRORHANDLER DEBUG] fetch_error failed: {type(e).__name__}: {e}")
        return None


async def fetch_recent(limit: int):
    try:
        cursor = db.db[COLLECTION].find({}).sort("created_at", -1).limit(limit)
        return await asyncio.wait_for(cursor.to_list(length=limit), timeout=DB_TIMEOUT_SECONDS)
    except Exception as e:
        print(f"[ERRORHANDLER DEBUG] fetch_recent failed ({type(e).__name__}: {e}); using in-memory copy")
        return list(reversed(_RECENT.values()))[:limit]


# --------------------------------------------------------------------------- #
# User-facing messages
# --------------------------------------------------------------------------- #

def _user_embed(user, code: str) -> discord.Embed:
    embed = embeds.error_embed("Error - Unexpected Error", f"An unexpected error occurred. Error Code: `{code}`")
    if user is not None:
        try:
            embed.set_author(name=user.name, icon_url=user.display_avatar.url)
        except Exception:
            pass
    return embed


async def _send_ephemeral(interaction: discord.Interaction, embed: discord.Embed):
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass  # interaction expired / unknown - nothing more we can do for the user


async def handle_unexpected(interaction: discord.Interaction, error, *, kind: str, source: str, extra: dict = None):
    try:
        err = _unwrap(error)
        code = await record_error(
            kind=kind, source=source, error=error, bot=interaction.client,
            user=interaction.user, guild=interaction.guild, channel_id=interaction.channel_id, extra=extra,
        )
        log.error("Unhandled %s error [%s] in %s", kind, code, source, exc_info=(type(err), err, err.__traceback__))
        await _send_ephemeral(interaction, _user_embed(interaction.user, code))
    except Exception:
        log.exception("The error handler itself failed")


# --------------------------------------------------------------------------- #
# Buttons / selects / modals: patch the base classes once per process
# --------------------------------------------------------------------------- #

_ui_state = {"refs": 0, "view": None, "modal": None}


async def _view_on_error(self, interaction: discord.Interaction, error: Exception, item):
    extra = {"view": type(self).__name__, "item": getattr(item, "custom_id", None) or getattr(item, "label", None)}
    await handle_unexpected(interaction, error, kind="view", source=f"view:{type(self).__name__}", extra=extra)


async def _modal_on_error(self, interaction: discord.Interaction, error: Exception):
    await handle_unexpected(interaction, error, kind="modal", source=f"modal:{type(self).__name__}")


def _install_ui_hooks():
    if _ui_state["refs"] == 0:
        _ui_state["view"] = discord.ui.View.on_error
        _ui_state["modal"] = discord.ui.Modal.on_error
        discord.ui.View.on_error = _view_on_error
        discord.ui.Modal.on_error = _modal_on_error
    _ui_state["refs"] += 1


def _remove_ui_hooks():
    _ui_state["refs"] = max(0, _ui_state["refs"] - 1)
    if _ui_state["refs"] == 0 and _ui_state["view"] is not None:
        discord.ui.View.on_error = _ui_state["view"]
        discord.ui.Modal.on_error = _ui_state["modal"]
        _ui_state["view"] = _ui_state["modal"] = None


# --------------------------------------------------------------------------- #
# /dev error debug
# --------------------------------------------------------------------------- #

async def _owner_check(interaction: discord.Interaction) -> bool:
    # BOT_OWNER_ID (not the Discord app owner) so tenant bots' owners can't read your logs.
    owner_id = os.getenv("BOT_OWNER_ID")
    if not owner_id or str(interaction.user.id) != str(owner_id):
        raise app_commands.CheckFailure("Developer tools are restricted to the bot owner.")
    return True


def _ts(doc: dict):
    dt = doc.get("created_at")
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return None


async def code_autocomplete(interaction: discord.Interaction, current: str):
    try:
        docs = await fetch_recent(25)
    except Exception:
        return []
    choices = []
    for d in docs:
        if current.lower() in d.get("code", ""):
            label = f"{d['code']} · {d.get('source', '?')} · {d.get('error_type', '?')}"[:100]
            choices.append(app_commands.Choice(name=label, value=d["code"]))
    return choices[:25]


@app_commands.check(_owner_check)
@app_commands.describe(code="The error code a user saw, e.g. nup0. Leave empty to list the 10 most recent errors.")
@app_commands.autocomplete(code=code_autocomplete)
async def error_debug(interaction: discord.Interaction, code: str = None):
    await interaction.response.defer(ephemeral=True)

    if not code:
        docs = await fetch_recent(10)
        if not docs:
            return await interaction.followup.send(embed=embeds.info_embed("Recent Errors", "No errors have been logged yet."))
        lines = []
        for d in docs:
            ts = _ts(d)
            when = f"<t:{ts}:R>" if ts else "?"
            lines.append(f"`{d.get('code', '?')}` · {when} · {d.get('source', '?')} · {d.get('error_type', '?')}")
        return await interaction.followup.send(
            embed=embeds.info_embed(f"Recent Errors ({len(docs)})", "\n".join(lines)[:4000])
        )

    code = code.strip().lower()
    doc = await fetch_error(code)
    if not doc:
        return await interaction.followup.send(
            embed=embeds.error_embed("Not Found", f"No error with code `{code[:20]}` was found. Logs are kept for {RETENTION_DAYS} days.")
        )

    tb = doc.get("traceback") or "(no traceback stored)"
    shown = tb.replace("```", "'''")
    attachment = None
    if len(shown) > 3300:
        shown = "…" + shown[-3300:]
        attachment = discord.File(io.BytesIO(tb.encode("utf-8")), filename=f"error_{code}.txt")

    embed = embeds.info_embed(f"Error Debug - {code}", f"```py\n{shown}\n```")
    embed.add_field(name="Error", value=f"`{doc.get('error_type', '?')}`: {doc.get('error_message') or '(no message)'}"[:1000], inline=False)
    embed.add_field(name="Source", value=f"{doc.get('kind', '?')} · {doc.get('source', '?')}"[:1000], inline=False)
    if doc.get("user_id"):
        embed.add_field(name="User", value=f"<@{doc['user_id']}> (`{doc['user_id']}`)", inline=True)
    if doc.get("guild_id"):
        embed.add_field(name="Server", value=f"{doc.get('guild_name') or '?'} (`{doc['guild_id']}`)"[:1000], inline=True)
    if doc.get("channel_id"):
        embed.add_field(name="Channel", value=f"<#{doc['channel_id']}>", inline=True)
    ts = _ts(doc)
    if ts:
        embed.add_field(name="When", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
    if doc.get("bot_id"):
        embed.add_field(name="Bot", value=f"`{doc['bot_id']}`", inline=True)

    kwargs = {"embed": embed}
    if attachment is not None:
        kwargs["file"] = attachment
    await interaction.followup.send(**kwargs)


# --------------------------------------------------------------------------- #
# The cog
# --------------------------------------------------------------------------- #

class ErrorHandler(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._prev_tree_error = None
        self._dev_group = None

    async def cog_load(self):
        try:
            col = db.db[COLLECTION]
            await col.create_index("code", unique=True)
            await col.create_index("created_at", expireAfterSeconds=RETENTION_DAYS * 86400)
        except Exception as e:
            print(f"[ERRORHANDLER DEBUG] couldn't create indexes: {type(e).__name__}: {e}")

        tree = self.bot.tree
        current = tree.__dict__.get("on_error")
        if getattr(current, "__self__", None) is not self:  # never remember our own handler as "previous"
            self._prev_tree_error = current
        tree.on_error = self.on_app_command_error
        self.bot.on_error = self.on_event_error
        _install_ui_hooks()

        dev = tree.get_command("dev")
        if isinstance(dev, app_commands.Group):
            group = app_commands.Group(name="error", description="Error log tools.")
            group.add_command(app_commands.Command(
                name="debug", description="Look up a logged error by its code.", callback=error_debug,
            ))
            dev.add_command(group, override=True)
            self._dev_group = dev
        else:
            log.warning("/dev group not found - load cogs.errorhandler AFTER cogs.devtools to get /dev error debug.")

    async def cog_unload(self):
        tree = self.bot.tree
        if self._prev_tree_error is not None:
            tree.on_error = self._prev_tree_error
        else:
            tree.__dict__.pop("on_error", None)
        self.bot.__dict__.pop("on_error", None)
        _remove_ui_hooks()
        if self._dev_group is not None:
            self._dev_group.remove_command("error")

    # ---- slash commands ---------------------------------------------------- #

    async def _soft(self, interaction: discord.Interaction, title: str, message: str):
        if interaction.response.is_done():
            return
        await _send_ephemeral(interaction, embeds.error_embed(title, message))

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        try:
            if isinstance(error, app_commands.CommandOnCooldown):
                return await self._soft(interaction, "Slow Down", f"Try again in {error.retry_after:.0f}s.")
            if isinstance(error, app_commands.CheckFailure):
                text = str(error)
                if not text or text.startswith("The check functions"):
                    text = "You can't use this command."
                return await self._soft(interaction, "Not Allowed", text)
            if isinstance(error, app_commands.CommandNotFound):
                return await self._soft(interaction, "Unknown Command", "That command doesn't exist anymore. Commands can take a little while to refresh.")

            command = interaction.command
            if command is None:
                source = "unknown"
            elif isinstance(command, app_commands.Command):
                source = f"/{command.qualified_name}"
            else:
                source = command.qualified_name if hasattr(command, "qualified_name") else str(command)
            await handle_unexpected(interaction, error, kind="slash", source=source)
        except Exception:
            log.exception("on_app_command_error failed")

    # ---- prefix commands --------------------------------------------------- #

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        try:
            if ctx.command is not None and ctx.command.has_error_handler():
                return
            if ctx.cog is not None and ctx.cog.has_error_handler():
                return
            quiet = (commands.CommandNotFound, commands.CheckFailure, commands.UserInputError,
                     commands.DisabledCommand, commands.CommandOnCooldown)
            if isinstance(error, quiet):
                return

            err = _unwrap(error)
            name = ctx.command.qualified_name if ctx.command else "?"
            code = await record_error(
                kind="prefix", source=f"{ctx.prefix}{name}", error=error, bot=self.bot,
                user=ctx.author, guild=ctx.guild, channel_id=getattr(ctx.channel, "id", None),
            )
            log.error("Unhandled prefix command error [%s] in %s", code, name, exc_info=(type(err), err, err.__traceback__))
            try:
                await ctx.send(embed=_user_embed(ctx.author, code))
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("on_command_error failed")

    # ---- background events ------------------------------------------------- #

    async def on_event_error(self, event_method: str, *args, **kwargs):
        exc = sys.exc_info()[1]
        if exc is None or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            return
        try:
            key = (event_method, type(exc).__name__)
            now = time.monotonic()
            last = _event_last_seen.get(key)
            if last is not None and now - last < EVENT_ERROR_COOLDOWN:
                log.error("Unhandled error in event %s (repeat, not stored)", event_method, exc_info=(type(exc), exc, exc.__traceback__))
                return
            _event_last_seen[key] = now
            code = await record_error(kind="event", source=event_method, error=exc, bot=self.bot)
            log.error("Unhandled error in event %s [%s]", event_method, code, exc_info=(type(exc), exc, exc.__traceback__))
        except Exception:
            log.exception("on_event_error failed")


async def setup(bot: commands.Bot):
    await bot.add_cog(ErrorHandler(bot))
