"""
All SQLite operations for the marketplace scanner.

Tracks one row per (publisher, image, sku, region, architecture).
The `version` column always holds the LATEST version seen for that SKU.

check_and_upsert() returns one of:
  "new"       -> brand-new SKU; row inserted with validated='unknown'
  "updated"   -> existing SKU got a newer version; row updated, validation state PRESERVED
  "unchanged" -> SKU known at same/older version; only last_checked refreshed
"""

import logging
import os
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

NEW = "new"
UPDATED = "updated"
UNCHANGED = "unchanged"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _lazy_migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema, for in-place upgrades."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(images)").fetchall()}
    if not cols:
        return
    adds = []
    if "architecture" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN architecture TEXT NOT NULL DEFAULT 'x86_64'")
    if "family" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN family TEXT NOT NULL DEFAULT 'unknown'")
    if "distro_label" not in cols:
        adds.append("ALTER TABLE images ADD COLUMN distro_label TEXT NOT NULL DEFAULT ''")
    if adds:
        logger.warning(
            "Legacy schema detected — adding new columns. "
            "Delete the DB file for a fully-clean schema (the legacy UNIQUE "
            "constraint cannot be altered)."
        )
        for stmt in adds:
            conn.execute(stmt)
        conn.commit()


def initialize(db_path: str, schema_path: str) -> None:
    """Create the database from schema.sql (idempotent)."""
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with open(schema_path, "r") as fh:
        schema_sql = fh.read()

    conn = _connect(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
        _lazy_migrate(conn)
        logger.info("Database ready at %s", db_path)
    finally:
        conn.close()


def check_and_upsert(
    db_path: str,
    publisher: str,
    image: str,
    sku: str,
    version: str,
    region: str,
    architecture: str = "x86_64",
    family: str = "unknown",
    distro_label: str = "",
) -> str:
    """Upsert a SKU row, deduplicating across versions.

    Returns 'new', 'updated', or 'unchanged' (see module docstring).
    On 'updated': version + date_added + last_modified + last_checked are all
    set to now; validated state is preserved (per design).
    """
    now = _now_iso()
    conn = _connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, version FROM images
            WHERE publisher    = ?
              AND image        = ?
              AND sku          = ?
              AND region       = ?
              AND architecture = ?
            """,
            (publisher, image, sku, region, architecture),
        )
        row = cursor.fetchone()

        if row is None:
            cursor.execute(
                """
                INSERT INTO images
                    (publisher, image, sku, version, region, architecture,
                     family, distro_label,
                     date_added, last_modified, last_checked, validated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unknown')
                """,
                (publisher, image, sku, version, region, architecture,
                 family, distro_label, now, now, now),
            )
            conn.commit()
            logger.info(
                "New SKU: %s / %s / %s [%s, %s] v%s",
                publisher, image, sku, region, architecture, version,
            )
            return NEW

        # Existing SKU — compare versions lexicographically (marketplace
        # versions are zero-padded date-style: '24.04.202405010', '9.3.2023121113').
        if version > row["version"]:
            cursor.execute(
                """
                UPDATE images
                   SET version       = ?,
                       date_added    = ?,
                       last_modified = ?,
                       last_checked  = ?,
                       family        = ?,
                       distro_label  = ?
                 WHERE id = ?
                """,
                (version, now, now, now, family, distro_label, row["id"]),
            )
            conn.commit()
            logger.info(
                "Version bump: %s / %s / %s [%s, %s]  %s -> %s",
                publisher, image, sku, region, architecture, row["version"], version,
            )
            return UPDATED

        # Same or older version we've already seen.
        cursor.execute(
            "UPDATE images SET last_checked = ? WHERE id = ?",
            (now, row["id"]),
        )
        conn.commit()
        return UNCHANGED

    finally:
        conn.close()


def get_image_record(
    db_path: str,
    publisher: str,
    image: str,
    sku: str,
    region: str,
    architecture: str = "x86_64",
) -> dict:
    conn = _connect(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM images
            WHERE publisher    = ?
              AND image        = ?
              AND sku          = ?
              AND region       = ?
              AND architecture = ?
            """,
            (publisher, image, sku, region, architecture),
        )
        row = cursor.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def get_all_records(db_path: str) -> list[dict]:
    """Return every tracked image row as a list of dicts (for the distro rollup)."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM images ORDER BY publisher, distro_label, sku"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def distinct_distro_labels(db_path: str) -> set[str]:
    """Return the set of distro_label values currently tracked.

    Used to diff at the distro-release level: snapshot before a scan, compare
    after, and the difference is the set of brand-new OS releases to validate.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT distro_label FROM images").fetchall()
        return {r["distro_label"] for r in rows}
    finally:
        conn.close()
