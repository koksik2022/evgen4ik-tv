#!/bin/bash
# Собирает dist/: две папки «что запускать», инструкцию и архив Evgen4ik-TV.zip для друзей
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist; mkdir -p dist
inst=dist/evgen4ik-tv-install.sh
{ cat install.sh; echo "__PAYLOAD__"; tar czf - --owner=0 --group=0 --exclude=__pycache__ --exclude='*.bak-*' src | base64; } > "$inst"
chmod 755 "$inst"
S="dist/Evgen4ik-TV/1 На сервер (VDS)"; P="dist/Evgen4ik-TV/2 На этот компьютер (Windows)"
mkdir -p "$S" "$P"
cp "$inst" "$S/"; cp "$inst" "$P/"

# --- установка одной командой: dist/get.sh (версия и SHA-256 вшиваются) ---
GH_REPO="${GH_REPO:-koksik2022/evgen4ik-tv}"     # репозиторий GitHub (Releases: тег v<версия>)
E4_MIRROR="${E4_MIRROR:-}"                        # зеркало, напр. https://tv.example.com/e4tv (пусто — без зеркала)
VER=$(sed -n 's/^E4_VER="\(.*\)"$/\1/p' install.sh)
SHA=$(sha256sum "$inst" | cut -d' ' -f1)
MLINE=""; [ -n "$E4_MIRROR" ] && MLINE="    \"${E4_MIRROR%/}/v$VER/\$NAME\""
E4_REF="${E4_REF:-v$VER}"   # тег или коммит, где лежит evgen4ik-tv-install.sh
sed -e "s|@REF@|$E4_REF|g" -e "s|@GH_REPO@|$GH_REPO|g" -e "s|@VER@|$VER|g" -e "s|@SHA@|$SHA|g" get.sh.in \
  | awk -v m="$MLINE" '{ if ($0=="@MIRROR_LINE@") { if (m!="") print m } else print }' > dist/get.sh
chmod 755 dist/get.sh
echo "$SHA  evgen4ik-tv-install.sh" > dist/evgen4ik-tv-install.sh.sha256
cp "$inst" evgen4ik-tv-install.sh   # копия в корне репозитория: get.sh берёт её по тегу v<версия>
echo "get.sh: версия $VER, sha256 $SHA, repo $GH_REPO, зеркало ${E4_MIRROR:-нет}"
cp pc/Evgen4ik-TV.ps1 "$S/"; cp pc/Evgen4ik-TV-PC.ps1 "$P/"
cp pc/Evgen4ik-TV-Start.ps1 "dist/Evgen4ik-TV/"   # меню «куда ставим» для единого файла запуска
bat() { printf '@echo off\r\nchcp 65001 >nul\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0%s" %s\r\n%s' "$2" "$3" "$4" > "$1"; }
bat "$S/ЗАПУСТИТЬ установку на сервер.bat" Evgen4ik-TV.ps1 "" $'echo.\r\npause\r\n'
bat "$P/ЗАПУСТИТЬ установку на компьютер.bat" Evgen4ik-TV-PC.ps1 "" ""
bat "$P/Удалить с компьютера.bat" Evgen4ik-TV-PC.ps1 "-Remove" ""
# единый файл запуска: только латиница (кодировка cmd), русский текст ошибки — через -EncodedCommand
printf '@echo off\r\nif not exist "%%~dp0Evgen4ik-TV-Start.ps1" (\r\n  powershell -NoProfile -EncodedCommand VwByAGkAdABlAC0ASABvAHMAdAAgACcAJwA7ACAAVwByAGkAdABlAC0ASABvAHMAdAAgACcAHQQ1BCAAPQQwBDkENAQ1BD0EIABEBDAEOQQ7BCAARQB2AGcAZQBuADQAaQBrAC0AVABWAC0AUwB0AGEAcgB0AC4AcABzADEAIABABE8ENAQ+BDwEIABBBCAATQRCBDgEPAQgAC4AYgBhAHQALgAnACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFIAZQBkADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwAfBD4ERQQ+BDYENQQsACAAMARABEUEOAQyBCAAPgRCBDoEQARLBEIEIAAxBDUENwQgAEAEMARBBD8EMAQ6BD4EMgQ6BDgELgAnACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFkAZQBsAGwAbwB3ADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwAdBDAENgQ8BDgEQgQ1BCAAPQQwBCAARQB2AGcAZQBuADQAaQBrAC0AVABWAC4AegBpAHAAIAA/BEAEMAQyBD4EOQQgADoEPQQ+BD8EOgQ+BDkEIAAtAD4AIACrABgENwQyBDsENQRHBEwEIAAyBEEEUQQmILsALAAnACAALQBGAG8AcgBlAGcAcgBvAHUAbgBkAEMAbwBsAG8AcgAgAFkAZQBsAGwAbwB3ADsAIABXAHIAaQB0AGUALQBIAG8AcwB0ACAAJwA+BEIEOgRABD4EOQRCBDUEIABABDAEQQQ/BDAEOgQ+BDIEMAQ9BD0EQwROBCAAPwQwBD8EOgRDBCAAOAQgADcEMAQ/BEMEQQRCBDgEQgQ1BCAAqwAjBEEEQgQwBD0EPgQyBDgEQgRMBCAARQB2AGcAZQBuADQAaQBrACAAVABWAC4AYgBhAHQAuwAgAD4EQgRCBEMENAQwBC4AJwAgAC0ARgBvAHIAZQBnAHIAbwB1AG4AZABDAG8AbABvAHIAIABZAGUAbABsAG8AdwA=\r\n  pause\r\n  exit /b 1\r\n)\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0Evgen4ik-TV-Start.ps1"\r\n' > "dist/Evgen4ik-TV/Установить Evgen4ik TV.bat"
python3 - <<'P'
t = """EVGEN4IK TV — С ЧЕГО НАЧАТЬ

1. Распакуйте архив: правой кнопкой по Evgen4ik-TV.zip → «Извлечь всё…».
2. В распакованной папке дважды щёлкните «Установить Evgen4ik TV.bat».
3. Выберите, куда ставим: 1 — этот компьютер, 2 — сервер в интернете (VDS).

Если Windows спросит «Запустить этот файл?» или покажет «Windows защитила
ваш компьютер» — нажмите «Подробнее» → «Выполнить в любом случае».
Программа не подписана цифровой подписью, поэтому Windows так предупреждает.


ЧТО НУЖНО ДЛЯ УСТАНОВКИ НА КОМПЬЮТЕР (пункт 1)

Ставить Ubuntu вручную НЕ нужно — установщик сделает это сам.

Заранее:
  • Windows 10 (версия 2004 и новее) или Windows 11;
  • права администратора — на вопрос Windows ответьте «Да»;
  • включённая виртуализация в BIOS (Intel VT-x / AMD SVM) — обычно уже включена;
  • интернет: скачается около 500 МБ; на диске нужно примерно 2 ГБ.

Руками, один раз:
  1. Если Windows попросит перезагрузку — перезагрузитесь и снова запустите
     «Установить Evgen4ik TV.bat», установка продолжится.
  2. Откроется окно Ubuntu: придумайте имя и пароль Linux (латиницей) и запишите их.
  3. Когда появится строка вида  имя@компьютер:~$  — напечатайте  exit  и нажмите Enter.
  4. Ответьте на вопросы установщика — можно просто нажимать Enter.

Ограничения:
  • работает дома, пока компьютер включён и вы вошли в Windows;
  • закрепите за компьютером IP-адрес в роутере, иначе ссылки в телевизорах
    перестанут работать после смены адреса;
  • режим с HLS Proxy — только на процессорах Intel/AMD (не на ARM/Snapdragon).


ЧТО НУЖНО ДЛЯ УСТАНОВКИ НА СЕРВЕР (пункт 2)

Ubuntu на компьютере не нужна.
  • арендованный VDS с Ubuntu 22.04 / 24.04 или Debian 12;
  • IP сервера и пароль root — из письма хостера;
  • на компьютере — Windows 10/11 (SSH там встроен).
Работает всегда и везде: дома, на даче, с телефона.


Папки «1» и «2» — те же установщики по отдельности, их можно не трогать.
Подробная инструкция — README.md (открывается Блокнотом).
"""
open("dist/Evgen4ik-TV/ИНСТРУКЦИЯ.txt", "w", encoding="utf-8-sig", newline="\r\n").write(t)
P
cp README.md dist/Evgen4ik-TV/README.md
(cd dist && python3 -c "
import zipfile, os
with zipfile.ZipFile('Evgen4ik-TV.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for r, _, fs in os.walk('Evgen4ik-TV'):
        for f in fs: z.write(os.path.join(r, f))
")
find dist -type f | sort
