import discord


class VerificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify via ROBLOX Login",
        style=discord.ButtonStyle.success,
        custom_id="verify_login"
    )
    async def verify_login(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        embed = discord.Embed(
            title="Verification",
            description=(
                "Press the button below to start verification."
            ),
            color=0x206694
        )

        view = discord.ui.View()

        view.add_item(
            discord.ui.Button(
                label="Open Verification",
                url="https://YOUR-RAILWAY-DOMAIN.up.railway.app/verify"
            )
        )

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True
        )

    @discord.ui.button(
        label="Verify via ROBLOX Game",
        style=discord.ButtonStyle.success,
        custom_id="verify_game"
    )
    async def verify_game(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Game verification coming soon.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Update Roles",
        style=discord.ButtonStyle.success,
        custom_id="update_roles"
    )
    async def update_roles(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await interaction.response.send_message(
            "Role update started.",
            ephemeral=True
        )