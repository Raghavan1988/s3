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
from datetime import datetime, timezone  # to timestamp bucket creation in UTC


def _now_iso() -> str:
    # S3 reports timestamps in ISO-8601, always UTC, e.g. "2026-10-26T15:04:05.000Z".
    # We generate that exact shape so our output matches real S3 later.
    #   datetime.now(timezone.utc) -> current time, timezone-aware, in UTC
    #   strftime(...)              -> format it; we hard-code .000 ms and a 'Z' suffix
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


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

    @staticmethod
    def now_iso() -> str:
        # A small public wrapper so other modules can reuse our timestamp format
        # without reaching for the private helper. (Used more in later steps.)
        return _now_iso()
