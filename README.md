# Marketplace Distro Scanner (Phase 1)

Phase 1 of the AzNFS marketplace validation pipeline.

Discovers Azure Marketplace VM images for a selected set of publishers and
regions, tracks them in a local SQLite database, emails the team about new
SKUs and version bumps, and emits a JSON list of images that still need
validation. Designed to run unattended every day at 00:00 UTC.

## Runtime architecture

```
GitHub Actions cron (00:00 UTC daily)
        │
        ▼
Self-hosted runner agent on Azure VM "vmscan"   ← label: azure-vm-marketplace
        │
        ▼
python scripts/scan_marketplace.py
        │
        ├── DefaultAzureCredential  ──► IMDS ──► user-assigned MI "miscan"
        │       ├── Azure Compute Mgmt API   (list SKUs/versions)
        │       └── ACS Email REST           (send notification)
        │
        ├── SQLite (marketplace.db, cached between runs)
        │       └── classify each SKU: NEW / UPDATED / UNCHANGED
        │
        └── output/distros_to_validate.json    (artifact, audit trail)
```

Key properties:

- **No secrets on disk and no service principals.** The VM has a user-assigned
  Managed Identity (`miscan`) attached. Both Azure SDK calls and the ACS email
  send pick up the MI token from IMDS.
- **One trigger.** GitHub Actions cron — not systemd. The actions-runner agent
  on `vmscan` polls GitHub and runs the job locally on the VM.
- **One email per run.** `notifier.py` runs inside `scan_marketplace.py`, so
  the email is sent before the workflow ends. There is no separate SMTP step.

## Current scope

| | |
|---|---|
| Region | `eastus` |
| Publishers | `Canonical`, `RedHat`, `SUSE`, `Debian` |
| Frequency | Daily, 00:00 UTC |
| Recipients | 5 (see `scripts/config.py`, override via `NOTIFY_RECIPIENTS`) |

## Repository layout

```
.github/workflows/scan-marketplace.yml   GH Actions cron, runs on self-hosted runner
db/schema.sql                            Authoritative schema (lazy-migrated at runtime)
scripts/
  config.py                              Regions, publishers, paths, env wiring
  azure_client.py                        SDK wrappers + architecture lookup
  db_manager.py                          SQLite ops, dedup/classification
  notifier.py                            ACS Email via Managed Identity
  scan_marketplace.py                    Entry point
tests/test_notifier.py                   Notifier tests (ACS SDK mocked)
requirements.txt                         Runtime deps
pyproject.toml                           Build + pytest config
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
| `distro_label` | Human-readable name, e.g. `Ubuntu 24.04`, `RHEL 9`. |
| `version` | Latest version observed. Bumped in place on a new release. |
| `validated` | Lifecycle: `unknown`, `known_supported`, `known_unsupported`. **Preserved across version bumps** so manual validation state is not lost. |
| `date_added`, `last_modified`, `last_checked` | All three are reset to "now" on a version bump. |

### Classification per scan

Each Marketplace SKU is classified as one of:

- **NEW** — never seen before. Inserted with `validated='unknown'`.
- **UPDATED** — same identity tuple, higher version. Version + timestamps
  refreshed; `family` / `distro_label` re-derived; `validated` preserved.
- **UNCHANGED** — version matches. Only `last_checked` is bumped.

NEW and UPDATED images go into the email and the JSON. UNCHANGED images
are silently re-stamped.

## Output

`output/distros_to_validate.json` is **overwritten** every run with the list of
NEW + UPDATED images from that scan. Schema per entry:

```json
{
  "publisher": "RedHat",
  "image": "RHEL",
  "sku": "9-lvm-gen2",
  "version": "9.3.2026",
  "region": "eastus",
  "architecture": "x86_64",
  "family": "yum",
  "distro_label": "RHEL 9",
  "validated": "unknown",
  "date_added": "2026-06-05T00:00:00Z"
}
```

Uploaded as a workflow artifact (`marketplace-scan-<run_number>`) with 30-day
retention. The Actions UI also gets a rendered Markdown summary of the same
table.

## Email

Sent via Azure Communication Services Email REST API using the VM's
Managed Identity — no SMTP, no passwords. Two HTML sections, each a 9-column
table:

1. **New SKUs** — first-time discoveries.
2. **Version bumps** — existing SKUs that got a newer version.

Subject line examples:

- `[AzNFS Phase 1] 3 new marketplace SKU(s)`
- `[AzNFS Phase 1] 2 version bump marketplace SKU(s)`
- `[AzNFS Phase 1] 3 new + 2 version bumps marketplace SKU(s)`

If both lists are empty no email is sent. Notification failures are caught and
logged — they never crash the scan.

## Exit codes

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

| Name | Value |
|---|---|
| `AZURE_MANAGED_IDENTITY_CLIENT_ID` | clientId of the user-assigned MI attached to the runner VM. |
| `ACS_ENDPOINT` | e.g. `https://acscomm.india.communication.azure.com` |
| `ACS_SENDER` | e.g. `DoNotReply@<guid>.azurecomm.net` |

Required RBAC on the Managed Identity:

- `Reader` on the subscription (so the Compute API can list images).
- `Communication and Email Service Owner` on the ACS resource (so the MI can
  send via the Email REST API).

Required runner: a self-hosted runner registered to the repo with labels
`self-hosted` and `azure-vm-marketplace` (the workflow targets that exact
pair). The runner's host VM must have the user-assigned MI attached.

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

# 4. Run
cd scripts
python scan_marketplace.py
```

Email sending will be skipped automatically if `ACS_ENDPOINT` and `ACS_SENDER`
are not set, so local runs do not spam recipients.

## Tests

```bash
pytest -q
```

`test_notifier.py` mocks `azure.communication.email` and `azure.identity`, so
it runs without Azure credentials or network access.

## What's next

Phase 2 (Gate 1 onwards) will consume `output/needs_validation.json` and the
DB to drive ESRP signing, PMC publishing, and LISA validation. The notifier
module is reused by later phases.
