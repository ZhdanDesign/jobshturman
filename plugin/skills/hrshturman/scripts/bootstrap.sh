#!/bin/sh
# HR Shturman installer for macOS and Linux.
#
#   sh bootstrap.sh [SOURCE]
#
# SOURCE (or $HRS_SOURCE) is where hrs comes from:
#   - a URL or path to the release zip with wheels (hrshturman-<version>-wheels.zip);
#   - a folder with those wheels;
#   - a checkout of the project (a folder with hrs/pyproject.toml), installed editable.
# Without SOURCE an hrs that is already installed is kept as is; otherwise the latest release
# from GitHub is installed.
#
# Steps: install uv if missing, install hrs with its own Python 3.12, then `hrs setup`
# connects it to Claude Code and Codex and copies this skill next to them.
set -eu

SKILL_DIR=$(cd "$(dirname "$0")/.." && pwd)
SOURCE=${1:-${HRS_SOURCE:-}}
DEFAULT_SOURCE="https://github.com/ZhdanDesign/hrshturman/releases/latest/download/hrshturman-wheels.zip"
FROM_PLUGIN=""
case "$SKILL_DIR" in */.claude/plugins/*) FROM_PLUGIN="--from-plugin" ;; esac

say() { printf '%s\n' "hrs-bootstrap: $*" >&2; }
fail() { say "$*"; exit 1; }

export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  say "ставлю uv (https://astral.sh/uv)"
  command -v curl >/dev/null 2>&1 || fail "нужен curl"
  curl -LsSf https://astral.sh/uv/install.sh | sh >&2
  command -v uv >/dev/null 2>&1 || fail "uv не установился"
fi

install_wheels() {
  ls "$1"/hrshturman-*.whl >/dev/null 2>&1 || fail "в $1 нет hrshturman-*.whl"
  uv tool install --force --python 3.12 --find-links "$1" hrshturman >&2
}

BIN=$(uv tool dir --bin)
if [ -z "$SOURCE" ] && [ ! -x "$BIN/hrs" ]; then
  SOURCE=$DEFAULT_SOURCE
fi
if [ -n "$SOURCE" ]; then
  WORK=$(mktemp -d)
  trap 'rm -rf "$WORK"' EXIT
  case "$SOURCE" in
    http://*|https://*)
      say "скачиваю $SOURCE"
      curl -fsSL -o "$WORK/hrs.zip" "$SOURCE" || fail "не удалось скачать $SOURCE"
      SOURCE="$WORK/hrs.zip" ;;
  esac
  if [ -f "$SOURCE/hrs/pyproject.toml" ]; then
    say "ставлю hrs из папки проекта $SOURCE"
    uv tool install --force --python 3.12 --editable "$SOURCE/hrs" >&2
  elif [ -d "$SOURCE" ]; then
    install_wheels "$SOURCE"
  elif [ -f "$SOURCE" ]; then
    mkdir -p "$WORK/wheels"
    uv run --no-project --python 3.12 python -m zipfile -e "$SOURCE" "$WORK/wheels" >&2
    install_wheels "$WORK/wheels"
  else
    fail "не нашёл источник $SOURCE"
  fi
fi

uv tool update-shell >/dev/null 2>&1 || true
"$BIN/hrs" setup --skill-dir "$SKILL_DIR" $FROM_PLUGIN
