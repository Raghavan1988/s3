"""
S3 error model.

Real S3 never returns a bare HTTP status — it returns an XML <Error> body with a
machine-readable <Code>. The AWS SDKs parse that Code to raise the right client
exception (e.g. NoSuchKey -> botocore ClientError). So to be SDK-compatible we
MUST emit this exact shape, not FastAPI's default JSON {"detail": ...}.
"""
from __future__ import annotations

import uuid
from xml.sax.saxutils import escape

from fastapi import Request
from fastapi.responses import Response


class S3Error(Exception):
    """An S3-style error. Carries the <Code>, HTTP status, and message."""

    def __init__(self, code: str, status: int, message: str, resource: str = ""):
        self.code = code
        self.status = status
        self.message = message
        self.resource = resource
        super().__init__(message)


# The canonical S3 error codes we use. (There are ~100 in real S3.)
def no_such_bucket(bucket: str) -> S3Error:
    return S3Error("NoSuchBucket", 404, "The specified bucket does not exist.", f"/{bucket}")


def no_such_key(bucket: str, key: str) -> S3Error:
    return S3Error("NoSuchKey", 404, "The specified key does not exist.", f"/{bucket}/{key}")


def bucket_already_exists(bucket: str) -> S3Error:
    return S3Error(
        "BucketAlreadyOwnedByYou", 409,
        "Your previous request to create the named bucket succeeded and you already own it.",
        f"/{bucket}",
    )


def bucket_not_empty(bucket: str) -> S3Error:
    return S3Error("BucketNotEmpty", 409, "The bucket you tried to delete is not empty.", f"/{bucket}")


def invalid_bucket_name(bucket: str) -> S3Error:
    return S3Error("InvalidBucketName", 400, "The specified bucket is not valid.", f"/{bucket}")


def render_error(err: S3Error) -> Response:
    """Serialize an S3Error to the XML body the SDKs expect."""
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
    return render_error(exc)
