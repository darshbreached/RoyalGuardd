"""
cogs/verification.py
---------------------
Roblox OAuth2 verification system.

/forceverify lets staff manually link a Discord user to a Roblox account,
bypassing the OAuth flow entirely - for cases where a member's browser/
network can't complete verification normally. Gated at admin level 50.
Logged to the same per-guild verification webhook as normal verifications,
clearly flagged as a manual override.

/unverify (admin level 50) removes a member's Roblox link, takes back their
verified roles (setup -> Verified Roles) and any timezone role, and gives back
the Non-Verified role if one is configured. Roles that come from rankbinds are
NOT touched. Logged to the same verification webhook.

The panel posted by /panel verification uses VerificationView below:
"Verify via ROBLOX Login" (switched off - flip OAUTH_LOGIN_ENABLED to bring it
back), "Verify via ROBLOX Ticket" (hands off to
cogs/ticketverify.py; already-verified members get the same "Is this your ROBLOX
account?" Yes/No as the Login button, and No opens a ticket to re-verify), "Verify via ROBLOX Game" (switched off - flip
GAME_VERIFY_ENABLED to bring it back), and "Update Roles". Panels that were
posted BEFORE a change keep their old buttons until /panel verification is run again.

Every "sync roles" action (the Update Roles buttons and /forceverify) also
assigns a timezone role based on the country/region captured during
verification - see utils/timezones.py for the country->label mapping and
its accuracy limitations. Assignment is by exact role NAME match
(e.g. a role literally named "EST"), same convention as verified_roles/
extra_roles - if the guild hasn't created that role, it's silently skipped.
"""

import os
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
import secrets

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level
from utils import roblox
from utils.timezones import get_timezone_label, all_timezone_labels
from cogs.update import sync_member_roles
from utils.roblox import RobloxAPIError
from config import settings

WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://your-railway-app.up.railway.app")

OAUTH_LOGIN_ENABLED = False  # True turns "Verify via ROBLOX Login" back on
GAME_VERIFY_ENABLED = False  # True turns "Verify via ROBLOX Game" back on

_ALL_TZ_LABELS = all_timezone_labels()


async def _apply_timezone_role(guild: discord.Guild, member: discord.Member, verification: dict) -> str:
    """Adds the matching timezone role by name and removes any other
    timezone-label role the member currently holds. Returns the label that
    was applied, or None if no country data / no matching role exists."""
    country_code = verification.get("verification_country_code")
    region = verification.get("verification_region")
    label = get_timezone_label(country_code, region)

    # Strip any other timezone-label role first, regardless of whether a
    # new one will be applied - handles someone re-verifying from a
    # different country.
    to_remove = [r for r in member.roles if r.name in _ALL_TZ_LABELS and r.name != label]
    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason="Timezone role changed on re-verification")
        except Exception:
            pass

    if not label:
        return None

    target_role = discord.utils.get(guild.roles, name=label)
    if not target_role:
        return None  # guild hasn't created this timezone role - skip silently

    if target_role not in member.roles:
        try:
            await member.add_roles(target_role, reason="Timezone role from verification country/region")
        except Exception:
            return None

    return label


def _begin_verification_embed_and_view(oauth_url: str) -> tuple[discord.Embed, discord.ui.View]:
    embed = embeds.info_embed(
        settings.VERIFICATION_PANEL_TITLE,
        "Click on the button below to begin verification process\n\n"
        "**Please DO NOT share this link with anyone**\n\n"
        "This link expires in **2 minutes** or once the verification process begins."
    )

    view = discord.ui.View(timeout=120)
    link_button = discord.ui.Button(
        label="Begin Verification",
        style=discord.ButtonStyle.link,
        url=oauth_url,
    )
    view.add_item(link_button)

    return embed, view


async def _post_force_verify_log(guild: discord.Guild, target: discord.Member, roblox_id: str, roblox_username: str, staff: discord.Member):
    import requests

    guild_config = await db.get_guild_config(guild.id)
    webhook_url = guild_config.get("verification_webhook_url") or os.getenv("DISCORD_VERIFICATION_WEBHOOK")
    if not webhook_url:
        return

    profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"
    description = (
        f"Discord: {target.mention} | `{target.id}`\n"
        f"ROBLOX: {roblox_username} | {profile_url}\n"
        f"Method: **Manual Override (/forceverify)**\n\n"
        f"⚠️ Manually verified by {staff.mention} (`{staff.id}`) — bypassed OAuth, no IP/account-age checks were run."
    )

    embed = {"title": "Verification Logs", "description": description, "color": 0x9B59B6}

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=5)
    except Exception:
        pass


async def _post_unverify_log(guild: discord.Guild, target: discord.Member, record: dict, staff: discord.Member):
    guild_config = await db.get_guild_config(guild.id) or {}
    webhook_url = guild_config.get("verification_webhook_url") or os.getenv("DISCORD_VERIFICATION_WEBHOOK")
    if not webhook_url:
        return

    description = (
        f"Discord: {target.mention} | `{target.id}`\n"
        f"ROBLOX: {record.get('roblox_username')} | `{record.get('roblox_id')}`\n"
        f"Method: **Unverified (/unverify)**\n\n"
        f"Unverified by {staff.mention} (`{staff.id}`)."
    )
    embed = {"title": "Verification Logs", "description": description, "color": 0xE74C3C}

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            await session.post(webhook_url, json={"embeds": [embed]})
    except Exception:
        pass


class AlreadyVerifiedUpdateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Update Roles", style=discord.ButtonStyle.success)
    async def update_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        verification = await db.get_verification(interaction.user.id)
        if not verification:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Verified", "You need to verify your Roblox account first."),
                ephemeral=True,
            )

        try:
            added, removed, _ = await sync_member_roles(interaction.guild, interaction.user, int(verification["roblox_id"]))
        except RobloxAPIError:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "Roblox Temporarily Unavailable",
                    "Roblox's API didn't respond correctly just now, so no roles were changed. Please try again in a minute."
                ),
                ephemeral=True,
            )

        tz_label = await _apply_timezone_role(interaction.guild, interaction.user, verification)

        desc = "Your roles are now up to date."
        if added:
            desc += f"\n**Added:** {', '.join(added)}"
        if removed:
            desc += f"\n**Removed:** {', '.join(removed)}"
        if tz_label:
            desc += f"\n**Timezone:** {tz_label}"

        await interaction.followup.send(embed=embeds.success_embed("Roles Updated", desc), ephemeral=True)


class ConfirmAccountView(discord.ui.View):
    def __init__(self, roblox_username: str, roblox_id: str):
        super().__init__(timeout=180)
        self.roblox_username = roblox_username
        self.roblox_id = roblox_id

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = embeds.warning_embed(
            settings.VERIFICATION_PANEL_TITLE,
            "You are already verified. If you wish to retrieve new roles or update yourself, "
            "please use the button below."
        )
        await interaction.response.edit_message(embed=embed, view=AlreadyVerifiedUpdateView())

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def confirm_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = secrets.token_urlsafe(24)
        await db.create_oauth_state(state, interaction.user.id, interaction.guild.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.edit_message(embed=embed, view=view)


class ConfirmTicketAccountView(discord.ui.View):
    """Shown when an already-verified member presses "Verify via ROBLOX Ticket".
    Yes -> Update Roles. No -> opens a verification ticket to link a different account."""

    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = embeds.warning_embed(
            settings.VERIFICATION_PANEL_TITLE,
            "You are already verified. If you wish to retrieve new roles or update yourself, "
            "please use the button below."
        )
        await interaction.response.edit_message(embed=embed, view=AlreadyVerifiedUpdateView())

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def confirm_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketVerify")
        if cog is None:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Unavailable", "Ticket verification isn't available right now. Use ROBLOX Login instead."),
                ephemeral=True,
            )
        await cog.start_ticket(interaction, reverify=True)


class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify via ROBLOX Login", style=discord.ButtonStyle.success,
                        custom_id="royalguard:verify_login", row=0, disabled=not OAUTH_LOGIN_ENABLED)
    async def verify_login(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not OAUTH_LOGIN_ENABLED:
            # Also covers panels posted before this was switched off.
            return await interaction.response.send_message(
                embed=embeds.warning_embed(
                    "Unavailable",
                    "Verification through ROBLOX Login is switched off. Use **Verify via ROBLOX Ticket** instead."
                ),
                ephemeral=True,
            )

        verification = await db.get_verification(interaction.user.id)

        if verification:
            roblox_id = verification["roblox_id"]
            roblox_username = verification["roblox_username"]
            profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

            embed = embeds.info_embed(
                settings.VERIFICATION_PANEL_TITLE,
                f"Is this your ROBLOX account?\n\n"
                f"ROBLOX Username: [{roblox_username}]({profile_url})\n"
                f"ROBLOX Profile: {profile_url}"
            )
            view = ConfirmAccountView(roblox_username, roblox_id)
            return await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        state = secrets.token_urlsafe(24)
        await db.create_oauth_state(state, interaction.user.id, interaction.guild.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Verify via ROBLOX Ticket", style=discord.ButtonStyle.success,
                        custom_id="royalguard:verify_ticket", row=0)
    async def verify_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = interaction.client.get_cog("TicketVerify")
        if cog is None:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Unavailable", "Ticket verification isn't available right now. Use ROBLOX Login instead."),
                ephemeral=True,
            )

        verification = await db.get_verification(interaction.user.id)
        if verification:
            roblox_id = verification["roblox_id"]
            roblox_username = verification["roblox_username"]
            profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

            embed = embeds.info_embed(
                settings.VERIFICATION_PANEL_TITLE,
                f"Is this your ROBLOX account?\n\n"
                f"ROBLOX Username: [{roblox_username}]({profile_url})\n"
                f"ROBLOX Profile: {profile_url}"
            )
            return await interaction.response.send_message(embed=embed, view=ConfirmTicketAccountView(), ephemeral=True)

        await cog.start_ticket(interaction)

    @discord.ui.button(label="Verify via ROBLOX Game", style=discord.ButtonStyle.success,
                        custom_id="royalguard:verify_game", row=1, disabled=not GAME_VERIFY_ENABLED)
    async def verify_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not GAME_VERIFY_ENABLED:
            # Also covers panels posted before this was switched off.
            return await interaction.response.send_message(
                embed=embeds.warning_embed(
                    "Unavailable",
                    "Verification through the ROBLOX game is switched off. Use **Verify via ROBLOX Login** or **Verify via ROBLOX Ticket** instead."
                ),
                ephemeral=True,
            )

        code = secrets.token_hex(3).upper()
        await db.create_oauth_state(f"gamecode:{code}", interaction.user.id, interaction.guild.id)

        embed = embeds.info_embed(
            "Verify via ROBLOX Game",
            f"Join the verification game and enter this code when prompted:\n\n"
            f"**`{code}`**\n\nThis code expires in 2 minutes."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Update Roles", style=discord.ButtonStyle.success,
                        custom_id="royalguard:update_roles", row=1)
    async def update_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        verification = await db.get_verification(interaction.user.id)
        if not verification:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Verified", "You need to verify your Roblox account first."),
                ephemeral=True,
            )

        try:
            added, removed, _ = await sync_member_roles(interaction.guild, interaction.user, int(verification["roblox_id"]))
        except RobloxAPIError:
            return await interaction.followup.send(
                embed=embeds.error_embed(
                    "Roblox Temporarily Unavailable",
                    "Roblox's API didn't respond correctly just now, so no roles were changed. Please try again in a minute."
                ),
                ephemeral=True,
            )

        tz_label = await _apply_timezone_role(interaction.guild, interaction.user, verification)

        desc = "Your roles are now up to date."
        if added:
            desc += f"\n**Added:** {', '.join(added)}"
        if removed:
            desc += f"\n**Removed:** {', '.join(removed)}"
        if tz_label:
            desc += f"\n**Timezone:** {tz_label}"

        await interaction.followup.send(embed=embeds.success_embed("Roles Updated", desc), ephemeral=True)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="verify", description="Check or start your Roblox verification.")
    async def verify(self, interaction: discord.Interaction):
        verification = await db.get_verification(interaction.user.id)
        if verification:
            roblox_id = verification["roblox_id"]
            roblox_username = verification["roblox_username"]
            profile_url = f"https://www.roblox.com/users/{roblox_id}/profile"

            embed = embeds.info_embed(
                settings.VERIFICATION_PANEL_TITLE,
                f"Is this your ROBLOX account?\n\n"
                f"ROBLOX Username: [{roblox_username}]({profile_url})\n"
                f"ROBLOX Profile: {profile_url}"
            )
            view = ConfirmAccountView(roblox_username, roblox_id)
            return await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        state = secrets.token_urlsafe(24)
        await db.create_oauth_state(state, interaction.user.id, interaction.guild.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="forceverify", description="Manually link a member to a Roblox account, bypassing OAuth.")
    @app_commands.describe(
        user="The Discord member to verify",
        roblox_username_or_id="Their Roblox username or numeric user ID",
        sync_roles="Immediately sync their roles after linking (default: yes)",
    )
    @require_level(50)
    async def forceverify(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        roblox_username_or_id: str,
        sync_roles: bool = True,
    ):
        await interaction.response.defer()

        roblox_username_or_id = roblox_username_or_id.strip()

        if roblox_username_or_id.isdigit():
            roblox_user = await roblox.get_user_by_id(int(roblox_username_or_id))
        else:
            roblox_user = await roblox.get_user_by_username(roblox_username_or_id)

        if not roblox_user:
            return await interaction.followup.send(
                embed=embeds.error_embed("Roblox User Not Found", f"Could not find a Roblox account matching `{roblox_username_or_id}`.")
            )

        roblox_id = roblox_user.get("id")
        roblox_username = roblox_user.get("name")

        await db.set_verification(user.id, roblox_id, roblox_username)

        await _post_force_verify_log(interaction.guild, user, str(roblox_id), roblox_username, interaction.user)

        result_desc = f"{user.mention} has been manually linked to **{roblox_username}** (`{roblox_id}`)."

        if sync_roles:
            try:
                added, removed, _ = await sync_member_roles(interaction.guild, user, roblox_id)
                if added:
                    result_desc += f"\n**Added:** {', '.join(added)}"
                if removed:
                    result_desc += f"\n**Removed:** {', '.join(removed)}"
            except RobloxAPIError:
                result_desc += "\n\n⚠️ Linked successfully, but role sync failed (Roblox API temporarily unavailable). Run /update on them later."

            # Note: forceverify bypasses OAuth entirely, so there's no
            # verification_country_code stored for this user - the
            # timezone role can't be applied here since we have no
            # location data at all for a manually-linked account.

        await interaction.followup.send(embed=embeds.success_embed("Force Verified", result_desc))

    @app_commands.command(name="unverify", description="Remove a member's Roblox link and their verified roles.")
    @app_commands.describe(user="The Discord member to unverify")
    @require_level(50)
    async def unverify(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer()

        if user.bot:
            return await interaction.followup.send(embed=embeds.error_embed("Can't Do That", "Bots can't be verified."))

        record = await db.get_verification(user.id)
        if not record:
            return await interaction.followup.send(embed=embeds.error_embed("Not Verified", f"{user.mention} isn't verified."))

        await db.remove_verification(user.id)

        guild_config = await db.get_guild_config(interaction.guild.id) or {}
        reason = f"Unverified by {interaction.user}"
        role_problem = False

        # Verified roles (setup -> Verified Roles, by name) and timezone roles come off.
        raw = guild_config.get("verified_roles") or []
        names = raw.split(",") if isinstance(raw, str) else raw
        names = {str(n).strip() for n in names if str(n).strip()}
        to_remove = [r for r in user.roles if r.name in names or r.name in _ALL_TZ_LABELS]
        if to_remove:
            try:
                await user.remove_roles(*to_remove, reason=reason)
            except discord.HTTPException:
                role_problem = True

        # The Non-Verified role (setup -> Non-Verified Role) goes back on.
        gave_non_verified = False
        try:
            nv_id = guild_config.get("non_verified_role_id")
            nv_role = interaction.guild.get_role(int(nv_id)) if nv_id else None
        except (TypeError, ValueError):
            nv_role = None
        if nv_role and nv_role not in user.roles:
            try:
                await user.add_roles(nv_role, reason=reason)
                gave_non_verified = True
            except discord.HTTPException:
                role_problem = True

        await _post_unverify_log(interaction.guild, user, record, interaction.user)

        desc = f"{user.mention} is no longer linked to **{record.get('roblox_username')}** (`{record.get('roblox_id')}`)."
        if to_remove and not role_problem:
            desc += f"\n**Removed:** {', '.join(r.name for r in to_remove)}"
        if gave_non_verified:
            desc += f"\n**Added:** {nv_role.name}"
        desc += "\n\nRoles that come from rankbinds were not touched."
        if role_problem:
            desc += "\n\n⚠️ Some roles couldn't be changed. Make sure my role is above them in the role list."

        await interaction.followup.send(embed=embeds.success_embed("Unverified", desc))


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))