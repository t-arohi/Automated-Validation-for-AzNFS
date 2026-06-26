# Phase 3 — LISA artifacts

These are the **authored LISA artifacts** for Phase 3 AzNFS validation. They are
kept here as the source of truth; to run them they are placed into a checkout of
[Azure/azfiles-lisa](https://github.com/Azure/azfiles-lisa) (LISA discovers test
suites by scanning the runbook's `extension:` paths).

See [`../docs/PHASE3.md`](../docs/PHASE3.md) for the full test plan.

## Contents

| File | What it is |
|------|------------|
| `testsuites/aznfs_validation.py` | The `AzNfsValidation` LISA test suite (3 cases, 5 tiers) |
| `testsuites/__init__.py` | Package marker |
| `runbooks/aznfs_validation.yml` | LISA runbook (platform + `aznfs_*` inputs) |
| `runbooks/aznfs_multidistro.yml` | Multi-distro `batch` runbook (one parallel run, many distros) |
| `orchestrator/` | Records the verdict in the DB + sends one summary e-mail (not a LISA test) |
| `run_phase3.py` | **Automation driver**: lisa_jobs.json → LISA → record_result |
| `AUTOMATION.md` | How Phase 3 runs end-to-end with no human in the loop |
| `examples/jobs.example.json` | Sample Phase 2 input for the driver |

See [`AUTOMATION.md`](AUTOMATION.md) for the automated end-to-end scenario.

## Placement in azfiles-lisa

```
azfiles-lisa/
  lisa/microsoft/testsuites/azfiles/
    __init__.py            <- testsuites/__init__.py
    aznfs_validation.py    <- testsuites/aznfs_validation.py
  runbooks/
    aznfs_validation.yml   <- runbooks/aznfs_validation.yml
```

Anything under `lisa/microsoft/testsuites/` is auto-discovered (the existing
runbooks already `extension: ../../testsuites`), so no extra wiring is needed.

## Test cases

| Case | Tiers | Needs a share |
|------|-------|---------------|
| `verify_aznfs_install_lifecycle` | 1–3 (artifact, install, footprint) | No |
| `verify_aznfs_nfs_functional` | 4 (mount + simple I/O, EIT off/on) | Yes |
| `verify_aznfs_resilience` | 5 (watchdog restart) | Yes |

## Run (from a LISA checkout, on WSL/Linux)

```bash
lisa run -r runbooks/aznfs_validation.yml \
  -v subscription_id:<sub> \
  -v marketplace_image:"RedHat:RHEL:9_5:latest" \
  -v aznfs_package_url:"https://packages.microsoft.com/rhel/9.0/prod/Packages/a/aznfs-0.3.458-1.x86_64.rpm" \
  -v aznfs_expected_version:"0.3.458"
```

Run a single case with `-v case_name:verify_aznfs_install_lifecycle` (the
`name` criteria is a regex fullmatch; the lifecycle case needs no Azure share,
so it is the cheapest to start with). See [`../docs/PHASE3.md`](../docs/PHASE3.md)
for parallel and multi-distro runs, and [`AUTOMATION.md`](AUTOMATION.md) for the
fully automated driver.

## Notes

- AzNFS names/paths (`aznfs`, `aznfswatchdog`, `mount.aznfs`) and the exact
  mount/EIT options are **runbook variables**, not hardcoded — confirm with the
  team and override via `-v` without editing code.
- Install is **prod URL first**, **PMC repo fallback**. Tier 1 artifact
  checks only run when a package URL is provided (you can only inspect a file
  you downloaded).
- Non-RPM/DEB distros are **skipped**, not failed.
