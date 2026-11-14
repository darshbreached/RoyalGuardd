"""
utils/modules.py
Single source of truth for every toggleable feature (module).
The website reads this to build the dashboard; the bot will read it
to enforce per-server enable/disable.
"""

CATEGORIES = [
    "Verification & Ranks",
    "Moderation & Security",
    "Community",
    "Operations",
]

MODULES = []


def _m(key, name, icon, category, desc, core=False, hidden=False):
    MODULES.append({
        "key": key, "name": name, "icon": icon, "category": category,
        "desc": desc, "core": core, "hidden": hidden,
        "cog": f"cogs.{key}",
    })


# Verification & Ranks
_m("verification", "Verification", "🛡️", "Verification & Ranks", "Roblox verification via login, game or ticket, with account-age and alt checks.")
_m("update", "Role Sync", "🔄", "Verification & Ranks", "One command syncs Discord roles with Roblox group ranks.")
_m("rankbinds", "Rank Binds", "🎖️", "Verification & Ranks", "Bind Discord roles to specific Roblox group ranks.")
_m("groupbinds", "Group Binds", "🧩", "Verification & Ranks", "Bind Discord roles to a whole Roblox group at any rank.")
_m("setrank", "Set Rank", "📈", "Verification & Ranks", "Change Roblox ranks from Discord with clean logged embeds.")
_m("rankrequest", "Rank Requests", "📝", "Verification & Ranks", "Members request ranks with proof; staff approve in one click.")
_m("acceptrequest", "Accept Requests", "✅", "Verification & Ranks", "Accept applicants and assign the right starting rank.")
_m("bgcheck", "Background Checks", "🔎", "Verification & Ranks", "Paginated background checks against your configured group lists.")

# Moderation & Security
_m("moderation", "Moderation", "🔨", "Moderation & Security", "Ban, mute and kick with DM notices and appeal links.")
_m("warnings", "Warnings", "⚠️", "Moderation & Security", "Warn members, view history and clear warnings.")
_m("automod", "Automod", "🤖", "Moderation & Security", "Automatic filtering for spam and unwanted content.")
_m("antinuke", "Anti-Nuke", "🧨", "Moderation & Security", "Protection against mass channel, role and ban attacks.")
_m("antiraid", "Anti-Raid", "🚧", "Moderation & Security", "Detects and slows down join floods.")
_m("globalban", "Global Bans", "🌐", "Moderation & Security", "Opt-in shared ban list across servers you choose.")

# Community
_m("tickets", "Tickets", "🎫", "Community", "Report and support tickets in private channels.")
_m("reactionroles", "Reaction Roles", "🎭", "Community", "Members self-assign roles from reactions.")
_m("invites", "Invite Tracking", "📨", "Community", "See who invited whom.")
_m("economy", "Economy", "🪙", "Community", "Per-server currency with daily, work, pay and games.")
_m("boosterroles", "Booster Colours", "💎", "Community", "A private colour-role picker for server boosters.")
_m("dmannounce", "DM Announce", "📣", "Community", "Send announcements to members by DM.")

# Operations
_m("activitycheck", "Activity Checks", "📋", "Operations", "Run activity checks and track who responds.")
_m("ssu", "SSU Announcements", "🚀", "Operations", "Server start-up announcements for your game.")
_m("bmt", "Basic Training", "🪖", "Operations", "A guided training request flow for new recruits.")
_m("loa", "Leave of Absence", "🏖️", "Operations", "Leave requests with tracking.")

# Core: always on, never shown as toggles
for _k in ("adminlevels", "setup", "panels", "errorhandler", "devtools"):
    _m(_k, _k.title(), "⚙️", "Core", "Core system.", core=True, hidden=True)

BY_KEY = {m["key"]: m for m in MODULES}
COG_TO_KEY = {m["cog"]: m["key"] for m in MODULES}
