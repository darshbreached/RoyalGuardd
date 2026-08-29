"""
cogs/ssu.py
------------
/ssu link:<roblox game link>

Posts a "Server Startup" announcement, pinging the role configured via
/setup -> Server Startup -> SSU Ping Role, in the channel configured via
/setup -> Channels -> SSU Channel (falls back to the channel the command
was run in if no SSU channel is configured).
"""

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level


class SSU(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ssu", description="Announce a Server Startup with a link to join.")
    @app_commands.describe(link="The Roblox game link to join")
    @require_level(20)
    async def ssu(self, interaction: discord.Interaction, link: str):
        guild_config = await db.get_guild_config(interaction.guild.id)

        target_channel = interaction.channel
        channel_id = guild_config.get("ssu_channel_id")
        if channel_id:
            configured_channel = interaction.guild.get_channel(int(channel_id))
            if configured_channel:
                target_channel = configured_channel

        ping_content = None
        role_id = guild_config.get("ssu_ping_role_id")
        if role_id:
            role = interaction.guild.get_role(int(role_id))
            if role:
                ping_content = role.mention

        embed = embeds.base_embed()
        embed.title = "Server Startup"
        embed.description = (
            "A Server Start Up is being hosted in the game right now. "
            "Use the link below to join.\n\n"
            f"**Link:** {link}\n"
            f"**Hosted By:** {interaction.user.mention}"
        )

        await target_channel.send(content=ping_content, embed=embed)

        await interaction.response.send_message(
            embed=embeds.success_embed("SSU Posted", f"Server Startup announcement posted in {target_channel.mention}."),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SSU(bot))
