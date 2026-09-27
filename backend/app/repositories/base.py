"""Repository abstraction.

The API layer depends only on this interface, so the development/demo in-memory
store can be swapped for a PostGIS-backed implementation without touching
routers or services.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from app.models.schemas import SearchResult

#: Every collection a repository must expose, in canonical order.
COLLECTIONS: tuple[str, ...] = (
    "parcels",
    "buildings",
    "floors",
    "properties",
    "infrastructure",
    "issues",
    "sources",
    "geometry_versions",
    "processing_jobs",
    "extracted_buildings",
    "extracted_floors",
    "generated_property_volumes",
    "review_cases",
    "review_decisions",
    "audit_events",
    "cadastral_changes",
    "provenance_links",
)


class DuplicateRecord(Exception):
    """Raised when a record's primary key already exists in the collection.

    Both adapters raise this, so a duplicate id fails identically on either
    backend. It used to be the worst kind of divergence: the in-memory store
    appended the row while PostGIS raised ``IntegrityError``, so the same request
    returned two rows on one backend and a 500 on the other. A collision means the
    caller's id derivation is wrong, which is a defect to surface rather than a
    state to store.

    Append-only collections are unaffected -- their ids carry a uuid or a
    microsecond prefix, so a collision there is already impossible.
    """

    def __init__(self, collection: str, object_id: object) -> None:
        super().__init__(
            f"duplicate primary key {object_id!r} in collection {collection!r}"
        )
        self.collection = collection
        self.object_id = object_id


class RecordNotFound(LookupError):
    """Raised when a search matches no cadastral object.

    Deliberately a domain error rather than ``HTTPException`` so that
    repositories stay transport-agnostic; the API layer maps it to a 404.
    """


#: Search order: (result kind, collection name). Shared by both implementations
#: so results are identical regardless of backend.
SEARCH_ORDER: tuple[tuple[str, str], ...] = (
    ("parcel", "parcels"),
    ("building", "buildings"),
    ("property", "properties"),
    ("infrastructure", "infrastructure"),
)

#: Collections scanned by exact-id lookup, in priority order.
SEARCHABLE_COLLECTIONS: tuple[str, ...] = (
    "parcels",
    "buildings",
    "properties",
    "infrastructure",
)


class CadastreRepository(ABC):
    """Storage boundary for cadastral records."""

    @abstractmethod
    def records(self, kind: str) -> list[dict[str, Any]]:
        """Return the live, mutable list backing ``kind``."""

    @abstractmethod
    def replace_all(self, data: dict[str, list[dict[str, Any]]]) -> None:
        """Atomically swap the entire store contents (used by demo seeding)."""

    @abstractmethod
    def add(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        """Insert one record and return it."""

    @abstractmethod
    def add_all(self, kind: str, records: Iterable[dict[str, Any]]) -> None:
        """Bulk-insert records into a collection."""

    @abstractmethod
    def update(
        self, kind: str, object_id: str, changes: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Apply a partial update to one record, returning it.

        Returns ``None`` when no such record exists.
        """

    @abstractmethod
    @abstractmethod
    def delete(self, kind: str, object_id: str) -> bool:
        """Remove one record. Returns whether a record was removed.

        Needed where a record's identity is *stable* and a regeneration replaces
        it rather than accumulating a second copy. An update cannot express this,
        because change sets drop ``None`` values, so a field that becomes unset
        on the new record would keep its stale old value.
        """

    def clear(self, kind: str) -> None:
        """Remove every record from a collection."""

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """Report backend reachability without raising."""

    @abstractmethod
    def search(self, ident: str) -> SearchResult:
        """Locate a cadastral object by substring match.

        Searches parcels, then buildings, then property volumes, then
        infrastructure. Raises ``RecordNotFound`` when nothing matches.
        """

    @abstractmethod
    def find_by_id(self, object_id: str) -> tuple[str, dict[str, Any]] | None:
        """Locate a record by exact primary key.

        Returns ``(collection_name, record)`` or ``None``. Geometry versioning
        needs this to resolve an object without knowing its type up front.
        """
