"""Chain watchdog: alert when a phase ran but the next phase never triggered.

Phase 2 fires on Phase 1's ``workflow_run``; Phase 3 fires on Phase 2's. That
event is best-effort: if GitHub drops it (or the conclusion gate is false), the
downstream phase silently never starts and no phase e-mail is sent. This script
runs on its own schedule and, assuming the phases themselves are healthy, checks
the two hand-offs and e-mails once per detected gap:

  * Phase 1 (the scheduled daily scan) has not run at all within SCAN_STALE_MIN.
  * Phase 1 succeeded but no Phase 2 run followed within GRACE.
  * Phase 2 succeeded but no Phase 3 run followed within GRACE.

The first check covers the case the cron *itself* was dropped (GitHub scheduled
runs are best-effort and can be delayed for hours or skipped entirely), so the
whole pipeline silently produces nothing -- no scan, no hand-off, no e-mail.
Note this watchdog is *also* a GitHub cron, so it cannot catch the case where
its own schedule is dropped; a VM-side systemd timer is the fully-reliable
backstop.

It is read-only (GitHub API) and emits nothing when everything is intact.
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone

import notifier

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("chain.watchdog")

REPO = os.environ.get("GITHUB_REPOSITORY", "t-arohi/marketplace-distro-scanner")
PHASE1 = "Scan Marketplace Images"
PHASE2 = "Phase 2 - Validate against PMC prod"
PHASE3 = "Phase 3 - Validate AzNFS with LISA"
# How long after an upstream success we still expect the downstream to appear.
GRACE_MIN = int(os.environ.get("CHAIN_GRACE_MIN", "120"))
# How long the daily scan (Phase 1) may go without ANY run before we treat the
# scheduled scan as missed. Default 26h = the 24h cadence + a 2h slack for a
# late cron pickup, so a merely-late run does not false-alarm.
SCAN_STALE_MIN = int(os.environ.get("SCAN_STALE_MIN", str(26 * 60)))


def _runs(workflow: str, limit: int = 20) -> list[dict]:
    out = subprocess.run(
        ["gh", "run", "list", "-R", REPO, "--workflow", workflow, "-L", str(limit),
         "--json", "databaseId,status,conclusion,createdAt"],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out or "[]")


def _latest_success(workflow: str) -> dict | None:
    for r in _runs(workflow):
        if r.get("conclusion") == "success":
            return r
    return None


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _followed(upstream: dict, downstream_runs: list[dict]) -> bool:
    """A downstream run exists that started at/after the upstream's create time."""
    up = _ts(upstream["createdAt"])
    return any(_ts(r["createdAt"]) >= up for r in downstream_runs)


def _check(up_name: str, down_name: str) -> str | None:
    up = _latest_success(up_name)
    if not up:
        return None  # nothing to expect
    age_min = (datetime.now(timezone.utc) - _ts(up["createdAt"])).total_seconds() / 60
    if age_min < GRACE_MIN:
        return None  # still within grace; downstream may yet trigger
    if _followed(up, _runs(down_name)):
        return None
    return (f"{up_name} succeeded {age_min:.0f} min ago but {down_name} did not "
            f"trigger within {GRACE_MIN} min. Check the workflow_run hand-off.")


def _check_scan_ran() -> str | None:
    """Alert if the scheduled daily scan (Phase 1) has not run at all recently.

    The hand-off checks above only fire when Phase 1 *did* run; if GitHub drops
    the scan's own cron, the last Phase 1 success is stale, Phase 2/3 already
    followed it, and no gap is seen -- yet nothing fresh was produced. This
    looks at the most recent Phase 1 run of ANY conclusion/event: if even that
    is older than SCAN_STALE_MIN, the daily scan was missed.
    """
    runs = _runs(PHASE1)
    if not runs:
        return (f"No {PHASE1!r} runs found at all -- the scheduled daily scan "
                f"may never have run.")
    newest = max(runs, key=lambda r: _ts(r["createdAt"]))
    age_min = (datetime.now(timezone.utc) - _ts(newest["createdAt"])).total_seconds() / 60
    if age_min > SCAN_STALE_MIN:
        return (f"{PHASE1!r} has not run for {age_min / 60:.1f}h "
                f"(threshold {SCAN_STALE_MIN / 60:.0f}h). The daily scheduled "
                f"scan appears to have been missed or dropped by GitHub.")
    return None


def main() -> int:
    gaps = [
        g for g in (
            _check_scan_ran(),
            _check(PHASE1, PHASE2),
            _check(PHASE2, PHASE3),
        ) if g
    ]
    if not gaps:
        logger.info("chain intact: scan fresh + both hand-offs OK")
        return 0
    body = "\n".join(f"- {g}" for g in gaps)
    logger.error("chain gap(s) detected:\n%s", body)
    notifier.notify("[AzNFS pipeline] broken phase hand-off", body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
