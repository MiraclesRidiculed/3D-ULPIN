"""On-disk retention for ingested point clouds.

Metadata inspection needs only a header, so ingestion could delete its spooled
copy immediately. Extraction genuinely needs the points, so an ingested cloud
has to survive its own upload.

Layout::

    <store>/<source_id>/<filename>

Keyed by ``source_id`` rather than by hash so two uploads of the same bytes are
kept as two sources -- they are two observations, and re-ingesting the same file
is a normal thing to do. ``point_cloud_storage_path`` on the source record holds
the relative path, so a store can be moved or re-rooted without rewriting rows.

Deliberately local filesystem: this is a single-process prototype. Point clouds
are large and immutable once written, so nothing here needs to be transactional.
"""
from __future__ import annotations

import shutil
from pathlib import Path

#: Root for retained uploads. Overridable so tests and deployments can redirect it.
DEFAULT_STORE_DIR = Path(__file__).resolve().parents[2] / "data" / "point_clouds"


def store_root() -> Path:
    """Root directory for retained point clouds, created on demand."""
    root = DEFAULT_STORE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def source_dir(source_id: str) -> Path:
    """Directory holding one source's file."""
    return store_root() / source_id


def retained_path(source_id: str, filename: str) -> Path:
    """Full path a source's file is (or would be) stored at.

    The filename is sanitised: an upload name is caller-controlled and must not
    be able to escape the store via ``..`` or a separator.
    """
    safe = Path(filename or "upload").name
    return source_dir(source_id) / safe


def relative_path(path: Path) -> str:
    """Store-relative path, for recording on a source row."""
    try:
        return str(Path(path).resolve().relative_to(store_root().resolve()))
    except ValueError:
        return str(path)


def store_upload(source_id: str, filename: str, source: Path) -> Path:
    """Move a spooled upload into the store and return its final path."""
    target = retained_path(source_id, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


def resolve_stored(relative: str | None) -> Path | None:
    """Resolve a stored relative path back to an absolute one, or ``None``."""
    if not relative:
        return None
    candidate = (store_root() / relative).resolve()
    root = store_root().resolve()
    # Refuse a path that escapes the store, even if the record was tampered with.
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def delete_source(source_id: str) -> bool:
    """Remove a source's retained file. Returns whether anything was removed."""
    directory = source_dir(source_id)
    if not directory.is_dir():
        return False
    shutil.rmtree(directory, ignore_errors=True)
    return True


__all__ = [
    "DEFAULT_STORE_DIR",
    "delete_source",
    "relative_path",
    "resolve_stored",
    "retained_path",
    "source_dir",
    "store_root",
    "store_upload",
]
