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

    def _fsync_dir(self) -> None:
        # fsync the BLOBS DIRECTORY itself. A rename (or unlink) only updates the
        # directory entry; that update can linger in the OS cache and be lost on a
        # power failure, leaving the blob "committed" in the file but absent from
        # the directory. Flushing the directory makes the create/delete durable.
        # On platforms where a directory can't be fsync'd we simply skip it.
        fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def writer(self) -> "BlobWriter":
        """Open a streaming writer for one new blob. The caller feeds bytes with
        .write(chunk) and finishes with .commit() -> (blob_id, etag, size). This
        lets the front-end stream a large upload straight to disk, hashing as it
        goes, without ever holding the whole object in memory."""
        return BlobWriter(self)

    def put(self, data: bytes) -> tuple[str, str]:
        """Store bytes durably. Returns (blob_id, etag) where etag = MD5 hex.

        A convenience wrapper over the streaming writer for callers that already
        hold the full bytes (tests, the Python shell). The durability guarantee is
        the writer's: temp file -> fsync -> atomic rename -> directory fsync, so a
        reader sees either no blob or the whole blob, never a half-written one.
        """
        w = self.writer()
        w.write(data)
        blob_id, etag, _size = w.commit()
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
            return  # already absent -> nothing to do (and nothing to flush)
        # Flush the directory so the removal is durable, same reasoning as put().
        self._fsync_dir()


class BlobWriter:
    """A single-use streaming writer for one blob.

    Usage:
        w = store.writer()
        for chunk in source:   # or: async for chunk in request.stream()
            w.write(chunk)
        blob_id, etag, size = w.commit()

    It writes to a temp file while updating the MD5 (the ETag) and a running byte
    count incrementally, so nothing is buffered in memory. .commit() fsyncs, does
    the atomic rename, and flushes the directory -- the same durability path as a
    whole-buffer put. If the upload fails midway, .abort() removes the temp file.
    """

    def __init__(self, store: BlobStore):
        self._store = store
        self._blob_id = uuid.uuid4().hex         # opaque id for these bytes
        self._final = store._path(self._blob_id)
        self._tmp = self._final + ".tmp"
        # usedforsecurity=False: this MD5 is an integrity checksum (the ETag),
        # not a security hash; it also keeps working on FIPS-restricted builds.
        self._md5 = hashlib.md5(usedforsecurity=False)
        self._size = 0
        self._f = open(self._tmp, "wb")          # write, binary; bytes pass as-is
        self._committed = False
        self._done = False                        # set once commit() or abort() ran

    def write(self, chunk: bytes) -> None:
        # Stream one chunk to disk and fold it into the hash + size as we go.
        # A writer is single-use: once committed or aborted its file is closed,
        # so writing more would either raise an opaque "I/O on closed file" or
        # silently corrupt an already-finished blob. Fail with a clear message.
        if self._done:
            raise ValueError("write() after the writer was committed or aborted")
        self._f.write(chunk)
        self._md5.update(chunk)
        self._size += len(chunk)

    def commit(self) -> tuple[str, str, int]:
        """Finish the blob durably and return (blob_id, etag, size)."""
        # Guard the single-use contract: committing twice would try to rename a
        # temp file that was already moved, raising a confusing FileNotFoundError.
        if self._done:
            raise ValueError("commit() called on an already-finished writer")
        self._done = True
        self._f.flush()                 # push Python's buffer to the OS
        os.fsync(self._f.fileno())      # force the OS to flush to physical disk
        self._f.close()
        # Atomic rename is THE commit point: before it the blob doesn't exist;
        # after it the whole blob exists. A reader never sees a partial file.
        os.replace(self._tmp, self._final)
        # Flush the directory entry so the rename survives a power failure too.
        self._store._fsync_dir()
        self._committed = True
        return self._blob_id, self._md5.hexdigest(), self._size

    def abort(self) -> None:
        """Discard a half-written blob (e.g. the client connection dropped)."""
        if self._committed:
            return
        # Mark the writer finished first, so any stray write() after this raises
        # clearly instead of appending to a file we're about to delete.
        self._done = True
        try:
            self._f.close()
        except OSError:
            pass
        try:
            os.remove(self._tmp)
        except FileNotFoundError:
            pass
