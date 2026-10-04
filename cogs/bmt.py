"""cogs/bmt.py — Basic Military Training graduation requests.

Flow (matches the reference recording):
  1. /bmt                -> modal: starting image link
  2. message + button    -> modal: ending image link
  3. message + button    -> modal: usernames (comma separated)
  4. confirmation embed  -> instructor presses Submit
  5. request is posted to the BMT logs channel with Accept / Deny buttons
  6. a reviewer presses Accept -> valid users are promoted to the BMT graduate rank
     (or Deny -> nothing changes)

Each modal is opened from a BUTTON click, never directly from another modal's
submit, because Discord does not support that.

Requests are stored in Mongo (collection "bmt_requests") so the Accept / Deny
buttons keep working after a restart or redeploy.
"""

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level

REQUESTS_COLLECTION = "bmt_requests"


def _requests():
    return db.db[REQUESTS_COLLECTION]


def _clean_link(value: str):
    """Return the stripped link if it looks like a URL, else None."""
    value = str(value).strip()
    if value.lower().startswith(("http://", "https://")) and " " not in value:
        return value
    return None


def _bullets(lines: list, limit: int = 1500) -> str:
    """Join lines as bullets, trimming so the embed never exceeds Discord's limits."""
    out, used = [], 0
    for i, line in enumerate(lines):
        text = f"• {line}"
        if used + len(text) > limit:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(text)
        used += len(text) + 1
    return "\n".join(out)


async def _show(interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View = None):
    """Update the step message in place; fall back to posting a new message."""
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
    except discord.HTTPException:
        kwargs = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)


# --------------------------------------------------------------------------- #
# Steps 1-3: the three modals, each opened from a button
# --------------------------------------------------------------------------- #

class StartImageModal(discord.ui.Modal, title="BMT System"):
    starting_image = discord.ui.TextInput(
        label="Please enter a link to your starting image",
        style=discord.TextStyle.paragraph,
        placeholder="Only enter the link to the starting image; eg; gyazo",
        required=True,
        max_length=4000,
    )

    def __init__(self, cog: "BMT"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        link = _clean_link(self.starting_image.value)
        if not link:
            return await interaction.response.send_message(
                embed=embeds.error_embed("BMT System", "That isn't a valid link. Run `/bmt` again and paste the full image link (starting with https://)."),
                ephemeral=True,
            )
        view = StepView(self.cog, interaction.user.id, "Upload Ending Image", EndImageModal, {"starting_image": link})
        await interaction.response.send_message(
            embed=embeds.info_embed("BMT System", "Use the button below to upload your ending image."),
            view=view,
        )


class EndImageModal(discord.ui.Modal, title="BMT System"):
    ending_image = discord.ui.TextInput(
        label="Please enter a link to your ending image",
        style=discord.TextStyle.paragraph,
        placeholder="Only enter the link to the ending image; eg; gyazo",
        required=True,
        max_length=4000,
    )

    def __init__(self, cog: "BMT", state: dict):
        super().__init__()
        self.cog = cog
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        link = _clean_link(self.ending_image.value)
        if not link:
            return await interaction.response.send_message(
                embed=embeds.error_embed("BMT System", "That isn't a valid link. Press the button again and paste the full image link (starting with https://)."),
                ephemeral=True,
            )
        self.state["ending_image"] = link
        view = StepView(self.cog, interaction.user.id, "Select Usernames", UsernamesModal, self.state)
        await _show(interaction, embeds.info_embed("BMT System", "Use the button below to name the users in your BMT."), view)


class UsernamesModal(discord.ui.Modal, title="BMT System"):
    usernames = discord.ui.TextInput(
        label="Please name the users in your BMT",
        style=discord.TextStyle.paragraph,
        placeholder="Username1, Username2, Username3",
        required=True,
        max_length=4000,
    )

    def __init__(self, cog: "BMT", state: dict):
        super().__init__()
        self.cog = cog
        self.state = state

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()  # Roblox lookups can take a while
        raw = str(self.usernames.value).replace("\n", ",").split(",")
        names, seen = [], set()
        for n in raw:
            n = n.strip()
            if n and n.lower() not in seen:  # drop blanks and duplicates
                seen.add(n.lower())
                names.append(n)
        await self.cog.process_bmt_submission(interaction, self.state["starting_image"], self.state["ending_image"], names)


class StepView(discord.ui.View):
    """One button that opens the next modal. Only the instructor can press it."""

    def __init__(self, cog: "BMT", invoker_id: int, label: str, modal_cls, state: dict):
        super().__init__(timeout=600)
        self.cog = cog
        self.invoker_id = invoker_id
        self.modal_cls = modal_cls
        self.state = state

        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        button.callback = self._open_modal
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran `/bmt` can use this.", ephemeral=True)
            return False
        return True

    async def _open_modal(self, interaction: discord.Interaction):
        if self.modal_cls is EndImageModal:
            await interaction.response.send_modal(EndImageModal(self.cog, self.state))
        else:
            await interaction.response.send_modal(UsernamesModal(self.cog, self.state))


# --------------------------------------------------------------------------- #
# Step 4: confirmation
# --------------------------------------------------------------------------- #

class ConfirmBMTView(discord.ui.View):
    def __init__(self, cog: "BMT", starting_image: str, ending_image: str, valid_users: list, invalid_entries: list, invoker_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.starting_image = starting_image
        self.ending_image = ending_image
        self.valid_users = valid_users          # list of (username, roblox_id)
        self.invalid_entries = invalid_entries  # list of (username, reason)
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran `/bmt` can use this.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Submit", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        self.stop()
        await self.cog.submit_request(interaction, self.starting_image, self.ending_image, self.valid_users, self.invalid_entries)


# --------------------------------------------------------------------------- #
# Step 6: Accept / Deny in the logs channel (persistent across restarts)
# --------------------------------------------------------------------------- #

class BMTReviewView(discord.ui.View):
    def __init__(self, cog: "BMT"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Accept Request", style=discord.ButtonStyle.success, custom_id="bmt:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_request(interaction, accept=True)

    @discord.ui.button(label="Deny Request", style=discord.ButtonStyle.danger, custom_id="bmt:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.review_request(interaction, accept=False)


class BMT(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # Re-register the Accept / Deny buttons so old requests still work after a restart.
        self.bot.add_view(BMTReviewView(self))

    @app_commands.command(name="bmt", description="Submit a Basic Military Training graduation request.")
    @require_level(10)
    async def bmt(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StartImageModal(self))

    # ----------------------------------------------------------------------- #
    # Validation + confirmation embed
    # ----------------------------------------------------------------------- #

    async def process_bmt_submission(self, interaction: discord.Interaction, starting_image: str, ending_image: str, raw_names: list):
        guild_config = await db.get_guild_config(interaction.guild.id)
        main_group_id = guild_config.get("main_group_id")
        graduate_rank_id = guild_config.get("bmt_graduate_rank_id")

        if not main_group_id or not graduate_rank_id or not guild_config.get("bmt_logs_channel_id"):
            return await _show(
                interaction,
                embeds.error_embed(
                    "Not Configured",
                    "An admin must set **Main Group ID** (Verification), **BMT Graduate Rank** (Ranking) and the **BMT Logs Channel** in `/setup` before this can be used.",
                ),
            )

        main_group_id = int(main_group_id)
        graduate_rank_id = int(graduate_rank_id)

        valid_users, invalid_entries = [], []
        for name in raw_names:
            roblox_user = await roblox.get_user_by_username(name)
            if not roblox_user:
                invalid_entries.append((name, "Invalid ROBLOX username"))
                continue

            roblox_id = roblox_user["id"]
            rank_id, _ = await roblox.get_user_rank_in_group(roblox_id, main_group_id)

            if not rank_id:
                invalid_entries.append((name, "Wasn't inside of group"))
                continue
            if rank_id >= graduate_rank_id:
                invalid_entries.append((name, "Rank too high to be ranked"))
                continue

            valid_users.append((name, roblox_id))

        lines = [
            "Are you sure you wish to submit the BMT request.",
            "",
            "**Reasons:**",
            "Wasn't inside of group ID",
            "Wasn't a valid username",
            "Wasn't the rank ID required to be ranked",
            "",
            f"**Usernames ({len(valid_users)})**",
        ]
        if valid_users:
            lines.append(_bullets([n for n, _ in valid_users]))
        lines.append(f"**Invalid Usernames ({len(invalid_entries)})**")
        if invalid_entries:
            lines.append(_bullets([f"{n}; ({reason})" for n, reason in invalid_entries]))
        description = "\n".join(lines)

        if not valid_users:
            return await _show(interaction, embeds.error_embed("BMT System", description + "\n\nNo valid users to promote. Run `/bmt` again."))

        view = ConfirmBMTView(self, starting_image, ending_image, valid_users, invalid_entries, interaction.user.id)
        await _show(interaction, embeds.warning_embed("BMT System", description), view)

    # ----------------------------------------------------------------------- #
    # Step 5: post the request to the logs channel
    # ----------------------------------------------------------------------- #

    async def submit_request(self, interaction: discord.Interaction, starting_image: str, ending_image: str, valid_users: list, invalid_entries: list):
        guild_config = await db.get_guild_config(interaction.guild.id)
        channel = None
        if guild_config.get("bmt_logs_channel_id"):
            channel = interaction.guild.get_channel(int(guild_config["bmt_logs_channel_id"]))
        if not channel:
            return await _show(interaction, embeds.error_embed("BMT System", "The BMT logs channel is missing or the bot can't see it. Ask an admin to check `/setup`."))

        log_embed = embeds.base_embed()
        log_embed.title = "BMT System"
        parts = [
            f"**Username of Host:** {interaction.user.mention} | {interaction.user.id} | {interaction.user.display_name}",
            "",
            "**Usernames Requested:**",
            _bullets([n for n, _ in valid_users]),
        ]
        if invalid_entries:
            parts += ["", "**Invalid Usernames:**", _bullets([f"{n}; ({reason})" for n, reason in invalid_entries])]
        parts += ["", f"**Starting Image Link:** {starting_image}", f"**Ending Image Link:** {ending_image}"]
        log_embed.description = "\n".join(parts)
        log_embed.timestamp = discord.utils.utcnow()

        try:
            message = await channel.send(embed=log_embed, view=BMTReviewView(self))
        except discord.HTTPException:
            return await _show(interaction, embeds.error_embed("BMT System", "I couldn't post to the BMT logs channel. Check my permissions there."))

        try:
            await _requests().insert_one({
                "message_id": message.id,
                "channel_id": channel.id,
                "guild_id": interaction.guild.id,
                "host_id": interaction.user.id,
                "valid_users": [{"name": n, "roblox_id": rid} for n, rid in valid_users],
                "invalid_users": [{"name": n, "reason": r} for n, r in invalid_entries],
                "starting_image": starting_image,
                "ending_image": ending_image,
                "status": "pending",
                "created_at": discord.utils.utcnow(),
            })
        except Exception as e:
            print(f"[BMT] failed to save request: {e}")
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return await _show(interaction, embeds.error_embed("BMT System", "Something went wrong saving your request. Nothing was submitted; please try again."))

        await _show(interaction, embeds.success_embed("BMT System", "Your BMT request has been submitted for review."))

    # ----------------------------------------------------------------------- #
    # Step 6: Accept / Deny
    # ----------------------------------------------------------------------- #

    async def review_request(self, interaction: discord.Interaction, accept: bool):
        # NOTE: no rank-level check here — access is limited to whoever can see the
        # (private) BMT logs channel. Add a permission check here if you want one.
        await interaction.response.defer()

        # Atomically claim the request so a double-click can't promote twice.
        doc = await _requests().find_one_and_update(
            {"message_id": interaction.message.id, "guild_id": interaction.guild.id, "status": "pending"},
            {"$set": {"status": "processing", "reviewer_id": interaction.user.id}},
        )
        if not doc:
            return await interaction.followup.send("This request was already handled, or its record could not be found.", ephemeral=True)

        base = interaction.message.embeds[0].copy() if interaction.message.embeds else embeds.base_embed()

        if not accept:
            await _requests().update_one({"_id": doc["_id"]}, {"$set": {"status": "denied"}})
            base.color = discord.Color.red()
            base.add_field(name="Status", value=f"Denied by {interaction.user.mention}", inline=False)
            return await interaction.edit_original_response(embed=base, view=None)

        # Remove the buttons right away while ranks are being changed.
        await interaction.edit_original_response(view=None)

        try:
            guild_config = await db.get_guild_config(interaction.guild.id)
            main_group_id = int(guild_config["main_group_id"])
            graduate_rank_id = int(guild_config["bmt_graduate_rank_id"])

            promoted, failed, skipped = [], [], []
            for entry in doc["valid_users"]:
                name, roblox_id = entry["name"], entry["roblox_id"]
                # Re-check: their rank may have changed since the request was submitted.
                rank_id, _ = await roblox.get_user_rank_in_group(roblox_id, main_group_id)
                if not rank_id or rank_id >= graduate_rank_id:
                    skipped.append(name)
                    continue
                ok = await roblox.set_group_rank(main_group_id, roblox_id, graduate_rank_id, guild_id=interaction.guild.id)
                (promoted if ok else failed).append(name)
                await asyncio.sleep(1)  # don't hammer Roblox's ranking endpoint
        except Exception as e:
            print(f"[BMT] accept failed: {e}")
            # Safe to retry: already-promoted users are skipped on the next attempt.
            await _requests().update_one({"_id": doc["_id"]}, {"$set": {"status": "pending"}})
            await interaction.edit_original_response(view=BMTReviewView(self))
            return await interaction.followup.send("Something went wrong while ranking. The request is back to pending, so you can press Accept again.", ephemeral=True)

        await _requests().update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "accepted", "promoted": promoted, "failed": failed, "skipped": skipped}},
        )

        result = [f"Accepted by {interaction.user.mention}", f"**Promoted ({len(promoted)}):** {', '.join(promoted) or 'None'}"]
        if failed:
            result.append(f"**Failed ({len(failed)}):** {', '.join(failed)}")
        if skipped:
            result.append(f"**Skipped ({len(skipped)}):** {', '.join(skipped)} (rank changed since submission)")
        base.color = discord.Color.green()
        base.add_field(name="Status", value="\n".join(result)[:1024], inline=False)
        await interaction.edit_original_response(embed=base, view=None)


async def setup(bot: commands.Bot):
    await bot.add_cog(BMT(bot))