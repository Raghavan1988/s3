"""
XML response bodies. S3's wire format is XML (not JSON) — the AWS SDKs parse
these exact element names and the namespace, so we reproduce them faithfully.

The interesting function is list_objects_v2: it turns a flat, sorted list of
keys into the "folders + files" view clients expect, using prefix + delimiter.
That collapse is the entire trick behind S3 "directories".
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from .metadata import ObjectRecord

NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def list_all_buckets(buckets) -> str:
    items = "".join(
        f"<Bucket><Name>{escape(b['name'])}</Name>"
        f"<CreationDate>{escape(b['created_at'])}</CreationDate></Bucket>"
        for b in buckets
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListAllMyBucketsResult xmlns="{NS}">'
        "<Owner><ID>local</ID><DisplayName>local</DisplayName></Owner>"
        f"<Buckets>{items}</Buckets>"
        "</ListAllMyBucketsResult>"
    )


def list_objects_v2(
    bucket: str,
    prefix: str,
    delimiter: str,
    max_keys: int,
    objects: list[ObjectRecord],
) -> str:
    """Build a ListBucketResult.

    With no delimiter, every matching key is a <Contents> row (a flat list).

    With delimiter="/" we simulate folders: for each key, look at the part
    *after* the prefix. If that part contains the delimiter, we don't list the
    object — instead we emit the substring up to and including the first
    delimiter as a <CommonPrefixes> entry (a "folder"). Keys with no further
    delimiter are listed normally as <Contents> (the "files" in this folder).

    Example: prefix="photos/", delimiter="/", keys:
        photos/a.jpg           -> Contents (a.jpg is directly in the folder)
        photos/2024/cat.jpg    -> CommonPrefix "photos/2024/"  (a subfolder)
        photos/2024/dog.jpg    -> folds into the SAME CommonPrefix
    """
    contents_rows: list[str] = []
    common_prefixes: set[str] = set()

    for obj in objects:
        key = obj.key
        if delimiter:
            rest = key[len(prefix):]                 # part after the prefix
            idx = rest.find(delimiter)
            if idx != -1:
                # There's a delimiter past the prefix -> this is a "subfolder".
                common_prefixes.add(prefix + rest[: idx + len(delimiter)])
                continue
        contents_rows.append(
            "<Contents>"
            f"<Key>{escape(key)}</Key>"
            f"<LastModified>{escape(obj.last_modified)}</LastModified>"
            f'<ETag>"{obj.etag}"</ETag>'        # ETag is always wrapped in quotes
            f"<Size>{obj.size}</Size>"
            "<StorageClass>STANDARD</StorageClass>"
            "</Contents>"
        )

    cp_rows = "".join(
        f"<CommonPrefixes><Prefix>{escape(p)}</Prefix></CommonPrefixes>"
        for p in sorted(common_prefixes)
    )
    key_count = len(contents_rows) + len(common_prefixes)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="{NS}">'
        f"<Name>{escape(bucket)}</Name>"
        f"<Prefix>{escape(prefix)}</Prefix>"
        f"<Delimiter>{escape(delimiter)}</Delimiter>"
        f"<MaxKeys>{max_keys}</MaxKeys>"
        f"<KeyCount>{key_count}</KeyCount>"
        "<IsTruncated>false</IsTruncated>"  # MVP: no pagination yet (see README extensions)
        + "".join(contents_rows)
        + cp_rows
        + "</ListBucketResult>"
    )
