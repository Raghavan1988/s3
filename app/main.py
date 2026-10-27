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
from xml.sax.saxutils import escape   # escapes &, <, > so names can't break our XML

from fastapi import FastAPI
from fastapi.responses import Response  # lets us return raw bodies + custom content-type

from .metadata import Metadata

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

# The FastAPI application object. Uvicorn looks for this (`app.main:app`) to run.
app = FastAPI(title="local-s3")

# The XML namespace every S3 response document declares. The SDKs check for it.
S3_NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _list_all_buckets_xml(rows) -> str:
    # Build the exact <ListAllMyBucketsResult> document the S3 clients expect.
    # `rows` is the list of sqlite3.Row from meta.list_buckets().
    #
    # For each bucket we emit <Bucket><Name>..</Name><CreationDate>..</CreationDate></Bucket>.
    # escape(...) turns a name like "a&b" into "a&amp;b" so it can't corrupt the XML.
    bucket_items = "".join(
        f"<Bucket><Name>{escape(r['name'])}</Name>"
        f"<CreationDate>{escape(r['created_at'])}</CreationDate></Bucket>"
        for r in rows
    )
    # The Owner block is required by the schema; we use a placeholder identity
    # since we don't model users/accounts in this build.
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListAllMyBucketsResult xmlns="{S3_NS}">'
        "<Owner><ID>local</ID><DisplayName>local</DisplayName></Owner>"
        f"<Buckets>{bucket_items}</Buckets>"
        "</ListAllMyBucketsResult>"
    )


@app.get("/")
async def list_buckets() -> Response:
    # GET / -> list every bucket. Ask the metadata plane, format as S3 XML.
    # We set media_type so the response carries `Content-Type: application/xml`,
    # which is what S3 clients look for when deciding how to parse the body.
    body = _list_all_buckets_xml(meta.list_buckets())
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
