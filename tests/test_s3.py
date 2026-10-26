"""
Integration tests using the REAL AWS SDK (boto3) pointed at our local server.

If these pass, our server is wire-compatible with S3 for the operations covered:
boto3 signs requests, parses our XML, reads our ETags/headers — exactly as it
would against aws.amazon.com. Start the server first:

    ./.venv/bin/uvicorn app.main:app --port 9000

Then:   ./.venv/bin/pytest -q
"""
import boto3
import pytest
from botocore.client import Config

ENDPOINT = "http://127.0.0.1:9000"


@pytest.fixture(scope="module")
def s3():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="test",            # our server ignores auth (MVP) — any value works
        aws_secret_access_key="test",
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),  # path-style for local servers
    )


def test_bucket_lifecycle(s3):
    s3.create_bucket(Bucket="demo")
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "demo" in names


def test_put_get_roundtrip(s3):
    s3.create_bucket(Bucket="demo")
    body = b"hello object storage"
    put = s3.put_object(Bucket="demo", Key="greeting.txt", Body=body)
    assert put["ETag"].strip('"')                      # server returned an MD5 ETag

    got = s3.get_object(Bucket="demo", Key="greeting.txt")
    assert got["Body"].read() == body                  # bytes round-trip exactly


def test_head_missing_key_raises(s3):
    s3.create_bucket(Bucket="demo")
    with pytest.raises(s3.exceptions.ClientError):     # SDK parses our 404 into an exception
        s3.head_object(Bucket="demo", Key="does-not-exist")


def test_list_with_prefix_and_delimiter(s3):
    s3.create_bucket(Bucket="gallery")
    for key in ["photos/a.jpg", "photos/2024/cat.jpg", "photos/2024/dog.jpg", "readme.txt"]:
        s3.put_object(Bucket="gallery", Key=key, Body=b"x")

    # Folder-style listing: prefix="photos/", delimiter="/"
    resp = s3.list_objects_v2(Bucket="gallery", Prefix="photos/", Delimiter="/")
    files = [c["Key"] for c in resp.get("Contents", [])]
    folders = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]

    assert files == ["photos/a.jpg"]                   # only the file directly under photos/
    assert folders == ["photos/2024/"]                 # the subfolder, collapsed


def test_delete(s3):
    s3.create_bucket(Bucket="demo")
    s3.put_object(Bucket="demo", Key="tmp", Body=b"bye")
    s3.delete_object(Bucket="demo", Key="tmp")
    with pytest.raises(s3.exceptions.ClientError):
        s3.get_object(Bucket="demo", Key="tmp")
