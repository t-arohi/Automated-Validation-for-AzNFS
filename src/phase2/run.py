"""Live Phase 2 entry point.

Wires the real clients (tux-dev repo index, onboarding, tux-dev package index,
AzNFS ADO build) plus Phase 1's *existing* notifier and *universal* database
into :func:`orchestrator.run_phase2`, and writes the Phase 3 hand-off artifact
``output/lisa_jobs.json``.

Design notes
------------
* The notifier is reused verbatim from Phase 1 (``scripts/notifier.py``) and the
  database verdicts are written through Phase 1's universal
  ``db_manager.set_validation_state`` -- this module only *adapts* the
  orchestrator's small Protocol surface onto those existing functions.
* Phase 1 modules are imported lazily (inside ``_load_phase1``) so that unit
  tests can import this module, and inject fakes, without Phase 1 on the path.
* Gate 4's ``packages.tux.csv`` is read from GitHub raw (public), so it does not
  require corp-network access; only Gate 1 (repo index) and Gate 3 (package
  listing) reach tux-dev. Both honour ``HTTPS_PROXY`` for the SOCKS tunnel.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

import requests

from . import ado_build, orchestrator, repo_index, tux_packages

logger = logging.getLogger(__name__)

# Sentinel distinguishing "argument omitted -> build from env" from an explicit
# ``None`` ("no client; e.g. ADO build disabled").
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Configuration (env-overridable; defaults mirror Phase 1 / the spec)
# ---------------------------------------------------------------------------
PHASE2_INPUT = os.environ.get("PHASE2_INPUT", "output/needs_validation.json")
LISA_JOBS_OUTPUT = os.environ.get("LISA_JOBS_OUTPUT", "output/lisa_jobs.json")
DB_PATH = os.environ.get("DB_PATH", "marketplace.db")

GITHUB_RAW_BASE = os.environ.get("GITHUB_RAW_BASE", "https://raw.githubusercontent.com")
AZNFS_REPO = os.environ.get("AZNFS_REPO", "Azure/AZNFS-mount")
PINNED_BRANCH = os.environ.get("PINNED_BRANCH", "main")
PACKAGES_TUX_CSV_PATH = os.environ.get("PACKAGES_TUX_CSV_PATH", "packages.tux.csv")
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

# Explicit version fallback; the runner normally uses the auto-incrementing
# counter (orchestrator.next_aznfs_version) so each build gets 0.3.0, 0.3.1, ...
AZNFS_PACKAGE_VERSION = os.environ.get("AZNFS_PACKAGE_VERSION", "")

# ADO MI client id used for the pre-flight connectivity ping.
ADO_MI_CLIENT_ID = os.environ.get("ADO_MI_CLIENT_ID", "") or None


# ---------------------------------------------------------------------------
# Phase 1 adapters: map the orchestrator Protocols onto Phase 1's functions
# ---------------------------------------------------------------------------
class Phase1NotifierAdapter:
    """Adapt :class:`orchestrator.NotifierLike` to Phase 1 ``scripts/notifier``.

    Phase 1's summary mail separates *unsupported* (label + reason) from
    *trusted* / *to_phase3*; the orchestrator only hands the summary plain label
    lists, so this adapter records the per-label reasons and the trusted labels
    as they happen and reconstructs the richer summary at the end.
    """

    def __init__(self, notifier_mod: Any) -> None:
        self._n = notifier_mod
        self._reasons: dict[str, str] = {}
        self._trusted: list[str] = []

    def notify_actionable(self, distro_label: str, message: str) -> None:
        self._reasons[distro_label] = message
        self._n.send_phase2_failure(distro_label, message)

    def notify_trusted(self, distro_label: str, message: str) -> None:
        self._trusted.append(distro_label)
        self._n.send_phase2_trusted(distro_label)

    def notify_summary(self, processed: int, unsupported: list[str], to_phase3: list[str]) -> None:
        unsupported_pairs = [(lbl, self._reasons.get(lbl, "")) for lbl in unsupported]
        # to_phase3 from the orchestrator includes the trusted labels; split them
        # back out so the mail distinguishes "freshly built" from "trusted".
        trusted = [lbl for lbl in to_phase3 if lbl in self._trusted]
        fresh = [lbl for lbl in to_phase3 if lbl not in self._trusted]
        self._n.send_phase2_summary(
            processed=processed,
            unsupported=unsupported_pairs,
            to_phase3=fresh,
            trusted=trusted,
        )


class Phase1DbAdapter:
    """Adapt :class:`orchestrator.DbLike` to Phase 1 ``db_manager.set_validation_state``.

    The orchestrator passes a ``date_added`` timestamp it computes; Phase 1 sets
    ``last_checked`` itself, so that argument is intentionally dropped here. The
    identity tuple order ``(publisher, image, sku, region, architecture)`` is
    identical on both sides.
    """

    def __init__(self, db_mod: Any, db_path: str) -> None:
        self._db = db_mod
        self._path = db_path

    def set_validation_state(self, identity, validated, reason, date_added) -> None:  # noqa: ARG002
        # The reason is delivered by e-mail (via the notifier), not persisted in
        # the DB, so it is intentionally not forwarded here.
        updated = self._db.set_validation_state(self._path, identity, validated)
        if not updated:
            logger.warning("No DB row matched identity %s (state=%s)", identity, validated)


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


def fetch_packages_tux_csv(session: requests.Session | None = None) -> str:
    """Read ``packages.tux.csv`` from the pinned AzNFS branch on GitHub raw.

    Public endpoint -- no corp network required.
    """
    url = f"{GITHUB_RAW_BASE}/{AZNFS_REPO}/{PINNED_BRANCH}/{PACKAGES_TUX_CSV_PATH}"
    sess = session or requests.Session()
    resp = sess.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def _load_phase1():
    """Import Phase 1's notifier + db_manager (co-located in the repo).

    Imported lazily so this module is importable (and testable with fakes)
    without Phase 1 on the path.
    """
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
    onboarding: Any | None = None,
    package_index: Any | None = None,
    ado: Any = _UNSET,
    notifier_obj: Any | None = None,
    db: Any | None = None,
    csv_text: str | None = None,
    index: Any | None = None,
    aznfs_version: str | None = None,
    lisa_jobs_path: str | None = None,
) -> list[dict]:
    """Wire the live clients (or injected fakes) and run Phase 2.

    All collaborators are optional so tests can inject fakes; anything left
    ``None`` is constructed from the environment.
    """
    if entries is None:
        entries = load_entries(PHASE2_INPUT)
    if lisa_jobs_path is None:
        lisa_jobs_path = LISA_JOBS_OUTPUT

    if notifier_obj is None or db is None:
        notifier_mod, db_mod = _load_phase1()
        notifier_obj = notifier_obj or Phase1NotifierAdapter(notifier_mod)
        db = db or Phase1DbAdapter(db_mod, DB_PATH)

    if index is None:
        logger.info("Fetching tux-dev repo index (Gate 1)...")
        index = repo_index.fetch()
    if onboarding is None:
        onboarding = orchestrator_onboarding_from_env()
    if package_index is None:
        package_index = tux_packages.from_env()
    if ado is _UNSET:
        ado = ado_build.from_env()
    if csv_text is None:
        logger.info("Fetching packages.tux.csv from GitHub raw (Gate 4)...")
        csv_text = fetch_packages_tux_csv()

    jobs = orchestrator.run_phase2(
        entries=entries,
        repo_index=index,
        onboarding=onboarding,
        package_index=package_index,
        db=db,
        notifier=notifier_obj,
        packages_tux_csv_text=csv_text,
        aznfs_version=aznfs_version if aznfs_version is not None else AZNFS_PACKAGE_VERSION,
        ado=ado,
        ado_client_id=ADO_MI_CLIENT_ID,
        lisa_jobs_path=lisa_jobs_path,
    )
    logger.info("Phase 2 complete: %d LISA job(s) -> %s", len(jobs), lisa_jobs_path)
    return jobs


def orchestrator_onboarding_from_env():
    """Build the live onboarding client (separate fn so it is easy to monkeypatch)."""
    from .onboarding_client import from_env
    return from_env()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 2: validate + publish AzNFS to tux-dev.")
    parser.add_argument("--input", default=PHASE2_INPUT, help="Phase 1 needs_validation.json")
    parser.add_argument("--output", default=LISA_JOBS_OUTPUT, help="lisa_jobs.json output path")
    parser.add_argument("--dry-run", action="store_true",
                        help="resolve clients + CSV and report counts, but do not run gates")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        entries = load_entries(args.input)
    except (OSError, ValueError) as exc:
        logger.error("Cannot read Phase 1 input %s: %s", args.input, exc)
        return 2

    if args.dry_run:
        logger.info("Dry run: %d entr(ies) from %s", len(entries), args.input)
        return 0

    try:
        run(entries=entries, lisa_jobs_path=args.output)
    except Exception:
        logger.exception("Phase 2 run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
