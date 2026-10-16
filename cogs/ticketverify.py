"""
cogs/ticketverify.py
---------------------
Ticket-style Roblox verification, same flow as the "!verify" systems on other
British Army servers:

  1. Member presses the Verify button (or types !verify).
  2. The bot opens a private channel and asks for their ROBLOX username.
     ("cancel" ends it.)
  3. It shows the account and asks them to reply YES or NO.
  4. On YES it gives a random word code to paste into their Roblox profile's
     About section. They have 2 minutes to do it and type "Done".
  5. The bot reads the public profile, checks the code, links the account with
     db.set_verification(), syncs roles, and closes the channel.

/verifypanel [channel] [category] [staff_role]   (admin level 50)
    Posts the Verify button panel and remembers the ticket category / staff role.

The profile code proves the person owns the Roblox account, so nobody can verify
as someone else. A Roblox account already linked to a DIFFERENT Discord member is
refused (use /forceverify to move it). Already-verified members are told to use
/update. The bot needs Manage Channels, and Message Content intent for replies.
Problems are logged as [TICKETVERIFY DEBUG] lines.
"""

import asyncio
import secrets
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from cogs.update import sync_member_roles

PANEL_LEVEL = 50
PROMPT_TIMEOUT = 120     # seconds to answer the username / YES-NO prompts
CODE_TIMEOUT = 120       # the two minute limit to paste the code and say Done
MAX_ATTEMPTS = 3         # username tries before the ticket closes
CLOSE_DELAY = 10
TITLE = "Verification System"
CANCEL_FOOTER = "Respond with cancel to end this prompt."
BRAND_FOOTER = "Verification System"
LOG_KINDS = ("verification", "verify")  # first /setup log channel kind that exists gets the result
COLOR = discord.Color.orange()
TIMEOUT = aiohttp.ClientTimeout(total=15)

WORDS = [
    "pizza", "army", "robot", "blue", "left", "right", "up", "down", "yes", "cheese",
    "verification", "tank", "apple", "river", "guard", "flag", "medal", "badge", "eagle", "stone",
    "orange", "green", "maple", "storm", "castle", "bridge", "falcon", "anchor", "comet", "harbor",
    "lantern", "meadow", "thunder", "violet", "walnut", "yonder", "zebra", "pepper", "rocket", "sunset",
]


def _debug(message: str):
    print(f"[TICKETVERIFY DEBUG] {message}")


def _cfg():
    return db.db["ticketverify_config"]


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


async def headshot_url(roblox_id: int):
    url = f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={roblox_id}&size=150x150&format=Png&isCircular=false"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = (await resp.json()).get("data") or []
                    if data and data[0].get("imageUrl"):
                        return data[0]["imageUrl"]
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        _debug(f"avatar lookup failed: {type(e).__name__}")
    return None


async def profile_description(roblox_id: int):
    """The public About text, or None if Roblox couldn't be asked."""
    url = f"https://users.roblox.com/v1/users/{roblox_id}"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(url) as resp:
                body = await resp.text()
                if resp.status != 200:
                    _debug(f"GET {url} -> HTTP {resp.status}: {body[:200]}")
                    return None
                return (await resp.json(content_type=None)).get("description") or ""
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        _debug(f"profile lookup failed: {type(e).__name__}")
        return None


def make_embed(description: str, footer: str = BRAND_FOOTER, thumbnail: str = None, fields=None, title: str = TITLE):
    embed = discord.Embed(title=title, description=description, color=COLOR)
    for name, value in fields or []:
        embed.add_field(name=name or "\u200b", value=value, inline=False)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text=footer)
    return embed


class VerifyPanelView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="ticketverify:start")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel, error = await self.cog.open_ticket(interaction.guild, interaction.user, interaction.channel)
        if error:
            return await interaction.followup.send(embed=embeds.error_embed("Can't Verify", error), ephemeral=True)
        await interaction.followup.send(f"Your verification channel: {channel.mention}", ephemeral=True)
        self.cog.start_flow(channel, interaction.user)


class TicketVerify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active = set()
        self._tasks = set()

    async def cog_load(self):
        self.bot.add_view(VerifyPanelView(self))

    # ---- opening a ticket ------------------------------------------------ #

    async def open_ticket(self, guild: discord.Guild, member: discord.Member, source_channel):
        """(channel, None) or (None, error text)."""
        if member.id in self.active:
            return None, "You already have a verification open. Check your channel list."
        self.active.add(member.id)
        try:
            record = await db.get_verification(member.id)
            if record:
                name = record.get("roblox_username") or record.get("roblox_id")
                self.active.discard(member.id)
                return None, f"You're already verified as **{name}**. Use `/update` to refresh your roles. To change accounts, ask staff."

            cfg = await _cfg().find_one({"guild_id": str(guild.id)}) or {}
            category = guild.get_channel(int(cfg["category_id"])) if cfg.get("category_id") else getattr(source_channel, "category", None)
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_channels=True
                ),
            }
            staff_role = guild.get_role(int(cfg["staff_role_id"])) if cfg.get("staff_role_id") else None
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

            slug = "".join(c for c in member.name.lower() if c.isalnum() or c in "-_")[:30] or str(member.id)
            channel = await guild.create_text_channel(
                f"verify-{slug}", category=category, overwrites=overwrites, reason=f"Verification for {member}"
            )
            return channel, None
        except discord.Forbidden:
            self.active.discard(member.id)
            return None, "I don't have permission to create channels here. Tell staff to give me **Manage Channels**."
        except Exception as e:
            _debug(f"open_ticket failed: {type(e).__name__}: {e}")
            self.active.discard(member.id)
            return None, "Something went wrong opening your channel. Try again in a minute."

    def start_flow(self, channel: discord.TextChannel, member: discord.Member):
        task = asyncio.create_task(self._flow(channel, member))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- the conversation ------------------------------------------------ #

    async def _ask(self, channel, member, timeout: float):
        def check(m: discord.Message):
            return m.author.id == member.id and m.channel.id == channel.id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return msg.content.strip()

    async def _end(self, channel, text: str, ok: bool = False, fields=None):
        embed = make_embed(f"{text}\n\nThis channel will close in {CLOSE_DELAY} seconds.", fields=fields)
        embed.color = discord.Color.green() if ok else discord.Color.red()
        await channel.send(embed=embed)

    async def _flow(self, channel: discord.TextChannel, member: discord.Member):
        try:
            await self._run(channel, member)
        except Exception as e:
            _debug(f"flow crashed: {type(e).__name__}: {e}")
            try:
                await self._end(channel, "Something went wrong. Press the Verify button to try again.")
            except Exception:
                pass
        finally:
            self.active.discard(member.id)
            await asyncio.sleep(CLOSE_DELAY)
            try:
                await channel.delete(reason="Verification finished")
            except Exception:
                pass

    async def _run(self, channel: discord.TextChannel, member: discord.Member):
        guild = channel.guild

        # 1) username + YES/NO
        roblox_user = None
        for _ in range(MAX_ATTEMPTS):
            await channel.send(
                content=f"Hello {member.mention}",
                embed=make_embed("What is your ROBLOX username?", footer=CANCEL_FOOTER),
            )
            answer = await self._ask(channel, member, PROMPT_TIMEOUT)
            if answer is None:
                return await self._end(channel, "Timed out. Press the Verify button to start again.")
            if answer.lower() == "cancel":
                return await self._end(channel, "Verification cancelled.")

            try:
                found = await roblox.get_user_by_username(answer)
            except Exception as e:
                _debug(f"username lookup failed: {type(e).__name__}: {e}")
                return await self._end(channel, "Roblox isn't responding right now. Try again in a few minutes.")
            if not found:
                await channel.send(embed=make_embed(f"No ROBLOX user named **{discord.utils.escape_markdown(answer)}** exists.", footer=CANCEL_FOOTER))
                continue

            rid, rname = found.get("id"), found.get("name")
            await channel.send(embed=make_embed(
                "Is this your ROBLOX account?",
                footer=CANCEL_FOOTER,
                thumbnail=await headshot_url(rid),
                fields=[
                    ("Username :", f"[{rname}](https://www.roblox.com/users/{rid}/profile)"),
                    ("ROBLOX Profile :", f"https://www.roblox.com/users/{rid}/profile"),
                    ("", 'Please reply "YES" or "NO"'),
                ],
            ))
            reply = await self._ask(channel, member, PROMPT_TIMEOUT)
            if reply is None:
                return await self._end(channel, "Timed out. Press the Verify button to start again.")
            if reply.lower() == "cancel":
                return await self._end(channel, "Verification cancelled.")
            if reply.lower() in ("yes", "y"):
                roblox_user = found
                break
        if roblox_user is None:
            return await self._end(channel, "Too many attempts. Press the Verify button to start again.")

        rid, rname = roblox_user.get("id"), roblox_user.get("name")

        # 2) refuse an account that already belongs to someone else
        existing = await db.get_verification_by_roblox(rid)
        if existing and str(existing.get("discord_id")) != str(member.id):
            return await self._end(channel, "That ROBLOX account is already linked to another Discord member. Ask staff for help.")

        # 3) code + Done
        code = " ".join(secrets.choice(WORDS) for _ in range(10))
        await channel.send(embed=make_embed(
            "Copy the verification code in the box below and paste it into the **About** section of your ROBLOX profile "
            "to verify your account.",
            fields=[
                ("Verification code", f"```{code}```"),
                ("", f"You have **{CODE_TIMEOUT // 60} minutes**. Type **Done** here when it's saved."),
            ],
            footer=CANCEL_FOOTER,
        ))
        deadline = time.monotonic() + CODE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await self._end(channel, "Time's up. Press the Verify button to start again.")
            answer = await self._ask(channel, member, remaining)
            if answer is None:
                return await self._end(channel, "Time's up. Press the Verify button to start again.")
            low = answer.lower()
            if low == "cancel":
                return await self._end(channel, "Verification cancelled.")
            if low != "done":
                continue
            await channel.send(embed=make_embed("Checking your user profile.."))
            description = await profile_description(rid)
            if description is not None and _norm(code) in _norm(description):
                break
            await channel.send(embed=make_embed(
                "I couldn't find the code on your profile. Make sure it's saved in the **About** section, then type **Done** again.",
                footer=CANCEL_FOOTER,
            ))

        # 4) link + roles
        try:
            await db.set_verification(member.id, rid, rname)
        except Exception as e:
            _debug(f"set_verification failed: {type(e).__name__}: {e}")
            return await self._end(channel, "I verified you but couldn't save it. Tell staff.")

        fields, note = [], ""
        try:
            added, removed, _ = await sync_member_roles(guild, member, rid)
            if added:
                fields.append(("Roles added", ", ".join(added)[:1000]))
            if removed:
                fields.append(("Roles removed", ", ".join(removed)[:1000]))
        except Exception as e:
            _debug(f"role sync failed: {type(e).__name__}: {e}")
            note = "\n\nLinked, but I couldn't sync your roles yet. Run `/update` in a minute."

        await self._end(channel, f"You're verified as **{rname}**.{note}", ok=True, fields=fields)
        await self._log(guild, member, rid, rname)

    async def _log(self, guild, member, rid, rname):
        for kind in LOG_KINDS:
            try:
                channel_id = await db.get_log_channel(guild.id, kind)
                log_channel = guild.get_channel(int(channel_id)) if channel_id else None
                if log_channel:
                    await log_channel.send(embed=make_embed(
                        f"{member.mention} verified as **{rname}** (`{rid}`) through the ticket flow.", title="Verification Complete"
                    ))
                    return
            except Exception:
                continue

    # ---- triggers -------------------------------------------------------- #

    @commands.command(name="verify")
    @commands.guild_only()
    async def verify_prefix(self, ctx: commands.Context):
        channel, error = await self.open_ticket(ctx.guild, ctx.author, ctx.channel)
        if error:
            return await ctx.reply(error, delete_after=15)
        await ctx.reply(f"Verification opened in {channel.mention}", delete_after=15)
        self.start_flow(channel, ctx.author)

    @app_commands.command(name="verifypanel", description="Post the Verify button panel.")
    @app_commands.describe(
        channel="Where to post the panel (default: here)",
        category="Category the private verify channels are created in",
        staff_role="Role that can see every verify channel",
    )
    @app_commands.guild_only()
    @require_level(PANEL_LEVEL)
    async def verifypanel(self, interaction: discord.Interaction, channel: discord.TextChannel = None,
                          category: discord.CategoryChannel = None, staff_role: discord.Role = None):
        await interaction.response.defer(ephemeral=True)
        target = channel or interaction.channel
        await _cfg().update_one(
            {"guild_id": str(interaction.guild.id)},
            {"$set": {
                "guild_id": str(interaction.guild.id),
                "category_id": str(category.id) if category else None,
                "staff_role_id": str(staff_role.id) if staff_role else None,
            }},
            upsert=True,
        )
        panel = make_embed(
            "Press **Verify** below, or type `!verify`, to link your ROBLOX account.\n"
            "A private channel opens and I'll walk you through it.",
            title="Verification",
        )
        await target.send(embed=panel, view=VerifyPanelView(self))
        await interaction.followup.send(embed=embeds.success_embed("Panel Posted", f"Panel posted in {target.mention}."), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketVerify(bot))
