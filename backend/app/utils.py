"""Small shared helpers."""
from __future__ import annotations

from datetime import datetime, timezone


def now() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()
