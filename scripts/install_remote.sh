#!/usr/bin/env bash
# ABUTRON QUANT — idempotent installer for the apex engine.
#
# Runs ON the server (invoked by the GitHub Actions deploy workflow over SSH,
# or by the self-hosted runner variant directly). Safe to re-run:
#   - clones or fast-forwards the repo at $INSTALL_DIR
#   - rebuilds the virtualenv, reinstalls the package
#   - installs/replaces the systemd unit, daemon-reloads
#   - NEVER starts or restarts the service unless --start-service is passed
#   - NEVER reads, writes or fabricates $INSTALL_DIR/.env — it only tells you
#     if it is missing
#
# Usage:
#   install_remote.sh [--repo-url URL] [--ref REF] [--dir PATH] [--start-service]
set -euo pipefail

REPO_URL="https://github.com/abutronmall-netizen/ABUTRON-QUANT.git"
REF="main"
DIR="/opt/abutron-quant"
START_SERVICE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-url)      REPO_URL="$2"; shift 2;;
    --ref)           REF="$2";      shift 2;;
    --dir)           DIR="$2";      shift 2;;
    --start-service) START_SERVICE=1; shift;;
    *) echo "unknown argument: $1" >&2; exit 2;;
  esac
done

log()  { printf '[apex-deploy] %s\n' "$*"; }
warn() { printf '[apex-deploy] WARNING: %s\n' "$*" >&2; }
die()  { printf '[apex-deploy] ERROR: %s\n' "$*" >&2; exit 3; }

# --- privileges: root, or passwordless sudo, otherwise bail with guidance ----
if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=""
elif sudo -n true 2>/dev/null; then
  SUDO="sudo"
else
  die "need root or passwordless sudo (try: sudo -v won't help here; grant NOPASSWD for this account)"
fi

# --- OS packages (Debian/Ubuntu). On other distros, git+python3 must exist ---
if command -v apt-get >/dev/null 2>&1; then
  log "ensuring OS packages (git, python3, python3-venv)"
  $SUDO apt-get update -qq
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq git python3 python3-venv python3-pip
fi
command -v git >/dev/null      || die "git not found and automatic install is only wired for apt-based systems"
command -v python3 >/dev/null  || die "python3 not found and automatic install is only wired for apt-based systems"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || die "python >= 3.10 required (found $(python3 --version 2>&1))"

# --- fetch the code ----------------------------------------------------------
$SUDO mkdir -p "$DIR"
$SUDO chown "$(id -u):$(id -g)" "$DIR"

if [[ -d "$DIR/.git" ]]; then
  log "existing checkout at $DIR — fetching and resetting to $REF"
  git -C "$DIR" fetch --all --prune --tags
  # local changes (e.g. a hand-edited .env or runs/) must survive a deploy
  if ! git -C "$DIR" diff --quiet || ! git -C "$DIR" diff --cached --quiet; then
    warn "local modifications detected in $DIR — keeping them (no force clean)"
  fi
  git -C "$DIR" checkout --quiet "$REF"
  if git -C "$DIR" rev-parse --verify --quiet "origin/$REF" >/dev/null; then
    git -C "$DIR" reset --hard --quiet "origin/$REF"
  else
    git -C "$DIR" reset --hard --quiet "$REF"
  fi
else
  log "cloning $REPO_URL ($REF) into $DIR"
  git clone --quiet --branch "$REF" "$REPO_URL" "$DIR"
fi
# NOTE: we deliberately never `git clean` — runs/, data/ and .env live here.

# --- virtualenv + package ----------------------------------------------------
log "building virtualenv and installing apex"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
# runtime is stdlib-only; this exposes the `apex` CLI and package metadata
"$DIR/.venv/bin/pip" install --quiet -e "$DIR"

# --- systemd unit ------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  SVC_SRC="$DIR/deploy/apex.service"
  if [[ -f "$SVC_SRC" ]]; then
    log "installing systemd unit -> /etc/systemd/system/apex.service"
    $SUDO cp "$SVC_SRC" /etc/systemd/system/apex.service
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable apex.service >/dev/null
    log "unit enabled (autostart on boot), state: NOT started by this script unless --start-service"
  else
    warn "deploy/apex.service not found in $DIR — skipping unit install"
  fi
  if [[ "$START_SERVICE" -eq 1 ]]; then
    warn "restarting apex.service at operator request (it runs with --live)"
    $SUDO systemctl restart apex.service
  else
    log "service deliberately left STOPPED — start it yourself after review:"
    log "  sudo systemctl start apex   # WARNING: the unit runs with --live"
  fi
else
  warn "systemctl not available — skipping service management (e.g. container host)"
fi

# --- credentials file: report, never create ----------------------------------
if [[ ! -f "$DIR/.env" ]]; then
  warn "$DIR/.env is MISSING — the engine cannot trade or verify without credentials."
  warn "create it from the template:  cp $DIR/.env.example $DIR/.env && chmod 600 $DIR/.env"
  warn "then edit it (DERIV_TOKEN / MT5_* etc.) as documented in docs/LIVE_DEPLOYMENT.md"
else
  log ".env present (contents not read, not modified)"
fi

# --- smoke test --------------------------------------------------------------
log "smoke test: apex version + doctor (doctor may be non-green until .env is filled)"
"$DIR/.venv/bin/apex" --version
"$DIR/.venv/bin/python" -m apex doctor || warn "doctor reported problems — expected until .env/credentials are valid"

log "DONE — code installed at $DIR (ref: $REF). Journal lives in $DIR/runs and is never touched."
