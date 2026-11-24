#!/usr/bin/env python3
"""Evgen4ik TV — редактор плейлистов.

Слушает 127.0.0.1:8095; снаружи доступен через nginx (вход по логину и паролю)
или только через SSH-туннель (access = tunnel в /etc/evgen4ik-tv/config.json).
Хранит правила в <data>/editor/rules.json. Их применяет build.py при каждой сборке
(утренний таймер или кнопка «Применить» -> файл apply.trigger -> e4tv-build.path).
Только стандартная библиотека Python.
"""
import json, os, re, subprocess, time, glob, secrets, ssl, urllib.request, urllib.parse, urllib.error, hmac, hashlib, threading, sys, socket, ipaddress, collections
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4
import catchup

HOST, PORT = E4["listen"], int(E4["port"])
DATA = E4["editor_dir"]
RULES = f"{DATA}/rules.json"
STATE = f"{DATA}/state.json"
TRIGGER = f"{DATA}/apply.trigger"
LTRIGGER = f"{DATA}/lists.trigger"
EPG_FLAG = f"{DATA}/epg-fetch.flag"         # «Обновить телегид сейчас» (выполняет lists.py)
EPG_STATUS = f"{DATA}/epg-status.json"      # итог последнего запроса обновления телегида
LSTATUS = f"{DATA}/lists-status.json"
ACTIVITY = f"{DATA}/activity.json"
HE_HIDDEN = f"{DATA}/he-hidden.json"   # «Не открылось у клиентов»: скрытые записи {канал: время последней ошибки}
XC_FILE = f"{DATA}/xc.json"          # каналы клиентов для входа через Xtream Codes (собирает lists.py)
XC_ACT = f"{DATA}/xc-activity.json"  # кто входил через Xtream и какой канал включал (пишет сам редактор)
EPG = f"{DATA}/epg.json"
AC_RESULT = f"{DATA}/autocheck.json"     # итог автопроверки потоков (пишет autocheck.py от root)
AC_CONF = f"{DATA}/autocheck-conf.json"  # автопроверка: по расписанию или нет, сколько проверок одновременно
CTRIGGER = f"{DATA}/check.trigger"       # кнопка «Проверить сейчас» -> e4tv-check.path
AC_PROGRESS = f"{DATA}/autocheck-progress.json"   # ход проверки (пишет autocheck.py раз в секунду)
AC_CTL = f"{DATA}/autocheck.ctl"         # пауза / остановка проверки: pause, stop или пусто
PUBLIC_BASE = E4["public_base"]
LINK_MODES = ("hls", "redirect", "direct")   # «Ссылки на каналы» у плейлиста/клиента (lists.py)
URL_RX = re.compile(r"^(https?|rtmp|rtsp|udp|rtp)://[^\s\"]{1,2000}$", re.I)
UPLOADS = E4["uploads"]
FILE_RX = re.compile(r"^file://" + re.escape(UPLOADS) + r"/([a-z0-9]{4,16})\.m3u$")
TOK_RX = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
BACKUPS = f"{DATA}/backups"
HERE = os.path.dirname(os.path.abspath(__file__))
MAX_BODY = 16 * 1024 * 1024   # правила целиком (у клиентов могут быть тысячи своих каналов из файлов)
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
    return [k.lower()[:200] for k in (v if isinstance(v, list) else [])[:n] if isinstance(k, str) and k.strip()]


def lst(v):
    return v if isinstance(v, list) else []


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
        if s(v.get("primary")): e["primary"] = s(v["primary"])[:40]   # основной источник у этого канала (остальные — резерв)
        sh = v.get("shift")   # сдвиг программы передач (tvg-shift), часов: -12…+14, 0 — не сдвигать
        if isinstance(sh, (int, float)) and not isinstance(sh, bool) and sh and -12 <= sh <= 14 and float(sh) * 2 == int(float(sh) * 2): e["shift"] = int(sh) if float(sh).is_integer() else float(sh)
        if v.get("logo") == "none" or (isinstance(v.get("logo"), str) and URL_RX.match(v["logo"].strip())): e["logo"] = v["logo"].strip()   # "none" — «Без логотипа»
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
    for x in (r.get("sources") or [])[:30]:   # до 30 источников (например, несколько своих ключей одного провайдера)
        if not isinstance(x, dict) or not s(x.get("name"), 40) or not isinstance(x.get("url"), str) or not (URL_RX.match(x["url"].strip()) or FILE_RX.match(x["url"].strip())): continue
        sid = str(x.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", sid) or sid in sids: sid = secrets.token_hex(4)
        sids.add(sid)
        src = {"id": sid, "name": s(x["name"], 40), "url": x["url"].strip(), "enabled": x.get("enabled") is not False}
        if re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("prov") or "")): src["prov"] = x["prov"]   # шаблон провайдера (для лимита)
        if x.get("nolim") is True: src["nolim"] = True   # сторонний плейлист без лимита устройств (режим лимита «Авто»)
        if x.get("nomerge") is True: src["nomerge"] = True   # не склеивать одноимённые каналы с другими источниками (HLS Proxy)
        if x.get("pa") is True and "prov" in src: src["pa"] = True   # провайдер определён автоматически (по ссылке/потокам)
        lim = x.get("lim")   # своё число устройств по тарифу (важнее шаблона провайдера)
        if isinstance(lim, int) and not isinstance(lim, bool) and 1 <= lim <= 20: src["lim"] = lim
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
                             "owner": str(L["owner"]) if isinstance(L.get("owner"), str) and re.fullmatch(r"[a-z0-9]{1,16}", L["owner"]) else "",   # личный плейлист клиента
                             "no_clients": L.get("no_clients") is True,   # общий плейлист, который нельзя давать клиентам
                             "xgroups": [s(g) for g in (L.get("xgroups") or [])[:3000] if s(g)],
                             "sources": [s(x, 40) for x in (L.get("sources") or [])[:20] if s(x, 40)],
                             "chno": L["chno"] if isinstance(L.get("chno"), int) and not isinstance(L.get("chno"), bool) and 1 <= L["chno"] <= 99999 else None,
                             # ссылки провайдера: hls — полностью (через HLS Proxy), redirect — только в файле, direct — не скрывать; "" — как у всех
                             "link_mode": L["link_mode"] if L.get("link_mode") in LINK_MODES else ""})
    # клиенты: люди со своей личной ссылкой, сроком и лимитом; файл плейлиста собирает lists.py
    lids = {L["id"] for L in out["lists"]}
    out["clients"], cids, ctoks = [], set(), {L["token"] for L in out["lists"]}
    for c in lst(r.get("clients"))[:200]:
        if not isinstance(c, dict) or not s(c.get("name"), 80): continue
        cid = str(c.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", cid) or cid in cids: cid = secrets.token_hex(4)
        cids.add(cid)
        tok = c.get("token") if isinstance(c.get("token"), str) and TOK_RX.match(c["token"]) and c["token"] not in ctoks else secrets.token_urlsafe(18)
        ctoks.add(tok)
        hp = c.get("hls_pw") if isinstance(c.get("hls_pw"), str) and re.fullmatch(r"[A-Za-z0-9_-]{16,64}", c["hls_pw"]) else secrets.token_urlsafe(18)
        st = c.get("streams")
        out["clients"].append({"id": cid, "name": s(c["name"], 80), "contact": s(c.get("contact"), 80) or "",
                               "note": s(c.get("note"), 300) or "", "enabled": c.get("enabled") is not False,
                               "expires": c["expires"] if isinstance(c.get("expires"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["expires"]) else "",
                               "lists": [x for x in dict.fromkeys(str(x) for x in lst(c.get("lists"))[:50]) if x in lids],
                               "streams": st if isinstance(st, int) and not isinstance(st, bool) and 0 <= st <= 50 else 1,
                               "no_adult": c.get("no_adult") is True, "token": tok, "hls_pw": hp,
                               # короткая ссылка /pl/<slug>.m3u8 (вторая, к длинной); уникальность — ниже, вместе с плейлистами
                               "slug": c["slug"] if isinstance(c.get("slug"), str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", c["slug"]) else "",
                               "link_mode": c["link_mode"] if c.get("link_mode") in LINK_MODES else "",   # "" — как у плейлиста
                               # свой список поверх плейлистов: выключенные каналы, добавленные из основного, свои ссылки и свои каналы
                               "off": keys(c.get("off"), 5000), "add": keys(c.get("add"), 3000),
                               "urls": {k.lower()[:200]: u.strip() for k, u in list((c.get("urls") or {}).items())[:3000]
                                        if isinstance(k, str) and k.strip() and isinstance(u, str) and URL_RX.match(u.strip())} if isinstance(c.get("urls"), dict) else {},
                               "extra": [{"id": x["id"] if isinstance(x.get("id"), str) and re.fullmatch(r"[a-z0-9]{1,16}", x["id"]) else secrets.token_hex(4),
                                          "name": s(x["name"], 100), "group": s(x.get("group"), 60) or "Мои каналы", "url": x["url"].strip(),
                                          "logo": x["logo"].strip() if isinstance(x.get("logo"), str) and re.match(r"^https?://[^\s\"]{1,1000}$", x["logo"].strip()) else "",
                                          "src": s(x.get("src"), 60) or ""}   # из какого файла (пусто — добавлен вручную)
                                         for x in lst(c.get("extra"))[:3000]
                                         if isinstance(x, dict) and s(x.get("name"), 100) and isinstance(x.get("url"), str) and URL_RX.match(x["url"].strip())],
                               "created": c["created"] if isinstance(c.get("created"), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", c["created"]) else time.strftime("%Y-%m-%d")})
    # личные плейлисты: только у живого владельца, без своей ссылки; у клиента — только свои личные и общие
    cset = {c["id"] for c in out["clients"]}
    out["lists"] = [L for L in out["lists"] if not L["owner"] or L["owner"] in cset]
    for L in out["lists"]:
        if L["owner"]: L.update({"enabled": False, "slug": "", "password": "", "expires": "", "no_clients": False})
    own = {L["id"]: L["owner"] for L in out["lists"]}
    for c in out["clients"]:
        c["lists"] = [x for x in c["lists"] if x in own and own[x] in ("", c["id"])]
    xu = set()
    for c, src in zip(out["clients"], [x for x in lst(r.get("clients"))[:200] if isinstance(x, dict) and s(x.get("name"), 80)]):
        u = str(src.get("xc_user") or "").lower()
        if not re.fullmatch(r"[a-z0-9_]{3,32}", u) or u in xu: u = "tv" + c["id"]
        xu.add(u); c["xc_user"] = u
        c["xc_pw"] = src["xc_pw"] if isinstance(src.get("xc_pw"), str) and re.fullmatch(r"[A-Za-z0-9]{8,32}", src["xc_pw"]) else secrets.token_hex(6)
        ips = []
        for x in lst(src.get("ips"))[:50]:
            try: ips.append(str(ipaddress.ip_network(str(x).strip(), strict=False)))
            except ValueError: pass
        c["ips"] = list(dict.fromkeys(ips))
        c["order"] = keys(src.get("order"), 8000)
    msg = r.get("client_msg")
    out["client_msg"] = re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", msg)[:2000] if isinstance(msg, str) else ""
    # «Скрывать ссылки провайдера»: общая настройка (у плейлиста и клиента link_mode "" = как у всех)
    out["link_mode"] = r["link_mode"] if r.get("link_mode") in LINK_MODES else ""
    out["logo_proxy"] = r.get("logo_proxy") is not False   # логотипы через сервер
    out["fallback"] = r.get("fallback") is not False       # своя ссылка не прошла через HLS Proxy — отдать «только в файле»
    # телегид: свои источники (пусто — как при установке, it999) и сколько дней хранить в HLS Proxy (None — не менять)
    es = r.get("epg_sources") if isinstance(r.get("epg_sources"), list) else []
    out["epg_sources"] = list(dict.fromkeys(u.strip() for u in es if isinstance(u, str) and re.match(r"^https?://[^\s\"<>]{1,1000}$", u.strip())))[:5]
    ed = r.get("epg_days")
    out["epg_days"] = ed if isinstance(ed, int) and not isinstance(ed, bool) and 1 <= ed <= 14 else None
    out["open_logos"] = r.get("open_logos") is True   # логотипы из открытых баз (it999, iptv-org, tv-logos) каналам без картинки
    out["stream_format"] = r["stream_format"] if r.get("stream_format") in ("hls", "ts") else ""   # формат потока HLS Proxy для плееров; "" — как настроено в нём
    seen_slug = {L["token"] for L in out["lists"]} | {c["token"] for c in out.get("clients", [])}   # короткая не совпадает ни с чьей длинной
    for x in out["lists"] + out.get("clients", []):
        if x.get("slug") in seen_slug: x["slug"] = ""
        if x.get("slug"): seen_slug.add(x["slug"])
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


def state_with_logos():
    """state.json для редактора. С HLS Proxy: если источник не прислал логотип, показываем тот,
    что HLS Proxy сам подставил в плейлист (то же, что увидит плеер)."""
    st = read_json(STATE)
    if not st or E4["mode"] != "hls": return st
    try:
        main_map(); lg = MAIN_CACHE.get("logo") or {}
        for c in st.get("channels") or []:
            if isinstance(c, dict) and not c.get("logo"):
                u = lg.get(str(c.get("k") or c.get("n") or "").lower())
                if u: c["logo"] = u; c["logo_hls"] = True; c["ls"] = "hls"
    except Exception: pass
    return st


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
    m, name, lg = {}, None, {}
    for l in t.replace("\r", "").split("\n"):
        if l.startswith("#EXTINF"):
            name = l.rsplit(",", 1)[-1].strip().lower()
            g = re.search(r'tvg-logo="([^"]+)"', l)
            if g: lg.setdefault(name, g.group(1))
        elif l.strip() and not l.startswith("#") and name:
            m.setdefault(name, l.strip()); learn_host(l.strip()); name = None
    MAIN_CACHE.update(at=time.time(), map=m, logo=lg)
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


def stream_domains(urls, top=3):
    """Самые частые домены потоков («имя.зона», без ссылок и токенов) — как build.stream_domains."""
    cnt = collections.Counter()
    for u in urls:
        m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/\s]*@)?([^/:\s?#]+)", u or "", re.I)
        if not m: continue
        h = m.group(1).lower()
        cnt[h if re.fullmatch(r"[\d.]+", h) else ".".join(h.split(".")[-2:])] += 1
    return [d for d, _ in cnt.most_common(top)]


# ---------- значки провайдеров: favicon с официального сайта, скачивается сервером один раз ----------
# Только подтверждённые сайты (18.11.2026). В установщик и на GitHub файлы значков не кладём — берём у провайдера.
# Нет в списке / не скачалось — в редакторе остаются буквы. Адрес берётся только отсюда, не из запроса.
PROV_SITES = {"sharavoz": "sharavoz.tv", "antifriz": "antifriz.tv", "ottclub": "ottclub.app",
              "bittv": "bittv.ltd", "iptvonline": "iptv.online", "rutv": "rutv.vip"}
# Telegram провайдера: ссылка с его официального сайта (Sharavoz, BitTV) или канал, сам ссылающийся на сайт (RUTV.VIP).
# Если на сайте значка нет — берём аватар канала (og:image с t.me, картинка только с серверов Telegram)
PROV_TG = {"sharavoz": "+UYr6eQahIFFiYTYy", "bittv": "BitTV_Forum", "rutv": "rutv_vip"}
TG_IMG_RX = re.compile(r"^https://cdn\d*\.telesco\.pe/file/[A-Za-z0-9_\-]+\.(?:jpg|jpeg|png|webp)$|^https://cdn\d*\.telesco\.pe/file/[A-Za-z0-9_\-]+$")
ICON_DIR = f"{DATA}/provicons"
ICON_MAX = 200 * 1024
ICON_LOCK = threading.Lock()


def icon_type(b):
    """Тип картинки по первым байтам; SVG не принимаем (в нём может быть скрипт)."""
    if b[:8] == b"\x89PNG\r\n\x1a\n": return "image/png"
    if b[:4] == b"\x00\x00\x01\x00": return "image/x-icon"
    if b[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if b[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP": return "image/webp"
    return None


def _get(url, limit):
    rq = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Evgen4ik TV editor)"})
    with urllib.request.urlopen(rq, timeout=8, context=ssl.create_default_context()) as r:
        return r.read(limit + 1)


def prov_icon(pid):
    """(байты, тип) значка провайдера или None. Кэш 30 дней, неудача запоминается на сутки."""
    site, tg = PROV_SITES.get(pid), PROV_TG.get(pid)
    if not site and not tg: return None
    p = f"{ICON_DIR}/{pid}"
    with ICON_LOCK:
        try:
            if time.time() - os.path.getmtime(p) < 30 * 86400:
                b = open(p, "rb").read(); t = icon_type(b)
                if t: return b, t
        except OSError: pass
        try:
            if time.time() - os.path.getmtime(p + ".fail") < 86400: return None
        except OSError: pass
        cands = []
        if site:
            try:   # <link rel="icon" | "shortcut icon" | "apple-touch-icon" href="…"> на главной
                html = _get(f"https://{site}/", 262144).decode("utf-8", "replace")
                for tag in re.findall(r"<link\b[^>]*>", html, re.I):
                    if re.search(r'rel=["\'][^"\']*icon[^"\']*["\']', tag, re.I):
                        m = re.search(r'href=["\']([^"\']+)["\']', tag, re.I)
                        if m and not m.group(1).lower().split("?")[0].endswith(".svg"):
                            cands.append(urllib.parse.urljoin(f"https://{site}/", m.group(1)))
            except Exception: pass
            cands.append(f"https://{site}/favicon.ico")
        cands = cands[:6]
        if tg:   # запасной вариант — аватар Telegram-канала
            try:
                html = _get(f"https://t.me/{tg}", 262144).decode("utf-8", "replace")
                m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
                if m and TG_IMG_RX.match(m.group(1)): cands.append(m.group(1))
            except Exception: pass
        for u in cands:
            if not u.startswith("https://"): continue
            try:
                b = _get(u, ICON_MAX)
                if len(b) <= ICON_MAX and icon_type(b):
                    os.makedirs(ICON_DIR, exist_ok=True)
                    with open(p + ".tmp", "wb") as f: f.write(b)
                    os.replace(p + ".tmp", p)
                    try: os.remove(p + ".fail")
                    except OSError: pass
                    return b, icon_type(b)
            except Exception: continue
        try:
            os.makedirs(ICON_DIR, exist_ok=True); open(p + ".fail", "w").close()
        except OSError: pass
        return None


# ---------- «Ссылки на каналы»: перенаправление и логотипы через сервер (карту пишет lists.py в links.json) ----------
# /live/_r/<ключ плейлиста>/<код> -> 302 на исходную ссылку; /live/_logo/<код> -> копия логотипа. Коды — HMAC от ссылки, не угадать.
# Адреса под /live/ nginx уже пускает в редактор без входа (как Xtream), правка nginx не нужна.
def add_query(u, raw_path):
    """Дописать к ссылке то, что плеер добавил к нашей (архив: utc, lutc и т. п.). Только безопасные знаки и не длиннее 300."""
    qs = raw_path.split("?", 1)[1] if "?" in raw_path else ""
    if not qs or not re.fullmatch(r"[A-Za-z0-9_.:%=&+-]{1,300}", qs): return u
    return u + ("&" if "?" in u else "?") + qs


def local_tz():
    """Часовой пояс сервера по-человечески (Europe/Moscow), для Xtream: плееры шлют время архива по нему."""
    try:
        p = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in p: return p.split("/zoneinfo/", 1)[1]
    except OSError: pass
    return time.strftime("%Z") or "UTC"


LINKS_FILE = f"{DATA}/links.json"
LOGO_DIR = f"{DATA}/logos"
LOGO_MAX = 512 * 1024
def _blank_png():
    """Прозрачная картинка 1x1 для «Без логотипа»."""
    import struct, zlib
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + ch(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00")) + ch(b"IEND", b"")
BLANK_PNG = _blank_png()
_LINKS = {"m": None, "d": {}}


def links():
    try: m = os.path.getmtime(LINKS_FILE)
    except OSError: return {}
    if m != _LINKS["m"]:
        d = read_json(LINKS_FILE, {}) or {}
        d["t"] = {k: set(v) for k, v in (d.get("t") or {}).items() if isinstance(v, list)}
        _LINKS.update(m=m, d=d)
    return _LINKS["d"]


def logo_get(h):
    """(байты, тип) копии логотипа или None. Кэш 7 дней, неудача — сутки. Только картинки (не SVG)."""
    u = (links().get("l") or {}).get(h)
    if not isinstance(u, str) or not re.match(r"^https?://", u): return None
    p = f"{LOGO_DIR}/{h}"
    try:
        if time.time() - os.path.getmtime(p) < 7 * 86400:
            b = open(p, "rb").read(); t = icon_type(b)
            if t: return b, t
    except OSError: pass
    try:
        if time.time() - os.path.getmtime(p + ".fail") < 86400: return None
    except OSError: pass
    try:
        rq = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (Evgen4ik TV)"})
        with urllib.request.urlopen(rq, timeout=8, context=ssl.create_default_context()) as r: b = r.read(LOGO_MAX + 1)
        if len(b) <= LOGO_MAX and icon_type(b):
            os.makedirs(LOGO_DIR, exist_ok=True)
            with open(p + ".tmp", "wb") as f: f.write(b)
            os.replace(p + ".tmp", p)
            return b, icon_type(b)
    except Exception: pass
    try:
        os.makedirs(LOGO_DIR, exist_ok=True); open(p + ".fail", "w").close()
    except OSError: pass
    return None


def check_url(u):
    """Проверка потока: ответ сервера, тип, для m3u8 — сколько качеств/сегментов. Читаем не больше 256 КБ."""
    if isinstance(u, str) and FILE_RX.match(u.strip()):
        r = fetch_playlist(u)
        return {"ok": "count" in r, "msg": f"файл: {r['count']} каналов" if "count" in r else r.get("error", "ошибка")}
    if not isinstance(u, str) or not re.match(r"^https?://[^\s\"]{1,2000}$", u.strip()):
        return {"ok": False, "msg": "проверяются только http/https ссылки"}
    if not u.strip().isascii():   # кириллица в адресе (например, незаменённый «ВАШ-ПОДДОМЕН») — иначе urllib падает с «ordinal not in range(256)»
        return {"ok": False, "msg": "в ссылке есть не-латинские символы (кириллица?) — проверьте адрес"}
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
            hosts = stream_domains(l.strip() for l in b.decode("utf-8", "replace").splitlines() if l.strip() and not l.startswith("#"))
            return {"ok": code < 400, "msg": f"{code} · {kind} · {ms} мс", "hosts": hosts}   # домены потоков — узнать провайдера
        elif b[:1] == b"\x47" or "mp2t" in ct or "video" in ct: kind = "видеопоток"
        else: kind = f"не похоже на поток ({ct or 'без типа'}, {len(b)} байт)"; media = False
        return {"ok": code < 400 and media, "msg": f"{code} · {kind} · {ms} мс"}
    except Exception as e:
        return {"ok": False, "msg": str(getattr(e, "reason", e))[:200]}


# ---------- автопроверка потоков (autocheck.py, служба e4tv-check) ----------
def ac_conf():
    n = read_json(AC_CONF, {}) or {}
    n = n if isinstance(n, dict) else {}
    try: w = max(1, min(6, int(n.get("workers", 2))))
    except Exception: w = 2
    return {"auto": bool(n.get("auto")), "workers": w}


# ---------- уведомления в Telegram (notify.py, таймер e4tv-notify) ----------
TG_FILE = f"{DATA}/telegram.json"          # токен бота и чат: права 600, не в rules.json и не в резервных копиях
NOTIFY_STATE = f"{DATA}/notify-state.json"
TG_EVENTS = ("expire", "build", "source", "newip")


def tg_info():
    c = read_json(TG_FILE, {}) or {}
    st = read_json(NOTIFY_STATE, {}) or {}
    ev = c.get("events") if isinstance(c.get("events"), dict) else {}
    tok = str(c.get("token") or "")
    return {"token_set": bool(tok), "token_hint": (tok.split(":")[0] + ":…" + tok[-4:]) if tok else "", "chat_id": str(c.get("chat_id") or ""),
            "events": {e: ev.get(e) is not False for e in TG_EVENTS}, "last_run": st.get("last_run"), "last_error": st.get("last_error") or "",
            "backup_on": bool((c.get("backup") or {}).get("on")), "backup_pass_set": len(str((c.get("backup") or {}).get("pass") or "")) >= 8,
            "last_backup": st.get("last_backup"), "backup_error": st.get("backup_error") or "",
            "timer": os.path.exists("/etc/systemd/system/e4tv-notify.timer")}


def tg_save(body):
    import notify
    c = read_json(TG_FILE, {}) or {}
    if body.get("clear"): c = {}
    else:
        tok = str(body.get("token") or "").strip()
        if tok:
            if not notify.TOKEN_RX.match(tok): raise ValueError("Токен не похож на токен бота: 123456789:AA… (берётся у @BotFather)")
            c["token"] = tok
        if "chat_id" in body:
            ch = str(body.get("chat_id") or "").strip()
            if ch and not notify.CHAT_RX.match(ch): raise ValueError("Чат: число (например 123456789 или -100…) или @имя_канала")
            c["chat_id"] = ch
        if isinstance(body.get("events"), dict): c["events"] = {e: body["events"].get(e) is not False for e in TG_EVENTS}
        bk = c.get("backup") if isinstance(c.get("backup"), dict) else {}
        pw = str(body.get("backup_pass") or "")
        if pw:
            if len(pw) < 8 or len(pw) > 200: raise ValueError("Пароль копии: от 8 символов")
            bk["pass"] = pw
        if "backup_on" in body:
            if body.get("backup_on") and len(str(bk.get("pass") or "")) < 8: raise ValueError("Чтобы включить копии, задайте пароль копии (от 8 символов) и сохраните его в менеджере паролей")
            bk["on"] = bool(body.get("backup_on"))
        c["backup"] = bk
    tmp = TG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False)
    os.chmod(tmp, 0o600); os.replace(tmp, TG_FILE)
    return tg_info()


def tg_chats(body):
    """Кто писал боту: чтобы выбрать чат без поиска chat_id вручную (getUpdates)."""
    import notify
    tok = str(body.get("token") or "").strip() or str((read_json(TG_FILE, {}) or {}).get("token") or "")
    if not notify.TOKEN_RX.match(tok): raise ValueError("Сначала укажите токен бота")
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/getUpdates?limit=100",
                                                           headers={"User-Agent": "Evgen4ik-TV"}), timeout=20) as r: j = json.load(r)
    except urllib.error.HTTPError as e:
        raise ValueError("Telegram: " + ("токен не подходит" if e.code in (401, 404) else "бот занят другой программой (getUpdates)" if e.code == 409 else f"HTTP {e.code}"))
    except Exception as e:
        raise ValueError(f"нет связи с api.telegram.org: {getattr(e, 'reason', e)}")
    out = {}
    for u in j.get("result") or []:
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        ch = m.get("chat") or {}
        if "id" in ch: out[str(ch["id"])] = ch.get("title") or " ".join(x for x in (ch.get("first_name"), ch.get("last_name")) if x) or ch.get("username") or str(ch["id"])
    return {"chats": [{"id": k, "name": v} for k, v in out.items()]}


# ---------- «Подключить домен» (HTTPS): проверка DNS здесь, перенастройку делает root-служба e4tv-domain ----------
DOMAIN_REQ = f"{DATA}/domain.request"
DOMAIN_ST = f"{DATA}/domain-status.json"
DOM_RX = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAIL_RX = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}$")


def domain_check(body):
    d = str(body.get("domain") or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
    if not DOM_RX.match(d): raise ValueError("Домен вида tv.example.com — без http:// и пути")
    try: ips = sorted({a[4][0] for a in socket.getaddrinfo(d, None, socket.AF_INET)})
    except socket.gaierror: ips = []
    mine = sorted({str(h) for h in [E4.get("address")] + list(E4.get("hosts") or []) if re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}", str(h or ""))})
    pub = [ip for ip in mine if not ipaddress.ip_address(ip).is_private]
    return {"domain": d, "ips": ips, "server_ips": pub or mine, "match": bool(set(ips) & set(pub or mine)),
            "home": not pub, "now": E4.get("domain") or "", "mode": E4["mode"], "can": os.path.exists(DOMAIN_REQ)}


def domain_set(body):
    r = domain_check(body)
    m = str(body.get("email") or "").strip()
    if m and not MAIL_RX.match(m): raise ValueError("E-mail неверный (или оставьте пустым)")
    if not r["can"]: raise ValueError("На сервере нет службы подключения домена — обновите Evgen4ik TV или выполните на сервере: sudo e4tv domain " + r["domain"])
    if not r["match"]: raise ValueError(f"Домен {r['domain']} указывает на {', '.join(r['ips']) or 'ничего'}, а нужен {', '.join(r['server_ips'])} — исправьте запись A и подождите")
    with open(DOMAIN_ST, "w") as f: json.dump({"state": "queued", "domain": r["domain"], "at": int(time.time())}, f)
    with open(DOMAIN_REQ, "w") as f: json.dump({"domain": r["domain"], "email": m}, f)
    return {"ok": True, **r}


def tg_backup_now(body):
    import notify
    ok, c, _ = notify.conf()
    if not ok: raise ValueError("Сначала настройте бота и чат")
    try: name = notify.send_backup(c)
    except Exception as e: raise ValueError(f"Не отправилось: {e}")
    return {"ok": True, "name": name}


def restore_file(body):
    """Восстановление из файла копии (.e4b): расшифровать, проверить правила, сохранить текущие в «Историю», применить."""
    import base64, notify
    try: data = base64.b64decode(str(body.get("data") or ""), validate=True)
    except Exception: raise ValueError("файл не прочитался")
    try: bk = notify.open_backup(data, str(body.get("pass") or ""))
    except Exception as e: raise ValueError(str(e))
    rules = validate(bk["rules"])
    if body.get("check"): return {"ok": True, "meta": bk["meta"], "clients": len(rules["clients"]), "lists": len(rules["lists"]), "sources": len(rules.get("sources") or [])}
    os.makedirs(UPLOADS, exist_ok=True)
    for n, b in bk["uploads"].items():
        with open(f"{UPLOADS}/{n}", "wb") as f: f.write(b)
    backup_rules(); write_json_atomic(RULES, rules)
    for t in (TRIGGER, LTRIGGER):
        with open(t, "w") as f: f.write(str(time.time()))
    return {"ok": True, "rules": rules, "meta": bk["meta"]}


def tg_test(body):
    import notify
    ok, c, _ = notify.conf()
    if not ok: raise ValueError("Сохраните токен бота и чат")
    try: notify.send(c, "✅ Evgen4ik TV: уведомления работают. Сюда придут: срок клиентов, ошибки сборки, источники, новые IP.")
    except Exception as e: raise ValueError(f"Не отправилось: {e}")
    return {"ok": True}


def ac_progress():
    """Ход проверки. Файл не обновлялся полторы минуты — процесс умер (перезагрузка, kill): не «идёт»."""
    p = read_json(AC_PROGRESS, {}) or {}
    if not isinstance(p, dict): return {}
    if p.get("running") and time.time() - (p.get("upd") or 0) > 90: p.update(running=False, stale=True)
    return p


def autocheck_info():
    return {"result": read_json(AC_RESULT, {}) or {}, "trigger_ready": os.path.exists(CTRIGGER), "conf": ac_conf(),
            "progress": ac_progress()}


def ac_ctl(cmd):
    """Кнопки «Пауза», «Продолжить», «Остановить» во время проверки."""
    if cmd not in ("pause", "resume", "stop"): raise ValueError("неизвестная команда")
    if not ac_progress().get("running"): raise ValueError("проверка сейчас не идёт")
    with open(AC_CTL, "w") as f: f.write("" if cmd == "resume" else cmd)
    return autocheck_info()


def save_ac_conf(body):
    if not isinstance(body, dict): raise ValueError("bad body")
    try: w = max(1, min(6, int(body.get("workers", 2))))
    except Exception: w = 2
    write_json_atomic(AC_CONF, {"auto": bool(body.get("auto")), "workers": w})
    return autocheck_info()


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
    d["warnings"] = st.get("warnings", [])
    d["sources"] = [{k: x.get(k) for k in ("name", "count", "status", "hosts")} for x in (st.get("sources") or []) if isinstance(x, dict)]   # без ссылок
    if E4["mode"] == "hls":   # для блока состояния в боковой панели
        try: d["hls"] = subprocess.run(["systemctl", "is-active", "hls-proxy"], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception: d["hls"] = "unknown"
        try:   # когда HLS Proxy последний раз обновил телегид (для «Обзора»)
            d["epg_at"] = time.strftime("%d.%m.%Y %H:%M", time.localtime(os.path.getmtime(os.path.join(E4.get("hls_dir") or "/opt/hls-proxy", "epg.xml.gz"))))
        except OSError: d["epg_at"] = ""
        d["epg_req"] = os.path.exists(EPG_FLAG)                # запрошено обновление, ещё не передано HLS Proxy
        d["epg_st"] = read_json(EPG_STATUS, {}) or {}
        try:   # что сейчас в HLS Proxy: источники и сколько дней хранить (local.json читает только root — берём из сводки lists.py)
            d["epg_hls"] = (read_json(EPG_STATUS, {}) or {}).get("hls") or {}
        except Exception: d["epg_hls"] = {}
    try:   # следующая плановая сборка (таймер e4tv-build)
        nx = subprocess.run(["systemctl", "show", "e4tv-build.timer", "-p", "NextElapseUSecRealtime"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)", nx)
        d["next_build"] = f"{m.group(3)}.{m.group(2)}.{m.group(1)} {m.group(4)}:{m.group(5)}" if m else ""
    except Exception: d["next_build"] = ""
    r = read_json(AC_RESULT, {}) or {}
    d["ac"] = {"at": r.get("at"), "total": r.get("total"), "ok": r.get("ok"), "bad": len(r.get("bad") or []), "error": r.get("error")} if r else {}
    d["lists"] = read_json(LSTATUS, {})
    d["activity"] = read_json(ACTIVITY, {})   # кто и когда открывал плейлисты (по журналу nginx, собирает lists.py --activity)
    d["hls_err"] = read_json(f"{DATA}/hls-errors.json", {}) or {}   # «Не открылось у клиентов»: ошибки источников из журнала HLS Proxy (lists.py --activity)
    d["he_hidden"] = read_json(HE_HIDDEN, {}) or {}
    with XC_ACT_LOCK: d["xc"] = dict(XC_ACT_MEM) if XC_ACT_MEM is not None else (read_json(XC_ACT, {}) or {})
    return d


NOW_SEEN, NOW_LOCK, NOW_HOLD = {}, threading.Lock(), 30   # IP -> (когда видели, подключений); «смотрит», если видели за последние 30 с


def hls_port():
    return int(E4.get("hls_port") or (443 if E4.get("scheme") == "https" else 80))


VIEW_MIN = 8 * 1024   # Б/с в среднем за NOW_HOLD. Видео даже в SD — от 60 КБ/с; страница статуса HLS Proxy в браузере — около 1 КБ/с


def scan_tcp(port):
    """{(IP, порт клиента): сколько байт сервер уже отдал} по установленным соединениям — через ss (iproute2).
    None, если ss нет или он не сработал."""
    try:
        r = subprocess.run(["ss", "-Htin", "state", "established", f"( sport = :{port} )"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError): return None
    if r.returncode != 0: return None
    out = r.stdout
    res, cur = {}, None
    for l in out.splitlines():
        if not l[:1].isspace():   # строка соединения: Recv-Q Send-Q Локальный Удалённый
            f, cur = l.split(), None
            if len(f) < 4: continue
            host, _, rp = f[3].rpartition(":")
            try: a = ipaddress.ip_address(host.strip("[]").split("%")[0])
            except ValueError: continue
            a = getattr(a, "ipv4_mapped", None) or a
            if a.is_loopback: continue
            cur = (str(a), rp); res[cur] = 0
        elif cur:   # строка подробностей (-i) под соединением
            m = re.search(r"\bbytes_acked:(\d+)", l)
            if m: res[cur] = int(m.group(1))
    return res


def watchers(cur, last, hist, t, first=False):
    """Кто реально получает поток: IP -> подключений. Зритель — тот, кому за NOW_HOLD секунд отдали
    в среднем не меньше VIEW_MIN байт/с. Открытая страница статуса HLS Proxy или редактора так не считается."""
    ips, gain = {}, {}
    for (ip, rp), b in cur.items():
        ips[ip] = ips.get(ip, 0) + 1
        if not first: gain[ip] = gain.get(ip, 0) + max(0, b - last.get((ip, rp), 0))
    for ip, g in gain.items(): hist.setdefault(ip, []).append((t, g))
    for ip in list(hist):
        hist[ip] = [x for x in hist[ip] if t - x[0] <= NOW_HOLD]
        if not hist[ip]: del hist[ip]
    return {ip: n for ip, n in ips.items() if sum(g for _, g in hist.get(ip, [])) >= VIEW_MIN * NOW_HOLD}


def now_sampler():
    """Опрашиваем соединения с HLS Proxy раз в 2 секунды и помним зрителя ещё 30 секунд.
    Без ss — по-старому: зритель любой, у кого есть соединение (тогда считается и открытая страница статуса)."""
    last, hist, first = {}, {}, True
    while True:
        try:
            port, t = hls_port(), time.time()
            cur = scan_tcp(port)
            if cur is None: ips = scan_conns(port)
            else: ips = watchers(cur, last, hist, t, first); last, first = cur, False
            with NOW_LOCK:
                for ip, n in ips.items(): NOW_SEEN[ip] = (t, n)
                for ip in [k for k, v in NOW_SEEN.items() if t - v[0] > NOW_HOLD]: del NOW_SEEN[ip]
        except Exception: pass
        time.sleep(2)


def now_watching():
    """Режим hls: кто сейчас смотрит через HLS Proxy (IP -> число подключений). Имени клиента HLS Proxy не сообщает — только IP."""
    if E4["mode"] != "hls": return {"mode": "direct"}
    t = time.time()
    with NOW_LOCK: ips = {ip: max(v[1], 1) for ip, v in NOW_SEEN.items() if t - v[0] <= NOW_HOLD}
    return {"mode": "hls", "port": hls_port(), "ips": ips, "at": time.strftime("%H:%M:%S")}


def scan_conns(port):
    ips = {}
    for fn, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):   # без внешних программ и без root
        try: lines = open(fn).read().splitlines()[1:]
        except OSError: continue
        for l in lines:
            f = l.split()
            if len(f) < 4 or f[3] != "01": continue   # 01 = ESTABLISHED
            la, lp = f[1].split(":"); ra, rp = f[2].split(":")
            if int(lp, 16) != port: continue
            try:
                if v6:
                    b = b"".join(bytes.fromhex(ra[k:k + 8])[::-1] for k in range(0, 32, 8)); a = ipaddress.ip_address(b)
                    a = a.ipv4_mapped or a
                else: a = ipaddress.ip_address(bytes.fromhex(ra)[::-1])
            except ValueError: continue
            if a.is_loopback: continue
            ips[str(a)] = ips.get(str(a), 0) + 1
    return ips


def he_hide(items, unhide=False):
    """Скрыть записи «Не открылось у клиентов». Запись помнит время последней ошибки: новая ошибка канала — запись снова видна."""
    h = read_json(HE_HIDDEN, {}) or {}
    if not isinstance(h, dict): h = {}
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, dict): continue
        n, ts = str(x.get("n") or "")[:200], str(x.get("ts") or "")[:40]
        if not n: continue
        if unhide: h.pop(n, None)
        elif ts: h.pop(n, None); h[n] = ts   # в конец: при переполнении уходят самые старые
    h = dict(list(h.items())[-500:])
    write_json_atomic(HE_HIDDEN, h)
    return h


# ---------- обновления: есть ли новая версия на GitHub, обновление по кнопке (делает root-служба e4tv-update) ----------
UPD_URL = E4.get("update_url") or "https://raw.githubusercontent.com/koksik2022/evgen4ik-tv/main/get.sh"
UTRIGGER = f"{DATA}/update.trigger"
VER_CACHE = {"t": 0.0, "latest": None, "err": None}


def vtuple(v):
    try: return tuple(int(x) for x in str(v).split("."))
    except ValueError: return (0,)


def version_info(force=False):
    age = time.time() - VER_CACHE["t"]
    if (force and age > 30) or age > 6 * 3600:   # GitHub спрашиваем раз в 6 часов; по кнопке «проверить» — не чаще раза в 30 секунд
        try:
            rq = urllib.request.Request(UPD_URL, headers={"User-Agent": "Evgen4ik-TV"})
            txt = urllib.request.urlopen(rq, timeout=10).read(20000).decode("utf-8", "replace")
            m = re.search(r'local VER="([0-9][0-9.]{0,15})"', txt)
            VER_CACHE.update(latest=m.group(1) if m else None, err=None if m else "не нашёл версию в get.sh")
        except Exception as e:
            VER_CACHE["err"] = str(getattr(e, "reason", e))[:150]
        VER_CACHE["t"] = time.time()
    try:
        out = subprocess.run(["systemctl", "show", "e4tv-update.service", "-p", "ActiveState", "-p", "Result", "-p", "ExecMainExitTimestamp"],
                             capture_output=True, text=True, timeout=5).stdout
        u = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    except Exception:
        u = {}
    cur = E4.get("version") or ""
    return {"current": cur, "latest": VER_CACHE["latest"], "error": VER_CACHE["err"],
            "newer": bool(VER_CACHE["latest"]) and vtuple(VER_CACHE["latest"]) > vtuple(cur),
            "updating": u.get("ActiveState") in ("activating", "active"), "last_result": u.get("Result") or "",
            "last_at": u.get("ExecMainExitTimestamp") or "", "can_update": os.path.exists(UTRIGGER)}


# ---------- Xtream Codes: вход в плеере по серверу, логину и паролю (TiviMate, IPTV Smarters, OTT Navigator…) ----------
XC_PATHS = ("/player_api.php", "/get.php", "/xmltv.php", "/panel_api.php", "/streaming/timeshift.php")
XC_ACT_MEM, XC_ACT_LOCK, XC_ACT_T = None, threading.Lock(), [0.0]


def xc_note(user, ip, ua, chan=None):
    """Запомнить заход клиента через Xtream (и какой канал включил). Сохраняется в файл не чаще раза в 10 секунд."""
    global XC_ACT_MEM
    with XC_ACT_LOCK:
        if XC_ACT_MEM is None: XC_ACT_MEM = read_json(XC_ACT, {}) or {}
        a = XC_ACT_MEM.setdefault(user, {"days": {}})
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z"); now = now[:-2] + ":" + now[-2:]; day = now[:10]   # с часовым поясом сервера
        a.update({"last": now, "ip": ip, "ua": ua})
        a["days"][day] = a["days"].get(day, 0) + (0 if chan else 1)
        if chan: a.update({"chan": chan[:100], "chan_at": now})
        lim = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
        a["days"] = {d: n for d, n in a["days"].items() if d >= lim}
        if time.time() - XC_ACT_T[0] > 10:
            XC_ACT_T[0] = time.time()
            try: write_json_atomic(XC_ACT, XC_ACT_MEM)
            except OSError: pass


def ip_ok(ip, nets):
    if not nets: return True
    try: a = ipaddress.ip_address(ip)
    except ValueError: return False
    for n in nets:
        try:
            if a in ipaddress.ip_network(n, strict=False): return True
        except ValueError: pass
    return False


def xc_user(q, ip):
    """(клиент, причина отказа). Клиент — запись из xc.json."""
    u, pw = (q.get("username") or [""])[0].lower()[:32], (q.get("password") or [""])[0][:64]
    c = (read_json(XC_FILE, {}) or {}).get(u)
    if not c or not hmac.compare_digest(str(c.get("pw", "")), pw): return None, "auth"
    if not ip_ok(ip, c.get("ips")): return None, "ip"
    return dict(c, user=u), None


# ---------- личная страница клиента /my/<токен>: срок, ссылка, QR, EPG, Xtream, как подключить ----------
MY_CSS = """:root{color-scheme:dark}body{margin:0;background:#09090b;color:#fafafa;font:15px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif}
main{max-width:640px;margin:0 auto;padding:22px 16px 40px}.logo{display:flex;align-items:center;gap:10px;font-weight:800;font-size:18px}
.logo i{font-style:normal;width:32px;height:32px;border-radius:9px;background:#f59e0b;color:#18181b;display:grid;place-items:center;font-size:13px}
h1{font-size:24px;margin:18px 0 6px}h2{font-size:16px;margin:22px 0 8px}.b{display:inline-block;border-radius:8px;padding:4px 10px;font-weight:600;font-size:14px}
.ok{background:rgba(74,222,128,.13);color:#4ade80}.warn{background:rgba(245,158,11,.15);color:#f59e0b}.bad{background:rgba(248,113,113,.14);color:#f87171}
.card{background:#18181b;border:1px solid rgba(255,255,255,.09);border-radius:12px;padding:16px;margin-top:14px}.r{margin:10px 0}
.r>span{display:block;color:#a1a1aa;font-size:13px;margin-bottom:4px}.v{display:flex;gap:8px}
code{flex:1;min-width:0;background:#0c0c0f;border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:9px 10px;font:13px Consolas,monospace;overflow-wrap:anywhere}
button{font:inherit;font-weight:600;border:1px solid rgba(255,255,255,.14);background:#1f1f23;color:#fafafa;border-radius:8px;padding:8px 12px;cursor:pointer}
#qr{width:200px;height:200px;background:#fff;border-radius:10px;padding:8px;margin:6px auto 0;box-sizing:border-box}#qr svg{width:100%;height:100%}
.m{color:#a1a1aa;font-size:14px}ol{padding-left:20px;margin:6px 0}li{margin:4px 0}"""

MY_JS = """document.addEventListener("click",e=>{const b=e.target.closest("[data-c]");if(!b)return;const t=b.dataset.c,ok=()=>{b.textContent="Скопировано";setTimeout(()=>b.textContent="Копировать",1500)};
if(navigator.clipboard&&isSecureContext)navigator.clipboard.writeText(t).then(ok);else{const a=document.createElement("textarea");a.value=t;document.body.appendChild(a);a.select();try{document.execCommand("copy");ok()}catch(x){}a.remove()}});
const q=document.getElementById("qr");if(q){const s=document.createElement("script");s.src="https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js";
s.onload=()=>{const g=qrcode(0,"M");g.addData(q.dataset.t);g.make();q.innerHTML=g.createSvgTag({cellSize:4,margin:2,scalable:true})};s.onerror=()=>q.remove();document.head.appendChild(s)}"""


def my_page(tok):
    """HTML личной страницы клиента или None, если такого токена нет. Вход только по длинной ссылке (токену)."""
    import html as H
    if not TOK_RX.match(tok or ""): return None
    rules = read_json(RULES, {}) or {}
    c = next((x for x in rules.get("clients") or [] if isinstance(x, dict) and x.get("token") == tok), None)
    if not c: return None
    e = lambda v: H.escape(str(v or ""), quote=True)
    st, exp = xc_status(c), c.get("expires") or ""
    ru = ".".join(reversed(exp.split("-"))) if exp else ""
    if st == "Disabled": badge, note = "bad", "Доступ выключен. Напишите тому, кто дал ссылку."
    elif st == "Expired": badge, note = "bad", f"Доступ закончился {ru}. Чтобы продлить, напишите тому, кто дал ссылку."
    elif exp:
        left = int((time.mktime(time.strptime(exp, "%Y-%m-%d")) - time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))) // 86400)
        badge = "warn" if left <= 7 else "ok"
        note = f"Работает до {ru}" + (" — сегодня последний день" if left == 0 else f", осталось дней: {left}" if left <= 7 else "")
    else: badge, note = "ok", "Работает, без срока"
    slug = c.get("slug") if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,31}", str(c.get("slug") or "")) else ""
    link = PUBLIC_BASE + (slug or tok) + ".m3u8"
    epg = E4.get("player_epg") or E4.get("epg_url") or ""
    sp = urllib.parse.urlsplit(PUBLIC_BASE); srv = f"{sp.scheme}://{sp.netloc}" if sp.netloc else ""
    lim = c.get("streams") if E4["mode"] == "hls" else None

    def row(t, v):
        return f'<div class="r"><span>{t}</span><div class="v"><code>{e(v)}</code><button data-c="{e(v)}">Копировать</button></div></div>'
    parts = ['<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta name="robots" content="noindex,nofollow"><title>Ваш доступ к ТВ</title><style>', MY_CSS, '</style></head><body><main>',
             '<div class="logo"><i>E4</i>Evgen4ik TV</div>', f'<h1>{e(c.get("name"))}</h1><span class="b {badge}">{e(note)}</span>']
    if st == "Active":
        parts += ['<div class="card"><h2 style="margin-top:0">Ссылка для плеера</h2>', row("Плейлист (M3U)", link)]
        if epg: parts.append(row("Программа передач (EPG)", epg))
        parts.append(f'<div id="qr" data-t="{e(link)}" title="Наведите камеру телефона"></div><p class="m" style="text-align:center">QR-код ссылки на плейлист</p>')
        if lim: parts.append(f'<p class="m">Одновременно работает каналов: {int(lim)}.</p>')
        parts += ['</div><div class="card"><h2 style="margin-top:0">Как подключить</h2><ol>',
                  '<li>Установите IPTV-плеер: на Android и Android TV — <b>Televizo</b> или <b>OTT Navigator</b>; на других устройствах — любой плеер, который открывает M3U-плейлист по ссылке.</li>',
                  '<li>Добавьте плейлист по ссылке: вставьте ссылку выше или наведите камеру телефона на QR-код.</li>',
                  '<li>Если плеер просит телепрограмму (EPG) — вставьте ссылку «Программа передач».</li>',
                  '<li>Каналы не появились — обновите плейлист в плеере или напишите тому, кто дал ссылку.</li></ol>']
        if c.get("xc_user") and c.get("xc_pw"):
            parts += ['<h2>Вход через Xtream Codes</h2><p class="m">Для TiviMate, IPTV Smarters и похожих: в плеере выберите «Xtream Codes» и введите:</p>',
                      row("Сервер", srv), row("Логин", c.get("xc_user")), row("Пароль", c.get("xc_pw"))]
        parts.append('</div>')
    parts += ['<p class="m" style="margin-top:18px">Ссылка личная — не пересылайте её. Эта страница открывается только по вашей ссылке.</p></main><script>',
              MY_JS, '</script></body></html>']
    return "".join(parts)


def xc_status(c):
    if not c.get("enabled", True): return "Disabled"
    e = c.get("expires")
    if e and time.strftime("%Y-%m-%d") > e: return "Expired"
    return "Active"


def _ts(d):
    try: return str(int(time.mktime(time.strptime(d, "%Y-%m-%d"))) + 86399)
    except Exception: return None


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

    def _xc(self, path):
        ip = self._ip(); ua = (self.headers.get("User-Agent") or "")[:200]
        if locked_for(ip): return self._send(429, {"user_info": {"auth": 0}})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        stream = path.startswith(("/live/", "/timeshift/")) or path == "/streaming/timeshift.php"   # ответ — переадресация на поток
        ts_start = ts_dur = None   # архив: время начала (секунды UTC) и длительность (секунды, если плеер её дал)
        if path.startswith("/live/"):   # /live/<логин>/<пароль>/<номер>.<ts|m3u8> -> ссылка на поток
            m = re.fullmatch(r"/live/([^/]{1,32})/([^/]{1,64})/(\d{1,6})(?:\.(?:ts|m3u8|m3u))?", path)
            if not m: return self._send(404, "not found", "text/plain")
            utc = (q.get("utc") or [""])[0]   # M3U из get.php: плеер просит архив как ?utc=<начало>&lutc=<сейчас>
            if utc.isdigit() and 0 < time.time() - int(utc) < 60 * 86400: ts_start = int(utc)
            q = {"username": [urllib.parse.unquote(m.group(1))], "password": [urllib.parse.unquote(m.group(2))]}; sid = m.group(3)
        elif stream:   # архив Xtream: /timeshift/<логин>/<пароль>/<минут>/<ГГГГ-ММ-ДД:ЧЧ-ММ>/<номер>.ts или /streaming/timeshift.php?…
            m = re.fullmatch(r"/timeshift/([^/]{1,32})/([^/]{1,64})/(\d{1,5})/(\d{4}-\d{2}-\d{2}:\d{2}-\d{2})/(\d{1,6})(?:\.(?:ts|m3u8|m3u))?", path)
            if m: q, start, sid, mins = {"username": [urllib.parse.unquote(m.group(1))], "password": [urllib.parse.unquote(m.group(2))]}, m.group(4), m.group(5), m.group(3)
            else: start, sid, mins = (q.get("start") or [""])[0], (q.get("stream") or [""])[0].split(".")[0], (q.get("duration") or [""])[0]
            try: ts_start = int(time.mktime(time.strptime(start, "%Y-%m-%d:%H-%M")))   # время — в поясе сервера (server_info.timezone)
            except (ValueError, OverflowError): return self._send(404, "not found", "text/plain")
            if not sid.isdigit() or not 0 < time.time() - ts_start < 60 * 86400: return self._send(404, "not found", "text/plain")
            if str(mins).isdigit() and 0 < int(mins) <= 1440: ts_dur = int(mins) * 60
        c, why = xc_user(q, ip)
        if not c:
            if why == "auth": add_fail(ip)
            return self._send(403 if stream or why == "ip" else 200,
                              {"user_info": {"auth": 0, "status": "Forbidden" if why == "ip" else ""}} if not stream else "forbidden",
                              "application/json; charset=utf-8" if not stream else "text/plain")
        st = xc_status(c); chans = c.get("channels") if st == "Active" else []
        host = self.headers.get("Host") or E4.get("host") or "localhost"
        proto = "https" if E4.get("scheme") == "https" else "http"
        base = f"{proto}://{host}"
        if stream:
            i = int(sid) - 1
            if st != "Active" or not (0 <= i < len(chans)): return self._send(404, "not found", "text/plain")
            xc_note(c["user"], ip, ua, chans[i]["name"])
            if ts_start is not None and path.startswith("/live/") and "cu" not in chans[i]: ts_start = None   # как раньше: ?utc&lutc уйдут дальше как есть
            if ts_start is not None:   # архив: как плееры с M3U — ?utc=<начало>&lutc=<сейчас> (HLS Proxy и «перенаправление» передают дальше)
                if not chans[i].get("arch"): return self._send(404, "no archive", "text/plain")
                u = chans[i]["url"]
                if "cu" in chans[i]:   # ссылка прямо на провайдера — сразу в его формате архива (flussonic, свой catchup-source…)
                    return self._redirect(catchup.archive_url(u, chans[i]["cu"], ts_start, int(time.time()), ts_dur))
                return self._redirect(u + ("&" if "?" in u else "?") + f"utc={ts_start}&lutc={int(time.time())}")
            u = chans[i]["url"]
            if path.endswith(".ts") and "/index.m3u8" in u:   # HLS Proxy отдаёт HLS, а плеер Xtream просил .ts — просим у него MPEG-TS
                u += ("&" if "?" in u else "?") + "output=ts"
            return self._redirect(add_query(u, self.path))
        if path == "/xmltv.php":
            es = (read_json(RULES, {}) or {}).get("epg_sources") or []
            return self._redirect((es[0] if E4["mode"] != "hls" and es else None) or E4.get("player_epg") or E4["epg_url"])
        gm, cats = {}, []
        for ch in chans:
            if ch["group"] not in gm: gm[ch["group"]] = str(len(gm) + 1); cats.append({"category_id": gm[ch["group"]], "category_name": ch["group"], "parent_id": 0})
        if path == "/get.php":   # плейлист M3U со ссылками /live/…
            xc_note(c["user"], ip, ua)
            ext = "m3u8" if (q.get("output") or ["m3u8"])[0] in ("m3u8", "hls") else "ts"
            u, pw = urllib.parse.quote(c["user"]), urllib.parse.quote(c["pw"])
            out = ["#EXTM3U"]
            for i, ch in enumerate(chans, 1):
                a = (f' tvg-id="{ch["tvg_id"]}"' if ch.get("tvg_id") else "") + (f' tvg-logo="{ch["logo"]}"' if ch.get("logo") else "")
                if ch.get("arch"):   # архив: плеер допишет ?utc=…&lutc=… к /live/…, редактор передаст дальше
                    a += f' tvg-rec="{int(ch["arch"])}" catchup="append" catchup-days="{int(ch["arch"])}" catchup-source="?utc={{utc}}&lutc={{lutc}}"'
                out += [f'#EXTINF:-1{a} group-title="{ch["group"]}",{ch["name"]}', f"{base}/live/{u}/{pw}/{i}.{ext}"]
            return self._send(200, "\n".join(out) + "\n", "audio/x-mpegurl; charset=utf-8")
        act = (q.get("action") or [""])[0]
        if not act:
            xc_note(c["user"], ip, ua)
            port = (host.rsplit(":", 1)[1] if ":" in host.split("]")[-1] else ("443" if proto == "https" else "80"))
            return self._send(200, {"user_info": {"username": c["user"], "password": c["pw"], "message": "", "auth": 1, "status": st,
                                                  "exp_date": _ts(c.get("expires")) if c.get("expires") else None, "is_trial": "0", "active_cons": "0",
                                                  "created_at": _ts(c.get("created") or "") or "0", "max_connections": str(c.get("streams") or 0),
                                                  "allowed_output_formats": ["m3u8", "ts"]},
                                    "server_info": {"url": host.rsplit(":", 1)[0] if ":" in host.split("]")[-1] else host, "port": port,
                                                    "https_port": "443", "server_protocol": proto, "rtmp_port": "0",
                                                    "timezone": local_tz(), "timestamp_now": int(time.time()),
                                                    "time_now": time.strftime("%Y-%m-%d %H:%M:%S")}})
        if act == "get_live_categories": return self._send(200, cats)
        if act == "get_live_streams":
            want = (q.get("category_id") or [""])[0]
            return self._send(200, [{"num": i, "name": ch["name"], "stream_type": "live", "stream_id": i, "stream_icon": ch.get("logo") or "",
                                     "epg_channel_id": ch.get("tvg_id") or None, "added": "0", "is_adult": "0", "category_id": gm[ch["group"]],
                                     "category_ids": [int(gm[ch["group"]])], "custom_sid": "", "tv_archive": 1 if ch.get("arch") else 0, "direct_source": "",
                                     "tv_archive_duration": int(ch.get("arch") or 0)}
                                    for i, ch in enumerate(chans, 1) if not want or gm[ch["group"]] == want])
        if act in ("get_short_epg", "get_simple_data_table"): return self._send(200, {"epg_listings": []})
        return self._send(200, [])   # фильмы и сериалы не поддерживаются

    def _link(self, path):
        m = re.fullmatch(r"/live/_r/([A-Za-z0-9_-]{3,64})/([0-9a-f]{16})", path)
        if m:   # перенаправление: код должен быть в карте именно этого плейлиста (выключенный клиент — ссылки перестают работать)
            d = links(); u = (d.get("u") or {}).get(m.group(2))
            if not u or m.group(2) not in d.get("t", {}).get(m.group(1), ()): return self._send(404, "not found", "text/plain")
            sp = (d.get("c") or {}).get(m.group(2))
            if isinstance(sp, dict):   # архив провайдера не «?utc&lutc» (flussonic и т. п.): переводим запрос плеера в его формат
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                utc, lutc = (q.get("utc") or [""])[0], (q.get("lutc") or [""])[0]
                if utc.isdigit() and 0 < time.time() - int(utc) < 60 * 86400:
                    return self._redirect(catchup.archive_url(u, sp, int(utc), int(lutc) if lutc.isdigit() else int(time.time())))
            return self._redirect(add_query(u, self.path))   # архив: плеер дописывает ?utc=…&lutc=… — передаём провайдеру
        m = re.fullmatch(r"/live/_logo/([0-9a-f]{16})", path)
        r = logo_get(m.group(1)) if m else ((BLANK_PNG, "image/png") if path == "/live/_logo/none" else None)   # «Без логотипа» — пустая картинка
        if not r:   # сайт картинки не отдал её серверу (бывает 403) — пусть плеер возьмёт сам, лишь бы логотип был
            u = (links().get("l") or {}).get(m.group(1)) if m else None
            if isinstance(u, str) and re.match(r"^https?://[^\s\"<>]{1,2000}$", u): return self._redirect(u)
            return self._send(404, "not found", "text/plain")
        b, t = r
        self.send_response(200)
        for k, v in (("Content-Type", t), ("Content-Length", str(len(b))), ("Cache-Control", "public, max-age=86400"),
                     ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "default-src 'none'")):
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

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
        if path.startswith(("/live/_r/", "/live/_logo/")):   # «Ссылки на каналы»: перенаправление / логотип (без входа, как Xtream)
            return self._link(path)
        if path.startswith("/my/"):   # личная страница клиента — без входа, по его токену (длинной ссылке)
            pg = my_page(path[4:].strip("/"))
            if pg is None: return self._send(404, "Страница не найдена", "text/plain; charset=utf-8")
            return self._send(200, pg, "text/html; charset=utf-8", {"X-Robots-Tag": "noindex, nofollow"})
        if path in XC_PATHS or path.startswith(("/live/", "/timeshift/")):   # Xtream Codes — без входа в редактор, по логину клиента
            return self._xc(path)
        if not self._session():
            return self._redirect("/login") if not path.startswith("/api/") else self._send(401, {"error": "нужен вход"})
        if path in ("/", "/index.html"):
            with open(f"{HERE}/index.html", "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/state":
            return self._send(200, {"state": state_with_logos(), "rules": read_json(RULES, {}), "status": prep_status(),
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
        if path == "/api/version":
            return self._send(200, version_info("force=1" in self.path))
        if self.path == "/api/now":
            return self._send(200, now_watching())
        if self.path == "/api/status":
            return self._send(200, prep_status())
        if self.path == "/api/tg":
            return self._send(200, tg_info())
        if self.path == "/api/domain-status":
            return self._send(200, read_json(DOMAIN_ST, {}) or {})
        if self.path == "/api/autocheck":
            return self._send(200, autocheck_info())
        if path.startswith("/api/provicon/") and re.fullmatch(r"[a-z0-9]{1,16}", path[14:]):   # значок провайдера (картинка)
            r = prov_icon(path[14:])
            if not r: return self._send(404, {"error": "нет значка"})
            b, t = r
            self.send_response(200)
            for k, v in (("Content-Type", t), ("Content-Length", str(len(b))), ("Cache-Control", "private, max-age=86400"),
                         ("X-Content-Type-Options", "nosniff"), ("Content-Security-Policy", "default-src 'none'"), ("Referrer-Policy", "no-referrer")):
                self.send_header(k, v)
            self.end_headers(); self.wfile.write(b); return
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
        if self.path == "/api/update":   # обновление по кнопке: служба e4tv-update (root) скачает get.sh и обновит
            if not os.path.exists(UTRIGGER): return self._send(400, {"error": "обновление по кнопке не настроено — обновите командой с сервера"})
            with open(UTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True})
        if self.path == "/api/check":
            return self._send(200, check_url(body.get("url")))
        if self.path == "/api/rebuild":   # кнопка «Пересобрать» в боковой панели: сборка без сохранения правил
            with open(TRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True})
        if self.path in ("/api/domain-check", "/api/domain-set"):
            try: return self._send(200, (domain_check if self.path == "/api/domain-check" else domain_set)(body if isinstance(body, dict) else {}))
            except ValueError as e: return self._send(400, {"error": str(e)})
        if self.path in ("/api/tg", "/api/tg-chats", "/api/tg-test", "/api/tg-backup", "/api/restore-file"):
            try:
                return self._send(200, {"/api/tg": tg_save, "/api/tg-chats": tg_chats, "/api/tg-test": tg_test, "/api/tg-backup": tg_backup_now,
                                        "/api/restore-file": restore_file}[self.path](body if isinstance(body, dict) else {}))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/api/autocheck-conf":
            try:
                return self._send(200, save_ac_conf(body))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/api/epg-fetch":   # «Обновить телегид сейчас»: флаг + пересборка листов; lists.py (root) попросит HLS Proxy скачать телегид
            if E4["mode"] != "hls": return self._send(400, {"error": "без HLS Proxy телегид плеер берёт сам по ссылке"})
            with open(EPG_FLAG, "w") as f: f.write(str(time.time()))
            with open(LTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True})
        if self.path == "/api/he-hide":   # «Не открылось у клиентов»: скрыть / вернуть записи (до следующей ошибки канала)
            return self._send(200, {"hidden": he_hide(body.get("items"), bool(body.get("unhide")))})
        if self.path == "/api/autocheck-run":   # служба e4tv-check-now (root) проверит потоки
            if not os.path.exists(CTRIGGER): return self._send(400, {"error": "автопроверка не настроена на сервере — обновите Evgen4ik TV"})
            if ac_progress().get("running"): return self._send(409, {"error": "проверка уже идёт"})
            with open(AC_CTL, "w"): pass   # прежняя «стоп» не должна остановить новую проверку
            with open(CTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True})
        if self.path == "/api/autocheck-ctl":
            try:
                return self._send(200, ac_ctl(body.get("cmd") if isinstance(body, dict) else None))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
        if self.path == "/api/rules":
            try:
                rules = validate(body.get("rules"))
            except Exception as e:
                return self._send(400, {"error": str(e)})
            old = read_json(RULES, {}) or {}
            backup_rules()
            write_json_atomic(RULES, rules)
            clean_uploads(rules)
            if body.get("apply") or bool(old.get("open_logos")) != rules["open_logos"]:   # полная пересборка (логотипы ставит сборка); листы пересоберутся после неё сами
                with open(TRIGGER, "w") as f: f.write(str(time.time()))
            elif old.get("lists") != rules["lists"] or any(old.get(k) != rules[k] for k in ("link_mode", "logo_proxy", "fallback", "stream_format", "epg_sources", "epg_days")) or old.get("lists_only_sources") != rules["lists_only_sources"] or old.get("free_sources") != rules["free_sources"] or (old.get("free_edits") or {}) != rules["free_edits"] or (old.get("clients") or []) != rules["clients"]:
                with open(LTRIGGER, "w") as f: f.write(str(time.time()))
            return self._send(200, {"ok": True, "applied": bool(body.get("apply")), "rules": rules})
        return self._send(404, {"error": "not found"})


PUBLIC_CONF = {"version": E4.get("version") or "", "mode": E4["mode"], "domain": E4.get("domain") or "", "host": E4.get("host") or "", "login": LOGIN_ON,
               "main_url": (PUBLIC_BASE + E4["main_token"] + ".m3u8") if PUBLIC_BASE and E4.get("main_token") else "",
               "epg_url": E4.get("player_epg") or E4["epg_url"], "hls_url": E4.get("hls_url") or ""}

if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    if E4["mode"] == "hls": threading.Thread(target=now_sampler, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
