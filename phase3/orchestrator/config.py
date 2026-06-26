"""
Configuration for Phase 3 (record-result orchestration).

Phase 3 is LISA testing only - there is NO PMC prod query, so this is now a
thin config: where the shared SQLite DB lives and where the Phase 3 schema
fragment is. Notifications reuse the Phase 1 ACS notifier (scripts/notifier.py),
which reads its own recipients/endpoint from scripts/config.py.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))

# Same SQLite DB Phase 1/Phase 2 use (images table); Phase 3 records the verdict.
DB_PATH = os.environ.get("DB_PATH", os.path.join(_PROJECT_ROOT, "marketplace.db"))

# Phase 3 schema fragment (adds the last_validated column; applied defensively).
PHASE3_SCHEMA_PATH = os.path.join(_THIS_DIR, "schema_phase3.sql")
