"""In-memory cadastral repository used for development and the offline demo.

State is process-global and non-durable: restarting the API (or reloading with
``uvicorn --reload``) discards everything and re-seeds the demo scene. This is
the deliberate offline-demo boundary; see ``PROJECT_CONTEXT.md``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.models.schemas import SearchResult
from app.repositories.base import (
    COLLECTIONS,
    SEARCH_ORDER,
    SEARCHABLE_COLLECTIONS,
    CadastreRepository,
    DuplicateRecord,
    RecordNotFound,
)
from app.services.storage import normalise_changes

#: Guard: the two vocabularies must stay in step, since ``search()`` relies on
#: their ordering agreeing.
assert tuple(key for _, key in SEARCH_ORDER) == SEARCHABLE_COLLECTIONS

#: Fields consulted when matching a search term, in join order.
SEARCH_FIELDS: tuple[str, ...] = (
    "id",
    "parcel_id",
    "building_id",
    "prototype_ulpin",
    "unit_label",
    "type",
)

#: Search order: (result kind, collection name).
SEARCH_ORDER: tuple[tuple[str, str], ...] = (
    ("parcel", "parcels"),
    ("building", "buildings"),
    ("property", "properties"),
    ("infrastructure", "infrastructure"),
)

#: Collections scanned by :meth:`InMemoryRepository.find_by_id`, in priority order.
SEARCHABLE_COLLECTIONS: tuple[str, ...] = (
    "parcels",
    "buildings",
    "properties",
    "infrastructure",
)


class InMemoryRepository(CadastreRepository):
    """Dict-of-lists store. Mirrors the original module-level ``DB`` exactly."""

    def __init__(self) -> None:
        self._db: dict[str, list[dict[str, Any]]] = {k: [] for k in COLLECTIONS}

    @property
    def db(self) -> dict[str, list[dict[str, Any]]]:
        """Direct access to the underlying store (services use ``records``)."""
        return self._db

    def records(self, kind: str) -> list[dict[str, Any]]:
        return self._db[kind]

    def replace_all(self, data: dict[str, list[dict[str, Any]]]) -> None:
        """Swap the whole store, guaranteeing every collection exists.

        Missing keys are seeded empty rather than dropped, so a caller that
        knows nothing about ``geometry_versions`` cannot leave the store in a
        shape the versioning service cannot handle.
        """
        self._db.clear()
        for kind in COLLECTIONS:
            self._db[kind] = list(data.get(kind, []))
        for kind, value in data.items():
            self._db.setdefault(kind, list(value))

    def find_by_id(self, object_id: str) -> tuple[str, dict[str, Any]] | None:
        for collection in SEARCHABLE_COLLECTIONS:
            for record in self._db[collection]:
                if record.get("id") == object_id:
                    return collection, record
        return None

    def add(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        """Insert one record, rejecting a duplicate primary key.

        This used to append unconditionally, so a duplicate ``id`` produced two
        rows here while the PostGIS adapter raised ``IntegrityError``. The two
        backends therefore returned different data for the same request -- a
        divergence the storage contract is supposed to make impossible. A
        duplicate id is a bug in the caller's id derivation, not a state to
        store, so both backends now reject it. Append-only collections are not
        affected: their ids carry a uuid or a microsecond prefix.
        """
        object_id = record.get("id")
        if object_id is not None:
            for existing in self._db[kind]:
                if existing.get("id") == object_id:
                    raise DuplicateRecord(kind, object_id)
        self._db[kind].append(record)
        return record

    def add_all(self, kind: str, records: Iterable[dict[str, Any]]) -> None:
        self._db[kind].extend(records)

    def update(
        self, kind: str, object_id: str, changes: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Apply a partial update to one record, returning it.

        The change set is filtered through :func:`normalise_changes` exactly as
        the PostGIS repository filters it. Skipping that here used to make this
        store accept writes the SQL one silently drops, so the two backends
        returned different records for the same call -- and it is what makes the
        append-only guarantee on the audit log real rather than aspirational: an
        empty updatable-field set has to be able to reject something.
        """
        for record in self._db[kind]:
            if record.get("id") == object_id:
                record.update(normalise_changes(kind, changes))
                return record
        return None

    def delete(self, kind: str, object_id: str) -> bool:
        records = self._db[kind]
        for index, record in enumerate(records):
            if record.get("id") == object_id:
                del records[index]
                return True
        return False

    def clear(self, kind: str) -> None:
        self._db[kind].clear()

    def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "backend": "memory",
            "collections": {k: len(v) for k, v in self._db.items()},
        }

    def search(self, ident: str) -> SearchResult:
        q = ident.strip().lower()
        for kind, key in SEARCH_ORDER:
            for r in self._db[key]:
                hay = " ".join(str(r.get(f, "")) for f in SEARCH_FIELDS).lower()
                if q in hay:
                    related = (
                        {
                            "children": [
                                p["id"]
                                for p in self._db["properties"]
                                if p.get("building_id") == r.get("building_id")
                            ]
                        }
                        if kind == "building"
                        else {}
                    )
                    return SearchResult(kind=kind, record=r, related=related)
        raise RecordNotFound(ident)

    def __len__(self) -> int:
        return sum(len(v) for v in self._db.values())
