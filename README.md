# Marketplace Distro Scanner

The **AzNFS marketplace validation pipeline**. It discovers new Azure Marketplace
Linux images, checks whether the AzNFS package is published for them on PMC
**production**, validates that the package actually installs and works on each
distro with **LISA**, and records a per-distro support decision — all unattended
on a self-hosted runner.

## Pipeline overview

| Phase | Workflow (`name`) | What it does | Output |
|---|---|---|---|
| **Phase 1 — Scan** (`scripts/`) | `Scan Marketplace Images` | Discover marketplace images for the tracked publishers/regions, classify NEW / UPDATED / UNCHANGED in SQLite, e-mail new releases plus a monthly status digest. | `output/needs_validation.json` |
| **Phase 2 — Prod validation** (`src/phase2/`) | `Phase 2 - Validate against PMC prod` | For each image, check the version-indexed PMC **prod** layout (`packages.microsoft.com/<distro>/<version>/prod/`): does the repo exist? is the tracked `0.3.x` AzNFS package published for this arch? is it newer than what was last validated? Apply the AzNFS support policy. Emit a LISA job for the ones that need testing. | `output/lisa_jobs.json` |
| **Phase 3 — LISA validation** (`phase3/`) | `Phase 3 - Validate AzNFS with LISA` | Provision a VM of each distro, install the AzNFS package, run the 5-tier test suite, and record `known_supported` / `known_unsupported` in the shared DB, with one summary e-mail. | DB verdict + e-mail |

The three phases share one SQLite DB (`marketplace.db`); a distro's
`validation_status` (the DB `validated` column) is the hand-off between them.
The phases chain automatically through GitHub Actions `workflow_run`:

```
Scan Marketplace Images ──workflow_run──▶ Phase 2 - Validate against PMC prod
                                                  │
                                          workflow_run
                                                  ▼
                                  Phase 3 - Validate AzNFS with LISA
```

> **`workflow_run` and `workflow_dispatch` only fire for workflow files on the
> default branch (`master`).** All three workflows live on `master`. Each phase
> also accepts a manual **Run workflow** (`workflow_dispatch`) so you can re-run
> a single phase against the most recent upstream artifact.

Phase 2 and Phase 3 both mutate `marketplace.db`, so they share one GitHub
Actions **concurrency group** (`marketplace-db`, `cancel-in-progress: false`) and
never run at the same time — last-writer-wins on the cache would otherwise clobber
verdicts.

---

## The three workflows

All three run on the same self-hosted runner (`runs-on: [self-hosted, azure-vm-marketplace]`)
and cache `marketplace.db` between runs under the key prefix `marketplace-db-v2-`.

### Phase 1 — `.github/workflows/scan-marketplace.yml`

| | |
|---|---|
| Triggers | `schedule` cron `30 03 * * *` (**03:30 UTC daily** = 09:00 IST) **+** `workflow_dispatch` |
| Manual input | `emit_backlog` (boolean, default `false`) — one-time backlog feed (see below) |
| Runs | `python scan_marketplace.py` (from `scripts/`) |
| Artifact | `marketplace-scan-<run_number>` → `output/needs_validation.json` (30-day retention) |
| Run summary | The cut-down distro list (one row per OS release, with SKU counts) is written to the Actions **Summary** tab on every run |

> The cron is deliberately **off the hour and not at midnight**. GitHub's
> `schedule` trigger is best-effort and frequently delays or drops runs queued at
> `:00` (and `00:00 UTC` worst of all), so an odd minute on an off-peak hour fires
> far more reliably.

**One-time backlog feed (`EMIT_BACKLOG`).** Normally `needs_validation.json`
carries only the new/updated **delta**, so distros already cached as `unknown`
never reach Phase 2/3. Setting the repo variable `EMIT_BACKLOG` (or ticking
`emit_backlog` on a manual run) overwrites the hand-off with the **full
unvalidated backlog**, deduplicated to one representative SKU per
`(distro_label, architecture)`. It is a **one-shot**: the scanner records the
token in the DB and self-disables after one emit, so a forgotten variable will
**not** keep re-provisioning Phase 3 VMs. Re-arm by setting `EMIT_BACKLOG` to a
new value (e.g. a fresh date).

### Phase 2 — `.github/workflows/phase2-publish.yml`

| | |
|---|---|
| Triggers | `workflow_run` after **Scan Marketplace Images** completes **+** `workflow_dispatch` |
| Guard | proceeds only if the upstream run **succeeded** (a manual dispatch is always allowed) |
| Input | downloads Phase 1's `needs_validation.json` (the triggering run's artifact, or the latest successful run on manual dispatch) |
| Runs | `python -m src.phase2.run --input output/needs_validation.json --output output/lisa_jobs.json` |
| Artifact | `lisa-jobs-<run_number>` → `output/lisa_jobs.json` (30-day retention) |
| Concurrency | `marketplace-db` (shared with Phase 3) |

Phase 2 talks to the **public, anonymous** `packages.microsoft.com` only — no PMC
API, no tux-dev, no ADO build, no corp proxy.

### Phase 3 — `.github/workflows/phase3-validate.yml`

| | |
|---|---|
| Triggers | `workflow_run` after **Phase 2 - Validate against PMC prod** completes **+** `workflow_dispatch` |
| Guard | proceeds only if Phase 2 **succeeded** (a manual dispatch is always allowed) |
| Input | downloads Phase 2's `lisa_jobs.json` (empty file ⇒ "nothing to validate", clean exit) |
| Engine | `Ensure LISA engine` step runs `phase3/setup_lisa.sh` if the LISA venv is missing (idempotent; later runs are a no-op fast path) |
| Runs | `python -m phase3.run_phase3 output/lisa_jobs.json --concurrency <n> --max-parallel-distros <n>` after `az login --identity` |
| Concurrency | `marketplace-db` (shared with Phase 2) |

Phase 3 provisions **real Azure VMs** via the runner's managed identity. Parallelism
is bounded by the subscription's regional vCPU quota (`PHASE3_CONCURRENCY` cases per
distro, `PHASE3_MAX_PARALLEL_DISTROS` distros at once).

---

## Phase 1 — Scan

Discovers Azure Marketplace VM images for a selected set of publishers and
regions, tracks them in a local SQLite database, e-mails the team about new
SKUs and version bumps, and emits a JSON list of images that still need
validation. Designed to run unattended every day.

### Runtime architecture

```
GitHub Actions cron (03:30 UTC daily)  +  manual "Run workflow"
        |
        v
Self-hosted runner agent on Azure VM "vmscan"   <- label: azure-vm-marketplace
        |
        v
python scripts/scan_marketplace.py
        |
        +-- DefaultAzureCredential --> IMDS --> user-assigned MI "miscan"
        |       +-- Azure Compute Mgmt API   (list SKUs/versions)
        |       +-- ACS Email REST           (send notification)
        |
        +-- SQLite (marketplace.db, cached between runs)
        |       +-- classify each SKU: NEW / UPDATED / UNCHANGED
        |
        +-- output/needs_validation.json    (the single Phase 1 artifact)
```

Key properties:

- **No secrets on disk and no service principals.** The VM has a user-assigned
  Managed Identity (`miscan`) attached. Both Azure SDK calls and the ACS email
  send pick up the MI token from IMDS.
- **One trigger.** GitHub Actions cron — not systemd. The actions-runner agent
  on `vmscan` polls GitHub and runs the job locally on the VM.
- **One email per run.** `notifier.py` runs inside `scan_marketplace.py`, so
  the email is sent before the workflow ends. There is no separate SMTP step.

### Current scope

| | |
|---|---|
| Region | `eastus` |
| Publishers | `Canonical`, `RedHat`, `SUSE`, `Debian`, `MicrosoftCBLMariner`, `OpenLogic` (CentOS), `resf` (Rocky) |
| Frequency | Daily, 03:30 UTC (plus manual dispatch) |
| Recipients | `scripts/config.py` default list, overridable via the `NOTIFY_RECIPIENTS` repo variable |

## Repository layout

```
.github/workflows/
  scan-marketplace.yml     Phase 1 cron + manual dispatch (self-hosted runner)
  phase2-publish.yml       Phase 2, chained after Phase 1 (workflow_run)
  phase3-validate.yml      Phase 3, chained after Phase 2 (workflow_run)
db/schema.sql              Authoritative schema (lazy-migrated at runtime)
scripts/                   Phase 1 + shared helpers
  config.py                Regions, publishers, paths, env wiring
  azure_client.py          SDK wrappers + architecture lookup
  db_manager.py            SQLite ops, dedup/classification, meta table
  notifier.py              ACS Email via Managed Identity (used by all phases)
  scan_marketplace.py      Phase 1 entry point
src/phase2/                Phase 2 (PMC prod validation)
  orchestrator.py          The 3 gates + AzNFS support policy
  pmc_packages.py          Read-only packages.microsoft.com client
  distro_map.yaml          distro_label/publisher -> PMC <distro> segment
  run.py                   Phase 2 entry point + Phase 1 adapters
phase3/                    Phase 3 (LISA validation)
  run_phase3.py            Driver: per-distro LISA run -> verdict
  setup_lisa.sh            Idempotent LISA-engine bootstrap
  testsuites/              The LISA 5-tier AzNFS suite
  runbooks/                LISA runbooks
  orchestrator/
    record_result.py       DB verdict + single summary e-mail
docs/PHASE3.md             Phase 3 design + bring-up notes
tests/                     pytest suite (Phase 1/2/3)
requirements.txt           Runtime deps
pyproject.toml             Build + pytest config
```

## Data model

One row per `(publisher, image, sku, region, architecture)`. Only the
**latest** version is kept — older versions are not retained. Schema
highlights:

| Column | Notes |
|---|---|
| `publisher`, `image`, `sku`, `region` | Identity tuple from the Marketplace API. `image` maps to the SDK's `offer`. |
| `architecture` | Normalised to `x86_64` or `arm64` (SDK values `x64` / `Arm64`). |
| `family` | `apt` or `yum` — used by Phase 2 gates. |
| `distro_label` | Human-readable name, e.g. `Ubuntu 24.04`, `RHEL 9`, `CentOS 8`, `Rocky 9`. |
| `version` | Latest version observed. Bumped in place on a new release. |
| `validated` | Lifecycle: `unknown` -> `pending_validation` -> `known_supported` / `known_unsupported` (Phase 2 may park a row at `pending_publish` while it waits for a manual prod publish). **Preserved across version bumps** so manual validation state is not lost. Surfaced in the Phase 1 JSON as `validation_status`. |
| `last_validated`, `last_validated_version` | Stamped by Phase 2/3 when a verdict is recorded. |
| `date_added`, `last_modified`, `last_checked` | All reset to "now" on a version bump. |

A small `meta` key/value table (same DB) holds cross-run state such as the
monthly-digest marker and the `EMIT_BACKLOG` one-shot token.

### Classification per scan

Each Marketplace SKU is classified as one of:

- **NEW** — never seen before. Inserted with `validated='unknown'`.
- **UPDATED** — same identity tuple, higher version. Version + timestamps
  refreshed; `family` / `distro_label` re-derived; `validated` preserved.
- **UNCHANGED** — version matches. Only `last_checked` is bumped.

NEW and UPDATED images go into the email and the JSON. UNCHANGED images
are silently re-stamped.

## Phase 1 output

`output/needs_validation.json` is the **only** file Phase 1 writes. It is
**overwritten** every run with the list of NEW + UPDATED images from that scan
(one row per SKU). Each row is numbered at the **distro-release** level: `id`
and `no` both run `1..N` over the unique `distro_label` values in the file (the
SQLite row id is not exposed). The DB `validated` column is surfaced here as
`validation_status`.

```json
{
  "id": 12,
  "no": 12,
  "publisher": "RedHat",
  "image": "RHEL",
  "sku": "9-lvm-gen2",
  "version": "9.3.2026",
  "region": "eastus",
  "architecture": "x86_64",
  "family": "yum",
  "distro_label": "RHEL 9",
  "validation_status": "unknown",
  "date_added": "2026-06-05T00:00:00Z"
}
```

Uploaded as a workflow artifact (`marketplace-scan-<run_number>`) with 30-day
retention, and consumed by Phase 2. The distro-level rollup (one entry per OS
release) is also computed in memory for the new-release diff, the monthly
digest, and the Actions **Summary** table.

## Email

Sent via Azure Communication Services Email REST API using the VM's
Managed Identity — no SMTP, no passwords. The same `notifier.py` is reused by
all three phases.

**Phase 1.** When a scan finds **new distro releases**, an email lists them (one
row per OS release, with publishers / architectures / SKU counts collapsed):

- `[AzFilesAutoPackager] 3 new distro release(s) need validation`

If no new distro releases are found, **no email is sent that day**. Separately,
once per calendar month — on the first scan of the month — a digest lists every
tracked distro release grouped into **three buckets by AzNFS validation state**
(`known_supported` / `known_unsupported` / `unknown`, the last folding in the
not-yet-decided `pending_*` states):

- `[AzFilesAutoPackager] Monthly reminder: 8 supported, 3 unsupported, 1 unknown`

**Phase 2.** Exactly **one** summary e-mail per run, listing every image and —
for the actionable ones — the reason (to Phase 3, trusted, pending publish, or
known-unsupported).

**Phase 3.** Exactly **one** summary e-mail per run. Each distro gets a pass/fail
line with the image URN, the run logs URL, and the DB state transition, e.g.:

- pass: `validation done for distro RHEL 9.5` → `validation_state changed to known_supported in DB`
- fail: `validation fails for distro "SLES 16"` → failing tier + `validation_state changed to known_unsupported in DB`

Notification failures are caught and logged — they never crash a run.

## Phase 2 — the three gates and the AzNFS support policy

For each image Phase 2 walks the version-indexed PMC prod layout:

1. **Gate 1 — repo exists?** `GET /<distro>/<version>/prod/` returns 200. If not,
   the release is marked `known_unsupported` (reason: *prod repo is missing*).
2. **Gate 2 — package published?** The aznfs directory lists a tracked `0.3.x`
   build for this architecture. If not, the **AzNFS support policy** decides:
   - the distro is **not** in the supported set ⇒ `known_unsupported`
     (*repo is found but packages are not found because distro is not supported by AzNFS*);
   - the distro **is** supported and is listed in
     [`Azure/AZNFS-mount/packages.csv`](https://github.com/Azure/AZNFS-mount/blob/main/packages.csv)
     ⇒ `pending_publish` (*publish packages manually, then re-invoke Phase 2*);
   - the distro **is** supported but is **missing** from `packages.csv` ⇒
     `pending_publish` (*team must update the csv/yaml, push a branch, and
     re-invoke Phase 2 with that branch*).
3. **Gate 3 — validation needed?** The latest `0.3.x` prod version is compared
   (numerically) against what was last validated. Already-validated ⇒
   `known_supported` (trusted); first time or newer ⇒ a LISA job is emitted and
   the row is parked `pending_validation`.

The **AzNFS-supported distros** are: Ubuntu 18.04 / 20.04 / 22.04 / 24.04 / 26.04;
CentOS 7 / 8; RHEL 7 / 8 / 9 / 10; Rocky 8 / 9; SLES 15 / 16.

## Phase 3 — LISA validation

Each emitted job provisions a fresh VM and runs the AzNFS 5-tier suite
(artifact integrity → install lifecycle → post-install footprint → mount + I/O →
basic resilience). A clean pass records `known_supported`; any failure records
`known_unsupported` (one strike — a human resets the DB row to `unknown` if a
transient/flaky failure buried a good distro). See [`docs/PHASE3.md`](docs/PHASE3.md)
for the full design, the test tiers, and bring-up findings.

## Exit codes (Phase 1)

| Code | Meaning |
|---|---|
| 0 | Scan completed, nothing new or updated. |
| 1 | Scan completed, NEW and/or UPDATED images found. **Not a failure** — the workflow treats this as success. |
| >1 | Real error. Workflow fails. |

## GitHub Actions configuration

Required **repository secret**:

| Name | Value |
|---|---|
| `AZURE_SUBSCRIPTION_ID` | Subscription that owns the VM, MI, and ACS resource. |

Required **repository variables** (Settings → Secrets and variables → Actions → Variables):

| Name | Used by | Value |
|---|---|---|
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | all phases | clientId of the user-assigned MI attached to the runner VM. |
| `ACS_ENDPOINT` | all phases | e.g. `https://acscomm.india.communication.azure.com` |
| `ACS_SENDER` | all phases | e.g. `DoNotReply@<guid>.azurecomm.net` |
| `NOTIFY_RECIPIENTS` | Phase 2/3 | Comma-separated recipient list. **Must be set** or Phase 2/3 skip their summary e-mail. (Phase 1 falls back to the default list in `config.py`.) |

Optional **repository variables**:

| Name | Used by | Default |
|---|---|---|
| `EMIT_BACKLOG` | Phase 1 | unset (delta only). Set to arm the one-shot backlog feed. |
| `PROD_REPO_BASE` | Phase 2 | `https://packages.microsoft.com` |
| `HTTP_TIMEOUT` | Phase 2 | `30` (seconds) |
| `LISA_VENV` | Phase 3 | `$HOME/lisa-venv` |
| `PHASE3_CONCURRENCY` | Phase 3 | `3` (LISA cases in parallel per distro) |
| `PHASE3_MAX_PARALLEL_DISTROS` | Phase 3 | `1` (distros validated at once) |

Required RBAC on the Managed Identity:

- `Reader` on the subscription (so the Compute API can list images and Phase 3
  can provision VMs).
- `Communication and Email Service Owner` on the ACS resource (so the MI can
  send via the Email REST API).
- Permissions to create/delete VMs and their resource groups for Phase 3.

Required runner: a self-hosted runner registered to the repo with labels
`self-hosted` and `azure-vm-marketplace` (every workflow targets that exact
pair). The runner's host VM must have the user-assigned MI attached, and — for
Phase 3 — the LISA engine (bootstrapped automatically by `phase3/setup_lisa.sh`).

## Local development

```bash
# 1. Clone, create venv, install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .[dev]

# 2. Authenticate to Azure
az login

# 3. Configure
cp .env.example .env
$EDITOR .env       # fill AZURE_SUBSCRIPTION_ID; leave MI client-id blank locally

# 4. Run Phase 1
cd scripts
python scan_marketplace.py

# Phase 2 (against the public prod content server)
python -m src.phase2.run --input output/needs_validation.json --output output/lisa_jobs.json
```

Email sending is skipped automatically when `ACS_ENDPOINT` / `ACS_SENDER` are not
set, so local runs do not spam recipients.

## Tests

```bash
pytest -q
```

The suite covers Phase 1 derivation/notifier, the Phase 2 gates + support policy,
and the Phase 3 record/parse logic. Network- and Azure-dependent calls are mocked
(the Phase 2 `packages.csv` lookup is monkeypatched), so the tests run offline.
