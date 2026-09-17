from contextlib import asynccontextmanager
import asyncio
from contextlib import suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import models  # noqa: F401
from app.api.router import api_router
from app.core.config import settings
from app.db import Base, engine
from app.services.campaign_runner import run_campaigns
from app.services.telegram_parser import run_parser_worker


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    runner = asyncio.create_task(run_campaigns())
    parser_worker = asyncio.create_task(run_parser_worker())
    try:
        yield
    finally:
        runner.cancel()
        parser_worker.cancel()
        with suppress(asyncio.CancelledError):
            await runner
        with suppress(asyncio.CancelledError):
            await parser_worker
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


@app.get("/health")
async def health():
    return {"status": "ok"}
