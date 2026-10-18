#!/bin/sh
# JobShturman: one-line install for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.sh | sh
#
# Downloads the skill from GitHub and runs its installer: uv (if missing), the hrs program from the
# latest release, then `hrs setup` connects hrs to Claude Code and Codex and installs the skill.
set -eu
REPO="ZhdanDesign/jobshturman"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" | tar xz -C "$WORK"
sh "$WORK"/*/plugin/skills/hrshturman/scripts/bootstrap.sh "${HRS_SOURCE:-}"
