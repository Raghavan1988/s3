# local-s3 — an S3-compatible object store you can run on your laptop

A teaching implementation of AWS S3's core: a flat object store with an
HTTP/XML API that the **real `aws` CLI and `boto3` SDK** talk to unchanged.
Built to internalize the architecture for system-design interviews.

## The one big idea: two planes

S3 separates the **metadata plane** (the index: names → location) from the
**data plane** (the raw bytes). Keys are logical pointers; bytes live elsewhere.

| File | Role | Interview concept |
|------|------|-------------------|
| `app/metadata.py` | SQLite index of buckets/objects | sharded, replicated KV store partitioned by (bucket, key) |
| `app/storage.py`  | blobs on disk, named by UUID | erasure-coded chunks across disks/AZs; durability |
| `app/xml.py`      | S3 XML bodies + prefix/delimiter | the "flat namespace" / fake-folders trick |
| `app/errors.py`   | S3 `<Error>` XML codes | why SDKs raise the right exception |
| `app/main.py`     | FastAPI routes | stateless front-ends behind a load balancer |
| `tests/test_s3.py`| boto3 integration tests | proves wire-compatibility |

## Run it

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 9000
```

## Use it with the real AWS SDK

```python
import boto3
from botocore.client import Config
s3 = boto3.client("s3", endpoint_url="http://127.0.0.1:9000",
                  aws_access_key_id="test", aws_secret_access_key="test",
                  region_name="us-east-1",
                  config=Config(s3={"addressing_style": "path"}))
s3.create_bucket(Bucket="demo")
s3.put_object(Bucket="demo", Key="photos/cat.jpg", Body=b"...")
s3.list_objects_v2(Bucket="demo", Prefix="photos/", Delimiter="/")
```

Or the AWS CLI:

```bash
aws --endpoint-url http://127.0.0.1:9000 s3 mb s3://demo
aws --endpoint-url http://127.0.0.1:9000 s3 cp file.txt s3://demo/
aws --endpoint-url http://127.0.0.1:9000 s3 ls s3://demo/
```

## Test

```bash
./.venv/bin/pytest -q      # runs boto3 against the live server
```

## Implemented
- Buckets: create, list, head, delete (refuses non-empty)
- Objects: PUT, GET, HEAD, DELETE
- Listing v2 with prefix + delimiter (CommonPrefixes)
- Correct ETags (MD5), XML error model, atomic+fsync'd writes

## Extensions (the "how would you scale this" follow-ups)
- **Pagination**: `continuation-token` + `IsTruncated` (metadata plane: cursor on sorted keys)
- **SigV4 auth**: validate the AWS signature instead of ignoring it
- **Multipart upload**: for large objects; ETag becomes `md5(md5s)-N`
- **Versioning**: multiple blob_ids per key + version markers
- **Content-addressed dedup**: name blobs by content hash + refcount
- **Durability**: replicate each blob to N nodes / erasure-code it
