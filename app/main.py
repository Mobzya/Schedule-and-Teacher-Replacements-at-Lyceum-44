from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web

from .config import load_settings
from .db import Database
from .handlers import admin, friends, student
from .health import make_app
from .services.avatars import AvatarService
from .services.calendar import CalendarService
from .services.notifications import NotificationService
from .services.schedule import ScheduleService
from .services.sync import SyncService
from .utils import now_local

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def sync_worker(sync: SyncService, minutes: int):
    while True:
        try:
            await sync.sync_all()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Periodic sync failed")
        await asyncio.sleep(minutes * 60)


async def avatar_worker(bot: Bot, db: Database, settings):
    from aiogram.types import FSInputFile, InputProfilePhotoStatic

    async def upload(theme):
        path = Path(__file__).resolve().parents[1] / "public" / "avatars" / theme.filename
        confirmed = await bot.set_my_profile_photo(photo=InputProfilePhotoStatic(photo=FSInputFile(path)))
        if confirmed is not True:
            raise RuntimeError("Telegram did not confirm profile photo update")

    service = AvatarService(db, settings.timezone, upload)
    while True:
        try:
            await service.refresh(now_local(settings.timezone))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Avatar rotation failed")
        await asyncio.sleep(600)


async def main():
    settings = load_settings()
    db = Database(settings.database_path, settings.database_url)
    await db.init()
    if await db.get_state("publication_state") is None:
        await db.set_state("publication_state", "waiting")
    sync = SyncService(db, settings)
    await sync.bootstrap()
    calendar = CalendarService(db)
    schedules = ScheduleService(db, settings, calendar)
    notifications = NotificationService(db, settings, schedules)

    bot = Bot(settings.bot_token)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin.router)
    dp.include_router(student.router)
    dp.include_router(friends.router)

    runner = web.AppRunner(make_app(db))
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.port)
    await site.start()
    logger.info("Health server on %s; publication=%s", settings.port, (await db.publication_state()).value)

    # Не блокируем старт Telegram даже если Google Sheets временно недоступны.
    tasks = [
        asyncio.create_task(sync_worker(sync, settings.source_sync_minutes)),
        asyncio.create_task(notifications.worker(bot)),
        asyncio.create_task(avatar_worker(bot, db, settings)),
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, db=db, settings=settings, schedules=schedules, sync=sync)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with suppress(asyncio.CancelledError):
                await t
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
