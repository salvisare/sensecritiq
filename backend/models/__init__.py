from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from config import settings
from .base import Base
from .account import Account
from .api_key import ApiKey
from .session import Session
from .usage_log import UsageLog

engine = create_async_engine(settings.async_database_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


__all__ = [
    "Base",
    "Account",
    "ApiKey",
    "Session",
    "UsageLog",
    "engine",
    "AsyncSessionLocal",
    "get_db",
]
