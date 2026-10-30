"""
The stateless FRONT-END.

This is the HTTP layer that ties the planes together. It receives requests,
calls the metadata plane (and, from Step 5, the data plane), and returns S3-shaped
responses. It holds no state of its own -- everything durable lives in the planes
-- which is exactly what lets real S3 run thousands of these behind a load balancer.

Addressing is PATH-STYLE:  http://host:9000/{bucket}/{key}
(Real S3 defaults to virtual-host style buckets.host; every local S3-compatible
server uses path-style, and so do we.)

This step serves BUCKETS only: create one, and list them all as S3 XML.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import format_datetime   # formats a datetime as an RFC-1123 HTTP date

from fastapi import FastAPI, Request    # Request lets us read the raw body + headers
from fastapi.responses import FileResponse, Response  # raw bodies, or stream a file

from . import errors                 # S3 error factories (no_such_bucket, ...)
from .errors import S3Error          # the exception type we register a handler for
from .metadata import Metadata, ObjectRecord
from .storage import BlobStore
from .xml import list_all_buckets, list_objects_v2  # S3 XML body builders

# Where the metadata database lives. We default to a `.s3data/` folder next to the
# project (it's git-ignored). An env var lets us override it, e.g. in tests.
DATA_DIR = os.environ.get(
    "S3_DATA_DIR",
    os.path.join(os.path.dirname(__file__), "..", ".s3data"),
)
os.makedirs(DATA_DIR, exist_ok=True)

# Instantiate the metadata plane once, at import time. A single long-lived index
# that every request shares. (The data plane gets added here in Step 5.)
meta = Metadata(os.path.join(DATA_DIR, "metadata.db"))

# Instantiate the data plane too, rooted in the same folder. Blobs land in
# <DATA_DIR>/blobs/. Both planes are shared by every request.
blobs = BlobStore(DATA_DIR)

@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup: nothing to do. Shutdown: close the metadata DB connection cleanly
    # so SQLite checkpoints the WAL and releases the file handle.
    yield
    meta.close()


# The FastAPI application object. Uvicorn looks for this (`app.main:app`) to run.
app = FastAPI(title="local-s3", lifespan=lifespan)

# Whenever any route raises S3Error, render it as an S3 <Error> XML document.
app.add_exception_handler(S3Error, errors.s3_exception_handler)
# Any OTHER uncaught exception becomes an S3 <Error><Code>InternalError</Code>
# (500) document too, so clients never get an unparseable JSON 500.
app.add_exception_handler(Exception, errors.unhandled_exception_handler)


def _http_date(iso: str) -> str:
    # Convert our STORED ISO-8601 timestamp ("2026-10-27T05:22:55.000Z") into the
    # HTTP-date format required by the Last-Modified header
    # ("Tue, 27 Oct 2026 05:22:55 GMT"). Crucially we format the object's *stored*
    # time -- NOT datetime.now() -- so repeated GETs report the real write time.
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    return format_datetime(dt, usegmt=True)


@app.get("/health")
async def health() -> Response:
    # Liveness probe for load balancers / orchestrators. Declared BEFORE the
    # GET /{bucket} route so "/health" isn't matched as a bucket name.
    return Response(status_code=200, content="ok", media_type="text/plain")


@app.get("/")
async def list_buckets() -> Response:
    # GET / -> list every bucket. Ask the metadata plane, format as S3 XML.
    # We set media_type so the response carries `Content-Type: application/xml`,
    # which is what S3 clients look for when deciding how to parse the body.
    body = list_all_buckets(meta.list_buckets())
    return Response(content=body, media_type="application/xml")


@app.get("/{bucket}")
async def list_objects(bucket: str, request: Request) -> Response:
    # GET /{bucket}?prefix=&delimiter=&max-keys= -> list objects (ListObjectsV2).
    # Clients also send list-type=2; we don't need to branch on it.
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)

    # Pull the listing controls out of the query string (with sane defaults).
    q = request.query_params
    prefix = q.get("prefix", "")
    delimiter = q.get("delimiter", "")
    # Parse max-keys defensively: a non-integer must not 500 the request, and S3
    # caps a page at 1000, so clamp into [0, 1000].
    try:
        max_keys = int(q.get("max-keys", "1000"))
    except ValueError:
        max_keys = 1000
    max_keys = max(0, min(max_keys, 1000))

    # Division of labor: the metadata plane filters by prefix + sorts by key;
    # the xml layer applies the delimiter grouping to produce folders.
    objects = meta.list_objects(bucket, prefix, max_keys)
    body = list_objects_v2(bucket, prefix, delimiter, max_keys, objects)
    return Response(content=body, media_type="application/xml")


@app.put("/{bucket}")
async def create_bucket(bucket: str) -> Response:
    # PUT /{bucket} -> create a bucket. FastAPI pulls `bucket` straight out of the
    # URL path for us. We ask the metadata plane to create it; we don't treat an
    # already-existing bucket as an error here (S3 is lenient about re-creating
    # a bucket you already own). Either way S3 answers 200 with a Location header
    # pointing at the new bucket.
    meta.create_bucket(bucket)
    return Response(status_code=200, headers={"Location": f"/{bucket}"})


@app.put("/{bucket}/{key:path}")
async def put_object(bucket: str, key: str, request: Request) -> Response:
    # PUT /{bucket}/{key} -> upload an object.
    #
    # The `{key:path}` converter lets the key contain slashes, so a key like
    # "photos/2024/cat.jpg" is captured whole (not just "photos"). Remember: that
    # slash is NOT a folder -- the whole string is one flat key.

    # Reject writes to a bucket that doesn't exist, with a proper S3 error.
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)

    # Note the content-type; the bytes are streamed below, not buffered.
    content_type = request.headers.get("content-type", "application/octet-stream")

    # ---- THE WRITE ORDER ----------------------------------------------------
    # 1) DATA PLANE FIRST: stream the request body straight to disk, chunk by
    #    chunk, so even a multi-gigabyte upload never sits whole in memory. The
    #    writer hashes (ETag) and counts bytes as it goes. If we crashed after
    #    commit, these bytes would be an orphan (harmless garbage); if the client
    #    drops mid-upload, we abort and leave nothing behind.
    writer = blobs.writer()
    try:
        async for chunk in request.stream():
            writer.write(chunk)
    except Exception:
        writer.abort()
        raise
    blob_id, etag, size = writer.commit()

    # 2) METADATA SECOND: commit the index row pointing at the new blob. THIS is
    #    the instant the object officially exists. put_object returns the OLD
    #    blob_id if this was an overwrite.
    rec = ObjectRecord(
        bucket=bucket, key=key, blob_id=blob_id, size=size,
        etag=etag, content_type=content_type, last_modified=Metadata.now_iso(),
    )
    old_blob_id = meta.put_object(rec)

    # 3) GC LAST: only now that the row points at the NEW blob is it safe to drop
    #    the OLD one. Doing this earlier could strand a concurrent reader that
    #    still sees the old row.
    if old_blob_id:
        blobs.delete(old_blob_id)

    # S3 returns 200 with the ETag header (quoted) on a successful PUT.
    return Response(status_code=200, headers={"ETag": f'"{etag}"'})


@app.get("/{bucket}/{key:path}")
async def get_object(bucket: str, key: str) -> Response:
    # GET /{bucket}/{key} -> download an object. The two-step read flow:
    #   step 1: metadata lookup
    rec = meta.get_object(bucket, key)
    if rec is None:
        # Nothing in the index. Distinguish a missing bucket from a missing key
        # (S3 returns different codes), and never touch the data plane.
        if not meta.bucket_exists(bucket):
            raise errors.no_such_bucket(bucket)
        raise errors.no_such_key(bucket, key)

    #   step 2: stream the bytes straight from disk instead of reading the whole
    #   object into memory -- this keeps memory flat even for very large objects.
    # Content-Type is set as an explicit header (not media_type) so the framework
    # echoes the stored value EXACTLY, without appending "; charset=utf-8".
    # FileResponse sets Content-Length from the file size automatically.
    return FileResponse(
        path=blobs.path(rec.blob_id),
        headers={
            "Content-Type": rec.content_type,       # exact stored value, unmodified
            "ETag": f'"{rec.etag}"',                # MD5, quoted
            "Last-Modified": _http_date(rec.last_modified),  # from the STORED time
        },
    )


@app.head("/{bucket}/{key:path}")
async def head_object(bucket: str, key: str) -> Response:
    # HEAD /{bucket}/{key} -> same as GET but headers only, no body. Step 1 only:
    # a pure metadata-plane call, so it's cheap no matter how big the object is.
    rec = meta.get_object(bucket, key)
    if rec is None:
        # HEAD responses carry no body, so we can only signal via the status code.
        return Response(status_code=404)

    # Empty body, but we explicitly set Content-Length to the object's real size
    # (what a GET *would* return). Because we provide Content-Length ourselves,
    # the framework won't overwrite it with 0.
    return Response(
        status_code=200,
        headers={
            "Content-Length": str(rec.size),
            "Content-Type": rec.content_type,
            "ETag": f'"{rec.etag}"',
            "Last-Modified": _http_date(rec.last_modified),
        },
    )


@app.delete("/{bucket}/{key:path}")
async def delete_object(bucket: str, key: str) -> Response:
    # DELETE /{bucket}/{key} -> remove an object.
    #   1) drop the index row first (object becomes invisible). delete_object
    #      hands back the orphaned blob_id, or None if the key wasn't there.
    orphan_blob_id = meta.delete_object(bucket, key)
    #   2) GC the bytes. If None, delete_object was a no-op -- fine.
    if orphan_blob_id:
        blobs.delete(orphan_blob_id)
    # S3 returns 204 No Content whether or not the key existed -> idempotent.
    return Response(status_code=204)


@app.delete("/{bucket}")
async def delete_bucket(bucket: str) -> Response:
    # DELETE /{bucket} -> remove a bucket, but only if it's empty.
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)
    if meta.bucket_object_count(bucket) > 0:
        # Guardrail: refuse to delete a bucket that still holds objects.
        raise errors.bucket_not_empty(bucket)
    meta.delete_bucket(bucket)
    return Response(status_code=204)


@app.head("/{bucket}")
async def head_bucket(bucket: str) -> Response:
    # HEAD /{bucket} -> does this bucket exist? (boto3's head_bucket). A pure
    # metadata-plane check; no body (HEAD), so we answer with the status alone.
    return Response(status_code=200 if meta.bucket_exists(bucket) else 404)
