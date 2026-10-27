# Progress summary — local-s3

A running log of what we've built and learned, step by step. See `AGENTS.md`
for the full roadmap and the rules we build by, and `system-design.svg` for the
target architecture.

**Where we are:** Steps 0–4 done. Next up: **Step 5 — PUT object (wire both planes).**

---

## ✅ Step 0 — Setup

- Created an isolated **virtualenv** at `.venv/` (one venv per project; installs
  here never touch the anaconda base environment).
- Installed **FastAPI** (the framework — *what* happens per request: routing,
  parsing) and **Uvicorn** (the ASGI server — *runs* the app, opens the socket,
  speaks HTTP). They connect through the **ASGI** contract.
- Added `.gitignore` so `.venv/`, caches, and the future `.s3data/` never get
  committed. (Committed and pushed.)

**One-liner:** *FastAPI = your logic; Uvicorn = the HTTP engine that runs it.*

---

## ✅ Step 1 — The two-plane concept

An object is really two different things with opposite needs, so we store them
in two systems:

| | **Metadata plane** | **Data plane** |
|---|---|---|
| Holds | names + attributes (bucket, key, size, etag, ...) | the raw bytes |
| Size | tiny, structured | huge, opaque |
| Access | looked up / listed constantly | read/written whole |
| Needs | fast lookup, consistency | durability, throughput |

- The two are joined by a **`blob_id`** — a pointer stored in the metadata row
  that tells the data plane which bytes belong to the object.
- The data plane **never** knows buckets/keys → this is what makes S3's **flat
  namespace** real.

**Key takeaways:**
- `LIST` / `HEAD` touch **only** the metadata plane → cheap regardless of how big
  the objects are.
- The two planes **scale independently** (add storage without touching the index;
  shard the index without moving bytes).
- **Durability** (not losing bytes) is a data-plane job (replication). **Namespace
  consistency** (after PUT, a GET sees it) is a metadata-plane job.
- A **delete spans both planes** → risk of **orphan blobs** (bytes no row points to).

**Interfaces sketched:**
- Data plane: `put(bytes) -> blob_id`, `get(blob_id) -> bytes`, `delete(blob_id)`.
- Metadata plane: `create_bucket/list_buckets/delete_bucket`, and
  `put_object/get_object/delete_object/list_objects` keyed by `(bucket, key)`.

---

## ✅ Step 2 — Data plane (`app/storage.py`)

Built the `BlobStore`: `put / get / delete` by opaque id. (Heavily commented for
learning.) Verified in a shell: ETag matches MD5, bytes round-trip, delete is
idempotent, identical bytes → same ETag but different blob_ids.

**Three ideas learned:**

1. **UUID blob id** — a random, coordination-free name. Decouples the physical
   blob from any logical key. *(Alternative: content-hash naming → dedup, but
   needs refcounting — see below.)*
2. **`ETag = MD5(bytes)`** — S3's integrity checksum for a single-part upload,
   computed at write time. *(Multipart changes this to `md5-of-md5s-N` — later.)*
3. **Atomic write: tmp → fsync → rename** — prevents two failures:
   - *Torn reads / consistency*: writing in place truncates the real file and
     lets a concurrent `get()` read a half-written blob. Writing to `<id>.tmp`
     keeps the real name untouched until it's ready.
   - *Lost data / durability*: `write()` may leave bytes in an OS cache; `fsync`
     forces them to physical disk. Then `os.replace()` (atomic rename) is the
     **commit point** — the blob springs into existence fully formed.

**Design decisions made explicit:**
- **No dedup, by design.** Each object owns its own blob → deletes are simple
  ("delete the one blob"), no reference counting. Dedup would name blobs by
  content hash and require a refcount (delete bytes only when count hits 0).
- **`get()` may raise `FileNotFoundError`**, and that's fine: it's the **metadata
  plane's** responsibility to only ever hand out blob_ids that exist. If it ever
  fired in practice, that signals a cross-plane inconsistency (a bug), not normal
  operation.

---

## ✅ Step 3 — Metadata plane, buckets (`app/metadata.py`)

Built the `Metadata` index on **SQLite** (a real transactional SQL DB in a single
file — our stand-in for S3's sharded, replicated KV store). This step: the
`buckets` table plus `create_bucket` / `list_buckets`. Verified buckets persist
across a fresh reopen of the DB file ("the index is the source of truth").

**Ideas learned:**
- **The index is the source of truth** — a bucket exists iff there's a row here.
- **`name TEXT PRIMARY KEY` does two jobs:** enforces **uniqueness** *and* builds
  an **index** for fast lookups.
- **Parameterized queries** (`"... WHERE name = ?", (name,)`) prevent **SQL
  injection** (code/data kept separate) and handle quoting. Never concatenate.
- **Two different locks guard two different things:** SQLite's internal lock
  protects the *data*; our `threading.Lock` makes our *check-then-insert* atomic,
  avoiding a **TOCTOU race** (so a duplicate create returns `False` cleanly
  instead of throwing).
- `create_bucket` returns `True`/`False` (created / already existed) rather than
  raising — simpler for the HTTP layer to turn into a response.

## ✅ Step 4 — First HTTP surface (`app/main.py`)

A FastAPI front-end serving two bucket endpoints, with the metadata plane wired
in. Verified end-to-end with `curl`: `PUT /photos` → 200 + `Location`, `GET /` →
a real `ListAllMyBucketsResult` XML document (`Content-Type: application/xml`).

**Ideas learned:**
- **Path-style addressing:** `http://host/{bucket}/{key}` → `PUT /{bucket}`
  creates a bucket. (Real S3 defaults to virtual-host style; local servers use
  path-style.)
- **S3 speaks XML as a fixed protocol contract.** The SDKs have XML parsers
  hard-wired to these element names + namespace; returning JSON would make
  boto3's parser fail. We match the contract, we don't pick a format.
- `escape()` on names keeps the XML *valid* (`a&b` → `a&amp;b`) — separate issue
  from XML-vs-JSON.
- **The front-end is stateless.** All state lives in the planes, so any front-end
  instance can serve any request → scale by adding interchangeable instances
  behind a load balancer.
- Bucket ops never touch the data plane (no bytes involved).

## Files so far

```
.gitignore           committed
AGENTS.md            the build rules + roadmap
README.md            project intro (original)
system-design.svg    architecture diagram (implemented scope)
summary.md           this file
app/__init__.py      makes `app` a package
app/storage.py       Step 2 — the data plane (BlobStore)
app/metadata.py      Step 3 — the metadata plane (buckets)
app/main.py          Step 4 — the FastAPI front-end (bucket endpoints)
.venv/               local virtualenv (git-ignored)
```

## Next

**Step 5 — PUT object**: add the `objects` table to the metadata plane and wire
the front-end to both planes. The key concept is **write order** — store the
bytes in the data plane first, commit the index row second, then GC any
superseded blob (orphan = harmless garbage; dangling row = corruption).
