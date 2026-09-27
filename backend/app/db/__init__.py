"""Database layer: engine, session handling, models and PostGIS column types."""
from app.db.base import Base
from app.db.models import COLLECTION_MODELS
from app.db.session import (
    check_database_health,
    get_database_url,
    get_db_session,
    get_engine,
    get_session_factory,
    reset_engine,
    session_scope,
    uses_postgres,
)
from app.db.types import GEOGRAPHIC_SRID, METRIC_SRID

__all__ = [
    "COLLECTION_MODELS",
    "GEOGRAPHIC_SRID",
    "METRIC_SRID",
    "Base",
    "check_database_health",
    "get_database_url",
    "get_db_session",
    "get_engine",
    "get_session_factory",
    "reset_engine",
    "session_scope",
    "uses_postgres",
]
