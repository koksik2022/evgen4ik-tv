#!/bin/bash
# Собирает dist/: две папки «что запускать», инструкцию и архив Evgen4ik-TV.zip для друзей
set -euo pipefail
cd "$(dirname "$0")"
rm -rf dist; mkdir -p dist
inst=dist/evgen4ik-tv-install.sh
{ cat install.sh; echo "__PAYLOAD__"; tar czf - --owner=0 --group=0 --exclude=__pycache__ src | base64; } > "$inst"
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
sed -e "s|@GH_REPO@|$GH_REPO|g" -e "s|@VER@|$VER|g" -e "s|@SHA@|$SHA|g" get.sh.in \
  | awk -v m="$MLINE" '{ if ($0=="@MIRROR_LINE@") { if (m!="") print m } else print }' > dist/get.sh
chmod 755 dist/get.sh
echo "$SHA  evgen4ik-tv-install.sh" > dist/evgen4ik-tv-install.sh.sha256
cp "$inst" evgen4ik-tv-install.sh   # копия в корне репозитория: get.sh берёт её по тегу v<версия>
echo "get.sh: версия $VER, sha256 $SHA, repo $GH_REPO, зеркало ${E4_MIRROR:-нет}"
cp pc/Evgen4ik-TV.ps1 "$S/"; cp pc/Evgen4ik-TV-PC.ps1 "$P/"
bat() { printf '@echo off\r\nchcp 65001 >nul\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "%%~dp0%s" %s\r\n%s' "$2" "$3" "$4" > "$1"; }
bat "$S/ЗАПУСТИТЬ установку на сервер.bat" Evgen4ik-TV.ps1 "" $'echo.\r\npause\r\n'
bat "$P/ЗАПУСТИТЬ установку на компьютер.bat" Evgen4ik-TV-PC.ps1 "" ""
bat "$P/Удалить с компьютера.bat" Evgen4ik-TV-PC.ps1 "-Remove" ""
python3 - <<'P'
t = """EVGEN4IK TV — С ЧЕГО НАЧАТЬ

Куда ставим?

1) НА СЕРВЕР В ИНТЕРНЕТЕ (VDS) — работает всегда, дома и в дороге.
   Откройте папку «1 На сервер (VDS)»
   и дважды щёлкните «ЗАПУСТИТЬ установку на сервер.bat».
   Понадобятся IP сервера и пароль root (Ubuntu 22.04/24.04 или Debian 12).

2) НА ЭТОТ КОМПЬЮТЕР С WINDOWS — работает только дома, пока компьютер включён.
   Откройте папку «2 На этот компьютер (Windows)»
   и дважды щёлкните «ЗАПУСТИТЬ установку на компьютер.bat».

Остальные файлы в папках запускать не нужно — они служебные.
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
