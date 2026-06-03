#!/usr/bin/env bash
# tldw bootstrap wrapper.
#
# Ensures an isolated Python environment with youtube-transcript-api exists in
# the skill folder, then runs transcript.py with whatever arguments it was
# given. Idempotent: the environment is created on first run only.
#
# Install strategy, in order of preference:
#   1. uv         (fastest; brings its own pip-less venv)
#   2. python -m venv + pip   (when ensurepip is available)
#   3. venv --without-pip + get-pip.py   (last-resort bootstrap over network)
#
# Usage:
#   scripts/transcript.sh list  <url|id>
#   scripts/transcript.sh fetch <url|id> [--lang xx]

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$SKILL_DIR/.venv"
PY="$VENV_DIR/bin/python"
REQ="$SKILL_DIR/requirements.txt"
STAMP="$VENV_DIR/.tldw-installed"

log() { printf 'tldw: %s\n' "$1" >&2; }

create_venv() {
  if command -v uv >/dev/null 2>&1; then
    log "creating venv with uv"
    uv venv "$VENV_DIR" >&2
  elif python3 -c 'import ensurepip' >/dev/null 2>&1; then
    log "creating venv with python -m venv"
    python3 -m venv "$VENV_DIR"
  else
    log "creating venv without pip, bootstrapping pip via get-pip.py"
    python3 -m venv --without-pip "$VENV_DIR"
    curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$PY" - >&2
  fi
}

install_deps() {
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r "$REQ" >&2
  else
    "$PY" -m pip install -r "$REQ" >&2
  fi
}

if [[ ! -x "$PY" ]]; then
  create_venv
fi

# (Re)install if deps were never installed or requirements changed.
if [[ ! -f "$STAMP" || "$REQ" -nt "$STAMP" ]]; then
  log "installing dependencies"
  install_deps
  touch "$STAMP"
fi

exec "$PY" "$SKILL_DIR/scripts/transcript.py" "$@"
