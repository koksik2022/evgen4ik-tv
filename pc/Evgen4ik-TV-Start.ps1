# Evgen4ik TV — меню первого запуска: куда ставим. Запуск: «Установить Evgen4ik TV.bat» в корне архива.
# Само ничего не ставит — передаёт управление установщику из нужной папки.
$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pc = Join-Path $Root "2 На этот компьютер (Windows)\Evgen4ik-TV-PC.ps1"
$Vds = Join-Path $Root "1 На сервер (VDS)\Evgen4ik-TV.ps1"

function Done { Read-Host "`nНажмите Enter, чтобы закрыть" | Out-Null; exit }
function Need($p) {
  if (Test-Path $p) { return $true }
  Write-Host "Не найден файл: $p" -ForegroundColor Red
  Write-Host "Распакуйте архив Evgen4ik-TV.zip целиком и запустите «Установить Evgen4ik TV.bat» из распакованной папки." -ForegroundColor Yellow
  return $false
}

Write-Host "`n  Evgen4ik TV — редактор IPTV-плейлистов" -ForegroundColor Yellow
Write-Host "`n  Куда ставим?`n"
Write-Host "  1 — на ЭТОТ компьютер с Windows" -ForegroundColor White
Write-Host "      работает дома, пока компьютер включён; нужны права администратора" -ForegroundColor DarkGray
Write-Host "  2 — на СЕРВЕР в интернете (VDS)" -ForegroundColor White
Write-Host "      работает всегда и везде; нужны IP сервера и пароль root" -ForegroundColor DarkGray
Write-Host "  3 — удалить с этого компьютера" -ForegroundColor White
Write-Host "  0 — выход`n" -ForegroundColor DarkGray

switch ((Read-Host "Выберите 1, 2, 3 или 0").Trim()) {
  "1" { if (Need $Pc) { & $Pc; exit } ; Done }   # установщик сам запросит права администратора и откроет своё окно
  "2" { if (Need $Vds) { & $Vds } ; Done }
  "3" { if (Need $Pc) { & $Pc -Remove; exit } ; Done }
  default { exit }
}
