#!/usr/bin/env python3
"""Evgen4ik TV: плейлисты для плееров — основной и свои подборки каналов со своими ссылками.

Режим hls: берёт клиентский плейлист HLS Proxy (ссылки через рестрим, токены провайдеров не видны).
Режим direct: берёт собранный combined.m3u (ссылки провайдеров напрямую).
Оставляет каналы, выбранные в плейлисте (rules.json -> lists), и пишет <pl_dir>/<token>.m3u8; nginx отдаёт их по /pl/.
Запуск: после пересборки (e4tv-build) и при сохранении плейлистов (e4tv-lists.path), а также раз в час.
"""
import json, os, re, ssl, sys, time, urllib.request, urllib.parse, glob, grp, hashlib, base64, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4

RULES = E4["editor_dir"] + "/rules.json"
FINAL = E4["editor_dir"] + "/final.json"
STATUS = E4["editor_dir"] + "/lists-status.json"
OUTDIR = E4["pl_dir"]
CONF = E4["hls_dir"] + "/local.json"
MAIN = E4["main_token"] if re.fullmatch(r"[A-Za-z0-9_-]{16,64}", str(E4.get("main_token") or "")) else None
TOK = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{2,31}$")
PASS = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
ADULT_GROUPS = {"18+"}


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


def master():
    if E4["mode"] != "hls":   # без HLS Proxy: собранный плейлист как есть
        with open(E4["combined"], encoding="utf-8", errors="replace") as f: return f.read()
    c = json.load(open(CONF, encoding="utf-8"))
    S = c["SERVER"]; key = list(c["clients"])[0]; cl = c["clients"][key]
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    last = None
    for user in dict.fromkeys([key, cl.get("name")]):
        if not user: continue
        url = f"{S.get('protocol') or 'https'}://127.0.0.1:{S.get('port', 443)}/m3u8?username={urllib.parse.quote(str(user))}&password={urllib.parse.quote(cl['password'])}"
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


def main():
    rules = rj(RULES, {}) or {}
    alll = [x for x in (rules.get("lists") or []) if isinstance(x, dict) and TOK.match(str(x.get("token", "")))]
    # выключенный или просроченный плейлист: файла нет, ссылка отдаёт 404
    lists = [x for x in alll if x.get("enabled") is not False and not expired(x)]
    AUTH = f"{OUTDIR}/auth"
    fmap = rj(FINAL, {}) or {}
    os.makedirs(OUTDIR, exist_ok=True)
    try: wgid = grp.getgrnam("www-data").gr_gid
    except KeyError: wgid = -1
    os.chown(OUTDIR, 0, wgid); os.chmod(OUTDIR, 0o750)
    os.makedirs(AUTH, exist_ok=True); os.chown(AUTH, 0, wgid); os.chmod(AUTH, 0o750)
    st = {"at": time.strftime("%d.%m.%Y %H:%M:%S"), "lists": {}, "error": None,
          "expired": [str(x.get("id")) for x in alll if expired(x)]}
    keep, keep_auth = set(), set()
    FREE = [x for x in (rules.get("free_sources") or []) if isinstance(x, dict) and re.fullmatch(r"[a-z0-9]{1,16}", str(x.get("id", ""))) and str(x.get("url", "")).startswith(("http://", "https://", "e4://"))]
    FCACHE = {}
    for fs in FREE:   # подборки собираем всегда — чтобы редактор мог показать их каналы
        if fs["url"].startswith("e4://"):
            FCACHE[fs["id"]] = free_items(fs)
            ps = preset_stat(fs["url"][5:])
            if ps: st.setdefault("presets", {})[fs["url"][5:]] = ps
    if lists or MAIN:
        t0 = time.time()
        while True:   # после перезапуска HLS Proxy плейлист появляется не сразу
            try:
                head, items = parse(master())
                if not fmap or len(items) >= 0.8 * len(fmap) or time.time() - t0 > WAIT: break
            except Exception as e:
                if time.time() - t0 > WAIT: st["error"] = str(e); items = None; break
            time.sleep(5)
        if items is not None and MAIN:   # основной плейлист: всё, что собрано
            fn = f"{OUTDIR}/{MAIN}.m3u8"; tmp = fn + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: f.write("\n".join([head] + [l for it in items for l in it["lines"]]) + "\n")
            os.chown(tmp, 0, wgid); os.chmod(tmp, 0o640); os.replace(tmp, fn); keep.add(fn)
            st["main"] = {"channels": len(items)}
        if items is not None:
            for L in lists:
                G, C, X = set(L.get("groups") or []), set(L.get("channels") or []), set(L.get("exclude") or [])
                SRCS = set(L.get("sources") or [])   # пусто = все источники, кроме «только для плейлистов»
                LO = set(rules.get("lists_only_sources") or [])
                ALLG, XG = L.get("all") is True, set(L.get("xgroups") or [])
                num = L.get("chno") if isinstance(L.get("chno"), int) else None
                out, miss, groups, sel_items = [head], 0, set(), []
                for it in items:
                    m = fmap.get(it["name"].lower())
                    if not m: miss += 1; continue
                    k, g = m[0], m[1]
                    srcs = set(m[2]) if len(m) > 2 else set()
                    if k in C and k not in X: pass   # канал выбран вручную — берём из любого источника
                    elif SRCS:
                        if srcs and not (SRCS & srcs): continue
                    elif srcs and LO and srcs <= LO: continue   # канал есть только в источниках «только для плейлистов»
                    if L.get("no_adult") and g in ADULT_GROUPS: continue   # детский профиль: 18+ никогда
                    ing = (g not in XG) if ALLG else (g in G)
                    if not ((ing and k not in X) or k in C): continue
                    sel_items.append((k, it))
                FG = set(L.get("fgroups") or [])   # «<id>:<группа>» отдельных плейлистов целиком
                fids = {x.split(":", 2)[1] for x in C | X if x.startswith("f:")} | {x.split(":", 1)[0] for x in FG}
                for fs in [f for f in FREE if f["id"] in fids]:
                    for it in FCACHE.setdefault(fs["id"], free_items(fs)):
                        k = it["k"]
                        if k in X or (L.get("no_adult") and "18+" in it["g"]): continue
                        if k in C or f"{fs['id']}:{it['g']}" in FG: sel_items.append((k, it))
                ORD = {kk: i for i, kk in enumerate(L.get("order") or [])}
                if ORD: sel_items.sort(key=lambda x: ORD.get(x[0], 10**6))   # свой порядок, остальные — как в основном
                FE = rules.get("free_edits") if isinstance(rules.get("free_edits"), dict) else {}
                for k, it in sel_items:
                    lines = list(it["lines"])
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
                    out += lines
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
                st["lists"][str(L.get("id"))] = {"channels": sum(1 for x in out if x.startswith("#EXTINF")), "groups": len(groups), "unmatched": miss}
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
    rc = 0
    for _ in range(3):   # если листы сохранили ещё раз, пока шла сборка, — собрать заново
        m0 = os.path.getmtime(RULES) if os.path.exists(RULES) else 0
        rc = main()
        if (os.path.getmtime(RULES) if os.path.exists(RULES) else 0) == m0: break
    sys.exit(rc)
