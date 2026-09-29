"""
cogs/loa.py
-----------
/loa request days:<int> reason:<str>  - general staff (level 10+) request
                                         a leave of absence
/loa end user:<member>                - approvers (level 20+) manually end
                                         someone's LOA early
/loa list                             - shows everyone currently on LOA
/loa setup role:<role> log_channel:<channel>  - configure this server
                                                (level 50+)

Approval flow: a request posts an embed with Approve/Deny buttons to the
configured LOA log channel. Anyone level 20+ can click either button - this
mirrors the kick/mute threshold, since no specific approver level was given.
Approving assigns the configured LOA role and schedules an automatic end
after the requested number of days. Denying just marks the request denied
and DMs the requester.

The buttons use a persistent view (timeout=None, custom_id encodes the
request's Mongo _id) so they keep working across bot restarts - re-added
for every still-pending request in on_ready, since a persistent view has
to be re-registered after a restart to keep handling clicks on old
messages.

A background task (loa_check_loop) runs every 5 minutes, finds every
approved LOA whose end_time has passed across ALL guilds, removes the LOA
role, marks it "ended", and logs it - this is what makes "ends
automatically after the requested days" work without anyone running a
command.
"""

import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from database.mongodb import db
from utils import embeds
from utils.permissions import require_level, has_level

CHECK_INTERVAL_MINUTES = 5


class LOAGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="loa", description="Request and manage leave of absence.")


class LOAApprovalView(discord.ui.View):
    """Persistent - has no state of its own beyond parsing the request_id
    out of each button's custom_id, so one instance re-added on_ready
    handles every pending request's message, old or new."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _get_request(self, interaction: discord.Interaction, custom_id_prefix: str):
        request_id = interaction.data["custom_id"].split(":", 1)[1]
        request = await db.get_loa_request(request_id)
        if request is None:
            await interaction.response.send_message(
                embed=embeds.error_embed("Not Found", "This LOA request no longer exists."), ephemeral=True
            )
            return None, None
        if request["status"] != "pending":
            await interaction.response.send_message(
                embed=embeds.info_embed("Already Resolved", f"This request was already **{request['status']}**."),
                ephemeral=True,
            )
            return None, None
        if not await has_level(interaction.user.id, interaction.guild, 20):
            await interaction.response.send_message(
                embed=embeds.error_embed("Not Allowed", "You need level 20+ to approve or deny LOA requests."),
                ephemeral=True,
            )
            return None, None
        return request_id, request

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="loa_approve:placeholder")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        request_id, request = await self._get_request(interaction, "loa_approve")
        if request is None:
            return

        guild = interaction.guild
        member = guild.get_member(int(request["user_id"]))
        guild_config = await db.get_guild_config(guild.id)
        role_id = guild_config.get("loa_role_id")
        role = guild.get_role(int(role_id)) if role_id else None

        if role is None:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Not Configured", "No LOA role is set. Run `/loa setup` first."),
                ephemeral=True,
            )

        end_time = time.time() + request["days"] * 86400
        await db.approve_loa_request(request_id, interaction.user.id, end_time)

        if member:
            try:
                await member.add_roles(role, reason=f"LOA approved by {interaction.user}")
            except discord.Forbidden:
                pass

        embed = embeds.success_embed(
            "LOA Approved",
            f"<@{request['user_id']}>'s leave of absence was approved by {interaction.user.mention} "
            f"for **{request['days']} day(s)**.\n**Reason:** {request['reason']}"
        )
        await interaction.response.edit_message(embed=embed, view=None)

        if member:
            try:
                await member.send(embed=embeds.success_embed(
                    "LOA Approved",
                    f"Your leave of absence request in **{guild.name}** was approved for **{request['days']} day(s)**."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="loa_deny:placeholder")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        request_id, request = await self._get_request(interaction, "loa_deny")
        if request is None:
            return

        await db.deny_loa_request(request_id, interaction.user.id)

        embed = embeds.error_embed(
            "LOA Denied",
            f"<@{request['user_id']}>'s leave of absence request was denied by {interaction.user.mention}."
        )
        await interaction.response.edit_message(embed=embed, view=None)

        guild = interaction.guild
        member = guild.get_member(int(request["user_id"]))
        if member:
            try:
                await member.send(embed=embeds.error_embed(
                    "LOA Denied",
                    f"Your leave of absence request in **{guild.name}** was denied."
                ))
            except (discord.Forbidden, discord.HTTPException):
                pass


class LOA(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.group = LOAGroup()

        self.group.add_command(
            app_commands.Command(name="request", description="Request a leave of absence.", callback=self.loa_request)
        )
        self.group.add_command(
            app_commands.Command(name="end", description="Manually end someone's active LOA.", callback=self.loa_end)
        )
        self.group.add_command(
            app_commands.Command(name="list", description="List everyone currently on LOA.", callback=self.loa_list)
        )
        self.group.add_command(
            app_commands.Command(name="setup", description="Configure the LOA role and log channel.", callback=self.loa_setup)
        )
        bot.tree.add_command(self.group)

        self.loa_check_loop.start()

    def cog_unload(self):
        self.loa_check_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        # Re-register the persistent view so old pending-request messages'
        # buttons keep working after a restart - discord.py forgets views
        # across process restarts otherwise.
        self.bot.add_view(LOAApprovalView())

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def loa_check_loop(self):
        due = await db.get_due_loa_requests(before=time.time())
        for request in due:
            guild = self.bot.get_guild(int(request["guild_id"]))
            if guild is None:
                await db.end_loa_request(request["_id"])
                continue

            member = guild.get_member(int(request["user_id"]))
            guild_config = await db.get_guild_config(guild.id)
            role_id = guild_config.get("loa_role_id")
            role = guild.get_role(int(role_id)) if role_id else None

            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="LOA period ended")
                except discord.Forbidden:
                    pass

            await db.end_loa_request(request["_id"])

            log_channel_id = await db.get_log_channel(guild.id, "loa")
            if log_channel_id:
                channel = guild.get_channel(int(log_channel_id))
                if channel:
                    await channel.send(embed=embeds.info_embed(
                        "LOA Ended",
                        f"<@{request['user_id']}>'s leave of absence has ended automatically after "
                        f"**{request['days']} day(s)**."
                    ))

    @loa_check_loop.before_loop
    async def before_loa_check_loop(self):
        await self.bot.wait_until_ready()

    @require_level(10)
    @app_commands.describe(days="How many days you're requesting off", reason="Reason for the leave of absence")
    async def loa_request(self, interaction: discord.Interaction, days: app_commands.Range[int, 1, 365], reason: str):
        await interaction.response.defer(ephemeral=True)

        existing = await db.get_active_loa_for_user(interaction.guild.id, interaction.user.id)
        if existing:
            return await interaction.followup.send(
                embed=embeds.error_embed("Already On LOA", "You already have an active leave of absence.")
            )

        request = await db.create_loa_request(interaction.guild.id, interaction.user.id, days, reason)

        log_channel_id = await db.get_log_channel(interaction.guild.id, "loa")
        if not log_channel_id:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Configured", "No LOA log channel is set. Ask an admin to run `/loa setup`.")
            )
        channel = interaction.guild.get_channel(int(log_channel_id))
        if not channel:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not Configured", "The configured LOA log channel no longer exists.")
            )

        embed = embeds.info_embed(
            "Leave of Absence Request",
            f"{interaction.user.mention} is requesting **{days} day(s)** off.\n**Reason:** {reason}"
        )
        view = LOAApprovalView()
        view.approve.custom_id = f"loa_approve:{request['_id']}"
        view.deny.custom_id = f"loa_deny:{request['_id']}"
        await channel.send(embed=embed, view=view)

        await interaction.followup.send(
            embed=embeds.success_embed("Request Submitted", "Your leave of absence request has been submitted for approval.")
        )

    @require_level(20)
    @app_commands.describe(user="The user whose LOA to end early")
    async def loa_end(self, interaction: discord.Interaction, user: discord.Member):
        await interaction.response.defer(ephemeral=True)

        request = await db.get_active_loa_for_user(interaction.guild.id, user.id)
        if not request:
            return await interaction.followup.send(
                embed=embeds.error_embed("Not On LOA", f"{user.mention} doesn't have an active LOA.")
            )

        guild_config = await db.get_guild_config(interaction.guild.id)
        role_id = guild_config.get("loa_role_id")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if role and role in user.roles:
            try:
                await user.remove_roles(role, reason=f"LOA ended early by {interaction.user}")
            except discord.Forbidden:
                pass

        await db.end_loa_request(request["_id"], ended_by=interaction.user.id)

        embed = embeds.success_embed("LOA Ended", f"{user.mention}'s leave of absence was ended early by {interaction.user.mention}.")
        await interaction.followup.send(embed=embed)

        log_channel_id = await db.get_log_channel(interaction.guild.id, "loa")
        if log_channel_id:
            channel = interaction.guild.get_channel(int(log_channel_id))
            if channel:
                await channel.send(embed=embed)

    @require_level(10)
    async def loa_list(self, interaction: discord.Interaction):
        active = await db.list_active_loas(interaction.guild.id)
        if not active:
            return await interaction.response.send_message(
                embed=embeds.info_embed("No Active LOAs", "Nobody is currently on leave of absence."), ephemeral=True
            )

        lines = []
        for r in active:
            end_str = f"<t:{int(r['end_time'])}:R>" if r.get("end_time") else "unknown"
            lines.append(f"• <@{r['user_id']}> — ends {end_str} — {r['reason']}")

        await interaction.response.send_message(embed=embeds.info_embed("Active LOAs", "\n".join(lines)), ephemeral=True)

    @require_level(50)
    @app_commands.describe(role="Role given to staff while on LOA", log_channel="Channel for LOA requests and logs")
    async def loa_setup(self, interaction: discord.Interaction, role: discord.Role, log_channel: discord.TextChannel):
        await db.set_guild_config(interaction.guild.id, loa_role_id=str(role.id))
        await db.set_log_channel(interaction.guild.id, "loa", log_channel.id)

        description = f"LOA role set to {role.mention}. Requests and logs will post in {log_channel.mention}."
        if role >= interaction.guild.me.top_role:
            description += f"\n\nWarning: {role.mention} is above my highest role, so I can't assign it."
        await interaction.response.send_message(embed=embeds.success_embed("LOA Configured", description), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LOA(bot))
