#!/usr/bin/env python3
"""Evgen4ik TV: несколько аккаунтов одного провайдера (Xtream) -> настройка «pooler» в HLS Proxy.

Как работает pooler в HLS Proxy (проверено на 8.4.8 с тестовым провайдером):
  * regex с группами ищет в ссылке потока части, которые отличают аккаунт;
  * каждая строка pool — одно подключение; значения через пробел подставляются в группы по порядку,
    «$$» — оставить как в ссылке (основной аккаунт);
  * слоты занимаются по порядку строк; когда все заняты, HLS Proxy пишет «Pool is over» и пускает поток
    через ссылку как есть, то есть через основной аккаунт сверх его лимита. Поэтому общий лимит каналов
    в редакторе должен быть не больше суммы аккаунтов (это делает режим «Авто»).
Только стандартная библиотека; без обращений к сети."""
import json, re, secrets, urllib.parse

CRED_RX = re.compile(r"^[A-Za-z0-9._@!*+=~-]{1,64}$")          # логин/пароль: без пробелов и «/», иначе pool не разобрать
SRV_RX = re.compile(r"^[A-Za-z0-9.-]{1,253}(?::\d{1,5})?$")      # другой сервер того же провайдера: host[:port]
PREFIX = "e4tv-"                                                # наши записи в pooler; остальные не трогаем


def parse_xc(url):
    """Ссылка Xtream (get.php / player_api.php с username и password) -> (scheme, host, port, user, pw) или None."""
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or not re.search(r"/(get|player_api)\.php$", p.path or ""):
        return None
    q = urllib.parse.parse_qs(p.query)
    u, pw = (q.get("username") or [""])[0], (q.get("password") or [""])[0]
    if not (CRED_RX.match(u) and CRED_RX.match(pw)):
        return None
    return p.scheme, p.hostname.lower(), p.port, u, pw


def _int(v, lo, hi, default=None):
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default


def clean_accounts(a, new_id=lambda: secrets.token_hex(4)):
    """Проверка аккаунтов источника: {"max": лимит основного, "list": [{id, name, user, pw, srv, max, prio, on}]} или None.
    Всё, что попадёт в local.json HLS Proxy, проходит строгие шаблоны (без пробелов, кавычек и «/»)."""
    if not isinstance(a, dict): return None
    out, ids = [], set()
    for x in (a.get("list") if isinstance(a.get("list"), list) else [])[:10]:
        if not isinstance(x, dict): continue
        u, p = str(x.get("user") or "").strip(), str(x.get("pw") or "").strip()
        if not (CRED_RX.match(u) and CRED_RX.match(p)): continue
        aid = str(x.get("id") or "")
        if not re.fullmatch(r"[a-z0-9]{1,16}", aid) or aid in ids: aid = new_id()
        ids.add(aid)
        srv = str(x.get("srv") or "").strip().lower()
        nm = re.sub(r"[\x00-\x1f\x7f]", "", str(x.get("name") or "")).strip()[:40] or u
        out.append({"id": aid, "name": nm, "user": u, "pw": p, "srv": srv if SRV_RX.match(srv) else "",
                    "max": _int(x.get("max"), 1, 20, 1), "prio": _int(x.get("prio"), 0, 99, 1), "on": x.get("on") is not False})
    mx = _int(a.get("max"), 1, 20)
    return {"max": mx, "list": out} if out or mx else None


def active_accounts(src):
    """Включённые дополнительные аккаунты источника в порядке приоритета (меньше — раньше)."""
    a = src.get("accounts") or {}
    ls = [x for x in (a.get("list") or []) if x.get("on", True) and x.get("user") and x.get("pw")]
    return sorted(ls, key=lambda x: (x.get("prio", 1), (a.get("list") or []).index(x)))


def entries(rules):
    """Записи pooler для всех включённых источников с аккаунтами. -> (entries, notes, totals{id: потоков всего})"""
    out, notes, totals = [], [], {}
    for src in (rules or {}).get("sources") or []:
        if not src.get("enabled", True):
            continue
        extra = active_accounts(src)
        if not extra:
            continue
        xc = parse_xc(src.get("url"))
        if not xc:
            notes.append(f"{src.get('name')}: аккаунты работают только со ссылкой Xtream (get.php?username=…&password=…) — пропущено")
            continue
        _, host, _, user, pw = xc
        pmax = (src.get("accounts") or {}).get("max")
        if not isinstance(pmax, int) or pmax < 1:
            notes.append(f"{src.get('name')}: не указан лимит основного аккаунта — пропущено")
            continue
        regex = "^https?://(" + re.escape(host) + r"(?::\d+)?)/(?:live/)?(" + re.escape(user) + "/" + re.escape(pw) + ")/.*$"
        pool = ["$$ $$"] * pmax
        for a in extra:
            pool += [f"{a.get('srv') or '$$'} {a['user']}/{a['pw']}"] * max(1, int(a.get("max") or 1))
        out.append({"name": PREFIX + str(src.get("id")), "isEnabled": True, "regex": regex, "replace": "", "pool": pool})
        totals[src.get("id")] = len(pool)
    return out, notes, totals


def load_jsonc(text):
    """JSON с комментариями // и /* */ (так написан default.json HLS Proxy). Комментарии внутри строк не трогаем."""
    res, i, n, ins = [], 0, len(text), False
    while i < n:
        c = text[i]
        if ins:
            res.append(c)
            if c == "\\" and i + 1 < n:
                res.append(text[i + 1]); i += 2; continue
            if c == '"': ins = False
            i += 1; continue
        if c == '"':
            ins = True; res.append(c); i += 1; continue
        if text.startswith("//", i):
            j = text.find("\n", i); i = n if j < 0 else j; continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2); i = n if j < 0 else j + 2; continue
        res.append(c); i += 1
    s = re.sub(r",(\s*[}\]])", r"\1", "".join(res))   # висячие запятые
    return json.loads(s)


def default_pooler(path):
    """pooler из default.json HLS Proxy (там шаблон cbilling, включённый по умолчанию). Ошибка чтения -> []."""
    try:
        with open(path, encoding="utf-8") as f:
            v = load_jsonc(f.read()).get("pooler")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def merge(local_cfg, defaults, ours):
    """Новый pooler для local.json: чужие записи сохраняем, наши (e4tv-*) заменяем.
    Если в local.json pooler ещё не было — берём записи из default.json, чтобы не потерять шаблон cbilling
    (неизвестно, дополняет local.json список из default.json или заменяет его целиком).
    Возвращает список или None — тогда ключ pooler из local.json нужно убрать (наших записей нет, остальное = default)."""
    base = local_cfg.get("pooler") if isinstance(local_cfg.get("pooler"), list) else list(defaults)
    base = [e for e in base if not (isinstance(e, dict) and str(e.get("name", "")).startswith(PREFIX))]
    if not ours and base == list(defaults):
        return None
    return base + ours
