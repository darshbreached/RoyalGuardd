"""
database/mongodb.py
--------------------
Async MongoDB Atlas connection layer using Motor.
"""

import os
import time
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId


class Database:
    def __init__(self):
        uri = os.getenv("MONGODB_URI")
        db_name = os.getenv("MONGODB_DB_NAME", "royalguard")

        if not uri:
            raise RuntimeError("MONGODB_URI is not set in the environment (.env)")

        self.client = AsyncIOMotorClient(uri)
        self.db = self.client[db_name]

        self.verifications = self.db["verifications"]
        self.admin_levels = self.db["admin_levels"]
        self.groupbinds = self.db["groupbinds"]
        self.rankbinds = self.db["rankbinds"]
        self.ticket_config = self.db["ticket_config"]
        self.tickets = self.db["tickets"]
        self.guild_config = self.db["guild_config"]
        self.oauth_states = self.db["oauth_states"]
        self.rank_requests = self.db["rank_requests"]
        self.reaction_roles = self.db["reaction_roles"]
        self.invites = self.db["invites"]
        self.invite_credits = self.db["invite_credits"]
        self.automod_config = self.db["automod_config"]
        self.antinuke_config = self.db["antinuke_config"]
        self.antinuke_whitelist = self.db["antinuke_whitelist"]
        self.action_tracking = self.db["action_tracking"]
        self.tenants = self.db["tenants"]
        self.join_tracking = self.db["join_tracking"]
        self.pending_tenants = self.db["pending_tenants"]

    async def ensure_indexes(self):
        import logging
        log = logging.getLogger("RoyalGuard")

        async def _safe_create_index(collection, *args, **kwargs):
            try:
                await collection.create_index(*args, **kwargs)
            except Exception as e:
                log.warning(f"Index creation skipped/failed for {collection.name}: {e}")

        await _safe_create_index(self.verifications, "discord_id", unique=True)
        await _safe_create_index(self.verifications, "roblox_id")
        # NOTE: kept as (guild_id, discord_id) even though admin_levels now stores
        # both users and roles under "discord_id" - Discord snowflakes are unique
        # across users/roles/etc in practice, so a user ID and role ID colliding
        # is not a realistic concern. If you ever want to be fully strict, this
        # would need to become a compound (guild_id, discord_id, type) index instead.
        await _safe_create_index(self.admin_levels, [("guild_id", 1), ("discord_id", 1)], unique=True)
        await _safe_create_index(self.groupbinds, "guild_id")
        await _safe_create_index(self.rankbinds, "guild_id")
        await _safe_create_index(self.ticket_config, "guild_id", unique=True)
        await _safe_create_index(self.tickets, "channel_id", unique=True)
        await _safe_create_index(self.guild_config, "guild_id", unique=True)
        await _safe_create_index(self.oauth_states, "state", unique=True)
        await _safe_create_index(self.oauth_states, "created_at", expireAfterSeconds=120)
        await _safe_create_index(self.rank_requests, "status")
        await _safe_create_index(self.reaction_roles, "message_id")
        await _safe_create_index(self.invites, [("guild_id", 1), ("code", 1)], unique=True)
        await _safe_create_index(self.invite_credits, [("guild_id", 1), ("inviter_id", 1)], unique=True)
        await _safe_create_index(self.automod_config, "guild_id", unique=True)
        await _safe_create_index(self.antinuke_config, "guild_id", unique=True)
        await _safe_create_index(self.antinuke_whitelist, [("guild_id", 1), ("user_id", 1)], unique=True)
        await _safe_create_index(self.action_tracking, [("guild_id", 1), ("user_id", 1), ("action_type", 1)])
        await _safe_create_index(self.action_tracking, "timestamp", expireAfterSeconds=60)
        await _safe_create_index(self.tenants, "owner_discord_id")
        await _safe_create_index(self.tenants, "status")
        await _safe_create_index(self.join_tracking, "timestamp", expireAfterSeconds=120)

    # VERIFICATION (global, not per-guild)
    async def get_verification(self, discord_id: int):
        return await self.verifications.find_one({"discord_id": str(discord_id)})

    async def get_verification_by_roblox(self, roblox_id: int):
        return await self.verifications.find_one({"roblox_id": str(roblox_id)})

    async def set_verification(self, discord_id: int, roblox_id: int, roblox_username: str):
        doc = {
            "discord_id": str(discord_id),
            "roblox_id": str(roblox_id),
            "roblox_username": roblox_username,
            "verified_at": time.time(),
        }
        await self.verifications.update_one({"discord_id": str(discord_id)}, {"$set": doc}, upsert=True)
        return doc

    async def remove_verification(self, discord_id: int):
        await self.verifications.delete_one({"discord_id": str(discord_id)})

    # ADMIN LEVELS (per-guild) - supports individual users AND Discord roles.
    # Docs look like: {guild_id, discord_id, level, type: "user"|"role", role_name?}
    # Legacy docs written before this change have no "type" field - they are
    # always treated as type="user" by the queries below ({"type": {"$ne": "role"}}).
    async def get_admin_level(self, guild_id: int, discord_id: int) -> int:
        """User's own explicit level only (ignores any roles they hold)."""
        doc = await self.admin_levels.find_one({
            "guild_id": str(guild_id),
            "discord_id": str(discord_id),
            "type": {"$ne": "role"},
        })
        return doc["level"] if doc else 0

    async def set_admin_level(self, guild_id: int, discord_id: int, level: int):
        await self.admin_levels.update_one(
            {"guild_id": str(guild_id), "discord_id": str(discord_id), "type": {"$ne": "role"}},
            {"$set": {
                "guild_id": str(guild_id), "discord_id": str(discord_id),
                "level": level, "type": "user",
            }},
            upsert=True,
        )

    async def remove_admin_level(self, guild_id: int, discord_id: int):
        await self.admin_levels.delete_one({
            "guild_id": str(guild_id), "discord_id": str(discord_id), "type": {"$ne": "role"},
        })

    async def get_role_admin_level(self, guild_id: int, role_id: int) -> int:
        doc = await self.admin_levels.find_one({
            "guild_id": str(guild_id), "discord_id": str(role_id), "type": "role",
        })
        return doc["level"] if doc else 0

    async def set_role_admin_level(self, guild_id: int, role_id: int, level: int, role_name: str = ""):
        await self.admin_levels.update_one(
            {"guild_id": str(guild_id), "discord_id": str(role_id), "type": "role"},
            {"$set": {
                "guild_id": str(guild_id), "discord_id": str(role_id),
                "level": level, "type": "role", "role_name": role_name,
            }},
            upsert=True,
        )

    async def remove_role_admin_level(self, guild_id: int, role_id: int):
        await self.admin_levels.delete_one({
            "guild_id": str(guild_id), "discord_id": str(role_id), "type": "role",
        })

    async def get_effective_admin_level(self, guild_id: int, discord_id: int, role_ids: list = None) -> int:
        """Highest of: this user's own explicit level, and the level of any role
        in role_ids that has been granted an admin level."""
        levels = [await self.get_admin_level(guild_id, discord_id)]
        if role_ids:
            cursor = self.admin_levels.find({
                "guild_id": str(guild_id),
                "discord_id": {"$in": [str(r) for r in role_ids]},
                "type": "role",
            })
            async for doc in cursor:
                levels.append(doc["level"])
        return max(levels)

    async def get_all_admin_levels(self, guild_id: int):
        """All user- and role-level admin_levels docs for this guild (for /admins view)."""
        cursor = self.admin_levels.find({"guild_id": str(guild_id)})
        return [doc async for doc in cursor]

    async def guild_has_any_admin(self, guild_id: int) -> bool:
        doc = await self.admin_levels.find_one({"guild_id": str(guild_id)})
        return doc is not None

    # GROUPBINDS
    async def add_groupbind(self, guild_id: int, group_id: int, group_name: str):
        await self.groupbinds.update_one(
            {"guild_id": str(guild_id), "group_id": str(group_id)},
            {"$set": {"guild_id": str(guild_id), "group_id": str(group_id), "group_name": group_name}},
            upsert=True,
        )

    async def remove_groupbind(self, guild_id: int, group_id: int):
        await self.groupbinds.delete_one({"guild_id": str(guild_id), "group_id": str(group_id)})
        await self.rankbinds.delete_many({"guild_id": str(guild_id), "group_id": str(group_id)})

    async def list_groupbinds(self, guild_id: int):
        cursor = self.groupbinds.find({"guild_id": str(guild_id)})
        return [doc async for doc in cursor]

    # RANKBINDS (unique on guild+group+rank+role, so multiple roles per rank work)
    async def add_rankbind(self, guild_id: int, group_id: int, rank_id: int, role_id: int, rank_name: str = "", nickname_prefix: str = ""):
        await self.rankbinds.update_one(
            {"guild_id": str(guild_id), "group_id": str(group_id), "rank_id": rank_id, "role_id": str(role_id)},
            {"$set": {
                "guild_id": str(guild_id), "group_id": str(group_id), "rank_id": rank_id,
                "role_id": str(role_id), "rank_name": rank_name, "nickname_prefix": nickname_prefix,
            }},
            upsert=True,
        )

    async def remove_rankbind(self, guild_id: int, group_id: int, rank_id: int, role_id: int = None):
        query = {"guild_id": str(guild_id), "group_id": str(group_id), "rank_id": rank_id}
        if role_id is not None:
            query["role_id"] = str(role_id)
            await self.rankbinds.delete_one(query)
        else:
            await self.rankbinds.delete_many(query)

    async def list_rankbinds(self, guild_id: int, group_id: int = None):
        query = {"guild_id": str(guild_id)}
        if group_id is not None:
            query["group_id"] = str(group_id)
        cursor = self.rankbinds.find(query)
        return [doc async for doc in cursor]

    # TICKET CONFIG / TICKETS
    async def get_ticket_config(self, guild_id: int):
        return await self.ticket_config.find_one({"guild_id": str(guild_id)})

    async def set_ticket_config(self, guild_id: int, **kwargs):
        kwargs["guild_id"] = str(guild_id)
        await self.ticket_config.update_one({"guild_id": str(guild_id)}, {"$set": kwargs}, upsert=True)

    async def create_ticket(self, channel_id: int, guild_id: int, owner_id: int, category: str):
        doc = {
            "channel_id": str(channel_id), "guild_id": str(guild_id), "owner_id": str(owner_id),
            "category": category, "created_at": time.time(), "closed": False,
        }
        await self.tickets.insert_one(doc)
        return doc

    async def close_ticket(self, channel_id: int):
        await self.tickets.update_one({"channel_id": str(channel_id)}, {"$set": {"closed": True, "closed_at": time.time()}})

    async def get_ticket(self, channel_id: int):
        return await self.tickets.find_one({"channel_id": str(channel_id)})

    # GUILD CONFIG (generic)
    async def get_guild_config(self, guild_id: int):
        return await self.guild_config.find_one({"guild_id": str(guild_id)}) or {}

    async def set_guild_config(self, guild_id: int, **kwargs):
        kwargs["guild_id"] = str(guild_id)
        await self.guild_config.update_one({"guild_id": str(guild_id)}, {"$set": kwargs}, upsert=True)

    # LOG CHANNELS
    async def get_log_channel(self, guild_id: int, log_type: str):
        config = await self.get_guild_config(guild_id)
        return config.get(f"{log_type}_log_channel_id")

    async def set_log_channel(self, guild_id: int, log_type: str, channel_id: int):
        await self.set_guild_config(guild_id, **{f"{log_type}_log_channel_id": str(channel_id)})

    # OAUTH STATE
    async def create_oauth_state(self, state: str, discord_id: int):
        await self.oauth_states.insert_one({"state": state, "discord_id": str(discord_id), "created_at": time.time()})

    async def consume_oauth_state(self, state: str):
        doc = await self.oauth_states.find_one({"state": state})
        if doc:
            await self.oauth_states.delete_one({"state": state})
        return doc

    # RANK REQUESTS
    async def create_rank_request(self, guild_id: int, requester_id: int, group_id: int, rank_id: int, rank_name: str, group_name: str):
        request_id = str(ObjectId())
        doc = {
            "_id": request_id, "guild_id": str(guild_id), "requester_id": str(requester_id),
            "group_id": str(group_id), "group_name": group_name, "rank_id": rank_id,
            "rank_name": rank_name, "status": "pending", "created_at": time.time(),
        }
        await self.rank_requests.insert_one(doc)
        return doc

    async def get_rank_request(self, request_id: str):
        return await self.rank_requests.find_one({"_id": request_id})

    async def update_rank_request_status(self, request_id: str, status: str, resolved_by: int = None):
        update = {"status": status, "resolved_at": time.time()}
        if resolved_by:
            update["resolved_by"] = str(resolved_by)
        await self.rank_requests.update_one({"_id": request_id}, {"$set": update})

    async def get_pending_rank_requests(self):
        cursor = self.rank_requests.find({"status": "pending"})
        return [doc async for doc in cursor]

    async def get_rank_request_config(self, guild_id: int):
        config = await self.get_guild_config(guild_id)
        return {"approver_role_id": config.get("rankrequest_approver_role_id"), "requests_channel_id": config.get("rankrequest_channel_id")}

    async def set_rank_request_config(self, guild_id: int, approver_role_id: int = None, requests_channel_id: int = None):
        update = {}
        if approver_role_id is not None:
            update["rankrequest_approver_role_id"] = str(approver_role_id)
        if requests_channel_id is not None:
            update["rankrequest_channel_id"] = str(requests_channel_id)
        await self.set_guild_config(guild_id, **update)

    # REACTION ROLES
    async def add_reaction_role(self, guild_id: int, channel_id: int, message_id: int, emoji: str, role_id: int):
        await self.reaction_roles.update_one(
            {"guild_id": str(guild_id), "message_id": str(message_id), "emoji": emoji},
            {"$set": {"guild_id": str(guild_id), "channel_id": str(channel_id), "message_id": str(message_id), "emoji": emoji, "role_id": str(role_id)}},
            upsert=True,
        )

    async def remove_reaction_role(self, message_id: int, emoji: str):
        await self.reaction_roles.delete_one({"message_id": str(message_id), "emoji": emoji})

    async def get_reaction_role(self, message_id: int, emoji: str):
        return await self.reaction_roles.find_one({"message_id": str(message_id), "emoji": emoji})

    async def list_reaction_roles(self, message_id: int):
        cursor = self.reaction_roles.find({"message_id": str(message_id)})
        return [doc async for doc in cursor]

    async def get_all_reaction_role_message_ids(self):
        cursor = self.reaction_roles.find({})
        seen = set()
        async for doc in cursor:
            seen.add(doc["message_id"])
        return list(seen)

    # INVITE TRACKING
    async def snapshot_invites(self, guild_id: int, invite_data: list):
        for inv in invite_data:
            await self.invites.update_one(
                {"guild_id": str(guild_id), "code": inv["code"]},
                {"$set": {"guild_id": str(guild_id), "code": inv["code"], "uses": inv["uses"], "inviter_id": inv["inviter_id"]}},
                upsert=True,
            )

    async def get_invite_snapshot(self, guild_id: int, code: str):
        return await self.invites.find_one({"guild_id": str(guild_id), "code": code})

    async def get_all_invite_snapshots(self, guild_id: int):
        cursor = self.invites.find({"guild_id": str(guild_id)})
        return {doc["code"]: doc async for doc in cursor}

    async def add_invite_credit(self, guild_id: int, inviter_id: int, amount: int = 1):
        await self.invite_credits.update_one(
            {"guild_id": str(guild_id), "inviter_id": str(inviter_id)},
            {"$inc": {"count": amount}, "$set": {"guild_id": str(guild_id), "inviter_id": str(inviter_id)}},
            upsert=True,
        )

    async def get_invite_credit(self, guild_id: int, inviter_id: int) -> int:
        doc = await self.invite_credits.find_one({"guild_id": str(guild_id), "inviter_id": str(inviter_id)})
        return doc["count"] if doc else 0

    async def get_invite_leaderboard(self, guild_id: int, limit: int = 10):
        cursor = self.invite_credits.find({"guild_id": str(guild_id)}).sort("count", -1).limit(limit)
        return [doc async for doc in cursor]

    # AUTOMOD
    async def get_automod_config(self, guild_id: int):
        return await self.automod_config.find_one({"guild_id": str(guild_id)}) or {}

    async def set_automod_config(self, guild_id: int, **kwargs):
        kwargs["guild_id"] = str(guild_id)
        await self.automod_config.update_one({"guild_id": str(guild_id)}, {"$set": kwargs}, upsert=True)

    # ANTI-NUKE
    async def get_antinuke_config(self, guild_id: int):
        return await self.antinuke_config.find_one({"guild_id": str(guild_id)}) or {}

    async def set_antinuke_config(self, guild_id: int, **kwargs):
        kwargs["guild_id"] = str(guild_id)
        await self.antinuke_config.update_one({"guild_id": str(guild_id)}, {"$set": kwargs}, upsert=True)

    async def add_antinuke_whitelist(self, guild_id: int, user_id: int):
        await self.antinuke_whitelist.update_one(
            {"guild_id": str(guild_id), "user_id": str(user_id)},
            {"$set": {"guild_id": str(guild_id), "user_id": str(user_id)}},
            upsert=True,
        )

    async def remove_antinuke_whitelist(self, guild_id: int, user_id: int):
        await self.antinuke_whitelist.delete_one({"guild_id": str(guild_id), "user_id": str(user_id)})

    async def is_antinuke_whitelisted(self, guild_id: int, user_id: int) -> bool:
        doc = await self.antinuke_whitelist.find_one({"guild_id": str(guild_id), "user_id": str(user_id)})
        return doc is not None

    async def record_action(self, guild_id: int, user_id: int, action_type: str):
        await self.action_tracking.insert_one({"guild_id": str(guild_id), "user_id": str(user_id), "action_type": action_type, "timestamp": time.time()})

    async def count_recent_actions(self, guild_id: int, user_id: int, action_type: str, window_seconds: int = 60) -> int:
        cutoff = time.time() - window_seconds
        return await self.action_tracking.count_documents({
            "guild_id": str(guild_id), "user_id": str(user_id), "action_type": action_type, "timestamp": {"$gte": cutoff},
        })

    # WELCOME DM (reuses guild_config)
    async def get_welcome_dm_config(self, guild_id: int):
        return await self.get_guild_config(guild_id)

    async def set_welcome_dm_config(self, guild_id: int, **kwargs):
        await self.set_guild_config(guild_id, **kwargs)

    # TENANTS (multi-instance bot hosting)
    async def add_tenant(self, owner_discord_id: int, encrypted_token: str, bot_name: str = ""):
        doc = {
            "owner_discord_id": owner_discord_id,
            "encrypted_token": encrypted_token,
            "bot_name": bot_name,
            "status": "active",
            "last_error": None,
            "created_at": time.time(),
        }
        result = await self.tenants.insert_one(doc)
        return str(result.inserted_id)

    async def get_tenant(self, tenant_id: str):
        return await self.tenants.find_one({"_id": ObjectId(tenant_id)})

    async def list_tenants(self, status: str = None, owner_discord_id: int = None):
        query = {}
        if status:
            query["status"] = status
        if owner_discord_id:
            query["owner_discord_id"] = owner_discord_id
        cursor = self.tenants.find(query)
        return [doc async for doc in cursor]

    async def set_tenant_status(self, tenant_id: str, status: str, last_error: str = None):
        await self.tenants.update_one(
            {"_id": ObjectId(tenant_id)},
            {"$set": {"status": status, "last_error": last_error}},
        )

    async def remove_tenant(self, tenant_id: str):
        await self.tenants.delete_one({"_id": ObjectId(tenant_id)})

    # JOIN TRACKING (anti-raid)
    async def record_join_event(self, guild_id: int):
        await self.join_tracking.insert_one({"guild_id": str(guild_id), "timestamp": time.time()})

    async def count_recent_joins(self, guild_id: int, window_seconds: int) -> int:
        cutoff = time.time() - window_seconds
        return await self.join_tracking.count_documents({"guild_id": str(guild_id), "timestamp": {"$gte": cutoff}})

    # PENDING TENANTS (awaiting owner approval, submitted via /register panel)
    async def add_pending_tenant(self, owner_discord_id: int, encrypted_token: str, bot_name: str = ""):
        doc = {
            "owner_discord_id": owner_discord_id,
            "encrypted_token": encrypted_token,
            "bot_name": bot_name,
            "submitted_at": time.time(),
        }
        result = await self.pending_tenants.insert_one(doc)
        return str(result.inserted_id)

    async def get_pending_tenant(self, pending_id: str):
        return await self.pending_tenants.find_one({"_id": ObjectId(pending_id)})

    async def list_pending_tenants(self):
        cursor = self.pending_tenants.find({})
        return [doc async for doc in cursor]

    async def remove_pending_tenant(self, pending_id: str):
        await self.pending_tenants.delete_one({"_id": ObjectId(pending_id)})


db = Database()
