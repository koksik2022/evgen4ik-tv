#!/usr/bin/env python3
"""Автотесты Evgen4ik TV (только стандартная библиотека Python). Запуск: python3 tests/run_tests.py
Проверяют то, что ломалось или легко сломать: проверку правил, сборку ссылок клиентов, Xtream, журнал активности,
правила IP для nginx, синтаксис установщика и страницы, согласованность get.sh и установщика."""
import base64, hashlib, io, json, os, random, re, shutil, subprocess, sys, tarfile, tempfile, threading, time, unittest, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
TMP = tempfile.mkdtemp(prefix="e4tv-test-")
for d in ("data/editor/uploads", "data/build", "pl", "hls"): os.makedirs(os.path.join(TMP, d), exist_ok=True)
CONF = {"mode": "direct", "data": f"{TMP}/data", "pl_dir": f"{TMP}/pl", "hls_dir": f"{TMP}/hls", "main_token": "MAINTOKENMAINTOKEN12",
        "public_base": "http://tv.test/pl/", "access": "tunnel", "port": 18195, "listen": "127.0.0.1", "player_epg": "http://tv.test/epg.xml.gz",
        "version": "9.9"}
json.dump(CONF, open(f"{TMP}/c.json", "w"))
os.environ["E4TV_CONF"] = f"{TMP}/c.json"
sys.path.insert(0, SRC)
open(f"{TMP}/data/combined.m3u", "w", encoding="utf-8").write(
    '#EXTM3U\n#EXTINF:-1 tvg-id="k1" group-title="Кино",Кино 1\nhttp://p/1\n#EXTINF:-1 group-title="18+",Взрослый\nhttp://p/2\n'
    '#EXTINF:-1 group-title="Детские",Мульт\nhttp://p/3\n#EXTINF:-1 tvg-rec="3" group-title="Новости",Новости 24\nhttp://p/4\n')
json.dump({"кино 1": ["кино 1", "Кино"], "взрослый": ["взрослый", "18+"], "мульт": ["мульт", "Детские"], "новости 24": ["новости 24", "Новости"]},
          open(f"{TMP}/data/editor/final.json", "w"), ensure_ascii=False)

import app, lists   # noqa: E402  (после настройки окружения)
lists.write_acl = lambda clients: None   # nginx в тестах не трогаем (генерацию проверяет отдельный тест)
lists.os.chown = lambda *a, **k: None    # тесты могут идти не от root


def rules(**kw):
    r = {"lists": [{"id": "b2", "name": "Дети", "groups": ["Детские", "Кино"]},
                   {"id": "p1", "name": "Личный", "groups": ["Новости"], "owner": "aa1"}],
         "clients": [{"id": "aa1", "name": "A", "lists": ["b2", "p1"], "off": ["кино 1"], "add": ["взрослый"], "urls": {"мульт": "http://my/m.m3u8"},
                      "extra": [{"name": "Свой, канал", "url": "http://own/1.m3u8", "group": "Своё"}], "no_adult": False, "xc_user": "mama_tv",
                      "order": ["новости 24"], "ips": ["127.0.0.1"]},
                     {"id": "bb2", "name": "B", "lists": ["b2", "p1"]},
                     {"id": "cc3", "name": "Выкл", "lists": ["b2"], "enabled": False},
                     {"id": "dd4", "name": "Старый", "lists": ["b2"], "expires": "2020-01-01"}]}
    r.update(kw)
    return app.validate(r)


def build(r):
    json.dump(r, open(f"{TMP}/data/editor/rules.json", "w"), ensure_ascii=False)
    assert lists.main() == 0
    return {c["name"]: (open(f"{TMP}/pl/{c['token']}.m3u8", encoding="utf-8").read() if os.path.exists(f"{TMP}/pl/{c['token']}.m3u8") else None)
            for c in r["clients"]}


class Rules(unittest.TestCase):
    def test_fuzz_never_crashes(self):
        bad = [None, 1, "x", [], {}, True, -5, "a" * 3000, {"a": 1}, [None], [{"x": 1}], "../../etc/passwd", ["1.2.3.4", "zz"], "2026-13-45"]
        rnd = random.Random(1)
        for _ in range(1500):
            r = {"client_msg": rnd.choice(bad), "clients": rnd.choice([rnd.choice(bad), [
                {k: rnd.choice(bad) for k in ["id", "name", "lists", "off", "add", "urls", "extra", "ips", "order", "xc_user", "xc_pw",
                                              "streams", "expires", "token", "hls_pw", "contact", "note", "created"]} | {"name": rnd.choice(["Мама", None, 1])}
                for _ in range(3)]]),
                 "lists": [{"id": rnd.choice(["a1", None, 1]), "name": "L", "owner": rnd.choice(bad), "no_clients": rnd.choice(bad)} for _ in range(2)]}
            app.validate(r)   # не должно бросать исключений

    def test_client_fields(self):
        r = rules()
        a = r["clients"][0]
        self.assertEqual(a["lists"], ["b2", "p1"])
        self.assertEqual(a["xc_user"], "mama_tv")
        self.assertRegex(a["xc_pw"], r"^[A-Za-z0-9]{8,32}$")
        self.assertEqual(a["ips"], ["127.0.0.1/32"])
        self.assertEqual(len(a["extra"]), 1)
        b = r["clients"][1]
        self.assertEqual(b["lists"], ["b2"], "чужой личный плейлист у клиента не остаётся")
        p1 = [L for L in r["lists"] if L["id"] == "p1"][0]
        self.assertFalse(p1["enabled"], "личный плейлист без своей ссылки")

    def test_bad_values_dropped(self):
        r = rules(clients=[{"name": "X", "urls": {"k": "javascript:alert(1)"}, "extra": [{"name": "y", "url": "ftp://x"}], "ips": ["nope"],
                            "lists": ["zzz"], "xc_user": "BAD USER"}])
        c = r["clients"][0]
        self.assertEqual((c["urls"], c["extra"], c["ips"], c["lists"]), ({}, [], [], []))
        self.assertRegex(c["xc_user"], r"^[a-z0-9_]{3,32}$")

    def test_owner_removed_with_client(self):
        r = rules()
        r2 = app.validate(dict(r, clients=[c for c in r["clients"] if c["id"] != "aa1"]))
        self.assertFalse(any(L["id"] == "p1" for L in r2["lists"]))


class Notify(unittest.TestCase):
    def test_events(self):
        import notify, datetime
        sent = []
        notify.send = lambda c, text: sent.append(text)
        notify.unit_result = lambda u: ("success", "t1")
        json.dump({"token": "123456:" + "A" * 35, "chat_id": "42"}, open(notify.TG, "w"))
        exp = (datetime.date.today() + datetime.timedelta(days=2)).isoformat()
        r = {"clients": [{"id": "aa1", "name": "Жека", "token": "TOKTOKTOKTOKTOKTOK", "expires": exp}]}
        json.dump(r, open(notify.RULES, "w"), ensure_ascii=False)
        json.dump({"TOKTOKTOKTOKTOKTOK": {"ip": "1.1.1.1", "last": "x"}}, open(notify.ACT, "w"))
        json.dump({"sources": [{"name": "Пров", "count": 10, "status": "ok"}], "built_at": "b1"}, open(notify.SNAP, "w"), ensure_ascii=False)
        if os.path.exists(notify.STATE): os.remove(notify.STATE)
        notify.main(); self.assertEqual(len(sent), 1, "первый запуск: только срок клиента, IP и источники лишь запоминает")
        self.assertIn("Жека", sent[0]); self.assertIn("осталось 2 дня", sent[0])
        notify.main(); self.assertEqual(len(sent), 1, "повторно не шлёт")
        json.dump({"TOKTOKTOKTOKTOKTOK": {"ip": "2.2.2.2", "last": "y", "ua": "TiviMate"}}, open(notify.ACT, "w"))
        json.dump({"sources": [{"name": "Пров", "count": 0, "status": "ошибка: 403"}], "built_at": "b2"}, open(notify.SNAP, "w"), ensure_ascii=False)
        notify.unit_result = lambda u: ("exit-code", "t2")
        notify.main()
        txt = "\n".join(sent[1:])
        self.assertIn("2.2.2.2", txt); self.assertIn("Пров", txt); self.assertIn("с ошибкой", txt)
        n = len(sent); notify.main(); self.assertEqual(len(sent), n, "о той же ошибке второй раз не пишет")


class Backup(unittest.TestCase):
    def test_roundtrip_and_restore(self):
        import notify, base64
        r = rules(); r["clients"][0]["name"] = "Копия"
        json.dump(r, open(notify.RULES, "w"), ensure_ascii=False)
        os.makedirs(notify.UPLOADS, exist_ok=True); open(f"{notify.UPLOADS}/abcd1234.m3u", "w").write("#EXTM3U\n")
        data = notify.make_backup("пароль-123")
        self.assertTrue(data.startswith(notify.BK_MAGIC)); self.assertNotIn(b"mama_tv", data, "зашифровано")
        with self.assertRaises(RuntimeError): notify.open_backup(data, "не тот")
        with self.assertRaises(RuntimeError): notify.make_backup("short")
        bk = notify.open_backup(data, "пароль-123")
        self.assertEqual(bk["rules"]["clients"][0]["name"], "Копия"); self.assertIn("abcd1234.m3u", bk["uploads"])
        json.dump(rules(), open(app.RULES, "w"), ensure_ascii=False); os.remove(f"{notify.UPLOADS}/abcd1234.m3u")
        b64 = base64.b64encode(data).decode()
        chk = app.restore_file({"data": b64, "pass": "пароль-123", "check": True})
        self.assertEqual(chk["clients"], len(r["clients"]))
        self.assertNotEqual(json.load(open(app.RULES))["clients"][0]["name"], "Копия", "check ничего не меняет")
        app.restore_file({"data": b64, "pass": "пароль-123"})
        self.assertEqual(json.load(open(app.RULES))["clients"][0]["name"], "Копия")
        self.assertTrue(os.path.exists(f"{notify.UPLOADS}/abcd1234.m3u"))
        with self.assertRaises(ValueError): app.restore_file({"data": base64.b64encode(b"junk").decode(), "pass": "x"})


class MyPage(unittest.TestCase):
    def test_client_page(self):
        r = rules(); a = r["clients"][0]; a["name"] = "<b>Жека</b>"; a["slug"] = "zheka-4tq"
        json.dump(r, open(app.RULES, "w"), ensure_ascii=False)
        pg = app.my_page(a["token"])
        self.assertIn("&lt;b&gt;Жека&lt;/b&gt;", pg); self.assertNotIn("<b>Жека</b>", pg)
        self.assertIn("http://tv.test/pl/zheka-4tq.m3u8", pg, "короткая ссылка, если задана")
        self.assertIn(a["xc_pw"], pg)
        self.assertIsNone(app.my_page("NOPE_NOPE_NOPE_NOPE_1")); self.assertIsNone(app.my_page("zheka-4tq"), "по короткой ссылке страница не открывается")
        off = next(c for c in r["clients"] if c["name"] == "Выкл")
        po = app.my_page(off["token"]); self.assertIn("выключен", po); self.assertNotIn(".m3u8", po)
        old = next(c for c in r["clients"] if c["name"] == "Старый")
        self.assertIn("закончился", app.my_page(old["token"]))


class Domain(unittest.TestCase):
    def test_check_and_set(self):
        with self.assertRaises(ValueError): app.domain_check({"domain": "не домен"})
        real, addr = app.socket.getaddrinfo, app.E4.get("address")
        self.addCleanup(setattr, app.socket, "getaddrinfo", real); self.addCleanup(app.E4.__setitem__, "address", addr)
        app.socket.getaddrinfo = lambda d, *a, **k: [(2, 1, 6, "", ("203.0.113.5", 0))] if d == "tv.example.com" else real(d, *a, **k)
        app.E4["address"] = "203.0.113.5"
        r = app.domain_check({"domain": "https://TV.Example.com/x"})
        self.assertEqual(r["domain"], "tv.example.com"); self.assertTrue(r["match"])
        open(app.DOMAIN_REQ, "w").close()
        app.domain_set({"domain": "tv.example.com", "email": "a@b.ru"})
        self.assertEqual(json.load(open(app.DOMAIN_REQ))["domain"], "tv.example.com")
        app.E4["address"] = "198.51.100.7"
        with self.assertRaises(ValueError): app.domain_set({"domain": "tv.example.com"})
        with self.assertRaises(ValueError): app.domain_set({"domain": "tv.example.com", "email": "bad"})

    def test_installer_set_domain(self):
        s = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        for x in ("--set-domain) NEWDOMAIN=$2; ACTION=update", "DOMAIN=${NEWDOMAIN:-$DOMAIN_DEF}", "e4tv-domain.path", "domain.request; do"):
            self.assertIn(x, s)
        self.assertIn("location ^~ /my/", s)


class Build(unittest.TestCase):
    def test_check_url_nonascii(self):   # кириллица в адресе -> понятная ошибка, без падения «ordinal not in range(256)»
        r = app.check_url("http://ВАШ-ПОДДОМЕН.zatikov.net/playlists/uplist/AAAAAAAAAAAAAA/playlist.m3u8")
        self.assertFalse(r["ok"]); self.assertIn("не-латинские", r["msg"])

    def test_client_files(self):
        r = rules(); r["clients"][0]["link_mode"] = "direct"; r["clients"][1]["link_mode"] = "direct"
        out = build(r)
        a = out["A"]
        self.assertNotIn("Кино 1", a, "выключенный канал")
        self.assertIn("http://my/m.m3u8", a, "своя ссылка")
        self.assertIn("Взрослый", a, "добавлен из основного")
        self.assertIn("Свой канал", a, "свой канал")
        self.assertTrue(a.split("\n")[1].endswith(",Новости 24"), "свой порядок: первым")
        self.assertIn("http://p/1", out["B"], "правки A не задевают B")
        self.assertNotIn("Новости 24", out["B"], "личный плейлист A не достаётся B")
        self.assertIsNone(out["Выкл"]); self.assertIsNone(out["Старый"])

    def test_client_short_link(self):
        r = rules(lists=[{"id": "b2", "name": "Дети", "groups": ["Детские", "Кино"], "slug": "deti-1x"}])
        r["clients"][0]["slug"] = "sasha-7x"; r["clients"][1]["slug"] = "deti-1x"; r["clients"][2]["slug"] = "vykl-2z"
        r = app.validate(r)
        a, b = r["clients"][0], r["clients"][1]
        self.assertEqual(a["slug"], "sasha-7x")
        self.assertEqual(b["slug"], "", "короткая уже занята плейлистом")
        self.assertEqual(app.validate(dict(r, clients=[dict(a, slug="BAD SLUG")]))["clients"][0]["slug"], "")
        a["link_mode"] = "direct"; build(r)
        self.assertEqual(open(f"{TMP}/pl/sasha-7x.m3u8", encoding="utf-8").read(), open(f"{TMP}/pl/{a['token']}.m3u8", encoding="utf-8").read())
        self.assertFalse(os.path.exists(f"{TMP}/pl/vykl-2z.m3u8"), "у выключенного клиента короткой ссылки нет")
        a["slug"] = ""; build(r)
        self.assertFalse(os.path.exists(f"{TMP}/pl/sasha-7x.m3u8"), "убрали короткую — файл удалён")

    def test_epg_shift(self):
        r = rules(channels={"кино 1": {"shift": 2}, "мульт": {"shift": "x"}})
        self.assertEqual(r["channels"]["кино 1"]["shift"], 2)
        self.assertNotIn("мульт", r["channels"], "неправильный сдвиг отброшен")
        self.assertNotIn("shift", app.validate({"channels": {"a": {"shift": 99}}}).get("channels", {}).get("a", {}))
        r["clients"][1]["link_mode"] = "direct"
        b = build(r)["B"]
        line = [l for l in b.split("\n") if l.endswith(",Кино 1")][0]
        self.assertIn('tvg-shift="2"', line)
        self.assertNotIn("tvg-shift", [l for l in b.split("\n") if l.endswith(",Мульт")][0])

    def test_no_adult_and_no_clients(self):
        r = rules()
        r["clients"][1]["no_adult"] = True; r["clients"][1]["add"] = ["взрослый"]
        r["lists"][0]["no_clients"] = True
        out = build(r)
        self.assertNotIn("Мульт", out["B"], "плейлист «не для клиентов» не попадает")
        self.assertNotIn("Взрослый", out["B"], "детский режим")

    def test_xc_json(self):
        build(rules())
        x = json.load(open(f"{TMP}/data/editor/xc.json", encoding="utf-8"))
        self.assertIn("mama_tv", x)
        self.assertTrue(x["mama_tv"]["channels"])
        dis = [v for v in x.values() if v["id"] == "cc3"][0]
        self.assertEqual(dis["channels"], [], "у выключенного каналов нет")


class Xtream(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        build(rules())
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = json.load(open(f"{TMP}/data/editor/xc.json"))["mama_tv"]["pw"]

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def get(self, path, ip=None):
        rq = urllib.request.Request(self.base + path, headers={"Host": "localhost", **({"X-Real-IP": ip} if ip else {})})
        op = urllib.request.build_opener(type("NoRedir", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
        try:
            with op.open(rq, timeout=10) as r: return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e: return e.code, e.headers, e.read().decode()

    def test_auth(self):
        self.assertEqual(json.loads(self.get("/player_api.php?username=mama_tv&password=bad")[2])["user_info"]["auth"], 0)
        j = json.loads(self.get(f"/player_api.php?username=mama_tv&password={self.pw}")[2])
        self.assertEqual((j["user_info"]["auth"], j["user_info"]["status"]), (1, "Active"))

    def test_ip_restriction(self):
        j = json.loads(self.get(f"/player_api.php?username=mama_tv&password={self.pw}", ip="8.8.8.8")[2])
        self.assertEqual(j["user_info"]["auth"], 0)

    def test_streams_and_redirect(self):
        cats = json.loads(self.get(f"/player_api.php?username=mama_tv&password={self.pw}&action=get_live_categories")[2])
        st = json.loads(self.get(f"/player_api.php?username=mama_tv&password={self.pw}&action=get_live_streams")[2])
        self.assertTrue(cats and st)
        code, h, _ = self.get(f"/live/mama_tv/{self.pw}/1.ts")
        self.assertEqual(code, 302); self.assertTrue(h["Location"].startswith("http"))
        self.assertEqual(self.get(f"/live/mama_tv/{self.pw}/9999.ts")[0], 404)
        m3u = self.get(f"/get.php?username=mama_tv&password={self.pw}&type=m3u_plus")[2]
        self.assertTrue(m3u.startswith("#EXTM3U") and "/live/mama_tv/" in m3u)

    def test_editor_api_needs_no_xc_bypass(self):
        self.assertEqual(self.get("/api/state")[0] in (200, 401), True)   # режим tunnel: вход не нужен; главное — не падает


class LinkMode(unittest.TestCase):
    """«Ссылки на каналы»: перенаправление (/live/_r/), напрямую, логотипы через сервер (/live/_logo/), служебные метки."""
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    get = Xtream.get

    def test_validate_fields(self):
        r = rules()
        r["lists"][0].update({"link_mode": "evil"}); r["clients"][0]["link_mode"] = "redirect"
        r.update({"link_mode": "direct", "logo_proxy": False, "fallback": "x"})
        r = app.validate(r)
        self.assertEqual(r["lists"][0]["link_mode"], "")
        self.assertEqual(r["clients"][0]["link_mode"], "redirect")
        self.assertEqual((r["link_mode"], r["logo_proxy"], r["fallback"]), ("direct", False, True))
        self.assertEqual(app.validate({"link_mode": "evil"})["link_mode"], "")

    def test_global_then_list_then_client(self):
        r = rules(link_mode="direct"); r["lists"][0]["enabled"] = True
        out = build(r)
        self.assertIn("http://p/3", out["B"], "общая настройка «не скрывать» — у клиента ссылки провайдера")
        r["clients"][1]["link_mode"] = "redirect"; out = build(r)
        self.assertNotIn("http://p/", out["B"], "у клиента своя настройка важнее общей")
        r["clients"][1]["link_mode"] = ""; r["lists"][0]["link_mode"] = "redirect"; out = build(r)
        self.assertNotIn("http://p/3", out["B"], "у плейлиста своя настройка важнее общей")

    def test_redirect_default_and_auth(self):
        r = rules(); out = build(r)
        a = out["A"]
        self.assertNotIn("http://p/", a, "в режиме direct по умолчанию исходных ссылок нет")
        m = re.search(r"http://tv\.test(/live/_r/([A-Za-z0-9_-]+)/([0-9a-f]{16}))", a)
        self.assertTrue(m)
        code, h, _ = self.get(m.group(1))
        self.assertEqual(code, 302); self.assertTrue(h["Location"].startswith("http://"))
        b = out["B"]; tb = re.search(r"/live/_r/([A-Za-z0-9_-]+)/", b).group(1)
        d = app.links()
        own = [h for h, u in d["u"].items() if u == "http://my/m.m3u8"]
        self.assertTrue(own, "своя ссылка клиента тоже перенаправлением")
        self.assertNotIn(own[0], b)
        self.assertEqual(self.get(f"/live/_r/{tb}/{own[0]}")[0], 404, "код канала A по ссылке B не открывается")
        self.assertEqual(self.get(f"/live/_r/{m.group(2)}/0000000000000000")[0], 404)

    def test_direct_list_and_logo(self):
        r = rules(); r["lists"][0]["link_mode"] = "direct"
        r["lists"][0]["enabled"] = True
        build(r)
        L = [x for x in r["lists"] if x["id"] == "b2"][0]
        t = open(f"{TMP}/pl/{L['token']}.m3u8", encoding="utf-8").read()
        self.assertIn("http://p/3", t, "напрямую — исходная ссылка")
        st = json.load(open(f"{TMP}/data/editor/lists-status.json", encoding="utf-8"))
        self.assertEqual(st["lists"]["b2"]["via"].get("direct"), 2)
        lk = lists.Links([])
        l0 = lk.logo('#EXTINF:-1 tvg-logo="https://img.example/x.png" group-title="G",Name')
        self.assertRegex(l0, r'tvg-logo="http://tv\.test/live/_logo/[0-9a-f]{16}"')
        self.assertEqual(lk.logo('#EXTINF:-1 tvg-logo="http://tv.test/own.png",N'), '#EXTINF:-1 tvg-logo="http://tv.test/own.png",N', "свои картинки не трогаем")
        self.assertEqual(self.get("/live/_logo/0000000000000000")[0], 404)

    def test_marks_and_extra_format(self):
        self.assertEqual(lists.unmark(['#EXTINF:-1 group-title="G",Канал [=0123456789]', "http://x"])[0], '#EXTINF:-1 group-title="G",Канал')
        self.assertEqual(lists.unmark(['#EXTINF:-1,Канал [~ab12]', "http://x"])[0], "#EXTINF:-1,Канал")
        import build as B
        p = f"{TMP}/extra-test.m3u"
        open(p, "w", encoding="utf-8").write('#EXTINF:-1 group-title="E4 extra",ok [=0123456789]\nhttp://a/1\n'
                                             '#EXTINF:-1 group-title="Other",bad [=0123456789]\nhttp://a/2\n'
                                             '#EXTINF:-1 group-title="E4 extra",bad2 [=0123456789]\njavascript:alert(1)\n')
        old = B.EXTRA_FILE; B.EXTRA_FILE = p
        try: self.assertEqual(B.extra_lines([]), ['#EXTINF:-1 group-title="E4 extra",ok [=0123456789]', "http://a/1"])
        finally: B.EXTRA_FILE = old


class GroupOrder(unittest.TestCase):
    def test_regroup_keeps_groups_whole_and_adult_last(self):
        L = lambda g, n: {"n": n, "lines": [f'#EXTINF:-1 group-title="{g}",{n}', "http://x"]}
        seq = [L("Эфирные", "A"), L("Разное", "B"), L("18+", "C"), L("Разное", "D"), L("Кинозал: BOX", "E"), L("Эфирные", "F")]
        got = [x["n"] for x in lists.regroup(seq, lambda it: lists._gt(it["lines"]))]
        self.assertEqual(got, ["A", "F", "B", "D", "E", "C"])

    def test_sources_limit_30(self):   # до 30 источников (несколько своих ключей одного провайдера)
        srcs = [{"name": f"Key {i}", "url": f"http://e.tv/iptv/KEY{i}/playlist.m3u8"} for i in range(31)]
        out = rules(sources=srcs)
        self.assertEqual(len(out["sources"]), 30)

    def test_brand_adult_genres(self):   # кинозалы (регистр/кириллица), 18+ по имени, словарь genres.txt
        import build as B
        self.assertEqual(B.brand("минимакс-боевик"), "MiniMax")       # кириллица
        self.assertEqual(B.brand("LIBERTY СКАЗКИ"), "Liberty")        # верхний регистр
        self.assertEqual(B.brand("TEAM RuKino"), "TEAM")
        self.assertEqual(B.brand("Kernel TV Доктор"), "Kernel")
        self.assertEqual(B.brand("Clarity4K Anime"), "Clarity4K")
        self.assertIsNone(B.brand("Первый канал"))
        self.assertTrue(B.ADULT_RX.search("Fap TV Anal"))
        self.assertTrue(B.ADULT_RX.search("Big Ass Adult TV"))
        self.assertTrue(B.ADULT_RX.search("HUSTLER HD"))
        self.assertTrue(B.ADULT_RX.search("Bang Bros UHD"))
        self.assertTrue(B.ADULT_RX.search("Interracial Red TV"))
        self.assertTrue(B.ADULT_RX.search("Naughty America"))
        self.assertFalse(B.ADULT_RX.search("Adult Swim"))             # мультики — не 18+
        self.assertFalse(B.ADULT_RX.search("Red Bull TV"))            # не заканчивается на «Red TV»
        self.assertFalse(B.ADULT_RX.search("Первый канал"))
        self.assertGreater(len(B.GENRES), 100)                        # словарь загрузился (genres.txt рядом с build.py)
        self.assertGreater(len(B.FOREIGN), 100)                       # foreign.txt рядом с build.py
        self.assertEqual(B.foreign_country("WDR HD Siegen [DE]"), "Германия")   # код страны в [скобках]
        self.assertEqual(B.foreign_country("TV3 [LT]"), "Литва")
        self.assertEqual(B.foreign_country("Kidzone [Baltic]"), "Прибалтика")
        self.assertEqual(B.foreign_country("Матч ТВ"), None)          # российский — не иностранный
        self.assertEqual(B.foreign_country("CNN US"), "США")         # суффикс-код

    def test_shift_channels_sorted_by_hours(self):
        import build
        self.assertEqual(int(build.SHIFT_RX.search("Пятый канал (+4)").group(1)), 4)
        self.assertEqual(int(build.SHIFT_RX.search("Пятый канал +2 (Уфа)").group(1)), 2)
        self.assertIsNone(build.SHIFT_RX.search("Пятый Канал"))


class OpenLogos(unittest.TestCase):
    def test_keys_and_pick_order(self):
        import build as B
        self.assertIn("matchtv", B.lkeys("Матч ТВ HD"))          # латиница для кириллицы, без HD
        self.assertEqual(B.lkeys("Ю"), [])                        # короче 4 знаков — не сравниваем
        idx = {"it999": {}, "iptv-org": {}, "tv-logos": {}}
        B._ol_add(idx["iptv-org"], "Match! TV", "https://a/kz.png", "KZ", "iptv-org")
        B._ol_add(idx["iptv-org"], "Match TV", "https://a/ru.png", "RU", "iptv-org")
        B._ol_add(idx["tv-logos"], "match tv", "https://b/ru.png", "RU", "tv-logos")
        self.assertEqual(B.open_logo_pick("Матч ТВ", idx), ("https://a/ru.png", "iptv-org"))   # своя страна, база по порядку
        B._ol_add(idx["it999"], "Матч ТВ", "https://c/it.png", "RU", "it999")
        self.assertEqual(B.open_logo_pick("Матч ТВ (+2)", idx), ("https://c/it.png", "it999"))
        self.assertEqual(B.open_logo_pick("Нет такого канала", idx), (None, None))
        self.assertEqual([u for u, _ in B.open_logo_cands("Матч ТВ", idx)][:2], ["https://c/it.png", "https://a/ru.png"])   # запасные — по порядку

    def test_no_logo_rule(self):
        import build as B
        self.assertEqual(app.validate({"channels": {"тнт": {"logo": "none"}}})["channels"]["тнт"]["logo"], "none")
        self.assertNotIn("тнт", app.validate({"channels": {"тнт": {"logo": "javascript:x"}}})["channels"])
        self.assertTrue(app.BLANK_PNG.startswith(b"\x89PNG") and app.icon_type(app.BLANK_PNG))
        old = B.CONF.get("public_base")
        try:
            B.CONF["public_base"] = "http://srv:8081/pl/"
            self.assertEqual(B.nologo_url(), "http://srv:8081/live/_logo/none")
            B.CONF["public_base"] = ""
            self.assertEqual(B.nologo_url(), "")
        finally: B.CONF["public_base"] = old

    def test_rules_flag_off_by_default(self):
        self.assertFalse(app.validate({})["open_logos"])
        self.assertTrue(app.validate({"open_logos": True})["open_logos"])


class HlsErrors(unittest.TestCase):
    def test_parse_real_errors_skip_cancels(self):
        st = {}
        um = {"http://a.example/iptv/K/1": "Первый канал", "p:/iptv/K/2": "НТВ"}
        lines = [
            "[2026-11-18T11:26:10.079Z] ERROR channelHandler Channel n2712c6db cannot be started: Error: 502: http://x/y",
            "[2026-11-18T11:26:16.643Z] INFO ==> Request playlist for stream НТВ +2 [~eca36c1c] (n2712c6db)",
            "[2026-11-18T17:34:39.000Z] ERROR Request failed, no retry left: Exception: 404 : http://a.example/iptv/K/1/index.m3u8?token=SECRET",
            "[2026-11-18T17:34:40.000Z] ERROR Request failed, no retry left: Exception: Aborted by main process http://a.example/iptv/K/1/1.ts",
            "[2026-11-18T17:46:10.000Z] ERROR HLS.fetchPlaylist: Пятница! (n7d) The request is interrupted by main process",
            "[2026-11-18T18:00:00.000Z] ERROR Request failed, no retry left: Exception: ESOCKETTIMEDOUT http://cdn.example/iptv/K/2/5.ts",
            "[2026-11-18T18:00:20.000Z] ERROR Request failed, no retry left: Exception: ESOCKETTIMEDOUT http://cdn.example/iptv/K/2/6.ts",
        ]
        lists.he_parse(lines, st, um)
        self.assertEqual(sorted(st["ev"]), ["НТВ", "НТВ +2", "Первый канал"])     # отмены и переключения не считаются
        self.assertEqual(len(st["ev"]["НТВ"]), 1)                                   # пачка ошибок по кускам — один случай
        s = lists.he_summary(st, now=lists._he_epoch("2026-11-19T00:00:00Z"))
        msgs = {x["n"]: x["msg"] for x in s["items"]}
        self.assertEqual(msgs["Первый канал"], "источник ответил ошибкой 404")
        self.assertEqual(msgs["НТВ"], "источник не ответил вовремя")
        kinds = {x["n"]: (x["f24"], x["g24"]) for x in s["items"]}
        self.assertEqual(kinds["НТВ"], (0, 1))            # кусок видео не скачался — перебои, а не «не открылся»
        self.assertEqual(kinds["Первый канал"], (1, 0))   # не взят плейлист — не открылся
        self.assertEqual(lists.he_msg("aborted"), "источник оборвал соединение")
        self.assertNotIn("SECRET", json.dumps(s, ensure_ascii=False))                # ссылки с ключами не попадают в сводку
        self.assertEqual(lists.he_summary(st, now=lists._he_epoch("2026-11-21T00:00:00Z"))["items"], [])   # старше 2 суток — убираются


class Epg(unittest.TestCase):
    def test_validate(self):
        r = app.validate({"epg_sources": ["http://a.example/x.xml.gz", " https://b.example/e.xml ", "javascript:x", "http://a.example/x.xml.gz"], "epg_days": 7})
        self.assertEqual(r["epg_sources"], ["http://a.example/x.xml.gz", "https://b.example/e.xml"])   # без мусора и повторов
        self.assertEqual(r["epg_days"], 7)
        self.assertIsNone(app.validate({"epg_days": 99})["epg_days"])
        self.assertIsNone(app.validate({"epg_days": True})["epg_days"])
        self.assertEqual(app.validate({})["epg_sources"], [])

    def test_sync_hls_epg(self):
        import tempfile, subprocess as sp
        d = tempfile.mkdtemp(dir=TMP); p = os.path.join(d, "local.json")
        json.dump({"SERVER": {"password": "x"}, "clients": {}, "epg": {"tvGuideUrl": ["http://epg.it999.ru/edem.xml.gz"], "historyDays": 3}}, open(p, "w"))
        old = (lists.CONF, lists.E4.get("mode"), lists.os.chown, sp.run, lists.EPG_FLAG, lists.EPG_STATUS)
        calls = []
        lists.CONF = p; lists.E4["mode"] = "hls"; lists.os.chown = lambda *a, **k: None; sp.run = lambda *a, **k: calls.append(a)
        lists.EPG_FLAG = os.path.join(d, "flag"); lists.EPG_STATUS = os.path.join(d, "st.json")
        try:
            self.assertFalse(lists.sync_hls_epg({}))                                   # пусто — как при установке, перезапуска нет
            self.assertTrue(lists.sync_hls_epg({"epg_sources": ["http://b.example/e.xml"], "epg_days": 7}))
            e = json.load(open(p))["epg"]
            self.assertEqual((e["tvGuideUrl"], e["historyDays"]), (["http://b.example/e.xml"], 7))
            self.assertTrue(os.path.exists(lists.EPG_FLAG))                            # новые источники — скачать сразу
            self.assertEqual(len(calls), 1)                                            # один перезапуск HLS Proxy
            self.assertFalse(lists.sync_hls_epg({"epg_sources": ["http://b.example/e.xml"], "epg_days": 7}))   # без изменений — без перезапуска
        finally:
            lists.CONF, lists.E4["mode"], lists.os.chown, sp.run, lists.EPG_FLAG, lists.EPG_STATUS = old


class StreamFormat(unittest.TestCase):
    def test_new_client_gets_hls_not_mpegts(self):
        import tempfile, subprocess as sp
        d = tempfile.mkdtemp(dir=TMP); p = os.path.join(d, "local.json")
        json.dump({"clients": {"tv": {"name": "tv", "password": "x" * 20, "isHttp": True, "maxStreamCount": 2, "n": 0}}}, open(p, "w"))
        old = (lists.CONF, lists.E4.get("mode"), lists.os.chown, sp.run, lists.time.sleep)
        lists.CONF = p; lists.E4["mode"] = "hls"; lists.os.chown = lambda *a, **k: None; sp.run = lambda *a, **k: None; lists.time.sleep = lambda *a: None
        try:
            act = [{"id": "abc", "hls_pw": "p" * 20, "streams": 1}]
            self.assertTrue(lists.sync_hls_clients(act, "hls"))
            c = json.load(open(p))["clients"]
            self.assertFalse(c["tv"]["isHttp"]); self.assertFalse(c[lists.HPFX + "abc"]["isHttp"])   # новый клиент — без галки MPEG-TS
            self.assertFalse(lists.sync_hls_clients(act, "hls"))                                     # второй раз — ничего не меняется
            self.assertTrue(lists.sync_hls_clients(act, "ts"))
            self.assertTrue(json.load(open(p))["clients"][lists.HPFX + "abc"]["isHttp"])
        finally:
            lists.CONF, lists.E4["mode"], lists.os.chown, sp.run, lists.time.sleep = old


class Watchers(unittest.TestCase):
    def test_status_page_is_not_a_viewer_but_stream_is(self):
        W = app.watchers
        last, hist = {}, {}
        tab, tv = ("10.0.0.5", "50000"), ("10.0.0.9", "41000")
        cur = {tab: 150000}
        self.assertEqual(W(cur, last, hist, 0, first=True), {})   # то, что отдано до запуска, не считаем
        last = cur
        for t in range(2, 32, 2):   # страница статуса: ~1 КБ/с
            cur = {tab: last[tab] + 1800}
            self.assertEqual(W(cur, last, hist, t), {}); last = cur
        cur = dict(last); cur[tv] = 400000   # плеер: за 2 секунды ушло 400 КБ
        self.assertEqual(W(cur, last, hist, 32), {"10.0.0.9": 1})


class Archive(unittest.TestCase):
    """Архив: передача utc/lutc в переадресации и Xtream, /timeshift/, метка архива у склеенных каналов."""
    @classmethod
    def setUpClass(cls):
        r = rules(); r["clients"][0]["link_mode"] = "redirect"; build(r)
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = json.load(open(f"{TMP}/data/editor/xc.json"))["mama_tv"]["pw"]

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    get = Xtream.get

    def test_helpers(self):
        self.assertEqual(app.add_query("http://p/4", "/live/_r/t/h?utc=1&lutc=2"), "http://p/4?utc=1&lutc=2")
        self.assertEqual(app.add_query("http://p/4?a=1", "/x?utc=1"), "http://p/4?a=1&utc=1")
        self.assertEqual(app.add_query("http://p/4", "/x?u=<script>"), "http://p/4", "чужие знаки не передаём")
        self.assertEqual(lists.arch_days('#EXTINF:-1 tvg-rec="7",A'), 7)
        self.assertEqual(lists.arch_days('#EXTINF:-1 catchup="shift" catchup-days="5",A'), 5)
        self.assertEqual(lists.arch_days('#EXTINF:-1 tvg-rec="0",A'), 0)

    def test_redirect_passes_utc(self):
        d = app.links(); tok, hx = next((t, h) for t, hs in d["t"].items() for h in hs if d["u"][h] == "http://p/4")
        code, h, _ = self.get(f"/live/_r/{tok}/{hx}?utc=100&lutc=200")
        self.assertEqual((code, h["Location"]), (302, "http://p/4?utc=100&lutc=200"))

    def test_xtream_archive(self):
        st = json.loads(self.get(f"/player_api.php?username=mama_tv&password={self.pw}&action=get_live_streams")[2])
        arch = [s for s in st if s["tv_archive"]]
        self.assertEqual([(s["name"], s["tv_archive_duration"]) for s in arch], [("Новости 24", 3)])
        sid = arch[0]["stream_id"]; t = time.strftime("%Y-%m-%d:%H-%M", time.localtime(time.time() - 3600))
        code, h, _ = self.get(f"/timeshift/mama_tv/{self.pw}/60/{t}/{sid}.ts")
        self.assertEqual(code, 302); self.assertRegex(h["Location"], r"[?&]utc=\d+&lutc=\d+$")
        self.assertEqual(self.get(f"/streaming/timeshift.php?username=mama_tv&password={self.pw}&stream={sid}&start={t}&duration=60")[0], 302)
        no = next(s for s in st if not s["tv_archive"])["stream_id"]
        self.assertEqual(self.get(f"/timeshift/mama_tv/{self.pw}/60/{t}/{no}.ts")[0], 404, "у канала без архива — нет")
        self.assertEqual(self.get(f"/timeshift/mama_tv/bad/60/{t}/{sid}.ts")[0], 403)
        m3u = self.get(f"/get.php?username=mama_tv&password={self.pw}&type=m3u_plus")[2]
        self.assertIn('catchup="append" catchup-days="3"', m3u)
        code, h, _ = self.get(f"/live/mama_tv/{self.pw}/{sid}.ts?utc=5&lutc=6")
        self.assertTrue(h["Location"].endswith("utc=5&lutc=6"), "Xtream /live/ передаёт архив дальше")

    def test_catchup_formats(self):   # архив в формате провайдера (catchup.py)
        import catchup as C
        S, N = 1700000000, 1700003600
        sp = C.spec
        self.assertIsNone(sp('#EXTINF:-1 tvg-rec="7",A'))                                   # обычный ?utc&lutc — правило не нужно
        self.assertIsNone(sp('#EXTINF:-1 catchup="shift" catchup-days="3",A'))
        self.assertEqual(sp('#EXTINF:0 catchup-type="flussonic-ts" catchup-days="6",A'), {"t": "flussonic-ts", "s": ""})
        self.assertEqual(sp('#EXTINF:-1 catchup="append" catchup-source="?offset=-${offset}",A'), {"t": "append", "s": "?offset=-${offset}"})
        au = C.archive_url
        self.assertEqual(au("http://e.tv/iptv/KEY/204/index.m3u8", None, S, N), f"http://e.tv/iptv/KEY/204/index.m3u8?utc={S}&lutc={N}")   # Edem/iLook
        self.assertEqual(au("http://v.tv/1.m3u8?t=1", None, S, N), f"http://v.tv/1.m3u8?t=1&utc={S}&lutc={N}")
        self.assertEqual(au("https://am01.spr24.net/128/mpegts?token=abc", {"t": "flussonic-ts", "s": ""}, S, N),
                         f"https://am01.spr24.net/128/timeshift_abs-{S}.ts?token=abc")                                          # Sharavoz
        self.assertEqual(au("https://am01.spr24.net/128/mpegts?token=abc", None, S, N), f"https://am01.spr24.net/128/timeshift_abs-{S}.ts?token=abc",
                         "Sharavoz без меток — по адресу")
        self.assertEqual(au("http://f.tv/ch/index.m3u8?token=x", {"t": "flussonic", "s": ""}, S, N), "http://f.tv/ch/timeshift_rel-3600.m3u8?token=x")
        self.assertEqual(au("http://s1.troya.one:8080/ch/mono.m3u8?token=x", None, S, N), "http://s1.troya.one:8080/ch/mono-timeshift_rel-3600.m3u8?token=x")   # TVTeam
        self.assertEqual(au("http://f.tv/ch", {"t": "flussonic", "s": ""}, S, N), f"http://f.tv/ch?utc={S}&lutc={N}", "непонятный адрес — как обычно")
        self.assertEqual(au("http://w.tv/CH_1/index.m3u8?a=1", {"t": "append", "s": "?offset=-${offset}"}, S, N), "http://w.tv/CH_1/index.m3u8?a=1&offset=-3600")   # Wink
        self.assertEqual(au("http://z.tv/x.m3u8", {"t": "append", "s": "?utcstart=${start}&utcend=${end}"}, S, N, 1800), f"http://z.tv/x.m3u8?utcstart={S}&utcend={S + 1800}")
        self.assertEqual(au("http://a.tv/1", {"t": "default", "s": "http://arc.tv/1/{Y}{m}{d}-{H}{M}.m3u8?s={utc}&e={lutc}"}, S, N),
                         f"http://arc.tv/1/20231114-2213.m3u8?s={S}&e={N}")
        self.assertEqual(lists.arch_utc('#EXTINF:0 catchup-type="flussonic-ts" catchup-days="6" group-title="G",A', 6),
                         '#EXTINF:0 tvg-rec="6" catchup="append" catchup-days="6" catchup-source="?utc={utc}&lutc={lutc}" group-title="G",A')

    def test_redirect_flussonic(self):   # «перенаправление»: плеер шлёт ?utc&lutc, сервер отдаёт формат провайдера
        lk = lists.Links([]); lk.base = "http://tv.test"
        l, how = lk.apply(['#EXTINF:0 catchup-type="flussonic-ts" catchup-days="6",Шара', "https://am01.spr24.net/128/mpegts?token=abc"], "ext", "redirect", "tokX", lp=False)
        self.assertEqual(how, "redirect"); self.assertIn('catchup="append"', l[0]); self.assertNotIn("flussonic", l[0])
        hx = l[1].rsplit("/", 1)[1]
        self.assertEqual(lk.c[hx], {"t": "flussonic-ts", "s": ""})
        l2, _ = lk.apply(['#EXTINF:-1 tvg-rec="7",Едем', "http://e.tv/iptv/K/1/index.m3u8"], "ext", "redirect", "tokX", lp=False)
        self.assertEqual(l2[0], '#EXTINF:-1 tvg-rec="7",Едем', "обычный архив — метки не трогаем"); self.assertEqual(len(lk.c), 1)
        d = json.load(open(app.LINKS_FILE))
        d["u"][hx] = lk.u[hx]; d["t"].setdefault("tokX", []).append(hx); d.setdefault("c", {})[hx] = lk.c[hx]
        json.dump(d, open(app.LINKS_FILE, "w")); os.utime(app.LINKS_FILE, (time.time() + 5, time.time() + 5))
        s = int(time.time()) - 3600
        code, h, _ = self.get(f"/live/_r/tokX/{hx}?utc={s}&lutc={s + 3600}")
        self.assertEqual((code, h["Location"]), (302, f"https://am01.spr24.net/128/timeshift_abs-{s}.ts?token=abc"))
        self.assertEqual(self.get(f"/live/_r/tokX/{hx}")[1]["Location"], "https://am01.spr24.net/128/mpegts?token=abc", "без utc — прямой эфир")

    def test_xtream_direct_flussonic(self):   # Xtream со ссылкой прямо на провайдера: /timeshift/ сразу в его формате
        p = f"{TMP}/data/editor/xc.json"; orig = open(p).read(); d = json.loads(orig)
        ch = d["mama_tv"]["channels"]; ch.append({"name": "Шара", "group": "G", "logo": "", "tvg_id": "", "arch": 6,
                                                  "url": "https://am01.spr24.net/128/mpegts?token=abc", "cu": {"t": "flussonic-ts", "s": ""}})
        json.dump(d, open(p, "w"))
        try:
            sid = len(ch); t = time.strftime("%Y-%m-%d:%H-%M", time.localtime(time.time() - 3600)); s = int(time.mktime(time.strptime(t, "%Y-%m-%d:%H-%M")))
            code, h, _ = self.get(f"/timeshift/mama_tv/{self.pw}/60/{t}/{sid}.ts")
            self.assertEqual((code, h["Location"]), (302, f"https://am01.spr24.net/128/timeshift_abs-{s}.ts?token=abc"))
            code, h, _ = self.get(f"/live/mama_tv/{self.pw}/{sid}.ts?utc={s}&lutc={s + 60}")
            self.assertEqual(h["Location"], f"https://am01.spr24.net/128/timeshift_abs-{s}.ts?token=abc", "M3U из get.php: ?utc -> формат провайдера")
            self.assertEqual(self.get(f"/live/mama_tv/{self.pw}/{sid}.ts")[1]["Location"], "https://am01.spr24.net/128/mpegts?token=abc")
        finally:
            open(p, "w").write(orig)
        own = lists.xc_channels(['#EXTINF:-1 tvg-rec="3",A', "http://tv.test/pl/x", '#EXTINF:-1 tvg-rec="3",B', "http://prov/1", '#EXTINF:-1,C', "http://prov/2"], {"tv.test"})
        self.assertEqual([("cu" in c, c.get("cu")) for c in own], [(False, None), (True, {}), (False, None)])

    def test_merged_archive_mark(self):
        lk = lists.Links([]); lk.arc = {"канал": 0, "другой": 3}; lk.orig = {"канал": "http://a/1", "другой": "http://b/1"}
        l, _ = lk.apply(['#EXTINF:-1 tvg-rec="7" catchup="shift" group-title="G",Канал', "http://srv/ch/1"], "hls", "hls", "t", lp=False)
        self.assertEqual(l[0], '#EXTINF:-1 group-title="G",Канал', "у основного источника архива нет — метку убираем")
        l, _ = lk.apply(['#EXTINF:-1 tvg-rec="3" group-title="G",Другой', "http://srv/ch/2"], "hls", "hls", "t", lp=False)
        self.assertIn('tvg-rec="3"', l[0])


class AutoCheck(unittest.TestCase):
    """Автопроверка потоков (autocheck.py) и её API в редакторе: настройки Telegram, кнопки."""
    @classmethod
    def setUpClass(cls):
        import autocheck
        cls.ac = autocheck
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls): cls.srv.shutdown()

    def api(self, path, body=None):
        rq = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                    headers={"Host": "localhost", "Content-Type": "application/json", "X-M3U-Editor": "1"})
        try:
            with urllib.request.urlopen(rq, timeout=10) as r: return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read().decode() or "{}")

    def test_channels_dedup_and_groups(self):
        p = f"{TMP}/ac.m3u"
        open(p, "w", encoding="utf-8").write('#EXTM3U\n#EXTINF:-1 group-title="Кино",Кино 1\nhttp://a/1\n#EXTINF:-1,кино 1\nhttp://b/1\n'
                                             '#EXTINF:-1,Без группы\n#EXTGRP:Разное\nudp://@239.0.0.1:1234\n#EXTINF:-1,\nhttp://c/1\n')
        ch = self.ac.channels(p)
        self.assertEqual([(c["name"], c["group"], c["url"]) for c in ch], [("Кино 1", "Кино", "http://a/1"), ("Без группы", "Разное", "udp://@239.0.0.1:1234")])
        self.assertIsNone(self.ac.channels(f"{TMP}/нет.m3u"))
        self.assertEqual(self.ac.probe("udp://@239.0.0.1:1234")[0], None)

    def test_summary(self):
        res = [{"name": "A", "group": "", "ok": True, "msg": ""}, {"name": "B", "group": "", "ok": False, "msg": "404"},
               {"name": "C", "group": "", "ok": None, "msg": ""}]
        first = self.ac.summarize(res, {})
        self.assertEqual((first["new_bad"], first["recovered"]), ([], []))   # первая проверка — без «перестали работать»
        self.assertEqual([c["name"] for c in self.ac.in_main([{"name": "Кино 1"}, {"name": "Старый"}])], ["Кино 1"])   # только основной плейлист
        s = self.ac.summarize(res, {"ts": 1, "bad": [{"name": "Z"}]})
        self.assertEqual((s["total"], s["ok"], s["skipped"], s["new_bad"], s["recovered"], s["source_down"]), (3, 1, 1, ["B"], ["Z"], False))
        s2 = self.ac.summarize(res, s)
        self.assertEqual((s2["new_bad"], s2["recovered"]), ([], []))
        many = self.ac.summarize([{"name": n, "group": "", "ok": False, "msg": ""} for n in "DEF"], {})
        self.assertTrue(many["source_down"])
        self.assertFalse(self.ac.summarize([], {})["source_down"])

    def test_retry_slow_channels(self):
        calls = []
        def fake(u, timeout=10):
            calls.append((u, timeout))
            return (timeout > 10 or u != "http://slow", f"t{timeout}")
        chs = [{"name": "A", "group": "", "url": "http://ok"}, {"name": "B", "group": "", "url": "http://slow"}]
        res = self.ac.run_check(chs, 2, fake)
        self.assertEqual([(r["name"], r["ok"], r["msg"]) for r in res], [("A", True, "t10"), ("B", True, "t20")])
        self.assertEqual(sorted(calls), [("http://ok", 10), ("http://slow", 10), ("http://slow", 20)])   # повтор только для упавших

    def test_scheduled_respects_switch(self):
        if os.path.exists(self.ac.CONF): os.remove(self.ac.CONF)
        self.assertEqual(self.ac.main(["--scheduled"]), 0)            # по умолчанию автопроверка выключена — ничего не делает
        self.assertFalse(os.path.exists(self.ac.RESULT))

    def test_conf_api(self):
        code, j = self.api("/api/autocheck")
        self.assertEqual(code, 200); self.assertEqual(j["conf"], {"auto": False, "workers": 2})
        code, j = self.api("/api/autocheck-conf", {"auto": True, "workers": 99})
        self.assertEqual((code, j["conf"]), (200, {"auto": True, "workers": 6}))
        self.assertEqual(self.ac.settings(), {"auto": True, "workers": 6})   # autocheck.py читает то, что записал редактор
        self.assertEqual(self.api("/api/autocheck-conf", {"auto": False, "workers": "x"})[1]["conf"], {"auto": False, "workers": 2})

    def test_he_hide(self):   # «Не открылось у клиентов»: скрыть до следующей ошибки, вернуть
        if os.path.exists(app.HE_HIDDEN): os.remove(app.HE_HIDDEN)
        code, j = self.api("/api/he-hide", {"items": [{"n": "Первый канал", "ts": "2026-11-19T08:53:00Z"}, {"n": "", "ts": "x"}, "мусор"]})
        self.assertEqual((code, j["hidden"]), (200, {"Первый канал": "2026-11-19T08:53:00Z"}))
        self.assertEqual(self.api("/api/status")[1]["he_hidden"], {"Первый канал": "2026-11-19T08:53:00Z"})
        self.assertEqual(self.api("/api/he-hide", {"items": [{"n": "Первый канал"}], "unhide": True})[1]["hidden"], {})
        many = [{"n": f"к{i}", "ts": "t"} for i in range(520)]
        h = self.api("/api/he-hide", {"items": many})[1]["hidden"]
        self.assertEqual((len(h), "к0" in h, "к519" in h), (500, False, True))   # не больше 500, уходят самые старые

    def test_run_button(self):
        if os.path.exists(app.CTRIGGER): os.remove(app.CTRIGGER)
        self.assertEqual(self.api("/api/autocheck-run", {})[0], 400)        # служба не установлена — честная ошибка
        open(app.CTRIGGER, "w").close()
        self.assertEqual(self.api("/api/autocheck-run", {})[0], 200)
        self.assertTrue(open(app.CTRIGGER).read())
        rq = urllib.request.Request(self.base + "/api/autocheck-run", data=b"{}", headers={"Host": "localhost", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(rq, timeout=10)   # без заголовка — CSRF-защита
        self.assertEqual(e.exception.code, 403)

    def test_progress_pause_stop(self):   # ход проверки, пауза и остановка по autocheck.ctl
        pp, ctl = f"{TMP}/data/editor/p-test.json", f"{TMP}/data/editor/ctl-test"
        open(ctl, "w").write("stop")                                       # старая «стоп» сбрасывается при старте
        chs = [{"name": f"K{i}", "group": "", "url": f"http://x/{i}"} for i in range(6)]
        seen = []
        def fake(u, timeout=10):
            seen.append(u); time.sleep(0.05); return (u != "http://x/5" or timeout > 10), "ok"
        prog = self.ac.Progress(len(chs), pp, ctl, every=0.05).start()
        self.assertEqual(open(ctl).read(), "")
        res = self.ac.run_check(chs, 1, fake, prog); prog.finish()
        p = json.load(open(pp))
        self.assertEqual([r["ok"] for r in res], [True] * 6)
        self.assertEqual((p["running"], p["stopped"], p["done"], p["total"], p["phase"], p["again_total"], p["again_done"]),
                         (False, False, 6, 6, "again", 1, 1))
        # пауза: новые каналы не берутся; продолжить — доделывает
        seen.clear(); open(ctl, "w").write("pause")
        prog = self.ac.Progress(len(chs), pp, ctl, every=0.05)
        prog.thread.start(); prog.poll()                                   # без start(): ctl не сбрасываем
        out = {}
        t = threading.Thread(target=lambda: out.update(r=self.ac.run_check(chs, 2, fake, prog))); t.start()
        time.sleep(0.4)
        self.assertEqual(seen, []); self.assertTrue(json.load(open(pp))["paused"])
        open(ctl, "w").write(""); t.join(5)
        self.assertEqual(len(out["r"]), 6); prog.finish()
        # остановка: run_check -> None, остальные каналы не проверяются
        seen.clear()
        prog = self.ac.Progress(len(chs), pp, ctl, every=0.02).start()
        def slow(u, timeout=10):
            seen.append(u)
            if len(seen) == 2: open(ctl, "w").write("stop")
            time.sleep(0.1); return True, "ok"
        self.assertIsNone(self.ac.run_check(chs, 1, slow, prog)); prog.finish(stopped=True)
        self.assertLess(len(seen), 6); self.assertTrue(json.load(open(pp))["stopped"]); self.assertEqual(open(ctl).read(), "")

    def test_progress_api(self):
        open(app.CTRIGGER, "w").close()
        if os.path.exists(app.AC_PROGRESS): os.remove(app.AC_PROGRESS)
        self.assertEqual(self.api("/api/autocheck-ctl", {"cmd": "pause"})[0], 400)   # не идёт — нечего ставить на паузу
        json.dump({"running": True, "upd": int(time.time()), "done": 3, "total": 10}, open(app.AC_PROGRESS, "w"))
        self.assertEqual(self.api("/api/autocheck")[1]["progress"]["done"], 3)
        self.assertEqual(self.api("/api/autocheck-run", {})[0], 409)                 # вторая проверка поверх первой
        self.assertEqual(self.api("/api/autocheck-ctl", {"cmd": "rm -rf"})[0], 400)
        self.assertEqual(self.api("/api/autocheck-ctl", {"cmd": "pause"})[0], 200); self.assertEqual(open(app.AC_CTL).read(), "pause")
        self.assertEqual(self.api("/api/autocheck-ctl", {"cmd": "resume"})[0], 200); self.assertEqual(open(app.AC_CTL).read(), "")
        self.assertEqual(self.api("/api/autocheck-ctl", {"cmd": "stop"})[0], 200); self.assertEqual(open(app.AC_CTL).read(), "stop")
        json.dump({"running": True, "upd": int(time.time()) - 600}, open(app.AC_PROGRESS, "w"))   # процесс умер
        p = self.api("/api/autocheck")[1]["progress"]
        self.assertEqual((p["running"], p["stale"]), (False, True))
        self.assertEqual(self.api("/api/autocheck-run", {})[0], 200); self.assertEqual(open(app.AC_CTL).read(), "")   # старая «стоп» сброшена
        os.remove(app.AC_PROGRESS)

    def test_installer_has_units(self):
        s = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
        for x in ("autocheck.py notify.py e4conf.py", "e4tv-check.timer e4tv-notify.timer", "OnUnitActiveSec=5min", "check.trigger domain.request; do", "e4tv-check.path e4tv-check.timer", "Unit=e4tv-check-now.service",
                  "OnCalendar=*-*-* 06:30:00"):
            self.assertIn(x, s)
        self.assertNotIn("telegram", open(os.path.join(SRC, "autocheck.py"), encoding="utf-8").read().lower())   # уведомления убраны по просьбе


class BuildNoSources(unittest.TestCase):
    def test_disabled_sources_clear_combined(self):
        """Все источники выключены — общий плейлист очищается, а не остаётся прошлым (решение 17.11.2026)."""
        d = tempfile.mkdtemp(prefix="e4tv-nosrc-")
        os.makedirs(f"{d}/data/editor"); os.makedirs(f"{d}/hls")
        json.dump({**CONF, "data": f"{d}/data", "pl_dir": f"{d}/pl", "hls_dir": f"{d}/hls"}, open(f"{d}/c.json", "w"))
        json.dump({"sources": [{"id": "s1", "name": "Выкл", "url": "http://127.0.0.1:9/x.m3u", "enabled": False}]},
                  open(f"{d}/data/editor/rules.json", "w"), ensure_ascii=False)
        out = f"{d}/data/combined.m3u"
        with open(out, "w", encoding="utf-8") as f: f.write('#EXTM3U\n#EXTINF:-1,Старый\nhttp://old/1\n')
        r = subprocess.run([sys.executable, "-B", os.path.join(SRC, "build.py")], env={**os.environ, "E4TV_CONF": f"{d}/c.json"},
                           capture_output=True, text=True, timeout=60)
        try:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertEqual(open(out, encoding="utf-8").read(), "#EXTM3U\n")
            self.assertIn("Включённых источников нет", r.stdout)   # final.json тут не проверяем: без root не пишется (chown)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class NoMerge(unittest.TestCase):
    """«Не склеивать с другими»: метка в имени для HLS Proxy, клиент видит обычное название (решение 17.11.2026)."""
    def test_build_marks_only_nomerge_source(self):
        d = tempfile.mkdtemp(prefix="e4tv-nomerge-")
        os.makedirs(f"{d}/data/editor"); os.makedirs(f"{d}/data/build"); os.makedirs(f"{d}/hls")
        json.dump({**CONF, "data": f"{d}/data", "pl_dir": f"{d}/pl", "hls_dir": f"{d}/hls"}, open(f"{d}/c.json", "w"))
        json.dump({"primary_source": "Шаравоз", "sources": [
            {"id": "s1", "name": "Шаравоз", "url": "http://src.test/1.m3u", "enabled": True},
            {"id": "s2", "name": "Свободный", "url": "http://src.test/2.m3u", "enabled": True, "nomerge": True}]},
            open(f"{d}/data/editor/rules.json", "w"), ensure_ascii=False)
        run = ("import sys; sys.path.insert(0, %r); import build\n"
               "S = {'http://src.test/1.m3u': '#EXTM3U\\n#EXTINF:-1 group-title=\"Общие\",Первый канал\\nhttp://a/1\\n',\n"
               "     'http://src.test/2.m3u': '#EXTM3U\\n#EXTINF:-1 group-title=\"Общие\",Первый канал\\nhttp://b/1\\n'\n"
               "                              '#EXTINF:-1 group-title=\"Общие\",Только свой\\nhttp://b/2\\n'}\n"
               "build.fetch = lambda u, t=120: S[u].encode(); build.it999_ids = lambda: ({}, 'тест')\n"
               "build.main()\n") % SRC
        r = subprocess.run([sys.executable, "-B", "-c", run], env={**os.environ, "E4TV_CONF": f"{d}/c.json"},
                           capture_output=True, text=True, timeout=120)
        try:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            t = open(f"{d}/data/combined.m3u", encoding="utf-8").read()
            self.assertRegex(t, r",Первый канал\nhttp://a/1")
            self.assertRegex(t, r",Первый канал \[~s2\]\nhttp://b/1")
            self.assertRegex(t, r",Только свой \[~s2\]\nhttp://b/2")
            self.assertIn("Не склеиваются с другими источниками: 2", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_channel_primary_source(self):
        """Свой основной источник у канала: его поток первым (HLS Proxy считает первый основным)."""
        d = tempfile.mkdtemp(prefix="e4tv-prim-")
        os.makedirs(f"{d}/data/editor"); os.makedirs(f"{d}/data/build"); os.makedirs(f"{d}/hls")
        json.dump({**CONF, "data": f"{d}/data", "pl_dir": f"{d}/pl", "hls_dir": f"{d}/hls"}, open(f"{d}/c.json", "w"))
        json.dump({"primary_source": "А", "channels": {"первый канал": {"primary": "Б"}}, "sources": [
            {"id": "s1", "name": "А", "url": "http://src.test/1.m3u", "enabled": True},
            {"id": "s2", "name": "Б", "url": "http://src.test/2.m3u", "enabled": True}]},
            open(f"{d}/data/editor/rules.json", "w"), ensure_ascii=False)
        run = ("import sys; sys.path.insert(0, %r); import build\n"
               "S = {'http://src.test/1.m3u': '#EXTM3U\\n#EXTINF:-1 group-title=\"Общие\",Первый канал\\nhttp://a/1\\n#EXTINF:-1 group-title=\"Общие\",Второй\\nhttp://a/2\\n',\n"
               "     'http://src.test/2.m3u': '#EXTM3U\\n#EXTINF:-1 group-title=\"Общие\",Первый канал\\nhttp://b/1\\n#EXTINF:-1 group-title=\"Общие\",Второй\\nhttp://b/2\\n'}\n"
               "build.fetch = lambda u, t=120: S[u].encode(); build.it999_ids = lambda: ({}, 'тест')\n"
               "build.main()\n") % SRC
        r = subprocess.run([sys.executable, "-B", "-c", run], env={**os.environ, "E4TV_CONF": f"{d}/c.json"},
                           capture_output=True, text=True, timeout=120)
        try:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            t = open(f"{d}/data/combined.m3u", encoding="utf-8").read()
            self.assertLess(t.index("http://b/1"), t.index("http://a/1"), "у канала основной — Б")
            self.assertLess(t.index("http://a/2"), t.index("http://b/2"), "у остальных — общий основной А")
            self.assertIn("свой основной у 1", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unmark(self):
        self.assertEqual(lists.unmark(['#EXTINF:-1 group-title="Общие",Первый канал [~s2]', "http://hls/1"])[0],
                         '#EXTINF:-1 group-title="Общие",Первый канал')
        same = ['#EXTINF:-1,Канал [HD]', "http://x"]   # обычные квадратные скобки в названии не трогаем
        self.assertEqual(lists.unmark(same), same)

    def test_quality_aliases(self):
        """HLS Proxy отрезает HD/FHD/UHD/4K в конце названия — канал всё равно находится (18.11.2026)."""
        fm = {"кинопремьера hd": ["кинопремьера hd", "Кино"], "известия": ["известия", "Новости"],
              "известия hd": ["известия hd", "Новости"], "мульт  4k": ["мульт  4k", "Детские"], "hd": ["hd", "Разное"],
              "первый канал hd [~s2]": ["первый канал hd", "Эфирные"]}
        a = lists.with_quality_aliases(fm)
        self.assertEqual(a["кинопремьера"][0], "кинопремьера hd")
        self.assertEqual(a["известия"][0], "известия")          # точное имя важнее запасного
        self.assertEqual(a["мульт"][0], "мульт  4k")
        self.assertNotIn("", a)
        self.assertNotIn("первый канал [~s2]", a)              # метку «не склеивать» HLS Proxy не трогает

    def test_rules_keep_flag(self):
        r = app.validate({"sources": [{"id": "s2", "name": "Св", "url": "http://src.test/2.m3u", "nomerge": True},
                                      {"id": "s3", "name": "Др", "url": "http://src.test/3.m3u", "nomerge": "да"}]})
        self.assertEqual([x.get("nomerge") for x in r["sources"]], [True, None])


class ProvDetect(unittest.TestCase):
    """Автоопределение провайдера: домены потоков без токенов, своё число устройств, отметка «авто» (18.11.2026)."""
    def test_stream_domains(self):
        urls = ["http://ru01.plink-pile.ch/a/b/index.m3u8?token=SECRET", "http://ru02.plink-pile.ch/c", "https://user:pw@cdn.x.org:8080/s",
                "http://10.0.0.5/live", "udp://@239.0.0.1:1234", "not a url", "http://ru03.PLINK-pile.ch/d"]
        d = app.stream_domains(urls)
        self.assertEqual(d[0], "plink-pile.ch")
        self.assertIn("x.org", d)
        self.assertFalse(any("SECRET" in x or "/" in x or "user" in x for x in d))
        self.assertEqual(app.stream_domains(["http://10.0.0.5/live"]), ["10.0.0.5"])
        import importlib.util
        spec = importlib.util.spec_from_file_location("e4build", os.path.join(SRC, "build.py")); b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        self.assertEqual(b.stream_domains(urls), d)   # сервер и сборка считают одинаково

    def test_rules_keep_lim_and_auto(self):
        r = app.validate({"sources": [
            {"id": "a1", "name": "А", "url": "http://x.ottclub.net/playlists/uplist/k/playlist.m3u8", "prov": "ottclub", "pa": True, "lim": 3},
            {"id": "a2", "name": "Б", "url": "http://y.test/2.m3u", "pa": True, "lim": 0},
            {"id": "a3", "name": "В", "url": "http://y.test/3.m3u", "lim": True},
            {"id": "a4", "name": "Г", "url": "http://y.test/4.m3u", "lim": 21}]})
        s = r["sources"]
        self.assertEqual((s[0].get("prov"), s[0].get("pa"), s[0].get("lim")), ("ottclub", True, 3))
        self.assertEqual((s[1].get("pa"), s[1].get("lim")), (None, None))   # «авто» без провайдера не храним; 0 — не число устройств
        self.assertIsNone(s[2].get("lim")); self.assertIsNone(s[3].get("lim"))

    def test_prov_icons(self):
        """Значки провайдеров: только картинки (не SVG), только сайты из списка, списки сервера и страницы совпадают."""
        self.assertEqual(app.icon_type(b"\x89PNG\r\n\x1a\nxxxx"), "image/png")
        self.assertEqual(app.icon_type(b"\x00\x00\x01\x00rest"), "image/x-icon")
        self.assertIsNone(app.icon_type(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"))
        self.assertIsNone(app.icon_type(b"<html>"))
        self.assertIsNone(app.prov_icon("tvteam"))   # нет подтверждённого сайта — сеть не трогаем
        self.assertIsNone(app.prov_icon("evil.example"))
        h = open(os.path.join(SRC, "index.html"), encoding="utf-8").read()
        m = re.search(r"const PROV_ICONS=new Set\(\[([^\]]*)\]\)", h)
        self.assertEqual(set(re.findall(r'"([a-z0-9]+)"', m.group(1))), set(app.PROV_SITES))
        ids = set(re.findall(r'\{id:"([a-z0-9]+)"', h[h.index("const PROV=["):h.index("const UNI=")]))
        self.assertTrue(set(app.PROV_SITES) <= ids)   # значки только для существующих шаблонов
        self.assertTrue(set(app.PROV_TG) <= ids)
        self.assertTrue(app.TG_IMG_RX.match("https://cdn4.telesco.pe/file/AbC_d-9.jpg"))
        self.assertFalse(app.TG_IMG_RX.match("https://evil.example/file/x.jpg"))
        self.assertFalse(app.TG_IMG_RX.match("https://cdn4.telesco.pe.evil.example/file/x.jpg"))
        self.assertIn('data-href="${esc(p.site)}"', h)   # плитки: «Сайт ↗» / «Канал ↗»
        self.assertNotIn('"ссылка из кабинета · "', h)

    def test_page_has_detection(self):
        h = open(os.path.join(SRC, "index.html"), encoding="utf-8").read()
        for s in ("function detectProv(", 'id:"ottclub"', 'h:["plink-pile.ch"]', 'class="sDev"', "provHint("):
            self.assertIn(s, h)


class Misc(unittest.TestCase):
    def test_activity_parser(self):
        log = f"{TMP}/pl.log"
        open(log, "w").write("2026-11-16T08:40:01+03:00 1.2.3.4 200 /pl/TOKENTOKENTOKENTOKEN12.m3u8 OTT Navigator/1.7 (Linux)\n"
                             "2026-11-16T08:41:01+03:00 1.2.3.4 404 /pl/nope.m3u8 VLC/3\n")
        lists.ALOG = log
        lists.activity(); lists.activity()   # второй проход не должен считать заново
        a = json.load(open(lists.ACT))["TOKENTOKENTOKENTOKEN12"]
        self.assertEqual((a["ua"], sum(a["days"].values())), ("OTT Navigator", 1))

    def test_acl_text(self):
        import importlib
        L2 = importlib.reload(lists)
        L2.ACL = f"{TMP}/acl.conf"
        real_isdir = os.path.isdir
        L2.os.path.isdir = lambda p: True if p == "/etc/nginx" else real_isdir(p)
        L2.subprocess = None
        import subprocess as sp
        orig = sp.run; sp.run = lambda *a, **k: type("R", (), {"returncode": 0})()
        try: L2.write_acl([{"id": "aa1", "token": "TOKENTOKENTOKENTOKEN12", "ips": ["10.0.0.0/8", "bad"]}])
        finally: sp.run = orig; L2.os.path.isdir = real_isdir
        t = open(L2.ACL).read()
        self.assertIn("geo $remote_addr $e4tv_ip_aa1 { default 0; 10.0.0.0/8 1; }", t)
        self.assertIn('"~^/pl/TOKENTOKENTOKENTOKEN12\\.m3u8?$" $e4tv_ip_aa1;', t)

    def test_shell_syntax(self):
        for f in ("install.sh", "make-bundle.sh", "get.sh", "src/e4tv"):
            self.assertEqual(subprocess.run(["bash", "-n", os.path.join(ROOT, f)]).returncode, 0, f)

    def test_page_js_syntax(self):
        node = shutil.which("node")
        if not node: self.skipTest("нет node")
        s = open(os.path.join(SRC, "index.html"), encoding="utf-8").read()
        js = "\n".join(re.findall(r"<script>(.*?)</script>", s, re.S))
        f = f"{TMP}/page.js"; open(f, "w").write(js)
        r = subprocess.run([node, "--check", f], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[:500])

    def test_get_sh_matches_installer(self):
        g = open(os.path.join(ROOT, "get.sh")).read()
        ver = re.search(r'local VER="([^"]+)" SHA="([0-9a-f]{64})"', g)
        inst = os.path.join(ROOT, "evgen4ik-tv-install.sh")
        if f'E4_VER="{ver.group(1)}"' not in open(inst, encoding="utf-8").read():
            self.skipTest("идёт выпуск: get.sh ещё указывает на прошлую версию")
        self.assertEqual(hashlib.sha256(open(inst, "rb").read()).hexdigest(), ver.group(2), "get.sh: SHA-256 установщика той же версии")

    def test_installer_payload_is_current(self):
        data = open(os.path.join(ROOT, "evgen4ik-tv-install.sh"), "rb").read()
        head, _, b64 = data.partition(b"\n__PAYLOAD__\n")
        self.assertEqual(head.decode("utf-8") + "\n", open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read(), "установщик собран из текущего install.sh")
        tf = tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz")
        for m in tf.getmembers():
            if m.isfile():
                self.assertEqual(tf.extractfile(m).read(), open(os.path.join(ROOT, m.name), "rb").read(), f"в установщике старый {m.name} — запустите make-bundle.sh")


if __name__ == "__main__":
    try:
        res = unittest.main(verbosity=2, exit=False).result
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(0 if res.wasSuccessful() else 1)
