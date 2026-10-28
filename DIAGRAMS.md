# local-s3 — Diagrams (revision sheet)

Three views of the solution, for remembering it end to end. (Diagrams are
Mermaid; GitHub renders them inline. `system-design.svg` is a static alternative.)

- [1. System design](#1-system-design-architecture)
- [2. Sequence diagrams](#2-sequence-diagrams) — PUT, GET/HEAD, LIST, DELETE
- [3. UML class diagram](#3-uml-class-diagram)
- [Invariants to remember](#invariants-to-remember)

---

## 1. System design (architecture)

The one big idea: **two planes joined by a `blob_id` pointer.** The front-end is
stateless; all state lives in the planes.

```mermaid
flowchart TB
    CLI["AWS CLI / boto3<br/>path-style, signed requests"]

    subgraph FE["Front-end · FastAPI · app/main.py (stateless, scale horizontally)"]
        direction TB
        RT["Routes<br/>GET / · PUT/GET/DELETE /{bucket}<br/>PUT/GET/HEAD/DELETE /{bucket}/{key}"]
        XML["app/xml.py<br/>S3 XML response bodies"]
        ERR["app/errors.py<br/>Error + Code documents"]
    end

    subgraph MP["Metadata plane · app/metadata.py · SQLite (the index)"]
        BK[("buckets<br/>name PK, created_at")]
        OB[("objects<br/>(bucket,key) PK<br/>blob_id, size, etag,<br/>content_type, last_modified")]
    end

    subgraph DP["Data plane · app/storage.py · filesystem (dumb bytes)"]
        BLOB["blobs/{uuid} = raw bytes<br/>write: tmp → fsync → rename"]
    end

    CLI -->|"HTTP + XML"| RT
    RT --> XML
    RT --> ERR
    RT -->|"lookup / commit index (names)"| MP
    RT -->|"store / fetch bytes"| DP
    OB -. "blob_id points to" .-> BLOB
```

| Plane | File | Holds | Scales via |
|------|------|-------|-----------|
| Metadata | `metadata.py` | names → `blob_id` + attrs | shard on `(bucket,key)` |
| Data | `storage.py` | raw bytes by UUID | add storage nodes; replicate/erasure-code |

---

## 2. Sequence diagrams

### 2a. PUT object — the write-order rule

**Bytes first, index row second, GC last.** (orphan blob = safe garbage; dangling row = corruption)

```mermaid
sequenceDiagram
    actor C as Client boto3
    participant F as Front-end
    participant D as Data plane
    participant M as Metadata plane

    C->>F: PUT /{bucket}/{key} (bytes)
    F->>M: bucket_exists(bucket)?
    alt bucket missing
        M-->>F: false
        F-->>C: 404 NoSuchBucket (Error XML)
    else bucket exists
        M-->>F: true
        Note over F,D: 1. DATA PLANE FIRST
        F->>D: put(bytes)
        D-->>F: blob_id, etag (after fsync + atomic rename)
        Note over F,M: 2. METADATA SECOND = the commit point
        F->>M: put_object(record)
        M-->>F: old_blob_id or None
        opt was an overwrite
            Note over F,D: 3. GC old blob AFTER row repointed
            F->>D: delete(old_blob_id)
        end
        F-->>C: 200, ETag
    end
```

### 2b. GET / HEAD object — the two-step read

```mermaid
sequenceDiagram
    actor C as Client
    participant F as Front-end
    participant M as Metadata plane
    participant D as Data plane

    C->>F: GET /{bucket}/{key}
    F->>M: get_object(bucket, key)
    alt not found
        M-->>F: None
        F-->>C: 404 NoSuchKey / NoSuchBucket (Error XML)
    else found
        M-->>F: record (blob_id, size, etag, type, mtime)
        F->>D: get(blob_id)
        D-->>F: bytes
        F-->>C: 200 + bytes (Content-Type, ETag, Last-Modified)
    end
    Note over F,M: HEAD = step 1 only (metadata), headers, NO body fetch
```

### 2c. LIST objects — folders as a read-time view

```mermaid
sequenceDiagram
    actor C as Client
    participant F as Front-end
    participant M as Metadata plane
    participant X as xml.py

    C->>F: GET /{bucket}?prefix=P delimiter=/
    F->>M: list_objects(bucket, prefix, max_keys)
    M-->>F: rows matching prefix, sorted by key
    F->>X: list_objects_v2(prefix, delimiter, rows)
    Note over X: for each key, look after prefix:<br/>has delimiter → CommonPrefix (set dedups)<br/>no delimiter → Contents (a file)
    X-->>F: ListBucketResult XML
    F-->>C: 200 (Contents = files, CommonPrefixes = folders)
```

### 2d. DELETE object — row first, then GC

```mermaid
sequenceDiagram
    actor C as Client
    participant F as Front-end
    participant M as Metadata plane
    participant D as Data plane

    C->>F: DELETE /{bucket}/{key}
    Note over F,M: 1. drop the row FIRST (object vanishes)
    F->>M: delete_object(bucket, key)
    M-->>F: orphan blob_id or None
    opt object existed
        Note over F,D: 2. GC the bytes AFTER the row is gone
        F->>D: delete(orphan blob_id)
    end
    F-->>C: 204 (idempotent — 204 even if absent)
```

---

## 3. UML class diagram

```mermaid
classDiagram
    class ObjectRecord {
        +str bucket
        +str key
        +str blob_id
        +int size
        +str etag
        +str content_type
        +str last_modified
    }

    class Metadata {
        -Connection _conn
        -Lock _lock
        +create_bucket(name) bool
        +bucket_exists(name) bool
        +list_buckets() rows
        +bucket_object_count(name) int
        +delete_bucket(name) None
        +put_object(rec) old_blob_id
        +get_object(bucket, key) ObjectRecord
        +list_objects(bucket, prefix, max_keys) list
        +delete_object(bucket, key) orphan_blob_id
    }

    class BlobStore {
        -str _dir
        +put(data) blob_id_and_etag
        +get(blob_id) bytes
        +delete(blob_id) None
    }

    class S3Error {
        +str code
        +int status
        +str message
        +str resource
    }

    class FrontEnd {
        +list_buckets()
        +create_bucket(bucket)
        +list_objects(bucket, request)
        +put_object(bucket, key, request)
        +get_object(bucket, key)
        +head_object(bucket, key)
        +delete_object(bucket, key)
        +delete_bucket(bucket)
    }

    FrontEnd --> Metadata : uses for names
    FrontEnd --> BlobStore : uses for bytes
    FrontEnd ..> ObjectRecord : builds
    FrontEnd ..> S3Error : raises
    Metadata ..> ObjectRecord : returns
    ObjectRecord ..> BlobStore : blob_id points into
```

`FrontEnd` = the FastAPI routes in `app/main.py`; XML bodies come from `app/xml.py`.

---

## Invariants to remember

1. **Two planes.** Metadata = names/index (SQLite); Data = bytes (files). Joined
   by `blob_id`. Only metadata understands `(bucket, key)`.
2. **Flat namespace.** A key like `2024/cat.jpg` is one opaque string. No folders
   are stored; the `/` is just a character.
3. **Write order (PUT):** bytes → index row → GC old blob. *Orphan blob = safe
   garbage; dangling row = corruption.* The metadata commit is when the object
   "exists."
4. **Delete order:** drop the row first, then GC the bytes (same invariant, flipped).
   Delete is **idempotent** → always `204`.
5. **Reads:** GET touches both planes; HEAD and LIST touch **only** metadata →
   cheap regardless of object size.
6. **Folders are a read-time view.** `delimiter` groups flat keys into
   `CommonPrefixes` per request (a `set` dedups); nothing hierarchical is stored.
7. **Durability** is a data-plane job (fsync + atomic rename → later replication).
   **Namespace consistency** is a metadata-plane job (commit the row last).
8. **Compatibility is a wire contract:** exact S3 XML + `Error`/`Code` + quoted
   ETags + unmodified Content-Type → the real boto3 SDK just works.
```
