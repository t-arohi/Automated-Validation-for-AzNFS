# Extending the pipeline to new packages (Azure Files Authenticator, AOD)

## Background

Today the pipeline answers one question for **AzNFS**: *is the AzNFS package
published on PMC prod for each marketplace Linux distro, and does it install and
work there?* Almost all of the logic is distro-agnostic and reusable. Only a
handful of places assume the package is specifically `aznfs` - its **name**, its
**PMC pool path**, its **version series**, its **support matrix**, and its **LISA
test suite**.

This document lists **exactly what is AzNFS-specific**, and what you change to
also validate:

- **Azure Files Authenticator** - PMC package `azfilesauth`, version series `1.0.x`
- **Always-on Diagnostics (AOD)** - PMC package name / series to be confirmed with the AOD team

All three are Azure Files Linux client packages published on the *same*
`packages.microsoft.com/<distro>/<version>/prod/` layout, so the crawler,
version-indexing, gates, and VM provisioning are all reusable.

## What is AzNFS-specific (and where it lives)

| # | AzNFS-specific assumption | File / symbol | AzNFS value | azfilesauth value |
|---|---|---|---|---|
| 1 | Publisher list (Phase 1 discovery) | `scripts/config.py` -> `PUBLISHERS` | 6 publishers (below) | add any missing eligible publisher |
| 2 | Package directory + filename | `src/phase2/pmc_packages.py` -> `aznfs_dir_url()`, the `startswith("aznfs")` filter in `list_packages()`, `_AZNFS_VERSION_RE`, `file_arch()` | `pool/main/a/aznfs/`, files `aznfs_*` | `pool/main/a/azfilesauth/`, files `azfilesauth_*` |
| 3 | Version series (which lineage is "latest") | `src/phase2/pmc_packages.py` -> `AZNFS_SERIES`, `in_series()` | `0.3` (tracks 0.3.x) | `1.0` (tracks 1.0.x) |
| 4 | Supported-distro allow-list | `src/phase2/orchestrator.py` -> `_SUPPORTED_UBUNTU/_RHEL/_ROCKY/_SLES`, `_is_aznfs_supported_distro()` | Ubuntu/RHEL/Rocky/SLES set | the package's own matrix |
| 5 | Support-matrix source (the "should it exist?" check) | `src/phase2/orchestrator.py` -> `_AZNFS_PACKAGES_CSV_URL`, `_packages_csv_mentions_distro()` | `Azure/AZNFS-mount/main/packages.csv` | azfilesauth's own matrix URL/format |
| 6 | LISA test suite (Phase 3) | `phase3/testsuites/aznfs_validation.py`, `phase3/runbooks/aznfs_validation.yml`, `phase3/run_phase3.py` -> `_BASE_RUNBOOK` | AzNFS 5-tier suite | azfilesauth suite (authored/reviewed by that team) |

Everything **not** in this table is shared and needs no change: the marketplace
crawl, the version-indexed prod URL scheme, **Gate 1** (does the distro's prod
repo exist?), the numeric-latest picker, the DB/cache mechanics, the e-mail
notifier, and the VM provisioning in Phase 3.

---

## Phase 1 - Publishers

Phase 1 is **package-agnostic**: it only discovers marketplace images and tracks
their versions. It does not know or care about aznfs. So "adding a package" does
**not** require Phase 1 changes *unless* the new package supports a distro whose
publisher is not being scanned yet.

Current `scripts/config.py` -> `PUBLISHERS` (6):

| Publisher (Marketplace API name) | Distro |
|---|---|
| `Canonical` | Ubuntu |
| `Debian` | Debian |
| `RedHat` | RHEL |
| `SUSE` | SLES |
| `resf` | Rocky Linux |
| `MicrosoftCBLMariner` | Azure Linux / CBL-Mariner |

**What to do for azfilesauth / AOD:** take each package's support matrix, list the
distros it supports, and make sure every one of those distros has its publisher
in `PUBLISHERS`. If azfilesauth adds, say, AlmaLinux, add its publisher; if it
only targets distros already covered, no Phase 1 change is needed. Publishers are
**shared across all packages** - one crawl feeds every package's Phase 2.

> See `docs/MODIFYING-THE-CODE.md` -> "How to add a publisher" for the exact edit
> (it also involves `distro_map.yaml` and, sometimes, the label deriver).

---

## Phase 2 - the three gates

### Gate 1 - does the prod repo exist? (NO CHANGE)

`src/phase2/pmc_packages.py` -> `repo_base_url()` builds
`{base}/<distro>/<version>/prod/` from the image's `distro_label` (via
`distro_map.yaml`) and checks it returns HTTP 200. This is the same repo for
every package, so **Gate 1 is reused as-is**.

### Gate 2 - is the package published for this arch?

This is the first AzNFS-specific gate. Two things are hardcoded to `aznfs`:

1. **The package directory.** `aznfs_dir_url(distro, version, family, base)` returns:
   - apt: `.../<distro>/<version>/prod/pool/main/a/aznfs/`
   - yum: `.../<distro>/<version>/prod/Packages/a/`

   For azfilesauth, the apt directory becomes `pool/main/a/azfilesauth/`
   (the pool layout is `pool/main/<first-letter>/<package-name>/`). Example the
   user verified:
   - AzNFS: `https://packages.microsoft.com/ubuntu/22.04/prod/pool/main/a/aznfs/`
   - azfilesauth: `https://packages.microsoft.com/ubuntu/22.04/prod/pool/main/a/azfilesauth/`

2. **The filename filter.** `list_packages()` keeps only files whose name
   `startswith("aznfs")`, and `_AZNFS_VERSION_RE` / `file_arch()` parse the
   `aznfs_<ver>_<arch>.deb` / `aznfs-<ver>-1.<arch>.rpm` shapes. For azfilesauth
   these become `azfilesauth_*` / the equivalent prefix.

**Change:** parameterize the package name so `aznfs_dir_url`, the `startswith`
filter, and the version/arch regexes use the target package instead of the
literal `aznfs`.

### Gate 3 - support policy + latest version

Two AzNFS-specific inputs:

1. **The supported-distro allow-list** (`src/phase2/orchestrator.py`):
   `_SUPPORTED_UBUNTU = {18.04, 20.04, 22.04, 24.04, 26.04}`, `_SUPPORTED_RHEL`,
   `_SUPPORTED_ROCKY`, `_SUPPORTED_SLES`, consumed by
   `_is_aznfs_supported_distro()`. azfilesauth and AOD each have their **own**
   set of allowed distro+versions (reviewed by their teams) - swap in that
   matrix.

2. **The support-matrix source** used to decide *"the package is missing but
   should it be here?"*: `_AZNFS_PACKAGES_CSV_URL`
   (`Azure/AZNFS-mount/main/packages.csv`) + `_packages_csv_mentions_distro()`.
   azfilesauth publishes its own allowed-distro list - point the check at that
   source and parser instead.

3. **The version series / "latest".** `src/phase2/pmc_packages.py` ->
   `AZNFS_SERIES = "0.3"` + `in_series()`. Prod carries many aznfs lineages
   (0.0.x ... 3.0.x) side by side; the pipeline filters to the tracked series and
   then takes the numeric-max as "latest". AzNFS = `0.3`, **azfilesauth = `1.0`**.
   The picking logic (numeric-max via `version_tuple`) is unchanged - only the
   series string differs. If a package has no lineage split, use an empty series
   to mean "pure numeric-max".

---

## Phase 3 - the LISA test suite

Phase 3 provisions a VM per distro and runs a LISA suite. The suite is
AzNFS-specific:

- `phase3/testsuites/aznfs_validation.py` - the 5-tier suite (artifact integrity
  -> install lifecycle -> footprint -> mount + I/O -> resilience).
- `phase3/runbooks/aznfs_validation.yml` - the runbook that selects that suite.
- `phase3/run_phase3.py` -> `_BASE_RUNBOOK = phase3/runbooks/aznfs_validation.yml`
  (hardcoded), executed via `lisa run -r <runbook>` with per-distro `-v` overrides.

**Change:** the azfilesauth team provides its own LISA suite. Add it as
`phase3/testsuites/azfilesauth_validation.py` + a runbook
`phase3/runbooks/azfilesauth_validation.yml`, and make `run_phase3.py` pick the
runbook per job/package instead of always using `_BASE_RUNBOOK`. Keep the
`[Tier N: step]` assertion tags so failures are attributed in the summary e-mail.

> See `docs/MODIFYING-THE-CODE.md` -> "How to add a test suite".

---

## The scalable approach: a product registry (recommended)

Doing the six edits above by hand for every package leads to `if package == ...`
branches scattered across the code. The maintainable alternative is a **product
registry**: one data file, `scripts/products.yaml`, with a helper
`scripts/products.py`. Each product entry carries exactly the AzNFS-specific
fields from the table above:

```yaml
products:
  aznfs:
    pmc_package_name: aznfs
    version_series: "0.3"          # null => pure numeric-max
    apt_pool_subdir: pool/main/a/aznfs
    yum_pool_subdir: Packages/a
    support_matrix_url: https://raw.githubusercontent.com/Azure/AZNFS-mount/main/packages.csv
    supported_distros: { ubuntu: [18.04, 20.04, 22.04, 24.04, 26.04], rhel: [7,8,9,10], rocky: [8,9], sles: [15,16] }
    phase3_runbook: phase3/runbooks/aznfs_validation.yml
    phase3_enabled: true
  azfilesauth:
    pmc_package_name: azfilesauth
    version_series: "1.0"
    apt_pool_subdir: pool/main/a/azfilesauth
    yum_pool_subdir: Packages/a
    support_matrix_url: <azfilesauth matrix URL>
    supported_distros: { ... from the azfilesauth team ... }
    phase3_runbook: phase3/runbooks/azfilesauth_validation.yml
    phase3_enabled: false          # report-only until the suite is in
  aod:
    pmc_package_name: <confirm with AOD team>
    version_series: <confirm>
    ...
```

Then `pmc_packages.py` and `orchestrator.py` read these fields per product
instead of the hardcoded constants, and Phase 2 loops over the enabled products
(Gate 1 checked once per distro, Gates 2-3 per product). Roll it out by first
refactoring to a registry that contains *only* `aznfs` (prove behaviour is
identical), then add `azfilesauth`, then `aod`.

## Data model: per-package validation state (required for real multi-package)

Today validation state is a single `validated` column per image row - it assumes
one package. Once more than one package is validated per image, state becomes
**per (image, package)**. The recommended change is a child table:

```sql
CREATE TABLE image_validation (
  image_id   INTEGER NOT NULL,
  product    TEXT    NOT NULL,          -- 'aznfs' | 'azfilesauth' | 'aod'
  validated  TEXT    NOT NULL DEFAULT 'unknown',
  last_validated          TEXT,
  last_validated_version  TEXT,
  reason     TEXT,
  PRIMARY KEY (image_id, product)
);
```

Backfill the existing `images.validated` as `product='aznfs'`. `db_manager`'s
`set_validation_state()` / `get_rows_by_state()` gain a `product` argument. Until
this lands you can only track one package's verdict per image.

## Checklist to onboard a new package

1. **Publishers** - confirm every supported distro's publisher is in `config.py PUBLISHERS`; add any missing (`docs/MODIFYING-THE-CODE.md`).
2. **Package name + pool** - PMC package name and apt pool subdir (`pmc_packages.py`).
3. **Version series** - the tracked lineage, e.g. `1.0` (`pmc_packages.py AZNFS_SERIES`/registry).
4. **Support matrix** - the allowed distro+version set and its source URL (`orchestrator.py`).
5. **Test suite** - the LISA suite + runbook from the owning team (`phase3/`).
6. **(For >1 package)** - the per-(image, package) DB change above.

Collect items 2-5 from the package's owning team before starting; they are the
only unknowns.
