"""
config/settings.py
-------------------
Central branding and styling configuration for the Royal Guard bot.
"""

import os

BOT_NAME = "Royal Guard Services ©"
BOT_TAG = "British Army Verification System"
BOT_VERSION = "All Rights Reserved."

BOT_ICON_URL = os.getenv(
    "BOT_ICON_URL",
    "https://i.imgur.com/ILUPJLN.png"
)

EMBED_COLOR = 0x87CEEB
SUCCESS_COLOR = 0x57A05A
ERROR_COLOR = 0x800000
WARNING_COLOR = 0xFFD700
INFO_COLOR = 0x3498DB

FOOTER_TEXT = f"{BOT_NAME} {BOT_VERSION}"
FOOTER_ICON = BOT_ICON_URL

AUTHOR_TEXT = BOT_NAME
AUTHOR_ICON = BOT_ICON_URL

VERIFICATION_PANEL_TITLE = "CANADIAN ARMY VERIFICATION SYSTEM V2"
VERIFICATION_PANEL_DESCRIPTION = (
    "Press the **Verify / Reverify** button to verify or reverify your ROBLOX account."
)

TICKET_PANEL_TITLE = "ROYAL GUARD SUPPORT"
TICKET_PANEL_DESCRIPTION = (
    "Select a category below to open a ticket with the appropriate team.\n\n"
    "Please only open a ticket for genuine matters. Abuse of the ticket system "
    "may result in disciplinary action."
)

REPORT_TICKET_OPTIONS = [
    ("Report High Rank", "report_high_rank", "🎖️"),
    ("Report Exploiter", "report_exploiter", "🛑"),
    ("Report Corruption", "report_corruption", "⚖️"),
    ("Report Abuser", "report_abuser", "🚫"),
    ("Report Rule Breaker", "report_rulebreaker", "📋"),
]

OTHER_TICKET_OPTIONS = [
    ("Report Bug / Glitch", "report_bug", "🐛"),
    ("Report Exploit Script", "report_exploit_script", "💻"),
    ("Developer Application", "dev_application", "🧑‍💻"),
    ("Alliance Application", "alliance_application", "🤝"),
]

OWNER_LEVEL = 999999
UPDATEALL_MIN_LEVEL = 10

COMMAND_PREFIX = "!"