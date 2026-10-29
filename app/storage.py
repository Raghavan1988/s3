"""
The DATA PLANE.

Just bytes. A blob is stored under an opaque blob_id (a UUID we generate) and
retrieved by that same id. Notice what is deliberately NOT here: no buckets, no
keys, no listing, no search. The data plane has no idea what an object is called
-- that knowledge lives only in the metadata plane. This is what makes S3's
"flat namespace" real.

In real S3 a blob would be chunked, erasure-coded, and spread across many disks
in many racks/AZs for durability. Here we write one file. But the *interface*
(put/get/delete by opaque id) is exactly the seam where that machinery plugs in.
"""

# `from __future__ import annotations` makes type hints like `tuple[str, str]`
# work as plain text on older Python versions (they're not evaluated at runtime).
# Harmless on 3.11; a good habit so the file is portable.
from __future__ import annotations

import hashlib   # gives us MD5, which we use to compute the ETag of the bytes
import os        # filesystem calls: paths, fsync, rename, remove, makedirs
import uuid      # generates the random, globally-unique blob_id


class BlobStore:
    # A tiny object store: bytes in, bytes out, addressed by an opaque id.
    # One instance manages one directory full of blob files.

    def __init__(self, root: str):
        # `root` is the base folder the caller chose for all storage.
        # We keep every blob inside a `blobs/` subfolder of it, e.g. <root>/blobs/.
        self._dir = os.path.join(root, "blobs")  # build the path in an OS-safe way
        # Create that folder now so put() can assume it exists.
        # exist_ok=True => don't error if it's already there (safe to call repeatedly).
        os.makedirs(self._dir, exist_ok=True)

    def _path(self, blob_id: str) -> str:
        # Internal helper: given a blob_id, return the full file path for it.
        # The leading underscore signals "private; not part of the public API".
        return os.path.join(self._dir, blob_id)

    def put(self, data: bytes) -> tuple[str, str]:
        """Store bytes durably. Returns (blob_id, etag) where etag = MD5 hex.

        Write path: temp file -> fsync -> atomic rename. A reader therefore sees
        either no blob or the whole blob, never a half-written one.
        """
        # 1) Mint a fresh, random id for this blob. uuid4() is random (not based on
        #    time or MAC address); .hex is its 32-char lowercase form with no dashes,
        #    e.g. "4ec6360d99154cbb9bd982bc6b667111". Practically collision-free, so
        #    we need no central coordinator to hand out ids.
        blob_id = uuid.uuid4().hex

        # 2) Compute the ETag = hex MD5 of the exact bytes. This is what S3 returns
        #    for a single-part upload; clients use it to detect corruption. MD5 is
        #    fine here because we only need an integrity checksum, not cryptographic
        #    security.
        # usedforsecurity=False documents that this MD5 is an integrity checksum,
        # not a security hash, and keeps it working on FIPS-restricted builds.
        etag = hashlib.md5(data, usedforsecurity=False).hexdigest()

        # 3) Work out the final resting path for the blob, plus a temporary path we
        #    write to first. Writing straight to `final` would risk a reader seeing a
        #    half-written file if we crashed mid-write.
        final = self._path(blob_id)
        tmp = final + ".tmp"

        # 4) Write all the bytes into the TEMP file.
        #    "wb" = write, binary mode (no text encoding, bytes go through as-is).
        #    The `with` block guarantees the file is closed even if an error happens.
        with open(tmp, "wb") as f:
            f.write(data)          # copy the bytes into the OS's file buffer
            f.flush()              # push them from Python's buffer to the OS
            os.fsync(f.fileno())   # force the OS to flush its buffer to the physical
                                   # disk. Without this, bytes can linger in a cache
                                   # and be lost on a power failure. fileno() is the
                                   # low-level OS file descriptor fsync needs.

        # 5) Atomically swap the temp file into its final name. On POSIX, os.replace
        #    is a rename that either fully happens or doesn't -- there is no in-between
        #    state. THIS is the commit point: before it, the blob doesn't "exist"; after
        #    it, the whole blob exists. A concurrent reader never sees a partial file.
        os.replace(tmp, final)

        # 6) Hand the caller the id (so it can find these bytes later) and the etag
        #    (so it can report it / verify integrity). As a tuple, unpacked like:
        #    blob_id, etag = store.put(data)
        return blob_id, etag

    def get(self, blob_id: str) -> bytes:
        """Return the bytes for a blob_id. Raises FileNotFoundError if missing."""
        # Open the blob's file in read-binary mode and return its full contents.
        # If the id doesn't map to a real file, open() raises FileNotFoundError --
        # we let that propagate: it's the *metadata plane's* job to only ever give
        # us blob_ids that exist (see the quick-check discussion).
        with open(self._path(blob_id), "rb") as f:
            return f.read()

    def path(self, blob_id: str) -> str:
        """Public path to a blob, so the front-end can stream it straight from
        disk (via FileResponse) instead of reading it all into memory. The caller
        must know the blob exists (the metadata plane guarantees that)."""
        return self._path(blob_id)

    def delete(self, blob_id: str) -> None:
        """Reclaim a blob's bytes. Idempotent: deleting a missing blob is a no-op."""
        # Try to remove the file. If it's already gone, swallow the error so that
        # calling delete twice (or on an unknown id) is harmless -- "idempotent".
        # Idempotent deletes matter because retries happen in distributed systems,
        # and a retry of DELETE should never blow up.
        try:
            os.remove(self._path(blob_id))
        except FileNotFoundError:
            pass  # already absent -> nothing to do
