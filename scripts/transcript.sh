#!/usr/bin/env bash
# didntwatch bootstrap wrapper.
#
# transcript.py runs on the Python standard library alone; what it needs is the
# fetching toolchain under .runtime/ :
#   bin/yt-dlp   — yt-dlp standalone build (bundles curl_cffi for browser TLS
#                  impersonation)
#   bgutil/      — bgutil PO-token provider: yt-dlp plugin + built script-mode
#                  server (server/build/generate_once.js)
#   node/        — local Node runtime, downloaded only when the system has no
#                  node >= 18 (bgutil needs Node to mint PO tokens)
#
# This wrapper provisions whatever is missing (idempotent, first run only),
# then execs transcript.py with the given arguments. Needs: python3, curl, tar.
#
# Usage:
#   scripts/transcript.sh fetch <url|id> [--lang xx]
#   scripts/transcript.sh list|get|find|batch|reindex ...

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNTIME="$SKILL_DIR/.runtime"
YTDLP="$RUNTIME/bin/yt-dlp"
BGUTIL="$RUNTIME/bgutil"
BGUTIL_BUILD="$BGUTIL/server/build/generate_once.js"
BGUTIL_TARBALL="https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/heads/master.tar.gz"
NODE_DIR="$RUNTIME/node"

log() { printf 'didntwatch: %s\n' "$1" >&2; }

# --- yt-dlp ------------------------------------------------------------------

ytdlp_asset() {
  case "$(uname -s)/$(uname -m)" in
    Linux/x86_64) echo "yt-dlp_linux" ;;
    Linux/aarch64 | Linux/arm64) echo "yt-dlp_linux_aarch64" ;;
    Darwin/*) echo "yt-dlp_macos" ;;
    *) return 1 ;;
  esac
}

provision_ytdlp() {
  [[ -x "$YTDLP" ]] && return 0
  local asset
  if ! asset="$(ytdlp_asset)"; then
    log "unsupported platform $(uname -s)/$(uname -m) — install yt-dlp manually as $YTDLP"
    return 1
  fi
  log "downloading yt-dlp standalone ($asset)…"
  mkdir -p "$RUNTIME/bin"
  curl -fL --progress-bar -o "$YTDLP.tmp" \
    "https://github.com/yt-dlp/yt-dlp/releases/latest/download/$asset"
  chmod +x "$YTDLP.tmp"
  mv "$YTDLP.tmp" "$YTDLP"
}

# --- Node (for bgutil) -------------------------------------------------------

node_major() { "$1" --version 2>/dev/null | sed 's/^v//; s/\..*//'; }

have_node() {
  local bin="$1" major
  major="$(node_major "$bin")" || return 1
  [[ "$major" =~ ^[0-9]+$ ]] && ((major >= 18))
}

provision_node() {
  have_node "$NODE_DIR/bin/node" && return 0
  have_node node && return 0
  local os arch plat file base="https://nodejs.org/dist/latest-v24.x"
  case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=darwin ;;
    *) return 1 ;;
  esac
  case "$(uname -m)" in
    x86_64) arch=x64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) return 1 ;;
  esac
  plat="$os-$arch"
  file="$(curl -fsSL "$base/SHASUMS256.txt" | grep -o "node-v[0-9.]*-$plat\.tar\.xz" | head -1)"
  if [[ -z "$file" ]]; then
    log "could not resolve a Node build for $plat"
    return 1
  fi
  log "downloading Node runtime ($file)…"
  rm -rf "$NODE_DIR.tmp"
  mkdir -p "$NODE_DIR.tmp"
  curl -fL --progress-bar "$base/$file" | tar -xJ --strip-components=1 -C "$NODE_DIR.tmp"
  rm -rf "$NODE_DIR"
  mv "$NODE_DIR.tmp" "$NODE_DIR"
}

# --- bgutil PO-token provider --------------------------------------------------

provision_bgutil() {
  [[ -f "$BGUTIL_BUILD" ]] && return 0
  log "fetching bgutil PO-token provider…"
  rm -rf "$BGUTIL.tmp"
  mkdir -p "$BGUTIL.tmp"
  curl -fL --progress-bar "$BGUTIL_TARBALL" | tar -xz --strip-components=1 -C "$BGUTIL.tmp"
  log "building bgutil server (npm install + tsc)…"
  (cd "$BGUTIL.tmp/server" && npm install --no-audit --no-fund >&2 && npx tsc >&2)
  rm -rf "$BGUTIL"
  mv "$BGUTIL.tmp" "$BGUTIL"
}

# --- main ---------------------------------------------------------------------

command -v python3 >/dev/null 2>&1 || {
  log "python3 not found — install Python 3.9+"
  exit 1
}

# transcript.py degrades gracefully when pieces are missing (clear JSON error
# for yt-dlp, plugin args simply omitted for bgutil), so provisioning failures
# warn instead of aborting the run.
provision_ytdlp || log "continuing without yt-dlp — fetch/list/batch will fail until it is installed"
if provision_node; then
  [[ -d "$NODE_DIR/bin" ]] && export PATH="$NODE_DIR/bin:$PATH"
  provision_bgutil || log "bgutil unavailable — PO-token-gated videos may be blocked"
else
  log "no Node >= 18 and none could be downloaded — skipping bgutil (some videos may be blocked)"
fi

exec python3 "$SKILL_DIR/scripts/transcript.py" "$@"
