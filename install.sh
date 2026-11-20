#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  Evgen4ik TV — установщик редактора IPTV-плейлистов               ║
# ║  Ubuntu 22.04 / 24.04, Debian 12. Запуск:  sudo bash <этот файл>  ║
# ╚══════════════════════════════════════════════════════════════════╝
# Без вопросов:  --mode direct|hls --domain tv.example.com --email me@mail.ru
#                --access login|tunnel --user admin --password СЕКРЕТ --address 192.168.1.10 --yes
#                --port 8090 (плейлисты без домена, если 80 занят) --alt-port 8091 (редактор в режиме HLS)
# Повторный запуск обновляет программу и сохраняет настройки.
set -uo pipefail

E4_VER="3.9"
HLSP_VER="8.4.8"
HLSP_URL="https://www.hls-proxy.com/downloads/${HLSP_VER}/hls-proxy-${HLSP_VER}.linux-x64.zip"
HLSP_SHA256="96a2faad73df375e2ce1b92466f21f4389e07b79a038435343659c984e16b587"   # SHA-256 архива 8.4.8 (сверено 16.11.2026); пусто — не сверяется
APP=/opt/evgen4ik-tv
ETC=/etc/evgen4ik-tv
DATA=/var/lib/evgen4ik-tv
PL=/var/lib/evgen4ik-tv-pl
HLS=/opt/hls-proxy
ACME=/var/www/e4tv-acme
CREDS=/root/evgen4ik-tv-доступ.txt
EPG_DEFAULT="http://epg.it999.ru/edem.xml.gz"

# ---------- оформление ----------
if [ -t 1 ]; then B=$'\e[1m'; D=$'\e[2m'; Y=$'\e[33m'; G=$'\e[32m'; R=$'\e[31m'; C=$'\e[36m'; N=$'\e[0m'; else B= D= Y= G= R= C= N=; fi
say()  { echo -e "${C}›${N} $*"; }
ok()   { echo -e "${G}✔${N} $*"; }
warn() { echo -e "${Y}!${N} $*"; }
die()  { echo -e "${R}✖ $*${N}" >&2; exit 1; }
ask()  { local q=$1 def=${2:-} a; if [ "$YES" = 1 ]; then REPLY=$def; return; fi
         read -r -p "$(echo -e "${B}${q}${N}${def:+ ${D}[${def}]${N}}: ")" a </dev/tty; REPLY=${a:-$def}; }
yesno(){ ask "$1 (д/н)" "${2:-д}"; case "${REPLY,,}" in д|да|y|yes|"") return 0;; *) return 1;; esac; }
rnd()  { python3 -c "import secrets,string;a=string.ascii_letters+string.digits;print(''.join(secrets.choice(a) for _ in range($1)))"; }

# ---------- параметры ----------
MODE='' DOMAIN='' EMAIL='' ACCESS='' USERNAME='' PASSWORD='' YES=0 ACTION='' ADDR='' ALT_PORT='' STREAMS='' HTTP_PORT='' NEWDOMAIN=''
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) MODE=$2; shift;; --domain) DOMAIN=$2; shift;; --email) EMAIL=$2; shift;;
    --access) ACCESS=$2; shift;; --user) USERNAME=$2; shift;; --password) PASSWORD=$2; shift;;
    --streams) STREAMS=$2; shift;;   # сколько каналов одновременно разрешает провайдер (режим hls); 0 — без лимита
    --alt-port) ALT_PORT=$2; shift;;   # порт редактора в режиме hls (по умолчанию 8080, с доменом 8443)
    --port) HTTP_PORT=$2; shift;;      # порт плейлистов без домена (по умолчанию 80; занят — подбирается сам)
    --address) ADDR=$2; shift;;   # адрес для плееров (домашний ПК: IP в домашней сети)
    --yes|-y) YES=1;; --update) ACTION=update;; --reconfigure) ACTION=reconfigure;;
    --set-domain) NEWDOMAIN=$2; ACTION=update; shift;;   # обновить и подключить домен (HTTPS), остальные настройки оставить
    -h|--help) sed -n '2,9p' "$0"; exit 0;;
    *) die "Неизвестный параметр: $1";;
  esac; shift
done

# ---------- файлы программы: рядом (src/) или внутри этого файла ----------
locate_src() {
  local here; here=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)
  if [ -n "$here" ] && [ -f "$here/src/app.py" ]; then SRC=$here/src; return; fi
  if grep -aq '^__PAYLOAD__$' "$0" 2>/dev/null; then
    TMPD=$(mktemp -d); trap 'rm -rf "$TMPD"' EXIT
    sed '1,/^__PAYLOAD__$/d' "$0" | base64 -d | tar xz -C "$TMPD" || die "Файл установщика повреждён — скачайте его заново"
    SRC=$TMPD/src; return
  fi
  die "Не найдены файлы программы. Запускайте файл целиком: sudo bash evgen4ik-tv-install.sh"
}

conf_get() { python3 -c 'import json,sys
try: print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception: print("")' "$ETC/config.json" "$1"; }

# ---------- проверки ----------
precheck() {
  [ "$(id -u)" = 0 ] || die "Запустите от root: sudo bash $0"
  [ -d /run/systemd/system ] || die "Нужен systemd (обычный VDS с Ubuntu/Debian). В контейнере без systemd установка не сработает."
  . /etc/os-release 2>/dev/null || true
  case "${ID:-}" in ubuntu|debian) ;; *) warn "Проверено на Ubuntu 22.04/24.04 и Debian 12, у вас: ${PRETTY_NAME:-неизвестно}. Продолжаю.";; esac
  command -v apt-get >/dev/null || die "Нужен apt (Ubuntu/Debian)"
}

public_ip() {
  local ip
  for u in https://api.ipify.org https://ifconfig.me/ip https://ipv4.icanhazip.com; do
    ip=$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d '[:space:]') && [[ $ip =~ ^[0-9.]+$ ]] && { echo "$ip"; return; }
  done
  hostname -I 2>/dev/null | awk '{print $1}'
}

lan_ip() { ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[0-9.]+' | head -1; }
is_private() { [[ $1 =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.) ]]; }
is_wsl() { grep -qi microsoft /proc/version 2>/dev/null; }

choose_address() {   # VDS — внешний адрес; домашний ПК/мини-ПК за роутером — адрес в домашней сети
  LAN=$(lan_ip)
  if is_wsl && [[ ${LAN:-} =~ ^172\. ]]; then
    warn "WSL работает в режиме сети NAT ($LAN) — телевизоры дома его не увидят."
    warn "Включите зеркальный режим: в %UserProfile%\\.wslconfig строки [wsl2] и networkingMode=mirrored, затем wsl --shutdown."
  fi
  [ -n "${LAN:-}" ] && is_private "$LAN" && [ "$LAN" != "$EXT_IP" ] || return 0
  local def=2; { is_wsl || [[ $LAN =~ ^192\.168\. ]]; } && def=1
  echo
  echo -e "${B}Сервер стоит за роутером.${N} По какому адресу плееры будут открывать плейлисты?"
  echo -e "  ${B}1${N} — домашний ${LAN} ${D}(только дома, в той же сети; для ПК и мини-ПК)${N}"
  echo -e "  ${B}2${N} — внешний ${EXT_IP} ${D}(из любого места, но на роутере нужен проброс портов, а у провайдера — белый IP)${N}"
  ask "Ваш выбор" "$def"
  [ "$REPLY" = 1 ] && { IP=$LAN; HOME_LAN=1; }
  return 0
}

port_busy() {   # занят ли порт чем-то кроме nginx и hls-proxy (наших программ)
  ss -ltnpH "sport = :$1" 2>/dev/null | grep -vi -e nginx -e hls-proxy | grep -q LISTEN
}
who_port() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'users:(("[^"]*' | cut -d'"' -f2 | head -1; }
free_port() {   # первый свободный порт начиная с $1, кроме $2
  local p=$1
  while [ "$p" -le 65000 ]; do
    if [ "$p" != "${2:-}" ] && ! port_busy "$p"; then echo "$p"; return; fi
    p=$((p + 1))
  done
}
sfx() { case "$1" in 80|443) ;; *) echo ":$1";; esac; }   # «:порт» в адресе, если порт не стандартный

# Порты 80/443 и 8080/8443 бывают заняты (например, WEB-прокси Telegram или другой сайт).
# Тогда без домена работаем на свободных портах; домен (нужны 80 и 443) на таком сервере не подключить.
resolve_ports() {
  PORT_NOTE=""
  if [ -n "$DOMAIN" ] && { port_busy 80 || port_busy 443; }; then
    warn "Порты 80/443 заняты ($(who_port 80)$(port_busy 443 && echo ", $(who_port 443)")) — домен на этом сервере не подключить."
    warn "Редактор будет работать по http на свободном порту."
    DOMAIN=""; EMAIL=""
  fi
  if [ -z "$DOMAIN" ]; then
    HTTP_PORT=${HTTP_PORT:-80}
    if port_busy "$HTTP_PORT"; then
      local o=$HTTP_PORT; HTTP_PORT=$(free_port 8090)
      PORT_NOTE="${PORT_NOTE}порт $o занят ($(who_port "$o")) → плейлисты на $HTTP_PORT; "
    fi
  else
    HTTP_PORT=443
  fi
  if [ "$MODE" = hls ]; then
    ALT_PORT=${ALT_PORT:-$( [ -n "$DOMAIN" ] && echo 8443 || echo 8080 )}
    if [ "$ALT_PORT" = "$HTTP_PORT" ] || port_busy "$ALT_PORT"; then
      local o=$ALT_PORT; ALT_PORT=$(free_port $((HTTP_PORT == 80 || HTTP_PORT == 443 ? 8091 : HTTP_PORT + 1)) "$HTTP_PORT")
      PORT_NOTE="${PORT_NOTE}порт $o занят ($(who_port "$o")) → редактор на $ALT_PORT; "
    fi
  fi
  [ -n "$HTTP_PORT" ] && { [ "$MODE" != hls ] || [ -n "$ALT_PORT" ]; } || die "Не нашлось свободного порта"
  return 0
}

valid_input() {   # всё, что подставляется в конфиги nginx/Python/certbot, — только безопасные символы
  DOMAIN=${DOMAIN,,}
  [ -z "$DOMAIN" ] || [[ $DOMAIN =~ ^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$ ]] || die "Неверный домен: «$DOMAIN». Пример: tv.example.com"
  [ -z "$EMAIL" ] || [[ $EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$ ]] || die "Неверный e-mail: «$EMAIL»"
  [ -z "$ADDR" ] || [[ $ADDR =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "Неверный адрес --address: «$ADDR». Нужен IPv4, например 192.168.1.10"
  [ -z "$ALT_PORT" ] || { [[ $ALT_PORT =~ ^[0-9]{2,5}$ ]] && [ "$ALT_PORT" -le 65535 ]; } || die "Неверный порт --alt-port: «$ALT_PORT»"
  [ -z "$HTTP_PORT" ] || { [[ $HTTP_PORT =~ ^[0-9]{2,5}$ ]] && [ "$HTTP_PORT" -le 65535 ]; } || die "Неверный порт --port: «$HTTP_PORT»"
  [ -z "$USERNAME" ] || [[ $USERNAME =~ ^[A-Za-z0-9._-]{1,32}$ ]] || die "Логин: только латиница, цифры и . _ - (до 32 символов)"
  return 0
}

# ---------- вопросы ----------
wizard() {
  echo
  echo -e "${B}Шаг 1. Как смотреть каналы?${N}"
  echo -e "  ${B}1${N} — через этот сервер ${B}(+ ставится HLS Proxy)${N}"
  echo -e "      ${D}HLS Proxy — отдельная программа стороннего автора,${N}"
  echo -e "      ${D}скачивается с его сайта hls-proxy.com.${N}"
  echo -e "      ${D}+ один вход провайдера на всю семью${N}"
  echo -e "      ${D}+ своя программа передач, резервные потоки${N}"
  echo -e "      ${D}− весь видеотрафик идёт через сервер (лучше от 2 ГБ памяти)${N}"
  echo -e "  ${B}2${N} — напрямую от провайдера ${D}(по умолчанию, проще)${N}"
  echo -e "      ${D}ставится только редактор, сервер почти не нагружен${N}"
  echo -e "      ${D}− в плейлистах прямые ссылки провайдера${N}"
  ask "Ваш выбор" "${MODE_DEF:-2}"
  case "$REPLY" in 1|hls) MODE=hls;; *) MODE=direct;; esac
  if [ "$MODE" = hls ] && [ "$(uname -m)" != x86_64 ]; then
    warn "HLS Proxy ставится только на x86_64, а у сервера $(uname -m). Выбран режим без HLS Proxy."; MODE=direct
  fi
  # лимит каналов не спрашиваем: в редакторе режим «Авто» сам берёт лимит провайдера по источникам
  # (вручную: --streams N при установке, e4tv streams N или в редакторе; 0 — без лимита)

  if [ "${HOME_LAN:-0}" = 1 ]; then DOMAIN=""
  elif port_busy 80 || port_busy 443; then   # 80/443 заняты другой программой — домен не подключить
    echo; echo -e "${B}Шаг 2. Адрес${N}"
    warn "Порты 80/443 заняты ($(who_port 80)$(port_busy 443 && echo ", $(who_port 443)")) — домен здесь не подключить."
    echo -e "${D}Редактор будет работать по http://${IP}:<свободный порт> — порт подберётся сам.${N}"
    DOMAIN=""
  else   # дома домен не нужен
  echo
  echo -e "${B}Шаг 2. Домен${N} ${D}— нужен для HTTPS. Запись A домена должна указывать на ${EXT_IP}.${N}"
  echo -e "${D}Нет домена — нажмите Enter: всё будет работать по http://${IP}${N}"
  ask "Домен" "${DOMAIN_DEF:-}"; DOMAIN=${REPLY,,}; DOMAIN=${DOMAIN#http*://}; DOMAIN=${DOMAIN%%/*}
  if [ -n "$DOMAIN" ]; then
    ask "E-mail для Let's Encrypt (напоминания о сертификате; Enter — без почты)" "${EMAIL_DEF:-}"; EMAIL=$REPLY
  fi

  fi
  echo
  echo -e "${B}Шаг 3. Вход в редактор${N}"
  echo -e "  ${B}1${N} — по логину и паролю из браузера ${D}(по умолчанию)${N}"
  echo -e "  ${B}2${N} — только через SSH-туннель ${D}(редактор вообще не виден из интернета)${N}"
  ask "Ваш выбор" "$( [ "${ACCESS_DEF:-login}" = tunnel ] && echo 2 || echo 1)"
  case "$REPLY" in 2|tunnel) ACCESS=tunnel;; *) ACCESS=login;; esac
  if [ "$ACCESS" = login ]; then
    ask "Логин" "${USER_DEF:-admin}"; USERNAME=$REPLY
    if [ -z "$PASSWORD" ]; then
      local p1 p2
      read -r -s -p "$(echo -e "${B}Пароль${N} ${D}[Enter — $( [ -f "$ETC/auth.json" ] && echo 'оставить прежний' || echo 'придумать автоматически')]${N}: ")" p1 </dev/tty; echo
      if [ -n "$p1" ]; then
        [ ${#p1} -ge 6 ] || die "Пароль должен быть не короче 6 символов"
        read -r -s -p "Ещё раз: " p2 </dev/tty; echo; [ "$p1" = "$p2" ] || die "Пароли не совпали"
        PASSWORD=$p1
      fi
    fi
  fi
}

summary() {
  echo
  echo -e "${B}Будет установлено:${N}"
  echo -e "  📺 Evgen4ik TV ${E4_VER}  — редактор плейлистов"
  [ "$MODE" = hls ] && echo -e "  🎬 HLS Proxy ${HLSP_VER}   — видео через сервер ${D}(сторонняя программа, сайт hls-proxy.com)${N}"
  echo -e "  🌐 nginx              — веб-сервер редактора"
  [ -n "$DOMAIN" ] && echo -e "  🔒 certbot            — сертификат для домена"
  echo
  echo -e "  Режим:     $( [ "$MODE" = hls ] && echo "через сервер (HLS Proxy)" || echo 'напрямую от провайдера')"
  echo -e "  Адрес:     $( [ -n "$DOMAIN" ] && echo "https://$DOMAIN (Let’s Encrypt)" || echo "http://${IP}$(sfx "$HTTP_PORT") (без домена)")"
  echo -e "  Порты:     $( [ -n "$DOMAIN" ] && echo '80, 443' || echo "$HTTP_PORT")$( [ "$MODE" = hls ] && echo ", $ALT_PORT (редактор)")"
  [ -n "${PORT_NOTE:-}" ] && echo -e "  ${Y}Внимание:  ${PORT_NOTE%; }${N}"
  echo -e "  Редактор:  $( [ "$ACCESS" = login ] && echo "логин ${USERNAME}" || echo 'только SSH-туннель')"
  if [ "$MODE" = hls ]; then
    if [ "${STREAMS:-}" = 0 ]; then echo -e "  Потоков:   без лимита (сторонний плейлист без ограничения устройств)"
    elif [ -n "${STREAMS:-}" ]; then echo -e "  Потоков:   не больше ${STREAMS} одновременно (лимит провайдера)"
    elif [ -f "$HLS/local.json" ]; then echo -e "  Потоков:   как сейчас ${D}(сменить — в редакторе или e4tv streams N)${N}"
    else echo -e "  Потоков:   авто — по лимиту провайдера, которого добавите в редакторе ${D}(до этого 3)${N}"; fi
  fi
  echo -e "  Пакеты:    python3, nginx$( [ -n "$DOMAIN" ] && echo ', certbot'), curl, unzip"
  if [ "$YES" != 1 ]; then yesno "Начинаем?" д || die "Отменено"; fi
}

# ---------- шаги установки ----------
install_packages() {
  say "Ставлю пакеты (1–3 минуты)…"
  export DEBIAN_FRONTEND=noninteractive
  local pk="python3 nginx curl ca-certificates unzip"
  [ -n "$DOMAIN" ] && pk="$pk certbot"
  apt-get update -qq >/dev/null 2>&1 || warn "apt-get update завершился с ошибкой — пробую ставить дальше"
  # shellcheck disable=SC2086
  apt-get install -y -qq $pk >/dev/null 2>&1 || die "Не удалось поставить пакеты: $pk. Проверьте интернет на сервере и apt."
  ok "Пакеты установлены"
}

install_files() {
  id e4tv >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$DATA" -M e4tv
  install -d -m 755 "$APP" "$ETC" "$DATA" "$ACME"
  install -d -m 750 -o e4tv -g e4tv "$DATA/editor" "$DATA/editor/uploads"
  install -d -m 700 "$DATA/build" "$DATA/free"
  install -d -m 750 -o root -g www-data "$PL" "$PL/auth"
  for f in app.py build.py lists.py autocheck.py notify.py e4conf.py index.html login.html; do install -m 644 "$SRC/$f" "$APP/$f"; done
  install -d -m 755 "$APP/lib"; install -m 644 "$SRC"/lib/* "$APP/lib/"   # плееры hls.js и mpegts.js (Apache-2.0)
  install -m 755 "$SRC/e4tv" /usr/local/bin/e4tv
  for t in apply.trigger lists.trigger update.trigger check.trigger domain.request; do [ -f "$DATA/editor/$t" ] || install -m 640 -o e4tv -g e4tv /dev/null "$DATA/editor/$t"; done
  if [ ! -f "$DATA/editor/rules.json" ]; then   # новая установка: лимит каналов «Авто», если не задан --streams
    if [[ ${STREAMS:-} =~ ^[0-9]+$ ]] && [ "$STREAMS" -le 50 ]; then echo "{\"stream_limit_mode\": \"manual\", \"stream_limit\": $((10#$STREAMS))}"
    else echo '{"stream_limit_mode": "auto"}'; fi > "$DATA/editor/rules.json"
    chown e4tv:e4tv "$DATA/editor/rules.json"; chmod 640 "$DATA/editor/rules.json"
  fi
  echo "$E4_VER" > "$APP/VERSION"
  ok "Программа скопирована в $APP"
}

get_cert() {   # выпуск сертификата Let's Encrypt (webroot через nginx на 80 порту)
  [ -z "$DOMAIN" ] && return 0
  if [ -f "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" ]; then ok "Сертификат для $DOMAIN уже есть"; return 0; fi
  local dip; dip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
  if [ "$dip" != "$EXT_IP" ]; then
    warn "Домен $DOMAIN указывает на ${dip:-ничего}, а у сервера внешний адрес $EXT_IP."
    warn "Пропишите у регистратора запись A: $DOMAIN → $EXT_IP (обновляется от 5 минут до нескольких часов)."
  fi
  say "Получаю сертификат для $DOMAIN…"
  cat > /etc/nginx/sites-available/evgen4ik-tv <<EOF
server {
$(lsn 80)
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root $ACME; }
    location / { return 404; }
}
EOF
  ln -sf /etc/nginx/sites-available/evgen4ik-tv /etc/nginx/sites-enabled/evgen4ik-tv
  nginx -t >/dev/null 2>&1 && systemctl reload nginx
  local em=(--register-unsafely-without-email); [ -n "$EMAIL" ] && em=(--email "$EMAIL")
  if certbot certonly --webroot -w "$ACME" -d "$DOMAIN" "${em[@]}" --agree-tos -n --keep-until-expiring >/tmp/e4tv-certbot.log 2>&1; then
    ok "Сертификат получен (продлевается сам)"
  else
    warn "Сертификат получить не удалось. Подробности: /tmp/e4tv-certbot.log"
    grep -m3 -i -E 'detail|error' /tmp/e4tv-certbot.log | sed 's/^/    /'
    if yesno "Продолжить без HTTPS, по адресу http://$IP? (домен можно добавить потом: запустите установщик ещё раз)" д; then DOMAIN=
    else die "Остановлено. Проверьте запись A домена и запустите установщик снова."; fi
  fi
}

install_hlsproxy() {
  [ "$MODE" = hls ] || return 0
  id hlsproxy >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin -d "$HLS" -M hlsproxy
  if [ ! -x "$HLS/hls-proxy" ]; then
    say "Скачиваю HLS Proxy ${HLSP_VER} с сайта автора (hls-proxy.com)…"
    local t; t=$(mktemp -d)
    curl -fL --max-time 300 -o "$t/h.zip" "$HLSP_URL" >/dev/null 2>&1 || { rm -rf "$t"; die "Не удалось скачать HLS Proxy: $HLSP_URL
  Скачайте архив вручную, распакуйте в $HLS и запустите установщик снова, или выберите режим без HLS Proxy."; }
    local hs; hs=$(sha256sum "$t/h.zip" | cut -d' ' -f1)
    if [ -n "$HLSP_SHA256" ] && [ "$hs" != "$HLSP_SHA256" ]; then rm -rf "$t"; die "Архив HLS Proxy не совпал с ожидаемым (SHA-256 $hs).
  Возможно, его подменили или автор выложил новую сборку. Установка остановлена."; fi
    [ -n "$HLSP_SHA256" ] && ok "Архив HLS Proxy проверен (SHA-256)" || warn "SHA-256 архива HLS Proxy: $hs (сверять не с чем)"
    unzip -q "$t/h.zip" -d "$t/x" || { rm -rf "$t"; die "Архив HLS Proxy повреждён"; }
    local bin; bin=$(find "$t/x" -maxdepth 3 -type f -name hls-proxy | head -1)
    [ -n "$bin" ] || { rm -rf "$t"; die "В архиве HLS Proxy не найден файл hls-proxy"; }
    install -d "$HLS"; cp -a "$(dirname "$bin")/." "$HLS/"; rm -rf "$t"
    chmod 755 "$HLS/hls-proxy"; touch "$HLS/.installed-by-e4tv"
    ok "HLS Proxy распакован в $HLS"
  else
    ok "HLS Proxy уже установлен в $HLS — оставляю как есть"
  fi
  install -d -m 755 "$HLS/tls"
  if [ -n "$DOMAIN" ]; then
    install -m 600 "/etc/letsencrypt/live/$DOMAIN/privkey.pem" "$HLS/tls/privkey.pem"
    install -m 644 "/etc/letsencrypt/live/$DOMAIN/fullchain.pem" "$HLS/tls/fullchain.pem"
  fi
  [ -f "$HLS/combined.m3u" ] || echo "#EXTM3U" > "$HLS/combined.m3u"
  # local.json: создаём или обновляем только адрес/порт/сертификат и плейлист Combined
  [[ ${STREAMS:-} =~ ^[0-9]+$ ]] && [ "$STREAMS" -ge 0 ] && [ "$STREAMS" -le 50 ] || { [ -n "${STREAMS:-}" ] && warn "Число потоков «$STREAMS» не подходит — ставлю 3"; [ -n "${STREAMS:-}" ] && STREAMS=3; }
  [ -f "$HLS/local.json" ] || STREAMS=${STREAMS:-3}
  E4_STREAMS=${STREAMS:-} HLS_ADMIN_PW=$(rnd 16) HLS_CLIENT_PW=$(rnd 20) python3 - "$HLS/local.json" "${DOMAIN:-$IP}" "$( [ -n "$DOMAIN" ] && echo https || echo http)" "$EPG_DEFAULT" "${HTTP_PORT:-80}" <<'P'
import json, os, sys
path, addr, proto, epg, hport = sys.argv[1:6]
new = not os.path.exists(path)
c = {} if new else json.load(open(path, encoding="utf-8"))
S = c.setdefault("SERVER", {})
S.update({"protocol": proto, "address": addr, "port": 443 if proto == "https" else int(hport)})
if proto == "https": S.update({"key": "./tls/privkey.pem", "cert": "./tls/fullchain.pem"})
else: S.pop("key", None); S.pop("cert", None)
S.setdefault("password", os.environ["HLS_ADMIN_PW"])
pls = c.setdefault("playlists", [])
if not any(p.get("uri") == "./combined.m3u" for p in pls):
    pls.insert(0, {"name": "Combined", "type": "file", "uri": "./combined.m3u", "plugin": "", "localCopy": "",
                   "isUseLocalAceStreamMediaProxy": False, "aceStreamMediaProxyURL": "", "useLogos": True,
                   "flussonicType": "index", "isPassThrough": False, "isManualUpdate": False, "isEnabled": True,
                   "nameReplacer": [], "headers": {"value": "", "isPlaylist": True, "isM3U8": True, "isChunk": True},
                   "remotePath": "", "ffmpegCommand": ""})
c.setdefault("epg", {"tvGuideUrl": [epg], "historyDays": 3, "isKeepDbMatchedOnlyChannels": True})
if not c.get("clients"):
    c["clients"] = {"tv": {"name": "tv", "password": os.environ["HLS_CLIENT_PW"], "isAdultAllowed": True,
                           "isHttp": False, "maxStreamCount": 3, "n": 0}}
c.setdefault("maxSimultaneousStreamCountPerClientDefault", 3)
n = os.environ.get("E4_STREAMS", "")
if n.isdigit():   # лимит провайдера: на всех клиентов и по умолчанию; 0 = без лимита (в HLS Proxy пишем 999)
    v = int(n) or 999
    c["maxSimultaneousStreamCountPerClientDefault"] = v
    for k, cl in c.get("clients", {}).items():
        if isinstance(cl, dict) and not k.startswith("e4c_"): cl["maxStreamCount"] = v   # e4c_* — клиенты, у них свой лимит
c.setdefault("isHtmlPlayerEnabled", False)
c["isBackupStreams"] = True   # каналы с одинаковым названием = резервные потоки
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False, indent=1)
os.chmod(tmp, 0o600); os.replace(tmp, path)
print("создан" if new else "обновлён")
P
  chown -R hlsproxy:hlsproxy "$HLS"; chmod 640 "$HLS/combined.m3u"
  if [ ! -f /etc/systemd/system/hls-proxy.service ] || [ -f "$HLS/.installed-by-e4tv" ]; then
    cat > /etc/systemd/system/hls-proxy.service <<EOF
[Unit]
Description=HLS Proxy for IPTV
Wants=network-online.target
After=network-online.target

[Service]
User=hlsproxy
Group=hlsproxy
WorkingDirectory=$HLS
ExecStart=$HLS/hls-proxy
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
Restart=always
RestartSec=5
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF
  fi
  ok "HLS Proxy настроен"
}

write_config() {
  EXT_IP=${EXT_IP:-$IP}; LAN=${LAN:-}
  local scheme=http host=${DOMAIN:-$IP} eport editor pb hlsurl= epg
  [ -n "$DOMAIN" ] && scheme=https
  local hp; hp=$( [ "$scheme" = https ] && echo 443 || echo "${HTTP_PORT:-80}" )
  if [ "$MODE" = hls ]; then
    eport=$( [ "$scheme" = https ] && echo 8443 || echo 8080 )
    [[ ${ALT_PORT:-} =~ ^[0-9]{2,5}$ ]] && eport=$ALT_PORT
    editor="$scheme://$host:$eport/"; hlsurl="$scheme://$host$(sfx "$hp")/"; epg="${hlsurl}epg.xml.gz"
  else
    eport=$hp
    editor="$scheme://$host$(sfx "$eport")/"; epg=$EPG_DEFAULT
  fi
  pb="${editor}pl/"
  [ "$ACCESS" = tunnel ] && editor="http://localhost:8095/ (через SSH-туннель)"
  local tok; tok=$(conf_get main_token); [ -n "$tok" ] || tok=$(rnd 24)
  python3 - "$ETC/config.json" <<P
import json
c = {"version": "$E4_VER", "mode": "$MODE", "domain": "$DOMAIN", "host": "$host", "scheme": "$scheme",
     "address": "$IP", "hosts": sorted({"$host", "$IP", "$EXT_IP", "${LAN:-}"} - {""}), "access": "$ACCESS", "editor_url": "$editor", "editor_port": $eport,
     "public_base": "$pb", "main_token": "$tok", "hls_port": $hp, "http_port": $hp, "epg_url": "$EPG_DEFAULT", "player_epg": "$epg", "hls_url": "$hlsurl",
     "data": "$DATA", "pl_dir": "$PL", "hls_dir": "$HLS", "listen": "127.0.0.1", "port": 8095,
     "auth_file": "$ETC/auth.json", "editor_group": "e4tv"}
json.dump(c, open("$ETC/config.json", "w"), ensure_ascii=False, indent=1)
P
  chmod 644 "$ETC/config.json"
  EPORT=$eport SCHEME=$scheme
  if [ "$ACCESS" = login ]; then
    if [ -n "$PASSWORD" ] || [ ! -f "$ETC/auth.json" ]; then
      [ -n "$PASSWORD" ] || { PASSWORD=$(rnd 14); GENPW=1; }
      E4PW=$PASSWORD /usr/local/bin/e4tv _setauth "${USERNAME:-admin}" || die "Не удалось сохранить пароль"
    elif [ -n "$USERNAME" ] && [ "$USERNAME" != "$(python3 -c 'import json;print(json.load(open("'"$ETC"'/auth.json"))["user"])' 2>/dev/null)" ]; then
      warn "Логин меняется вместе с паролем: e4tv passwd"
    fi
  fi
  ok "Настройки записаны в $ETC/config.json"
}

lsn() {   # строка listen; IPv6 — только если он есть на сервере (иначе nginx не запустится)
  local o=${2:+ $2}
  if [ -f /proc/net/if_inet6 ]; then echo "    listen $1$o; listen [::]:$1$o;"; else echo "    listen $1$o;"; fi
}

pl_locations() {
  cat <<EOF
    # плейлисты: /pl/<ссылка>.m3u8; если у плейлиста есть пароль — спросит логин tv и пароль
    location ~ "^/pl/([A-Za-z0-9_-]{3,64})\\.m3u8?\$" {
        set \$pl \$1;
        if (\$e4tv_acl = 0) { return 403; }   # у клиента заданы разрешённые IP, а запрос не с них
        if (-f $PL/auth/\$pl.htpasswd) { rewrite ^ /_auth/\$pl last; }
        alias $PL/\$pl.m3u8;
        types { } default_type "application/x-mpegURL; charset=utf-8";
        add_header Cache-Control "no-store";
        access_log /var/log/nginx/e4tv-pl.log e4tv_pl;   # кто открывал ссылку (раздел «Клиенты» → активность)
    }
    # Xtream Codes: плеер входит по серверу, логину и паролю клиента (без входа в редактор)
    location ~ "^/(player_api|get|xmltv|panel_api|streaming/timeshift)\\.php\$" {
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        access_log off;   # в адресе пароль клиента — в журнал не пишем
    }
    location ^~ /live/ {
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        access_log off;
    }
    location ^~ /my/ {   # личная страница клиента (по его ссылке, без входа в редактор)
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        access_log off;
    }
    location ^~ /timeshift/ {   # архив через Xtream Codes
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        access_log off;
    }
    location ~ "^/_auth/([A-Za-z0-9_-]{3,64})\$" {
        internal;
        set \$pa \$1;
        auth_basic "IPTV";
        auth_basic_user_file $PL/auth/\$pa.htpasswd;
        alias $PL/\$pa.m3u8;
        types { } default_type "application/x-mpegURL; charset=utf-8";
        add_header Cache-Control "no-store";
        access_log /var/log/nginx/e4tv-pl.log e4tv_pl;
    }
EOF
}

editor_location() {
  if [ "$ACCESS" = tunnel ]; then echo '    location / { return 404; }'; return; fi
  cat <<'EOF'
    location / {
        proxy_pass http://127.0.0.1:8095;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 180s;
        client_max_body_size 40m;
    }
EOF
}

write_nginx() {
  local f=/etc/nginx/sites-available/evgen4ik-tv tls=
  [ -f /etc/nginx/e4tv-acl.conf ] || printf '# Evgen4ik TV: разрешённые IP клиентов (пишет lists.py)\nmap $uri $e4tv_acl {\n    default 1;\n}\n' > /etc/nginx/e4tv-acl.conf
  [ -n "$DOMAIN" ] && tls="    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;"
  # стандартный сайт nginx занимает 80 порт — убираем только его ссылку (сам файл остаётся)
  [ -L /etc/nginx/sites-enabled/default ] && rm -f /etc/nginx/sites-enabled/default
  {
    echo "# Evgen4ik TV — создано установщиком, при повторной установке перезаписывается"
    echo 'log_format e4tv_pl "$time_iso8601 $remote_addr $status $request_uri $http_user_agent";'   # журнал открытий ссылок (плеер — в конце)
    echo "include /etc/nginx/e4tv-acl.conf;"   # разрешённые IP клиентов (файл обновляет lists.py)
    if [ -n "$DOMAIN" ]; then
      echo "server {"
      lsn 80
      echo "    server_name $DOMAIN;"
      echo "    access_log off;"
      echo "    location /.well-known/acme-challenge/ { root $ACME; }"
      pl_locations   # старые телевизоры без HTTPS тоже получат плейлист
      echo '    location / { return 301 https://$host$request_uri; }'
      echo "}"
      echo "server {"
      lsn "$EPORT" ssl
      echo "    server_name $DOMAIN _;"
      echo "    access_log off;"
      echo "$tls"
    else
      echo "server {"
      lsn "$EPORT" default_server
      echo "    server_name _;"
      echo "    access_log off;"
    fi
    pl_locations
    editor_location
    echo "}"
  } > "$f"
  ln -sf "$f" /etc/nginx/sites-enabled/evgen4ik-tv
  nginx -t >/tmp/e4tv-nginx.log 2>&1 || { cat /tmp/e4tv-nginx.log; die "Ошибка в настройке nginx (см. выше)"; }
  systemctl enable -q nginx; systemctl reload nginx 2>/dev/null || systemctl restart nginx
  if [ -n "$DOMAIN" ]; then
    install -d /etc/letsencrypt/renewal-hooks/deploy
    cat > /etc/letsencrypt/renewal-hooks/deploy/e4tv.sh <<EOF
#!/bin/sh
# Evgen4ik TV: после продления сертификата
case "\$RENEWED_DOMAINS" in *$DOMAIN*) ;; *) exit 0 ;; esac
systemctl reload nginx
if [ -f $HLS/local.json ] && grep -q '"$DOMAIN"' $HLS/local.json; then
  install -o hlsproxy -g hlsproxy -m 600 /etc/letsencrypt/live/$DOMAIN/privkey.pem $HLS/tls/privkey.pem
  install -o hlsproxy -g hlsproxy -m 644 /etc/letsencrypt/live/$DOMAIN/fullchain.pem $HLS/tls/fullchain.pem
  systemctl restart hls-proxy
fi
EOF
    chmod 755 /etc/letsencrypt/renewal-hooks/deploy/e4tv.sh
  fi
  ok "nginx настроен"
}

write_units() {
  local U=/etc/systemd/system post=
  if [ "$MODE" = hls ]; then
    post="ExecStartPost=/bin/sh -c \"chown hlsproxy:hlsproxy $HLS/combined.m3u; chmod 640 $HLS/combined.m3u\"
ExecStartPost=/bin/sh -c 'n=\$\$(cat $HLS/combined.m3u $HLS/local.json 2>/dev/null | sha256sum); f=$DATA/build/hls.sha256; [ \"\$\$n\" = \"\$\$(cat \$\$f 2>/dev/null)\" ] || { systemctl restart hls-proxy && echo \"\$\$n\" > \$\$f; }'
ExecStartPost=-/usr/bin/python3 $APP/lists.py --wait 180"
  else
    post="ExecStartPost=-/usr/bin/python3 $APP/lists.py"
  fi
  cat > $U/e4tv-editor.service <<EOF
[Unit]
Description=Evgen4ik TV — редактор плейлистов (127.0.0.1:8095)
After=network.target

[Service]
User=e4tv
Group=e4tv
ExecStart=/usr/bin/python3 $APP/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA/editor

[Install]
WantedBy=multi-user.target
EOF
  cat > $U/e4tv-build.service <<EOF
[Unit]
Description=Evgen4ik TV — сборка плейлиста из источников
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=900
ExecStart=/usr/bin/python3 $APP/build.py
$post
EOF
  cat > $U/e4tv-build.path <<EOF
[Unit]
Description=Evgen4ik TV — пересборка по кнопке «Применить»

[Path]
PathModified=$DATA/editor/apply.trigger
Unit=e4tv-build.service

[Install]
WantedBy=multi-user.target
EOF
  cat > $U/e4tv-build.timer <<EOF
[Unit]
Description=Evgen4ik TV — ежедневная пересборка (свежие ссылки провайдеров)

[Timer]
OnCalendar=*-*-* 05:15:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF
  cat > $U/e4tv-lists.service <<EOF
[Unit]
Description=Evgen4ik TV — плейлисты для плееров
After=hls-proxy.service

[Service]
Type=oneshot
TimeoutStartSec=600
ExecStart=/usr/bin/python3 $APP/lists.py --wait 60
EOF
  cat > $U/e4tv-lists.path <<EOF
[Unit]
Description=Evgen4ik TV — обновить плейлисты после сохранения

[Path]
PathModified=$DATA/editor/lists.trigger
Unit=e4tv-lists.service

[Install]
WantedBy=multi-user.target
EOF
  cat > $U/e4tv-lists.timer <<EOF
[Unit]
Description=Evgen4ik TV — ежечасное обновление плейлистов (срок действия ссылок)

[Timer]
OnCalendar=hourly
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF
  cat > $U/e4tv-update.service <<EOF
[Unit]
Description=Evgen4ik TV — обновление по кнопке в редакторе
After=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=1200
# get.sh сам сверяет SHA-256 установщика; журнал — только для root (в нём пароль панели HLS Proxy)
ExecStart=/bin/sh -c 'umask 077; exec /usr/local/bin/e4tv update > /var/log/e4tv-update.log 2>&1'
EOF
  cat > $U/e4tv-domain.service <<EOF
[Unit]
Description=Evgen4ik TV — подключить домен (HTTPS) по кнопке в редакторе
After=network-online.target

[Service]
Type=oneshot
TimeoutStartSec=1200
ExecStart=/bin/sh -c 'umask 077; exec /usr/local/bin/e4tv domain > /var/log/e4tv-domain.log 2>&1'
EOF
  cat > $U/e4tv-domain.path <<EOF
[Unit]
Description=Evgen4ik TV — кнопка «Подключить домен» в редакторе

[Path]
PathModified=$DATA/editor/domain.request
Unit=e4tv-domain.service

[Install]
WantedBy=multi-user.target
EOF
  cat > $U/e4tv-update.path <<EOF
[Unit]
Description=Evgen4ik TV — кнопка «Обновить» в редакторе

[Path]
PathModified=$DATA/editor/update.trigger
Unit=e4tv-update.service

[Install]
WantedBy=multi-user.target
EOF
  # по таймеру — только если автопроверка включена в редакторе; по кнопке — всегда (файл-триггер не перезаписываем, иначе петля)
  local cu n a d
  for cu in "check|--scheduled|автопроверка потоков по расписанию" "check-now|--now|проверка потоков по кнопке"; do
    IFS='|' read -r n a d <<<"$cu"
    cat > $U/e4tv-$n.service <<EOF
[Unit]
Description=Evgen4ik TV — $d
Wants=network-online.target
After=network-online.target e4tv-build.service

[Service]
Type=oneshot
# ~2 с на канал у провайдера: 1000 каналов в 2 потока — около 17 минут
TimeoutStartSec=3600
Nice=10
ExecStart=/usr/bin/python3 $APP/autocheck.py $a
EOF
  done
  cat > $U/e4tv-check.path <<EOF
[Unit]
Description=Evgen4ik TV — кнопка «Проверить сейчас» в редакторе

[Path]
PathModified=$DATA/editor/check.trigger
Unit=e4tv-check-now.service

[Install]
WantedBy=multi-user.target
EOF
  cat > $U/e4tv-check.timer <<EOF
[Unit]
Description=Evgen4ik TV — автопроверка потоков раз в сутки (после утренней сборки)

[Timer]
OnCalendar=*-*-* 06:30:00
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
EOF
  cat > $U/e4tv-notify.service <<EOF
[Unit]
Description=Evgen4ik TV — уведомления в Telegram (срок клиентов, сборка, источники, новые IP)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $APP/notify.py
EOF
  cat > $U/e4tv-notify.timer <<EOF
[Unit]
Description=Evgen4ik TV — уведомления в Telegram раз в 5 минут

[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
AccuracySec=30s

[Install]
WantedBy=timers.target
EOF
  cat > $U/e4tv-activity.service <<EOF
[Unit]
Description=Evgen4ik TV — кто открывал плейлисты (журнал nginx)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $APP/lists.py --activity
EOF
  cat > $U/e4tv-activity.timer <<EOF
[Unit]
Description=Evgen4ik TV — активность клиентов раз в минуту

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  if [ "$MODE" = hls ]; then systemctl enable -q --now hls-proxy; systemctl restart hls-proxy
  elif [ -f "$HLS/.installed-by-e4tv" ]; then systemctl disable -q --now hls-proxy 2>/dev/null; warn "HLS Proxy остановлен (выбран режим без него). Файлы остались в $HLS."; fi
  systemctl enable -q --now e4tv-editor.service e4tv-build.path e4tv-build.timer e4tv-lists.path e4tv-lists.timer e4tv-activity.timer e4tv-update.path e4tv-check.path e4tv-check.timer e4tv-notify.timer e4tv-domain.path
  systemctl restart e4tv-editor.service
  ok "Службы запущены"
}

firewall() {
  command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active" || return 0
  local hp=${HTTP_PORT:-80}; [ -n "$DOMAIN" ] && hp=80
  ufw allow "$hp/tcp" >/dev/null; [ -n "$DOMAIN" ] && ufw allow 443/tcp >/dev/null
  [ "$MODE" = hls ] && ufw allow "$EPORT/tcp" >/dev/null
  ok "Открыты порты в ufw: $hp$( [ -n "$DOMAIN" ] && echo ', 443')$( [ "$MODE" = hls ] && echo ", $EPORT")"
}

first_build_and_check() {
  say "Первая сборка и проверка…"
  systemctl start e4tv-build.service || warn "Первая сборка завершилась с ошибкой: e4tv logs"
  local code i
  for i in 1 2 3 4 5; do code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: 127.0.0.1" http://127.0.0.1:8095/login); [ "$code" = 200 ] || [ "$code" = 302 ] && break; sleep 1; done
  [ "$code" = 200 ] || [ "$code" = 302 ] && ok "Редактор отвечает" || warn "Редактор не отвечает (код $code): journalctl -u e4tv-editor"
  if [ "$MODE" = hls ]; then
    sleep 3; systemctl is-active -q hls-proxy && ok "HLS Proxy работает" || warn "HLS Proxy не запустился: journalctl -u hls-proxy -n 50"
  fi
}

final_report() {
  local user; user=$(python3 -c 'import json;print(json.load(open("'"$ETC"'/auth.json"))["user"])' 2>/dev/null || echo admin)
  local main; main="$(conf_get public_base)$(conf_get main_token).m3u8"
  # Строка «Пароль:» в файле доступа. Пароль редактора хранится только хешем (auth.json), поэтому:
  #  - пароль задан сейчас (--password, ввод в мастере или сгенерирован) — пишем его;
  #  - пароль не менялся (обновление) — переносим строку из прошлого файла, в том числе в режиме туннеля.
  # До 2.9.1 строка терялась при каждом обновлении; до 3.5.1 — при ручном пароле и при переходе на туннель.
  local oldpw=""; [ -f "$CREDS" ] && oldpw=$(grep -m1 '^Пароль: ' "$CREDS" || true)
  local pwline=""
  if [ -n "$PASSWORD" ]; then pwline="Пароль:            $PASSWORD"
  elif [ -n "$oldpw" ]; then pwline=$oldpw
  elif [ "$ACCESS" = login ]; then pwline="Пароль:            (прежний; забыли — смените: e4tv passwd)"; fi
  {
    echo "Evgen4ik TV — доступ ($(date '+%d.%m.%Y %H:%M'))"
    echo "Редактор:          $(conf_get editor_url)"
    [ "$ACCESS" = login ] && echo "Логин:             $user"
    [ "$ACCESS" = tunnel ] && [ -n "$pwline" ] && echo "Логин:             $user (вход по паролю выключен; включить: установщик → «изменить настройки»)"
    [ -n "$pwline" ] && echo "$pwline"
    [ "$ACCESS" = tunnel ] && echo "Туннель с ПК:      ssh -N -L 8095:127.0.0.1:8095 root@$IP"
    echo "Основной плейлист: $main"
    echo "Программа (EPG):   $(conf_get player_epg)"
    if [ "$MODE" = hls ]; then
      echo "Панель HLS Proxy:  $(conf_get hls_url)"
      echo "Потоков максимум:  $(python3 -c 'import json;v=json.load(open("'"$HLS"'/local.json")).get("maxSimultaneousStreamCountPerClientDefault","?");print("без лимита" if isinstance(v,int) and v>=999 else v)') (сменить: e4tv streams N, 0 — без лимита)"
      echo "Пароль панели:     $(python3 -c 'import json;print(json.load(open("'"$HLS"'/local.json"))["SERVER"].get("password",""))')"
    fi
  } > "$CREDS.tmp" && chmod 600 "$CREDS.tmp" && mv -f "$CREDS.tmp" "$CREDS"   # старый файл заменяется только целиком
  echo
  echo -e "${G}${B}══════════════ Готово! ══════════════${N}"
  # пароли на экран не выводим (скриншоты, запись терминала) — они только в файле
  sed '1d' "$CREDS" | sed -E "s|^(Пароль[^:]*:[[:space:]]*).*|\1(скрыт — посмотреть: cat $CREDS)|" | sed 's/^/  /'
  echo
  echo -e "${B}Дальше:${N}"
  echo -e "  1. Откройте редактор$( [ "$ACCESS" = tunnel ] && echo ' (сначала туннель, см. выше)') → раздел «Источники»."
  echo -e "  2. Выберите провайдера, вставьте ссылку на плейлист из его кабинета → «Сохранить и применить»."
  echo -e "  3. Через минуту каналы появятся. Ссылку «Основной плейлист» вставьте в плеер (Televizo, OTT Navigator, TiviMate…)."
  echo -e "  4. Свои подборки (детям, на кухню…) — «Объединённые плейлисты» → «+ Новый плейлист»."
  echo
  [ "${HOME_LAN:-0}" = 1 ] && echo -e "${Y}Сервер в домашней сети: плейлисты работают дома и пока этот компьютер включён.${N}"
  echo -e "${D}Эти данные сохранены в $CREDS (виден только root).${N}"
  if [ -z "$DOMAIN" ] && [ "$ACCESS" != tunnel ] && ! [[ ${IP:-} =~ ^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.|127\.) ]]; then
    echo; warn "Редактор открыт по http на внешнем адресе — логин и пароль идут по сети открытым текстом."
    echo "    Лучше: подключите домен (запустите установщик снова и укажите домен — сертификат получится сам)"
    echo "    или включите вход только через SSH-туннель (установщик → «изменить настройки»)."
  fi
  echo -e "${D}Команды: e4tv info · e4tv status · e4tv passwd · e4tv token · e4tv update · e4tv logs${N}"
  [ "$MODE" = hls ] && echo -e "${D}HLS Proxy — программа стороннего автора (hls-proxy.com), скачана с его сайта; условия использования — на сайте автора.${N}"
  return 0
}

main() {
  echo -e "${B}Evgen4ik TV ${E4_VER}${N} — установка редактора IPTV-плейлистов"
  precheck
  locate_src
  command -v curl >/dev/null || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq curl >/dev/null 2>&1; }
  command -v python3 >/dev/null || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq python3 >/dev/null 2>&1; }
  EXT_IP=$(public_ip); IP=$EXT_IP; LAN=$(lan_ip); HOME_LAN=0
  [ -n "$IP" ] || die "Не удалось узнать адрес сервера"
  [ -f "$ETC/config.json" ] && [ -n "$(conf_get address)" ] && IP=$(conf_get address)
  if [ -f "$ETC/config.json" ]; then
    MODE_DEF=$( [ "$(conf_get mode)" = hls ] && echo 1 || echo 2 ); DOMAIN_DEF=$(conf_get domain); ACCESS_DEF=$(conf_get access)
    USER_DEF=$(python3 -c 'import json;print(json.load(open("'"$ETC"'/auth.json"))["user"])' 2>/dev/null || echo admin)
    if [ -z "$ACTION" ] && [ -z "$MODE" ]; then
      echo; echo -e "Найдена установка $(cat "$APP/VERSION" 2>/dev/null || echo '?'): $( [ "$MODE_DEF" = 1 ] && echo 'с HLS Proxy' || echo 'без HLS Proxy'), ${DOMAIN_DEF:-$IP}"
      echo -e "  ${B}1${N} — обновить программу, настройки и пароли оставить ${D}(по умолчанию)${N}"
      echo -e "  ${B}2${N} — изменить настройки (режим, домен, вход)"
      ask "Ваш выбор" 1; [ "$REPLY" = 2 ] && ACTION=reconfigure || ACTION=update
    fi
    if [ "$ACTION" = update ]; then
      MODE=$(conf_get mode); DOMAIN=${NEWDOMAIN:-$DOMAIN_DEF}; ACCESS=$ACCESS_DEF; YES=1
      if [ -n "$NEWDOMAIN" ]; then HTTP_PORT=''; ALT_PORT=''; say "Подключаю домен $NEWDOMAIN (HTTPS)"   # порты — как для домена: 443 и 8443
      else [ -z "$ALT_PORT" ] && [ "$MODE" = hls ] && ALT_PORT=$(conf_get editor_port); fi
      if [ -z "$HTTP_PORT" ] && [ -z "$DOMAIN" ]; then   # 2.8 и раньше: http_port нет — берём из hls_port / editor_port
        HTTP_PORT=$(conf_get http_port); [ -n "$HTTP_PORT" ] || HTTP_PORT=$( [ "$MODE" = hls ] && conf_get hls_port || conf_get editor_port )
      fi
    fi
  fi
  if [ -n "$ADDR" ]; then IP=$ADDR; HOME_LAN=1
  elif [ "$ACTION" != update ] && [ "$YES" != 1 ]; then IP=$EXT_IP; choose_address; fi
  [ -z "$MODE" ] && [ "$YES" != 1 ] && wizard
  MODE=${MODE:-direct}; ACCESS=${ACCESS:-login}; [ "$MODE" = hls ] || MODE=direct
  case "$MODE" in direct|hls) ;; *) die "--mode: direct или hls";; esac
  valid_input
  resolve_ports
  if [ "$ACTION" != update ]; then
    summary
  elif [ -n "$PORT_NOTE" ]; then
    warn "Порты изменены: ${PORT_NOTE%; }"
  fi
  install_packages
  install_files
  get_cert
  install_hlsproxy
  write_config
  write_nginx
  write_units
  firewall
  first_build_and_check
  final_report
}

# E4TV_SOURCE_ONLY=1 — только загрузить функции (для проверок)
if [ "${E4TV_SOURCE_ONLY:-}" = 1 ]; then return 0 2>/dev/null || exit 0; fi
main "$@"
exit $?
