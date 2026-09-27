"""Declarative base for the PostGIS schema.

Alembic autogenerate reads ``Base.metadata``, so every table the application
owns must be imported somewhere for migrations to see it.
"""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all persisted models."""

    def to_dict(self) -> dict:
        """Shallow column mapping, excluding the derived metric geometry."""
        return {
            column.name: getattr(self, column.name)
            for column in self.__table__.columns
            if not column.name.endswith("_metric")
        }
