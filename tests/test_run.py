from __future__ import annotations

import json

import pytest
import requests

from src.phase2 import run
from src.phase2.repo_index import RepoIndex


# ---------------------------------------------------------------------------
# Phase 1 module fakes (stand in for scripts/notifier.py + scripts/db_manager.py)
# ---------------------------------------------------------------------------
class FakeNotifierMod:
    def __init__(self):
        self.failures = []
        self.trusted = []
        self.summaries = []

    def send_phase2_failure(self, distro_label, detail, recipients=None):
        self.failures.append((distro_label, detail))

    def send_phase2_trusted(self, distro_label, download_url=None, version=None, recipients=None):
        self.trusted.append((distro_label, download_url, version))

    def send_phase2_summary(self, processed, unsupported=None, to_phase3=None,
                            trusted=None, skipped=0, errors=None, recipients=None):
        self.summaries.append({
            "processed": processed,
            "unsupported": unsupported or [],
            "to_phase3": to_phase3 or [],
            "trusted": trusted or [],
        })


class FakeDbMod:
    def __init__(self, matched=True):
        self.calls = []
        self.matched = matched

    def set_validation_state(self, db_path, identity, state):
        self.calls.append((db_path, identity, state))
        return self.matched


# ---------------------------------------------------------------------------
# Adapter tests
# ---------------------------------------------------------------------------
def test_notifier_adapter_actionable_and_trusted_pass_through():
    mod = FakeNotifierMod()
    ad = run.Phase1NotifierAdapter(mod)

    ad.notify_actionable("Ubuntu 24.04", "add CSV row")
    ad.notify_trusted("RHEL 9", "already published,trusted.")

    assert mod.failures == [("Ubuntu 24.04", "add CSV row")]
    assert mod.trusted == [("RHEL 9", None, None)]


def test_notifier_adapter_summary_splits_trusted_from_fresh_and_pairs_reasons():
    mod = FakeNotifierMod()
    ad = run.Phase1NotifierAdapter(mod)

    ad.notify_actionable("Debian 12", "no tuxdev repo")
    ad.notify_trusted("RHEL 9", "trusted")
    # to_phase3 from the orchestrator contains BOTH trusted (RHEL 9) and freshly
    # built (Ubuntu 24.04) labels.
    ad.notify_summary(processed=3, unsupported=["Debian 12"], to_phase3=["RHEL 9", "Ubuntu 24.04"])

    s = mod.summaries[-1]
    assert s["processed"] == 3
    assert s["unsupported"] == [("Debian 12", "no tuxdev repo")]
    assert s["trusted"] == ["RHEL 9"]
    assert s["to_phase3"] == ["Ubuntu 24.04"]   # fresh only


def test_db_adapter_forwards_path_identity_state_reason():
    mod = FakeDbMod()
    ad = run.Phase1DbAdapter(mod, "/tmp/marketplace.db")
    ident = ("Canonical", "ubuntu-24_04-lts", "server", "eastus", "x86_64")

    ad.set_validation_state(ident, "known_supported", None, "2026-01-01T00:00:00Z")

    # The reason is e-mailed, not persisted: the DB layer receives only the state.
    assert mod.calls == [("/tmp/marketplace.db", ident, "known_supported")]


def test_db_adapter_warns_when_no_row(caplog):
    mod = FakeDbMod(matched=False)
    ad = run.Phase1DbAdapter(mod, "db")
    with caplog.at_level("WARNING"):
        ad.set_validation_state(("p", "i", "s", "r", "a"), "known_unsupported", "why", "ts")
    assert "No DB row matched" in caplog.text


# ---------------------------------------------------------------------------
# CSV fetch + input loading
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _Session:
    def __init__(self, resp):
        self._resp = resp
        self.url = None

    def get(self, url, timeout=None):
        self.url = url
        return self._resp


def test_fetch_packages_tux_csv_builds_raw_url_and_returns_text():
    sess = _Session(_Resp("Ubuntu-24.04,aznfsDeb,microsoft-ubuntu-noble,noble\n"))
    text = run.fetch_packages_tux_csv(session=sess)
    assert text.startswith("Ubuntu-24.04,aznfsDeb")
    assert sess.url == (
        f"{run.GITHUB_RAW_BASE}/{run.AZNFS_REPO}/{run.PINNED_BRANCH}/{run.PACKAGES_TUX_CSV_PATH}"
    )


def test_load_entries_rejects_non_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps({"not": "a list"}))
    with pytest.raises(ValueError):
        run.load_entries(str(p))


def test_load_entries_reads_list(tmp_path):
    p = tmp_path / "needs.json"
    p.write_text(json.dumps([{"distro_label": "Ubuntu 24.04"}]))
    assert run.load_entries(str(p)) == [{"distro_label": "Ubuntu 24.04"}]


# ---------------------------------------------------------------------------
# End-to-end run() with injected fakes (no network, no Phase 1 modules)
# ---------------------------------------------------------------------------
class FakeOnboarding:
    def __init__(self, configs):
        self.configs = configs

    def get_repo_config(self, repo_name):
        return self.configs.get(repo_name)

    def ping(self):
        return True


class FakePackageIndex:
    def __init__(self, files):
        self.files = files

    def list_packages(self, repo, family):
        ext = ".rpm" if family.lower() in {"yum", "rpm", "dnf"} else ".deb"
        return [n for n in self.files if n.startswith("aznfs") and n.endswith(ext)]

    def ping(self):
        return True


def _entry():
    return {
        "publisher": "Canonical",
        "image": "ubuntu-24_04-lts",
        "sku": "server",
        "version": "24.04.202506",
        "region": "eastus",
        "architecture": "x86_64",
        "family": "apt",
        "distro_label": "Ubuntu 24.04",
    }


def test_run_end_to_end_trusted_writes_lisa_jobs(tmp_path):
    notifier_mod = FakeNotifierMod()
    db_mod = FakeDbMod()
    out = tmp_path / "lisa_jobs.json"

    idx = RepoIndex(apt=frozenset({"microsoft-ubuntu-noble"}), yum=frozenset())
    onboarding = FakeOnboarding({"microsoft-ubuntu-noble": {"signing_service": "esrp", "repo_groups": ["shared"]}})
    package_index = FakePackageIndex(["aznfs_0.3.2_amd64.deb"])   # < 0.3.10 -> trusted, no build

    jobs = run.run(
        entries=[_entry()],
        onboarding=onboarding,
        package_index=package_index,
        ado=None,
        notifier_obj=run.Phase1NotifierAdapter(notifier_mod),
        db=run.Phase1DbAdapter(db_mod, "marketplace.db"),
        csv_text="Ubuntu-24.04,aznfsDeb,microsoft-ubuntu-noble,noble\n",
        index=idx,
        aznfs_version="0.3.0",
        lisa_jobs_path=str(out),
    )

    assert len(jobs) == 1
    written = json.loads(out.read_text())
    assert written[0]["distro_label"] == "Ubuntu 24.04"
    assert written[0]["newly_published"] is False
    # DB written as known_supported for the trusted entry.
    assert db_mod.calls[-1][2] == "known_supported"
    # Trusted mail + a summary were emitted.
    assert notifier_mod.trusted and notifier_mod.summaries
