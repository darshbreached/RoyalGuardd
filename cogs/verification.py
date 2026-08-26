"""
cogs/verification.py
---------------------
Roblox OAuth2 verification system. The panel posted by /panel verification
uses a persistent View so buttons keep working after bot restarts.

Flow:
1. User clicks "Verify via ROBLOX Login"
2. If they're already verified in the database, they're shown a
   confirmation ("Is this your ROBLOX account?") with Yes/No buttons
   instead of immediately being sent a new link.
   - Yes -> shown "You are already verified" with an Update Roles button
   - No  -> sent a fresh verification link to reverify with a different account
3. If they're not verified yet, they're sent a "Begin Verification" link
   button that opens the OAuth flow in their browser
4. Website handles the Roblox OAuth2 code exchange and calls back into
   MongoDB directly (see website/routes/oauth.py) storing the link
5. User clicks "Update Roles" (or runs /update) to sync roles immediately

All panel/embed titles are pulled from config/settings.py (VERIFICATION_PANEL_TITLE)
rather than hardcoded here, so rebranding only ever requires editing settings.py.

All responses in this cog are ephemeral - note that deferring with
ephemeral=True does NOT make a later followup.send() ephemeral automatically;
every followup call below explicitly passes ephemeral=True as well.
"""

import os
import secrets
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from cogs.update import sync_member_roles
from utils.roblox import RobloxAPIError
from config import settings

WEBSITE_BASE_URL = os.getenv("WEBSITE_BASE_URL", "https://your-railway-app.up.railway.app")


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


class AlreadyVerifiedUpdateView(discord.ui.View):
    """Shown after the user confirms 'Yes, this is my account' - just the
    Update Roles button, matching the reference image."""

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

        desc = "Your roles are now up to date."
        if added:
            desc += f"\n**Added:** {', '.join(added)}"
        if removed:
            desc += f"\n**Removed:** {', '.join(removed)}"

        await interaction.followup.send(embed=embeds.success_embed("Roles Updated", desc), ephemeral=True)


class ConfirmAccountView(discord.ui.View):
    """Shown when the user is already verified - asks them to confirm the
    linked Roblox account is still correct before offering Update Roles."""

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
        await db.create_oauth_state(state, interaction.user.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.edit_message(embed=embed, view=view)


class VerificationView(discord.ui.View):
    """Persistent view - registered once in main.py with view=VerificationView(), timeout=None."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify via ROBLOX Login", style=discord.ButtonStyle.success,
                        custom_id="royalguard:verify_login", row=0)
    async def verify_login(self, interaction: discord.Interaction, button: discord.ui.Button):
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
        await db.create_oauth_state(state, interaction.user.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Verify via ROBLOX Game", style=discord.ButtonStyle.success,
                        custom_id="royalguard:verify_game", row=1)
    async def verify_game(self, interaction: discord.Interaction, button: discord.ui.Button):
        code = secrets.token_hex(3).upper()
        await db.create_oauth_state(f"gamecode:{code}", interaction.user.id)

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

        desc = "Your roles are now up to date."
        if added:
            desc += f"\n**Added:** {', '.join(added)}"
        if removed:
            desc += f"\n**Removed:** {', '.join(removed)}"

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
        await db.create_oauth_state(state, interaction.user.id)
        oauth_url = f"{WEBSITE_BASE_URL}/authorize?state={state}"

        embed, view = _begin_verification_embed_and_view(oauth_url)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))