"""
cogs/redeem.py
---------------
Let people who bought your gamepass rank themselves, automatically.

/redeem [product]
    Anyone. Uses the Roblox account the person VERIFIED with this bot (never a typed
    username, so nobody can claim with someone else's account). Checks that account
    owns the gamepass, then ranks it into the configured group role.

/redeemadmin add name gamepass_id group_id role
/redeemadmin remove name
/redeemadmin list
    Admin level REDEEM_ADMIN_LEVEL. Links a gamepass to a role in a group that is
    already bound to this server with /groupbind.

Safety rules (edit the constants below to change them):
  * only ever ranks UP - someone already at or above the role is left alone
  * Guest and Owner roles can't be configured
  * 20s cooldown per person so Roblox isn't hammered
  * uses the same per-server cookie / fallback cookie as /setrank

Ownership check: Roblox's public inventory endpoint first (no key needed). If that
fails and ROBLOX_INVENTORY_API_KEY is set, the Open Cloud inventory API is tried.
Both are logged as [REDEEM DEBUG] lines so a wrong response shape shows up in Railway.
"""

import json
import os
import time
from datetime import datetime, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level
from cogs.update import sync_member_roles

REDEEM_ADMIN_LEVEL = 50
COOLDOWN_SECONDS = 20
LOG_CHANNEL_KIND = "rank"  # same log channel /acceptrequest posts to
TIMEOUT = aiohttp.ClientTimeout(total=15)

# database/mongodb.py method names that might return a person's verification record.
VERIFY_LOOKUPS = ("get_verification", "get_verification_by_discord", "get_verified_user", "get_user_verification")
ROBLOX_ID_KEYS = ("roblox_id", "roblox_user_id", "robloxId")


def _debug(message: str):
    print(f"[REDEEM DEBUG] {message}")


def _cfg():
    return db.db["redeem_config"]


def _claims():
    return db.db["redeem_claims"]


async def linked_roblox_id(discord_id: int):
    """(roblox_id or None, lookup_method_found). Only the verified link is trusted."""
    found_method = False
    for name in VERIFY_LOOKUPS:
        fn = getattr(db, name, None)
        if fn is None:
            continue
        found_method = True
        for candidate in (discord_id, str(discord_id)):
            try:
                record = await fn(candidate)
            except TypeError:
                break  # wrong signature, try the next method name
            except Exception as e:
                _debug(f"{name} failed: {type(e).__name__}: {e}")
                continue
            if record:
                for key in ROBLOX_ID_KEYS:
                    if record.get(key):
                        return int(record[key]), True
    if not found_method:
        _debug("no verification lookup matched. Methods with 'verif' in the name: "
               + ", ".join(n for n in dir(db) if "verif" in n.lower()))
    return None, found_method


async def owns_gamepass(roblox_id: int, gamepass_id: int):
    """True / False, or None if Roblox couldn't be asked."""
    url = f"https://inventory.roblox.com/v1/users/{roblox_id}/items/GamePass/{gamepass_id}"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.get(url) as resp:
                body = await resp.text()
                _debug(f"GET {url} -> HTTP {resp.status}: {body[:200]}")
                if resp.status == 200:
                    data = json.loads(body).get("data")
                    if isinstance(data, list):
                        return len(data) > 0
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        _debug(f"public inventory check failed: {type(e).__name__}")

    key = os.getenv("ROBLOX_INVENTORY_API_KEY")
    if not key:
        return None
    url = f"https://apis.roblox.com/cloud/v2/users/{roblox_id}/inventory-items"
    try:
        async with aiohttp.ClientSession(headers={"x-api-key": key}, timeout=TIMEOUT) as session:
            async with session.get(url, params={"filter": f"gamePassIds={gamepass_id}", "maxPageSize": 10}) as resp:
                body = await resp.text()
                _debug(f"GET {url} gamePassIds={gamepass_id} -> HTTP {resp.status}: {body[:200]}")
                if resp.status == 200:
                    return len(json.loads(body).get("inventoryItems") or []) > 0
    except (aiohttp.ClientError, TimeoutError, ValueError) as e:
        _debug(f"open cloud inventory check failed: {type(e).__name__}")
    return None


class Redeem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._last = {}

    redeemadmin = app_commands.Group(name="redeemadmin", description="Set up gamepass redemption.", guild_only=True)

    # ---- helpers --------------------------------------------------------- #

    async def _products(self, guild_id: int):
        return await _cfg().find({"guild_id": str(guild_id)}).to_list(length=50)

    async def _group_choices(self, interaction: discord.Interaction, current: str):
        try:
            binds = await db.list_groupbinds(interaction.guild.id)
        except Exception:
            return []
        cur = (current or "").lower()
        out = []
        for b in binds:
            name = b.get("group_name") or "Group"
            if cur in name.lower() or cur in str(b["group_id"]):
                out.append(app_commands.Choice(name=f"{name} ({b['group_id']})"[:100], value=int(b["group_id"])))
        return out[:25]

    async def _role_choices(self, interaction: discord.Interaction, current: str):
        try:
            roles = await roblox.get_group_roles(int(interaction.namespace.group_id))
        except Exception:
            return []
        cur = (current or "").lower()
        out = []
        for r in roles or []:
            if r.get("rank") in (0, 255) or cur not in r.get("name", "").lower():
                continue
            out.append(app_commands.Choice(name=f"{r['name']} (Rank {r['rank']})"[:100], value=int(r["id"])))
        return out[:25]

    async def _product_choices(self, interaction: discord.Interaction, current: str):
        try:
            products = await self._products(interaction.guild.id)
        except Exception:
            return []
        cur = (current or "").lower()
        return [app_commands.Choice(name=p["name"][:100], value=p["name"][:100])
                for p in products if cur in p["name"].lower()][:25]

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        try:
            channel_id = await db.get_log_channel(guild.id, LOG_CHANNEL_KIND)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send(embed=embed)
        except Exception:
            pass

    # ---- /redeem --------------------------------------------------------- #

    @app_commands.command(name="redeem", description="Redeem your gamepass purchase for a group rank.")
    @app_commands.describe(product="Which gamepass to redeem (leave empty if there is only one)")
    @app_commands.guild_only()
    async def redeem(self, interaction: discord.Interaction, product: str = None):
        now = time.monotonic()
        if now - self._last.get(interaction.user.id, 0) < COOLDOWN_SECONDS:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Slow Down", f"Try again in {COOLDOWN_SECONDS} seconds."), ephemeral=True
            )
        self._last[interaction.user.id] = now
        await interaction.response.defer(ephemeral=True)

        async def fail(title, text):
            await interaction.followup.send(embed=embeds.error_embed(title, text), ephemeral=True)

        guild = interaction.guild
        products = await self._products(guild.id)
        if not products:
            return await fail("Not Set Up", "Gamepass redemption isn't set up in this server yet.")
        if product:
            cfg = next((p for p in products if p["name"].lower() == product.strip().lower()), None)
            if cfg is None:
                return await fail("Unknown Product", "Pick one from the list: " + ", ".join(f"`{p['name']}`" for p in products))
        elif len(products) == 1:
            cfg = products[0]
        else:
            return await fail("Pick One", "Which one? " + ", ".join(f"`{p['name']}`" for p in products))

        roblox_id, has_lookup = await linked_roblox_id(interaction.user.id)
        if not has_lookup:
            return await fail("Setup Problem", "I couldn't find the verification lookup. The bot owner should check the Railway logs for `[REDEEM DEBUG]`.")
        if not roblox_id:
            return await fail("Verify First", "You need to verify your Roblox account with this server's verification first, then run this again.")

        group_id, gamepass_id = int(cfg["group_id"]), int(cfg["gamepass_id"])
        try:
            roles = await roblox.get_group_roles(group_id)
        except Exception:
            roles = None
        role = next((r for r in roles or [] if str(r.get("id")) == str(cfg["role_id"])), None)
        if role is None:
            return await fail("Setup Problem", "The role this gamepass gives no longer exists. Ask an admin to set it up again.")

        owns = await owns_gamepass(roblox_id, gamepass_id)
        if owns is None:
            return await fail("Couldn't Check", "I couldn't check your inventory with Roblox right now. Try again in a minute.")
        if not owns:
            return await fail(
                "Gamepass Not Found",
                f"Your linked Roblox account (ID `{roblox_id}`) doesn't own **{cfg['name']}**. "
                f"If you bought it on a different account, verify that account instead. If you just bought it, wait a minute and try again.",
            )

        current = None
        try:
            current = await roblox.get_user_rank_in_group(roblox_id, group_id)
        except Exception as e:
            _debug(f"rank lookup failed: {type(e).__name__}: {e}")
        if current is None:
            return await fail("Couldn't Check Rank", "I couldn't read your rank in the group. Try again in a minute.")

        if current[0] == 0:  # not a member: accept a pending join request if there is one
            try:
                await roblox.accept_group_join_request(group_id, roblox_id, guild_id=guild.id)
                current = await roblox.get_user_rank_in_group(roblox_id, group_id)
            except Exception as e:
                _debug(f"join request step failed: {type(e).__name__}: {e}")
            if current[0] == 0:
                return await fail("Join The Group", f"You're not in **{cfg.get('group_name') or group_id}** yet. Join it on Roblox, then run this again.")

        if current[0] >= role["rank"]:
            return await interaction.followup.send(
                embed=embeds.info_embed("Nothing To Do", f"You're already **{current[1]}**, which is at or above **{role['name']}**."),
                ephemeral=True,
            )

        try:
            ok = await roblox.set_group_rank(group_id, roblox_id, role["id"], guild_id=guild.id)
        except RuntimeError as e:
            return await fail("Rank Change Failed", str(e))
        if not ok:
            return await fail("Rank Change Failed", "Roblox didn't accept the rank change. Tell a staff member.")

        try:
            await sync_member_roles(guild, interaction.user, roblox_id)
        except Exception as e:
            _debug(f"discord role sync failed: {type(e).__name__}: {e}")

        try:
            await _claims().update_one(
                {"guild_id": str(guild.id), "discord_id": str(interaction.user.id), "name": cfg["name"]},
                {"$set": {"roblox_id": str(roblox_id), "role_id": str(role["id"]), "at": datetime.now(timezone.utc)},
                 "$inc": {"count": 1}},
                upsert=True,
            )
        except Exception as e:
            _debug(f"claim record failed: {type(e).__name__}")

        await interaction.followup.send(
            embed=embeds.success_embed(
                "Redeemed",
                f"Gamepass verified. You've been ranked **{role['name']}** in **{cfg.get('group_name') or group_id}**.",
            ),
            ephemeral=True,
        )
        await self._log(guild, embeds.info_embed(
            "Gamepass Redeemed",
            f"{interaction.user.mention} redeemed **{cfg['name']}** and was ranked **{role['name']}** "
            f"(Roblox ID `{roblox_id}`).",
        ))

    @redeem.autocomplete("product")
    async def _redeem_product_ac(self, interaction: discord.Interaction, current: str):
        return await self._product_choices(interaction, current)

    # ---- /redeemadmin ---------------------------------------------------- #

    @redeemadmin.command(name="add", description="Link a gamepass to a group role (or update one).")
    @app_commands.describe(
        name="Short name, e.g. BOD",
        gamepass_id="The number in the gamepass page URL",
        group_id="A Roblox group bound to this server",
        role="The role buyers get",
    )
    @require_level(REDEEM_ADMIN_LEVEL)
    async def add_product(self, interaction: discord.Interaction, name: str, gamepass_id: int, group_id: int, role: int):
        await interaction.response.defer(ephemeral=True)
        name = name.strip()[:32]
        if not name:
            return await interaction.followup.send(embed=embeds.error_embed("Can't Do That", "Give it a name."), ephemeral=True)
        binds = await db.list_groupbinds(interaction.guild.id)
        bound = next((b for b in binds if str(b["group_id"]) == str(group_id)), None)
        if bound is None:
            return await interaction.followup.send(
                embed=embeds.error_embed("Can't Do That", "That group isn't bound to this server. Bind it first with `/groupbind add`."),
                ephemeral=True,
            )
        try:
            roles = await roblox.get_group_roles(group_id)
        except Exception:
            roles = None
        picked = next((r for r in roles or [] if str(r.get("id")) == str(role)), None)
        if picked is None:
            return await interaction.followup.send(embed=embeds.error_embed("Can't Do That", "That role doesn't exist in the group."), ephemeral=True)
        if picked.get("rank") in (0, 255):
            return await interaction.followup.send(embed=embeds.error_embed("Can't Do That", "The Guest and Owner roles can't be used."), ephemeral=True)

        doc = {
            "guild_id": str(interaction.guild.id),
            "key": name.lower(),
            "name": name,
            "gamepass_id": str(gamepass_id),
            "group_id": str(group_id),
            "group_name": bound.get("group_name") or str(group_id),
            "role_id": str(picked["id"]),
            "role_name": picked.get("name"),
        }
        await _cfg().update_one({"guild_id": doc["guild_id"], "name": name}, {"$set": doc}, upsert=True)
        await interaction.followup.send(
            embed=embeds.success_embed(
                "Redeem Set Up",
                f"**{name}** (gamepass `{gamepass_id}`) now ranks buyers to **{picked.get('name')}** in **{doc['group_name']}**.\n\n"
                f"The bot's Roblox account must rank **above** that role or ranking will fail.",
            ),
            ephemeral=True,
        )

    @add_product.autocomplete("group_id")
    async def _add_group_ac(self, interaction: discord.Interaction, current: str):
        return await self._group_choices(interaction, current)

    @add_product.autocomplete("role")
    async def _add_role_ac(self, interaction: discord.Interaction, current: str):
        return await self._role_choices(interaction, current)

    @redeemadmin.command(name="remove", description="Stop a gamepass from ranking people.")
    @app_commands.describe(name="The name you gave it")
    @require_level(REDEEM_ADMIN_LEVEL)
    async def remove_product(self, interaction: discord.Interaction, name: str):
        result = await _cfg().delete_one({"guild_id": str(interaction.guild.id), "name": name})
        if result.deleted_count:
            embed = embeds.success_embed("Removed", f"**{name}** can no longer be redeemed.")
        else:
            embed = embeds.error_embed("Not Found", "No gamepass with that name.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @remove_product.autocomplete("name")
    async def _remove_name_ac(self, interaction: discord.Interaction, current: str):
        return await self._product_choices(interaction, current)

    @redeemadmin.command(name="list", description="Show every redeemable gamepass.")
    @require_level(REDEEM_ADMIN_LEVEL)
    async def list_products(self, interaction: discord.Interaction):
        products = await self._products(interaction.guild.id)
        if not products:
            return await interaction.response.send_message(
                embed=embeds.info_embed("Redeem", "Nothing set up yet. Use `/redeemadmin add`."), ephemeral=True
            )
        rows = [
            f"**{p['name']}** - gamepass `{p['gamepass_id']}` -> **{p.get('role_name')}** in {p.get('group_name')}"
            for p in products
        ]
        await interaction.response.send_message(embed=embeds.info_embed("Redeemable Gamepasses", "\n".join(rows)[:4000]), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Redeem(bot))
