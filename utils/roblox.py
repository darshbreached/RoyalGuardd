"""
utils/roblox.py
----------------
Thin async wrapper around Roblox's public (unauthenticated) APIs used
for bgcheck, rank syncing, and group lookups.
"""

import os
import json
import aiohttp

USERS_API = "https://users.roblox.com/v1"
GROUPS_API = "https://groups.roblox.com/v1"
THUMBNAILS_API = "https://thumbnails.roblox.com/v1"
FRIENDS_API = "https://friends.roblox.com/v1"
PREMIUM_API = "https://premiumfeatures.roblox.com/v1"


class RobloxAPIError(Exception):
    """Raised when a Roblox API call fails (non-200) in a context where a
    silent fallback would be dangerous - specifically group/rank lookups,
    where treating 'API failed' the same as 'user is not in this group'
    causes false de-ranks. Callers of get_user_rank_in_group /
    get_user_groups must handle this explicitly rather than let a failure
    look identical to 'not in the group'."""
    pass


async def _get_json(session: aiohttp.ClientSession, url: str, **kwargs):
    async with session.get(url, **kwargs) as resp:
        if resp.status != 200:
            return None
        return await resp.json()


async def get_user_by_id(roblox_id: int):
    async with aiohttp.ClientSession() as session:
        return await _get_json(session, f"{USERS_API}/users/{roblox_id}")


async def get_user_by_username(username: str):
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{USERS_API}/usernames/users",
            json={"usernames": [username], "excludeBannedUsers": False},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("data", [])
            return results[0] if results else None


async def get_avatar_headshot_url(roblox_id: int, size: str = "420x420"):
    async with aiohttp.ClientSession() as session:
        data = await _get_json(
            session,
            f"{THUMBNAILS_API}/users/avatar-headshot",
            params={"userIds": roblox_id, "size": size, "format": "png", "isCircular": "false"},
        )
        if data and data.get("data"):
            return data["data"][0].get("imageUrl")
        return None


async def get_friends_count(roblox_id: int):
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{FRIENDS_API}/users/{roblox_id}/friends/count")
        return data.get("count", 0) if data else 0


async def get_followers_count(roblox_id: int):
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{FRIENDS_API}/users/{roblox_id}/followers/count")
        return data.get("count", 0) if data else 0


async def get_following_count(roblox_id: int):
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{FRIENDS_API}/users/{roblox_id}/followings/count")
        return data.get("count", 0) if data else 0


async def get_premium_status(roblox_id: int):
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{PREMIUM_API}/users/{roblox_id}/validate-membership") as resp:
            if resp.status != 200:
                return False
            try:
                return bool(await resp.json())
            except Exception:
                return False


async def get_user_groups(roblox_id: int):
    """Raises RobloxAPIError on a failed request instead of returning an
    empty list, so callers checking group membership never mistake 'Roblox's
    API is down/rate-limited right now' for 'this user is in zero groups' -
    that false equivalence caused mass false de-ranks in sync_member_roles."""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{GROUPS_API}/users/{roblox_id}/groups/roles") as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RobloxAPIError(f"Roblox groups API returned {resp.status} for user {roblox_id}: {body}")
            data = await resp.json()
            return data.get("data", [])


async def get_group_info(group_id: int):
    async with aiohttp.ClientSession() as session:
        return await _get_json(session, f"{GROUPS_API}/groups/{group_id}")


async def get_group_roles(group_id: int):
    async with aiohttp.ClientSession() as session:
        data = await _get_json(session, f"{GROUPS_API}/groups/{group_id}/roles")
        return data.get("roles", []) if data else []


async def get_user_rank_in_group(roblox_id: int, group_id: int):
    """Raises RobloxAPIError (propagated from get_user_groups) if the lookup
    fails - does NOT default to rank 0/Guest on failure, since that used to
    be indistinguishable from a genuine 'not in this group' result."""
    groups = await get_user_groups(roblox_id)
    for entry in groups:
        if str(entry["group"]["id"]) == str(group_id):
            return entry["role"]["rank"], entry["role"]["name"]
    return 0, "Guest"


def _get_cookie(guild_config: dict | None):
    cookie = None
    if guild_config:
        cookie = guild_config.get("roblox_cookie")
    if not cookie:
        cookie = os.getenv("ROBLOX_SECURITY_COOKIE")
    return cookie


def _extract_error_message(body: str) -> str:
    try:
        data = json.loads(body)
        errors = data.get("errors") or []
        if errors:
            return errors[0].get("userFacingMessage") or errors[0].get("message") or body
    except (json.JSONDecodeError, AttributeError):
        pass
    return body or "Unknown error from Roblox."


# ============================================================
# AUTHENTICATED GROUP JOIN-REQUEST HANDLING (service account required)
# ============================================================
async def accept_group_join_request(group_id: int, roblox_user_id: int, guild_id: int = None):
    """Accepts a pending join request for roblox_user_id in group_id, using
    the same per-guild-cookie-then-env-var-fallback pattern as
    set_group_rank(). This must run BEFORE set_group_rank() for anyone who
    is still in the group's "Requests" tab rather than already a member -
    Roblox's rank-change endpoint returns a generic "user is invalid or
    does not exist" error for a non-member, which looks identical to a
    genuinely bad user ID unless you know to check the Requests tab.

    NOTE: this endpoint/flow has not been confirmed against a live Roblox
    response the way set_group_rank's has (that one has verified debug
    logging from production use). If this raises or behaves unexpectedly,
    capture the printed [ACCEPTJOIN DEBUG] status/body and it can be fixed
    from the real response shape.

    Returns True if a pending request was found and accepted. Returns
    False (does NOT raise) if there was no pending request for this user -
    that's an expected, non-error case: it just means they're already a
    member (or never requested), so the caller should proceed straight to
    set_group_rank() either way.

    Raises RuntimeError only for an unexpected failure (bad/expired
    cookie, permissions issue, etc.) - the caller should treat that the
    same as a set_group_rank() RuntimeError and show it to the user.
    """
    guild_config = None
    if guild_id is not None:
        from database.mongodb import db
        guild_config = await db.get_guild_config(guild_id)

    cookie = _get_cookie(guild_config)
    if not cookie:
        raise RuntimeError("ROBLOX_SECURITY_COOKIE not configured - cannot accept join requests.")

    cookies = {".ROBLOSECURITY": cookie}
    url = f"{GROUPS_API}/groups/{group_id}/join-requests/users/{roblox_user_id}"

    async with aiohttp.ClientSession(cookies=cookies) as session:
        async with session.post(url) as resp:
            body = await resp.text()
            print(f"[ACCEPTJOIN DEBUG] first post status={resp.status} body={body}")
            csrf_token = None
            if resp.status == 403:
                csrf_token = resp.headers.get("x-csrf-token")
                print(f"[ACCEPTJOIN DEBUG] csrf_token_received={bool(csrf_token)}")
            elif resp.status in (200, 204):
                return True
            elif resp.status == 400:
                # No pending join request for this user - not an error,
                # just means they're already a member or never requested.
                return False
            else:
                raise RuntimeError(_extract_error_message(body))

        headers = {"x-csrf-token": csrf_token} if csrf_token else {}
        async with session.post(url, headers=headers) as resp:
            body = await resp.text()
            print(f"[ACCEPTJOIN DEBUG] second post status={resp.status} body={body}")
            if resp.status in (200, 204):
                return True
            if resp.status == 400:
                return False
            raise RuntimeError(_extract_error_message(body))


# ============================================================
# AUTHENTICATED GROUP RANKING (service account required)
# ============================================================
async def set_group_rank(group_id: int, roblox_user_id: int, role_id: int, guild_id: int = None):
    """Sets a member's rank in a group using a service account cookie.

    Checks the per-guild cookie configured via /setup (Ranking > ROBLOX Cookie)
    first, so each tenant server can rank with its own Roblox service account.
    Falls back to the global ROBLOX_SECURITY_COOKIE env var if the guild hasn't
    configured one, or if guild_id isn't passed at all - this keeps the main
    bot working with zero config changes needed.

    Roblox requires a fresh X-CSRF-TOKEN per request, obtained from a 403
    response header.

    Raises RuntimeError with Roblox's actual error message on failure, so
    callers can show the real reason instead of a generic "check your cookie"
    message. NOTE: Roblox returns this same generic error for a user who is
    not yet a member of the group (still a pending join request) - call
    accept_group_join_request() first if that might be the case.
    """
    guild_config = None
    if guild_id is not None:
        from database.mongodb import db
        guild_config = await db.get_guild_config(guild_id)

    cookie = _get_cookie(guild_config)

    if not cookie:
        print(f"[SETRANK DEBUG] No cookie found (guild_id={guild_id}, checked per-guild config and env var).")
        raise RuntimeError("ROBLOX_SECURITY_COOKIE not configured - cannot rank users.")

    cookies = {".ROBLOSECURITY": cookie}
    url = f"{GROUPS_API}/groups/{group_id}/users/{roblox_user_id}"

    print(f"[SETRANK DEBUG] cookie_length={len(cookie)} url={url} role_id={role_id} guild_id={guild_id}")

    async with aiohttp.ClientSession(cookies=cookies) as session:
        async with session.patch(url, json={"roleId": role_id}) as resp:
            body = await resp.text()
            print(f"[SETRANK DEBUG] first patch status={resp.status} body={body}")
            if resp.status == 403:
                csrf_token = resp.headers.get("x-csrf-token")
                print(f"[SETRANK DEBUG] csrf_token_received={bool(csrf_token)}")
            elif resp.status == 200:
                return True
            else:
                raise RuntimeError(_extract_error_message(body))

        headers = {"x-csrf-token": csrf_token} if csrf_token else {}
        async with session.patch(url, json={"roleId": role_id}, headers=headers) as resp:
            body = await resp.text()
            print(f"[SETRANK DEBUG] second patch status={resp.status} body={body}")
            if resp.status == 200:
                return True
            raise RuntimeError(_extract_error_message(body))
