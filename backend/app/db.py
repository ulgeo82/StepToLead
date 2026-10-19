from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


# Фоновые задачи (сбор контактов параллельно, рассылки, опрос каналов) держат соединения подолгу:
# запас пула, чтобы запросы из интерфейса не ждали свободного соединения. Только для PostgreSQL.
_pool = {"pool_size": 10, "max_overflow": 20, "pool_timeout": 15} if settings.database_url.startswith("postgresql") else {}
engine = create_async_engine(settings.database_url, pool_pre_ping=True, **_pool)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session

