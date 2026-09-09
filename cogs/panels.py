"""
cogs/panels.py
---------------
/panel verification  - posts the persistent verification panel, branded
                       per-guild (Army Name / Crest via /setup -> Branding)
/panel tickets        - posts the REPORT TICKETS / OTHER TICKETS panels,
                        each with a single "Create Ticket" button
"""

import discord
from discord import app_commands
from discord.ext import commands

from utils import embeds
from utils.permissions import require_level
from cogs.verification import VerificationView
from cogs.tickets import ReportPanelView, OtherPanelView


class PanelGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="panel", description="Post Royal Guard panels.")


class Panels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = PanelGroup()

        self.group.add_command(
            app_commands.Command(name="verification", description="Post the Roblox verification panel.",
                                  callback=self.panel_verification)
        )
        self.group.add_command(
            app_commands.Command(name="tickets", description="Post the ticket support panels.",
                                  callback=self.panel_tickets)
        )
        bot.tree.add_command(self.group)

    @require_level(10)
    async def panel_verification(self, interaction: discord.Interaction):
        embed = await embeds.verification_panel_embed(interaction.guild.id)
        await interaction.channel.send(embed=embed, view=VerificationView())
        await interaction.response.send_message(
            embed=embeds.success_embed("Panel Posted", "Verification panel has been posted."), ephemeral=True
        )

    @require_level(10)
    async def panel_tickets(self, interaction: discord.Interaction):
        report_embed = embeds.base_embed(
            title="REPORT TICKETS",
            description="Press the 🚨 **Create Ticket** for tickets to report an incident or other users."
        )
        await interaction.channel.send(embed=report_embed, view=ReportPanelView())

        other_embed = embeds.base_embed(
            title="OTHER TICKETS",
            description="Press the 🚨 **Create Ticket** for tickets regarding other matters."
        )
        await interaction.channel.send(embed=other_embed, view=OtherPanelView())

        await interaction.response.send_message(
            embed=embeds.success_embed("Panels Posted", "Ticket panels have been posted."), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Panels(bot))
