#!/usr/bin/env python3
"""Evgen4ik TV: плейлисты для плееров — основной и свои подборки каналов со своими ссылками.

Режим hls: берёт клиентский плейлист HLS Proxy (ссылки через рестрим, токены провайдеров не видны).
Режим direct: берёт собранный combined.m3u (ссылки провайдеров напрямую).
Оставляет каналы, выбранные в плейлисте (rules.json -> lists), и пишет <pl_dir>/<token>.m3u8; nginx отдаёт их по /pl/.
Запуск: после пересборки (e4tv-build) и при сохранении плейлистов (e4tv-lists.path), а также раз в час.
"""
import json, os, re, ssl, sys, time, urllib.request, urllib.parse, glob, grp, hashlib, hmac, base64, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4

RULES = E4["editor_dir"] + "/rules.json"
FINAL = E4["editor_dir"] + "/final.json"
STATUS = E4["editor_dir"] + "/lists-status.json"
OUTDIR = E4["pl_dir"]
CONF = E4["hls_dir"] + "/local.json"
MAIN = E4["main_token"] if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", str(E4.get("main_token") or "")) else None
TOK = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
NM_RX = re.compile(r" \[~[a-z0-9]{1,16}\]$")   # метка «не склеивать» (build.py NM_MARK) в конце имени канала
EX_RX = re.compile(r" \[=([0-9a-f]{10})\]$")   # метка своего канала «через сервер» (extra.m3u -> build.py -> HLS Proxy)
MARK_RX = re.compile(r" \[(?:~[a-z0-9]{1,16}|=[0-9a-f]{10})\]$")


def unmark(lines):
    """Убрать служебные метки («не склеивать», «свой канал через сервер») из названия канала: клиент видит обычное имя."""
    if lines and lines[0].startswith("#EXTINF") and MARK_RX.search(lines[0].rstrip()):
        lines = list(lines); lines[0] = MARK_RX.sub("", lines[0].rstrip())
    return lines


QUAL_RX = re.compile(r"\s+(?:hd|fhd|uhd|4k)$", re.I)   # HLS Proxy отрезает это слово в конце названия


def with_quality_aliases(fmap):
    """HLS Proxy убирает из названия последнее слово HD/FHD/UHD/4K («Кино HD» -> «Кино»), а final.json хранит полные имена.
    Добавляем короткие имена как запасные ключи, чтобы такие каналы не терялись (проверено на стенде 18.11.2026:
    462 из 462 расхождений — именно эти приписки). Точное имя всегда важнее: существующие ключи не трогаем."""
    out = dict(fmap)
    for k, v in fmap.items():
        a = QUAL_RX.sub("", k)
        if a != k and a and a not in out: out[a] = v
    return out


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
PASS = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
ADULT_GROUPS = {"18+"}
GT_RX = re.compile(r'group-title="([^"]*)"')

def _gt(lines):
    m = GT_RX.search(lines[0]) if lines else None
    return m.group(1) if m else ""

def regroup(seq, gf):
    """Каждая группа — одним куском, на месте большей части своих каналов (медиана позиций); 18+ — в самый конец.
    HLS Proxy склеивает тёзок («HUSTLER HD», «Hustler HD orig») и даёт каналу группу другой записи,
    поэтому без этого 18+ попадает кусками внутрь «Разное», а одиночный канал «Разное» — внутрь кинозала.
    Порядок каналов внутри группы не меняется."""
    pos = {}
    for i, x in enumerate(seq): pos.setdefault(gf(x), []).append(i)
    med = {g: p[(len(p) - 1) // 2] for g, p in pos.items()}
    return sorted(seq, key=lambda x: (gf(x) in ADULT_GROUPS or "18+" in gf(x), med[gf(x)]))


def ssha(pw):   # формат {SSHA} для nginx auth_basic
    salt = os.urandom(8)
    return "{SSHA}" + base64.b64encode(hashlib.sha1(pw.encode() + salt).digest() + salt).decode()


def expired(L):
    e = L.get("expires")
    try: return bool(e) and datetime.date.today() > datetime.date.fromisoformat(e)
    except ValueError: return False
INF = re.compile(r'#EXTINF:\s*-?\d+(?:\s+[\w-]+="[^"]*")*\s*,(.*)$')
WAIT = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0


def rj(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default


HPFX = "e4c_"   # логины клиентов в HLS Proxy (local.json -> clients), их ведёт только lists.py
NO_LIMIT = 999   # «без лимита» для HLS Proxy


def master(user=None, pw=None):
    """Плейлист для плееров. Режим hls — от HLS Proxy под логином user (по умолчанию — общий логин установщика)."""
    if E4["mode"] != "hls":   # без HLS Proxy: собранный плейлист как есть
        with open(E4["combined"], encoding="utf-8", errors="replace") as f: return f.read()
    c = json.load(open(CONF, encoding="utf-8"))
    S = c["SERVER"]
    if user: names, pw0 = [user], pw
    else:
        key = next(k for k in c["clients"] if not k.startswith(HPFX)); cl = c["clients"][key]
        names, pw0 = [key, cl.get("name")], cl["password"]
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    last = None
    for user in dict.fromkeys(names):
        if not user: continue
        url = f"{S.get('protocol') or 'https'}://127.0.0.1:{S.get('port', 443)}/m3u8?username={urllib.parse.quote(str(user))}&password={urllib.parse.quote(pw0)}"
        try:
            rq = urllib.request.Request(url, headers={"Host": S.get("address", "localhost"), "User-Agent": "m3u-lists"})
            with urllib.request.urlopen(rq, context=ctx, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
    raise RuntimeError(f"HLS Proxy не отдал плейлист: {last}")


def parse(text):
    head, items, cur = "#EXTM3U", [], None
    for l in text.replace("\r", "").split("\n"):
        if l.startswith("#EXTM3U"): head = l; continue
        if l.startswith("#EXTINF"):
            m = INF.match(l); cur = {"name": (m.group(1).strip() if m else ""), "lines": [l]}
        elif not l.strip(): continue
        elif cur is None: continue
        elif l.startswith("#"): cur["lines"].append(l)
        else: cur["lines"].append(l); items.append(cur); cur = None
    return head, items


FREE_DIR = E4["free_dir"]   # кэш отдельных (бесплатных) плейлистов
FREE_TTL = 6 * 3600


# ---------- готовые подборки, которые сервер собирает сам (url вида e4://имя) ----------
RAW = "https://raw.githubusercontent.com/iptv-org/iptv/gh-pages/"
PRESETS = {
    "ru-music": [("ru", RAW + "countries/ru.m3u"),
                 ("freetv", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_russia.m3u8"),
                 ("rus", RAW + "languages/rus.m3u"),
                 ("music", RAW + "categories/music.m3u")],
}
CAT = {"general": "Общие", "movies": "Кино", "series": "Сериалы", "music": "Музыка", "kids": "Детские", "animation": "Детские",
       "family": "Детские", "sports": "Спорт", "news": "Новости", "business": "Новости", "legislative": "Новости",
       "documentary": "Познавательные", "science": "Познавательные", "education": "Познавательные", "culture": "Познавательные",
       "travel": "Познавательные", "outdoor": "Познавательные", "entertainment": "Развлекательные", "comedy": "Развлекательные",
       "lifestyle": "Развлекательные", "cooking": "Развлекательные", "auto": "Развлекательные", "relax": "Развлекательные",
       "classic": "Развлекательные", "religious": "Религия", "shop": "Магазины"}
RU_ORDER = ["Эфирные", "Общие", "Новости", "Кино", "Сериалы", "Спорт", "Детские", "Познавательные", "Развлекательные", "Музыка",
            "Религия", "Магазины", "Разное"]
CIS = {"ru", "by", "kz", "ua", "kg", "uz", "md", "am", "az", "ge", "tj", "tm"}
REGION = {}
for _r, _cs in {"США и Канада": "us ca", "Европа": "uk gb ie fr de at ch it es pt nl be lu pl cz sk hu ro bg gr se no dk fi ee lv lt "
                "rs hr si ba mk me al xk cy mt is ad mc sm li", "Латинская Америка": "mx br ar cl co pe ve ec bo py uy cr pa gt hn sv ni do cu pr jm tt ht",
                "Азия": "in pk bd lk np cn tw hk jp kr kp vn th my sg id ph kh la mm mn af ir iq il ps jo lb sy sa ae qa kw bh om ye tr",
                "Африка": "eg ma dz tn ly ng gh ke tz ug et za sn ci cm cd ao zm zw mz mg"}.items():
    for _c in _cs.split(): REGION[_c] = _r
MUS_ORDER = ["США и Канада", "Европа", "Латинская Америка", "Азия", "Африка", "Другие страны"]
TAG = re.compile(r"\s*(\(\d{3,4}[pi]\)|\[[^\]]*\]|[\u24b6-\u24e9])")   # (1080p), [Not 24/7], значки Free-TV Ⓖ Ⓥ


def _items(text):
    out, cur = [], None
    for l in text.replace("\r", "").split("\n"):
        if l.startswith("#EXTINF"):
            m = re.match(r'#EXTINF:\s*-?\d+((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$', l)   # в кавычках бывают запятые
            if not m: cur = None; continue
            cur = {"name": m.group(2).strip(), "a": dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(1))), "x": []}
        elif l.startswith("#EXTVLCOPT") and cur: cur["x"].append(l.strip())
        elif l.startswith("#"): continue
        elif l.strip() and cur: cur["url"] = l.strip(); out.append(cur); cur = None
    return out


def _alive(it):
    """Поток отвечает? Смотрим первые килобайты: плейлист HLS, DASH или видео."""
    h = {"User-Agent": it["a"].get("http-user-agent") or "VLC/3.0.20 LibVLC/3.0.20"}
    if it["a"].get("http-referrer"): h["Referer"] = it["a"]["http-referrer"]
    for x in it["x"]:
        k, _, v = x.partition("=")
        if k.endswith("http-user-agent"): h["User-Agent"] = v
        elif k.endswith("http-referrer"): h["Referer"] = v
    try:
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(urllib.request.Request(it["url"], headers=h), timeout=8, context=ctx) as r:
            b = r.read(4096); ct = (r.headers.get("Content-Type") or "").lower()
        return r.status < 400 and (b"#EXTM3U" in b or b"<MPD" in b or b[:1] == b"\x47" or "video" in ct or "mpegurl" in ct or "dash" in ct)
    except Exception:
        return False


def build_preset(name):
    """Собрать подборку: каналы России (без дублей, по русским группам) + зарубежная музыка по регионам.
    В подборку попадают только ссылки, которые сейчас отвечают. Возвращает (текст M3U, статистика) или (None, ошибка)."""
    from concurrent.futures import ThreadPoolExecutor
    cand = []
    for part, url in PRESETS[name]:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Evgen4ik-TV"}), timeout=60) as r:
                items = _items(r.read(30 * 1024 * 1024).decode("utf-8", "replace"))
        except Exception as e:
            return None, f"{part}: {e}"
        for it in items:
            m = re.search(r"\.([a-z]{2})(?:@|$)", it["a"].get("tvg-id", ""))
            cc = m.group(1) if m else (it["a"].get("tvg-country") or "").lower()[:2]
            if part == "music":
                if cc in CIS: continue   # русская музыка уже есть в каналах России
                grp = "Зарубежная музыка: " + REGION.get(cc, "Другие страны")
            else:
                if cc != "ru" and not (part == "freetv" and not cc): continue   # только Россия
                cat = (it["a"].get("group-title") or "").split(";")[0].strip().lower()
                grp = "РУ: " + ("Эфирные" if part == "freetv" else CAT.get(cat, "Разное"))
            nm = TAG.sub("", it["name"]).strip() or it["name"]
            cand.append({"name": nm, "grp": grp, "url": it["url"], "x": it["x"], "a": it["a"],
                         "logo": it["a"].get("tvg-logo", ""), "id": it["a"].get("tvg-id", "")})
    seen, uniq = set(), []
    for c in cand:   # одинаковые ссылки — один раз
        if c["url"] not in seen: seen.add(c["url"]); uniq.append(c)
    with ThreadPoolExecutor(48) as ex:
        ok = list(ex.map(_alive, uniq)) if os.environ.get("E4TV_NOCHECK") != "1" else [True] * len(uniq)   # NOCHECK — только для проверок
    names, out = set(), []
    for c, good in zip(uniq, ok):   # одинаковые названия — первый рабочий (приоритет: Россия, Free-TV, на русском, музыка)
        key = re.sub(r"\s+", " ", c["name"].lower().replace("ё", "е"))
        if good and key not in names: names.add(key); out.append(c)
    def gkey(g):
        if g.startswith("РУ: "): return (0, RU_ORDER.index(g[4:]) if g[4:] in RU_ORDER else 99)
        return (1, MUS_ORDER.index(g.split(": ", 1)[1]) if g.split(": ", 1)[1] in MUS_ORDER else 99)
    out.sort(key=lambda c: (gkey(c["grp"]), c["name"].casefold()))
    lines = ["#EXTM3U"]
    for c in out:
        a = (f' tvg-id="{c["id"]}"' if c["id"] else "") + (f' tvg-logo="{c["logo"]}"' if c["logo"] else "")
        a += "".join(f' {k}="{c["a"][k]}"' for k in ("http-user-agent", "http-referrer") if c["a"].get(k))   # нужны некоторым потокам
        lines += [f'#EXTINF:-1{a} group-title="{c["grp"]}",{c["name"]}'] + c["x"] + [c["url"]]
    return "\n".join(lines) + "\n", {"total": len(uniq), "alive": len(out), "at": time.strftime("%d.%m.%Y %H:%M")}


def preset_items(src):
    """Подборка e4://имя: пересобирается раз в 6 часов; если рабочих стало меньше половины — остаётся прошлая."""
    name = src["url"][5:]
    if name not in PRESETS: return []
    os.makedirs(FREE_DIR, exist_ok=True); os.chmod(FREE_DIR, 0o700)
    cp, sp = f"{FREE_DIR}/preset-{name}.m3u", f"{FREE_DIR}/preset-{name}.json"
    old = open(cp, encoding="utf-8", errors="replace").read() if os.path.exists(cp) else ""
    if not old or time.time() - os.path.getmtime(cp) > FREE_TTL:
        new, stat = build_preset(name)
        if new and new.count("#EXTINF") >= 0.5 * old.count("#EXTINF"):
            with open(cp + ".tmp", "w", encoding="utf-8") as f: f.write(new)
            os.replace(cp + ".tmp", cp); old = new
        elif os.path.exists(cp): os.utime(cp)
        if isinstance(stat, dict):
            with open(sp, "w", encoding="utf-8") as f: json.dump(stat, f)
    # копия для редактора (он показывает каналы подборки при выборе)
    pub = os.path.join(os.path.dirname(RULES), f"preset-{name}.m3u")
    if old and (not os.path.exists(pub) or os.path.getmtime(pub) < os.path.getmtime(cp)):
        with open(pub + ".tmp", "w", encoding="utf-8") as f: f.write(old)
        try: os.chown(pub + ".tmp", 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
        except Exception: pass
        os.chmod(pub + ".tmp", 0o640); os.replace(pub + ".tmp", pub)
    _, items = parse(old) if old else ("", [])
    for it in items:
        gm = re.search(r'group-title="([^"]*)"', it["lines"][0])
        it["g"] = gm.group(1) if gm else "Без группы"
        it["k"] = f"f:{src['id']}:{it['name'].lower()}"
    return items


def preset_stat(name):
    return rj(f"{FREE_DIR}/preset-{name}.json", None)


URLOK = re.compile(r'^(https?|rtmp|rtsp|udp|rtp)://[^\s"]{1,2000}$', re.I)


def free_items(src):
    if str(src.get("url", "")).startswith("e4://"): return preset_items(src)
    """Отдельный плейлист (не входит в основной и в HLS Proxy): каналы берутся как есть, ссылки напрямую.
    Обновляется раз в 6 часов; если пришло меньше половины от прошлого — остаётся прошлая копия."""
    os.makedirs(FREE_DIR, exist_ok=True); os.chmod(FREE_DIR, 0o700)
    cp = f"{FREE_DIR}/{src['id']}.m3u"
    old = open(cp, encoding="utf-8", errors="replace").read() if os.path.exists(cp) else ""
    if not old or time.time() - os.path.getmtime(cp) > FREE_TTL:
        try:
            rq = urllib.request.Request(src["url"], headers={"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"})
            with urllib.request.urlopen(rq, timeout=60) as r: new = r.read(30 * 1024 * 1024).decode("utf-8", "replace")
            if "#EXTINF" in new and new.count("#EXTINF") >= 0.5 * old.count("#EXTINF"):
                with open(cp + ".tmp", "w", encoding="utf-8") as f: f.write(new)
                os.replace(cp + ".tmp", cp); old = new
            else: os.utime(cp) if os.path.exists(cp) else None
        except Exception:
            pass
    _, items = parse(old) if old else ("", [])
    for it in items:
        gm = re.search(r'group-title="([^"]*)"', it["lines"][0])
        it["g"] = gm.group(1) if gm else "Без группы"
        it["k"] = f"f:{src['id']}:{it['name'].lower()}"
    return items


def _hls_hash():
    """Та же сумма, что считает e4tv-build (combined.m3u + local.json): чтобы после нашего перезапуска он не перезапускал ещё раз."""
    h = hashlib.sha256()
    for p in (E4["hls_dir"] + "/combined.m3u", CONF):
        try: h.update(open(p, "rb").read())
        except OSError: pass
    return h.hexdigest()


# ---------- Телегид в HLS Proxy: источники, сколько дней хранить, «Обновить сейчас» ----------
EPG_FLAG = E4["editor_dir"] + "/epg-fetch.flag"
EPG_STATUS = E4["editor_dir"] + "/epg-status.json"


def _epg_status(upd):
    st = rj(EPG_STATUS, {}) or {}
    st.update(upd)
    try:
        with open(EPG_STATUS + ".tmp", "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
        try: os.chown(EPG_STATUS + ".tmp", 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
        except Exception: pass
        os.chmod(EPG_STATUS + ".tmp", 0o640); os.replace(EPG_STATUS + ".tmp", EPG_STATUS)
    except OSError: pass


def sync_hls_epg(rules):
    """Свои источники телегида и срок хранения — в настройку HLS Proxy (local.json). Поменялось — перезапуск и сразу скачать.
    Пустой список источников — как при установке (epg_url из config.json)."""
    if E4["mode"] != "hls": return False
    try: c = json.load(open(CONF, encoding="utf-8"))
    except Exception: return False
    epg = c.setdefault("epg", {})
    srcs = [u for u in (rules.get("epg_sources") or []) if re.match(r"^https?://[^\s\"<>]{1,1000}$", str(u))][:5] or [E4.get("epg_url") or "http://epg.it999.ru/edem.xml.gz"]
    days = rules.get("epg_days")
    changed = False
    if epg.get("tvGuideUrl") != srcs: epg["tvGuideUrl"] = srcs; changed = True
    if isinstance(days, int) and 1 <= days <= 14 and epg.get("historyDays") != days: epg["historyDays"] = days; changed = True
    _epg_status({"hls": {"sources": epg.get("tvGuideUrl") or [], "days": epg.get("historyDays")}})
    if not changed: return False
    st_ = os.stat(CONF); tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False, indent=1)
    os.chown(tmp, st_.st_uid, st_.st_gid); os.chmod(tmp, 0o600); os.replace(tmp, CONF)
    import subprocess
    try: subprocess.run(["systemctl", "restart", "hls-proxy"], timeout=60)
    except Exception as e: print("не удалось перезапустить HLS Proxy:", e)
    try:
        if os.path.isdir(E4["build_dir"]):
            with open(E4["build_dir"] + "/hls.sha256", "w") as f: f.write(_hls_hash() + "  -\n")
    except OSError: pass
    with open(EPG_FLAG, "w") as f: f.write(str(time.time()))   # новые источники — скачать сразу, а не ждать расписания
    print("телегид: настройки HLS Proxy обновлены, HLS Proxy перезапущен")
    return True


def hls_fetch_epg():
    """Попросить HLS Proxy скачать телегид сейчас (то же, что «Fetch EPG» в его панели): вход админом, затем /fetchepg."""
    if E4["mode"] != "hls": return
    st0 = rj(EPG_STATUS, {}) or {}
    try: got = os.path.getmtime(os.path.join(E4.get("hls_dir") or "/opt/hls-proxy", "epg.xml.gz"))
    except OSError: got = 0
    req = st0.get("req_ts") or 0
    # прошлый запрос не дошёл до конца (HLS Proxy перезапустила сборка посреди скачивания) — повторить, но не дольше получаса
    pending = st0.get("ok") and req > got and time.time() - req < 1800
    if not os.path.exists(EPG_FLAG) and not pending: return
    try: os.remove(EPG_FLAG)
    except OSError: pass
    try:
        c = json.load(open(CONF, encoding="utf-8")); S = c.get("SERVER") or {}
        pw = S.get("password") or ""
        proto = "https" if S.get("protocol") == "https" else "http"
        host = S.get("address") if proto == "https" else "127.0.0.1"   # https: по имени, с обычной проверкой сертификата
        base = f"{proto}://{host}:{int(S.get('port') or (443 if proto == 'https' else 80))}"
        if not pw: raise RuntimeError("нет пароля администратора HLS Proxy")

        class NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        op = urllib.request.build_opener(NoRedir)
        tok, err = None, ""
        for _ in range(15):   # после перезапуска HLS Proxy поднимается несколько секунд
            try:
                rq = urllib.request.Request(base + "/login", data=urllib.parse.urlencode({"username": "admin", "password": pw}).encode(), method="POST")
                try: r = op.open(rq, timeout=10); hdrs = r.headers
                except urllib.error.HTTPError as e: hdrs = e.headers
                for v in hdrs.get_all("Set-Cookie") or []:
                    m = re.match(r"\s*X-Auth-Token=([^;]+)", v)
                    if m: tok = m.group(1)
                if tok: break
                err = "HLS Proxy не принял пароль администратора"; break
            except Exception as e: err = str(e); time.sleep(2)
        if not tok: raise RuntimeError(err or "нет ответа HLS Proxy")
        try:   # уже качает — второй раз не просим
            with op.open(urllib.request.Request(base + "/status.json", headers={"Cookie": "X-Auth-Token=" + tok}), timeout=10) as r:
                if json.loads(r.read(200000).decode("utf-8", "replace")).get("isEpgFetch"): return
        except Exception: pass
        rq = urllib.request.Request(base + "/fetchepg", headers={"Cookie": "X-Auth-Token=" + tok})
        try: op.open(rq, timeout=20); code = 200
        except urllib.error.HTTPError as e: code = e.code
        if code not in (200, 301, 302, 303): raise RuntimeError(f"HLS Proxy ответил {code}")
        _epg_status({"at": time.strftime("%d.%m.%Y %H:%M"), "ok": True, "error": "", "req_ts": req if pending else time.time()})
        print("телегид: HLS Proxy начал скачивание")
    except Exception as e:
        _epg_status({"at": time.strftime("%d.%m.%Y %H:%M"), "ok": False, "error": str(e)[:200]})
        print("телегид: не удалось запустить обновление:", e)


def sync_hls_clients(active, fmt=""):
    """Режим hls: у каждого клиента свой логин в HLS Proxy со своим лимитом потоков.
    Выключенные и просроченные клиенты из HLS Proxy убираются — их старые ссылки на потоки перестают работать.
    fmt: "hls" / "ts" — формат потока для всех (галка «MPEG-TS» у клиента HLS Proxy = isHttp), "" — не трогать.
    Если что-то поменялось — перезапуск HLS Proxy. Возвращает True, если перезапускали."""
    if E4["mode"] != "hls": return False
    try: c = json.load(open(CONF, encoding="utf-8"))
    except Exception: return False
    cls = c.get("clients")
    if not isinstance(cls, dict): return False
    base = next((v for k, v in cls.items() if not k.startswith(HPFX) and isinstance(v, dict)), None)
    if base is None: return False
    changed = False
    if fmt in ("hls", "ts"):   # общий формат: базовым логинам (tv и др.) ставим галку как велено, личные возьмут её у базового
        for k, v in cls.items():
            if not k.startswith(HPFX) and isinstance(v, dict) and v.get("isHttp") != (fmt == "ts"):
                v["isHttp"] = fmt == "ts"; changed = True
    F = ("name", "password", "maxStreamCount", "isAdultAllowed", "isHttp")
    want = {}
    for x in active:
        n = HPFX + x["id"]
        e = dict(base); e.update(cls.get(n) or {})
        e.update({"name": n, "password": x.get("hls_pw") or "", "isHttp": base.get("isHttp"),
                  "maxStreamCount": int(x.get("streams") or 0) or NO_LIMIT, "isAdultAllowed": not x.get("no_adult")})
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", e["password"]): continue
        want[n] = e
    cur = {k: v for k, v in cls.items() if k.startswith(HPFX)}
    if not changed and set(cur) == set(want) and all({f: cur[k].get(f) for f in F} == {f: want[k].get(f) for f in F} for k in want): return False
    for k in cur: cls.pop(k)
    cls.update(want)
    st_ = os.stat(CONF); tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False, indent=1)
    os.chown(tmp, st_.st_uid, st_.st_gid); os.chmod(tmp, 0o600); os.replace(tmp, CONF)
    import subprocess
    try: subprocess.run(["systemctl", "restart", "hls-proxy"], timeout=60)
    except Exception as e: print("не удалось перезапустить HLS Proxy:", e)
    try:
        hf = E4["build_dir"] + "/hls.sha256"
        if os.path.isdir(E4["build_dir"]):
            with open(hf, "w") as f: f.write(_hls_hash() + "  -\n")
    except OSError: pass
    time.sleep(3)
    return True


def _url_idx(lines):
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].strip() and not lines[i].startswith("#"): return i
    return None


# ---------- «Ссылки на каналы»: через сервер / перенаправление / напрямую (у плейлиста и у клиента) ----------
LINK_MODES = ("hls", "redirect", "direct")
DEF_MODE = "hls" if E4["mode"] == "hls" else "redirect"
GMODE, GFB, GLP = "", True, True   # общая настройка из rules.json (main() задаёт)
LINKS_FILE = E4["editor_dir"] + "/links.json"      # карта перенаправлений и логотипов для app.py (/live/_r/…, /live/_logo/…)
SALT_FILE = E4["editor_dir"] + "/links.salt"
EXTRA_FILE = E4["build_dir"] + "/extra.m3u"        # свои каналы «через сервер» для HLS Proxy (читает build.py)
EXTRA_MAX = 5000


def _base():
    """Адрес редактора для ссылок /live/… (nginx пускает /live/ в редактор без входа): public_base без /pl/."""
    b = str(E4.get("public_base") or "").rstrip("/")
    return re.sub(r"/pl$", "", b) if re.match(r"^https?://", b) else ""


ARCH_RX = re.compile(r'\s+(?:tvg-rec|timeshift|catchup|catchup-days|catchup-type|catchup-source|catchup-correction)="[^"]*"')


def arch_days(l0):
    """Сколько дней архива у канала по строке #EXTINF (tvg-rec, catchup-days, timeshift; catchup без дней — 1). 0 — нет архива."""
    a = dict(re.findall(r'([\w-]+)="([^"]*)"', l0))
    n = 0
    for k in ("tvg-rec", "catchup-days", "timeshift"):
        v = a.get(k, "")
        if v.isdigit(): n = max(n, min(int(v), 60))
    if not n and a.get("catchup") and a["catchup"] not in ("0", "none", "disabled"): n = 1
    return n


def x10(url):
    return hashlib.sha1(url.encode()).hexdigest()[:10]


class Links:
    """Выбирает ссылку канала по режиму и собирает карту перенаправлений/логотипов для links.json."""
    def __init__(self, items):
        self.base = _base()
        try: self.salt = open(SALT_FILE, "rb").read()
        except OSError: self.salt = b""
        if len(self.salt) < 16:
            self.salt = os.urandom(32)
            try:
                with open(SALT_FILE, "wb") as f: f.write(self.salt)
                os.chmod(SALT_FILE, 0o600)
            except OSError: pass
        self.u, self.t, self.l, self.need = {}, {}, {}, {}
        self.own = {urllib.parse.urlsplit(x).hostname for x in (self.base, E4.get("hls_url") or "", E4.get("public_base") or "") if x} - {None}
        self.orig = {}   # имя канала (как в общем списке) -> исходная ссылка провайдера: для «перенаправления» и «напрямую»
        self.arc = {}    # имя канала -> дней архива у ОСНОВНОГО источника (первая ссылка; ей HLS Proxy и шлёт запрос архива)
        self.arch_off = 0
        try:
            for it in parse(open(E4["combined"], encoding="utf-8", errors="replace").read())[1]:
                if EX_RX.search(it["name"]): continue
                i = _url_idx(it["lines"])
                if i is not None and it["name"].lower() not in self.orig:
                    self.orig[it["name"].lower()] = it["lines"][i].strip(); self.arc[it["name"].lower()] = arch_days(it["lines"][0])
        except OSError: pass
        for k, v in list(self.orig.items()):   # HLS Proxy отрезает HD/FHD/UHD/4K в конце названия
            q = QUAL_RX.sub("", k)
            if q not in self.orig: self.orig[q] = v; self.arc[q] = self.arc[k]
        self.ex = {}     # код своего канала -> строки из HLS Proxy (общий логин), если он уже там
        for it in items or []:
            m = EX_RX.search(it["name"])
            if m: self.ex[m.group(1)] = it["lines"]

    def h(self, url):
        return hmac.new(self.salt, url.encode(), hashlib.sha256).hexdigest()[:16]

    def redirect(self, tok, url):
        hx = self.h(url); self.u[hx] = url; self.t.setdefault(tok, set()).add(hx)
        return f"{self.base}/live/_r/{tok}/{hx}"

    def logo(self, l0):
        m = re.search(r'tvg-logo="(https?://[^"]+)"', l0)
        if not m or not self.base or urllib.parse.urlsplit(m.group(1)).hostname in self.own: return l0
        hx = self.h(m.group(1)); self.l[hx] = m.group(1)
        return l0[:m.start(1)] + f"{self.base}/live/_logo/{hx}" + l0[m.end(1):]

    def mode(self, mode):
        """Режим, который реально получится: без HLS Proxy «через сервер» невозможно, без адреса редактора — перенаправление."""
        m = mode if mode in LINK_MODES else DEF_MODE
        if m == "hls" and E4["mode"] != "hls": m = "redirect"
        if m == "redirect" and not self.base: m = "direct"
        return m

    def apply(self, lines, kind, mode, tok, fb=True, lp=True, ex=None, rn=None):
        """lines — строки канала; kind: hls (канал источника, ссылка HLS Proxy/исходная) или ext (своя ссылка / отдельный плейлист).
        rn — имя канала в HLS Proxy с меткой «не склеивать» (по нему ищется исходная ссылка именно его источника).
        Возвращает (строки, как отдан) или (None, "drop"). ex — свои каналы в HLS Proxy под логином этого клиента."""
        lines = list(lines); i = _url_idx(lines)
        if i is None: return None, "drop"
        cur = lines[i].strip()
        m = self.mode(mode)
        name = INF.match(lines[0]).group(1).strip().lower() if INF.match(lines[0]) else ""
        key = (rn or name).lower()
        src = cur if kind == "ext" or E4["mode"] != "hls" else (self.orig.get(key) or self.orig.get(QUAL_RX.sub("", key)))
        if kind == "hls" and ARCH_RX.search(lines[0]) and arch_days(lines[0]):   # склеенный канал: метка архива могла прийти от запасного источника
            d = self.arc.get(key, self.arc.get(QUAL_RX.sub("", key)))
            if d == 0: lines[0] = ARCH_RX.sub("", lines[0]); self.arch_off += 1   # у основного архива нет — запись не откроется
        via = m
        if m == "hls":
            if kind == "ext":
                hl = None
                if re.match(r"^https?://", cur):
                    x = x10(cur); self.need[x] = (re.sub(r'[\r\n",]', " ", name)[:120] or "channel", cur)
                    hl = (ex if ex is not None else self.ex).get(x)
                j = _url_idx(hl) if hl else None
                if j is not None: lines[i] = hl[j]
                elif fb and self.base: lines[i] = self.redirect(tok, cur); via = "redirect"
                else: return None, "drop"
        elif m == "redirect":
            if src: lines[i] = self.redirect(tok, src)
            else: via = "hls"   # исходная ссылка неизвестна — остаётся ссылка сервера
        else:
            if src: lines[i] = src
            else: via = "hls"
        if lp: lines[0] = self.logo(lines[0])
        return lines, via

    def save(self, wgid):
        d = {"u": self.u, "t": {k: sorted(v) for k, v in self.t.items()}, "l": self.l}
        tmp = LINKS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f)
        try: os.chown(tmp, 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
        except Exception: pass
        os.chmod(tmp, 0o640); os.replace(tmp, LINKS_FILE)

    def sync_extra(self):
        """Свои каналы «через сервер»: записать extra.m3u и, если HLS Proxy их ещё не знает, дописать в общий список и перезапустить его.
        True — HLS Proxy перезапущен, плейлисты надо собрать ещё раз (тогда ссылки станут ссылками сервера)."""
        need = dict(list(self.need.items())[:EXTRA_MAX])
        txt = "".join(f'#EXTINF:-1 group-title="E4 extra",{n} [={x}]\n{u}\n' for x, (n, u) in sorted(need.items()))
        try: old = open(EXTRA_FILE, encoding="utf-8").read()
        except OSError: old = ""
        if txt != old:
            try:
                os.makedirs(os.path.dirname(EXTRA_FILE), exist_ok=True)
                with open(EXTRA_FILE + ".tmp", "w", encoding="utf-8") as f: f.write(txt)
                os.replace(EXTRA_FILE + ".tmp", EXTRA_FILE)
            except OSError as e: print("extra.m3u:", e); return False
        if E4["mode"] != "hls" or set(need) <= set(self.ex): return False
        p = E4["combined"]   # дописываем сами, чтобы не ждать полной пересборки; build.py дальше берёт extra.m3u сам
        try:
            st_ = os.stat(p); head, items = parse(open(p, encoding="utf-8", errors="replace").read())
            body = [l for it in items if not EX_RX.search(it["name"]) for l in it["lines"]]
            with open(p + ".tmp", "w", encoding="utf-8") as f: f.write("\n".join([head] + body) + "\n" + txt)
            os.chown(p + ".tmp", st_.st_uid, st_.st_gid); os.chmod(p + ".tmp", st_.st_mode & 0o777); os.replace(p + ".tmp", p)
        except OSError as e: print("общий список:", e); return False
        import subprocess
        try: subprocess.run(["systemctl", "restart", "hls-proxy"], timeout=60)
        except Exception as e: print("не удалось перезапустить HLS Proxy:", e); return False
        try:
            with open(E4["build_dir"] + "/hls.sha256", "w") as f: f.write(_hls_hash() + "  -\n")
        except OSError: pass
        print(f"через сервер: в HLS Proxy добавлено своих каналов: {len(set(need) - set(self.ex))}")
        time.sleep(3)
        return True


XCCH = {}   # id клиента -> каналы для Xtream Codes


def xc_channels(lines):
    """Строки M3U -> [{name, group, logo, tvg_id, url}] для входа через Xtream Codes."""
    out, cur = [], None
    for l in lines:
        if l.startswith("#EXTINF"):
            a = dict(re.findall(r'([\w-]+)="([^"]*)"', l))
            m = INF.match(l)
            cur = {"name": (m.group(1).strip() if m else "") or a.get("tvg-name", "") or "Канал", "group": a.get("group-title") or "Без группы",
                   "logo": a.get("tvg-logo", ""), "tvg_id": a.get("tvg-id", ""), "arch": arch_days(l)}
        elif l.startswith("#") or not l.strip(): continue
        elif cur: cur["url"] = l.strip(); out.append(cur); cur = None
    return out


def write_xc(clients):
    """xc.json для редактора: логин -> пароль, статус, срок, IP и каналы (каналы — только у активных)."""
    d = {}
    for x in clients:
        u = str(x.get("xc_user") or "")
        if not re.fullmatch(r"[a-z0-9_]{3,32}", u) or not re.fullmatch(r"[A-Za-z0-9]{8,32}", str(x.get("xc_pw") or "")): continue
        d[u] = {"id": x["id"], "pw": x["xc_pw"], "enabled": x.get("enabled") is not False, "expires": x.get("expires") or "",
                "created": x.get("created") or "", "streams": x.get("streams") or 0, "ips": x.get("ips") or [],
                "channels": XCCH.get(str(x["id"]), [])}
    p = E4["editor_dir"] + "/xc.json"; tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(d, f, ensure_ascii=False)
    try: os.chown(tmp, 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
    except Exception: pass
    os.chmod(tmp, 0o640); os.replace(tmp, p)


ACL = "/etc/nginx/e4tv-acl.conf"


def write_acl(clients):
    """Разрешённые IP клиентов -> правила nginx для ссылок /pl/<токен>.m3u8 (если поменялись — проверка и перезагрузка nginx)."""
    import ipaddress, subprocess
    geo, mp = [], []
    for x in clients:
        nets = []
        for n in x.get("ips") or []:
            try: nets.append(str(ipaddress.ip_network(str(n), strict=False)))
            except ValueError: pass
        if not nets or not TOK.match(str(x.get("token", ""))) or not re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("id", ""))): continue
        v = f"$e4tv_ip_{x['id']}"
        geo.append(f"geo $remote_addr {v} {{ default 0; " + " ".join(f"{n} 1;" for n in nets) + " }")
        for nm in [x["token"]] + ([x["slug"]] if SLUG.match(str(x.get("slug") or "")) else []):   # и для короткой ссылки
            mp.append('    "~^/pl/' + nm + r'\.m3u8?$" ' + v + ';')
    txt = ("# Evgen4ik TV: разрешённые IP клиентов. Файл пишет lists.py — правки вручную будут перезаписаны.\n" + "\n".join(geo) + ("\n" if geo else "")
           + "map $uri $e4tv_acl {\n    default 1;\n" + "".join(l + "\n" for l in mp) + "}\n")
    try: old = open(ACL, encoding="utf-8").read()
    except OSError: old = None
    if old == txt or not os.path.isdir("/etc/nginx"): return
    with open(ACL, "w", encoding="utf-8") as f: f.write(txt)
    try:
        if subprocess.run(["nginx", "-t"], capture_output=True, timeout=30).returncode != 0:
            if old is not None:
                with open(ACL, "w", encoding="utf-8") as f: f.write(old)
            print("nginx не принял разрешённые IP — оставлены прежние"); return
        subprocess.run(["systemctl", "reload", "nginx"], timeout=30)
    except Exception as e:
        print("не удалось обновить nginx:", e)


def build_clients(active, LOUT, head, keep, wgid, st, items=None, fmap=None, LK=None):
    """Личный плейлист клиента: каналы выбранных ему плейлистов (без повторов) + его правки:
    off — выключенные каналы, add — добавленные из основного, extra — свои каналы, urls — своя ссылка у канала.
    В режиме hls ссылки — под логином клиента (кроме своих ссылок и отдельных плейлистов: они идут напрямую)."""
    st["clients"], st["clients_err"] = {}, {}
    kmap = {}   # ключ канала основного плейлиста -> (строки, группа)
    for it in items or []:
        m = (fmap or {}).get(it["name"].lower())
        if EX_RX.search(it["name"]): continue
        if m and (m[0] not in kmap or NM_RX.search(kmap[m[0]][0]["name"])): kmap[m[0]] = (it, m[1])   # как в основном: без «не склеивать»
    for x in active:
        cid = str(x["id"])
        OFF, URLS = set(x.get("off") or []), x.get("urls") if isinstance(x.get("urls"), dict) else {}
        seen, ent, multi = set(), [], len([l for l in (x.get("lists") or []) if l in LOUT]) > 1 or bool(x.get("add") or x.get("extra"))
        kid = lambda g: x.get("no_adult") and (g in ADULT_GROUPS or "18+" in g)
        for lid in x.get("lists") or []:
            for e in LOUT.get(lid, []):
                if e["n"] in seen or e.get("k") in seen or e.get("k") in OFF or kid(e["g"]): continue   # по k: «не склеивать» и тёзка — один раз
                seen.add(e["n"]); seen.add(e.get("k")); ent.append(e)
        for k in x.get("add") or []:   # добавленные вручную из основного плейлиста
            if k in OFF or k not in kmap: continue
            it, g = kmap[k]
            if it["name"].lower() in seen or k in seen or kid(g): continue
            seen.add(it["name"].lower()); seen.add(k); ent.append({"n": it["name"].lower(), "k": k, "free": False, "lines": unmark(list(it["lines"])), "g": g, "rn": it["name"]})
        for j, e in enumerate(ent):   # своя ссылка у канала: идёт напрямую, мимо HLS Proxy (копия — записи общие для всех клиентов)
            u = URLS.get(e.get("k"))
            i = _url_idx(e["lines"]) if u and URLOK.match(u) else None
            if i is not None:
                ln = list(e["lines"]); ln[i] = u
                ent[j] = dict(e, lines=ln, free=True, kind="ext")
        for c in x.get("extra") or []:   # свои каналы клиента
            if not (isinstance(c, dict) and c.get("name") and URLOK.match(str(c.get("url", "")))) or ("x:" + str(c.get("id"))) in OFF: continue
            nm = re.sub(r"\s{2,}", " ", re.sub(r'[\r\n,"]', " ", c["name"])).strip(); gr = re.sub(r'[\r\n"]', " ", c.get("group") or "Мои каналы").strip()
            lg = f' tvg-logo="{c["logo"]}"' if URLOK.match(str(c.get("logo") or "")) else ""
            ent.append({"n": nm.lower(), "k": "x:" + str(c.get("id")), "free": True, "g": gr,
                        "lines": [f'#EXTINF:-1{lg} group-title="{gr}",{nm}', c["url"]]})
        ORD = {k: i for i, k in enumerate(x.get("order") or [])}
        if ORD: ent.sort(key=lambda e: ORD.get(e.get("k"), 10**6))   # свой порядок клиента (перетаскивание), остальные — после
        cm = x.get("link_mode") if x.get("link_mode") in LINK_MODES else ""   # пусто = как у плейлиста
        emode = lambda e: LK.mode(cm or e.get("lm") or GMODE) if LK else "hls"
        cmap, miss, via = None, 0, {}
        if E4["mode"] == "hls" and any(emode(e) == "hls" for e in ent):
            t0, err = time.time(), None
            while True:   # после перезапуска HLS Proxy логин появляется не сразу
                try: cmap = {it["name"].lower(): it["lines"] for it in parse(master(HPFX + cid, x.get("hls_pw") or ""))[1]}; break
                except Exception as e:
                    err = e
                    if time.time() - t0 > 45: break
                    time.sleep(5)
            if cmap is None: st["clients_err"][cid] = f"HLS Proxy не отдал плейлист клиента: {err}"; continue
        cex = {m.group(1): ln for n, ln in (cmap or {}).items() for m in [EX_RX.search(n)] if m}   # свои каналы под логином клиента
        out = [head]
        for e in ent:
            lines = list(e["lines"])
            if multi: lines[0] = re.sub(r'\s+tvg-chno="[^"]*"', "", lines[0])   # номера из разных плейлистов совпали бы
            em, kind = emode(e), e.get("kind") or ("ext" if e["free"] else "hls")
            if cmap is not None and kind == "hls" and em == "hls":
                cl = cmap.get(e["n"]); i, j = _url_idx(lines), (_url_idx(cl) if cl else None)
                if i is None or j is None: miss += 1; continue   # без своей ссылки клиент шёл бы мимо своего лимита
                lines[i] = cl[j]
            if LK:
                lines, how = LK.apply(lines, kind, em, x["token"], e.get("fb", GFB), e.get("lp", GLP), ex=cex if cmap is not None else None,
                                      rn=e.get("rn"))
                via[how] = via.get(how, 0) + 1
                if not lines: continue
            out += lines
        for nm in [x["token"]] + ([x["slug"]] if SLUG.match(str(x.get("slug") or "")) else []):   # длинная и короткая ссылка — один и тот же файл
            fn = f"{OUTDIR}/{nm}.m3u8"; tmp = fn + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: f.write("\n".join(out) + "\n")
            os.chown(tmp, 0, wgid); os.chmod(tmp, 0o640); os.replace(tmp, fn); keep.add(fn)
        st["clients"][cid] = {"channels": sum(1 for l in out if l.startswith("#EXTINF")), "missing": miss, "via": via,
                              "mode": LK.mode(cm) if (LK and cm) else ""}
        XCCH[cid] = xc_channels(out)


# ---------- активность: кто открывал плейлисты (журнал nginx /var/log/nginx/e4tv-pl.log) ----------
ALOG = "/var/log/nginx/e4tv-pl.log"
ACT = E4["editor_dir"] + "/activity.json"
APOS = E4["build_dir"] + "/activity.pos"
UA_NAMES = [("ott navigator", "OTT Navigator"), ("ott-navigator", "OTT Navigator"), ("tivimate", "TiviMate"), ("televizo", "Televizo"),
            ("perfect player", "Perfect Player"), ("smarters", "IPTV Smarters"), ("kodi", "Kodi"), ("vlc", "VLC"), ("lavf", "плеер (ffmpeg)"),
            ("lampa", "Lampa"), ("ss iptv", "SS IPTV"), ("tizen", "Samsung TV"), ("webos", "LG TV"), ("exoplayer", "Android-плеер"),
            ("okhttp", "Android-плеер"), ("dalvik", "Android-плеер"), ("mozilla", "браузер")]


# ---------- «Не открылось у клиентов»: ошибки источников из журнала HLS Proxy ----------
HERR = E4["editor_dir"] + "/hls-errors.json"          # сводка для редактора (Обзор, Журнал)
HSTATE = E4["build_dir"] + "/hls-errors.state.json"   # курсор журнала, события, коды каналов HLS Proxy
HE_KEEP = 48 * 3600
# отмены, а не поломки: зритель переключил канал или HLS Proxy сам прервал запрос; проверка лицензии HLS Proxy
HE_SKIP = re.compile(r"Aborted by main process|Cancelled by parent|interrupted by main process|lms\.hls-proxy\.com", re.I)
HE_REQ = re.compile(r"(?:Request playlist for stream|HLS\.fetchPlaylist:) (.+?) \(([0-9a-z]{4,16})\)")
HE_CH = re.compile(r"channelHandler Channel ([0-9a-z]{4,16}) cannot be started: (.*)")
HE_FP = re.compile(r"HLS\.fetchPlaylist: (.+?) \(([0-9a-z]{4,16})\) (.*)")
HE_RQ = re.compile(r"Request failed, no retry left: Exception: (.*?)\s*(https?://\S+)")
HE_TS = re.compile(r"\[(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?Z\]")


def he_name(n):
    return re.sub(r"\s*\[~[0-9a-f]+\]\s*$", "", n or "").strip()


def he_msg(m):
    """Причина по-русски, без ссылок (в них ключи провайдера)."""
    m = re.sub(r"https?://\S+", "", m or "").strip(" :")
    if re.search(r"ESOCKETTIMEDOUT|ETIMEDOUT|timed? ?out", m, re.I): return "источник не ответил вовремя"
    if re.search(r"aborted by speed|speed is below", m, re.I): return "источник отдаёт поток слишком медленно"
    c = re.search(r"(?<!\d)([45]\d\d)(?!\d)", m)
    if c: return f"источник ответил ошибкой {c.group(1)}"
    if re.search(r"certificate", m, re.I): return "у источника неверный или просроченный сертификат"
    if re.search(r"EXT-X-|EXTM3U|Cannot read properties", m): return "источник прислал не плейлист"
    if re.search(r"ENOTFOUND|EAI_AGAIN|ECONNREFUSED|EHOSTUNREACH", m, re.I): return "нет связи с источником"
    if re.fullmatch(r"aborted|ECONNRESET|socket hang up", m, re.I): return "источник оборвал соединение"
    return re.sub(r"[\x00-\x1f]", " ", m)[:120] or "ошибка источника"


def he_urlkey(u):
    u = re.sub(r"[?#].*$", "", u or "")
    return u.rsplit("/", 1)[0] if "/" in u[8:] else u


def he_pathkey(u):
    """Та же папка без адреса сервера: куски видео часто отдаёт другой сервер провайдера (CDN). Только глубокие пути."""
    p = re.sub(r"^[a-z]+://[^/]+", "", he_urlkey(u), flags=re.I)
    return "p:" + p if p.count("/") >= 3 else ""


def he_urlmap(path):
    """Папка ссылки потока -> название канала (по общему плейлисту, который читает HLS Proxy)."""
    m, name = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for l in f:
                l = l.strip()
                if l.startswith("#EXTINF"): name = l.rsplit(",", 1)[-1].strip()
                elif l and not l.startswith("#") and name:
                    m.setdefault(he_urlkey(l), name)
                    pk = he_pathkey(l)
                    if pk: m[pk] = name if m.get(pk, name) == name else ""   # одна папка у разных каналов — не угадываем
                    name = None
    except OSError: pass
    return m


def he_parse(lines, st, urlmap):
    """Разобрать строки журнала HLS Proxy, добавить события в st["ev"] (название -> [[время, причина, вид], ...]).
    Вид: "f" — канал не открылся (не взят плейлист), "g" — перебои (не скачался кусок видео, канал при этом может идти)."""
    ids, ev = st.setdefault("ids", {}), st.setdefault("ev", {})
    for l in lines:
        m = HE_REQ.search(l)
        if m: ids[m.group(2)] = he_name(m.group(1))
        if not re.search(r"\bERROR\b", l): continue
        if HE_SKIP.search(l): continue
        name = msg = None; kind = "f"
        m = HE_CH.search(l)
        if m: name, msg = ids.get(m.group(1)) or "#" + m.group(1), m.group(2)   # код без названия — уточним ниже
        else:
            m = HE_FP.search(l)
            if m: name, msg = he_name(m.group(1)), m.group(3)
            else:
                m = HE_RQ.search(l)
                if m:
                    name, msg = urlmap.get(he_urlkey(m.group(2))) or urlmap.get(he_pathkey(m.group(2)) or "-"), m.group(1)
                    if not re.search(r"\.m3u8?(?:[?#]|$)", m.group(2), re.I): kind = "g"   # кусок видео, а не плейлист
        if not name: continue
        t = HE_TS.search(l)
        ts = (t.group(1) + "Z") if t else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lst = ev.setdefault(name, [])
        if lst and abs(_he_epoch(ts) - _he_epoch(lst[-1][0])) < 60:   # пачка ошибок подряд — один случай; «не открылся» важнее «перебоев»
            if kind == "f" or (lst[-1][2:] or ["f"])[0] != "f": lst[-1][1:] = [he_msg(msg), kind if kind == "f" else (lst[-1][2:] or ["f"])[0]]
            continue
        lst.append([ts, he_msg(msg), kind])
    for k in [k for k in ev if k.startswith("#") and ids.get(k[1:])]:   # название канала стало известно позже ошибки
        ev.setdefault(ids[k[1:]], []).extend(ev.pop(k)); ev[ids[k[1:]]].sort()
    if len(ids) > 30000: st["ids"] = dict(list(ids.items())[-20000:])


def _he_epoch(ts):
    try: return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc).timestamp()
    except ValueError: return 0


def he_summary(st, now=None):
    now = now or time.time(); items = []
    for n, lst in list(st.get("ev", {}).items()):
        lst[:] = [e for e in lst if now - _he_epoch(e[0]) < HE_KEEP][-50:]
        if not lst: st["ev"].pop(n, None); continue
        if n.startswith("#"): continue   # канал не опознан — не показываем
        d24 = [e for e in lst if now - _he_epoch(e[0]) < 86400]
        f24 = sum(1 for e in d24 if (e[2:] or ["f"])[0] == "f")
        fl = [e for e in lst if (e[2:] or ["f"])[0] == "f"]
        top = fl[-1] if fl else lst[-1]   # причина: последнее «не открылся», иначе последние перебои
        last = datetime.datetime.fromtimestamp(_he_epoch(lst[-1][0])).strftime("%d.%m.%Y %H:%M")
        items.append({"n": n, "c24": len(d24), "f24": f24, "g24": len(d24) - f24, "f48": len(fl), "c48": len(lst),
                      "last": last, "ts": lst[-1][0], "msg": top[1]})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"at": time.strftime("%d.%m.%Y %H:%M"), "items": items[:300]}


def hls_errors():
    if E4.get("mode") != "hls": return
    import subprocess
    st = rj(HSTATE, {}) or {}
    cmd = ["journalctl", "-u", "hls-proxy", "-o", "cat", "--no-pager", "--show-cursor"]
    cmd += ["--after-cursor", st["cur"]] if st.get("cur") else ["--since", "-24h"]
    try: out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, errors="replace").stdout
    except Exception: return
    lines = out.splitlines()
    if lines and lines[-1].startswith("-- cursor: "): st["cur"] = lines.pop()[11:].strip()
    he_parse(lines, st, he_urlmap(E4.get("combined") or ""))
    summ = he_summary(st)
    os.makedirs(os.path.dirname(HSTATE), exist_ok=True)
    with open(HSTATE + ".tmp", "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
    os.chmod(HSTATE + ".tmp", 0o600); os.replace(HSTATE + ".tmp", HSTATE)
    with open(HERR + ".tmp", "w", encoding="utf-8") as f: json.dump(summ, f, ensure_ascii=False)
    try: os.chown(HERR + ".tmp", 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
    except Exception: pass
    os.chmod(HERR + ".tmp", 0o640); os.replace(HERR + ".tmp", HERR)


def ua_name(ua):
    u = (ua or "").lower()
    for k, v in UA_NAMES:
        if k in u: return v
    return re.split(r"[/\s(]", ua or "", 1)[0][:40] or "неизвестно"


def activity():
    """Разобрать новые строки журнала (с прошлого места) и обновить activity.json: последний заход, плеер, IP, заходы по дням."""
    act = rj(ACT, {}) or {}
    pos = rj(APOS, {}) or {}
    try: stt = os.stat(ALOG)
    except OSError: return 0
    off = pos.get("off", 0) if pos.get("ino") == stt.st_ino and pos.get("off", 0) <= stt.st_size else 0   # журнал сменился — с начала
    rx = re.compile(r"^/pl/([A-Za-z0-9_-]{3,64})\.m3u8?(?:\?|$)")
    rules = rj(RULES, {}) or {}   # заходы по короткой ссылке клиента считаем под его длинной (токеном)
    smap = {str(c["slug"]): str(c.get("token")) for c in (rules.get("clients") or []) if isinstance(c, dict) and SLUG.match(str(c.get("slug") or ""))}
    with open(ALOG, "rb") as f:
        f.seek(off)
        for raw in f:
            p = raw.decode("utf-8", "replace").rstrip("\n").split(" ", 4)   # время IP код запрос плеер
            if len(p) < 5 or p[2] not in ("200", "304"): continue
            m = rx.match(p[3])
            if not m: continue
            day = p[0][:10]
            a = act.setdefault(smap.get(m.group(1), m.group(1)), {"days": {}})
            if p[0][:19] >= a.get("last", ""): a.update({"last": p[0][:19], "ip": p[1][:64], "ua": ua_name(p[4].strip('"'))})
            a["days"][day] = a["days"].get(day, 0) + 1
        off = f.tell()
    lim = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    for a in act.values(): a["days"] = {d: n for d, n in a.get("days", {}).items() if d >= lim}
    tmp = ACT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(act, f, ensure_ascii=False)
    try: os.chown(tmp, 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
    except Exception: pass
    os.chmod(tmp, 0o640); os.replace(tmp, ACT)
    os.makedirs(os.path.dirname(APOS), exist_ok=True)
    with open(APOS, "w") as f: json.dump({"ino": stt.st_ino, "off": off}, f)
    return 0


def main():
    rules = rj(RULES, {}) or {}
    global GMODE, GFB, GLP   # общая настройка «Скрывать ссылки провайдера» (у плейлиста и клиента "" = как у всех)
    GMODE = rules.get("link_mode") if rules.get("link_mode") in LINK_MODES else ""
    GFB, GLP = rules.get("fallback") is not False, rules.get("logo_proxy") is not False
    alll = [x for x in (rules.get("lists") or []) if isinstance(x, dict) and TOK.match(str(x.get("token", "")))]
    # выключенный или просроченный плейлист: файла нет, ссылка отдаёт 404
    lists = [x for x in alll if x.get("enabled") is not False and not expired(x)]
    calc = [x for x in alll if not expired(x)]   # выключенные тоже считаем: их могут брать клиенты
    LOUT = {}   # id плейлиста -> каналы (для клиентов)
    clients = [x for x in (rules.get("clients") or []) if isinstance(x, dict) and TOK.match(str(x.get("token", "")))
               and re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("id", "")))]
    active = [x for x in clients if x.get("enabled") is not False and not expired(x)]
    AUTH = f"{OUTDIR}/auth"
    fmap = rj(FINAL, {}) or {}
    nfin = len(fmap)   # каналов в основном (для ожидания HLS Proxy) — до запасных ключей
    fmap = with_quality_aliases(fmap)
    os.makedirs(OUTDIR, exist_ok=True)
    try: wgid = grp.getgrnam("www-data").gr_gid
    except KeyError: wgid = -1
    os.chown(OUTDIR, 0, wgid); os.chmod(OUTDIR, 0o750)
    os.makedirs(AUTH, exist_ok=True); os.chown(AUTH, 0, wgid); os.chmod(AUTH, 0o750)
    st = {"at": time.strftime("%d.%m.%Y %H:%M:%S"), "lists": {}, "error": None,
          "expired": [str(x.get("id")) for x in alll if expired(x)]}
    keep, keep_auth = set(), set()
    items, head = None, "#EXTM3U"
    FREE = [x for x in (rules.get("free_sources") or []) if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("id", ""))) and str(x.get("url", "")).startswith(("http://", "https://", "e4://"))]
    FCACHE = {}
    for fs in FREE:   # подборки собираем всегда — чтобы редактор мог показать их каналы
        if fs["url"].startswith("e4://"):
            FCACHE[fs["id"]] = free_items(fs)
            ps = preset_stat(fs["url"][5:])
            if ps: st.setdefault("presets", {})[fs["url"][5:]] = ps
    sync_hls_clients(active, rules.get("stream_format") or "")   # свои логины клиентов в HLS Proxy (перезапускает его, если что-то поменялось)
    try:
        sync_hls_epg(rules)   # свои источники телегида / срок хранения (перезапуск — только если поменялись)
        hls_fetch_epg()       # «Обновить телегид сейчас» или новые источники
    except Exception as e: print("телегид:", e)
    if lists or MAIN or active:
        t0 = time.time()
        while True:   # после перезапуска HLS Proxy плейлист появляется не сразу
            try:
                head, items = parse(master())
                if not nfin or len(items) >= 0.8 * nfin or time.time() - t0 > WAIT: break
            except Exception as e:
                if time.time() - t0 > WAIT: st["error"] = str(e); items = None; break
            time.sleep(5)
        LK = Links(items)   # «Ссылки на каналы»: через сервер / перенаправление / напрямую
        if items is not None and MAIN:   # основной плейлист: всё, что собрано
            fn = f"{OUTDIR}/{MAIN}.m3u8"; tmp = fn + ".tmp"
            mi = [it for it in items if not NM_RX.search(it["name"]) and not EX_RX.search(it["name"])]   # «не склеивать» и свои каналы — не сюда
            mi = regroup(mi, lambda it: _gt(it["lines"]))
            with open(tmp, "w", encoding="utf-8") as f: f.write("\n".join([head] + [l for it in mi for l in it["lines"]]) + "\n")
            os.chown(tmp, 0, wgid); os.chmod(tmp, 0o640); os.replace(tmp, fn); keep.add(fn)
            st["main"] = {"channels": len(mi)}
        if items is not None:
            for L in calc:
                G, C, X = set(L.get("groups") or []), set(L.get("channels") or []), set(L.get("exclude") or [])
                SRCS = set(L.get("sources") or [])   # пусто = все источники, кроме «только для плейлистов»
                LO = set(rules.get("lists_only_sources") or [])
                ALLG, XG = L.get("all") is True, set(L.get("xgroups") or [])
                num = L.get("chno") if isinstance(L.get("chno"), int) else None
                out, miss, groups, sel_items = [head], 0, set(), []
                kpos, kpos_src = {}, {}
                lmode, lfb, llp, via = L.get("link_mode") or GMODE, rules.get("fallback") is not False, rules.get("logo_proxy") is not False, {}
                for it in items:
                    if EX_RX.search(it["name"]): continue   # свои каналы «через сервер» — служебные, в выбор не входят
                    m = fmap.get(it["name"].lower())
                    if not m: miss += 1; continue
                    k, g = m[0], m[1]
                    srcs = set(m[2]) if len(m) > 2 else set()
                    if NM_RX.search(it["name"]) and not (SRCS & srcs): continue   # «не склеивать» — только где источник выбран явно (и не по ручной отметке)
                    if k in C and k not in X: pass   # канал выбран вручную — берём из любого источника
                    elif SRCS:
                        if srcs and not (SRCS & srcs): continue
                    elif srcs and LO and srcs <= LO: continue   # канал есть только в источниках «только для плейлистов»
                    if L.get("no_adult") and g in ADULT_GROUPS: continue   # детский профиль: 18+ никогда
                    ing = (g not in XG) if ALLG else (g in G)
                    if not ((ing and k not in X) or k in C): continue
                    j = kpos.get(k)   # один канал «не склеивать» и его тёзка: в плейлисте — только один
                    if j is None: kpos[k] = len(sel_items); sel_items.append((k, it))
                    elif (SRCS and (SRCS & srcs) and not (SRCS & kpos_src[k])) or \
                         (not SRCS and NM_RX.search(sel_items[j][1]["name"]) and not NM_RX.search(it["name"])): sel_items[j] = (k, it)
                    else: continue
                    kpos_src[k] = srcs
                FG = set(L.get("fgroups") or [])   # «<id>:<группа>» отдельных плейлистов целиком
                fids = {x.split(":", 2)[1] for x in C | X if x.startswith("f:")} | {x.split(":", 1)[0] for x in FG}
                for fs in [f for f in FREE if f["id"] in fids]:
                    for it in FCACHE.setdefault(fs["id"], free_items(fs)):
                        k = it["k"]
                        if k in X or (L.get("no_adult") and "18+" in it["g"]): continue
                        if k in C or f"{fs['id']}:{it['g']}" in FG: sel_items.append((k, it))
                ORD = {kk: i for i, kk in enumerate(L.get("order") or [])}
                if ORD: sel_items.sort(key=lambda x: ORD.get(x[0], 10**6))   # свой порядок, остальные — как в основном
                else: sel_items = regroup(sel_items, lambda x: _gt(x[1]["lines"]))
                FE = rules.get("free_edits") if isinstance(rules.get("free_edits"), dict) else {}
                for k, it in sel_items:
                    lines = unmark(list(it["lines"]))
                    fe = FE.get(k) if k.startswith("f:") else None
                    if isinstance(fe, dict):   # своя ссылка / логотип у канала отдельного плейлиста
                        lg = fe.get("logo") if isinstance(fe.get("logo"), str) and URLOK.match(fe["logo"]) else None
                        if lg:
                            if re.search(r'tvg-logo="[^"]*"', lines[0]): lines[0] = re.sub(r'tvg-logo="[^"]*"', lambda _: f'tvg-logo="{lg}"', lines[0], count=1)
                            else: lines[0] = re.sub(r'^(#EXTINF:\s*-?\d+)', lambda mm: f'{mm.group(1)} tvg-logo="{lg}"', lines[0], count=1)
                        u = fe.get("url") if isinstance(fe.get("url"), str) and URLOK.match(fe["url"]) else None
                        if u:
                            for i in range(len(lines) - 1, 0, -1):
                                if lines[i].strip() and not lines[i].startswith("#"): lines[i] = u; break
                    gm = re.search(r'group-title="([^"]*)"', lines[0]); groups.add(gm.group(1) if gm else "")
                    if num is not None:   # нумерация каналов (tvg-chno) по порядку
                        l0 = re.sub(r'\s+tvg-chno="[^"]*"', "", lines[0])
                        lines[0] = re.sub(r'^(#EXTINF:\s*-?\d+)', lambda mm: f'{mm.group(1)} tvg-chno="{num}"', l0, count=1); num += 1
                    kind = "ext" if k.startswith("f:") else "hls"   # отдельный плейлист — своя ссылка, остальное — из HLS Proxy/общего списка
                    if not L.get("no_clients"): LOUT.setdefault(L["id"], []).append({"n": it["name"].lower(), "k": k, "free": kind == "ext", "lines": lines,
                                                         "g": gm.group(1) if gm else "", "kind": kind, "lm": lmode, "fb": lfb, "lp": llp, "rn": it["name"]})
                    if L in lists:   # файл есть только у включённого плейлиста
                        fl, how = LK.apply(lines, kind, lmode, L["token"], lfb, llp, rn=it["name"])
                        via[how] = via.get(how, 0) + 1
                        if fl: out += fl
                if L not in lists: continue   # выключенный плейлист: файла и ссылки нет, но клиенты его каналы получают
                names = [L["token"]] + ([L["slug"]] if SLUG.match(str(L.get("slug") or "")) else [])   # длинная и короткая ссылка
                pw = L.get("password") if PASS.match(str(L.get("password") or "")) else None
                for nm in names:
                    af = f"{AUTH}/{nm}.htpasswd"
                    if pw:   # пароль: nginx спросит логин tv и пароль (файл пароля — раньше плейлиста)
                        tmp = af + ".tmp"
                        with open(tmp, "w") as f: f.write(f"tv:{ssha(pw)}\n")
                        os.chown(tmp, 0, wgid); os.chmod(tmp, 0o640); os.replace(tmp, af); keep_auth.add(af)
                    elif os.path.exists(af): os.remove(af)
                    fn = f"{OUTDIR}/{nm}.m3u8"; tmp = fn + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f: f.write("\n".join(out) + "\n")
                    os.chown(tmp, 0, wgid); os.chmod(tmp, 0o640); os.replace(tmp, fn)
                    keep.add(fn)
                st["lists"][str(L.get("id"))] = {"channels": sum(1 for x in out if x.startswith("#EXTINF")), "groups": len(groups), "unmatched": miss,
                                                 "via": via, "mode": LK.mode(lmode)}
    if items is not None:
        build_clients(active, LOUT, head, keep, wgid, st, items, fmap, LK)
        global RERUN
        RERUN = LK.sync_extra()   # свои каналы «через сервер» впервые попали в HLS Proxy — собрать ещё раз со ссылками сервера
        try: LK.save(wgid)
        except OSError as e: print("links.json:", e)
        if LK.arch_off: print(f"метка архива снята (у основного источника архива нет): {LK.arch_off} раз в плейлистах и у клиентов")
        st["arch_off"] = LK.arch_off
    for x in active:   # не удалять прошлый файл клиента, если в этот раз собрать не вышло
        fn = f"{OUTDIR}/{x['token']}.m3u8"
        if fn not in keep and os.path.exists(fn) and (st["error"] or str(x["id"]) in st.get("clients_err", {})): keep.add(fn)
    st["clients_off"] = [str(x["id"]) for x in clients if x not in active]
    if items is not None or not active:
        try: write_xc(clients)
        except OSError as e: print("xc.json:", e)
    try: write_acl(active)
    except OSError as e: print("e4tv-acl.conf:", e)
    if st["error"] is None:   # удалить файлы удалённых листов / старых ссылок
        for f in glob.glob(f"{OUTDIR}/*.m3u8"):
            if f not in keep:
                try: os.remove(f)
                except OSError: pass
        for f in glob.glob(f"{AUTH}/*.htpasswd"):   # пароли удалённых плейлистов (файлы плейлистов уже удалены выше)
            if f not in keep_auth and not os.path.exists(f"{OUTDIR}/{os.path.basename(f)[:-9]}.m3u8"):
                try: os.remove(f)
                except OSError: pass
    tmp = STATUS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(st, f, ensure_ascii=False)
    os.chmod(tmp, 0o644); os.replace(tmp, STATUS)
    print(f"листов: {len(lists)}; " + ("; ".join(f"{k}: {v['channels']}" for k, v in st["lists"].items()) or "—") + (f"; ОШИБКА: {st['error']}" if st["error"] else ""))
    return 1 if st["error"] else 0


if __name__ == "__main__":
    if "--activity" in sys.argv:
        rc = activity()
        try: hls_errors()   # «Не открылось у клиентов» — ошибки источников из журнала HLS Proxy
        except Exception as e: print(f"hls-errors: {e}")
        sys.exit(rc)
    rc = 0
    for _ in range(3):   # если листы сохранили ещё раз, пока шла сборка, — собрать заново
        m0 = os.path.getmtime(RULES) if os.path.exists(RULES) else 0
        RERUN = False
        rc = main()
        if RERUN: WAIT = max(WAIT, 60); continue   # свои каналы только что попали в HLS Proxy — ещё раз, уже со ссылками сервера
        if (os.path.getmtime(RULES) if os.path.exists(RULES) else 0) == m0: break
    sys.exit(rc)
