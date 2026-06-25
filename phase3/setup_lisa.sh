#!/usr/bin/env bash
# Phase 3 - install / refresh the LISA engine on a runner (idempotent).
#
# Sets up everything the Phase 3 GitHub Actions workflow (phase3-validate.yml)
# needs on the self-hosted runner so `python -m phase3.run_phase3` can drive
# `lisa run`:
#
#   1. system build deps (apt)               -- needs sudo; skipped if missing
#   2. the LISA engine source (azfiles-lisa) -- cloned/updated into LISA_SRC
#   3. a Python venv at LISA_VENV            -- created if absent
#   4. LISA installed editable '.[azure]'    -- the `lisa` CLI on PATH
#   5. the project's own requirements        -- so the in-venv driver + ACS
#                                               e-mail notifier work too
#   6. a smoke check                         -- `lisa --help` resolves
#
# Re-runnable: re-cloning -> git pull, existing venv reused. Override any of the
# paths/refs via env vars (defaults match the workflow's LISA_VENV default).
#
#   LISA_VENV   venv dir            (default: $HOME/lisa-venv)
#   LISA_SRC    engine checkout dir (default: $HOME/azfiles-lisa)
#   LISA_REPO   engine git URL      (default: https://github.com/Azure/azfiles-lisa.git)
#   LISA_REF    engine git ref      (default: main)
#   REQUIREMENTS  project reqs file (default: autodetected from repo root)
#
# Usage (on the runner):
#   bash phase3/setup_lisa.sh
set -euo pipefail

LISA_VENV="${LISA_VENV:-$HOME/lisa-venv}"
LISA_SRC="${LISA_SRC:-$HOME/azfiles-lisa}"
LISA_REPO="${LISA_REPO:-https://github.com/Azure/azfiles-lisa.git}"
LISA_REF="${LISA_REF:-main}"

# Repo root = two levels up from this script (phase3/ -> repo root).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
REQUIREMENTS="${REQUIREMENTS:-$_REPO_ROOT/requirements.txt}"

log() { printf '\n=== %s ===\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. System build dependencies (LISA's azure extra builds a few native wheels).
#    Best-effort: needs sudo; if unavailable we warn and continue (a CI image
#    may already have them baked in).
# ---------------------------------------------------------------------------
APT_PKGS=(git gcc libgirepository1.0-dev libcairo2-dev qemu-utils libvirt-dev
          python3-pip python3-venv unixodbc-dev pkg-config)
log "system deps (apt)"
if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "${APT_PKGS[@]}"
else
  echo "WARN: sudo/apt-get unavailable; assuming build deps already present."
fi

# ---------------------------------------------------------------------------
# 2. LISA engine source (clone or update).
# ---------------------------------------------------------------------------
log "LISA engine source -> $LISA_SRC ($LISA_REPO @ $LISA_REF)"
if [ -d "$LISA_SRC/.git" ]; then
  git -C "$LISA_SRC" fetch --depth 1 origin "$LISA_REF"
  git -C "$LISA_SRC" checkout -q "$LISA_REF"
  git -C "$LISA_SRC" reset --hard -q "origin/$LISA_REF"
else
  git clone --depth 1 --branch "$LISA_REF" "$LISA_REPO" "$LISA_SRC"
fi

# ---------------------------------------------------------------------------
# 3. Python venv.
# ---------------------------------------------------------------------------
log "venv -> $LISA_VENV"
if [ ! -x "$LISA_VENV/bin/python" ]; then
  python3 -m venv "$LISA_VENV"
fi
# shellcheck disable=SC1091
source "$LISA_VENV/bin/activate"
python -m pip install --upgrade pip wheel

# ---------------------------------------------------------------------------
# 4. LISA engine (editable, azure extra only -- NOT libvirt).
# ---------------------------------------------------------------------------
log "pip install LISA (editable, .[azure])"
pip install --editable "$LISA_SRC/.[azure]" --config-settings editable_mode=compat

# ---------------------------------------------------------------------------
# 5. Project requirements (the driver runs in THIS venv and lazily imports the
#    Phase 1 ACS notifier, which needs azure-communication-email etc.).
# ---------------------------------------------------------------------------
if [ -f "$REQUIREMENTS" ]; then
  log "pip install project requirements ($REQUIREMENTS)"
  pip install -r "$REQUIREMENTS"
else
  echo "WARN: $REQUIREMENTS not found; skipping project requirements."
fi

# ---------------------------------------------------------------------------
# 6. Smoke check.
# ---------------------------------------------------------------------------
log "verify"
lisa --help >/dev/null 2>&1 && echo "OK: lisa CLI resolves in $LISA_VENV"
echo "LISA engine: $LISA_SRC"
echo "Activate with: source $LISA_VENV/bin/activate"
echo "Set the workflow repo variable LISA_VENV=$LISA_VENV"
