# HR Shturman: one-line install for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/ZhdanDesign/hrshturman/main/install.ps1 | iex
#
# Downloads the skill from GitHub and runs its installer: uv (if missing), the hrs program from the
# latest release, then `hrs setup` connects hrs to Claude Code and Codex and installs the skill.
$ErrorActionPreference = 'Stop'
$Repo = 'ZhdanDesign/hrshturman'
$Work = Join-Path ([IO.Path]::GetTempPath()) ("hrs-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    $zip = Join-Path $Work 'main.zip'
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $Work
    $bootstrap = Join-Path $Work 'hrshturman-main\plugin\skills\hrshturman\scripts\bootstrap.ps1'
    powershell -NoProfile -ExecutionPolicy Bypass -File $bootstrap $env:HRS_SOURCE
} finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
