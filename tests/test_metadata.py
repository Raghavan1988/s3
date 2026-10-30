"""
Unit tests for the METADATA PLANE (app/metadata.py), in isolation.

Focus: the prefix listing contract after it became an indexed key-range scan --
prefixes must match LITERALLY (no LIKE wildcards), a prefix equal to a whole key
is included, max_keys is honored -- plus the overwrite/orphan-GC behavior of
put_object.
"""
from __future__ import annotations

from app.metadata import Metadata, ObjectRecord


def _meta(tmp_path) -> Metadata:
    return Metadata(str(tmp_path / "metadata.db"))


def _rec(bucket: str, key: str, blob_id: str = "b") -> ObjectRecord:
    return ObjectRecord(
        bucket=bucket, key=key, blob_id=blob_id, size=1,
        etag="e", content_type="application/octet-stream",
        last_modified=Metadata.now_iso(),
    )


def _keys(meta, bucket, prefix="", max_keys=1000):
    return [r.key for r in meta.list_objects(bucket, prefix, max_keys)]


def test_prefix_range_drilldown(tmp_path):
    meta = _meta(tmp_path)
    meta.create_bucket("g")
    for k in ["a.jpg", "2024", "2024/cat.jpg", "2024/dog.jpg", "2025/x.jpg"]:
        meta.put_object(_rec("g", k))

    # Empty prefix lists everything, sorted by key.
    assert _keys(meta, "g") == ["2024", "2024/cat.jpg", "2024/dog.jpg", "2025/x.jpg", "a.jpg"]
    # "2024/" must NOT pull in the sibling key "2024" (which lacks the slash)...
    assert _keys(meta, "g", "2024/") == ["2024/cat.jpg", "2024/dog.jpg"]
    # ...but a prefix that equals a whole key DOES include that key.
    assert _keys(meta, "g", "2024") == ["2024", "2024/cat.jpg", "2024/dog.jpg"]


def test_prefix_special_chars_are_literal(tmp_path):
    # Under LIKE, '%' and '_' are wildcards; the range scan treats them literally.
    meta = _meta(tmp_path)
    meta.create_bucket("s")
    for k in ["a%b", "axb", "a_b", "aQb"]:
        meta.put_object(_rec("s", k))
    assert _keys(meta, "s", "a%") == ["a%b"]   # '%' is a plain character here
    assert _keys(meta, "s", "a_") == ["a_b"]   # '_' is a plain character here


def test_max_keys_limits_rows(tmp_path):
    meta = _meta(tmp_path)
    meta.create_bucket("m")
    for i in range(5):
        meta.put_object(_rec("m", f"k{i}"))
    assert _keys(meta, "m", "", max_keys=2) == ["k0", "k1"]


def test_put_object_reports_overwritten_blob(tmp_path):
    meta = _meta(tmp_path)
    meta.create_bucket("o")
    assert meta.put_object(_rec("o", "k", blob_id="first")) is None   # brand new
    old = meta.put_object(_rec("o", "k", blob_id="second"))           # overwrite
    assert old == "first"                                             # GC target
    assert meta.get_object("o", "k").blob_id == "second"              # row repointed


def test_delete_returns_orphan_then_none(tmp_path):
    meta = _meta(tmp_path)
    meta.create_bucket("d")
    meta.put_object(_rec("d", "k", blob_id="blob1"))
    assert meta.delete_object("d", "k") == "blob1"   # hand back bytes to reclaim
    assert meta.delete_object("d", "k") is None      # second delete: nothing left
