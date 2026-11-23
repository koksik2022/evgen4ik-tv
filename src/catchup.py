"""Evgen4ik TV — ссылка на запись (архив) канала по правилам провайдера.

Плеер просит архив у сервера одинаково: ?utc=<начало>&lutc=<сейчас> (M3U, «перенаправление») или /timeshift/… (Xtream).
А провайдеры отдают архив по-разному — это видно по меткам в их плейлисте (#EXTINF):
  shift / archive / только tvg-rec   -> ссылка + ?utc=<начало>&lutc=<сейчас>          (Edem, iLook, VipLime, Antifriz, CBilling, 1OTT…)
  append + catchup-source="?…"         -> ссылка + catchup-source с подставленным временем (Wink, Zabava, MyOTT…)
  default + catchup-source="http…"     -> catchup-source целиком с подставленным временем
  flussonic / flussonic-hls / fs       -> …/index.m3u8 -> …/timeshift_rel-<сек назад>.m3u8,
                                          …/mono.m3u8   -> …/mono-timeshift_rel-<сек назад>.m3u8   (TVTeam/Troya и др.)
  flussonic-ts                         -> …/mpegts      -> …/timeshift_abs-<начало>.ts             (Sharavoz)
Нет меток типа — по адресу: spr24.net (Sharavoz) и troya/tvdealers/1usd (TVTeam) — flussonic, остальные — shift.
Форматы сверены с дополнением httpTimeshift для SimpleTV и описанием catchup в Kodi IPTV Simple.
Только стандартная библиотека Python.
"""
import re, time, urllib.parse

ATTR_RX = re.compile(r'([\w-]+)="([^"]*)"')
FLUSSONIC = ("flussonic", "flussonic-hls", "flussonic-ts", "fs")
SHIFT = ("shift", "archive", "")
# адрес провайдера -> тип архива, если в плейлисте метки типа нет
HOSTS = ((re.compile(r"(^|\.)spr24\.net$|sharavoz", re.I), "flussonic"),
         (re.compile(r"(^|\.)(troya|tvdealers|1usd)\.[a-z]+$", re.I), "flussonic"))


def spec(l0):
    """Правило архива из строки #EXTINF: {"t": тип, "s": catchup-source} или None (архива нет / обычный ?utc&lutc).
    Возвращается только то, что отличается от обычного ?utc=…&lutc=… — его сервер умеет и так."""
    a = {k.lower(): v for k, v in ATTR_RX.findall(l0 or "")}
    t = (a.get("catchup") or a.get("catchup-type") or "").strip().lower()
    s = (a.get("catchup-source") or "").strip()
    if t in ("0", "none", "disabled", "no", "vod"): return None
    if t in FLUSSONIC: return {"t": t, "s": ""}
    if t in ("default", "append") and s: return {"t": t, "s": s[:500]}
    return None


def kind(url, sp):
    """Тип архива: из правила плейлиста, иначе по адресу провайдера, иначе shift."""
    if sp and sp.get("t"): return sp["t"]
    host = urllib.parse.urlsplit(url).hostname or ""
    for rx, t in HOSTS:
        if rx.search(host): return t
    return "shift"


def fill(tpl, start, now, dur=None):
    """Подставить время в шаблон catchup-source: ${start} {utc} {start}, ${timestamp} {lutc} {now} {timestamp},
    ${offset} {offset}, ${duration} {duration}, ${end} {utcend} {end}, {Y} {m} {d} {H} {M} {S} (начало, UTC)."""
    dur = dur if dur and dur > 0 else max(60, now - start)
    g = time.gmtime(start)
    v = {"start": start, "utc": start, "timestamp": now, "lutc": now, "now": now, "offset": max(0, now - start),
         "duration": dur, "length": dur, "end": start + dur, "utcend": start + dur,
         "Y": f"{g.tm_year:04d}", "m": f"{g.tm_mon:02d}", "d": f"{g.tm_mday:02d}", "H": f"{g.tm_hour:02d}", "M": f"{g.tm_min:02d}", "S": f"{g.tm_sec:02d}"}
    return re.sub(r"\$?\{(\w+)(?::\d+)?\}", lambda m: str(v[m.group(1)]) if m.group(1) in v else m.group(0), tpl)


def _join(url, q):
    if not q: return url
    if q[0] in "?&": q = q[1:]
    return url + ("&" if "?" in url else "?") + q


def flussonic(url, start, now, ts=False):
    """Flussonic: …/index.m3u8 -> …/timeshift_rel-N.m3u8, …/mono.m3u8 -> …/mono-timeshift_rel-N.m3u8,
    …/mpegts или …/*.ts -> …/timeshift_abs-<начало>.ts. Параметры (?token=…) сохраняются. Не узнали адрес — None."""
    p = urllib.parse.urlsplit(url)
    path = p.path
    m = re.match(r"^(.*/)([^/]+)$", path)
    if not m: return None
    d, name = m.groups()
    if ts or name == "mpegts" or name.endswith(".ts"):
        path = f"{d}timeshift_abs-{start}.ts"
    elif name.endswith(".m3u8"):
        base = name[:-5]
        rel = max(1, now - start)
        path = f"{d}timeshift_rel-{rel}.m3u8" if base in ("index", "playlist") else f"{d}{base}-timeshift_rel-{rel}.m3u8"
    else:
        return None
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, p.query, p.fragment))


def archive_url(url, sp, start, now=None, dur=None):
    """Ссылка на запись с начала start (секунды UTC) по правилу sp (из spec) для ссылки провайдера url."""
    now = int(now or time.time()); start = int(start)
    t = kind(url, sp)
    if t in FLUSSONIC:
        u = flussonic(url, start, now, ts=(t == "flussonic-ts"))
        if u: return u
        t = "shift"
    if t == "default" and sp and re.match(r"^https?://", sp.get("s", "")):
        return fill(sp["s"], start, now, dur)
    if t in ("append", "default") and sp and sp.get("s"):
        return _join(url, fill(sp["s"], start, now, dur))
    return _join(url, f"utc={start}&lutc={now}")
