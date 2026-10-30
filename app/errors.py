"""
The S3 error model.

Real S3 never returns a bare status with a plain-text body. It returns an XML
<Error> document whose <Code> the AWS SDKs parse to raise the RIGHT typed
exception (e.g. NoSuchKey -> botocore ClientError with Code "NoSuchKey"). To be
SDK-compatible we must emit exactly that shape.

Design: one S3Error exception type + factory helpers for the specific errors, and
a single handler (registered in main.py) that renders ANY S3Error to XML. Routes
just `raise` the right error; all the formatting lives here, in one place.
"""
from __future__ import annotations

import uuid
from xml.sax.saxutils import escape  # top-level stdlib xml, not app.xml

from fastapi import Request
from fastapi.responses import Response


class S3Error(Exception):
    # Carries everything we need to render an S3 <Error>: the machine-readable
    # Code, the HTTP status to send, a human Message, and the Resource path.
    def __init__(self, code: str, status: int, message: str, resource: str = ""):
        self.code = code
        self.status = status
        self.message = message
        self.resource = resource
        super().__init__(message)


# --- Factory helpers: one per error we actually raise -----------------------
# (Real S3 has ~100 codes; we implement the handful this build produces.)

def no_such_bucket(bucket: str) -> S3Error:
    return S3Error("NoSuchBucket", 404,
                   "The specified bucket does not exist.", f"/{bucket}")


def no_such_key(bucket: str, key: str) -> S3Error:
    return S3Error("NoSuchKey", 404,
                   "The specified key does not exist.", f"/{bucket}/{key}")


def bucket_not_empty(bucket: str) -> S3Error:
    return S3Error("BucketNotEmpty", 409,
                   "The bucket you tried to delete is not empty.", f"/{bucket}")


def internal_error(resource: str = "") -> S3Error:
    # The catch-all for anything unexpected. Real S3 answers a server-side fault
    # with <Code>InternalError</Code> and HTTP 500, which the SDKs retry; a bare
    # JSON 500 (FastAPI's default) would instead surface as an unparseable error.
    return S3Error("InternalError", 500,
                   "We encountered an internal error. Please try again.", resource)


# --- Rendering ---------------------------------------------------------------

def render_error(err: S3Error) -> Response:
    # Serialize an S3Error into the XML body the SDKs expect. Note there is NO
    # xmlns on <Error> (S3's error document is a bare element). We escape the
    # fields so odd characters can't break the XML. RequestId is just a random id.
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Error>"
        f"<Code>{escape(err.code)}</Code>"
        f"<Message>{escape(err.message)}</Message>"
        f"<Resource>{escape(err.resource)}</Resource>"
        f"<RequestId>{uuid.uuid4().hex}</RequestId>"
        "</Error>"
    )
    return Response(content=body, status_code=err.status, media_type="application/xml")


async def s3_exception_handler(_request: Request, exc: S3Error) -> Response:
    # FastAPI calls this whenever a route raises S3Error (we register it in main.py).
    return render_error(exc)


async def unhandled_exception_handler(request: Request, _exc: Exception) -> Response:
    # The safety net for any exception that ISN'T an S3Error: render it as an S3
    # InternalError document (not FastAPI's default JSON 500) so SDK clients still
    # get a parseable <Error>. Registered for Exception in main.py.
    return render_error(internal_error(request.url.path))
