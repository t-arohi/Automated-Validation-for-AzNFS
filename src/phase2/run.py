"""Live Phase 2 entry point (PMC **prod**, no PMC API).

Wires the public prod package client (``pmc_packages.ProdPackageIndex``) plus
Phase 1's existing notifier and universal database into
:func:`orchestrator.run_phase2`, and writes the Phase 3 hand-off artifact
``output/lisa_jobs.json``.

Design notes
------------
* Everything is read from the anonymous, public ``packages.microsoft.com`` -- no
  corp proxy, no PMC API, no ADO build, no onboarding metadata.
* The notifier is reused verbatim from Phase 1 (``scripts/notifier.py``) and DB
  verdicts go through Phase 1's universal ``db_manager`` -- this module only
  *adapts* the orchestrator's small Protocol surface onto those functions.
* Before the gates run, each image is enriched with its DB
  ``last_validated_version`` (so Gate 3 can decide if re-validation is needed),
  and any image parked ``pending_publish`` on a previous run is merged back in so
  it re-flows once the package finally appears on prod.
* Phase 1 modules are imported lazily (inside ``_load_phase1``) so tests can
  import this module, and inject fakes, without Phase 1 on the path.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

from . import orchestrator, pmc_packages

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration (env-overridable; defaults mirror Phase 1 / the spec)
# ---------------------------------------------------------------------------
PHASE2_INPUT = os.environ.get("PHASE2_INPUT", "output/needs_validation.json")
LISA_JOBS_OUTPUT = os.environ.get("LISA_JOBS_OUTPUT", "output/lisa_jobs.json")
DB_PATH = os.environ.get("DB_PATH", "marketplace.db")


def _identity(entry: dict) -> tuple[str, str, str, str, str]:
    return (
        entry.get("publisher", ""),
        entry.get("image") or entry.get("offer") or "",
        entry.get("sku", ""),
        entry.get("region", ""),
        entry.get("architecture") or entry.get("arch") or "",
    )


# ---------------------------------------------------------------------------
# Phase 1 adapters: map the orchestrator Protocols onto Phase 1's functions
# ---------------------------------------------------------------------------
class Phase1NotifierAdapter:
    """Adapt :class:`orchestrator.NotifierLike` onto Phase 1 ``scripts/notifier``.

    Phase 2 sends exactly one e-mail per run -- the end-of-run summary listing
    every distro and, for the failing ones, the reason. No per-distro mail.
    """

    def __init__(self, notifier_mod: Any) -> None:
        self._n = notifier_mod

    def notify_summary(self, processed, unsupported, pending_publish, trusted, to_phase3, errors) -> None:
        self._n.send_phase2_summary(
            processed=processed,
            unsupported=unsupported,
            pending_publish=pending_publish,
            to_phase3=to_phase3,
            trusted=trusted,
            errors=errors,
        )


class Phase1DbAdapter:
    """Adapt :class:`orchestrator.DbLike` onto Phase 1 ``db_manager.set_validation_state``."""

    def __init__(self, db_mod: Any, db_path: str) -> None:
        self._db = db_mod
        self._path = db_path

    def set_validation_state(self, identity, state) -> None:
        updated = self._db.set_validation_state(self._path, identity, state)
        if not updated:
            logger.warning("No DB row matched identity %s (state=%s)", identity, state)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------
def load_entries(path: str) -> list[dict]:
    """Load the Phase 1 hand-off (``needs_validation.json``)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list, got {type(data).__name__}")
    return data


def enrich_and_merge(entries: list[dict], db_mod: Any, db_path: str) -> list[dict]:
    """Add each image's DB ``last_validated_version`` and merge pending_publish rows.

    * Enrich: Gate 3 compares the latest prod version against what Phase 3 last
      validated; that baseline lives in the DB, so copy it onto each entry.
    * Merge: images parked ``pending_publish`` on a previous run are re-queued
      here even if Phase 1 did not re-emit them, so they re-flow once the package
      is finally published. De-duplicated against the incoming entries by identity.
    """
    out: list[dict] = []
    seen: set[tuple] = set()

    for e in entries:
        ident = _identity(e)
        seen.add(ident)
        v_last = e.get("last_validated_version", "")
        try:
            rec = db_mod.get_image_record(db_path, *ident)
            v_last = rec.get("last_validated_version", v_last) if rec else v_last
        except Exception:  # pragma: no cover - DB best-effort; entry default stands
            logger.debug("DB lookup failed for %s; using entry default", ident)
        out.append({**e, "last_validated_version": v_last or ""})

    try:
        for row in db_mod.get_rows_by_state(db_path, "pending_publish"):
            ident = (
                row.get("publisher", ""), row.get("image", ""), row.get("sku", ""),
                row.get("region", ""), row.get("architecture", ""),
            )
            if ident in seen:
                continue
            seen.add(ident)
            out.append(row)  # DB rows already carry family / distro_label / last_validated_version
    except Exception:  # pragma: no cover - re-entry is best-effort
        logger.exception("pending_publish merge skipped")

    return out


def _load_phase1():
    """Import Phase 1's notifier + db_manager (co-located in the repo)."""
    try:
        import notifier  # type: ignore
        import db_manager  # type: ignore
    except ModuleNotFoundError:  # pragma: no cover - exercised in the real repo
        from scripts import notifier  # type: ignore
        from scripts import db_manager  # type: ignore
    return notifier, db_manager


# ---------------------------------------------------------------------------
# Orchestration entry point
# ---------------------------------------------------------------------------
def run(
    *,
    entries: list[dict] | None = None,
    prod: Any | None = None,
    notifier_obj: Any | None = None,
    db: Any | None = None,
    lisa_jobs_path: str | None = None,
) -> list[dict]:
    """Wire the live prod client (or injected fakes) and run Phase 2.

    All collaborators are optional so tests can inject fakes; anything left
    ``None`` is constructed from the environment. When ``entries`` is ``None`` the
    Phase 1 hand-off is loaded and enriched/merged from the DB; tests pass an
    explicit ``entries`` list and skip the DB step.
    """
    if lisa_jobs_path is None:
        lisa_jobs_path = LISA_JOBS_OUTPUT

    notifier_mod = db_mod = None
    if notifier_obj is None or db is None or entries is None:
        notifier_mod, db_mod = _load_phase1()
    if notifier_obj is None:
        notifier_obj = Phase1NotifierAdapter(notifier_mod)
    if db is None:
        db = Phase1DbAdapter(db_mod, DB_PATH)
    if entries is None:
        entries = enrich_and_merge(load_entries(PHASE2_INPUT), db_mod, DB_PATH)
    if prod is None:
        logger.info("Using PMC prod content server %s", pmc_packages.PROD_BASE)
        prod = pmc_packages.from_env()

    jobs = orchestrator.run_phase2(
        entries=entries,
        prod=prod,
        db=db,
        notifier=notifier_obj,
        lisa_jobs_path=lisa_jobs_path,
    )
    logger.info("Phase 2 complete: %d LISA job(s) -> %s", len(jobs), lisa_jobs_path)
    return jobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2: validate AzNFS coverage on PMC prod.")
    parser.add_argument("--input", default=PHASE2_INPUT, help="Phase 1 needs_validation.json")
    parser.add_argument("--output", default=LISA_JOBS_OUTPUT, help="lisa_jobs.json output path")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve clients + input and report counts, but do not run gates")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        entries = load_entries(args.input)
    except (OSError, ValueError) as exc:
        logger.error("Cannot read Phase 1 input %s: %s", args.input, exc)
        return 2

    if args.dry_run:
        logger.info("Dry run: %d entr(ies) from %s", len(entries), args.input)
        return 0

    try:
        notifier_mod, db_mod = _load_phase1()
        entries = enrich_and_merge(entries, db_mod, DB_PATH)
        run(
            entries=entries,
            notifier_obj=Phase1NotifierAdapter(notifier_mod),
            db=Phase1DbAdapter(db_mod, DB_PATH),
            lisa_jobs_path=args.output,
        )
    except Exception:
        logger.exception("Phase 2 run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
