# AGENTS.md — how we build local-s3 together

This project is built **by hand, one step at a time**, as a guided learning
exercise. The goal is for me (the human) to understand every file and every
design decision — **not** to get a finished product quickly.

`system-design.svg` shows the target architecture for the scope we're building.
`summary.md` is the running log of what's done and learned so far.

**Current status:** Steps 0–2 complete. Next: Step 3 (metadata plane).

---

## Ground rules for the assistant (re-read these every turn)

1. **One step at a time.** Do only the current step. Never jump ahead or
   implement a later step's feature early, even when it looks trivial.
2. **Teach before you type.** Before writing any code, explain in plain words:
   what we're about to build, why, and the key trade-off. Keep it short.
3. **Small, readable pieces.** Add the minimum code for the current step only.
4. **Stop and check.** After each step, verify it with me, then **pause and
   wait** for me to confirm or ask questions before starting the next step.
5. **Explain the "why", not just the "how".** Name the alternative you rejected.
6. **Let me drive the pace.** If unsure whether to continue, ask — don't assume.
7. **I write or approve each piece.** Offer the code and explain it; let me type
   it or accept it. Do not bulk-generate the whole repo.
8. Act as a practical, hands-on teacher.

---

## What we are building

An S3-compatible object store that the real `aws` CLI and `boto3` can talk to,
built on the single core idea that S3 separates:

- a **metadata plane** — the index: names → where the bytes live; and
- a **data plane** — the raw object bytes themselves.

Keys are logical pointers; the bytes are stored separately. Everything else
follows from that split.

---

## The build roadmap

Each step = **one concept + one small piece of code + one way to verify it.**
We tick the box together as we finish each one.

- [x] **Step 0 — Setup.** Create a virtualenv; install FastAPI + uvicorn (and
      boto3 for testing). Lay out the `app/` package. *Concept:* why we isolate
      dependencies instead of installing into the base environment.
- [x] **Step 1 — The two planes (concept only).** Understand and sketch the
      metadata/data split and the interface each side exposes — before any code.
- [x] **Step 2 — Data plane (`storage.py`).** A blob store: `put(bytes) → id`,
      `get(id)`, `delete(id)`. *Concepts:* flat namespace, `ETag = MD5(bytes)`,
      atomic write (tmp → fsync → rename). *Verify:* a few lines in a Python shell.
- [ ] **Step 3 — Metadata plane, buckets (`metadata.py`).** SQLite schema;
      create and list buckets. *Concept:* the index is the source of truth.
- [ ] **Step 4 — First HTTP surface (`main.py` + bucket XML).** A FastAPI app
      that creates a bucket and lists buckets as S3 XML. *Verify:* `curl`.
- [ ] **Step 5 — PUT object.** Add the `objects` table and wire both planes.
      *Concept:* write order — bytes first, index row second, then GC the old
      blob (orphan = harmless garbage; dangling row = corruption). *Verify:* `curl`.
- [ ] **Step 6 — GET + HEAD object.** Round-trip the bytes and the headers.
- [ ] **Step 7 — DELETE object + bucket.** Idempotent delete, orphan-blob GC,
      and refusing to delete a non-empty bucket.
- [ ] **Step 8 — List objects with prefix + delimiter (`xml.py`).** The
      "folders are only a view" trick: collapse keys into CommonPrefixes.
- [ ] **Step 9 — Error model (`errors.py`).** Return `<Error><Code>` XML so the
      SDKs raise the right typed exception instead of a generic failure.
- [ ] **Step 10 — Test with boto3 (`tests/`).** Point the real SDK at the running
      server and get a green suite. That is our definition of "it works".

---

## How we verify, by stage

- Early steps: a few lines in a Python shell.
- Once the server exists: `curl` against `http://127.0.0.1:9000`.
- End: `boto3` / `aws --endpoint-url` integration tests.

---

## Out of scope for now (we revisit these later, one at a time)

Pagination, request-signature auth, multipart upload, versioning, range GET,
conditional requests, user metadata, copy-object, lifecycle rules, and real
replication / sharding. We note them when relevant but do not build them yet.
