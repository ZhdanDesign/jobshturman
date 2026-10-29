# JobShturman: проверка, можно ли установить программу на этот компьютер с Windows.
#
#   $w=New-Object Net.WebClient;$w.Encoding=[Text.Encoding]::UTF8;iex $w.DownloadString('https://tools.owai.ru/jobshturman/check.ps1')
#
# Only reads and downloads into memory: nothing is installed, nothing on the disk is changed. At the
# end the report goes to the clipboard so the person can paste it to support. The whole script is one
# script block: under `irm | iex` an `exit` would close the person's window.
& {
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$report = New-Object Collections.Generic.List[string]
$problems = New-Object Collections.Generic.List[string]
function Line($mark, $text, $color) {
    Write-Host "  $mark " -ForegroundColor $color -NoNewline
    Write-Host $text
    $report.Add("$mark $text")
}
function Ok($text) { Line '[ок]' $text 'Green' }
function Warn($text) { Line '[!!]' $text 'Yellow' }
function Bad($text, $todo) { Line '[нет]' $text 'Red'; $problems.Add($todo) }
function Head($text) { Write-Host ''; Write-Host $text -ForegroundColor Cyan; $report.Add("== $text") }

Write-Host ''
Write-Host 'JobShturman: проверяю, можно ли установить программу. Ничего не устанавливаю и не меняю.' -ForegroundColor Cyan

Head 'Система'
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) { Ok "$($os.Caption), сборка $($os.BuildNumber)" } else { Warn 'не удалось узнать версию Windows' }
$arch = $env:PROCESSOR_ARCHITEW6432; if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
if ($arch -eq 'AMD64') { Ok 'процессор x64' }
elseif ($arch -eq 'ARM64') { Warn 'процессор ARM64: установка должна пройти, но мы её на ARM ещё не проверяли' }
else { Bad "процессор $arch" 'Нужна 64-разрядная Windows: на 32-разрядной программа не работает.' }
Ok "PowerShell $($PSVersionTable.PSVersion)"
if ($env:USERPROFILE -match '[^\x00-\x7F]') { Ok 'в имени папки пользователя есть русские буквы: это не мешает' }

$drive = (Split-Path -Qualifier $env:LOCALAPPDATA)
$disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'" -ErrorAction SilentlyContinue
if ($disk) {
    $gb = [math]::Round($disk.FreeSpace / 1GB, 1)
    if ($gb -ge 1) { Ok "свободно на диске ${drive}: $gb ГБ" }
    else { Bad "свободно на диске ${drive}: $gb ГБ" "Освободите на диске $drive хотя бы 1 ГБ: программа с Python занимает около 500 МБ." }
}

$policy = Get-ExecutionPolicy -List | Where-Object { $_.Scope -in 'MachinePolicy', 'UserPolicy' -and $_.ExecutionPolicy -ne 'Undefined' }
if ($policy) {
    Warn "запуск скриптов ограничен групповой политикой: $(($policy | ForEach-Object { $_.ExecutionPolicy }) -join ', ')"
    $problems.Add('Запуск скриптов PowerShell ограничен администратором компьютера. Если установка не пойдёт, попросите его разрешить запуск скриптов.')
} else { Ok 'запуск скриптов PowerShell не запрещён' }

Head 'Интернет: откуда установка всё скачивает'
$proxy = [Net.WebRequest]::GetSystemWebProxy().GetProxy([Uri]'https://github.com')
if ($proxy.Host -ne 'github.com') { Warn "в системе задан прокси $($proxy.Host)" }
function Probe($name, $url, $todo) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $req = [Net.HttpWebRequest]::Create($url)
        $req.Timeout = 20000; $req.ReadWriteTimeout = 60000; $req.UserAgent = 'JobShturman-check'
        $req.AddRange(0, 2MB - 1)  # the first 2 MB are enough to see the speed
        $resp = $req.GetResponse()
        $stream = $resp.GetResponseStream(); $buf = New-Object byte[] 65536; $size = 0
        while (($n = $stream.Read($buf, 0, $buf.Length)) -gt 0) { $size += $n }
        $resp.Close()
        $sec = [math]::Max($sw.Elapsed.TotalSeconds, 0.01)
        $kb = [math]::Round($size / 1KB)
        $speed = [math]::Round($size / 1KB / $sec)
        if ($size -gt 100KB -and $speed -lt 200) {
            Warn "$name открывается, но медленно: $kb КБ за $([math]::Round($sec, 1)) с ($speed КБ/с)"
            $problems.Add("$name отдаёт файлы медленно: установка займёт много времени. Попробуйте другую сеть.")
        } else {
            Ok "$name ($([math]::Round($sec, 1)) с)"
        }
    } catch {
        $msg = $_.Exception.Message
        if ($_.Exception.InnerException) { $msg = $_.Exception.InnerException.Message }
        Bad "$name недоступен: $msg" $todo
    }
}
$vpn = 'Попробуйте другую сеть (например, раздачу с телефона) или VPN и запустите проверку ещё раз.'
Probe 'GitHub, выпуск JobShturman' 'https://github.com/ZhdanDesign/jobshturman/releases/latest/download/jobshturman-wheels.zip' "Не скачивается выпуск JobShturman с GitHub. $vpn"
Probe 'GitHub, установщик' 'https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.ps1' "Не открывается raw.githubusercontent.com, откуда берётся команда установки. $vpn"
Probe 'GitHub API, проверка обновлений' 'https://api.github.com/repos/ZhdanDesign/jobshturman/releases/latest' "Не отвечает api.github.com: программа поставится, но не будет сама обновляться. $vpn"
Probe 'astral.sh, установщик uv' 'https://astral.sh/uv/install.ps1' "Не открывается astral.sh, откуда ставится uv. $vpn"
Probe 'GitHub, сам uv' 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip' "Не скачивается uv с GitHub. $vpn"
Probe 'PyPI, список библиотек' 'https://pypi.org/simple/six/' "Не открывается pypi.org, откуда берутся библиотеки. $vpn"
Probe 'PyPI, файлы библиотек' 'https://files.pythonhosted.org/packages/b7/ce/149a00dd41f10bc29e5921b496af8b574d8413afcd5e30dfa0ed46c2cc5e/six-1.17.0-py2.py3-none-any.whl' "Не скачиваются файлы с files.pythonhosted.org. $vpn"
Probe 'сервер ключей JobShturman' 'https://tools.owai.ru/jobshturman/keys/v1/tariffs' 'Не отвечает наш сервер ключей tools.owai.ru: без него ключ не проверится. Напишите нам в поддержку.'

Head 'Что уже стоит'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) { $uv = Get-Command (Join-Path $HOME '.local\bin\uv.exe') -ErrorAction SilentlyContinue }
if ($uv) {
    Ok "uv $((& $uv.Source --version 2>$null) -replace '^uv ', '')"
    $bin = (& $uv.Source tool dir --bin 2>$null)
    $hrs = if ($bin) { Join-Path "$bin".Trim() 'hrs.exe' }
    if ($hrs -and (Test-Path $hrs)) { Ok "JobShturman $((& $hrs --version 2>$null) -join ' ')" }
    else { Line '[--]' 'JobShturman ещё не установлен' 'Gray' }
} else { Line '[--]' 'uv ещё не установлен, установка поставит его сама' 'Gray' }
foreach ($agent in @(@{ Name = 'Codex'; Cmd = 'codex'; Config = '.codex\config.toml' },
                     @{ Name = 'Claude Code'; Cmd = 'claude'; Config = '.claude.json' })) {
    $config = Join-Path $HOME $agent.Config
    $found = (Get-Command $agent.Cmd -ErrorAction SilentlyContinue) -or (Test-Path $config)
    if (-not $found) { Line '[--]' "$($agent.Name) не найден" 'Gray'; continue }
    if ((Test-Path $config) -and (Select-String -Path $config -Pattern 'hrshturman' -SimpleMatch -Quiet)) {
        Ok "$($agent.Name): JobShturman подключён"
    } else { Ok "$($agent.Name) есть, JobShturman к нему ещё не подключён" }
}

Write-Host ''
if ($problems.Count -eq 0) {
    Write-Host 'Итог: всё в порядке, установка должна пройти.' -ForegroundColor Green
    Write-Host 'Запустите в обычном окне PowerShell:'
    Write-Host '  irm https://raw.githubusercontent.com/ZhdanDesign/jobshturman/main/install.ps1 | iex' -ForegroundColor White
    Write-Host 'В первый раз она скачивает около 120 МБ и идёт несколько минут.'
    $report.Add('Итог: всё в порядке')
} else {
    Write-Host 'Итог: есть препятствия, вот что сделать:' -ForegroundColor Yellow
    $i = 1
    foreach ($p in ($problems | Select-Object -Unique)) { Write-Host "  $i. $p"; $i++ }
    $report.Add("Итог: препятствий $($problems.Count)")
}
try {
    Set-Clipboard -Value ($report -join "`r`n")
    Write-Host ''
    Write-Host 'Отчёт скопирован. Если нужна помощь, вставьте его (Ctrl+V) в сообщение поддержке.' -ForegroundColor Cyan
} catch {}
Write-Host ''
}
