# JobShturman: one-line install for Windows (PowerShell).
#
#   irm https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.ps1 | iex
#
# Downloads the latest release (one file with the program and the skill) and runs the skill's
# installer from it: uv (if missing), the hrs program with its own Python 3.12, then `hrs setup`
# connects hrs to Claude Code and Codex and installs the skill. With $env:HRS_SOURCE set, the skill
# comes from the main branch and hrs from that source.
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 redraws its progress bar for every downloaded block, which makes downloads
# many times slower; older Windows does not offer TLS 1.2 by default.
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$Repo = 'ZhdanDesign/jobshturman'
function Say($msg) { [Console]::Error.WriteLine("JobShturman: $msg") }
$Work = Join-Path ([IO.Path]::GetTempPath()) ("hrs-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    $Source = $env:HRS_SOURCE
    if ($Source) {
        $url = "https://github.com/$Repo/archive/refs/heads/main.zip"
    } else {
        $url = "https://github.com/$Repo/releases/latest/download/jobshturman-wheels.zip"
    }
    Say "скачиваю установщик с GitHub: $url"
    $zip = Join-Path $Work 'jobshturman.zip'
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 300 -Uri $url -OutFile $zip
    } catch {
        Say "не удалось скачать: $($_.Exception.Message). Проверьте, открывается ли github.com в браузере."
        return
    }
    $dir = Join-Path $Work 'jobshturman'
    Expand-Archive -Path $zip -DestinationPath $dir
    if ($Source) {
        $top = Get-ChildItem -Path $dir -Directory | Select-Object -First 1
        $bootstrap = Join-Path $top.FullName 'plugin\skills\hrshturman\scripts\bootstrap.ps1'
    } else {
        $bootstrap = Join-Path $dir 'skill\scripts\bootstrap.ps1'
        $Source = $dir
    }
    powershell -NoProfile -ExecutionPolicy Bypass -File $bootstrap $Source
    if ($LASTEXITCODE -ne 0) { Say 'установка не завершилась. Пришлите в поддержку весь текст из этого окна.' }
} finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}
