# Evgen4ik TV — помощник для Windows: установка на VDS и вход в редактор через SSH-туннель
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cfg = Join-Path $env:APPDATA "Evgen4ik-TV"; New-Item -ItemType Directory -Force -Path $Cfg | Out-Null
$SrvFile = Join-Path $Cfg "server.txt"

function Need-Ssh {
  if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "Не найден ssh. Включите: Параметры -> Приложения -> Дополнительные компоненты -> «Клиент OpenSSH»." -ForegroundColor Red
    throw "ssh не найден"
  }
}
function Ask($q, $def) { $a = Read-Host ("{0}{1}" -f $q, $(if ($def) { " [$def]" } else { "" })); if ([string]::IsNullOrWhiteSpace($a)) { $def } else { $a.Trim() } }
function Get-Server {
  $old = if (Test-Path $SrvFile) { (Get-Content $SrvFile -Raw).Trim() -split "\s+" } else { @("", "root", "22") }
  $ip = Ask "Адрес сервера (IP или домен)" $old[0]; if (-not $ip) { throw "Адрес не указан" }
  $user = Ask "Пользователь SSH" $(if ($old[1]) { $old[1] } else { "root" })
  $port = Ask "Порт SSH" $(if ($old[2]) { $old[2] } else { "22" })
  Set-Content -Path $SrvFile -Value "$ip $user $port" -Encoding ASCII
  return @{ ip = $ip; user = $user; port = $port }
}

function Install-Vds {
  Need-Ssh
  $inst = Join-Path $Here "evgen4ik-tv-install.sh"
  if (-not (Test-Path $inst)) { throw "Рядом нет файла evgen4ik-tv-install.sh" }
  $s = Get-Server
  $dst = if ($s.user -eq "root") { "/root/evgen4ik-tv-install.sh" } else { "/tmp/evgen4ik-tv-install.sh" }
  $sudo = if ($s.user -eq "root") { "" } else { "sudo " }
  Write-Host "`nКопирую установщик на сервер (ssh спросит пароль сервера)..." -ForegroundColor Cyan
  scp -P $s.port $inst ("{0}@{1}:{2}" -f $s.user, $s.ip, $dst)
  if ($LASTEXITCODE -ne 0) { throw "Не удалось скопировать файл на сервер" }
  Write-Host "Запускаю установку — отвечайте на вопросы в этом окне.`n" -ForegroundColor Cyan
  ssh -t -p $s.port ("{0}@{1}" -f $s.user, $s.ip) "${sudo}bash $dst"
}

function Open-Tunnel {
  Need-Ssh
  $s = Get-Server
  $busy = Get-NetTCPConnection -LocalPort 8095 -State Listen -ErrorAction SilentlyContinue
  if (-not $busy) {
    Write-Host "`nОткрываю туннель (в отдельном окне ssh может спросить пароль сервера; окно не закрывайте)..." -ForegroundColor Cyan
    Start-Process ssh -ArgumentList @("-N", "-o", "ServerAliveInterval=30", "-L", "8095:127.0.0.1:8095", "-p", $s.port, ("{0}@{1}" -f $s.user, $s.ip))
    for ($i = 0; $i -lt 60; $i++) { Start-Sleep 1; if (Get-NetTCPConnection -LocalPort 8095 -State Listen -ErrorAction SilentlyContinue) { break } }
  }
  if (Get-NetTCPConnection -LocalPort 8095 -State Listen -ErrorAction SilentlyContinue) { Start-Process "http://localhost:8095/" ; Write-Host "Редактор открыт: http://localhost:8095" -ForegroundColor Green }
  else { Write-Host "Туннель не поднялся. Проверьте адрес и пароль в окне ssh." -ForegroundColor Red }
}

function Show-Info {
  Need-Ssh
  $s = Get-Server
  $sudo = if ($s.user -eq "root") { "" } else { "sudo " }
  ssh -t -p $s.port ("{0}@{1}" -f $s.user, $s.ip) "${sudo}e4tv info"
}

Write-Host "`n  Evgen4ik TV" -ForegroundColor Yellow
Write-Host "  1 — Установить или обновить на сервере (VDS)"
Write-Host "  2 — Открыть редактор через SSH-туннель (если выбран вход «только через туннель»)"
Write-Host "  3 — Показать ссылки (редактор, плейлист, EPG)"
$c = Read-Host "`nВыберите 1, 2 или 3"
try {
  switch ($c) { "1" { Install-Vds } "2" { Open-Tunnel } "3" { Show-Info } default { Write-Host "Ничего не выбрано" } }
} catch { Write-Host ("Ошибка: " + $_.Exception.Message) -ForegroundColor Red }
