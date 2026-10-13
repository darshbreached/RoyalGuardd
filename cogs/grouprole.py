"""
cogs/grouprole.py
------------------
Turn Roblox group role permissions on or off from Discord.

/grouprole view group_id role
    Read-only: shows what a role can currently do (and the exact names Roblox uses).

/grouprole permission group_id role permission [enabled]
    Turns ONE permission on (default) or off for ONE role.

Safety rules (all enforced in code, edit the constants below to change them):
  * needs admin level GROUPROLE_MIN_LEVEL
  * only works on groups already bound to this server with /groupbind
  * uses this server's own Roblox cookie (/setup -> Ranking -> ROBLOX Cookie). The
    shared ROBLOX_SECURITY_COOKIE is only used when the bot owner runs it - otherwise a
    server could bind someone else's group and edit it through the shared account
  * permissions are in three tiers (see PERMISSIONS): SAFE are switched directly,
    SENSITIVE ask for a confirmation button before being turned ON, BLOCKED
    (spending funds, API keys, games, avatar items...) cannot be changed from Discord
  * the Guest and Owner roles are never touched
  * every change is re-read from Roblox afterwards, so "success" means it really changed

Roblox only lets the group OWNER (or an account with role-management rights) edit role
permissions, so the cookie's account usually has to own the group. If Roblox refuses,
the command says so. Failures are logged as [GROUPROLE DEBUG] lines (never the cookie).
"""

import json
import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from database.mongodb import db
from utils import embeds, roblox
from utils.permissions import require_level

GROUPROLE_MIN_LEVEL = 50
CONFIRM_TIMEOUT = 60
TIMEOUT = aiohttp.ClientTimeout(total=15)
LOG_CHANNEL_KIND = "ranking"  # which /setup log channel gets the audit embed (skipped if unset)

SAFE, SENSITIVE, BLOCKED = "safe", "sensitive", "blocked"

# key (as Roblox names it) -> (label, category, tier)
PERMISSIONS = {
    "viewWall":              ("View group wall", "Posts", SAFE),
    "postToWall":            ("Post on the group wall", "Posts", SAFE),
    "deleteFromWall":        ("Delete wall posts", "Posts", SAFE),
    "viewStatus":            ("View the group shout", "Posts", SAFE),
    "postToStatus":          ("Post the group shout", "Posts", SAFE),
    "viewAuditLogs":         ("View the audit log", "Management", SAFE),
    "inviteMembers":         ("Accept join requests / invite members", "Membership", SAFE),
    "changeRank":            ("Change member ranks", "Membership", SENSITIVE),
    "removeMembers":         ("Remove (exile) members", "Membership", SENSITIVE),
    "manageRelationships":   ("Manage allies and enemies", "Management", SENSITIVE),
    "manageClan":            ("Manage the clan", "Management", SENSITIVE),
    "spendGroupFunds":       ("Spend group funds", "Economy", BLOCKED),
    "advertiseGroup":        ("Advertise the group (costs Robux)", "Economy", BLOCKED),
    "createItems":           ("Create items", "Economy", BLOCKED),
    "manageItems":           ("Manage items", "Economy", BLOCKED),
    "addGroupPlaces":        ("Add group places", "Economy", BLOCKED),
    "manageGroupGames":      ("Manage group games", "Economy", BLOCKED),
    "viewGroupPayouts":      ("View group payouts", "Economy", BLOCKED),
    "useCloudAuthentication":        ("Use API keys", "Open Cloud", BLOCKED),
    "administerCloudAuthentication": ("Manage API keys", "Open Cloud", BLOCKED),
}
CATEGORY_ORDER = ["Posts", "Membership", "Management", "Economy", "Open Cloud"]

PERMISSION_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, (label, _cat, tier) in PERMISSIONS.items()
    if tier != BLOCKED
]

# Roblox's PATCH naming isn't consistent with its GET naming in every place, so each
# permission is tried under a few spellings and every attempt is verified by re-reading.
_ALIASES = {"viewAuditLogs": ["ViewAuditLog", "ViewAuditLogs", "viewAuditLogs", "viewAuditLog"]}


def _debug(message: str):
    print(f"[GROUPROLE DEBUG] {message}")


def norm(name: str) -> str:
    """Case- and plural-insensitive key so viewAuditLog == ViewAuditLogs."""
    name = (name or "").lower()
    return name[:-1] if name.endswith("s") else name


def candidate_keys(key: str) -> list:
    if key in _ALIASES:
        return list(_ALIASES[key])
    return [key[:1].upper() + key[1:], key]


def flatten_permissions(data) -> dict:
    """Roblox returns permissions grouped by category; return one {name: bool} dict."""
    perms = data.get("permissions") if isinstance(data, dict) else None
    flat = {}
    if isinstance(perms, dict):
        for name, value in perms.items():
            if isinstance(value, dict):
                for inner_name, inner_value in value.items():
                    if isinstance(inner_value, bool):
                        flat[inner_name] = inner_value
            elif isinstance(value, bool):
                flat[name] = value
    return flat


def lookup(flat: dict, key: str):
    """The bool for `key` in a flat permissions dict (tolerant of naming differences), or None."""
    wanted = norm(key)
    for name, value in flat.items():
        if norm(name) == wanted:
            return value
    return None


def _error_code(body: str):
    try:
        errors = (json.loads(body) or {}).get("errors") or []
        return errors[0].get("code") if errors else None
    except (ValueError, AttributeError, IndexError):
        return None


async def _request(session: aiohttp.ClientSession, state: dict, method: str, url: str, **kwargs):
    """One request with Roblox's X-CSRF-TOKEN handshake. Returns (status, body)."""
    for attempt in range(2):
        headers = {"x-csrf-token": state["csrf"]} if state.get("csrf") else {}
        async with session.request(method, url, headers=headers, **kwargs) as resp:
            body = await resp.text()
            if resp.status == 403 and method != "GET" and attempt == 0 and resp.headers.get("x-csrf-token"):
                state["csrf"] = resp.headers["x-csrf-token"]
                continue
            return resp.status, body
    return 403, ""


def explain_failure(status: int, body: str) -> str:
    code = _error_code(body)
    if code == 3 or status == 403:
        return ("Roblox says this account isn't allowed to change that role's permissions. "
                "Usually the cookie's account has to be the group **owner**.")
    if code == 4:
        return "Roblox says that role's permissions can't be modified (for example the owner role)."
    if status == 401:
        return "Roblox rejected the cookie (it may have expired). Update it in `/setup` and try again."
    if status == 429:
        return "Roblox is rate limiting the bot right now. Try again in a minute."
    return roblox._extract_error_message(body) if body else f"Roblox returned HTTP {status}."


async def fetch_role_permissions(cookie: str, group_id: int, role_id: int):
    """(flat_permissions or None, error_message or None)."""
    url = f"{roblox.GROUPS_API}/groups/{group_id}/roles/{role_id}/permissions"
    try:
        async with aiohttp.ClientSession(cookies={".ROBLOSECURITY": cookie}, timeout=TIMEOUT) as session:
            status, body = await _request(session, {}, "GET", url)
    except (aiohttp.ClientError, TimeoutError) as e:
        _debug(f"GET permissions failed: {type(e).__name__}")
        return None, "Couldn't reach Roblox. Try again in a moment."
    if status != 200:
        _debug(f"GET {url} -> HTTP {status}: {body[:300]}")
        return None, explain_failure(status, body)
    try:
        flat = flatten_permissions(json.loads(body))
    except ValueError:
        return None, "Roblox sent back something I couldn't read."
    if not flat:
        _debug(f"GET {url} -> 200 but no permissions found in: {body[:300]}")
        return None, "Roblox didn't return any permissions for that role."
    return flat, None


async def set_role_permission(cookie: str, group_id: int, role_id: int, key: str, value: bool):
    """Turn one permission on/off and VERIFY it. Returns (ok, message)."""
    url = f"{roblox.GROUPS_API}/groups/{group_id}/roles/{role_id}/permissions"
    state = {}
    last_problem = None
    try:
        async with aiohttp.ClientSession(cookies={".ROBLOSECURITY": cookie}, timeout=TIMEOUT) as session:
            for candidate in candidate_keys(key):
                status, body = await _request(session, state, "PATCH", url, json={"permissions": {candidate: value}})
                _debug(f"PATCH {url} {{{candidate}: {value}}} -> HTTP {status}: {body[:300]}")
                if status != 200:
                    last_problem = explain_failure(status, body)
                    if status in (401, 403, 429) or _error_code(body) in (3, 4):
                        return False, last_problem  # not a naming problem - retrying won't help
                    continue
                check_status, check_body = await _request(session, state, "GET", url)
                if check_status == 200:
                    try:
                        if lookup(flatten_permissions(json.loads(check_body)), key) == value:
                            return True, "ok"
                    except ValueError:
                        pass
                _debug(f"'{candidate}' returned 200 but the permission did not change")
                last_problem = "Roblox accepted the request but the permission didn't actually change."
    except (aiohttp.ClientError, TimeoutError) as e:
        _debug(f"PATCH failed: {type(e).__name__}")
        return False, "Couldn't reach Roblox. Try again in a moment."
    return False, last_problem or "Roblox didn't accept the change."


class ConfirmView(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=CONFIRM_TIMEOUT)
        self.invoker_id = invoker_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Only the person who ran the command can use these buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


class GroupRole(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    grouprole = app_commands.Group(name="grouprole", description="Manage Roblox group role permissions.", guild_only=True)

    # ---- shared checks --------------------------------------------------- #

    async def _context(self, interaction: discord.Interaction, group_id: int, role_id: int):
        """Validate everything common to both commands. Returns a dict, or an error string."""
        guild = interaction.guild
        binds = await db.list_groupbinds(guild.id)
        bound = next((b for b in binds if str(b["group_id"]) == str(group_id)), None)
        if bound is None:
            return "That group isn't bound to this server. Bind it first with `/groupbind add`."

        guild_config = await db.get_guild_config(guild.id) or {}
        owner_id = os.getenv("BOT_OWNER_ID")
        is_bot_owner = bool(owner_id) and str(interaction.user.id) == str(owner_id)
        if not guild_config.get("roblox_cookie") and not is_bot_owner:
            return ("Changing group role permissions needs this server's **own** Roblox service-account cookie "
                    "(`/setup` -> Ranking -> ROBLOX Cookie). The shared bot account is reserved for the bot owner.")
        cookie = roblox._get_cookie(guild_config)
        if not cookie:
            return "No Roblox cookie is configured, so I can't talk to the group."

        try:
            roles = await roblox.get_group_roles(group_id)
        except Exception:
            roles = None
        if not roles:
            return "I couldn't load that group's roles from Roblox."
        role = next((r for r in roles if str(r.get("id")) == str(role_id)), None)
        if role is None:
            return "That role doesn't exist in the group."
        if role.get("rank") == 0:
            return "The Guest role isn't supported."
        if role.get("rank") == 255:
            return "The group owner role's permissions can't be changed."

        return {"cookie": cookie, "group_name": bound.get("group_name") or str(group_id), "role": role}

    async def _log(self, guild: discord.Guild, embed: discord.Embed):
        try:
            channel_id = await db.get_log_channel(guild.id, LOG_CHANNEL_KIND)
            channel = guild.get_channel(int(channel_id)) if channel_id else None
            if channel:
                await channel.send(embed=embed)
        except Exception:
            pass

    # ---- autocomplete ---------------------------------------------------- #

    async def _group_choices(self, interaction: discord.Interaction, current: str):
        try:
            binds = await db.list_groupbinds(interaction.guild.id)
        except Exception:
            return []
        cur = (current or "").lower()
        out = []
        for b in binds:
            name = b.get("group_name") or "Group"
            if cur in name.lower() or cur in str(b["group_id"]):
                out.append(app_commands.Choice(name=f"{name} ({b['group_id']})"[:100], value=int(b["group_id"])))
        return out[:25]

    async def _role_choices(self, interaction: discord.Interaction, current: str):
        try:
            group_id = int(interaction.namespace.group_id)
            roles = await roblox.get_group_roles(group_id)
        except Exception:
            return []
        cur = (current or "").lower()
        out = []
        for r in roles or []:
            if r.get("rank") in (0, 255) or cur not in r.get("name", "").lower():
                continue
            out.append(app_commands.Choice(name=f"{r['name']} (Rank {r['rank']})"[:100], value=int(r["id"])))
        return out[:25]

    # ---- /grouprole view ------------------------------------------------- #

    @grouprole.command(name="view", description="Show what a group role is allowed to do.")
    @app_commands.describe(group_id="A Roblox group bound to this server", role="The group role to look at")
    @require_level(GROUPROLE_MIN_LEVEL)
    async def view(self, interaction: discord.Interaction, group_id: int, role: int):
        await interaction.response.defer(ephemeral=True)
        ctx = await self._context(interaction, group_id, role)
        if isinstance(ctx, str):
            return await interaction.followup.send(embed=embeds.error_embed("Can't Do That", ctx), ephemeral=True)

        flat, problem = await fetch_role_permissions(ctx["cookie"], group_id, role)
        if flat is None:
            return await interaction.followup.send(embed=embeds.error_embed("Couldn't Read Permissions", problem), ephemeral=True)

        tier_note = {SAFE: "", SENSITIVE: " *(asks to confirm)*", BLOCKED: " *(locked)*"}
        known = set()
        lines = []
        for category in CATEGORY_ORDER:
            rows = []
            for key, (label, cat, tier) in PERMISSIONS.items():
                value = lookup(flat, key)
                if cat != category or value is None:
                    continue
                known.add(norm(key))
                rows.append(f"`{'ON ' if value else 'OFF'}` {label}{tier_note[tier]}")
            if rows:
                lines.append(f"**{category}**\n" + "\n".join(rows))
        extras = sorted(n for n in flat if norm(n) not in known)
        if extras:
            lines.append("**Other (not managed by this command)**\n" + ", ".join(f"`{n}` {'ON' if flat[n] else 'OFF'}" for n in extras))

        title = f"{ctx['role'].get('name')} - {ctx['group_name']}"
        await interaction.followup.send(embed=embeds.info_embed(title, "\n\n".join(lines)[:4000]), ephemeral=True)

    @view.autocomplete("group_id")
    async def _view_group_ac(self, interaction: discord.Interaction, current: str):
        return await self._group_choices(interaction, current)

    @view.autocomplete("role")
    async def _view_role_ac(self, interaction: discord.Interaction, current: str):
        return await self._role_choices(interaction, current)

    # ---- /grouprole permission ------------------------------------------- #

    @grouprole.command(name="permission", description="Turn one permission on or off for a group role.")
    @app_commands.describe(
        group_id="A Roblox group bound to this server",
        role="The group role to change",
        permission="Which permission",
        enabled="On (default) or off",
    )
    @app_commands.choices(permission=PERMISSION_CHOICES)
    @require_level(GROUPROLE_MIN_LEVEL)
    async def permission(self, interaction: discord.Interaction, group_id: int, role: int,
                         permission: app_commands.Choice[str], enabled: bool = True):
        key = permission.value
        meta = PERMISSIONS.get(key)
        if meta is None or meta[2] == BLOCKED:
            return await interaction.response.send_message(
                embed=embeds.error_embed("Locked", "That permission can't be changed from Discord. Change it on Roblox directly."),
                ephemeral=True,
            )
        label, _category, tier = meta

        await interaction.response.defer()  # public: the reply is the record of the change
        ctx = await self._context(interaction, group_id, role)
        if isinstance(ctx, str):
            return await interaction.edit_original_response(embed=embeds.error_embed("Can't Do That", ctx))
        role_name, group_name = ctx["role"].get("name"), ctx["group_name"]

        if enabled and tier == SENSITIVE:
            view = ConfirmView(interaction.user.id)
            await interaction.edit_original_response(
                embed=embeds.warning_embed(
                    "Confirm Permission Change",
                    f"Turn **ON** `{label}` for **{role_name}** in **{group_name}**?\n\n"
                    f"Everyone holding that role gets this power in the Roblox group immediately.",
                ),
                view=view,
            )
            await view.wait()
            if view.value is not True:
                reason = "Cancelled." if view.value is False else "Timed out."
                return await interaction.edit_original_response(embed=embeds.info_embed("Nothing Changed", reason), view=None)

        ok, message = await set_role_permission(ctx["cookie"], group_id, role, key, enabled)
        if not ok:
            return await interaction.edit_original_response(embed=embeds.error_embed("Permission Not Changed", message), view=None)

        state = "ON" if enabled else "OFF"
        done = embeds.success_embed("Permission Updated", f"`{label}` is now **{state}** for **{role_name}** in **{group_name}**.")
        done.add_field(name="Changed by", value=interaction.user.mention, inline=False)
        await interaction.edit_original_response(embed=done, view=None)
        await self._log(interaction.guild, done)

    @permission.autocomplete("group_id")
    async def _perm_group_ac(self, interaction: discord.Interaction, current: str):
        return await self._group_choices(interaction, current)

    @permission.autocomplete("role")
    async def _perm_role_ac(self, interaction: discord.Interaction, current: str):
        return await self._role_choices(interaction, current)


async def setup(bot: commands.Bot):
    await bot.add_cog(GroupRole(bot))
