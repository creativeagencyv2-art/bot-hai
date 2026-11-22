from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db() -> None:
    from app import models  # noqa: F401  (registers tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(conn) -> None:
    """create_all never alters existing tables. This adds columns introduced in later phases.
    (For PostgreSQL in production we will switch to Alembic migrations.)"""
    from sqlalchemy import inspect, text

    wanted = {
        "plans": {"description": "TEXT NOT NULL DEFAULT ''", "delivery": "VARCHAR(16) NOT NULL DEFAULT 'instant'"},
        "orders": {"customer_info": "TEXT NOT NULL DEFAULT ''"},
    }
    insp = inspect(conn)
    for table, cols in wanted.items():
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in cols.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
