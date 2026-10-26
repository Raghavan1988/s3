"""
The stateless front-end: HTTP routing that ties the metadata plane and the data
plane together. In real S3 there are thousands of these behind a load balancer;
they hold no state themselves, which is what makes S3 horizontally scalable.

Addressing is PATH-STYLE:  http://host:9000/{bucket}/{key}
(Real S3 defaults to virtual-host style buckets.host, but every S3-compatible
local server uses path-style, and boto3 supports it via addressing_style='path'.)
"""
from __future__ import annotations

import os
from email.utils import formatdate

from fastapi import FastAPI, Request
from fastapi.responses import Response

from . import errors, xml
from .errors import S3Error
from .metadata import Metadata, ObjectRecord
from .storage import BlobStore

DATA_DIR = os.environ.get("S3_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", ".s3data"))
os.makedirs(DATA_DIR, exist_ok=True)

meta = Metadata(os.path.join(DATA_DIR, "metadata.db"))
blobs = BlobStore(DATA_DIR)

app = FastAPI(title="local-s3")
app.add_exception_handler(S3Error, errors.s3_exception_handler)


def _http_date() -> str:
    # Last-Modified must be an RFC-1123 HTTP date for the SDKs to parse it.
    return formatdate(usegmt=True)


# ─── Service: list all buckets ────────────────────────────────────────────
@app.get("/")
async def list_buckets() -> Response:
    body = xml.list_all_buckets(meta.list_buckets())
    return Response(content=body, media_type="application/xml")


# ─── Bucket operations ────────────────────────────────────────────────────
@app.put("/{bucket}")
async def create_bucket(bucket: str) -> Response:
    # Idempotent-ish: creating a bucket you already own is not an error in our MVP.
    meta.create_bucket(bucket)
    return Response(status_code=200, headers={"Location": f"/{bucket}"})


@app.head("/{bucket}")
async def head_bucket(bucket: str) -> Response:
    if not meta.bucket_exists(bucket):
        # HEAD has no body, so we can't send the XML error; status code only.
        return Response(status_code=404)
    return Response(status_code=200)


@app.delete("/{bucket}")
async def delete_bucket(bucket: str) -> Response:
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)
    if meta.bucket_object_count(bucket) > 0:
        raise errors.bucket_not_empty(bucket)       # S3 refuses to delete non-empty buckets
    meta.delete_bucket(bucket)
    return Response(status_code=204)


# ─── Bucket listing (GET /{bucket}?list-type=2) ───────────────────────────
@app.get("/{bucket}")
async def list_objects(bucket: str, request: Request) -> Response:
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)
    q = request.query_params
    prefix = q.get("prefix", "")
    delimiter = q.get("delimiter", "")
    max_keys = int(q.get("max-keys", "1000"))
    objs = meta.list_objects(bucket, prefix, max_keys)
    body = xml.list_objects_v2(bucket, prefix, delimiter, max_keys, objs)
    return Response(content=body, media_type="application/xml")


# ─── Object operations ────────────────────────────────────────────────────
@app.put("/{bucket}/{key:path}")
async def put_object(bucket: str, key: str, request: Request) -> Response:
    if not meta.bucket_exists(bucket):
        raise errors.no_such_bucket(bucket)

    data = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")

    # ORDER MATTERS: commit the bytes to the data plane FIRST...
    blob_id, md5 = blobs.put(data)
    rec = ObjectRecord(
        bucket=bucket, key=key, blob_id=blob_id, size=len(data),
        etag=md5, content_type=content_type, last_modified=Metadata.now_iso(),
    )
    # ...then commit the index row. If we crash between these two, we have an
    # orphan blob (garbage) — never a dangling index row (corruption).
    old_blob_id = meta.put_object(rec)
    if old_blob_id:
        blobs.delete(old_blob_id)                   # overwrite: GC the superseded bytes

    return Response(status_code=200, headers={"ETag": f'"{md5}"'})


@app.get("/{bucket}/{key:path}")
async def get_object(bucket: str, key: str) -> Response:
    rec = meta.get_object(bucket, key)
    if not rec:
        if not meta.bucket_exists(bucket):
            raise errors.no_such_bucket(bucket)
        raise errors.no_such_key(bucket, key)
    data = blobs.get(rec.blob_id)                   # metadata lookup, THEN fetch bytes
    return Response(
        content=data,
        media_type=rec.content_type,
        headers={"ETag": f'"{rec.etag}"', "Last-Modified": _http_date()},
    )


@app.head("/{bucket}/{key:path}")
async def head_object(bucket: str, key: str) -> Response:
    rec = meta.get_object(bucket, key)
    if not rec:
        return Response(status_code=404)
    # HEAD returns all the headers of a GET but no body — purely a metadata-plane call.
    return Response(
        status_code=200,
        headers={
            "ETag": f'"{rec.etag}"',
            "Content-Length": str(rec.size),
            "Content-Type": rec.content_type,
            "Last-Modified": _http_date(),
        },
    )


@app.delete("/{bucket}/{key:path}")
async def delete_object(bucket: str, key: str) -> Response:
    orphan = meta.delete_object(bucket, key)        # drop index row first...
    if orphan:
        blobs.delete(orphan)                        # ...then reclaim the bytes
    # S3 returns 204 even if the key didn't exist (delete is idempotent).
    return Response(status_code=204)
