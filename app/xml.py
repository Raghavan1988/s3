"""
S3 wire-format XML bodies.

S3's protocol is XML: the AWS SDKs parse these exact element names + namespace,
so we reproduce them faithfully. All the response-body builders live here, so the
front-end (main.py) stays focused on routing.

The interesting one is list_objects_v2: it turns a flat, sorted list of keys into
the "folders + files" view clients expect, using prefix + delimiter. That collapse
is the ENTIRE trick behind S3 "directories" -- they exist only in this function's
output, never in storage.
"""
from __future__ import annotations

from urllib.parse import quote        # for encoding-type=url key encoding
from xml.sax.saxutils import escape   # top-level stdlib xml, NOT this app.xml module

from .metadata import ObjectRecord

# The XML namespace every S3 response document declares. The SDKs check for it.
NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def _enc(value: str, encoding_type: str) -> str:
    # Render a key-ish value for the XML body. With encoding-type=url we first
    # percent-encode it, which makes keys containing XML-illegal control chars
    # (and other oddities) safe on the wire; boto3 URL-decodes them back when it
    # sees <EncodingType>url</EncodingType>. Either way we XML-escape the result.
    if encoding_type == "url":
        value = quote(value, safe="")
    return escape(value)


def list_all_buckets(rows) -> str:
    # Build the <ListAllMyBucketsResult> document (moved here from main.py).
    # `rows` is the list of sqlite3.Row from meta.list_buckets().
    bucket_items = "".join(
        f"<Bucket><Name>{escape(r['name'])}</Name>"
        f"<CreationDate>{escape(r['created_at'])}</CreationDate></Bucket>"
        for r in rows
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListAllMyBucketsResult xmlns="{NS}">'
        "<Owner><ID>local</ID><DisplayName>local</DisplayName></Owner>"
        f"<Buckets>{bucket_items}</Buckets>"
        "</ListAllMyBucketsResult>"
    )


def list_objects_v2(
    bucket: str,
    prefix: str,
    delimiter: str,
    max_keys: int,
    objects: list[ObjectRecord],
    encoding_type: str = "",
) -> str:
    """Build a <ListBucketResult> from a sorted list of matching objects.

    `objects` is already filtered to this bucket+prefix and sorted by key (the
    metadata plane did that). Here we only apply the DELIMITER grouping.

    `encoding_type` is the client's requested key encoding ("url" or ""); it only
    affects how key-ish values are rendered, not which ones are returned.
    """
    contents_rows: list[str] = []     # the "files" -> <Contents> entries
    common_prefixes: set[str] = set()  # the "folders" -> <CommonPrefixes>; a set dedupes

    for obj in objects:
        key = obj.key

        if delimiter:
            # Look at the part of the key AFTER the prefix.
            #   key   = "photos/2024/cat.jpg"
            #   prefix= "photos/"
            #   rest  = "2024/cat.jpg"
            rest = key[len(prefix):]
            idx = rest.find(delimiter)   # position of the first delimiter in `rest`
            if idx != -1:
                # There's a delimiter past the prefix -> this key lives in a
                # "subfolder". Emit the folder name = prefix + everything up to and
                # INCLUDING that first delimiter, e.g. "photos/" + "2024/" = "photos/2024/".
                # Many keys (cat.jpg, dog.jpg) collapse to the same string; the set
                # keeps just one. We do NOT list the object itself.
                common_prefixes.add(prefix + rest[: idx + len(delimiter)])
                continue

        # No delimiter requested, OR no delimiter in the remainder -> this key is a
        # "file" at this level: list it as a normal <Contents> entry.
        contents_rows.append(
            "<Contents>"
            f"<Key>{_enc(key, encoding_type)}</Key>"
            f"<LastModified>{escape(obj.last_modified)}</LastModified>"
            f'<ETag>"{obj.etag}"</ETag>'          # ETag is wrapped in literal quotes
            f"<Size>{obj.size}</Size>"
            "<StorageClass>STANDARD</StorageClass>"
            "</Contents>"
        )

    # Sort the folder names for stable, predictable output.
    cp_rows = "".join(
        f"<CommonPrefixes><Prefix>{_enc(p, encoding_type)}</Prefix></CommonPrefixes>"
        for p in sorted(common_prefixes)
    )

    # KeyCount = number of <Contents> + number of <CommonPrefixes> returned.
    key_count = len(contents_rows) + len(common_prefixes)

    # Real S3 includes <Delimiter> only when a delimiter was supplied; omit the
    # element entirely otherwise (rather than emitting an empty one).
    delimiter_el = f"<Delimiter>{_enc(delimiter, encoding_type)}</Delimiter>" if delimiter else ""

    # Echo <EncodingType> only when the client asked for one (same rule as S3).
    encoding_el = f"<EncodingType>{escape(encoding_type)}</EncodingType>" if encoding_type else ""

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<ListBucketResult xmlns="{NS}">'
        f"<Name>{escape(bucket)}</Name>"
        f"<Prefix>{_enc(prefix, encoding_type)}</Prefix>"
        f"{delimiter_el}"
        f"{encoding_el}"
        f"<MaxKeys>{max_keys}</MaxKeys>"
        f"<KeyCount>{key_count}</KeyCount>"
        # MVP: we never truncate yet, so always false. Pagination is a later extension.
        "<IsTruncated>false</IsTruncated>"
        + "".join(contents_rows)
        + cp_rows
        + "</ListBucketResult>"
    )
