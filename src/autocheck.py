#!/usr/bin/env python3
"""Evgen4ik TV — автопроверка потоков.

Проверяет первую ссылку каждого канала из общего плейлиста (combined.m3u — ссылки провайдеров,
не рестрим HLS Proxy, чтобы не запускать каналы в прокси). Скачивает только начало ответа
(список сегментов m3u8 или первые байты потока) — сами сегменты не качаются.

Запуск: таймер e4tv-check.timer (раз в сутки) или кнопка в редакторе (check.trigger -> e4tv-check.path).
  autocheck.py              — проверить сейчас
  autocheck.py --scheduled  — то же, но только если в редакторе включена «проверка по расписанию»
Результат: <data>/editor/autocheck.json (без ссылок). Настройки: <data>/editor/autocheck-conf.json (пишет редактор).
Ход проверки: <data>/editor/autocheck-progress.json (раз в секунду). Пауза и остановка: редактор пишет
«pause» или «stop» в <data>/editor/autocheck.ctl (пусто — продолжать). Остановленная проверка прежний итог не меняет.
Только стандартная библиотека Python.
"""
import json, os, re, socket, ssl, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor
try:
    import fcntl
except ImportError:   # не Linux (тесты на Windows) — без защиты от двух проверок сразу
    fcntl = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e4conf import C as E4

DATA = E4["editor_dir"]
RESULT = f"{DATA}/autocheck.json"
CONF = f"{DATA}/autocheck-conf.json"
PROGRESS = f"{DATA}/autocheck-progress.json"
CTL = f"{DATA}/autocheck.ctl"
LOCK = f"{DATA}/autocheck.lock"
UA ="VLC/3.0.20 LibVLC/3.0.20"
READ_MAX = 65536
TIMEOUT = 10
# auto — проверять по расписанию (иначе таймер ничего не делает); workers — одновременных проверок
DEFAULTS = {"auto": False, "workers": 2}


def read_json(p, d=None):
    try:
        with open(p, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return d


def write_json_atomic(p, obj, mode=0o644):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
    os.chmod(tmp, mode); os.replace(tmp, p)


def settings():
    s = dict(DEFAULTS); s.update({k: v for k, v in (read_json(CONF, {}) or {}).items() if k in DEFAULTS})
    s["auto"] = bool(s["auto"])
    try: s["workers"] = max(1, min(6, int(s["workers"])))
    except Exception: s["workers"] = 2
    return s


def channels(path=None):
    """combined.m3u -> [{name, group, url}] — по одному (первому) адресу на название."""
    path = path or E4["combined"]
    out, seen, name, group = [], set(), None, ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f: lines = f.read().replace("\r", "").split("\n")
    except OSError:
        return None
    for l in lines:
        l = l.strip()
        if l.startswith("#EXTINF"):
            name = l.rsplit(",", 1)[-1].strip() if "," in l else ""
            m = re.search(r'group-title="([^"]*)"', l); group = m.group(1) if m else ""
        elif l.startswith("#EXTGRP:") and name is not None and not group:
            group = l[8:].strip()
        elif l and not l.startswith("#") and name is not None:
            k = name.lower()
            if name and k not in seen:
                seen.add(k); out.append({"name": name, "group": group, "url": l})
            name, group = None, ""
    return out


def probe(u, timeout=TIMEOUT):
    """(ok, сообщение). Не-http ссылки (udp, rtmp…) не проверяются: ok=None."""
    if not re.match(r"^https?://", u, re.I): return None, "не http — не проверяется"
    t = time.time()
    try:
        rq = urllib.request.Request(u, headers={"User-Agent": UA})
        with urllib.request.urlopen(rq, timeout=timeout, context=ssl.create_default_context()) as r:
            b = r.read(READ_MAX); ct = (r.headers.get("Content-Type") or "").lower(); code = r.status
        ms = int((time.time() - t) * 1000)
        txt = b.decode("utf-8", "replace")
        if "#EXTM3U" in txt and "#EXT-X-" in txt: return code < 400, f"{code} · HLS · {ms} мс"
        if b[:1] == b"\x47" or "mp2t" in ct or "video" in ct or "octet-stream" in ct: return code < 400, f"{code} · видео · {ms} мс"
        return False, f"{code} · не похоже на поток ({ct or 'без типа'})"
    except urllib.error.HTTPError as e:
        return False, f"{e.code} {e.reason}"[:120]
    except (socket.timeout, TimeoutError):
        return False, f"нет ответа за {timeout} с"
    except Exception as e:
        return False, str(getattr(e, "reason", e))[:120]


def clear_ctl(path):
    """Сбросить паузу/остановку. Файл не создаём: его пишет редактор (пользователь e4tv),
    созданный от root он уже не смог бы изменить."""
    if os.path.exists(path):
        try:
            with open(path, "w"): pass
        except OSError: pass


class Progress:
    """Ход проверки для редактора + пауза/остановка по файлу autocheck.ctl.
    Фоновый поток раз в секунду читает команду и пишет autocheck-progress.json."""
    def __init__(self, total, path=PROGRESS, ctl=CTL, every=1.0):
        self.path, self.ctl, self.every = path, ctl, every
        self.total, self.done, self.phase = total, 0, "first"
        self.again_total = self.again_done = 0
        self.started = time.time(); self.paused_s = 0.0; self.pause_at = None
        self.state = ""   # "" — идёт, "pause", "stop"
        self.lock = threading.Lock(); self.end = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        clear_ctl(self.ctl); self.write(); self.thread.start(); return self

    def _loop(self):
        while not self.end.wait(self.every):
            self.poll(); self.write()

    def poll(self):
        try:
            with open(self.ctl, encoding="utf-8") as f: v = f.read().strip()
        except OSError: v = ""
        v = v if v in ("pause", "stop") else ""
        with self.lock:
            if self.state == "stop": return            # остановку не отменить
            now = time.time()
            if v == "pause" and self.state != "pause": self.pause_at = now
            if v != "pause" and self.pause_at: self.paused_s += now - self.pause_at; self.pause_at = None
            self.state = v

    def gate(self):
        """Перед каждой проверкой канала: на паузе ждёт, при остановке — False."""
        while True:
            with self.lock: st = self.state
            if st == "stop": return False
            if st != "pause": return True
            time.sleep(0.5)

    def again(self, n):
        with self.lock: self.phase, self.again_total = "again", n

    def tick(self):
        with self.lock:
            if self.phase == "again": self.again_done += 1
            else: self.done += 1

    def snapshot(self):
        with self.lock:
            now = time.time()
            active = now - self.started - self.paused_s - (now - self.pause_at if self.pause_at else 0)
            n = self.done + self.again_done
            left = (self.total - self.done) + (self.again_total - self.again_done)
            return {"running": True, "pid": os.getpid(), "upd": int(now), "started": int(self.started),
                    "total": self.total, "done": self.done, "phase": self.phase,
                    "again_total": self.again_total, "again_done": self.again_done,
                    "paused": self.state == "pause", "stopping": self.state == "stop",
                    "elapsed": int(active), "eta": int(active / n * left) if n and left else None}

    def write(self, **extra):
        try: write_json_atomic(self.path, {**self.snapshot(), **extra})
        except OSError: pass

    def finish(self, stopped=False):
        self.end.set()
        if self.thread.is_alive(): self.thread.join(5)
        self.write(running=False, stopped=stopped, ended=int(time.time()))
        clear_ctl(self.ctl)


def run_check(chs, workers=2, probe_fn=None, prog=None):
    """Проверка в несколько потоков; не ответившие проверяются ещё раз с таймаутом 20 с —
    провайдер иногда долго «поднимает» канал, и с первого раза это выглядит как сбой.
    prog — Progress (ход, пауза, остановка). Остановили — None."""
    probe_fn = probe_fn or probe
    gate = prog.gate if prog else (lambda: True)

    def one(u, timeout=TIMEOUT):
        if not gate(): return "stop", ""
        r = probe_fn(u) if timeout == TIMEOUT else probe_fn(u, timeout)
        if prog: prog.tick()
        return r

    res = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ch, (ok, msg) in zip(chs, ex.map(lambda c: one(c["url"]), chs)):
            res.append({"name": ch["name"], "group": ch["group"], "ok": ok, "msg": msg})
        if any(r["ok"] == "stop" for r in res): return None
        again = [i for i, r in enumerate(res) if r["ok"] is False]
        if prog: prog.again(len(again))
        for i, (ok, msg) in zip(again, ex.map(lambda i: one(chs[i]["url"], 2 * TIMEOUT), again)):
            if ok == "stop": return None
            res[i].update(ok=ok, msg=msg)
    return res


def in_main(chs, final_path=None):
    """Только каналы, которые сейчас есть в основном плейлисте (final.json пишет build.py).
    combined.m3u сборка не стирает, если источников не осталось, — без этого фильтра проверялись бы старые каналы."""
    final = read_json(final_path or f"{DATA}/final.json", None)
    if not isinstance(final, dict): return chs
    return [c for c in chs if c["name"].lower() in final]


def summarize(res, prev):
    bad = [r for r in res if r["ok"] is False]
    first = not (prev or {}).get("ts")   # первая проверка — сравнивать не с чем, «перестали работать» не показываем
    prev_bad = {x["name"] for x in (prev or {}).get("bad", [])}
    now_bad = {x["name"] for x in bad}
    checked = sum(1 for r in res if r["ok"] is not None)
    return {"at": time.strftime("%d.%m.%Y %H:%M"), "ts": int(time.time()), "total": len(res),
            "ok": sum(1 for r in res if r["ok"]), "skipped": len(res) - checked,
            "bad": [{"name": r["name"], "group": r["group"], "msg": r["msg"]} for r in bad],
            "new_bad": [] if first else sorted(now_bad - prev_bad), "recovered": [] if first else sorted(prev_bad - now_bad),
            "source_down": bool(checked) and len(bad) > checked / 2}   # не отвечает больше половины — скорее всего, сам источник


def main(argv):
    s = settings()
    if "--scheduled" in argv and not s["auto"]:
        print("Автопроверка по расписанию выключена в редакторе — пропускаю"); return 0
    prev = read_json(RESULT, {}) or {}
    chs = channels()
    if chs is not None: chs = in_main(chs)
    if chs is None:
        prev.update(error="общий плейлист ещё не собран", at=time.strftime("%d.%m.%Y %H:%M"))
        write_json_atomic(RESULT, prev); print(prev["error"]); return 1
    lk = None
    if fcntl:   # таймер и кнопка — разные службы: вторая проверка, пока идёт первая, не нужна
        lk = open(LOCK, "a")
        try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("Проверка уже идёт — вторую не запускаю"); return 0
    prog = Progress(len(chs)).start()
    try:
        raw = run_check(chs, s["workers"], prog=prog)
    except BaseException:
        prog.finish(stopped=True); raise
    if raw is None:
        prog.finish(stopped=True); print("Проверка остановлена в редакторе — прежний итог не меняю"); return 0
    res = summarize(raw, prev)
    write_json_atomic(RESULT, res)
    prog.finish()
    print(f"Проверено {res['total']}: работают {res['ok']}, не отвечают {len(res['bad'])}, не http {res['skipped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
