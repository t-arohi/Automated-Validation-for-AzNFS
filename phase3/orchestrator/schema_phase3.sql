-- Phase 3 addition to the Phase 1 `images` table.
--
-- Records the post-validation outcome:
--   last_validated  : ISO8601 UTC time of the last LISA validation
--
-- Phase 3 is LISA testing only (no PMC prod query), so the previous
-- `pmc_prod_state` column was removed. The validated state itself
-- (known_supported / known_unsupported) lives in the existing `validated`
-- column written via the shared identity.
--
-- SQLite has no "ADD COLUMN IF NOT EXISTS"; the orchestrator applies this
-- defensively and ignores the "duplicate column" error, so it is safe to
-- (re)run.

ALTER TABLE images ADD COLUMN last_validated TEXT;
