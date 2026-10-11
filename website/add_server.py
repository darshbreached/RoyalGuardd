"""
website/add_server.py
----------------------
Add, remove and list the servers on the home page without hand-editing JSON.

Run from the repo root:

  python3 -m website.add_server add https://discord.gg/abc --category "Infantry" --desc "Main regiment"
  python3 -m website.add_server remove https://discord.gg/abc
  python3 -m website.add_server list

Then commit and push servers.json so the website redeploys with the change.
"""

import argparse
import json
import os
import sys

from website.servers import CONFIG_PATH, fetch_invite, parse_invite_code


def _load():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        sys.exit("servers.json isn't a JSON object - fix or delete it first.")
    data.setdefault("title", "Regiment Servers")
    data.setdefault("intro", "")
    if not isinstance(data.get("servers"), list):
        data["servers"] = []
    return data


def _save(data):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)  # never leaves a half-written file behind


def _code_of(entry):
    return parse_invite_code(entry.get("invite")) if isinstance(entry, dict) else None


def cmd_add(args):
    code = parse_invite_code(args.invite)
    if not code:
        sys.exit("That doesn't look like a Discord invite (expected something like https://discord.gg/abc123).")
    data = _load()
    if any(_code_of(s) == code for s in data["servers"]):
        sys.exit(f"{code} is already listed. Remove it first if you want to change it.")

    info = fetch_invite(code)
    if info is None:
        print("Warning: couldn't reach Discord to check this invite. Adding it anyway.")
    elif info.get("ok") is False and not args.force:
        sys.exit("Discord says that invite doesn't exist or has expired. Make a permanent one (or use --force).")
    elif info.get("ok"):
        members = info.get("members")
        print(f"Found: {info.get('name')}" + (f" ({members:,} members)" if isinstance(members, int) else ""))
        if info.get("expires_at"):
            print(f"Warning: this invite EXPIRES ({info['expires_at']}). Create one set to 'Never' expire, or the listing will break.")

    entry = {"invite": f"https://discord.gg/{code}"}
    if args.name:
        entry["name"] = args.name
    if args.category:
        entry["category"] = args.category
    if args.desc:
        entry["description"] = args.desc
    data["servers"].append(entry)
    _save(data)
    print(f"Added. {len(data['servers'])} server(s) listed. Now commit and push servers.json.")


def cmd_remove(args):
    code = parse_invite_code(args.invite)
    if not code:
        sys.exit("That doesn't look like a Discord invite or invite code.")
    data = _load()
    kept = [s for s in data["servers"] if _code_of(s) != code]
    if len(kept) == len(data["servers"]):
        sys.exit(f"{code} isn't in the list.")
    data["servers"] = kept
    _save(data)
    print(f"Removed. {len(kept)} server(s) listed. Now commit and push servers.json.")


def cmd_list(args):
    data = _load()
    if not data["servers"]:
        print("No servers listed yet.")
    for i, s in enumerate(data["servers"], start=1):
        label = s.get("name") or "(name comes from Discord)"
        cat = f" [{s['category']}]" if s.get("category") else ""
        print(f"{i}. {label}{cat} - {s.get('invite')}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Manage the regiment server list.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="add a server")
    p.add_argument("invite", help="permanent invite link, e.g. https://discord.gg/abc123")
    p.add_argument("--name", help="override the name Discord reports")
    p.add_argument("--category", help="e.g. Infantry, Armour, Navy")
    p.add_argument("--desc", help="short description shown on the card")
    p.add_argument("--force", action="store_true", help="add even if Discord says the invite is dead")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="remove a server")
    p.add_argument("invite", help="its invite link or code")
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="show what's listed")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
