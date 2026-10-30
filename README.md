# local-s3

A small, readable S3-compatible object store you can talk to with the real
`aws` CLI and `boto3`. It is built around S3's single core idea: separate the

- **metadata plane** — the index: names → where the bytes live, and
- **data plane** — the raw object bytes themselves.

Keys are logical pointers; the bytes are stored separately. Everything else
(listing, "folders", overwrite, delete) follows from that split.

## Layout

| File | Plane / role |
| --- | --- |
| `app/storage.py` | Data plane — a blob store: `put` / `get` / `delete` by opaque id |
| `app/metadata.py` | Metadata plane — SQLite index of buckets and objects |
| `app/xml.py` | S3 wire-format XML response bodies |
| `app/errors.py` | S3 `<Error>` documents so SDKs raise typed exceptions |
| `app/main.py` | Stateless HTTP front-end that ties the planes together |

Addressing is **path-style**: `http://host:9000/{bucket}/{key}`.

## Run

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn app.main:app --port 9000
```

The data directory defaults to `./.s3data/` (git-ignored); override it with the
`S3_DATA_DIR` environment variable.

Quick check with the AWS CLI:

```bash
aws --endpoint-url http://127.0.0.1:9000 s3 mb s3://demo
aws --endpoint-url http://127.0.0.1:9000 s3 cp ./file.txt s3://demo/file.txt
aws --endpoint-url http://127.0.0.1:9000 s3 ls s3://demo
```

## Test

The suite points the real `boto3` SDK at a throwaway server instance:

```bash
./.venv/bin/pytest -q
```

## Scope

Implemented: create/list buckets, PUT/GET/HEAD/DELETE objects, list with
prefix + delimiter, and the S3 error model.

Out of scope (for now): pagination, request-signature auth, multipart upload,
versioning, range GET, conditional requests, user metadata, copy-object,
lifecycle rules, and real replication / sharding.
