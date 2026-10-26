"""
The METADATA PLANE.

This is the interview headline: S3 keeps an *index* of every object separate
from the object's bytes. The index is small, consistent, and queried constantly
(every GET/HEAD/LIST hits it). The bytes are big and live in the data plane
(storage.py).

Here the index is SQLite. In real S3 it's a massive, horizontally sharded,
replicated key-value store partitioned by (bucket, key). The *interface* is the
same: given a bucket+key, find where the bytes are and what the object looks like.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


def _now_iso() -> str:
    # S3 list responses use this exact ISO-8601 shape, always UTC with milliseconds.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class ObjectRecord:
    bucket: str
    key: str
    blob_id: str        # pointer into the data plane
    size: int
    etag: str           # MD5 hex of the content (no quotes stored)
    content_type: str
    last_modified: str  # ISO-8601


class Metadata:
    """Thin SQLite-backed index. One connection, guarded by a lock for writes."""

    def __init__(self, db_path: str):
        # check_same_thread=False: uvicorn may call us from worker threads.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS buckets (
                    name        TEXT PRIMARY KEY,
                    created_at  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS objects (
                    bucket        TEXT NOT NULL,
                    key           TEXT NOT NULL,
                    blob_id       TEXT NOT NULL,
                    size          INTEGER NOT NULL,
                    etag          TEXT NOT NULL,
                    content_type  TEXT NOT NULL,
                    last_modified TEXT NOT NULL,
                    PRIMARY KEY (bucket, key)
                );
                """
            )

    # ---- buckets ---------------------------------------------------------
    def create_bucket(self, name: str) -> bool:
        """Returns True if created, False if it already existed."""
        with self._lock, self._conn:
            cur = self._conn.execute("SELECT 1 FROM buckets WHERE name = ?", (name,))
            if cur.fetchone():
                return False
            self._conn.execute(
                "INSERT INTO buckets (name, created_at) VALUES (?, ?)", (name, _now_iso())
            )
            return True

    def bucket_exists(self, name: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM buckets WHERE name = ?", (name,))
        return cur.fetchone() is not None

    def list_buckets(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT name, created_at FROM buckets ORDER BY name"
        ).fetchall()

    def bucket_object_count(self, name: str) -> int:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM objects WHERE bucket = ?", (name,))
        return cur.fetchone()["n"]

    def delete_bucket(self, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM buckets WHERE name = ?", (name,))

    # ---- objects ---------------------------------------------------------
    def put_object(self, rec: ObjectRecord) -> str | None:
        """Upsert the index row. Returns the OLD blob_id if we overwrote one
        (so the caller can garbage-collect the orphaned bytes)."""
        with self._lock, self._conn:
            old = self._conn.execute(
                "SELECT blob_id FROM objects WHERE bucket = ? AND key = ?",
                (rec.bucket, rec.key),
            ).fetchone()
            self._conn.execute(
                """
                INSERT INTO objects (bucket, key, blob_id, size, etag, content_type, last_modified)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bucket, key) DO UPDATE SET
                    blob_id=excluded.blob_id, size=excluded.size, etag=excluded.etag,
                    content_type=excluded.content_type, last_modified=excluded.last_modified
                """,
                (rec.bucket, rec.key, rec.blob_id, rec.size, rec.etag,
                 rec.content_type, rec.last_modified),
            )
            return old["blob_id"] if old else None

    def get_object(self, bucket: str, key: str) -> ObjectRecord | None:
        row = self._conn.execute(
            "SELECT * FROM objects WHERE bucket = ? AND key = ?", (bucket, key)
        ).fetchone()
        if not row:
            return None
        return ObjectRecord(
            bucket=row["bucket"], key=row["key"], blob_id=row["blob_id"],
            size=row["size"], etag=row["etag"], content_type=row["content_type"],
            last_modified=row["last_modified"],
        )

    def delete_object(self, bucket: str, key: str) -> str | None:
        """Returns the blob_id that is now orphaned, if the key existed."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT blob_id FROM objects WHERE bucket = ? AND key = ?", (bucket, key)
            ).fetchone()
            if not row:
                return None
            self._conn.execute(
                "DELETE FROM objects WHERE bucket = ? AND key = ?", (bucket, key)
            )
            return row["blob_id"]

    def list_objects(self, bucket: str, prefix: str, max_keys: int) -> list[ObjectRecord]:
        """Return up to max_keys objects whose key starts with prefix, sorted.
        Sorted order is what makes prefix/delimiter listing (and pagination) work."""
        rows = self._conn.execute(
            """
            SELECT * FROM objects
            WHERE bucket = ? AND key LIKE ? ESCAPE '\\'
            ORDER BY key
            LIMIT ?
            """,
            (bucket, _like_prefix(prefix), max_keys),
        ).fetchall()
        return [
            ObjectRecord(
                bucket=r["bucket"], key=r["key"], blob_id=r["blob_id"], size=r["size"],
                etag=r["etag"], content_type=r["content_type"], last_modified=r["last_modified"],
            )
            for r in rows
        ]

    @staticmethod
    def now_iso() -> str:
        return _now_iso()


def _like_prefix(prefix: str) -> str:
    """Build a safe LIKE pattern for 'starts with prefix', escaping LIKE wildcards."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"
