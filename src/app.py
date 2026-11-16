#!/usr/bin/env python3
"""Evgen4ik TV — редактор плейлистов.

Слушает 127.0.0.1:8095; снаружи доступен через nginx (вход по логину и паролю)
или только через SSH-туннель (access = tunnel в /etc/evgen4ik-tv/config.json).
Хранит правила в <data>/editor/rules.json. Их применяет build.py при каждой сборке
(утренний таймер или кнопка «Применить» -> файл apply.trigger -> e4tv-build.path).
Только стандартная библиотека Python.
"""
import json, os, re, subprocess, time, glob, secrets, ssl, urllib.request, urllib.parse, urllib.error, hmac, hashlib, threading, sys, socket, ipaddress
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4

HOST, PORT = E4["listen"], int(E4["port"])
DATA = E4["editor_dir"]
RULES = f"{DATA}/rules.json"
STATE = f"{DATA}/state.json"
TRIGGER = f"{DATA}/apply.trigger"
LTRIGGER = f"{DATA}/lists.trigger"
LSTATUS = f"{DATA}/lists-status.json"
EPG = f"{DATA}/epg.json"
PUBLIC_BASE = E4["public_base"]
URL_RX = re.compile(r"^(https?|rtmp|rtsp|udp|rtp)://[^\s\"]{1,2000}$", re.I)
UPLOADS = E4["uploads"]
FILE_RX = re.compile(r"^file://" + re.escape(UPLOADS) + r"/([a-z0-9]{4,16})\.m3u$")
TOK_RX = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
BACKUPS = f"{DATA}/backups"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 4 * 1024 * 1024
ALLOWED_HOSTS = {"localhost", "127.0.0.1", *[str(h).lower() for h in E4["hosts"]]}
LOGIN_ON = E4["access"] == "login"
COOKIE = "e4tv"
SESSION_DAYS = 30
FAILS, GFAIL, LOCK = {}, [], threading.Lock()   # неудачные входы: по адресу и всего


# ---------- вход по логину и паролю ----------
def auth_conf():
    a = read_json(E4["auth_file"], None)
    return a if isinstance(a, dict) and a.get("user") and a.get("pbkdf2") and a.get("secret") else None


def pw_ok(a, user, pw):
    try:
        it, salt, h = a["pbkdf2"].split("$")
        d = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(it)).hex()
        return hmac.compare_digest(user.encode(), a["user"].encode()) & hmac.compare_digest(d.encode(), h.encode())
    except Exception:
        return False


def sign(a, exp):
    return hmac.new(bytes.fromhex(a["secret"]), f"{a['user']}|{exp}|{a['pbkdf2']}".encode(), "sha256").hexdigest()


def locked_for(ip):
    now = time.time()
    with LOCK:
        f = FAILS.get(ip)
        if f and f[2] > now: return int(f[2] - now) + 1
        GFAIL[:] = [t for t in GFAIL if now - t < 900]
        if len(GFAIL) >= 30: return int(900 - (now - GFAIL[0])) + 1   # подбор с разных адресов
    return 0


def add_fail(ip):
    now = time.time()
    with LOCK:
        f = FAILS.setdefault(ip, [0, now, 0])
        if now - f[1] > 900: f[:] = [0, now, 0]
        f[0] += 1; GFAIL.append(now)
        if f[0] >= 5: f[:] = [0, now, now + 900]   # 5 ошибок -> пауза 15 минут
        if len(FAILS) > 5000: FAILS.clear()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def s(v, n=100):
    return re.sub(r'[\x00-\x1f\x7f"]', " ", v).strip()[:n] if isinstance(v, str) else None


def urls(v):
    if not isinstance(v, list): return []
    return [u.strip() for u in v[:10] if isinstance(u, str) and URL_RX.match(u.strip())]


def keys(v, n=20000):
    return [k.lower()[:200] for k in (v or [])[:n] if isinstance(k, str) and k.strip()]


def validate(r):
    if not isinstance(r, dict):
        raise ValueError("ожидается объект")
    out = {"version": 1, "hidden_patterns": [], "groups": {}, "channels": {}, "group_order": [], "custom_groups": [],
           "custom_channels": [], "lists": [], "sources": [], "disabled_sources": [], "primary_source": None,
           "lists_only_sources": [], "channel_order": {}, "auto_rules": [], "free_sources": []}
    for p in (r.get("hidden_patterns") or [])[:300]:
        if s(p): out["hidden_patterns"].append(s(p))
    for k, v in list((r.get("groups") or {}).items())[:3000]:
        if not s(k) or not isinstance(v, dict): continue
        e = {}
        if s(v.get("rename")): e["rename"] = s(v["rename"])
        if isinstance(v.get("hidden"), bool): e["hidden"] = v["hidden"]
        if e: out["groups"][s(k)] = e
    for k, v in list((r.get("channels") or {}).items())[:30000]:
        if not isinstance(k, str) or not k.strip() or not isinstance(v, dict): continue
        e = {}
        if s(v.get("rename")): e["rename"] = s(v["rename"])
        if s(v.get("group")): e["group"] = s(v["group"])
        if isinstance(v.get("hidden"), bool): e["hidden"] = v["hidden"]
        if urls(v.get("urls")): e["urls"] = urls(v["urls"])
        if s(v.get("tvg_id")): e["tvg_id"] = s(v["tvg_id"])
        if isinstance(v.get("logo"), str) and URL_RX.match(v["logo"].strip()): e["logo"] = v["logo"].strip()
        mg = [m for m in keys(v.get("merge"), 30) if m != k.lower()]
        if mg: e["merge"] = mg
        if e: out["channels"][k.lower()[:200]] = e
    out["group_order"] = [s(g) for g in (r.get("group_order") or [])[:3000] if s(g)]
    out["custom_groups"] = [s(g) for g in (r.get("custom_groups") or [])[:500] if s(g)]
    seen = set()
    for c in (r.get("custom_channels") or [])[:500]:
        if not isinstance(c, dict) or not s(c.get("name")) or not s(c.get("group")) or not urls(c.get("urls")): continue
        if s(c["name"]).lower() in seen: continue
        seen.add(s(c["name"]).lower())
        lg = c.get("logo").strip() if isinstance(c.get("logo"), str) else ""
        out["custom_channels"].append({"name": s(c["name"]), "group": s(c["group"]), "urls": urls(c["urls"]),
                                       "logo": lg if URL_RX.match(lg) else "", "tvg_id": s(c.get("tvg_id")) or ""})
    sids = set()
    for x in (r.get("sources") or [])[:10]:
        if not isinstance(x, dict) or not s(x.get("name"), 40) or not isinstance(x.get("url"), str) or not (URL_RX.match(x["url"].strip()) or FILE_RX.match(x["url"].strip())): continue
        sid = str(x.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", sid) or sid in sids: sid = secrets.token_hex(4)
        sids.add(sid)
        src = {"id": sid, "name": s(x["name"], 40), "url": x["url"].strip(), "enabled": x.get("enabled") is not False}
        if re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("prov") or "")): src["prov"] = x["prov"]   # шаблон провайдера (для лимита)
        if x.get("nolim") is True: src["nolim"] = True   # сторонний плейлист без лимита устройств (режим лимита «Авто»)
        out["sources"].append(src)
    out["disabled_sources"] = [s(x, 40) for x in (r.get("disabled_sources") or [])[:20] if s(x, 40)]
    out["primary_source"] = s(r.get("primary_source"), 40) or None
    fids = set()
    for x in (r.get("free_sources") or [])[:20]:   # отдельные плейлисты: только для своих плейлистов, ссылки напрямую
        if not isinstance(x, dict) or not s(x.get("name"), 40) or not isinstance(x.get("url"), str) or not (re.match(r"^(https?://[^\s\"]{1,2000}|e4://[a-z0-9-]{1,32})$", x["url"].strip()) or FILE_RX.match(x["url"].strip())): continue
        fid = str(x.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", fid) or fid in fids: fid = secrets.token_hex(4)
        fids.add(fid)
        out["free_sources"].append({"id": fid, "name": s(x["name"], 40), "url": x["url"].strip(), "ic": s(x.get("ic"), 12) or ""})
    out["free_edits"] = {}   # свои ссылка/логотип у каналов отдельных плейлистов: «f:<id>:<название>»
    for k, v in list((r.get("free_edits") or {}).items())[:5000]:
        m = re.fullmatch(r"f:([a-z0-9]{1,16}):(.{1,200})", k) if isinstance(k, str) else None
        if not m or m.group(1) not in fids or not isinstance(v, dict): continue
        e = {}
        for f in ("url", "logo"):
            if isinstance(v.get(f), str) and URL_RX.match(v[f].strip()): e[f] = v[f].strip()
        if e: out["free_edits"][k.lower()] = e
    out["lists_only_sources"] = [s(x, 40) for x in (r.get("lists_only_sources") or [])[:20] if s(x, 40)]
    sl = r.get("stream_limit")   # сколько каналов одновременно (HLS Proxy), применяет build.py; 0 = без лимита
    out["stream_limit"] = sl if isinstance(sl, int) and not isinstance(sl, bool) and 0 <= sl <= 50 else None
    out["stream_limit_mode"] = "auto" if r.get("stream_limit_mode") == "auto" else "manual"   # «Авто»: число считает редактор по источникам
    for ar in (r.get("auto_rules") or [])[:200]:
        if not isinstance(ar, dict) or ar.get("t") not in ("rename", "group"): continue
        f = ar.get("find") if isinstance(ar.get("find"), str) else ""
        f = re.sub(r"[\x00-\x1f\x7f]", "", f)[:200]
        if not f.strip(): continue
        try: re.compile(f if ar.get("rx") else re.escape(f))
        except re.error as e: raise ValueError(f"ошибка в правиле «{f}»: {e}")
        e2 = {"t": ar["t"], "find": f, "rx": ar.get("rx") is True}
        if ar["t"] == "rename": e2["repl"] = (s(ar.get("repl")) or "") if ar.get("repl") else ""
        else:
            if not s(ar.get("group")): continue
            e2["group"] = s(ar["group"])
        out["auto_rules"].append(e2)
    for g, ks in list((r.get("channel_order") or {}).items())[:3000]:
        if s(g) and isinstance(ks, list):
            v = keys(ks, 5000)
            if v: out["channel_order"][s(g)] = v
    ids = set()
    for L in (r.get("lists") or [])[:50]:
        if not isinstance(L, dict): continue
        lid = str(L.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", lid) or lid in ids: lid = secrets.token_hex(4)
        ids.add(lid)
        tok = L.get("token") if isinstance(L.get("token"), str) and TOK_RX.match(L.get("token")) else secrets.token_urlsafe(18)
        out["lists"].append({"id": lid, "name": s(L.get("name"), 80) or "Лист", "token": tok,
                             "groups": [s(g) for g in (L.get("groups") or [])[:3000] if s(g)],
                             "channels": keys(L.get("channels")), "exclude": keys(L.get("exclude")),
                             "enabled": L.get("enabled") is not False,
                             "all": L.get("all") is True,
                             "order": keys(L.get("order")),
                             "fgroups": [s(g, 140) for g in (L.get("fgroups") or [])[:500] if s(g, 140)],
                             "slug": L["slug"] if isinstance(L.get("slug"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", L["slug"]) else "",
                             "password": L["password"] if isinstance(L.get("password"), str) and re.fullmatch(r"[A-Za-z0-9_-]{4,64}", L["password"]) else "",
                             "expires": L["expires"] if isinstance(L.get("expires"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", L["expires"]) else "",
                             "no_adult": L.get("no_adult") is True,
                             "xgroups": [s(g) for g in (L.get("xgroups") or [])[:3000] if s(g)],
                             "sources": [s(x, 40) for x in (L.get("sources") or [])[:20] if s(x, 40)],
                             "chno": L["chno"] if isinstance(L.get("chno"), int) and not isinstance(L.get("chno"), bool) and 1 <= L["chno"] <= 99999 else None})
    seen_slug = set()
    for L in out["lists"]:
        if L["slug"] in seen_slug: L["slug"] = ""
        if L["slug"]: seen_slug.add(L["slug"])
    return out


def backup_rules():
    if not os.path.exists(RULES): return
    os.makedirs(BACKUPS, exist_ok=True)
    dst = f"{BACKUPS}/rules-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(RULES, "rb") as a, open(dst, "wb") as b: b.write(a.read())
    os.chmod(dst, 0o640)
    old = sorted(glob.glob(f"{BACKUPS}/rules-*.json"))[:-30]
    for f in old:
        try: os.remove(f)
        except OSError: pass


INF_RX = re.compile(r'#EXTINF:\s*-?\d+((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$')
ATTR_RX = re.compile(r'([\w-]+)="([^"]*)"')


def fetch_playlist(u):
    """Скачать чужой M3U и вернуть список каналов (название, группа, логотип) — без ссылок на потоки."""
    fm = FILE_RX.match(u.strip()) if isinstance(u, str) else None
    pm = re.fullmatch(r"e4://([a-z0-9-]{1,32})", u.strip()) if isinstance(u, str) else None
    if pm:   # подборка, которую собирает сервер (lists.py)
        try: b = open(f"{DATA}/preset-{pm.group(1)}.m3u", "rb").read(30 * 1024 * 1024)
        except OSError: return {"error": "Подборка ещё собирается на сервере (проверка ссылок занимает 2–5 минут). Нажмите «Сохранить», подождите и откройте снова"}
    elif fm:
        try: b = open(f"{UPLOADS}/{fm.group(1)}.m3u", "rb").read(30 * 1024 * 1024)
        except OSError as e: return {"error": f"файл не найден ({e.strerror})"}
    elif not isinstance(u, str) or not re.match(r"^https?://[^\s\"]{1,2000}$", u.strip()):
        return {"error": "нужна ссылка http(s)://…"}
    else:
      try:
        rq = urllib.request.Request(u.strip(), headers={"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"})
        with urllib.request.urlopen(rq, timeout=40, context=ssl.create_default_context()) as r:
            b = r.read(30 * 1024 * 1024)
      except Exception as e:
        return {"error": str(getattr(e, "reason", e))[:200]}
    t = b.decode("utf-8", "replace").replace("\r", "")
    if "#EXTM3U" not in t[:2000] and "#EXTINF" not in t[:20000]:
        return {"error": "по ссылке не M3U-плейлист"}
    out, cur = [], None
    for l in t.split("\n"):
        if l.startswith("#EXTINF"):
            m = INF_RX.match(l.strip())
            if not m: cur = None; continue
            a = dict(ATTR_RX.findall(m.group(1)))
            cur = {"n": s(m.group(2), 100) or "", "g": s(a.get("group-title") or "", 100) or "Без группы", "logo": a.get("tvg-logo", "")[:500]}
        elif l.startswith("#EXTGRP:") and cur:
            cur["g"] = s(l[8:], 100) or cur["g"]
        elif l.strip() and not l.startswith("#") and cur:
            if cur["n"]: cur["u"] = l.strip()[:2000]; learn_host(cur["u"]); out.append(cur)
            cur = None
            if len(out) >= 30000: break
    if not out: return {"error": "в плейлисте нет каналов"}
    return {"count": len(out), "channels": out}


# ---------- плеер: ссылки каналов и пересылка потока в браузер ----------
PLAY_HOSTS, PLAY_LOCK = set(), threading.Lock()   # хосты из ваших плейлистов — только к ним можно обращаться через /api/play
MAIN_CACHE = {"at": 0, "map": {}}
LIBS = {"hls.min.js", "mpegts.js"}


def learn_host(u):
    try:
        h = urllib.parse.urlsplit(u).hostname
        if h:
            with PLAY_LOCK:
                if len(PLAY_HOSTS) < 20000: PLAY_HOSTS.add(h.lower())
    except ValueError:
        pass


def own_hosts():
    return {str(h).lower() for h in (E4.get("address"), E4.get("host"), E4.get("domain")) if h}


def main_map():
    """Название канала -> ссылка из основного плейлиста (с HLS Proxy — ссылка рестрима). Кэш 60 секунд."""
    if time.time() - MAIN_CACHE["at"] < 60 and MAIN_CACHE["map"]: return MAIN_CACHE["map"]
    tok, port = E4.get("main_token"), E4.get("editor_port") or 80
    if not tok: return {}
    scheme = "https" if E4.get("scheme") == "https" else "http"
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        rq = urllib.request.Request(f"{scheme}://127.0.0.1:{port}/pl/{tok}.m3u8", headers={"Host": E4.get("host") or "localhost"})
        with urllib.request.urlopen(rq, timeout=15, context=ctx) as r: t = r.read(40 * 1024 * 1024).decode("utf-8", "replace")
    except Exception:
        return MAIN_CACHE["map"]
    m, name = {}, None
    for l in t.replace("\r", "").split("\n"):
        if l.startswith("#EXTINF"): name = l.rsplit(",", 1)[-1].strip().lower()
        elif l.strip() and not l.startswith("#") and name:
            m.setdefault(name, l.strip()); learn_host(l.strip()); name = None
    MAIN_CACHE.update(at=time.time(), map=m)
    return m


STATE_LEARNED = {"at": 0}


def learn_state():
    """Хосты ссылок каналов из снимка (state.json) — чтобы можно было открыть и скрытый канал напрямую."""
    if time.time() - STATE_LEARNED["at"] < 60: return
    STATE_LEARNED["at"] = time.time()
    for c in (read_json(STATE, {}) or {}).get("channels", []):
        for _, u in c.get("u", []): learn_host(u)


def private_host(host):
    try:
        for fam, _, _, _, sa in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(sa[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast: return True
    except Exception:
        return True
    return False


def list_backups():
    out = []
    for f in sorted(glob.glob(f"{BACKUPS}/rules-*.json"), reverse=True)[:40]:
        try: out.append({"name": os.path.basename(f), "size": os.path.getsize(f), "mtime": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(os.path.getmtime(f)))})
        except OSError: pass
    return out


def save_upload(body):
    """Загруженный с компьютера .m3u: сохраняем в uploads/<id>.m3u, возвращаем file:// ссылку для источника."""
    data = body.get("data")
    if not isinstance(data, str) or "#EXTINF" not in data[:200000]:
        return 400, {"error": "это не M3U-плейлист (нет #EXTINF)"}
    os.makedirs(UPLOADS, exist_ok=True)
    fid = secrets.token_hex(5)
    with open(f"{UPLOADS}/{fid}.m3u", "w", encoding="utf-8") as f: f.write(data)
    os.chmod(f"{UPLOADS}/{fid}.m3u", 0o640)
    return 200, {"url": f"file://{UPLOADS}/{fid}.m3u", "count": data.count("#EXTINF")}


def clean_uploads(rules):
    used = {m.group(1) for x in (rules.get("sources") or []) + (rules.get("free_sources") or []) for m in [FILE_RX.match(x.get("url", ""))] if m}   # файлы своих источников и отдельных плейлистов
    for f in glob.glob(f"{UPLOADS}/*.m3u"):
        if os.path.basename(f)[:-4] not in used and time.time() - os.path.getmtime(f) > 86400:
            try: os.remove(f)
            except OSError: pass


def check_url(u):
    """Проверка потока: ответ сервера, тип, для m3u8 — сколько качеств/сегментов. Читаем не больше 256 КБ."""
    if isinstance(u, str) and FILE_RX.match(u.strip()):
        r = fetch_playlist(u)
        return {"ok": "count" in r, "msg": f"файл: {r['count']} каналов" if "count" in r else r.get("error", "ошибка")}
    if not isinstance(u, str) or not re.match(r"^https?://[^\s\"]{1,2000}$", u.strip()):
        return {"ok": False, "msg": "проверяются только http/https ссылки"}
    ctx = ssl.create_default_context()
    t = time.time()
    try:
        rq = urllib.request.Request(u.strip(), headers={"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"})
        with urllib.request.urlopen(rq, timeout=10, context=ctx) as r:
            b = r.read(262144); ct = r.headers.get("Content-Type", ""); code = r.status
        ms = int((time.time() - t) * 1000)
        txt = b[:65536].decode("utf-8", "replace")
        media = True
        if "#EXTM3U" in txt and "#EXT-X-" in txt:
            v = txt.count("#EXT-X-STREAM-INF"); seg = txt.count("#EXTINF")
            kind = f"поток HLS: {v} качеств" if v else f"поток HLS: {seg} сегментов"
        elif "#EXTM3U" in txt:
            kind = f"плейлист каналов: {txt.count('#EXTINF')}+ записей"
        elif b[:1] == b"\x47" or "mp2t" in ct or "video" in ct: kind = "видеопоток"
        else: kind = f"не похоже на поток ({ct or 'без типа'}, {len(b)} байт)"; media = False
        return {"ok": code < 400 and media, "msg": f"{code} · {kind} · {ms} мс"}
    except Exception as e:
        return {"ok": False, "msg": str(getattr(e, "reason", e))[:200]}


def prep_status():
    try:
        out = subprocess.run(["systemctl", "show", "e4tv-build.service", "-p", "ActiveState",
                              "-p", "Result", "-p", "ExecMainExitTimestamp"],
                             capture_output=True, text=True, timeout=5).stdout
        d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    except Exception as e:
        d = {"error": str(e)}
    st = read_json(STATE, {}) or {}
    d["built_at"] = st.get("built_at")
    d["log"] = st.get("log", [])
    d["lists"] = read_json(LSTATUS, {})
    return d


class H(BaseHTTPRequestHandler):
    server_version = "evgen4ik-tv"

    def log_message(self, fmt, *args):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ALLOWED_HOSTS

    def _ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1") and self.headers.get("X-Real-IP"): ip = self.headers["X-Real-IP"].strip()[:64]
        return ip

    def _session(self):
        if not LOGIN_ON: return True
        a = auth_conf()
        if not a: return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE and "." in v:
                exp, _, sig = v.partition(".")
                if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig.encode(), sign(a, int(exp)).encode()):
                    return True
        return False

    def _play(self, u):
        """Пересылка потока в браузер (у потоков обычно нет CORS). Плейлисты HLS переписываются на /api/play."""
        try: p = urllib.parse.urlsplit(u)
        except ValueError: return self._send(400, {"error": "плохая ссылка"})
        host = (p.hostname or "").lower()
        if p.scheme not in ("http", "https") or not host: return self._send(400, {"error": "нужна ссылка http(s)"})
        local = host in own_hosts()
        with PLAY_LOCK: known = host in PLAY_HOSTS
        if not local and not known:
            learn_state()
            with PLAY_LOCK: known = host in PLAY_HOSTS
        if not local and not known: return self._send(403, {"error": "ссылка не из ваших плейлистов"})
        if not local and private_host(host): return self._send(403, {"error": "адрес внутри сети запрещён"})
        if local and (p.port or (443 if p.scheme == "https" else 80)) not in (80, 443): return self._send(403, {"error": "на этом сервере можно смотреть только через HLS Proxy"})
        target, hdr = u, {"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"}
        if local and p.scheme == "http":   # HLS Proxy на этом же сервере — напрямую, без петли через роутер
            target = urllib.parse.urlunsplit(("http", f"127.0.0.1:{p.port or 80}", p.path, p.query, "")); hdr["Host"] = p.netloc
        ctx = ssl.create_default_context()
        if local: ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        try:
            r = urllib.request.urlopen(urllib.request.Request(target, headers=hdr), timeout=20, context=ctx)
        except urllib.error.HTTPError as e:
            msg = {401: "провайдер не пустил (401)", 403: "провайдер не пустил (403) — возможно, превышен лимит устройств", 404: "канал не найден (404)",
                   429: "слишком много подключений (429) — лимит провайдера", 509: "превышен лимит (509)"}.get(e.code, f"ошибка {e.code}")
            return self._send(502, {"error": msg})
        except Exception as e:
            return self._send(502, {"error": "поток не отвечает: " + str(getattr(e, "reason", e))[:150]})
        with r:
            first = r.read(65536); ct = r.headers.get("Content-Type") or "application/octet-stream"
            if b"#EXTM3U" in first[:1024]:
                body = (first + r.read(4 * 1024 * 1024)).decode("utf-8", "replace")
                base = r.geturl()
                if local and p.scheme == "http": base = base.replace(f"127.0.0.1:{p.port or 80}", p.netloc, 1)
                def wrap(x):
                    a = urllib.parse.urljoin(base, x.strip()); learn_host(a)
                    return "/api/play?u=" + urllib.parse.quote(a, safe="")
                out = []
                for l in body.replace("\r", "").split("\n"):
                    if l.startswith("#"): out.append(re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + wrap(m.group(1)) + '"', l))
                    elif l.strip(): out.append(wrap(l))
                    else: out.append(l)
                return self._send(200, "\n".join(out), "application/vnd.apple.mpegurl")
            self.send_response(200)   # сегмент или непрерывный поток TS — пересылаем как есть
            self.send_header("Content-Type", ct); self.send_header("Cache-Control", "no-store")
            if r.headers.get("Content-Length"): self.send_header("Content-Length", r.headers["Content-Length"])
            self.end_headers()
            try:
                self.wfile.write(first)
                t0 = time.time()
                while time.time() - t0 < 4 * 3600:
                    b = r.read(65536)
                    if not b: break
                    self.wfile.write(b)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass   # плеер закрыли — поток освобождается

    def _cookie(self, val, age):
        return f"{COOKIE}={val}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}" + ("; Secure" if E4.get("scheme") == "https" else "")

    def _redirect(self, to, cookie=None):
        self.send_response(302)
        self.send_header("Location", to)
        if cookie: self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _login(self, body):
        ip = self._ip(); wait = locked_for(ip)
        if wait: return self._send(429, {"error": f"Слишком много попыток. Подождите {wait // 60 + 1} мин."})
        a = auth_conf()
        if not a: return self._send(500, {"error": "Нет файла входа. На сервере выполните: e4tv passwd"})
        u, p = str(body.get("user") or "")[:64], str(body.get("password") or "")[:256]
        if not pw_ok(a, u, p):
            add_fail(ip); time.sleep(1)
            return self._send(401, {"error": "Неверный логин или пароль"})
        with LOCK: FAILS.pop(ip, None)
        exp = int(time.time()) + SESSION_DAYS * 86400
        return self._send(200, {"ok": True}, headers={"Set-Cookie": self._cookie(f"{exp}.{sign(a, exp)}", SESSION_DAYS * 86400)})

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok(): return self._send(403, {"error": "bad host"})
        path = self.path.split("?", 1)[0]
        if path == "/login":
            if not LOGIN_ON or self._session(): return self._redirect("/")
            with open(f"{HERE}/login.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/logout":
            return self._redirect("/login" if LOGIN_ON else "/", self._cookie("", 0))
        if not self._session():
            return self._redirect("/login") if not path.startswith("/api/") else self._send(401, {"error": "нужен вход"})
        if path in ("/", "/index.html"):
            with open(f"{HERE}/index.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, {"state": read_json(STATE), "rules": read_json(RULES, {}), "status": prep_status(),
                                    "public_base": PUBLIC_BASE, "conf": PUBLIC_CONF})
        if self.path == "/api/backups":
            return self._send(200, list_backups())
        if path.startswith("/lib/") and path[5:] in LIBS:   # плееры hls.js и mpegts.js (Apache-2.0), лежат рядом с программой
            with open(f"{HERE}/lib/{path[5:]}", "rb") as f:
                return self._send(200, f.read(), "application/javascript; charset=utf-8")
        if path == "/api/chanurl":
            n = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("n", [""])[0].strip().lower()
            u = main_map().get(n)
            return self._send(200, {"url": u} if u else {"error": "канала нет в основном плейлисте"})
        if path == "/api/play":
            return self._play(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("u", [""])[0])
        if self.path == "/api/epg":
            return self._send(200, read_json(EPG, []))
        if self.path == "/api/status":
            return self._send(200, prep_status())
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok(): return self._send(403, {"error": "bad host"})
        # защита от CSRF: нестандартный заголовок + JSON вызывают preflight, которого мы не разрешаем
        if self.headers.get("X-M3U-Editor") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > (32 * 1024 * 1024 if self.path == "/api/upload" else MAX_BODY): return self._send(413, {"error": "слишком большой файл"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/login":
            return self._login(body if isinstance(body, dict) else {})
        if not self._session(): return self._send(401, {"error": "нужен вход"})
        if self.path == "/api/fetch":
            url = body.get("url")
            if body.get("source"):   # по имени своего источника — ссылка берётся из rules.json
                src = next((x for x in (read_json(RULES, {}) or {}).get("sources", []) if x.get("name") == body["source"]), None)
                url = src and src.get("url")
            return self._send(200, fetch_playlist(url))
        if self.path == "/api/upload":
            code, res = save_upload(body)
            return self._send(code, res)
        if self.path == "/api/force":   # собрать, даже если источник резко «похудел»
            with open(f"{DATA}/force.flag", "w") as f: f.write("1")
            with open(TRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True})
        if self.path == "/api/restore":
            name = str(body.get("name") or "")
            if not re.fullmatch(r"rules-[0-9A-Za-z_-]{6,40}\.json", name) or not os.path.exists(f"{BACKUPS}/{name}"):
                return self._send(400, {"error": "нет такой копии"})
            try:
                rules = validate(read_json(f"{BACKUPS}/{name}"))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            old = read_json(RULES, {}) or {}
            backup_rules(); write_json_atomic(RULES, rules)
            if old.get("lists") != rules["lists"]:
                with open(LTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True, "rules": rules})
        if self.path == "/api/check":
            return self._send(200, check_url(body.get("url")))
        if self.path == "/api/rules":
            try:
                rules = validate(body.get("rules"))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            old = read_json(RULES, {}) or {}
            backup_rules()
            write_json_atomic(RULES, rules)
            clean_uploads(rules)
            if body.get("apply"):   # полная пересборка; листы пересоберутся после неё сами
                with open(TRIGGER, "w") as f: f.write(str(time.time()))
            elif old.get("lists") != rules["lists"] or old.get("lists_only_sources") != rules["lists_only_sources"] or old.get("free_sources") != rules["free_sources"] or (old.get("free_edits") or {}) != rules["free_edits"]:
                with open(LTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True, "applied": bool(body.get("apply")), "rules": rules})
        return self._send(404, {"error": "not found"})


PUBLIC_CONF = {"mode": E4["mode"], "host": E4.get("host") or "", "login": LOGIN_ON,
               "main_url": (PUBLIC_BASE + E4["main_token"] + ".m3u8") if PUBLIC_BASE and E4.get("main_token") else "",
               "epg_url": E4.get("player_epg") or E4["epg_url"], "hls_url": E4.get("hls_url") or ""}

if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
