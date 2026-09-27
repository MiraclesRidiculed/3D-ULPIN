"""Alembic environment.

The database URL always comes from the application settings / ``DATABASE_URL``;
``alembic.ini`` deliberately leaves ``sqlalchemy.url`` empty so there is a single
source of truth and no credentials end up committed.

PostGIS is required: the schema is meaningless without it, so extensions are
ensured in ``include_object``-independent setup rather than being assumed.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

import sqlalchemy as sa
from alembic import context
from sqlalchemy import engine_from_config, pool, text

# Make the `app` package importable when alembic runs from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import COLLECTION_MODELS, Base  # noqa: E402
from app.db.session import get_database_url, set_database_url  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Schema metadata Alembic compares the database against.
target_metadata = Base.metadata

#: Tables the application owns. Anything else in the database (the Tiger
#: geocoder schema shipped by the postgis image) is outside our remit.
APPLICATION_TABLES = frozenset(
    model.__tablename__ for model in COLLECTION_MODELS.values()
)


def _database_url() -> str:
    """Resolve the target URL for the migration run.

    ``alembic.ini`` leaves ``sqlalchemy.url`` empty on purpose, so there is one
    source of truth and no credentials get committed. Resolution order:

    1. ``-x url=...`` on the command line, for one-off targets
    2. ``ALEMBIC_DATABASE_URL`` (used by CI and the test harness)
    3. ``DATABASE_URL``

    Note this bypasses ``VCAD_REPOSITORY=memory`` deliberately: a migration run
    is an explicit request to touch the database.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    url = x_args.get("url") or os.getenv("ALEMBIC_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit(
            "No database URL for migrations. Set one of:\n"
            "  ALEMBIC_DATABASE_URL=postgresql+psycopg://vcad:vcad@localhost:5432/vcad\n"
            "  DATABASE_URL=...\n"
            "  alembic -x url=... upgrade head"
        )
    set_database_url(url)
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        # PostGIS geometry types are not comparable through SQLAlchemy's type
        # system, so autogenerate would otherwise try to "fix" them.
        include_object=_skip_derived_metric_columns,
    )
    with context.begin_transaction():
        context.run_migrations()


def _skip_derived_metric_columns(object_, name, type_, reflected, compare_to) -> bool:
    """Restrict autogenerate to application tables and real columns.

    Two exclusions:

    * **Generated metric columns.** They are ``STORED`` columns produced by the
      database from their geographic source; SQLAlchemy cannot reflect them
      meaningfully and would propose dropping them.
    * **Tables the application does not own.** The ``postgis/postgis`` image
      ships the Tiger geocoder schema (``tiger_*``, ``zip_lookup*``, and ~100
      others). Autogenerate would otherwise report every one as "removed".
    """
    if isinstance(object_, sa.Table):
        if object_.name not in APPLICATION_TABLES:
            return False
        return not str(name).endswith("_metric")
    if type_ is not None and getattr(type_, "computed", None) is not None:
        return False
    return True


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        # PostGIS must exist before any geometry column can be created.
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS pgcrypto"))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=_skip_derived_metric_columns,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
