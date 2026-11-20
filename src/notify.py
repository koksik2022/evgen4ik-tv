#!/usr/bin/env python3
"""Evgen4ik TV — уведомления в Telegram.

Запуск: таймер e4tv-notify.timer (раз в 5 минут), от root. Читает настройки <data>/editor/telegram.json
(пишет редактор, права 600: там токен бота) и шлёт сообщения о событиях:
  expire — у клиента заканчивается срок (за 3 дня, один раз на каждую дату срока);
  build  — сборка общего плейлиста или листов завершилась с ошибкой (и когда снова в порядке);
  source — источник перестал отдавать каналы (и когда снова отдаёт);
  newip  — клиент зашёл с IP, которого у него раньше не было.
Что уже отправлено — в <data>/editor/notify-state.json (без токенов и ссылок).
  notify.py          — проверить события и отправить
  notify.py --test   — отправить пробное сообщение
Только стандартная библиотека Python.
"""
import datetime, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4

DATA = E4["editor_dir"]
TG = f"{DATA}/telegram.json"
STATE = f"{DATA}/notify-state.json"
RULES = f"{DATA}/rules.json"
SNAP = f"{DATA}/state.json"
LSTATUS = f"{DATA}/lists-status.json"
ACT = f"{DATA}/activity.json"
XC_ACT = f"{DATA}/xc-activity.json"
EVENTS = ("expire", "build", "source", "newip")
TOKEN_RX = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
CHAT_RX = re.compile(r"^-?\d{1,20}$|^@[A-Za-z0-9_]{5,32}$")


def rj(p, d=None):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return d


def wj(p, obj, mode=0o600):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, mode); os.replace(tmp, p)


def conf():
    c = rj(TG, {}) or {}
    ok = bool(TOKEN_RX.match(str(c.get("token", "")))) and bool(CHAT_RX.match(str(c.get("chat_id", ""))))
    ev = c.get("events") if isinstance(c.get("events"), dict) else {}
    return ok, c, {e: ev.get(e) is not False for e in EVENTS}


def send(c, text):
    """Сообщение в Telegram. Ошибка — исключение с понятным текстом (токен в текст не попадает)."""
    data = urllib.parse.urlencode({"chat_id": c["chat_id"], "text": text[:4000], "disable_web_page_preview": "true"}).encode()
    rq = urllib.request.Request(f"https://api.telegram.org/bot{c['token']}/sendMessage", data=data,
                                headers={"User-Agent": "Evgen4ik-TV"})
    try:
        with urllib.request.urlopen(rq, timeout=20) as r: j = json.load(r)
    except urllib.error.HTTPError as e:
        try: j = json.load(e)
        except Exception: j = {"description": f"HTTP {e.code}"}
    except Exception as e:
        raise RuntimeError(f"нет связи с api.telegram.org: {getattr(e, 'reason', e)}")
    if not j.get("ok"): raise RuntimeError(str(j.get("description") or "Telegram не принял сообщение")[:200])


def host():
    return E4.get("public_base", "").split("/")[2] if E4.get("public_base", "").count("/") >= 2 else os.uname().nodename


def unit_result(u):
    try:
        out = subprocess.run(["systemctl", "show", u, "-p", "Result", "-p", "ExecMainExitTimestamp"], capture_output=True, text=True, timeout=10).stdout
        d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        return d.get("Result") or "", d.get("ExecMainExitTimestamp") or ""
    except Exception:
        return "", ""


def ru(iso):
    return ".".join(reversed(iso.split("-"))) if iso else ""


def plural(n, a, b, c):
    m, h = n % 10, n % 100
    return f"{n} " + (a if m == 1 and h != 11 else b if 2 <= m <= 4 and not 12 <= h <= 14 else c)


def collect(rules, st, first):
    """Список (ключ события, текст). Ключи уже отправленных пропускает вызывающий."""
    msgs, today = [], datetime.date.today()
    clients = [c for c in (rules.get("clients") or []) if isinstance(c, dict) and c.get("id")]
    # 1) срок клиента
    for c in clients:
        e = c.get("expires") or ""
        if c.get("enabled") is False or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", e): continue
        d = (datetime.date.fromisoformat(e) - today).days
        if 0 <= d <= 3:
            left = "сегодня последний день" if d == 0 else ("остался " if d == 1 else "осталось ") + plural(d, "день", "дня", "дней")
            msgs.append(("expire", f"exp:{c['id']}:{e}", f"⏳ Клиент «{c.get('name')}»: доступ до {ru(e)} — {left}.\nПродлить: Клиенты → «{c.get('name')}» → +1 месяц."))
    # 2) сборка
    for unit, what in (("e4tv-build.service", "Сборка общего плейлиста"), ("e4tv-lists.service", "Сборка плейлистов и ссылок клиентов")):
        res, at = unit_result(unit)
        if not res: continue
        bad = res != "success"
        prev = st.get("units", {}).get(unit)
        st.setdefault("units", {})[unit] = "bad" if bad else "ok"
        if first or prev is None: continue
        if bad and prev != "bad": msgs.append(("build", f"b:{unit}:{at}", f"✖ {what} завершилась с ошибкой ({res}).\nПодробности: редактор → «Журнал» или на сервере: e4tv logs"))
        if not bad and prev == "bad": msgs.append(("build", f"bok:{unit}:{at}", f"✅ {what} снова в порядке."))
    # 3) источники
    snap = rj(SNAP, {}) or {}
    for s in snap.get("sources") or []:
        n, status = str(s.get("name") or ""), str(s.get("status") or "")
        if not n or status == "выключен": continue
        bad = status.startswith("ошибка") or not s.get("count")
        prev = st.setdefault("sources", {}).get(n)
        st["sources"][n] = "bad" if bad else "ok"
        if first or prev is None: continue
        if bad and prev != "bad": msgs.append(("source", f"s:{n}:{snap.get('built_at')}", f"⚠ Источник «{n}» не отдал каналы при сборке {snap.get('built_at', '')}.\n{status[:200]}"))
        if not bad and prev == "bad": msgs.append(("source", f"sok:{n}:{snap.get('built_at')}", f"✅ Источник «{n}» снова работает: {s.get('count')} каналов."))
    # 4) новый IP клиента (по журналу заходов и входам Xtream)
    act, xca = rj(ACT, {}) or {}, rj(XC_ACT, {}) or {}
    known = st.setdefault("ips", {})
    for c in clients:
        seen = [a for a in ((act.get(c.get("token")) or {}), (xca.get(c.get("xc_user")) or {})) if a.get("ip")]
        kn = known.setdefault(c["id"], [])
        for a in seen:
            ip = str(a["ip"])[:64]
            if ip in kn: continue
            kn.append(ip); del kn[:-30]
            if first or len(kn) == 1: continue   # первый заход клиента — не «новый IP»
            msgs.append(("newip", f"ip:{c['id']}:{ip}", f"🌐 Клиент «{c.get('name')}» зашёл с нового IP {ip}" + (f" ({a.get('ua')})" if a.get("ua") else "") + ".\nЕсли это не он — «Новая ссылка» или «Разрешённые IP» у клиента."))
    for cid in [k for k in known if k not in {c["id"] for c in clients}]: del known[cid]
    return msgs


def main():
    ok, c, ev = conf()
    if "--test" in sys.argv:
        if not ok: print("Telegram не настроен: в редакторе → «Уведомления» укажите токен бота и чат"); return 1
        try: send(c, f"✅ Evgen4ik TV ({host()}): уведомления работают.")
        except Exception as e: print("Не отправилось:", e); return 1
        print("Отправлено"); return 0
    if not ok: return 0
    st = rj(STATE, None)
    first = st is None   # первый запуск: только запоминаем, что есть, без потока сообщений
    st = st or {}
    sent = st.setdefault("sent", {})
    rules = rj(RULES, {}) or {}
    msgs = collect(rules, st, first)
    errs = []
    for kind, key, text in msgs:
        if key in sent or not ev.get(kind): continue
        try:
            send(c, f"Evgen4ik TV · {host()}\n{text}"); sent[key] = int(time.time())
        except Exception as e:
            errs.append(str(e)); break   # нет связи — попробуем в следующий раз
    lim = time.time() - 90 * 86400
    st["sent"] = {k: v for k, v in sent.items() if v >= lim}
    st["last_run"] = int(time.time()); st["last_error"] = errs[0] if errs else ""
    wj(STATE, st, 0o640)
    try:
        import grp; os.chown(STATE, 0, grp.getgrnam(E4.get("editor_group", "e4tv")).gr_gid)
    except Exception: pass
    if errs: print("Telegram:", errs[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())
