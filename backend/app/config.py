"""Environment-driven configuration for the V-CAD API.

Only values that come from the environment (or have an environment default)
live here. Domain constants such as the local projection origin belong in
``app.services.geometry``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

#: Bumped only when the externally observable contract changes.
API_VERSION = "0.1.0"

#: Maximum accepted upload size, in bytes. Exceeding it is a 413.
MAX_UPLOAD_BYTES = 10_000_000

DEFAULT_CORS_ORIGIN = "http://localhost:3000"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings resolved from the environment."""

    api_title: str
    api_version: str
    api_description: str
    cors_origins: tuple[str, ...]
    database_url: str | None

    @property
    def cors_origin_list(self) -> list[str]:
        return list(self.cors_origins)


def get_settings() -> Settings:
    """Build settings from the environment.

    ``CORS_ORIGINS`` is a comma-separated list and is split verbatim (no
    whitespace stripping) to preserve the original behaviour exactly.
    """
    return Settings(
        api_title="V-CAD API",
        api_version=API_VERSION,
        # Prototype disclaimer: these identifiers are not official ULPINs.
        api_description=(
            "Prototype 3D ULPIN and volumetric cadastral API. "
            "Identifiers are not official Government of India ULPINs."
        ),
        cors_origins=tuple(
            os.getenv("CORS_ORIGINS", DEFAULT_CORS_ORIGIN).split(",")
        ),
        database_url=os.getenv("DATABASE_URL"),
    )


@lru_cache(maxsize=1)
def cached_settings() -> Settings:
    """Process-wide settings singleton."""
    return get_settings()
