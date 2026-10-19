"""
cogs/emailverify.py
--------------------
Email verification panel, so staff can give support by email.

/emailpanel [channel] [log_channel]     (admin level 50)
    Posts the "EMAIL SUPPORT SYSTEM" panel with Verify Email / Remove Email buttons.
    log_channel (keep it private) is where every verified email is logged.

/emaillookup member                      (admin level 50)
    Shows the email a member verified. Each lookup is logged to the log channel.

How a member verifies:
  1. Press Verify Email, type an email address.
  2. The bot emails a 6-digit code (10 minutes, 5 tries). Press Enter Code, type it.
  3. The address is saved for this server and logged to the staff log channel.

Sending uses an HTTPS email API (Brevo), because Railway blocks SMTP on Free/Trial/Hobby
plans. Environment variables on the service that runs the bot:
    BREVO_API_KEY     API key from Brevo (SMTP & API -> API Keys)
    EMAIL_FROM        a sender address you verified in Brevo
    EMAIL_FROM_NAME   optional display name (default: the server's name)
    EMAIL_REPLY_TO    optional address replies go to

Privacy rules (edit the constants to change them):
  * emails are saved per server and only staff with admin level can look them up
  * members can remove their own email with the Remove Email button
  * the email is deleted when the member leaves the server (DELETE_ON_LEAVE)
  * codes are stored hashed, expire, and are limited per member and per address, so the
    bot can't be used to spam someone's inbox
  * the panel tells members how their email will be used
Problems are logged as [EMAILVERIFY DEBUG] lines (never the email address or the code).
"""

import hashlib
import hmac
import html
import os
import re
import secrets
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level

ADMIN_LEVEL = 50
PANEL_TITLE = "EMAIL SUPPORT SYSTEM V1"
CODE_TTL = 600               # seconds a code works
MAX_ATTEMPTS = 5             # wrong guesses before the code is thrown away
SENDS_PER_MEMBER_HOUR = 3
SENDS_PER_EMAIL_HOUR = 3
DELETE_ON_LEAVE = True
TIMEOUT = aiohttp.ClientTimeout(total=15)
BREVO_URL = "https://api.brevo.com/v3/smtp/email"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")

PANEL_TEXT = (
    "Press the **Verify Email** button to link your email address, so staff can give you support by email.\n\n"
    "Staff only use it to contact you about support. You can remove it any time with **Remove Email**, "
    "and it is deleted if you leave the server."
)


def _debug(message: str):
    print(f"[EMAILVERIFY DEBUG] {message}")


def _col(name: str):
    return db.db[name]


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def _hash(code: str, salt: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


def _email_key(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:32]


async def _allow(key: str, limit: int) -> bool:
    """True if `key` has sent fewer than `limit` codes in the last hour (and records this one)."""
    now = time.time()
    doc = await _col("email_throttle").find_one({"_id": key}) or {}
    times = [t for t in doc.get("times", []) if now - t < 3600]
    if len(times) >= limit:
        return False
    times.append(now)
    await _col("email_throttle").update_one({"_id": key}, {"$set": {"times": times}}, upsert=True)
    return True


async def send_code_email(to_email: str, code: str, guild_name: str):
    """(ok, reason). reason is 'not_configured', 'rejected' or 'unreachable' when ok is False."""
    api_key = os.getenv("BREVO_API_KEY")
    sender = os.getenv("EMAIL_FROM")
    if not api_key or not sender:
        return False, "not_configured"

    safe_name = html.escape(guild_name)
    payload = {
        "sender": {"name": os.getenv("EMAIL_FROM_NAME") or guild_name, "email": sender},
        "to": [{"email": to_email}],
        "subject": f"Your {guild_name} email verification code",
        "textContent": (
            f"Your email verification code for {guild_name} is: {code}\n\n"
            f"It expires in {CODE_TTL // 60} minutes. If you didn't ask for this, ignore this email.\n"
        ),
        "htmlContent": (
            f"<p>Your email verification code for <b>{safe_name}</b> is:</p>"
            f"<p style='font-size:28px;letter-spacing:4px'><b>{code}</b></p>"
            f"<p>It expires in {CODE_TTL // 60} minutes. If you didn't ask for this, ignore this email.</p>"
        ),
    }
    reply_to = os.getenv("EMAIL_REPLY_TO")
    if reply_to:
        payload["replyTo"] = {"email": reply_to}

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(BREVO_URL, json=payload, headers={"api-key": api_key, "accept": "application/json"}) as resp:
                body = await resp.text()
                if resp.status in (200, 201, 202):
                    return True, None
                _debug(f"Brevo -> HTTP {resp.status}: {body[:200]}")
                return False, "rejected"
    except (aiohttp.ClientError, TimeoutError) as e:
        _debug(f"Brevo unreachable: {type(e).__name__}")
        return False, "unreachable"


# ---- modals and views ------------------------------------------------------ #

class EmailModal(discord.ui.Modal, title="Verify Email"):
    email = discord.ui.TextInput(label="Email address", placeholder="you@example.com", min_length=5, max_length=254)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.begin_verification(interaction, str(self.email.value))


class CodeModal(discord.ui.Modal, title="Enter Code"):
    code = discord.ui.TextInput(label="6-digit code from the email", placeholder="123456", min_length=6, max_length=6)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.cog.check_code(interaction, str(self.code.value))


class EnterCodeView(discord.ui.View):
    """Shown after a code is sent (or is still waiting)."""

    def __init__(self, cog):
        super().__init__(timeout=CODE_TTL)
        self.cog = cog

    @discord.ui.button(label="Enter Code", style=discord.ButtonStyle.success)
    async def enter_code(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CodeModal(self.cog))

    @discord.ui.button(label="Use a different email", style=discord.ButtonStyle.secondary)
    async def other_email(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmailModal(self.cog))


class ChangeEmailView(discord.ui.View):
    """Shown to someone who already has a verified email."""

    def __init__(self, cog):
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="Change Email", style=discord.ButtonStyle.primary)
    async def change(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(EmailModal(self.cog))

    @discord.ui.button(label="Remove Email", style=discord.ButtonStyle.danger)
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.remove_email(interaction)


class EmailPanelView(discord.ui.View):
    """Persistent panel buttons (registered once in cog_load)."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify Email", style=discord.ButtonStyle.secondary, custom_id="emailverify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start(interaction)

    @discord.ui.button(label="Remove Email", style=discord.ButtonStyle.secondary, custom_id="emailverify:remove")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.remove_email(interaction)


# ---- the cog --------------------------------------------------------------- #

class EmailVerify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(EmailPanelView(self))

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        try:
            cfg = await _col("email_config").find_one({"guild_id": str(guild.id)}) or {}
            channel_id = cfg.get("log_channel_id")
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send(embed=embed)
        except Exception as e:
            _debug(f"log failed: {type(e).__name__}: {e}")

    # ---- Verify Email button ----------------------------------------------- #

    async def start(self, interaction: discord.Interaction):
        gid, uid = str(interaction.guild.id), str(interaction.user.id)

        if not os.getenv("BREVO_API_KEY") or not os.getenv("EMAIL_FROM"):
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Available", "Email verification isn't set up yet. Tell a staff member."),
                ephemeral=True,
            )

        existing = await _col("user_emails").find_one({"guild_id": gid, "discord_id": uid})
        if existing:
            return await interaction.response.send_message(
                embed=embeds.info_embed("Email Verified", f"Your verified email is **{_mask(existing['email'])}**."),
                view=ChangeEmailView(self),
                ephemeral=True,
            )

        pending = await _col("email_pending").find_one({"guild_id": gid, "discord_id": uid})
        if pending and pending.get("expires_at", 0) > time.time():
            return await interaction.response.send_message(
                embed=embeds.info_embed(
                    "Code Waiting",
                    f"A code was already sent to **{_mask(pending['email'])}**. Press Enter Code and type it in. "
                    f"Check your spam folder if you can't find it.",
                ),
                view=EnterCodeView(self),
                ephemeral=True,
            )

        await interaction.response.send_modal(EmailModal(self))

    # ---- sending the code ---------------------------------------------------- #

    async def begin_verification(self, interaction: discord.Interaction, raw_email: str):
        async def fail(title, text):
            await interaction.followup.send(embed=embeds.error_embed(title, text), ephemeral=True)

        try:
            gid, uid = str(interaction.guild.id), str(interaction.user.id)
            email = raw_email.strip().lower()
            if len(email) > 254 or not EMAIL_RE.match(email):
                return await fail("Invalid Email", "That doesn't look like a valid email address.")

            taken = await _col("user_emails").find_one({"guild_id": gid, "email": email, "discord_id": {"$ne": uid}})
            if taken:
                return await fail("Email In Use", "That email is already linked to another member. Contact staff if that's a mistake.")

            if not await _allow(f"u:{gid}:{uid}", SENDS_PER_MEMBER_HOUR) or not await _allow(f"e:{_email_key(email)}", SENDS_PER_EMAIL_HOUR):
                return await fail("Too Many Codes", "Too many codes were requested. Try again in an hour.")

            code = f"{secrets.randbelow(10 ** 6):06d}"
            ok, reason = await send_code_email(email, code, interaction.guild.name)
            if not ok:
                if reason == "not_configured":
                    return await fail("Not Available", "Email verification isn't set up yet. Tell a staff member.")
                if reason == "unreachable":
                    return await fail("Couldn't Send", "I couldn't reach the email service. Try again in a minute.")
                return await fail("Couldn't Send", "The email service refused to send to that address. Check it for typos, or try another one.")

            salt = secrets.token_hex(8)
            await _col("email_pending").update_one(
                {"guild_id": gid, "discord_id": uid},
                {"$set": {
                    "guild_id": gid, "discord_id": uid, "email": email, "salt": salt,
                    "code_hash": _hash(code, salt), "expires_at": time.time() + CODE_TTL, "attempts": 0,
                }},
                upsert=True,
            )
            await interaction.followup.send(
                embed=embeds.info_embed(
                    "Code Sent",
                    f"I sent a 6-digit code to **{_mask(email)}**. It works for {CODE_TTL // 60} minutes.\n"
                    f"Press **Enter Code** and type it in. Check your spam folder if you can't see it.\n\n"
                    f"By verifying, you agree that staff may email you about support. "
                    f"You can remove your email at any time.",
                ),
                view=EnterCodeView(self),
                ephemeral=True,
            )
        except Exception as e:
            _debug(f"begin_verification crashed: {type(e).__name__}: {e}")
            await fail("Something Went Wrong", "Try again in a minute.")

    # ---- checking the code --------------------------------------------------- #

    async def check_code(self, interaction: discord.Interaction, code: str):
        async def fail(title, text):
            await interaction.followup.send(embed=embeds.error_embed(title, text), ephemeral=True)

        try:
            guild, user = interaction.guild, interaction.user
            gid, uid = str(guild.id), str(user.id)
            pending = await _col("email_pending").find_one({"guild_id": gid, "discord_id": uid})
            if not pending or pending.get("expires_at", 0) < time.time():
                if pending:
                    await _col("email_pending").delete_one({"guild_id": gid, "discord_id": uid})
                return await fail("Code Expired", "That code expired. Press **Verify Email** to get a new one.")
            if pending.get("attempts", 0) >= MAX_ATTEMPTS:
                await _col("email_pending").delete_one({"guild_id": gid, "discord_id": uid})
                return await fail("Too Many Tries", "Too many wrong codes. Press **Verify Email** to get a new one.")

            if not hmac.compare_digest(_hash(code.strip(), pending["salt"]), pending["code_hash"]):
                await _col("email_pending").update_one({"guild_id": gid, "discord_id": uid}, {"$inc": {"attempts": 1}})
                left = MAX_ATTEMPTS - pending.get("attempts", 0) - 1
                return await fail("Wrong Code", f"That isn't the right code. {max(left, 0)} tries left.")

            email = pending["email"]
            record = await db.get_verification(user.id)
            doc = {"guild_id": gid, "discord_id": uid, "email": email, "verified_at": int(time.time())}
            if record:
                doc["roblox_id"] = str(record.get("roblox_id"))
                doc["roblox_username"] = record.get("roblox_username")
            await _col("user_emails").update_one({"guild_id": gid, "discord_id": uid}, {"$set": doc}, upsert=True)
            await _col("email_pending").delete_one({"guild_id": gid, "discord_id": uid})

            await interaction.followup.send(
                embed=embeds.success_embed(
                    "Email Verified",
                    f"**{_mask(email)}** is verified. Staff can now contact you by email for support.",
                ),
                ephemeral=True,
            )

            text = f"{user.mention} (`{user.id}`) verified an email.\n**Email:** {email}"
            if record:
                text += f"\n**Roblox:** {record.get('roblox_username')} (`{record.get('roblox_id')}`)"
            await self._log(guild, embeds.info_embed("Email Verified", text))
        except Exception as e:
            _debug(f"check_code crashed: {type(e).__name__}: {e}")
            await fail("Something Went Wrong", "Try again in a minute.")

    # ---- removing --------------------------------------------------------------- #

    async def remove_email(self, interaction: discord.Interaction):
        guild, user = interaction.guild, interaction.user
        gid, uid = str(guild.id), str(user.id)
        existing = await _col("user_emails").find_one({"guild_id": gid, "discord_id": uid})
        await _col("email_pending").delete_one({"guild_id": gid, "discord_id": uid})
        if not existing:
            return await interaction.followup.send(
                embed=embeds.info_embed("Nothing To Remove", "You don't have a verified email."), ephemeral=True
            )
        await _col("user_emails").delete_one({"guild_id": gid, "discord_id": uid})
        await interaction.followup.send(
            embed=embeds.success_embed("Email Removed", "Your email was removed. Staff can no longer see it."), ephemeral=True
        )
        await self._log(guild, embeds.info_embed(
            "Email Removed", f"{user.mention} (`{user.id}`) removed their email ({_mask(existing['email'])})."
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not DELETE_ON_LEAVE:
            return
        try:
            key = {"guild_id": str(member.guild.id), "discord_id": str(member.id)}
            await _col("user_emails").delete_one(key)
            await _col("email_pending").delete_one(key)
        except Exception as e:
            _debug(f"cleanup on leave failed: {type(e).__name__}")

    # ---- staff commands ------------------------------------------------------------ #

    @app_commands.command(name="emailpanel", description="Post the email verification panel.")
    @app_commands.describe(
        channel="Where to post the panel (default: here)",
        log_channel="PRIVATE channel where verified emails are logged",
    )
    @app_commands.guild_only()
    @require_level(ADMIN_LEVEL)
    async def emailpanel(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                         log_channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        if log_channel:
            await _col("email_config").update_one(
                {"guild_id": str(interaction.guild.id)},
                {"$set": {"guild_id": str(interaction.guild.id), "log_channel_id": str(log_channel.id)}},
                upsert=True,
            )
        await target.send(embed=embeds.info_embed(PANEL_TITLE, PANEL_TEXT), view=EmailPanelView(self))

        notes = [f"Panel posted in {target.mention}."]
        cfg = await _col("email_config").find_one({"guild_id": str(interaction.guild.id)}) or {}
        if not cfg.get("log_channel_id"):
            notes.append("No log channel is set, so emails won't be logged. Run this again with `log_channel`.")
        if not os.getenv("BREVO_API_KEY") or not os.getenv("EMAIL_FROM"):
            notes.append("`BREVO_API_KEY` / `EMAIL_FROM` aren't set on this bot, so codes can't be sent yet.")
        await interaction.followup.send(embed=embeds.success_embed("Panel Posted", "\n".join(notes)), ephemeral=True)

    @app_commands.command(name="emaillookup", description="Show the email a member verified.")
    @app_commands.describe(member="The member to look up")
    @app_commands.guild_only()
    @require_level(ADMIN_LEVEL)
    async def emaillookup(self, interaction: discord.Interaction, member: discord.Member):
        doc = await _col("user_emails").find_one({"guild_id": str(interaction.guild.id), "discord_id": str(member.id)})
        if not doc:
            return await interaction.response.send_message(
                embed=embeds.info_embed("No Email", f"{member.mention} hasn't verified an email."), ephemeral=True
            )
        text = f"{member.mention}\n**Email:** {doc['email']}\n**Verified:** <t:{doc['verified_at']}:F>"
        if doc.get("roblox_username"):
            text += f"\n**Roblox:** {doc['roblox_username']} (`{doc.get('roblox_id')}`)"
        await interaction.response.send_message(embed=embeds.info_embed("Member Email", text), ephemeral=True)
        await self._log(interaction.guild, embeds.info_embed(
            "Email Looked Up", f"{interaction.user.mention} looked up the email of {member.mention}."
        ))


async def setup(bot: commands.Bot):
    await bot.add_cog(EmailVerify(bot))
