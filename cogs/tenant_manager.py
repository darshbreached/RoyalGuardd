"""
tenant_manager.py
------------------
Rebuilt from scratch - there is no old tenant-runner service left on
Railway to restore, this is a new implementation.

Polls the `tenants` collection every 30 seconds and runs one full
RoyalGuardBot instance per tenant whose status is "active" - same cogs,
same intents, same persistent views, same command sync as main.py's
worker process, just logged in under that tenant's own bot token instead
of DISCORD_TOKEN.

Deploy as its own Railway service (Procfile entry: tenant-runner), separate
from "worker" (main.py) and "web" (website), so a crash here never touches
the main bot or the website.
"""

import asyncio
import logging

import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("TenantRunner")

from database.mongodb import db
from utils.token_crypto import decrypt_token
from main import RoyalGuardBot

POLL_INTERVAL_SECONDS = 30


class TenantWorker:
    """Wraps one tenant's running bot instance and its asyncio task."""

    def __init__(self, tenant_id: str, bot: RoyalGuardBot):
        self.tenant_id = tenant_id
        self.bot = bot
        self.task: asyncio.Task | None = None

    async def stop(self):
        try:
            await self.bot.close()
        except Exception:
            log.exception(f"Error closing bot for tenant {self.tenant_id}")
        if self.task:
            self.task.cancel()


class TenantRunner:
    def __init__(self):
        self.workers: dict[str, TenantWorker] = {}

    async def start_tenant(self, tenant_doc: dict):
        tenant_id = str(tenant_doc["_id"])
        bot_name = tenant_doc.get("bot_name") or tenant_id

        try:
            token = decrypt_token(tenant_doc["encrypted_token"])
        except ValueError as e:
            log.error(f"Tenant {tenant_id} ({bot_name}): {e}")
            await db.set_tenant_status(tenant_id, "error", str(e))
            return

        bot = RoyalGuardBot()
        worker = TenantWorker(tenant_id, bot)

        async def _run():
            try:
                async with bot:
                    await bot.start(token)
            except discord.LoginFailure:
                log.error(f"Tenant {tenant_id} ({bot_name}): invalid or revoked bot token.")
                await db.set_tenant_status(tenant_id, "error", "Invalid or revoked bot token.")
            except Exception as e:
                log.exception(f"Tenant {tenant_id} ({bot_name}) crashed.")
                await db.set_tenant_status(tenant_id, "error", str(e))
            finally:
                self.workers.pop(tenant_id, None)

        worker.task = asyncio.create_task(_run())
        self.workers[tenant_id] = worker
        log.info(f"Started tenant bot: {tenant_id} ({bot_name})")

    async def stop_tenant(self, tenant_id: str):
        worker = self.workers.get(tenant_id)
        if worker:
            log.info(f"Stopping tenant bot: {tenant_id}")
            await worker.stop()
            self.workers.pop(tenant_id, None)

    async def reconcile(self):
        """Compares which tenants are marked 'active' in Mongo against what's
        actually running right now, and starts/stops workers to match."""
        active_docs = await db.list_tenants(status="active")
        active_ids = {str(doc["_id"]) for doc in active_docs}
        running_ids = set(self.workers.keys())

        for tenant_id in running_ids - active_ids:
            await self.stop_tenant(tenant_id)

        for doc in active_docs:
            tenant_id = str(doc["_id"])
            if tenant_id not in running_ids:
                await self.start_tenant(doc)

    async def run_forever(self):
        await db.ensure_indexes()
        log.info(f"Tenant runner started. Polling every {POLL_INTERVAL_SECONDS}s.")
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("Error during tenant reconciliation pass.")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    runner = TenantRunner()
    asyncio.run(runner.run_forever())