# JobShturman installer for Windows.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 [SOURCE]
#
# SOURCE (or $env:HRS_SOURCE) is where hrs comes from:
#   - a URL or path to the release zip with wheels (hrshturman-<version>-wheels.zip);
#   - a folder with those wheels;
#   - a checkout of the project (a folder with hrs\pyproject.toml), installed editable.
# Without SOURCE the latest release from GitHub is installed, unless the installed hrs is already
# that version or newer (or GitHub cannot be asked: then the installed hrs is kept).
#
# Steps: install uv if missing, install hrs with its own Python 3.12, then `hrs setup`
# connects it to Claude Code and Codex and copies this skill next to them.
param([string]$Source = $env:HRS_SOURCE)
$ErrorActionPreference = 'Stop'

$SkillDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$FromPlugin = @()
if ($SkillDir -match '[\\/]\.claude[\\/]plugins[\\/]') { $FromPlugin = @('--from-plugin') }

function Say($msg) { [Console]::Error.WriteLine("JobShturman: $msg") }
function Fail($msg) { Say $msg; exit 1 }
function Run {
    # Native tools report progress on stderr; only the exit code means failure.
    $exe, $rest = $args
    $ErrorActionPreference = 'Continue'
    & $exe @rest 2>&1 | ForEach-Object { [Console]::Error.WriteLine("$_") }
    if ($LASTEXITCODE -ne 0) { Fail "$exe завершился с кодом $LASTEXITCODE" }
}

$LocalBin = Join-Path $HOME '.local\bin'
$env:Path = "$LocalBin;$env:Path"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'ставлю uv (https://astral.sh/uv)'
    powershell -NoProfile -ExecutionPolicy Bypass -Command 'irm https://astral.sh/uv/install.ps1 | iex' | Out-Null
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Fail 'uv не установился' }
}

function Install-Wheels($dir) {
    if (-not (Get-ChildItem -Path $dir -Filter 'hrshturman-*.whl' -ErrorAction SilentlyContinue)) {
        Fail "в $dir нет hrshturman-*.whl"
    }
    Run uv tool install --force --python 3.12 --find-links $dir hrshturman
}

$Bin = (& uv tool dir --bin).Trim()
$Hrs = Join-Path $Bin $(if ($IsLinux -or $IsMacOS) { 'hrs' } else { 'hrs.exe' })
$Latest = 'https://github.com/ZhdanDesign/jobshturman/releases/latest/download/jobshturman-wheels.zip'
function Version-Of($text) { ([regex]'\d+(\.\d+){2,3}').Match("$text").Value }
if (-not $Source) {
    if (-not (Test-Path $Hrs)) {
        $Source = $Latest
    } else {
        $have = ''; $newest = ''
        try { $have = Version-Of (& $Hrs --version 2>$null) } catch {}
        try { $newest = Version-Of (Invoke-RestMethod -Uri 'https://api.github.com/repos/ZhdanDesign/jobshturman/releases/latest').tag_name } catch {}
        if (-not $have -or ($newest -and [version]$have -lt [version]$newest)) {
            Say "обновляю программу с версии $have до $newest"
            $Source = $Latest
        }
    }
}
$Work = $null
try {
    if ($Source) {
        $Work = Join-Path ([IO.Path]::GetTempPath()) ("hrs-" + [guid]::NewGuid())
        New-Item -ItemType Directory -Path $Work | Out-Null
        if ($Source -match '^https?://') {
            Say "скачиваю $Source"
            $zip = Join-Path $Work 'hrs.zip'
            Invoke-WebRequest -UseBasicParsing -Uri $Source -OutFile $zip
            $Source = $zip
        }
        if (Test-Path (Join-Path $Source 'hrs\pyproject.toml')) {
            Say "ставлю программу из папки проекта $Source"
            Run uv tool install --force --python 3.12 --editable (Join-Path $Source 'hrs')
        } elseif (Test-Path $Source -PathType Container) {
            Install-Wheels $Source
        } elseif (Test-Path $Source -PathType Leaf) {
            $wheels = Join-Path $Work 'wheels'
            Expand-Archive -Path $Source -DestinationPath $wheels
            Install-Wheels $wheels
        } else {
            Fail "не нашёл источник $Source"
        }
    }
} finally {
    if ($Work) { Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue }
}

& uv tool update-shell 2>&1 | Out-Null
& $Hrs setup --skill-dir $SkillDir @FromPlugin
exit $LASTEXITCODE
