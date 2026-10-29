"""
The METADATA PLANE.

This is the index: the authoritative record of what exists. Every GET / HEAD /
LIST ultimately asks a question of this plane ("does this name exist, and what
does it point to?"). It is small, structured, and queried constantly.

We implement it with SQLite -- a real transactional SQL database that happens to
live in a single file with no separate server. In real S3 this role is played by
a massive, horizontally sharded, replicated key-value store partitioned by
(bucket, key). The technology is completely different; the *interface* -- "given
a name, find its record" -- is the same. That shared interface is the point.

This step implements BUCKETS only. The `objects` table comes in Step 5.
"""

# Lets newer type-hint syntax be treated as plain strings; harmless, portable.
from __future__ import annotations

import sqlite3                      # the embedded SQL database in Python's stdlib
import threading                   # for a lock, since the web server is multi-threaded
from dataclasses import dataclass  # a tidy container for an object's metadata row
from datetime import datetime, timezone  # to timestamp bucket creation in UTC


def _now_iso() -> str:
    # S3 reports timestamps in ISO-8601, always UTC, e.g. "2026-10-26T15:04:05.000Z".
    # We generate that exact shape so our output matches real S3 later.
    #   datetime.now(timezone.utc) -> current time, timezone-aware, in UTC
    #   strftime(...)              -> format it; we hard-code .000 ms and a 'Z' suffix
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _like_prefix(prefix: str) -> str:
    # Build a safe SQL LIKE pattern meaning "starts with prefix". In LIKE, '%'
    # and '_' are wildcards and '\' is our chosen escape char, so we escape any
    # of those that appear literally in the prefix, then append '%' to match the
    # rest of the key. (A key like "photos/" has none of these, but a user-chosen
    # prefix could, so we handle it.)
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


@dataclass
class ObjectRecord:
    # One object's metadata -- everything about it EXCEPT the bytes themselves.
    # `blob_id` is the pointer into the data plane; the rest is what we report to
    # clients (size, etag, content-type, when it was last written).
    bucket: str
    key: str
    blob_id: str          # pointer into the data plane (which blob holds the bytes)
    size: int             # number of bytes
    etag: str             # MD5 hex of the bytes (stored WITHOUT surrounding quotes)
    content_type: str     # e.g. "image/jpeg"; what we send back on GET
    last_modified: str    # ISO-8601 timestamp of this write


# The object columns in a fixed order. Queries select these explicitly (never
# SELECT *, which silently depends on table definition order) so they always
# match what _row_to_record expects. It's a constant, not user input, so
# interpolating it into SQL is safe.
_OBJECT_COLUMNS = "bucket, key, blob_id, size, etag, content_type, last_modified"


def _row_to_record(row: sqlite3.Row) -> ObjectRecord:
    # Build an ObjectRecord from a DB row -- defined once so get_object and
    # list_objects stay in sync if the object columns ever change.
    return ObjectRecord(
        bucket=row["bucket"], key=row["key"], blob_id=row["blob_id"],
        size=row["size"], etag=row["etag"], content_type=row["content_type"],
        last_modified=row["last_modified"],
    )


class Metadata:
    # The index. One instance wraps one SQLite database file.

    def __init__(self, db_path: str):
        # Open (or create, if missing) the SQLite database file at db_path.
        #
        # check_same_thread=False: by default SQLite forbids using a connection
        # from a thread other than the one that created it. Our web server
        # (Starlette/Uvicorn) may run request handlers on different worker
        # threads, so we disable that guard -- and take responsibility for
        # serializing access ourselves with the lock below.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)

        # By default a query returns plain tuples (row[0], row[1], ...). Setting
        # row_factory to sqlite3.Row lets us access columns BY NAME instead
        # (row["name"], row["created_at"]) -- far more readable and less fragile.
        self._conn.row_factory = sqlite3.Row

        # Performance / concurrency pragmas:
        #   WAL  -> readers and a writer can proceed concurrently (the default
        #           rollback journal blocks readers during a write);
        #   synchronous=NORMAL -> the safe, faster companion to WAL;
        #   busy_timeout -> a blocked write waits up to 5s instead of failing
        #           immediately with "database is locked".
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

        # A mutex. We hold it around WRITES so two threads can't interleave a
        # check-then-insert and both think they created the same bucket. SQLite
        # also locks internally, but this keeps our own logic atomic and simple.
        self._lock = threading.Lock()

        # Make sure the tables exist before anyone uses this object.
        self._init_schema()

    def _init_schema(self) -> None:
        # `with self._conn:` opens a transaction and COMMITS it automatically if
        # the block succeeds (or ROLLS BACK if it raises). It does NOT close the
        # connection -- just scopes a transaction.
        with self._conn:
            # CREATE TABLE IF NOT EXISTS -> safe to run on every startup; it only
            # creates the table the first time.
            #
            #   name TEXT PRIMARY KEY  -> the bucket name is the unique identity.
            #     PRIMARY KEY does two jobs at once:
            #       (1) enforces uniqueness  -- two rows can't share a name;
            #       (2) builds an index on it -- so lookups by name are fast.
            #   created_at TEXT NOT NULL -> the ISO timestamp; NOT NULL means it
            #     must always be provided (no missing creation times).
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    name        TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL
                )
                """
            )
            # The objects table: one row per stored object.
            #
            #   PRIMARY KEY (bucket, key) -> a COMPOSITE key. The identity of an
            #   object is the PAIR (bucket, key). This both enforces "one row per
            #   (bucket,key)" and indexes that pair for fast lookups. It is also
            #   conceptually the SHARD KEY: real S3 partitions the metadata store
            #   on (bucket, key) to spread keys across many machines.
            #
            #   blob_id is just a plain column here -- the pointer into the data
            #   plane. The objects table has no idea where/how the bytes are
            #   actually stored; it only holds the id.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS objects (
                    bucket        TEXT NOT NULL,
                    key           TEXT NOT NULL,
                    blob_id       TEXT NOT NULL,
                    size          INTEGER NOT NULL,
                    etag          TEXT NOT NULL,
                    content_type  TEXT NOT NULL,
                    last_modified TEXT NOT NULL,
                    PRIMARY KEY (bucket, key)
                )
                """
            )

    def create_bucket(self, name: str) -> bool:
        """Create a bucket. Returns True if newly created, False if it already existed."""
        # Hold the lock so the "does it exist? / insert it" pair is atomic: no
        # other thread can slip in between the check and the insert.
        with self._lock, self._conn:
            # Look for an existing row with this name.
            #   "SELECT 1 ..."  -> we don't care about column values, just existence.
            #   WHERE name = ?  -> the ? is a PLACEHOLDER. We pass the value
            #     separately as a tuple (name,). NEVER build SQL by string-
            #     concatenation -- placeholders prevent SQL-injection and quoting bugs.
            #   .fetchone()     -> returns the first row, or None if there are none.
            exists = self._conn.execute(
                "SELECT 1 FROM buckets WHERE name = ?", (name,)
            ).fetchone()

            if exists:
                return False  # already present -> nothing created

            # Insert the new bucket row with the current UTC timestamp.
            # Again, values go in via placeholders, matched positionally to the ?s.
            self._conn.execute(
                "INSERT INTO buckets (name, created_at) VALUES (?, ?)",
                (name, _now_iso()),
            )
            return True  # we created it

    def list_buckets(self) -> list[sqlite3.Row]:
        """Return all buckets as rows, sorted by name."""
        # A read -- no lock needed (SQLite handles concurrent reads fine).
        #   ORDER BY name -> deterministic, alphabetical output (what clients expect).
        #   .fetchall()   -> return every matching row as a list of sqlite3.Row.
        # Each row supports row["name"] and row["created_at"].
        return self._conn.execute(
            "SELECT name, created_at FROM buckets ORDER BY name"
        ).fetchall()

    def bucket_exists(self, name: str) -> bool:
        # Quick existence check -- used by the front-end to reject writes to a
        # bucket that was never created. A read, so no lock needed.
        return self._conn.execute(
            "SELECT 1 FROM buckets WHERE name = ?", (name,)
        ).fetchone() is not None

    def put_object(self, rec: ObjectRecord) -> str | None:
        """Insert or replace an object's index row.

        Returns the OLD blob_id if this write OVERWROTE an existing object, so
        the caller can garbage-collect the now-orphaned bytes. Returns None if
        this key is brand new.
        """
        # Lock so the read-old-then-upsert pair is atomic (same TOCTOU reasoning
        # as create_bucket), and wrap it in a transaction (`with self._conn`).
        with self._lock, self._conn:
            # First, find out whether this (bucket, key) already has a blob, so we
            # can tell the caller which old bytes to reclaim after we repoint the row.
            old = self._conn.execute(
                "SELECT blob_id FROM objects WHERE bucket = ? AND key = ?",
                (rec.bucket, rec.key),
            ).fetchone()

            # Upsert: insert a new row, or if (bucket,key) already exists, UPDATE it.
            #   ON CONFLICT(bucket, key) DO UPDATE SET ... -> SQLite's "upsert".
            #   excluded.<col> refers to the value we TRIED to insert, so this line
            #   overwrites every field with the new write's values.
            self._conn.execute(
                """
                INSERT INTO objects
                    (bucket, key, blob_id, size, etag, content_type, last_modified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket, key) DO UPDATE SET
                    blob_id       = excluded.blob_id,
                    size          = excluded.size,
                    etag          = excluded.etag,
                    content_type  = excluded.content_type,
                    last_modified = excluded.last_modified
                """,
                (rec.bucket, rec.key, rec.blob_id, rec.size,
                 rec.etag, rec.content_type, rec.last_modified),
            )
            # Committing this transaction is THE moment the object "exists" (or
            # points at its new bytes). Hand back the superseded blob_id, if any.
            return old["blob_id"] if old else None

    def get_object(self, bucket: str, key: str) -> ObjectRecord | None:
        """Look up one object's metadata. Returns the record, or None if absent."""
        # A plain read -- no lock. SELECT * grabs every column for this (bucket,key).
        row = self._conn.execute(
            f"SELECT {_OBJECT_COLUMNS} FROM objects WHERE bucket = ? AND key = ?",
            (bucket, key),
        ).fetchone()
        if row is None:
            return None  # no such object -> caller turns this into a 404
        return _row_to_record(row)

    def list_objects(self, bucket: str, prefix: str, max_keys: int) -> list[ObjectRecord]:
        """Return up to max_keys objects in a bucket whose key starts with prefix,
        sorted by key. The delimiter grouping is NOT done here -- that's a pure
        formatting concern handled in xml.list_objects_v2."""
        # key LIKE 'prefix%' -> starts-with match. ORDER BY key -> sorted output,
        # which is what makes delimiter grouping and (later) pagination work.
        rows = self._conn.execute(
            f"SELECT {_OBJECT_COLUMNS} FROM objects "
            r"WHERE bucket = ? AND key LIKE ? ESCAPE '\' ORDER BY key LIMIT ?",
            (bucket, _like_prefix(prefix), max_keys),
        ).fetchall()
        return [_row_to_record(r) for r in rows]

    def delete_object(self, bucket: str, key: str) -> str | None:
        """Delete an object's index row. Returns the now-orphaned blob_id so the
        caller can reclaim the bytes, or None if the key didn't exist."""
        # Lock so "read the blob_id, then delete the row" is one atomic unit.
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT blob_id FROM objects WHERE bucket = ? AND key = ?",
                (bucket, key),
            ).fetchone()
            if row is None:
                return None  # nothing to delete -> caller still returns 204 (idempotent)
            # Drop the row FIRST. After this commits, the object is gone (a GET
            # will 404). The blob it pointed at is now an orphan for the caller to GC.
            self._conn.execute(
                "DELETE FROM objects WHERE bucket = ? AND key = ?", (bucket, key)
            )
            return row["blob_id"]

    def bucket_object_count(self, name: str) -> int:
        """How many objects a bucket holds. Used to refuse deleting a non-empty one."""
        return self._conn.execute(
            "SELECT COUNT(*) AS n FROM objects WHERE bucket = ?", (name,)
        ).fetchone()["n"]

    def delete_bucket(self, name: str) -> None:
        """Remove a bucket row. The caller must have already checked it is empty."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM buckets WHERE name = ?", (name,))

    @staticmethod
    def now_iso() -> str:
        # A small public wrapper so other modules can reuse our timestamp format
        # without reaching for the private helper. (Used more in later steps.)
        return _now_iso()
