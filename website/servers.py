"""
website/servers.py
-------------------
The regiment server directory shown on the home page.

Your list lives in website/servers.json (edit it with
`python3 -m website.add_server`). For each entry this module asks Discord's
public invite endpoint for the server's name, icon and member counts, caches
the answer for 10 minutes, and falls back to whatever is in servers.json if
Discord can't be reached - so the page always renders.

Only the invite *code* is ever put into a URL, and only after it has been
checked against a strict pattern, so a bad entry can't make the server fetch
anything else.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

log = logging.getLogger("RoyalGuard")

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "servers.json")
CACHE_TTL = 600          # seconds an invite lookup is reused
FAIL_BACKOFF = 60        # seconds before retrying an invite whose lookup failed
FETCH_TIMEOUT = 4

_CODE_RE = re.compile(r"^[A-Za-z0-9-]{2,32}$")
_HOSTS = {"discord.gg", "www.discord.gg", "discord.com", "www.discord.com", "discordapp.com", "www.discordapp.com"}

_cache = {}     # code -> (fetched_at, info)
_backoff = {}   # code -> retry_after_timestamp


def parse_invite_code(value):
    """'https://discord.gg/abc', 'discord.com/invite/abc' or 'abc' -> 'abc'. None if it isn't one."""
    value = (value or "").strip() if isinstance(value, str) else ""
    if not value:
        return None
    if _CODE_RE.match(value):
        return value
    try:
        url = urlparse(value if "//" in value else "https://" + value)
        host = (url.hostname or "").lower()
    except ValueError:
        return None
    if host not in _HOSTS:
        return None
    parts = [p for p in url.path.split("/") if p]
    if host.endswith("discord.gg"):
        code = parts[0] if parts else ""
    elif len(parts) >= 2 and parts[0] == "invite":
        code = parts[1]
    else:
        return None
    return code if _CODE_RE.match(code) else None


def fetch_invite(code):
    """Ask Discord about an invite.

    Returns {"ok": True, ...details...}, {"ok": False} when the invite is dead,
    or None when we simply couldn't find out (network error, rate limit)."""
    try:
        resp = requests.get(
            f"https://discord.com/api/v10/invites/{code}",
            params={"with_counts": "true"},
            headers={"User-Agent": "RegimentDirectory/1.0"},
            timeout=FETCH_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if resp.status_code == 404:
        return {"ok": False}
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
        guild = data.get("guild") or {}
    except ValueError:
        return None
    icon = guild.get("icon")
    gid = guild.get("id")
    return {
        "ok": True,
        "name": guild.get("name"),
        "description": guild.get("description"),
        "icon_url": f"https://cdn.discordapp.com/icons/{gid}/{icon}.png?size=128" if icon and gid else None,
        "members": data.get("approximate_member_count"),
        "online": data.get("approximate_presence_count"),
        "expires_at": data.get("expires_at"),
    }


def get_invite_infos(codes):
    """code -> info for every code, using the cache and looking the rest up in parallel."""
    now = time.time()
    result, todo = {}, []
    for code in dict.fromkeys(codes):
        cached = _cache.get(code)
        if cached and now - cached[0] < CACHE_TTL:
            result[code] = cached[1]
        elif _backoff.get(code, 0) > now:
            result[code] = cached[1] if cached else {"ok": None}
        else:
            todo.append(code)

    if todo:
        with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
            fetched = list(pool.map(fetch_invite, todo))
        for code, info in zip(todo, fetched):
            if info is None:  # couldn't reach Discord: keep serving the old answer if we have one
                _backoff[code] = now + FAIL_BACKOFF
                stale = _cache.get(code)
                result[code] = stale[1] if stale else {"ok": None}
            else:
                _cache[code] = (now, info)
                result[code] = info
    return result


def load_config():
    cfg = {"title": "Regiment Servers", "intro": "", "servers": []}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return cfg
    except (ValueError, OSError) as e:
        log.error(f"servers.json couldn't be read: {e}")
        return cfg
    if not isinstance(data, dict):
        log.error("servers.json must contain a JSON object")
        return cfg
    if isinstance(data.get("title"), str) and data["title"].strip():
        cfg["title"] = data["title"].strip()
    if isinstance(data.get("intro"), str):
        cfg["intro"] = data["intro"].strip()
    if isinstance(data.get("servers"), list):
        cfg["servers"] = [s for s in data["servers"] if isinstance(s, dict)]
    return cfg


def get_listing():
    """Everything the home page template needs."""
    cfg = load_config()
    entries = []
    for raw in cfg["servers"]:
        code = parse_invite_code(raw.get("invite"))
        if code is None:
            log.warning(f"servers.json: skipping an entry with an invalid invite: {raw.get('invite')!r}")
            continue
        entries.append((code, raw))

    infos = get_invite_infos([code for code, _ in entries])

    servers = []
    for code, raw in entries:
        info = infos.get(code) or {"ok": None}
        text = lambda key: raw.get(key).strip() if isinstance(raw.get(key), str) else ""
        servers.append({
            "name": text("name") or info.get("name") or "Unnamed server",
            "description": text("description") or info.get("description") or "",
            "category": text("category"),
            "invite_url": f"https://discord.gg/{code}",
            "icon_url": info.get("icon_url"),
            "members": info.get("members"),
            "online": info.get("online"),
            "dead": info.get("ok") is False,
        })
    return {"title": cfg["title"], "intro": cfg["intro"], "servers": servers}
