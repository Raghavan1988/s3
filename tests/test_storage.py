"""
Unit tests for the DATA PLANE (app/storage.py), in isolation.

These are fast, no-server tests of the blob store's contract: content-addressed
integrity (ETag == MD5), exact round-trips, idempotent deletes, independent ids
per write, and the streaming writer. They complement the boto3 integration
suite, which exercises the whole HTTP surface but not this plane on its own.
"""
from __future__ import annotations

import hashlib

import pytest

from app.storage import BlobStore


@pytest.fixture
def store(tmp_path):
    # tmp_path is pytest's per-test temp dir, so each test gets a clean blobs/.
    return BlobStore(str(tmp_path))


def test_put_returns_md5_etag_and_round_trips(store):
    data = b"hello object storage"
    blob_id, etag = store.put(data)
    assert etag == hashlib.md5(data).hexdigest()   # ETag is the MD5 of the bytes
    assert store.get(blob_id) == data              # bytes come back exactly


def test_each_put_gets_a_distinct_id(store):
    # Even identical bytes get a fresh id -- the data plane never dedupes or
    # mutates an existing blob; overwrite is the metadata plane's concern.
    id1, _ = store.put(b"same")
    id2, _ = store.put(b"same")
    assert id1 != id2


def test_empty_object_is_valid(store):
    blob_id, etag = store.put(b"")
    assert etag == hashlib.md5(b"").hexdigest()
    assert store.get(blob_id) == b""


def test_delete_is_idempotent(store):
    blob_id, _ = store.put(b"bye")
    store.delete(blob_id)
    store.delete(blob_id)               # deleting an absent blob must not raise
    store.delete("never-existed")       # nor must an unknown id
    with pytest.raises(FileNotFoundError):
        store.get(blob_id)              # gone for good


def test_streaming_writer_matches_whole_buffer_put(store):
    chunks = [b"ab", b"cd", b"ef"]
    whole = b"".join(chunks)
    w = store.writer()
    for c in chunks:
        w.write(c)
    blob_id, etag, size = w.commit()
    assert size == len(whole)
    assert etag == hashlib.md5(whole).hexdigest()  # incremental == one-shot MD5
    assert store.get(blob_id) == whole


def test_writer_abort_leaves_nothing_behind(store):
    w = store.writer()
    w.write(b"partial upload")
    w.abort()
    # The blob was never committed, so its id resolves to no file.
    with pytest.raises(FileNotFoundError):
        store.get(w._blob_id)
