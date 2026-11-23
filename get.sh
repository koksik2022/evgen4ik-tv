#!/bin/bash
# Evgen4ik TV — установка одной командой:
#   curl -fsSL https://raw.githubusercontent.com/koksik2022/evgen4ik-tv/main/get.sh | sudo bash
#   wget -qO- https://raw.githubusercontent.com/koksik2022/evgen4ik-tv/main/get.sh | sudo bash
# Параметры установщика (без вопросов):  ... | sudo bash -s -- --mode direct --access login --yes
# Скачивает установщик версии 3.19, сверяет SHA-256 и запускает его. Источники пробуются по очереди.
main() {
  set -euo pipefail
  local VER="3.19" SHA="8781033b904ed0031f8da9a8d0d2a89bf1c0a967a5a182ebf8fae7d834b6f370" NAME="evgen4ik-tv-install.sh"
  local SOURCES=(
    "https://raw.githubusercontent.com/koksik2022/evgen4ik-tv/7aa8691aeaaa72825e23709d6d4a117b36e5979d/$NAME"
    "https://github.com/koksik2022/evgen4ik-tv/releases/download/v3.19/$NAME"
  )
  [ -n "${E4_SOURCE:-}" ] && SOURCES=("$E4_SOURCE")   # свой адрес: E4_SOURCE=https://.../evgen4ik-tv-install.sh
  if [ "$(id -u)" -ne 0 ]; then echo "Нужны права root: добавьте sudo перед bash" >&2; exit 1; fi

  fetch() {   # fetch URL FILE
    if command -v curl >/dev/null 2>&1; then curl -fsSL --retry 2 --connect-timeout 15 -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then wget -q -T 15 -t 2 -O "$2" "$1"
    else return 127; fi
  }
  if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    echo "› Ставлю curl..."; (apt-get update -qq && apt-get install -y -qq curl) >/dev/null 2>&1 || { echo "Не удалось установить curl" >&2; exit 1; }
  fi

  E4_TMP=$(mktemp -d); trap 'rm -rf "${E4_TMP:-}"' EXIT
  local f="$E4_TMP/$NAME" src got=""
  for src in "${SOURCES[@]}"; do
    echo "› Скачиваю Evgen4ik TV $VER: $src"
    if fetch "$src" "$f" && [ -s "$f" ]; then
      if echo "$SHA  $f" | sha256sum -c --status; then got=1; break; fi
      echo "! Контрольная сумма не совпала — пробую следующий источник" >&2
    else
      echo "! Источник недоступен — пробую следующий" >&2
    fi
  done
  [ -n "$got" ] || { echo "✖ Не удалось скачать установщик ни из одного источника" >&2; exit 1; }
  echo "✔ Файл проверен (SHA-256)"
  bash "$f" "$@"   # вопросы установщик читает сам из /dev/tty
}
main "$@"
