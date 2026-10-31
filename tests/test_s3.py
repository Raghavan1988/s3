"""
Integration tests using the REAL AWS SDK (boto3), pointed at our local server.

If these pass, our server is wire-compatible with S3 for the operations covered:
boto3 signs the requests, parses our XML, reads our ETags/headers, and raises its
own typed exceptions from our <Error> documents -- exactly as it would against AWS.

The `s3` fixture starts the server as a subprocess on a test port with a throwaway
data directory, and tears it down at the end. Just run:

    ./.venv/bin/pytest -q
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import urllib.request

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

TEST_PORT = 9009
ENDPOINT = f"http://127.0.0.1:{TEST_PORT}"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _wait_until_up(url: str, timeout: float = 30.0) -> None:
    # Poll the server until it answers, so tests don't race startup.
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError("server did not start in time")


@pytest.fixture(scope="session")
def s3():
    # Launch our FastAPI app under uvicorn, in its own process, with an isolated
    # temp data dir so tests never touch the real .s3data.
    tmp = tempfile.mkdtemp(prefix="s3test-")
    env = {**os.environ, "S3_DATA_DIR": tmp}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(TEST_PORT)],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # Poll the dedicated /health liveness endpoint -- that's exactly what it
        # exists for. Waiting on "/" instead would list buckets (a metadata-plane
        # query) on every poll, so readiness would hinge on more than the server
        # merely being up.
        _wait_until_up(ENDPOINT + "/health")
        # The real AWS SDK client, aimed at our server.
        client = boto3.client(
            "s3",
            endpoint_url=ENDPOINT,
            aws_access_key_id="test",            # ignored by our server (no auth)
            aws_secret_access_key="test",
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"}),  # path-style for local
        )
        yield client
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_create_and_list_buckets(s3):
    s3.create_bucket(Bucket="alpha")
    s3.create_bucket(Bucket="beta")
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert "alpha" in names and "beta" in names


def test_put_get_roundtrip(s3):
    s3.create_bucket(Bucket="rt")
    body = b"hello object storage"
    put = s3.put_object(Bucket="rt", Key="greeting.txt", Body=body, ContentType="text/plain")
    assert put["ETag"].strip('"')                 # server returned an MD5 ETag

    got = s3.get_object(Bucket="rt", Key="greeting.txt")
    assert got["Body"].read() == body             # bytes round-trip exactly
    assert got["ContentType"] == "text/plain"     # content-type preserved
    assert got["ContentLength"] == len(body)


def test_empty_object_roundtrips(s3):
    # A zero-byte object is a real object: it must store, report size 0, and the
    # MD5 ETag of the empty string. Easy to get wrong if "no body" is mishandled.
    s3.create_bucket(Bucket="empty")
    put = s3.put_object(Bucket="empty", Key="zero", Body=b"")
    assert put["ETag"] == '"d41d8cd98f00b204e9800998ecf8427e"'   # MD5 of b""
    got = s3.get_object(Bucket="empty", Key="zero")
    assert got["Body"].read() == b""
    assert got["ContentLength"] == 0


def test_overwrite_replaces_bytes_and_etag(s3):
    # Re-PUTting the same key must replace the bytes and the ETag -- the metadata
    # row is repointed at the new blob and the old blob is garbage-collected.
    s3.create_bucket(Bucket="ow")
    first = s3.put_object(Bucket="ow", Key="k", Body=b"one")
    second = s3.put_object(Bucket="ow", Key="k", Body=b"two different")
    assert first["ETag"] != second["ETag"]            # new content -> new ETag
    got = s3.get_object(Bucket="ow", Key="k")
    assert got["Body"].read() == b"two different"     # latest write wins
    assert got["ContentLength"] == len(b"two different")


def test_head_object(s3):
    s3.create_bucket(Bucket="hd")
    s3.put_object(Bucket="hd", Key="f.bin", Body=b"12345")
    head = s3.head_object(Bucket="hd", Key="f.bin")
    assert head["ContentLength"] == 5
    # HEAD on a missing key -> boto3 raises (generic 404, since HEAD has no body)
    with pytest.raises(ClientError):
        s3.head_object(Bucket="hd", Key="missing")


def test_list_with_prefix_and_delimiter(s3):
    s3.create_bucket(Bucket="gallery")
    for key in ["a.jpg", "2024/cat.jpg", "2024/dog.jpg", "2025/x.jpg"]:
        s3.put_object(Bucket="gallery", Key=key, Body=b"x")

    resp = s3.list_objects_v2(Bucket="gallery", Delimiter="/")
    files = [c["Key"] for c in resp.get("Contents", [])]
    folders = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    assert files == ["a.jpg"]                      # only the file at top level
    assert folders == ["2024/", "2025/"]           # subfolders, collapsed

    # Drill into a "folder"
    resp2 = s3.list_objects_v2(Bucket="gallery", Prefix="2024/", Delimiter="/")
    assert [c["Key"] for c in resp2["Contents"]] == ["2024/cat.jpg", "2024/dog.jpg"]
    assert "CommonPrefixes" not in resp2


def test_delete_is_idempotent(s3):
    s3.create_bucket(Bucket="del")
    s3.put_object(Bucket="del", Key="tmp", Body=b"bye")
    s3.delete_object(Bucket="del", Key="tmp")
    s3.delete_object(Bucket="del", Key="tmp")      # second delete must not error
    with pytest.raises(s3.exceptions.NoSuchKey):
        s3.get_object(Bucket="del", Key="tmp")     # typed exception from our <Error>


def test_head_bucket(s3):
    s3.create_bucket(Bucket="hbucket")
    s3.head_bucket(Bucket="hbucket")               # exists -> no error
    with pytest.raises(ClientError):
        s3.head_bucket(Bucket="no-such-bucket")    # missing -> 404


def test_get_missing_key_raises_nosuchkey(s3):
    s3.create_bucket(Bucket="err")
    with pytest.raises(s3.exceptions.NoSuchKey):   # proves our <Code> is parsed
        s3.get_object(Bucket="err", Key="nope")


def test_delete_nonempty_bucket_fails(s3):
    s3.create_bucket(Bucket="guard")
    s3.put_object(Bucket="guard", Key="keep", Body=b"x")
    with pytest.raises(ClientError) as exc:
        s3.delete_bucket(Bucket="guard")
    assert exc.value.response["Error"]["Code"] == "BucketNotEmpty"
    # empty it, then the delete succeeds
    s3.delete_object(Bucket="guard", Key="keep")
    s3.delete_bucket(Bucket="guard")
