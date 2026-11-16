#!/usr/bin/env python3
# Evgen4ik TV: сборка общего плейлиста из источников редактора — единые группы, tvg-id (из EPG it999), правила редактора
import re, os, sys, gzip, zlib, urllib.request, collections, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as CONF

SRC = []   # встроенных источников нет: свои ссылки добавляются в редакторе («Источники»)
IT999 = "http://epg.it999.ru/edem.xml.gz"
EPG_HDR = CONF["epg_url"]
DIR = CONF["build_dir"]
OUT = sys.argv[sys.argv.index("--out")+1] if "--out" in sys.argv else CONF["combined"]
DRY = "--dry" in sys.argv
# Группы, которые не попадают в итоговый лист (точное имя или префикс с "*")
PRIMARY_SRC = ""   # основной источник у дублей выбирается в редакторе
EXCLUDE_GROUPS = []
UA = {"User-Agent": "VLC/3.0.20 LibVLC/3.0.20"}

COUNTRY = {"UK":"Великобритания","GB":"Великобритания","US":"США","DE":"Германия","AT":"Австрия","CH":"Швейцария","FR":"Франция","ES":"Испания","PT":"Португалия","IT":"Италия","NL":"Нидерланды","BE":"Бельгия","PL":"Польша","CZ":"Чехия","SK":"Словакия","HU":"Венгрия","RO":"Румыния","BG":"Болгария","GR":"Греция","TR":"Турция","IL":"Израиль","AE":"ОАЭ","QA":"Катар","SA":"Саудовская Аравия","EG":"Египет","IN":"Индия","PK":"Пакистан","CN":"Китай","JP":"Япония","KR":"Корея","SE":"Швеция","NO":"Норвегия","DK":"Дания","FI":"Финляндия","EE":"Эстония","LV":"Латвия","LT":"Литва","BY":"Беларусь","UA":"Украина","MD":"Молдова","GE":"Грузия","AM":"Армения","AZ":"Азербайджан","KZ":"Казахстан","UZ":"Узбекистан","KG":"Киргизия","TJ":"Таджикистан","TM":"Туркменистан","RS":"Сербия","HR":"Хорватия","BA":"Босния","SI":"Словения","MK":"Северная Македония","ME":"Черногория","AL":"Албания","XK":"Косово","CY":"Кипр","IE":"Ирландия","BR":"Бразилия","AR":"Аргентина","MX":"Мексика","CA":"Канада","AU":"Австралия","NZ":"Новая Зеландия","IR":"Иран","IQ":"Ирак","AF":"Афганистан","BD":"Бангладеш","TH":"Таиланд","VN":"Вьетнам","ID":"Индонезия","PH":"Филиппины","MY":"Малайзия","ZA":"ЮАР","NG":"Нигерия","MA":"Марокко","LB":"Ливан","JO":"Иордания","SY":"Сирия","KW":"Кувейт","CL":"Чили","CO":"Колумбия","PE":"Перу","IS":"Исландия","LU":"Люксембург","MT":"Мальта"}
BRANDS = [("VF ","VF"),("MM ","MiniMax"),("BCU ","BCU"),("BOX ","BOX"),("Liberty ","Liberty"),("Sky High ","Sky High"),("Yosso","Yosso"),("CineMan ","CineMan"),("Velilla","Velilla"),("VeleS ","VeleS"),("TVPlay ","TVPlay"),("Venera.TV","Venera.TV"),("StrahTV","StrahTV"),("Tviksel","Tviksel"),("Z!","Z!"),("KinoLiving","KinoLiving"),("FilmBox","FilmBox")]
FED = ["первый канал","россия 1","матч!","матч тв","нтв","пятый канал","россия к","россия-культура","россия культура","культура","россия 24","карусель","отр","тв центр","тв-центр","твц","рен тв","спас","стс","домашний","тв-3","тв3","пятница!","пятница","звезда","мир","тнт","муз-тв","муз тв","суббота!","суббота","ю","че","2x2","2х2","солнце","тнт4","тнт 4","стс love","ru.tv","мир 24","че!","ю тв","нтв мир","первый канал европа","тв центр international","тв центр международный","звезда плюс","рт","rt"]
ADULT = {"взрослые","xxx (18+)","18+","для взрослых"}
SRCMAP = {"новости":"Новости","кино":"Кино и сериалы","музыка":"Музыка","музыкальные":"Музыка","познавательные":"Познавательные","детские":"Детские","развлекательные":"Развлекательные","спорт":"Спорт","общие":"Эфирные"}
KW = [
 ("Спорт", r"спорт|sport|\bspor\b|матч!|футбол|football|хоккей|кхл|setanta|eurosport|arena|espn|dazn|\bnba\b|\bnfl\b|\bufc\b|\bfight|golf|tennis|теннис|racing|\bf1\b|moto|laliga|bundesliga|premier league|окко event|megogo спорт|старт|бокс"),
 ("Детские", r"kids|детск|мульт|cartoon|nick|disney|baby|малыш|карусель|tiji|gulli|boomerang|jim jam|уникум|cartoonito|duck|рыжий|смайлик|о!"),
 ("Новости", r"news|новост|известия|\bрбк\b|\bcnn\b|euronews|al jazeera|bloomberg|cnbc|deutsche welle|france 24|россия 24|360|соловь|вести|\b24\b"),
 ("Музыка", r"music|муз|\bmtv\b|hits|шансон|europa plus|ru\.tv|bridge|\bvh1\b|jukebox|dance|\brock|ля-минор|караоке|ретро|клип|\bфм\b|\bfm\b"),
 ("Познавательные", r"rt док|discovery|national geographic|nat ?geo|history|истори|animal|explore|nature|природ|наука|science|познав|travel|путеш|food|\bеда\b|кухня|охот|рыбал|дача|здоров|арсенал|моя стихия|planet|docu|доку|оружие|авто|техно|космо"),
 ("Кино и сериалы", r"кино|cinema|film|movie|сериал|serial|series|\bkino\b|tv1000|viju|\bhbo\b|\bamc\b|paramount|ленфильм|мосфильм|комеди|иллюзион|феникс|bollywood|drama|дорам|триллер|детектив|ужас|horror|action|thriller|vhs|premiere|премьер"),
 ("Развлекательные", r"квн|тнт|\bстс\b|пятница|юмор|comedy|развлек|fashion|show|шоу|мужск|женск|\btlc\b|life|relax|камин|extreme|home|lo-fi|fun"),
]

def fetch(url, t=120):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=t) as x:
        return x.read()

def get_src(name, url):
    cp = f"{DIR}/cache/{name}.m3u"
    try:
        b = fetch(url)
        if b"#EXTINF" not in b: raise ValueError("no #EXTINF")
        open(cp, "wb").write(b); return b.decode("utf-8", "replace"), "загружен"
    except Exception as e:
        if os.path.exists(cp): return open(cp, encoding="utf-8", errors="replace").read(), f"из прошлой копии — источник не ответил ({e})"
        raise

def it999_ids():
    cp = f"{DIR}/cache/it999_channels.txt"
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS); buf = ""
        with urllib.request.urlopen(urllib.request.Request(IT999, headers=UA), timeout=120) as x:
            while True:
                ch = x.read(1 << 16)
                if not ch: break
                buf += d.decompress(ch).decode("utf-8", "replace")
                if "<programme" in buf: break
        m = {}
        for cid, body in re.findall(r'<channel id="([^"]+)">(.*?)</channel>', buf, re.S):
            for n in re.findall(r"<display-name[^>]*>([^<]+)<", body):
                m.setdefault(n.strip().lower(), cid)
        if len(m) > 500:
            open(cp, "w", encoding="utf-8").write("\n".join(f"{k}\t{v}" for k, v in m.items()))
        return m, "fresh"
    except Exception as e:
        if os.path.exists(cp):
            return dict(l.split("\t", 1) for l in open(cp, encoding="utf-8").read().splitlines() if "\t" in l), f"CACHE ({e})"
        return {}, f"FAIL ({e})"

INF = re.compile(r'#EXTINF:\s*(-?\d+)((?:\s+[\w-]+="[^"]*")*)\s*,(.*)$')
def parse(text, src):
    items, cur = [], None
    for l in text.replace("\r", "").split("\n"):
        l = l.strip()
        if l.startswith("#EXTINF"):
            m = INF.match(l)
            if not m: cur = None; continue
            cur = {"src": src, "dur": m.group(1), "attrs": dict(re.findall(r'([\w-]+)="([^"]*)"', m.group(2))), "name": m.group(3).strip(), "extra": []}
            cur["grp"] = cur["attrs"].pop("group-title", "")
        elif l.startswith("#EXTGRP:") and cur: cur["grp"] = l[8:].strip()
        elif l.startswith("#EXTM3U") or not l: continue
        elif l.startswith("#") and cur: cur["extra"].append(l)
        elif cur: cur["url"] = l; items.append(cur); cur = None
    return items

def country(n):
    t = n.split()
    return t[-1] if t and t[-1] in COUNTRY else None

def base(n):
    s = re.sub(r"\(.*?\)", " ", n.lower().replace("ё", "е"))
    t = [x for x in s.split() if x not in {"hd","fhd","uhd","4k","hdr","orig","sd","50","60","fps","hevc","premium+","premium"} and not re.fullmatch(r"\+\d+", x)]
    if t and t[-1].upper() in COUNTRY: t = t[:-1]
    return " ".join(t).strip()

def brand(n):
    for p, b in BRANDS:
        if n.startswith(p): return b
    return None

def kw_genre(n):
    s = n.lower().replace("ё", "е")
    for g, rx in KW:
        if re.search(rx, s): return g
    return None

def region(n):
    m = re.search(r"\(([^)]+)\)", n)
    if not m: return None
    r = m.group(1).strip()
    if re.search(r"18\+|триколор|орбита|europe|европа|int|\+\d|мск|msk|hd|^\d", r, re.I): return None
    return r

# ---------- Редактор: правила и снимок каналов ----------
EDITOR_DIR = CONF["editor_dir"]
RULES_FILE = f"{EDITOR_DIR}/rules.json"
STATE_FILE = f"{EDITOR_DIR}/state.json"
FINAL_FILE = f"{EDITOR_DIR}/final.json"   # итоговое имя -> [ключ, группа]; по нему lists.py собирает «листы»
SNAP = {}
URL_RX = re.compile(r"^(https?|rtmp|rtsp|udp|rtp)://[^\s\"]{1,2000}$", re.I)
FILE_RX = re.compile(r"^file://" + re.escape(CONF["uploads"]) + r"/[a-z0-9]{4,16}\.m3u$")   # загруженный файл .m3u

def _urls(v):
    if not isinstance(v, list): return []
    return [u.strip() for u in v[:10] if isinstance(u, str) and URL_RX.match(u.strip())]

def _clean(s, n=100):
    return re.sub(r'[\x00-\x1f\x7f"]', " ", str(s)).strip()[:n]

def load_rules(log):
    """None = файла нет или он битый -> работаем по EXCLUDE_GROUPS, как раньше."""
    if not os.path.exists(RULES_FILE): return None
    try:
        r = json.load(open(RULES_FILE, encoding="utf-8"))
        if not isinstance(r, dict): raise ValueError("not an object")
        out = {"hidden_patterns": [], "groups": {}, "channels": {}, "group_order": [], "custom_channels": [],
               "sources": [], "disabled_sources": [], "primary_source": None, "channel_order": {}, "auto_rules": []}
        for p in (r.get("hidden_patterns") or [])[:300]:
            if isinstance(p, str) and p.strip(): out["hidden_patterns"].append(_clean(p))
        for k, v in list((r.get("groups") or {}).items())[:3000]:
            if not isinstance(k, str) or not isinstance(v, dict): continue
            rn = v.get("rename"); hd = v.get("hidden")
            out["groups"][_clean(k)] = {"rename": _clean(rn) if isinstance(rn, str) and rn.strip() else None,
                                        "hidden": hd if isinstance(hd, bool) else None}
        for k, v in list((r.get("channels") or {}).items())[:30000]:
            if not isinstance(k, str) or not isinstance(v, dict): continue
            rn = v.get("rename"); gr = v.get("group"); hd = v.get("hidden")
            out["channels"][k.lower()[:200]] = {"rename": _clean(rn) if isinstance(rn, str) and rn.strip() else None,
                                                "group": _clean(gr) if isinstance(gr, str) and gr.strip() else None,
                                                "hidden": hd if isinstance(hd, bool) else None,
                                                "urls": _urls(v.get("urls")),
                                                "tvg_id": _clean(v["tvg_id"]) if isinstance(v.get("tvg_id"), str) and v["tvg_id"].strip() else None,
                                                "logo": v["logo"].strip() if isinstance(v.get("logo"), str) and URL_RX.match(v["logo"].strip()) else None,
                                                "merge": [m.lower()[:200] for m in (v.get("merge") or [])[:30] if isinstance(m, str) and m.strip()]}
        for cc in (r.get("custom_channels") or [])[:500]:
            if not isinstance(cc, dict): continue
            nm, gr, us = cc.get("name"), cc.get("group"), _urls(cc.get("urls"))
            if not (isinstance(nm, str) and nm.strip() and isinstance(gr, str) and gr.strip() and us): continue
            lg = cc.get("logo").strip() if isinstance(cc.get("logo"), str) else ""
            out["custom_channels"].append({"name": _clean(nm), "group": _clean(gr), "urls": us,
                                           "logo": lg if URL_RX.match(lg) else "", "tvg_id": _clean(cc.get("tvg_id") or "")})
        names = {n for n, _ in SRC}
        for x in (r.get("sources") or [])[:10]:
            if not isinstance(x, dict): continue
            nm, u, sid = x.get("name"), x.get("url"), str(x.get("id") or "")
            if not (isinstance(nm, str) and nm.strip() and isinstance(u, str) and (URL_RX.match(u.strip()) or FILE_RX.match(u.strip())) and re.fullmatch(r"[a-z0-9]{1,16}", sid)): continue
            nm = _clean(nm, 40)
            if nm in names or nm == "Свой": continue
            names.add(nm)
            out["sources"].append({"id": sid, "name": nm, "url": u.strip(), "enabled": x.get("enabled") is not False})
        out["disabled_sources"] = [_clean(x, 40) for x in (r.get("disabled_sources") or [])[:20] if isinstance(x, str)]
        ps = r.get("primary_source")
        out["primary_source"] = _clean(ps, 40) if isinstance(ps, str) and ps.strip() else None
        for ar in (r.get("auto_rules") or [])[:200]:   # постоянные правила: переименование и группа по шаблону
            if not isinstance(ar, dict) or ar.get("t") not in ("rename", "group") or not isinstance(ar.get("find"), str) or not ar["find"]: continue
            try: pat = re.compile(ar["find"] if ar.get("rx") else re.escape(ar["find"]), re.I)
            except re.error as e: log.append(f"Правило «{ar['find']}» пропущено: ошибка в выражении ({e})"); continue
            if ar["t"] == "rename": out["auto_rules"].append(("rename", pat, _clean(ar.get("repl") or "")))
            elif isinstance(ar.get("group"), str) and ar["group"].strip(): out["auto_rules"].append(("group", pat, _clean(ar["group"])))
        for g, ks in list((r.get("channel_order") or {}).items())[:3000]:
            if isinstance(g, str) and isinstance(ks, list):
                out["channel_order"][_clean(g)] = {k.lower()[:200]: i for i, k in enumerate(ks[:5000]) if isinstance(k, str)}
        out["group_order"] = [_clean(g) for g in (r.get("group_order") or [])[:3000] if isinstance(g, str) and g.strip()]
        sl = r.get("stream_limit")   # 0 = без лимита
        out["stream_limit"] = sl if isinstance(sl, int) and not isinstance(sl, bool) and 0 <= sl <= 50 else None
        log.append(f"Правила редактора: групп {len(out['groups'])}, каналов {len(out['channels'])}, шаблонов скрытия {len(out['hidden_patterns'])}, своих каналов {len(out['custom_channels'])}, замен ссылок {sum(1 for v in out['channels'].values() if v['urls'])}")
        return out
    except Exception as e:
        log.append(f"ОШИБКА в {RULES_FILE} ({e}) — правила не применены, работаю по EXCLUDE_GROUPS")
        return None

def _pat(g, pats):
    return any(g.startswith(x[:-1]) if x.endswith("*") else g == x for x in pats)

def _auto_name(name, R):
    n0 = name
    for t, pat, val in R["auto_rules"]:
        if t == "rename": name = re.sub(r"\s{2,}", " ", pat.sub(lambda m: val, name)).strip()
    return name or n0

def _auto_group(name, R):
    for t, pat, val in R["auto_rules"]:
        if t == "group" and pat.search(name): return val
    return None

def apply_rules(allc, R):
    keep, dropped, done = [], 0, set()
    alias = {}   # склеенные каналы: ключ -> ключ основного
    for mk, v in R["channels"].items():
        for a in v.get("merge") or []:
            if a != mk: alias.setdefault(a, mk)
    first = {}
    for c in allc: first.setdefault(c["name"].lower(), (c["name"], c["new"], dict(c["attrs"])))
    for c in allc:
        k = c["name"].lower()
        if k in alias and alias[k] in first:   # становится резервным потоком основного канала
            mk = alias[k]; mn, mg, ma = first[mk]
            c["name"], c["new"], k = mn, mg, mk
            for a in ("tvg-id", "tvg-logo", "tvg-name"):
                if ma.get(a): c["attrs"][a] = ma[a]
                else: c["attrs"].pop(a, None)
        c["k"] = k
        cr = R["channels"].get(k, {})
        if cr.get("group"): c["new"] = cr["group"]
        else:
            ag = _auto_group(c["name"], R)
            if ag: c["new"] = ag
        gr = R["groups"].get(c["new"], {})
        ghid = gr["hidden"] if gr.get("hidden") is not None else _pat(c["new"], R["hidden_patterns"])
        if cr.get("hidden") or ghid: dropped += 1; continue
        if cr.get("rename"): c["name"] = cr["rename"]
        elif R["auto_rules"]: c["name"] = _auto_name(c["name"], R)
        if cr.get("tvg_id"): c["attrs"]["tvg-id"] = cr["tvg_id"]
        if cr.get("logo"): c["attrs"]["tvg-logo"] = cr["logo"]
        c["out"] = gr.get("rename") or c["new"]
        if cr.get("urls"):   # свои ссылки вместо всех исходных; первая — основная, остальные — резерв
            if k in done: continue
            done.add(k)
            for u in cr["urls"]:
                d = dict(c); d["attrs"] = dict(c["attrs"]); d["url"] = u; d["src"] = "Свой"; keep.append(d)
            continue
        keep.append(c)
    return keep, dropped

def write_state(allc, auto_order, sortkey):
    ch = {}
    for c in sorted(allc, key=sortkey):
        k = c["name"].lower(); e = ch.get(k)
        if not e:
            ch[k] = {"k": k, "n": c["name"], "g": c["new"], "src": [c["src"]],
                     "logo": c["attrs"].get("tvg-logo", ""), "id": c["attrs"].get("tvg-id", ""), "u": [[c["src"], c["url"]]]}
        else:
            if c["src"] not in e["src"]: e["src"].append(c["src"])
            if len(e["u"]) < 10 and [c["src"], c["url"]] not in e["u"]: e["u"].append([c["src"], c["url"]])
            if not e["logo"]: e["logo"] = c["attrs"].get("tvg-logo", "")
    SNAP.update({"channels": list(ch.values()), "auto_order": auto_order, "default_hidden": EXCLUDE_GROUPS})

def _editor_gid():
    try:
        import grp; return grp.getgrnam(CONF["editor_group"]).gr_gid
    except Exception: return -1

def finish_state(log, final_groups, allc, final=True):
    if not SNAP: return
    try:
        os.makedirs(EDITOR_DIR, exist_ok=True)
        SNAP.update({"built_at": time.strftime("%d.%m.%Y %H:%M:%S"), "log": log, "dry": DRY, "final_groups": final_groups})
        # в снимке есть ссылки источников (с токенами) -> читать может только root и редактор
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(SNAP, f, ensure_ascii=False)
        os.chmod(tmp, 0o640); os.chown(tmp, 0, _editor_gid()); os.replace(tmp, STATE_FILE)
        if not DRY and final:
            fm = {}
            for c in allc:
                e = fm.setdefault(c["name"].lower(), [c.get("k", c["name"].lower()), c["new"], []])
                if c["src"] not in e[2]: e[2].append(c["src"])
            tmp = FINAL_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(fm, f, ensure_ascii=False)
            os.chmod(tmp, 0o644); os.replace(tmp, FINAL_FILE)
    except Exception as e:
        print(f"state.json не записан: {e}")

NO_LIMIT = 999   # «без лимита» в local.json HLS Proxy (как он понимает 0 — не проверено, поэтому большое число)

def apply_stream_limit(R, log):
    """Режим hls: лимит одновременных каналов из редактора -> local.json HLS Proxy (перезапуск — после сборки)."""
    if CONF["mode"] != "hls": return
    p = CONF["hls_dir"] + "/local.json"
    try: c = json.load(open(p, encoding="utf-8"))
    except Exception: return
    cur = c.get("maxSimultaneousStreamCountPerClientDefault")
    SNAP["stream_limit"] = 0 if isinstance(cur, int) and cur >= NO_LIMIT else cur   # 0 = без лимита
    n = (R or {}).get("stream_limit")
    if not isinstance(n, int) or DRY: return
    hn = n or NO_LIMIT   # 0 в редакторе = без лимита: в HLS Proxy заведомо большое число
    cls = [v for k, v in (c.get("clients") or {}).items() if isinstance(v, dict) and not k.startswith("e4c_")]   # e4c_* — логины клиентов, их лимит свой
    if c.get("maxSimultaneousStreamCountPerClientDefault") == hn and all(v.get("maxStreamCount") == hn for v in cls): return
    c["maxSimultaneousStreamCountPerClientDefault"] = hn
    for v in cls: v["maxStreamCount"] = hn
    try:
        st = os.stat(p); tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(c, f, ensure_ascii=False, indent=1)
        os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, 0o600); os.replace(tmp, p)
        SNAP["stream_limit"] = n; log.append("HLS Proxy: без лимита каналов" if n == 0 else f"HLS Proxy: не больше {n} каналов одновременно")
    except Exception as e:
        log.append(f"Лимит каналов не записан: {e}")

def write_epg(ids):
    """Список каналов программы it999 для выбора EPG в редакторе: [[id, название], ...]."""
    if len(ids) < 500: return
    try:
        inv = {}
        for n, i in ids.items(): inv.setdefault(i, n)
        tmp = f"{EDITOR_DIR}/epg.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(sorted(inv.items(), key=lambda x: x[1]), f, ensure_ascii=False)
        os.chmod(tmp, 0o644); os.replace(tmp, f"{EDITOR_DIR}/epg.json")
    except Exception as e:
        print(f"epg.json не записан: {e}")

def main():
    log = []
    os.makedirs(f"{DIR}/cache", mode=0o700, exist_ok=True)
    allc = []
    RULES = load_rules(log)
    apply_stream_limit(RULES, log)
    R0 = RULES or {}
    prim = R0.get("primary_source") or PRIMARY_SRC
    srcs = [(n, u, n, True) for n, u in SRC if n not in R0.get("disabled_sources", [])]
    srcs += [(x["name"], x["url"], "x_" + x["id"], False) for x in R0.get("sources", []) if x["enabled"]]
    sinfo = [{"name": n, "count": 0, "status": "выключен"} for n, _ in SRC if n in R0.get("disabled_sources", [])]
    # слежение за источниками: если пришло меньше половины от прошлого раза — берём прошлую рабочую копию
    STATS_FILE = f"{EDITOR_DIR}/source-stats.json"
    try: stats = json.load(open(STATS_FILE, encoding="utf-8"))
    except Exception: stats = {}
    FORCE = os.path.exists(f"{EDITOR_DIR}/force.flag")
    warns = []
    for name, url, ck, builtin in srcs:
        try:
            txt, st = get_src(ck, url); items = parse(txt, name)
            prev = stats.get(ck, {}).get("count", 0)
            gp = f"{DIR}/cache/{ck}.good.m3u"
            if prev >= 50 and len(items) < prev * 0.5 and not FORCE and os.path.exists(gp):
                w = f"{name}: пришло {len(items)} каналов вместо ~{prev} — взята прошлая рабочая копия"
                items = parse(open(gp, encoding="utf-8", errors="replace").read(), name); st = "прошлая рабочая копия"
                warns.append(w); log.append("ПРЕДУПРЕЖДЕНИЕ " + w)
            elif not DRY:
                with open(gp, "w", encoding="utf-8") as f: f.write(txt)
                stats[ck] = {"count": len(items), "at": time.strftime("%d.%m.%Y %H:%M")}
            allc += items
        except Exception as e:
            log.append(f"{name}: ОШИБКА загрузки ({e}) — источник пропущен")
            sinfo.append({"name": name, "count": 0, "status": f"ошибка: {e}"[:200]}); continue
        log.append(f"{name}: {len(items)} каналов ({st})")
        sinfo.append({"name": name, "count": len(items), "status": st[:200]})
    if not srcs:   # первый запуск: источников ещё нет — пустой снимок, чтобы редактор открылся
        log.append("Источников пока нет. Откройте «Источники» в редакторе и добавьте ссылку на плейлист провайдера.")
        SNAP.update({"channels": [], "auto_order": [], "default_hidden": EXCLUDE_GROUPS, "warnings": [], "sources": sinfo,
                     "builtin_sources": [], "primary_source": prim})
        if not os.path.exists(OUT):
            with open(OUT, "w", encoding="utf-8") as f: f.write("#EXTM3U\n")
        finish_state(log, [], []); print("\n".join(log)); return
    if not allc:   # все источники с ошибкой: показать ошибку в редакторе, старый плейлист не трогать
        SNAP.update({"channels": [], "auto_order": [], "default_hidden": EXCLUDE_GROUPS, "warnings": [], "sources": sinfo,
                     "builtin_sources": [], "primary_source": prim})
        finish_state(log, [], [], final=False)
        raise SystemExit("Нет ни одного канала ни из одного источника — плейлист не тронут")
    if not DRY:
        try:
            with open(STATS_FILE + ".tmp", "w", encoding="utf-8") as f: json.dump(stats, f, ensure_ascii=False)
            os.replace(STATS_FILE + ".tmp", STATS_FILE)
            if FORCE: os.remove(f"{EDITOR_DIR}/force.flag")
        except Exception as e: log.append(f"source-stats.json не записан: {e}")
    ids, st = it999_ids(); log.append(f"it999: {len(ids)} названий ({st})")
    write_epg(ids)
    # словарь жанров по базовому имени из каналов с известной группой
    known = {}
    for c in allc:
        g = SRCMAP.get(c["grp"].lower())
        if g and g != "Эфирные": known.setdefault(base(c["name"]), g)
    fedset = set(FED)
    for c in allc:
        n, sg = c["name"], c["grp"].lower()
        cc = country(n); b = brand(n); g0 = SRCMAP.get(sg)
        genre = g0 if g0 and g0 != "Эфирные" else (known.get(base(n)) or kw_genre(n))
        if sg in ADULT: grp = "18+"
        elif cc and cc != "RU":
            grp = "Спорт: зарубежный" if (genre == "Спорт" or kw_genre(n) == "Спорт") else f"Страна: {COUNTRY[cc]}"
        elif b:
            grp = "Спорт" if kw_genre(n) == "Спорт" else f"Кинозал: {b}"
        elif region(n) and base(n) not in fedset: grp = "Региональные"
        elif base(n) in fedset or sg == "общие": grp = "Эфирные: часовые пояса" if re.search(r"\+\d", n) else "Эфирные"
        elif sg.startswith("кинозал "): grp = "Кинозал: " + c["grp"][8:]
        else: grp = genre or "Разное"
        c["new"] = grp
        if not c["attrs"].get("tvg-id"):
            tid = ids.get(n.lower())
            if tid: c["attrs"]["tvg-id"] = tid; c["attrs"]["tvg-name"] = n
    # мелкие страны и кинозалы — в общие группы
    cnt = collections.Counter(c["new"] for c in allc)
    for c in allc:
        if c["new"].startswith("Страна: ") and cnt[c["new"]] < 8: c["new"] = "Страна: другие"
        if c["new"].startswith("Кинозал: ") and cnt[c["new"]] < 5: c["new"] = "Кинозал: разные"
    ORDER = ["Эфирные","Эфирные: часовые пояса","Новости","Кино и сериалы","Спорт","Детские","Познавательные","Развлекательные","Музыка","Региональные","Разное"]
    def gkey0(g):
        if g in ORDER: return (0, ORDER.index(g), g)
        if g.startswith("Кинозал: "): return (1, 0 if g != "Кинозал: разные" else 1, g)
        if g == "Спорт: зарубежный": return (2, 0, g)
        if g.startswith("Страна: "): return (3, 1 if g == "Страна: другие" else 0, g)
        return (4, 0, g)
    def ckey(c):
        if c["new"].startswith("Эфирные"):
            bb = base(c["name"]); i = FED.index(bb) if bb in FED else 99
            return (i, c["src"] != prim)
        return (0,)
    write_state(allc, sorted({c["new"] for c in allc}, key=gkey0), lambda c: (gkey0(c["new"]), ckey(c)))
    SNAP["warnings"] = warns
    SNAP["sources"] = sinfo; SNAP["builtin_sources"] = [n for n, _ in SRC]; SNAP["primary_source"] = prim
    for cc in (RULES or {}).get("custom_channels", []):   # свои каналы из редактора
        at = {}
        if cc["tvg_id"]: at["tvg-id"] = cc["tvg_id"]
        if cc["logo"]: at["tvg-logo"] = cc["logo"]
        for u in cc["urls"]:
            allc.append({"src": "Свой", "dur": "-1", "attrs": dict(at), "name": cc["name"], "extra": [], "grp": "", "url": u, "new": cc["group"]})
    UORDER = (RULES or {}).get("group_order") or []
    def gkey(g):
        if g in UORDER: return (-1, UORDER.index(g), g)
        return gkey0(g)
    if RULES is not None:
        allc, dropped = apply_rules(allc, RULES)
        log.append(f"Скрыто правилами редактора: {dropped} каналов")
    else:
        dropped = sum(1 for c in allc if _pat(c["new"], EXCLUDE_GROUPS))
        allc = [c for c in allc if not _pat(c["new"], EXCLUDE_GROUPS)]
        log.append(f"Исключено по EXCLUDE_GROUPS: {dropped} каналов")
    seen_urls = set(); uniq = []
    for c in allc:
        if c["url"] in seen_urls: continue
        seen_urls.add(c["url"]); uniq.append(c)
    if len(uniq) != len(allc): log.append(f"Убрано дублей по ссылке: {len(allc) - len(uniq)}")
    allc = uniq
    CORD = (RULES or {}).get("channel_order") or {}
    def okey(c):   # свой порядок каналов в группе (из редактора), остальные — после, по-старому
        o = CORD.get(c["new"])
        if o and c.get("k") in o: return (0, o[c["k"]])
        return (1,) + tuple(ckey(c))
    allc.sort(key=lambda c: (gkey(c["new"]), okey(c)))
    # Резерв: HLS Proxy (isBackupStreams) склеивает каналы с одинаковым именем, первый = основной.
    # Основным делаем PRIMARY_SRC: на занятые дублями позиции ставим его запись раньше остальных.
    pos = collections.defaultdict(list)
    for i, c in enumerate(allc): pos[c["name"].lower()].append(i)   # HLS Proxy сравнивает без учёта регистра
    pairs = 0
    for n, idx in pos.items():
        if len(idx) < 2: continue
        grp = sorted((allc[i] for i in idx), key=lambda c: c["src"] != prim)
        for i, c in zip(idx, grp): allc[i] = c
        pairs += 1
    log.append(f"Каналов с резервом (одинаковое имя): {pairs}, основной источник {prim}")
    out = [f'#EXTM3U url-tvg="{EPG_HDR}"']
    for c in allc:
        a = "".join(f' {k}="{v}"' for k, v in c["attrs"].items())
        out.append(f'#EXTINF:{c["dur"]}{a} group-title="{c.get("out", c["new"])}",{c["name"]}')
        out += c["extra"]; out.append(c["url"])
    tmp = OUT + ".tmp"; open(tmp, "w", encoding="utf-8").write("\n".join(out) + "\n"); os.chmod(tmp, 0o640); os.replace(tmp, OUT)
    fin = collections.Counter(c.get("out", c["new"]) for c in allc)
    log.append(f"Итого: {len(allc)} каналов, {len(fin)} групп -> {OUT}")
    log.append(f"С программой передач (tvg-id): {sum(1 for c in allc if c['attrs'].get('tvg-id'))} из {len(allc)} каналов")
    finish_state(log, [[g, fin[g]] for g in dict.fromkeys(c.get("out", c["new"]) for c in allc)], allc)
    print("\n".join(log))
    if DRY:
        for g in sorted(fin, key=gkey): print(f"  {fin[g]:5d}  {g}")
        rz = [c["name"] for c in allc if c["new"] == "Разное"]
        print("Разное, примеры:", "; ".join(rz[:60]))
        rg = [c["name"] for c in allc if c["new"] == "Региональные"]
        print("Региональные, примеры:", "; ".join(rg[:25]))
        ef = [c["name"] for c in allc if c["new"] == "Эфирные"]
        print("Эфирные, первые:", "; ".join(ef[:40]))

if __name__ == "__main__":
    main()
