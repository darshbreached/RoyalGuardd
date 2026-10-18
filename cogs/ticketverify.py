"""
cogs/ticketverify.py
---------------------
Ticket-style Roblox verification. There is NO command here: the "Verify via ROBLOX
Ticket" button on the /panel verification panel calls TicketVerify.start_ticket().

Looks like the reference video: a private channel named verify-t-<number>, every bot
message is "Hello @member" plus a blue embed titled "Verification System 4.0".

Flow:
  1. Button press opens the private channel and asks for the ROBLOX username.
     ("cancel" ends it.)
  2. The bot shows the account and asks them to reply YES or NO.
  3. On YES it gives a random word code to paste into their Roblox profile's
     About section. They have 2 minutes to do it and type "Done".
  4. The bot reads the public profile, checks the code, links the account with
     db.set_verification(), syncs roles, and closes the channel.

The profile code proves the person owns the Roblox account, so nobody can verify
as someone else. A Roblox account already linked to a DIFFERENT Discord member is
refused (use /forceverify to move it). Already-verified members are told to use
/update (the panel button shows them the "Is this your ROBLOX account?" Yes/No first;
No comes here with reverify=True and replaces their old link). The channel is created in the same category as the panel's channel, and
only the member, the bot and Administrators can see it. The bot needs Manage
Channels, and Message Content intent for replies.
Footer text/icon on the non-prompt embeds come from embeds.base_embed(), the same
footer the rest of the bot uses. Problems are logged as [TICKETVERIFY DEBUG] lines.
"""

import asyncio
import secrets
import time

import aiohttp
import discord
from discord.ext import commands
from pymongo import ReturnDocument

from database.mongodb import db
from utils import embeds, roblox
from cogs.update import sync_member_roles

PROMPT_TIMEOUT = 120     # seconds to answer the username / YES-NO prompts
CODE_TIMEOUT = 120       # the two minute limit to paste the code and say Done
MAX_ATTEMPTS = 3         # username tries before the ticket closes
CLOSE_DELAY = 10
TITLE = "Verification System 4.0"
CANCEL_FOOTER = "Respond with cancel to end this prompt."
CODE_PLACE = "roblox about"   # where the code goes. The video says "roblox feed", which Roblox no longer has.
CODE_WORDS = 14               # length of the code, same as the video
LOG_KINDS = ("verification", "verify")  # first /setup log channel kind that exists gets the result
COLOR = discord.Color.from_rgb(72, 133, 190)   # the blue bar from the video
TIMEOUT = aiohttp.ClientTimeout(total=15)

# The exact word list the video's codes are made from.
WORDS = ["pizza", "right", "verification", "army", "left", "blue", "up", "robot", "down", "yes", "cheese"]


def _debug(message: str):
    print(f"[TICKETVERIFY DEBUG] {message}")


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _brand():
    """(text, icon_url) of the bot's normal embed footer."""
    try:
        base = embeds.base_embed()
        return base.footer.text, base.footer.icon_url
    except Exception:
        return None, None


async def avatar_url(roblox_id: int):
    url = f"https://thumbnails.roblox.com/v1/users/avatar-bust?userIds={roblox_id}&size=150x150&format=Png&isCircular=false"
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


async def next_ticket_number(guild_id: int):
    try:
        doc = await db.db["ticketverify_counter"].find_one_and_update(
            {"_id": str(guild_id)}, {"$inc": {"n": 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        return int(doc["n"])
    except Exception as e:
        _debug(f"ticket counter failed: {type(e).__name__}: {e}")
        return None


def make_embed(description: str, cancel: bool = False, thumbnail: str = None, title: str = TITLE):
    """Blue embed, bold title, plain description, small-icon footer - like the video."""
    brand_text, icon = _brand()
    embed = discord.Embed(title=title, description=description, color=COLOR)
    embed.set_footer(text=CANCEL_FOOTER if cancel else (brand_text or "Verification System"), icon_url=icon)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


class TicketVerify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active = set()
        self._tasks = set()

    # ---- entry point for the panel button -------------------------------- #

    async def start_ticket(self, interaction: discord.Interaction, reverify: bool = False):
        """Use this as the WHOLE callback of the panel's ticket button.
        reverify=True lets an already-verified member link a different account."""
        await interaction.response.defer(ephemeral=True)
        channel, error = await self.open_ticket(interaction.guild, interaction.user, interaction.channel, reverify)
        if error:
            return await interaction.followup.send(embed=embeds.error_embed("Can't Verify", error), ephemeral=True)
        await interaction.followup.send(f"Your verification channel: {channel.mention}", ephemeral=True)
        self.start_flow(channel, interaction.user)

    # ---- opening a ticket ------------------------------------------------ #

    async def open_ticket(self, guild: discord.Guild, member: discord.Member, source_channel, reverify: bool = False):
        """(channel, None) or (None, error text)."""
        if member.id in self.active:
            return None, "You already have a verification open. Check your channel list."
        self.active.add(member.id)
        try:
            record = None if reverify else await db.get_verification(member.id)
            if record:
                name = record.get("roblox_username") or record.get("roblox_id")
                self.active.discard(member.id)
                return None, f"You're already verified as **{name}**. Use `/update` to refresh your roles. To change accounts, ask staff."

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True, read_message_history=True, manage_channels=True
                ),
            }
            number = await next_ticket_number(guild.id)
            if number is not None:
                name = f"verify-t-{number}"
            else:
                name = "verify-" + ("".join(c for c in member.name.lower() if c.isalnum() or c in "-_")[:30] or str(member.id))
            channel = await guild.create_text_channel(
                name,
                category=getattr(source_channel, "category", None),
                overwrites=overwrites,
                reason=f"Verification for {member}",
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

    async def _say(self, channel, member, embed: discord.Embed):
        """Every bot message is 'Hello @member' plus an embed, like the video."""
        await channel.send(content=f"Hello {member.mention}", embed=embed)

    async def _ask(self, channel, member, timeout: float):
        def check(m: discord.Message):
            return m.author.id == member.id and m.channel.id == channel.id

        try:
            msg = await self.bot.wait_for("message", check=check, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return msg.content.strip()

    async def _end(self, channel, member, text: str):
        await self._say(channel, member, make_embed(f"{text}\n\nThis channel will close in {CLOSE_DELAY} seconds."))

    async def _flow(self, channel: discord.TextChannel, member: discord.Member):
        try:
            await self._run(channel, member)
        except Exception as e:
            _debug(f"flow crashed: {type(e).__name__}: {e}")
            try:
                await self._end(channel, member, "Something went wrong. Press the Verify button to try again.")
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
            await self._say(channel, member, make_embed("What is your ROBLOX username?", cancel=True))
            answer = await self._ask(channel, member, PROMPT_TIMEOUT)
            if answer is None:
                return await self._end(channel, member, "Timed out. Press the Verify button to start again.")
            if answer.lower() == "cancel":
                return await self._end(channel, member, "Verification cancelled.")

            try:
                found = await roblox.get_user_by_username(answer)
            except Exception as e:
                _debug(f"username lookup failed: {type(e).__name__}: {e}")
                return await self._end(channel, member, "Roblox isn't responding right now. Try again in a few minutes.")
            if not found:
                await self._say(channel, member, make_embed(
                    f"No ROBLOX user named {discord.utils.escape_markdown(answer)} exists.", cancel=True
                ))
                continue

            rid, rname = found.get("id"), found.get("name")
            profile = f"https://www.roblox.com/users/{rid}/profile"
            await self._say(channel, member, make_embed(
                "Is this your ROBLOX account?\n\n"
                f"Username : [{rname}]({profile})\n"
                f"ROBLOX Profile : {profile}\n\n"
                'Please reply "**YES**" or "**NO**"',
                cancel=True,
                thumbnail=await avatar_url(rid),
            ))
            reply = await self._ask(channel, member, PROMPT_TIMEOUT)
            if reply is None:
                return await self._end(channel, member, "Timed out. Press the Verify button to start again.")
            if reply.lower() == "cancel":
                return await self._end(channel, member, "Verification cancelled.")
            if reply.lower() in ("yes", "y"):
                roblox_user = found
                break
        if roblox_user is None:
            return await self._end(channel, member, "Too many attempts. Press the Verify button to start again.")

        rid, rname = roblox_user.get("id"), roblox_user.get("name")

        # 2) refuse an account that already belongs to someone else
        existing = await db.get_verification_by_roblox(rid)
        if existing and str(existing.get("discord_id")) != str(member.id):
            return await self._end(channel, member, "That ROBLOX account is already linked to another Discord member. Ask staff for help.")

        # 3) code + Done
        code = " ".join(secrets.choice(WORDS) for _ in range(CODE_WORDS))
        await self._say(channel, member, make_embed(
            f"Copy the verification code in the box below and paste it in your `{CODE_PLACE}` to verify your account.\n\n"
            f"```{code}```\n"
            f"Reply **Done** once it's saved. You have {CODE_TIMEOUT // 60} minutes.",
            cancel=True,
        ))
        deadline = time.monotonic() + CODE_TIMEOUT
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return await self._end(channel, member, "Time's up. Press the Verify button to start again.")
            answer = await self._ask(channel, member, remaining)
            if answer is None:
                return await self._end(channel, member, "Time's up. Press the Verify button to start again.")
            low = answer.lower()
            if low == "cancel":
                return await self._end(channel, member, "Verification cancelled.")
            if low != "done":
                continue
            await self._say(channel, member, make_embed("Checking your user profile.."))
            description = await profile_description(rid)
            if description is not None and _norm(code) in _norm(description):
                break
            await self._say(channel, member, make_embed(
                f"I couldn't find the code on your profile. Make sure it's saved in your `{CODE_PLACE}`, then reply **Done** again.",
                cancel=True,
            ))

        # 4) link + roles
        try:
            await db.set_verification(member.id, rid, rname)
        except Exception as e:
            _debug(f"set_verification failed: {type(e).__name__}: {e}")
            return await self._end(channel, member, "I verified you but couldn't save it. Tell staff.")

        text = f"You're verified as **{rname}**."
        try:
            added, removed, _ = await sync_member_roles(guild, member, rid)
            if added:
                text += f"\n**Added:** {', '.join(added)}"
            if removed:
                text += f"\n**Removed:** {', '.join(removed)}"
        except Exception as e:
            _debug(f"role sync failed: {type(e).__name__}: {e}")
            text += "\n\nLinked, but I couldn't sync your roles yet. Run `/update` in a minute."

        await self._end(channel, member, text)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketVerify(bot))
