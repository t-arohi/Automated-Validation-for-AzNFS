-- Schema for marketplace image tracking database.
-- This file is the source of truth; the Python script creates the DB from this on first run.
--
-- Uniqueness: one row per (publisher, image, sku, region, architecture).
-- The `version` column tracks the LATEST version seen for that SKU; older versions
-- never get their own rows (see db_manager.check_and_upsert for dedup logic).

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    publisher     TEXT    NOT NULL,
    image         TEXT    NOT NULL,   -- Azure SDK "offer" field (e.g. 0001-com-ubuntu-server-focal)
    sku           TEXT    NOT NULL,   -- Azure SDK "sku"   field (e.g. 20_04-lts-gen2)
    version       TEXT    NOT NULL,   -- Latest known version  (e.g. 20.04.202405010)
    region        TEXT    NOT NULL,   -- Azure region          (e.g. eastus)
    architecture  TEXT    NOT NULL DEFAULT 'x86_64',
                                      -- x86_64 | arm64
    family        TEXT    NOT NULL DEFAULT 'unknown',
                                      -- apt | yum   (package manager kind, drives Phase 2 install commands)
    distro_label  TEXT    NOT NULL DEFAULT '',
                                      -- Human-readable label (e.g. "Ubuntu 24.04", "RHEL 9")
    date_added    TEXT    NOT NULL,   -- ISO8601 UTC; set on insert AND on version bump
    last_modified TEXT    NOT NULL,   -- ISO8601 UTC, updated when version changes
    last_checked  TEXT    NOT NULL,   -- ISO8601 UTC, updated on every scan run
    validated     TEXT    NOT NULL DEFAULT 'unknown',
                                      -- unknown           : not yet handed to Phase 2/3
                                      -- known_supported   : passed Phase 3 LISA test cases
                                      -- known_unsupported : failed at some phase (reason e-mailed)
    UNIQUE(publisher, image, sku, region, architecture)
);

CREATE INDEX IF NOT EXISTS idx_validated    ON images(validated);
CREATE INDEX IF NOT EXISTS idx_region       ON images(region);
CREATE INDEX IF NOT EXISTS idx_publisher    ON images(publisher);
CREATE INDEX IF NOT EXISTS idx_architecture ON images(architecture);
CREATE INDEX IF NOT EXISTS idx_family       ON images(family);
