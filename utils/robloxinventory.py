"""
utils/robloxinventory.py
-------------------------
Badge and gamepass counts for the !bgcheck embed.

Badges      - Roblox's public badges API (no key needed).
Gamepasses  - Roblox Open Cloud inventory API. Needs an API key with inventory
              read access in the ROBLOX_INVENTORY_API_KEY environment variable;
              without it the gamepass line shows "Unavailable".

Both only work when the user's inventory is public, so a hidden inventory is
reported as "Private". Nothing in here raises: on any failure the embed simply
shows "Unavailable" for that line, so !bgcheck never breaks because of it.

Counts are capped at MAX_PAGES * PAGE_SIZE (shown as "1000+") and each count
stops after TIME_BUDGET seconds (shown as "N+"), so a huge account can't make
the command hang.
"""

import asyncio
import os
import time

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=8)
PAGE_SIZE = 100
MAX_PAGES = 10
TIME_BUDGET = 10.0

BADGES_URL = "https://badges.roblox.com/v1/users/{uid}/badges"
CAN_VIEW_URL = "https://inventory.roblox.com/v1/users/{uid}/can-view-inventory"
INVENTORY_URL = "https://apis.roblox.com/cloud/v2/users/{uid}/inventory-items"

_warned_missing_key = False


def _debug(message: str):
    print(f"[BGCHECK DEBUG] {message}")


def _ok(count: int, capped: bool = False) -> dict:
    return {"status": "ok", "count": count, "capped": capped}


def _partial_or_unavailable(total: int) -> dict:
    """A page failed part-way: show what we counted so far as 'N+', or Unavailable if nothing."""
    return _ok(total, True) if total else {"status": "unavailable"}


def format_result(result) -> str:
    status = (result or {}).get("status")
    if status == "ok":
        return f"{result['count']}+" if result.get("capped") else str(result["count"])
    if status == "private":
        return "Private (inventory is hidden)."
    return "Unavailable."


async def _can_view_inventory(session: aiohttp.ClientSession, uid: int):
    """True / False, or None if Roblox didn't tell us."""
    try:
        async with session.get(CAN_VIEW_URL.format(uid=uid)) as resp:
            if resp.status != 200:
                _debug(f"can-view-inventory -> HTTP {resp.status}")
                return None
            data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
        _debug(f"can-view-inventory failed: {type(e).__name__}")
        return None
    can = data.get("canView") if isinstance(data, dict) else None
    return can if isinstance(can, bool) else None


async def _count_badges(session: aiohttp.ClientSession, uid: int) -> dict:
    deadline = time.monotonic() + TIME_BUDGET
    total, cursor, pages = 0, None, 0
    while True:
        params = {"limit": PAGE_SIZE, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        try:
            async with session.get(BADGES_URL.format(uid=uid), params=params) as resp:
                if resp.status != 200:
                    _debug(f"badges page {pages + 1} -> HTTP {resp.status}")
                    return _partial_or_unavailable(total)
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            _debug(f"badges page {pages + 1} failed: {type(e).__name__}")
            return _partial_or_unavailable(total)

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            _debug("badges response had no 'data' list")
            return _partial_or_unavailable(total)

        total += len(items)
        pages += 1
        cursor = data.get("nextPageCursor")
        if not cursor:
            return _ok(total)
        if pages >= MAX_PAGES or time.monotonic() > deadline:
            return _ok(total, True)


def _first_list(data: dict):
    for key in ("inventoryItems", "data", "items"):
        if isinstance(data.get(key), list):
            return data[key]
    for value in data.values():
        if isinstance(value, list):
            return value
    return []  # a 200 with no list at all means "nothing owned"


async def _count_gamepasses(session: aiohttp.ClientSession, uid: int, api_key: str) -> dict:
    deadline = time.monotonic() + TIME_BUDGET
    total, token, pages, size = 0, None, 0, PAGE_SIZE
    headers = {"x-api-key": api_key}
    while True:
        params = {"filter": "gamePasses=true"}
        if size:
            params["maxPageSize"] = size
        if token:
            params["pageToken"] = token
        try:
            async with session.get(INVENTORY_URL.format(uid=uid), params=params, headers=headers) as resp:
                if resp.status == 400 and pages == 0 and size:
                    size = None  # that page size isn't accepted: retry once with Roblox's default
                    continue
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    _debug(f"gamepasses -> HTTP {resp.status}: {body}")
                    return _partial_or_unavailable(total)
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            _debug(f"gamepasses failed: {type(e).__name__}")
            return _partial_or_unavailable(total)

        if not isinstance(data, dict):
            _debug("gamepasses response wasn't a JSON object")
            return _partial_or_unavailable(total)

        total += len(_first_list(data))
        pages += 1
        token = data.get("nextPageToken")
        if not token:
            return _ok(total)
        if pages >= MAX_PAGES or time.monotonic() > deadline:
            return _ok(total, True)


async def get_inventory_counts(roblox_id) -> dict:
    """{'badges': result, 'gamepasses': result}. Never raises."""
    global _warned_missing_key
    unavailable = {"status": "unavailable"}
    try:
        uid = int(roblox_id)
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            if await _can_view_inventory(session, uid) is False:
                return {"badges": {"status": "private"}, "gamepasses": {"status": "private"}}

            api_key = os.getenv("ROBLOX_INVENTORY_API_KEY")
            if api_key:
                badges, gamepasses = await asyncio.gather(
                    _count_badges(session, uid), _count_gamepasses(session, uid, api_key)
                )
            else:
                if not _warned_missing_key:
                    _warned_missing_key = True
                    _debug("ROBLOX_INVENTORY_API_KEY is not set - gamepass counts will show 'Unavailable'.")
                badges, gamepasses = await _count_badges(session, uid), unavailable
            return {"badges": badges, "gamepasses": gamepasses}
    except Exception as e:
        _debug(f"get_inventory_counts crashed: {type(e).__name__}: {e}")
        return {"badges": unavailable, "gamepasses": unavailable}
