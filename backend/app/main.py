from contextlib import asynccontextmanager
import asyncio
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.config import settings
from app.db import Base, engine
from app.db_upgrade import upgrade_existing_schema
from app.services.avito_leads import run_worker as run_avito_worker
from app.services.campaign_runner import run_campaigns
from app.services.crm_automation import run_worker as run_crm_worker
from app.services.messaging import run_worker as run_messaging_worker
from app.services.telephony import run_worker as run_telephony_worker
from app.services.watchdog import run_worker as run_watchdog
from app.services.ad_sync import run_worker as run_ad_sync
from app.services.telegram_parser import run_parser_worker
from app.domains.leadgen.scheduler import run_worker as run_leadgen_repeat
from app.domains.leadgen.sequences import run_worker as run_leadgen_sequences


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(upgrade_existing_schema)
    runner = asyncio.create_task(run_campaigns())
    parser_worker = asyncio.create_task(run_parser_worker())
    avito_worker = asyncio.create_task(run_avito_worker())
    crm_worker = asyncio.create_task(run_crm_worker())
    messaging_worker = asyncio.create_task(run_messaging_worker())
    from app.services.telegram_profiles import run_worker as run_profiles_worker
    profiles_worker = asyncio.create_task(run_profiles_worker())
    from app.services.chat_automation import run_worker as run_chat_automation
    chat_automation_worker = asyncio.create_task(run_chat_automation())
    telephony_worker = asyncio.create_task(run_telephony_worker())
    watchdog = asyncio.create_task(run_watchdog())
    ad_sync = asyncio.create_task(run_ad_sync())
    leadgen_repeat = asyncio.create_task(run_leadgen_repeat())
    leadgen_sequences = asyncio.create_task(run_leadgen_sequences())
    from app.services import monitor
    monitor.install()
    monitor.register("Telegram-профили клиентов", profiles_worker)
    monitor.register("Автоответы в чатах", chat_automation_worker)
    from app.services.tg_bot import run_worker as run_tg_bot
    tg_worker = asyncio.create_task(run_tg_bot()) if settings.telegram_bot_token else None
    if tg_worker:
        monitor.register("Telegram-бот", tg_worker)
    for name, task in (("Рассылки Telegram", runner), ("Сбор контактов", parser_worker), ("Авито", avito_worker),
                       ("Автоматизации CRM", crm_worker), ("Переписки", messaging_worker), ("Телефония", telephony_worker),
                       ("Контроль заявок и отчёты", watchdog), ("Обновление рекламы", ad_sync), ("Лидогенерация: повтор поиска", leadgen_repeat), ("Аутрич: цепочки и почта", leadgen_sequences)):
        monitor.register(name, task)
    monitor_worker = asyncio.create_task(monitor.run_worker())
    try:
        yield
    finally:
        runner.cancel()
        parser_worker.cancel()
        avito_worker.cancel()
        crm_worker.cancel()
        messaging_worker.cancel()
        profiles_worker.cancel()
        chat_automation_worker.cancel()
        with suppress(asyncio.CancelledError):
            await chat_automation_worker
        with suppress(asyncio.CancelledError):
            await profiles_worker
        telephony_worker.cancel()
        watchdog.cancel()
        ad_sync.cancel()
        leadgen_repeat.cancel()
        leadgen_sequences.cancel()
        monitor_worker.cancel()
        if tg_worker:
            tg_worker.cancel()
            with suppress(asyncio.CancelledError):
                await tg_worker
        with suppress(asyncio.CancelledError):
            await ad_sync
        with suppress(asyncio.CancelledError):
            await monitor_worker
        with suppress(asyncio.CancelledError):
            await runner
        with suppress(asyncio.CancelledError):
            await parser_worker
        with suppress(asyncio.CancelledError):
            await avito_worker
        with suppress(asyncio.CancelledError):
            await crm_worker
        with suppress(asyncio.CancelledError):
            await messaging_worker
        with suppress(asyncio.CancelledError):
            await telephony_worker
        with suppress(asyncio.CancelledError):
            await watchdog
        await engine.dispose()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
allowed_frontend_origins = sorted({
    settings.frontend_origin,
    "http://localhost:3000",
    "http://127.0.0.1:3000",
})
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")


class RememberFailures:
    """Ошибка 500 → в монитор с адресом, проектом и местом в коде (для понятного уведомления в Telegram).
    Чистый ASGI-слой: не буферизует ответы и не мешает стримингу, в отличие от @app.middleware."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        try:
            await self.app(scope, receive, send)
        except Exception as exc:
            try:
                from urllib.parse import parse_qs
                from app.services import monitor
                query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
                monitor.record_exception(scope.get("method", ""), scope.get("path", ""),
                                         (query.get("project_id") or [None])[0], exc)
            except Exception:
                pass
            raise


app.add_middleware(RememberFailures)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/health")
async def public_health():
    """For an external uptime monitor (reachable through Caddy → Next): fails when the database is down."""
    from sqlalchemy import text
    from app.db import SessionLocal
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    return {"status": "ok"}
