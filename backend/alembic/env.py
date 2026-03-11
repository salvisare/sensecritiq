import asyncio
import os
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Ensure backend/ is on sys.path so local modules resolve
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config import settings  # noqa: E402
from models.base import Base  # noqa: E402
# Import all models so their tables are registered on Base.metadata
from models.account import Account  # noqa: F401, E402
from models.api_key import ApiKey  # noqa: F401, E402
from models.session import Session  # noqa: F401, E402
from models.usage_log import UsageLog  # noqa: F401, E402

alembic_config = context.config

# Override sqlalchemy.url from pydantic-settings (reads .env automatically)
alembic_config.set_main_option("sqlalchemy.url", settings.async_database_url)

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (emit SQL to stdout)."""
    url = alembic_config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live DB using the async engine."""
    connectable = async_engine_from_config(
        alembic_config.get_section(alembic_config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
