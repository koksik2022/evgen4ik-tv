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
import datetime, glob, io, json, os, re, subprocess, sys, tarfile, time, urllib.error, urllib.parse, urllib.request, uuid

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
UPLOADS = E4["uploads"]
EVENTS = ("expire", "build", "source", "newip")
BK_MAGIC = b"E4TVBK1\n"   # начало файла резервной копии (дальше — вывод openssl enc)
UP_RX = re.compile(r"^[a-z0-9]{4,16}\.m3u$")
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


def send_doc(c, name, data, caption):
    """Файл в Telegram (sendDocument, multipart вручную — только стандартная библиотека)."""
    b = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", c["chat_id"]), ("caption", caption[:1000])):
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="document"; filename="{name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    rq = urllib.request.Request(f"https://api.telegram.org/bot{c['token']}/sendDocument", data=b"".join(parts),
                                headers={"Content-Type": f"multipart/form-data; boundary={b}", "User-Agent": "Evgen4ik-TV"})
    try:
        with urllib.request.urlopen(rq, timeout=60) as r: j = json.load(r)
    except urllib.error.HTTPError as e:
        try: j = json.load(e)
        except Exception: j = {"description": f"HTTP {e.code}"}
    except Exception as e:
        raise RuntimeError(f"нет связи с api.telegram.org: {getattr(e, 'reason', e)}")
    if not j.get("ok"): raise RuntimeError(str(j.get("description") or "Telegram не принял файл")[:200])


def _openssl(args, data, pw):
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256"] + args + ["-pass", "env:E4BKPW"],
                       input=data, capture_output=True, timeout=120, env={**os.environ, "E4BKPW": pw})
    if r.returncode != 0: raise RuntimeError("неверный пароль или файл повреждён" if "-d" in args else "openssl: " + r.stderr.decode(errors="replace")[:200])
    return r.stdout


def make_backup(pw):
    """Зашифрованная копия: правила (источники, плейлисты, клиенты) и загруженные файлы плейлистов. Без токена бота."""
    if len(pw or "") < 8: raise RuntimeError("пароль копии не задан (не короче 8 символов)")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        t.add(RULES, arcname="rules.json")
        for f in sorted(glob.glob(f"{UPLOADS}/*.m3u")):
            if UP_RX.match(os.path.basename(f)) and os.path.getsize(f) <= 30 * 1024 * 1024: t.add(f, arcname="uploads/" + os.path.basename(f))
        meta = json.dumps({"version": E4.get("version", ""), "host": host(), "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False).encode()
        ti = tarfile.TarInfo("meta.json"); ti.size = len(meta); t.addfile(ti, io.BytesIO(meta))
    return BK_MAGIC + _openssl(["-salt"], buf.getvalue(), pw)


def open_backup(data, pw):
    """Расшифровать копию -> {"rules": dict, "uploads": {имя: байты}, "meta": dict}. Берём только известные файлы."""
    if not data.startswith(BK_MAGIC): raise RuntimeError("это не файл резервной копии Evgen4ik TV (.e4b)")
    raw = _openssl(["-d"], data[len(BK_MAGIC):], pw)
    out = {"rules": None, "uploads": {}, "meta": {}}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as t:
        for m in t.getmembers():
            if not m.isfile() or m.size > 30 * 1024 * 1024: continue
            if m.name == "rules.json": out["rules"] = json.load(t.extractfile(m))
            elif m.name == "meta.json": out["meta"] = json.load(t.extractfile(m))
            elif m.name.startswith("uploads/") and UP_RX.match(m.name[8:]): out["uploads"][m.name[8:]] = t.extractfile(m).read()
    if not isinstance(out["rules"], dict): raise RuntimeError("в копии нет правил")
    return out


def send_backup(c):
    bk = c.get("backup") if isinstance(c.get("backup"), dict) else {}
    data = make_backup(str(bk.get("pass") or ""))
    rules = rj(RULES, {}) or {}
    name = f"evgen4ik-tv-{re.sub(r'[^A-Za-z0-9.-]', '_', host())}-{time.strftime('%Y-%m-%d')}.e4b"
    cap = (f"💾 Evgen4ik TV · {host()}: резервная копия {time.strftime('%d.%m.%Y %H:%M')}\n"
           f"Клиентов: {len(rules.get('clients') or [])}, плейлистов: {len(rules.get('lists') or [])}, источников: {len(rules.get('sources') or [])}.\n"
           "Восстановить: редактор → «История и копии» → «Восстановить из файла» + пароль копии.")
    send_doc(c, name, data, cap)
    return name


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
    bk = c.get("backup") if isinstance(c.get("backup"), dict) else {}
    if bk.get("on") and st is not None and time.time() - (st.get("last_backup") or 0) > 23.5 * 3600 and time.localtime().tm_hour >= 4:
        try: send_backup(c); st["last_backup"] = int(time.time()); st["backup_error"] = ""
        except Exception as e: st["backup_error"] = str(e)[:200]; st["last_backup"] = int(time.time()) - 22 * 3600   # повторить через ~1,5 часа
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
