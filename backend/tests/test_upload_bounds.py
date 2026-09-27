"""Upload size is bounded while the body is read, not after.

The regression: both non-point-cloud ingest routes did ``await file.read()``,
which materialises the entire request body as one ``bytes`` object, and only then
handed it to a service that checked ``len(raw) > MAX_UPLOAD_BYTES``. The cap was
therefore enforced *after* the allocation it was supposed to prevent, so it
bounded nothing: an unauthenticated caller could force a multi-gigabyte
allocation and be told 413 afterwards.

The point-cloud route was already correct -- it spools to disk and aborts
mid-stream -- which is why the fix is to make the other two paths match it rather
than to invent a new limit. These tests pin both halves: the new bounded reader,
and the unchanged behaviour of everything around it.
"""
from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.api.imports import UPLOAD_CHUNK_BYTES, read_bounded
from app.config import MAX_UPLOAD_BYTES
from app.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _geojson(features: int = 1) -> bytes:
    return json.dumps(
        {"type": "FeatureCollection", "features": [{"id": i} for i in range(features)]}
    ).encode()


# ==========================================================================
# the bounded reader itself
# ==========================================================================


def test_a_small_body_reads_back_unchanged():
    """Below the cap the reader must be indistinguishable from ``read()``."""

    class _Upload:
        def __init__(self, data: bytes) -> None:
            self.file = io.BytesIO(data)

    import anyio

    payload = _geojson(3)
    assert anyio.run(read_bounded, _Upload(payload)) == payload


def test_a_body_exactly_at_the_cap_is_accepted():
    """The cap is inclusive: ``== MAX_UPLOAD_BYTES`` is not over the limit."""
    import anyio

    body = b"x" * MAX_UPLOAD_BYTES

    class _Upload:
        def __init__(self) -> None:
            self.file = io.BytesIO(body)

    assert len(anyio.run(read_bounded, _Upload())) == MAX_UPLOAD_BYTES


def test_one_byte_over_the_cap_is_rejected():
    import anyio
    from fastapi import HTTPException

    body = b"x" * (MAX_UPLOAD_BYTES + 1)

    class _Upload:
        def __init__(self) -> None:
            self.file = io.BytesIO(body)

    with pytest.raises(HTTPException) as exc:
        anyio.run(read_bounded, _Upload())
    assert exc.value.status_code == 413


def test_the_reader_stops_reading_once_the_cap_is_passed():
    """The point of the fix: it must abort, not read the body and then measure.

    A ``SpooledTemporaryFile`` records how many bytes were actually pulled. If
    the reader drained a 40 MB body before raising, the peak allocation was still
    40 MB and the cap bounded nothing.
    """
    import anyio
    from fastapi import HTTPException

    oversize = 4 * MAX_UPLOAD_BYTES

    class _Counting(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.bytes_served = 0

        def read(self, size: int = -1) -> bytes:
            chunk = super().read(size)
            self.bytes_served += len(chunk)
            return chunk

    stream = _Counting(b"x" * oversize)

    class _Upload:
        def __init__(self) -> None:
            self.file = stream

    with pytest.raises(HTTPException) as exc:
        anyio.run(read_bounded, _Upload())
    assert exc.value.status_code == 413
    # It must have stopped shortly after the cap, not after the whole body.
    assert stream.bytes_served < oversize / 2, (
        f"read {stream.bytes_served} of {oversize} bytes before refusing; "
        "the body was buffered rather than bounded"
    )
    # And it must never have held more than the cap plus one chunk.
    assert stream.bytes_served <= MAX_UPLOAD_BYTES + UPLOAD_CHUNK_BYTES


def test_the_reader_never_calls_the_whole_body_read():
    """``UploadFile.read()`` is exactly the call that must not reappear.

    Checked with the AST rather than by scanning text: the function's docstring
    *names* ``file.read()`` in order to explain why it is not used, so a textual
    scan fails on its own explanation. An AST walk sees only real code.
    """
    import ast
    import inspect

    import app.api.imports as imports

    tree = ast.parse(inspect.getsource(read_bounded).lstrip())
    offending = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Await file.read() with no size argument.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "read"
            and isinstance(func.value, ast.Name)
            and func.value.id == "file"
            and not node.args
            and not node.keywords
        ):
            offending.append(node.lineno)
        # stream.read() with no size argument.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "read"
            and isinstance(func.value, ast.Name)
            and func.value.id == "stream"
            and not node.args
        ):
            offending.append(node.lineno)
    assert not offending, (
        f"read_bounded reads the whole body on line(s) {offending}; the chunked "
        "call must pass an explicit size"
    )

    # And the module must not reintroduce the old call site anywhere.
    module_tree = ast.parse(inspect.getsource(imports))
    for node in ast.walk(module_tree):
        if (
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "read"
            and not node.value.args
        ):
            raise AssertionError(
                f"line {node.lineno} awaits an unsized read(); the body would be "
                "materialised whole before the cap is checked"
            )


# ==========================================================================
# over HTTP
# ==========================================================================


def test_a_valid_geojson_still_uploads(client):
    r = client.post("/import/geojson", files={"file": ("a.geojson", _geojson(4), "application/geo+json")})
    assert r.status_code == 200
    assert r.json()["features"] == 4


def test_a_valid_csv_still_uploads(client):
    body = b"id,name\n1,parcel\n2,building\n"
    r = client.post("/import/source", files={"file": ("a.csv", body, "text/csv")})
    assert r.status_code == 200


def test_an_oversized_geojson_is_refused_with_413(client):
    body = b'{"features":[' + b"0" * (MAX_UPLOAD_BYTES + 10_000) + b"]}"
    r = client.post("/import/geojson", files={"file": ("b.geojson", body, "application/geo+json")})
    assert r.status_code == 413
    assert "maximum upload size" in r.json()["detail"].lower()


def test_an_oversized_csv_is_refused_with_413(client):
    body = b"id\n" + b"0" * (MAX_UPLOAD_BYTES + 10_000)
    r = client.post("/import/source", files={"file": ("b.csv", body, "text/csv")})
    assert r.status_code == 413


def test_a_body_just_under_the_cap_is_accepted_over_http(client):
    """Not over the limit, so it must not be refused for size."""
    filler = "0" * (MAX_UPLOAD_BYTES - 200)
    body = json.dumps({"type": "FeatureCollection", "features": [], "pad": filler}).encode()
    assert len(body) <= MAX_UPLOAD_BYTES
    r = client.post("/import/geojson", files={"file": ("c.geojson", body, "application/geo+json")})
    assert r.status_code == 200, r.text


# ==========================================================================
# behaviour that must NOT have changed
# ==========================================================================


def test_the_existing_service_level_limit_is_still_enforced(client):
    """``ingestion.py`` keeps its own post-read check; it is a second gate, not
    the only one. A caller reaching the service directly is still bounded."""
    from fastapi.testclient import TestClient as _TC

    with _TC(app) as c:
        r = c.post("/import/geojson", files={"file": ("a.geojson", _geojson(1), "application/geo+json")})
        assert r.status_code == 200


def test_unsupported_extensions_are_still_rejected(client):
    r = client.post("/import/source", files={"file": ("a.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400
    assert "supported inputs" in r.json()["detail"].lower()


def test_invalid_json_is_still_a_400(client):
    r = client.post("/import/geojson", files={"file": ("a.geojson", b"{not json", "application/geo+json")})
    assert r.status_code == 400
    assert "invalid json" in r.json()["detail"].lower()


def test_path_sanitisation_is_unchanged(client, tmp_path, monkeypatch):
    """A traversal-shaped upload name is still reduced to a basename."""
    from app.services import point_cloud_store as pc_store

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    body = _geojson(1)
    r = client.post(
        "/import/geojson",
        files={"file": ("../../escape.geojson", body, "application/geo+json")},
    )
    assert r.status_code == 200
    # Nothing escaped the store root.
    assert not (tmp_path / "escape.geojson").exists()
    assert not list(root.glob("**/escape.geojson")) or all(
        str(p).startswith(str(root)) for p in root.glob("**/escape.geojson")
    )


def test_the_point_cloud_path_is_untouched_by_the_new_reader(client, tmp_path, monkeypatch):
    """Point clouds still spool to disk and are retained, unchanged."""
    from app.services import point_cloud_store as pc_store
    from tests.fixtures.point_clouds import write_las

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    path = write_las(tmp_path / "scan.las", n_points=32)
    r = client.post(
        "/import/source",
        files={"file": ("scan.las", path.read_bytes(), "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_id"]
    assert body["point_count"] == 32
    # Retained on disk for extraction to re-read.
    retained = list(root.rglob("*.las"))
    assert retained, "the uploaded cloud must be retained, not deleted after ingest"


def test_the_point_cloud_path_still_enforces_its_own_cap(client, tmp_path, monkeypatch):
    """The streaming path keeps its mid-stream 413; the new reader did not
    replace it or widen the limit."""
    from app.services import point_cloud_store as pc_store
    from tests.fixtures.point_clouds import write_las

    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(pc_store, "DEFAULT_STORE_DIR", root)
    path = write_las(tmp_path / "big.las", n_points=32)
    # Pad the file past the cap without changing the format's early bytes.
    body = path.read_bytes() + b"\0" * (MAX_UPLOAD_BYTES + 10_000)
    r = client.post(
        "/import/source",
        files={"file": ("big.las", body, "application/octet-stream")},
    )
    assert r.status_code == 413
