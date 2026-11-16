# Evgen4ik TV — установка на этот компьютер (Windows 10/11) через WSL2 + Ubuntu.
# Запуск: «ЗАПУСТИТЬ установку на компьютер.bat» (сам попросит права администратора). Повторный запуск безопасен: продолжит или обновит.
# Удаление: «Удалить с компьютера.bat»
param([switch]$Remove)
$ErrorActionPreference = "Continue"   # ошибки проверяем сами: в PowerShell 5.1 «Stop» ломается на выводе wsl.exe в stderr
$env:WSL_UTF8 = "1"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dir = Join-Path $env:ProgramData "Evgen4ikTV"
$TaskName = "Evgen4ik TV (WSL)"
$RuleName = "Evgen4ik TV (WSL)"
$WslVmId = "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}"   # идентификатор WSL для брандмауэра Hyper-V
$Ports = @(80, 8080, 8081, 8090, 8888, 18080)   # для удаления: все порты, которые мог выбрать установщик

# ---------- права администратора ----------
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  $a = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
  if ($Remove) { $a += "-Remove" }
  Start-Process powershell -Verb RunAs -ArgumentList $a
  exit
}

function Say($t) { Write-Host "› $t" -ForegroundColor Cyan }
function Ok($t) { Write-Host "✔ $t" -ForegroundColor Green }
function Warn($t) { Write-Host "! $t" -ForegroundColor Yellow }
function Fail($t) { Write-Host "✖ $t" -ForegroundColor Red; Read-Host "`nНажмите Enter, чтобы закрыть"; exit 1 }
function YesNo($q, $def = "д") { $a = Read-Host "$q (д/н) [$def]"; if (-not $a) { $a = $def }; return ($a -match '^(д|да|y|yes)$') }
function Wsl([string[]]$a) { $o = & wsl.exe @a 2>&1; $script:WslRc = $LASTEXITCODE; return (($o | Out-String) -replace "`0", "") }
function WslRoot([string]$sh) {   # bash-скрипт внутри Ubuntu от root (через base64 — без проблем с кавычками)
  $b = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($sh.Replace("`r", "")))
  & wsl.exe -d $script:Distro -u root -- bash -c "echo $b | base64 -d | bash" | Out-Host
  return $LASTEXITCODE
}
function Get-Distros {
  if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { return @() }
  $t = Wsl @("-l", "-v")
  $r = @()
  foreach ($l in ($t -split "`r?`n")) {
    if ($l -match '^\s*\*?\s*(\S+)\s+(\S+)\s+([12])\s*$') { $r += [pscustomobject]@{ Name = $Matches[1]; Ver = [int]$Matches[3] } }
  }
  return $r
}
function Get-Lan {   # настоящая домашняя сеть: физический адаптер с реальным шлюзом (не VPN, не WSL)
  $skip = "vEthernet|WSL|VirtualBox|VMware|Hyper-V|happ|xray|v2ray|sing-?box|clash|mihomo|tun|tap|wintun|wireguard|wg|amnezia|vpn|outline|zerotier|tailscale|radmin|hamachi"
  $all = @(Get-NetIPConfiguration | Where-Object {
    $_.NetAdapter.Status -eq "Up" -and $_.InterfaceAlias -notmatch $skip -and $_.NetAdapter.InterfaceDescription -notmatch $skip -and
    ($_.IPv4DefaultGateway | Where-Object { $_.NextHop -and $_.NextHop -ne "0.0.0.0" }) -and
    ($_.IPv4Address.IPAddress | Where-Object { $_ -match '^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)' })
  })
  $hw = @($all | Where-Object { $_.NetAdapter.HardwareInterface })
  $pick = if ($hw.Count) { $hw } else { $all }
  return ($pick | Sort-Object { $_.NetIPv4Interface.InterfaceMetric } | Select-Object -First 1)
}
function Get-FreePort([int[]]$cands) {   # свободный в Windows порт (wslrelay — это сама Ubuntu, он не мешает)
  foreach ($p in $cands) {
    $l = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Where-Object { (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName -notmatch "wslrelay" }
    if (-not $l) { return $p }
  }
  return $null
}

# ---------- удаление ----------
if ($Remove) {
  $st = if (Test-Path "$Dir\pc.json") { Get-Content "$Dir\pc.json" -Raw | ConvertFrom-Json } else { $null }
  Write-Host "`nУдаление Evgen4ik TV с этого компьютера" -ForegroundColor Yellow
  if (-not (YesNo "Удалить автозапуск, правила брандмауэра и программу внутри Ubuntu?" "н")) { exit }
  if ($st -and $st.distro) {   # сначала программа в Ubuntu: если не удалилась — Windows-часть не трогаем
    $script:Distro = $st.distro
    $rc = WslRoot "echo 'УДАЛИТЬ' | e4tv uninstall"
    if ($rc -ne 0) { Fail "Программа в Ubuntu не удалилась (код $rc). Автозапуск, брандмауэр и проброс портов оставлены." }
  }
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  if (Get-Command Remove-NetFirewallHyperVRule -ErrorAction SilentlyContinue) { Remove-NetFirewallHyperVRule -Name "E4TV-WSL" -ErrorAction SilentlyContinue }
  foreach ($p in $Ports) {
    netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 2>$null | Out-Null
    if ($st -and $st.ip) { netsh interface portproxy delete v4tov4 listenport=$p listenaddress=$($st.ip) 2>$null | Out-Null }
  }
  Ok "Готово. Ubuntu и настройки WSL (.wslconfig) оставлены."
  Write-Host "Удалить Ubuntu целиком (со всеми файлами внутри!): wsl --unregister $($st.distro)" -ForegroundColor DarkGray
  Read-Host "`nНажмите Enter, чтобы закрыть"; exit
}

Write-Host "`n  Evgen4ik TV — установка на этот компьютер" -ForegroundColor Yellow
Write-Host "  Сервер будет работать внутри Windows (WSL2 + Ubuntu): дома, пока компьютер включён.`n" -ForegroundColor DarkGray

# ---------- проверки ----------
$build = [int](Get-CimInstance Win32_OperatingSystem).BuildNumber
if ($build -lt 19041) { Fail "Нужна Windows 10 версии 2004 или новее (сейчас сборка $build). Обновите Windows." }
$Net = if ($build -ge 22621) { "mirrored" } else { "portproxy" }
$inst = Join-Path $Here "evgen4ik-tv-install.sh"
if (-not (Test-Path $inst)) { Fail "Рядом нет файла evgen4ik-tv-install.sh — положите его в ту же папку." }
$lan = Get-Lan
if (-not $lan) { Fail "Не найдено подключение к домашней сети (нет шлюза). Подключитесь к роутеру и запустите снова." }
$Ip = $lan.IPv4Address.IPAddress | Where-Object { $_ -match '^(192\.168\.|10\.|172\.)' } | Select-Object -First 1
# прошлые пробросы портов этой программы не считаются «занятыми» — убираем их перед выбором порта
foreach ($p in @(80, 8080, 8081, 8090, 8888, 18080)) {
  foreach ($la in @("0.0.0.0", $Ip)) { netsh interface portproxy delete v4tov4 listenport=$p listenaddress=$la 2>$null | Out-Null }
}
if (-not (Get-FreePort @(80))) {
  $l = Get-NetTCPConnection -LocalPort 80 -State Listen | Select-Object -First 1
  Warn "Порт 80 на этом компьютере занят программой «$((Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue).ProcessName)». Закройте её, иначе плееры не достучатся."
}
$AltPort = Get-FreePort @(8080, 8081, 8090, 8888, 18080)
if (-not $AltPort) { Fail "Не нашёл свободный порт для редактора (8080, 8081, 8090, 8888, 18080 заняты)." }
$Ports = @(80, $AltPort)

Write-Host "Будет сделано:" -ForegroundColor White
Write-Host "  • WSL2 и Ubuntu 24.04 (если ещё нет) — встроенная в Windows подсистема Linux"
Write-Host "  • в Ubuntu включится systemd, внутри поставится Evgen4ik TV"
if ($Net -eq "mirrored") { Write-Host "  • сеть WSL — «зеркальный» режим (файл $env:USERPROFILE\.wslconfig, копия сохранится): Ubuntu получит адрес компьютера"
  Write-Host "    (режим общий для всех дистрибутивов WSL и Docker Desktop)" -ForegroundColor DarkGray }
else { Write-Host "  • проброс портов 80 и $AltPort из Windows в Ubuntu (обновляется при каждом входе)" }
Write-Host "  • брандмауэр: входящие 80 и $AltPort только для частной (домашней) сети"
Write-Host "  • автозапуск при входе в Windows: задача «$TaskName»"
Write-Host "  • адрес для плееров: $Ip (компьютер в сети «$($lan.InterfaceAlias)»)"
$have = @(Get-Distros | Where-Object { $_.Name -match '^Ubuntu' })
if ($have.Count) { Warn "Будет использована уже установленная у вас $($have[0].Name): в неё добавятся systemd, nginx и программа." }
if (-not (YesNo "`nПродолжить?")) { exit }

# ---------- WSL и Ubuntu ----------
New-Item -ItemType Directory -Force -Path $Dir | Out-Null
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { Fail "В Windows нет wsl.exe. Установите обновления Windows и запустите снова." }
$ds = @(Get-Distros | Where-Object { $_.Name -match '^Ubuntu' })
if ($ds.Count -eq 0) {
  Say "Ставлю WSL и Ubuntu 24.04 (скачивается ~500 МБ)..."
  Write-Host "  Когда Ubuntu попросит — придумайте имя пользователя и пароль Linux (латиницей)." -ForegroundColor White
  Write-Host "  Когда появится строка вида  имя@компьютер:~$  — напечатайте  exit  и нажмите Enter." -ForegroundColor White
  & wsl.exe --install -d Ubuntu-24.04
  $ds = @(Get-Distros | Where-Object { $_.Name -match '^Ubuntu' })
  if ($ds.Count -eq 0) {
    Write-Host "`nНужна перезагрузка компьютера." -ForegroundColor Yellow
    Write-Host "После перезагрузки откроется окно Ubuntu — придумайте имя и пароль, напечатайте exit."
    Write-Host "Затем снова запустите «ЗАПУСТИТЬ установку на компьютер.bat» — установка продолжится."
    Write-Host "Если Windows пишет про виртуализацию — включите её в BIOS (Intel VT-x / AMD SVM)." -ForegroundColor DarkGray
    Read-Host "`nНажмите Enter, чтобы закрыть"; exit
  }
}
$pick = $ds | Where-Object { $_.Name -eq "Ubuntu-24.04" } | Select-Object -First 1
if (-not $pick) { $pick = $ds | Select-Object -First 1 }
$script:Distro = $pick.Name
Ok "Ubuntu: $Distro"
if ($pick.Ver -ne 2) { Say "Перевожу $Distro на WSL2..."; & wsl.exe --set-version $Distro 2; if ($LASTEXITCODE -ne 0) { Fail "Не удалось перевести на WSL2. Включите виртуализацию в BIOS." } }
if ($Net -eq "mirrored") { Say "Обновляю WSL..."; & wsl.exe --update 2>&1 | Out-Null }

# ---------- systemd ----------
$rc = WslRoot @'
f=/etc/wsl.conf; touch $f
if grep -qE '^\s*systemd\s*=' $f; then sed -i -E 's/^\s*systemd\s*=.*/systemd=true/' $f
elif grep -q '^\[boot\]' $f; then sed -i '/^\[boot\]/a systemd=true' $f
else printf '\n[boot]\nsystemd=true\n' >> $f; fi
'@
if ($rc -ne 0) { Fail "Не удалось включить systemd в Ubuntu" }
Ok "systemd включён"

# ---------- сеть ----------
if ($Net -eq "mirrored") {
  $wc = Join-Path $env:USERPROFILE ".wslconfig"
  $t = if (Test-Path $wc) { [string](Get-Content $wc -Raw) } else { "" }
  if ($t -notmatch '(?m)^\s*networkingMode\s*=\s*mirrored') {
    if (Test-Path $wc) { Copy-Item $wc "$wc.bak-evgen4ik" -Force }
    if ($t -match '(?m)^\s*networkingMode\s*=') { $t = $t -replace '(?m)^\s*networkingMode\s*=.*$', 'networkingMode=mirrored' }
    elseif ($t -match '(?m)^\s*\[wsl2\]\s*$') { $t = $t -replace '(?m)^\s*\[wsl2\]\s*$', "[wsl2]`r`nnetworkingMode=mirrored" }
    else { $t = $t.TrimEnd() + "`r`n[wsl2]`r`nnetworkingMode=mirrored`r`n" }
    [IO.File]::WriteAllText($wc, $t.TrimStart(), (New-Object Text.UTF8Encoding $false))
  }
  Ok "Сеть WSL: зеркальный режим"
}
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Ports -Profile Private -ErrorAction Stop | Out-Null
if (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue) {
  Remove-NetFirewallHyperVRule -Name "E4TV-WSL" -ErrorAction SilentlyContinue
  try { New-NetFirewallHyperVRule -Name "E4TV-WSL" -DisplayName $RuleName -Direction Inbound -VMCreatorId $WslVmId -Protocol TCP -LocalPorts $Ports -Action Allow -Profiles Private -ErrorAction Stop | Out-Null }
  catch { Warn "Правило брандмауэра Hyper-V не создано: $($_.Exception.Message)" }
}
$prof = Get-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex -ErrorAction SilentlyContinue
if ($prof -and $prof.NetworkCategory -eq "Public") {
  Warn "Сеть «$($prof.Name)» отмечена как общественная — телевизоры не достучатся до компьютера."
  if (YesNo "Сделать её частной (домашней)?") { Set-NetConnectionProfile -InterfaceIndex $lan.InterfaceIndex -NetworkCategory Private; Ok "Сеть стала частной" }
}
Ok "Брандмауэр: открыты 80 и $AltPort для домашней сети"

# ---------- перезапуск Ubuntu с systemd ----------
Say "Перезапускаю Ubuntu..."
& wsl.exe --shutdown
Start-Sleep 3
$state = ""
for ($i = 0; $i -lt 40; $i++) {
  $state = (Wsl @("-d", $Distro, "-u", "root", "--", "systemctl", "is-system-running")).Trim()
  if ($state -match '^(running|degraded)') { break }
  Start-Sleep 2
}
if ($state -notmatch '^(running|degraded)') { Warn "systemd отвечает «$state» — пробую продолжить" } else { Ok "Ubuntu запущена (systemd: $state)" }

# зеркальный режим включается не везде (VPN-адаптеры, политики) — проверяем, что получилось на деле
if ($Net -eq "mirrored") {
  $nm = (Wsl @("-d", $Distro, "-u", "root", "--", "wslinfo", "--networking-mode")).Trim()
  if ($nm -ne "mirrored") {
    Warn "Зеркальный режим сети WSL на этом компьютере не включился (сейчас «$nm») — настраиваю проброс портов."
    $Net = "portproxy"
    $wc = Join-Path $env:USERPROFILE ".wslconfig"
    if (Test-Path "$wc.bak-evgen4ik") { Copy-Item "$wc.bak-evgen4ik" $wc -Force }
    elseif (Test-Path $wc) {
      $t = ([string](Get-Content $wc -Raw)) -replace '(?m)^\s*networkingMode\s*=\s*mirrored\s*$\r?\n?', ''
      if ($t.Trim() -match '^\[wsl2\]$' -or -not $t.Trim()) { Remove-Item $wc -Force } else { [IO.File]::WriteAllText($wc, $t, (New-Object Text.UTF8Encoding $false)) }
    }
  } else { Ok "Сеть WSL: зеркальный режим работает" }
}
function Set-PortProxy {
  $wip = ((Wsl @("-d", $Distro, "-u", "root", "--", "hostname", "-I")) -split '\s+' | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
  if (-not $wip) { Warn "Не удалось узнать адрес Ubuntu для проброса портов"; return }
  foreach ($p in $Ports) {
    netsh interface portproxy delete v4tov4 listenport=$p listenaddress=0.0.0.0 2>$null | Out-Null
    netsh interface portproxy delete v4tov4 listenport=$p listenaddress=$Ip 2>$null | Out-Null
    netsh interface portproxy add v4tov4 listenport=$p listenaddress=$Ip connectport=$p connectaddress=$wip | Out-Null
  }
  Ok "Проброс портов: $Ip ($($Ports -join ', ')) -> Ubuntu $wip"
}
if ($Net -eq "portproxy") {
  Start-Service iphlpsvc -ErrorAction SilentlyContinue
  Set-PortProxy
}

# ---------- установка программы внутри Ubuntu ----------
Copy-Item $inst "$Dir\evgen4ik-tv-install.sh" -Force
Say "Запускаю установщик Evgen4ik TV — отвечайте на вопросы ниже.`n"
& wsl.exe -d $Distro -u root -- bash /mnt/c/ProgramData/Evgen4ikTV/evgen4ik-tv-install.sh --address $Ip --alt-port $AltPort
if ($LASTEXITCODE -ne 0) { Fail "Установщик внутри Ubuntu завершился с ошибкой (см. выше). Исправьте и запустите «ЗАПУСТИТЬ установку на компьютер.bat» снова." }

# ---------- автозапуск ----------
@"
# Evgen4ik TV: при входе в Windows поднимает Ubuntu и держит её включённой
`$env:WSL_UTF8 = "1"
`$D = "$Distro"
if ("$Net" -eq "portproxy") {
  `$ip = `$null
  for (`$i = 0; `$i -lt 30 -and -not `$ip; `$i++) {
    `$ip = ((wsl.exe -d `$D -u root -- hostname -I) -replace "``0", "" -split '\s+' | Where-Object { `$_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
    if (-not `$ip) { Start-Sleep 2 }
  }
  foreach (`$p in $($Ports -join ', ')) {
    netsh interface portproxy delete v4tov4 listenport=`$p listenaddress=0.0.0.0 | Out-Null
    netsh interface portproxy delete v4tov4 listenport=`$p listenaddress=$Ip | Out-Null
    if (`$ip) { netsh interface portproxy add v4tov4 listenport=`$p listenaddress=$Ip connectport=`$p connectaddress=`$ip | Out-Null }
  }
}
wsl.exe -d `$D -u root --exec sleep infinity
"@ | Set-Content -Path "$Dir\keepalive.ps1" -Encoding UTF8
@{ distro = $Distro; net = $Net; ip = $Ip; installed = (Get-Date -Format "dd.MM.yyyy HH:mm") } | ConvertTo-Json | Set-Content "$Dir\pc.json" -Encoding UTF8
$user = "$env:USERDOMAIN\$env:USERNAME"
$act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Dir\keepalive.ps1`""
$trg = New-ScheduledTaskTrigger -AtLogOn -User $user
$prn = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Principal $prn -Settings $set -Force | Out-Null
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName $TaskName
Ok "Автозапуск при входе в Windows настроен"

if (YesNo "Отключить спящий режим при питании от сети? (иначе ночью плейлисты пропадут)") {
  powercfg /change standby-timeout-ac 0 | Out-Null; Ok "Сон от сети отключён (экран гаснуть будет)"
}

# ---------- проверка снаружи Ubuntu ----------
Start-Sleep 4
$cfgJson = (Wsl @("-d", $Distro, "-u", "root", "--", "cat", "/etc/evgen4ik-tv/config.json"))
$port = if ($cfgJson -match '"editor_port":\s*(\d+)') { [int]$Matches[1] } else { 80 }
$url = if ($port -eq 80) { "http://$Ip/" } else { "http://${Ip}:$port/" }
try { $r = Invoke-WebRequest -Uri "${url}login" -UseBasicParsing -TimeoutSec 10 -MaximumRedirection 0 -ErrorAction Stop; $code = $r.StatusCode }
catch { $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 } }
if ($code -in 200, 302) { Ok "Редактор открывается по адресу $url" } else { Warn "Редактор по адресу $url не ответил (код $code). Подождите минуту и откройте вручную." }

Write-Host "`n══════════════ Готово! ══════════════" -ForegroundColor Green
Write-Host "  Редактор:  $url   (логин и пароль — выше, в выводе установщика)"
Write-Host "  Работает дома, пока компьютер включён и вы вошли в Windows."
Write-Host "  ВАЖНО: закрепите за компьютером адрес $Ip в роутере (резервирование DHCP)," -ForegroundColor Yellow
Write-Host "  иначе после смены адреса ссылки в телевизорах перестанут работать." -ForegroundColor Yellow
Write-Host "  Если адрес всё же сменился — запустите «ЗАПУСТИТЬ установку на компьютер.bat» снова и выберите «обновить»," -ForegroundColor DarkGray
Write-Host "  а затем вставьте в телевизоры новые ссылки на плейлисты: старые, с прежним адресом, работать не будут." -ForegroundColor DarkGray
Start-Process $url
Read-Host "`nНажмите Enter, чтобы закрыть"
