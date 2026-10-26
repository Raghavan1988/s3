"""
The DATA PLANE.

Just bytes. Objects are stored as opaque blobs named by a blob_id (a UUID the
metadata plane hands out). Note what is NOT here: no notion of buckets, no notion
of keys, no directory tree. That is the whole point of S3's "flat namespace" —
the key "photos/2024/cat.jpg" is a *string*, not a path. Hierarchy is an illusion
reconstructed at list time (see xml.py / main.py delimiter handling).

In real S3 a blob is split into chunks, erasure-coded, and spread across many
disks in many racks/AZs for durability (the famous "11 nines"). Here we just
write one file and rename it into place — but the interface (put/get/delete by
opaque id) is the same seam where that machinery would plug in.
"""
from __future__ import annotations

import hashlib
import os
import uuid


class BlobStore:
    def __init__(self, root: str):
        self._dir = os.path.join(root, "blobs")
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, blob_id: str) -> str:
        return os.path.join(self._dir, blob_id)

    def put(self, data: bytes) -> tuple[str, str]:
        """Write bytes durably. Returns (blob_id, md5_hex).

        We write to a temp file then os.replace() it into place: that rename is
        atomic on POSIX, so a reader never sees a half-written blob. This is a
        tiny taste of how storage systems guarantee you never read torn data.
        """
        blob_id = uuid.uuid4().hex
        md5 = hashlib.md5(data).hexdigest()  # S3's ETag for a single-part upload
        final = self._path(blob_id)
        tmp = final + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())  # force bytes to disk before we commit the rename
        os.replace(tmp, final)
        return blob_id, md5

    def get(self, blob_id: str) -> bytes:
        with open(self._path(blob_id), "rb") as f:
            return f.read()

    def delete(self, blob_id: str) -> None:
        try:
            os.remove(self._path(blob_id))
        except FileNotFoundError:
            pass  # already gone; delete is idempotent
