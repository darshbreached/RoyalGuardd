"""
cogs/tickets.py
----------------
Two ticket panels (Report / Other), each showing a single red "Create
Ticket" button. Clicking it opens an ephemeral "Select Ticket Category"
dropdown, matching the reference layout. Selecting a category creates
the routed ticket channel with verified-user gating, support-role access,
a close button, and transcript generation.
"""

import io
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from config import settings


def _find_option(category_key: str):
    for label, key, emoji in settings.REPORT_TICKET_OPTIONS + settings.OTHER_TICKET_OPTIONS:
        if key == category_key:
            return label, emoji
    return category_key, "🎫"


async def _create_ticket_channel(interaction: discord.Interaction, category_key: str):
    guild = interaction.guild
    config = await db.get_ticket_config(guild.id)

    if not config:
        return await interaction.response.send_message(
            embed=embeds.error_embed("Not Configured", "The ticket system has not been configured yet. Ask an admin to run `/ticketconfig` first."),
            ephemeral=True,
        )

    if config.get("require_verification", True):
        verification = await db.get_verification(interaction.user.id)
        if not verification:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Warning - Not Verified", "You must be verified to create report or other tickets."),
                ephemeral=True,
            )

    label, emoji = _find_option(category_key)

    category_channel = None
    category_id = config.get("category_id")
    if category_id:
        category_channel = guild.get_channel(int(category_id))

    support_role_id = config.get("support_role_id")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
    }
    if support_role_id:
        role = guild.get_role(int(support_role_id))
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    channel_name = f"ticket-{interaction.user.name}"[:90]

    await interaction.response.defer(ephemeral=True)
    channel = await guild.create_text_channel(
        name=channel_name,
        category=category_channel,
        overwrites=overwrites,
        topic=f"Ticket | {label} | Opened by {interaction.user} ({interaction.user.id})",
    )

    await db.create_ticket(channel.id, guild.id, interaction.user.id, category_key)

    intro = embeds.info_embed(
        label,
        f"{interaction.user.mention} has opened a ticket for **{label}**.\n\n"
        "Please describe your issue in detail. A member of staff will assist you shortly."
    )
    await channel.send(embed=intro, view=CloseTicketView())

    await interaction.followup.send(
        embed=embeds.success_embed("Ticket Created", f"Your ticket has been created: {channel.mention}"),
        ephemeral=True,
    )


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger,
                        custom_id="royalguard:close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        ticket = await db.get_ticket(interaction.channel.id)
        if not ticket:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not a Ticket", "This isn't a tracked ticket channel."), ephemeral=True
            )

        await interaction.response.send_message(embed=embeds.info_embed("Closing Ticket", "Generating transcript..."))

        lines = []
        async for msg in interaction.channel.history(limit=None, oldest_first=True):
            timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"[{timestamp}] {msg.author}: {msg.content}")
        transcript_text = "\n".join(lines) if lines else "No messages."

        transcript_file = discord.File(
            io.BytesIO(transcript_text.encode("utf-8")),
            filename=f"transcript-{interaction.channel.name}.txt",
        )

        config = await db.get_ticket_config(interaction.guild.id)
        log_channel = None
        if config and config.get("log_channel_id"):
            log_channel = interaction.guild.get_channel(int(config["log_channel_id"]))

        if log_channel:
            await log_channel.send(
                embed=embeds.info_embed("Ticket Closed", f"Channel: `{interaction.channel.name}`\nClosed by: {interaction.user.mention}"),
                file=transcript_file,
            )

        await db.close_ticket(interaction.channel.id)
        await interaction.channel.send("This ticket will be deleted in 5 seconds.")
        await discord.utils.sleep_until(discord.utils.utcnow() + __import__("datetime").timedelta(seconds=5))
        await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")


class ReportTicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key)
            for label, key, emoji in settings.REPORT_TICKET_OPTIONS
        ]
        super().__init__(placeholder="Select Ticket Category", options=options)

    async def callback(self, interaction: discord.Interaction):
        await _create_ticket_channel(interaction, self.values[0])


class OtherTicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=label, value=key)
            for label, key, emoji in settings.OTHER_TICKET_OPTIONS
        ]
        super().__init__(placeholder="Select Ticket Category", options=options)

    async def callback(self, interaction: discord.Interaction):
        await _create_ticket_channel(interaction, self.values[0])


class ReportPanelView(discord.ui.View):
    """Persistent view - the single red button shown on the REPORT TICKETS panel."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(embeds.powered_by_button())

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.danger,
                        custom_id="royalguard:report_panel_button")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(ReportTicketSelect())
        await interaction.response.send_message(
            embed=embeds.info_embed("Create Ticket", "Use the select menu below to select which ticket you wish to open"),
            view=view,
            ephemeral=True,
        )


class OtherPanelView(discord.ui.View):
    """Persistent view - the single red button shown on the OTHER TICKETS panel."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(embeds.powered_by_button())

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.danger,
                        custom_id="royalguard:other_panel_button")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=180)
        view.add_item(OtherTicketSelect())
        await interaction.response.send_message(
            embed=embeds.info_embed("Create Ticket", "Use the select menu below to select which ticket you wish to open"),
            view=view,
            ephemeral=True,
        )


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ticketconfig", description="Configure the ticket system for this server.")
    @app_commands.describe(
        category="Category channel new tickets are created under",
        support_role="Role that can view all tickets",
        log_channel="Channel to send closed-ticket transcripts to",
        require_verification="Whether users must be Roblox-verified to open a ticket",
    )
    async def ticketconfig(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        support_role: discord.Role,
        log_channel: discord.TextChannel,
        require_verification: bool = True,
    ):
        await db.set_ticket_config(
            interaction.guild.id,
            category_id=category.id,
            support_role_id=support_role.id,
            log_channel_id=log_channel.id,
            require_verification=require_verification,
        )
        await interaction.response.send_message(
            embed=embeds.success_embed("Ticket System Configured", "Settings saved successfully."), ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))