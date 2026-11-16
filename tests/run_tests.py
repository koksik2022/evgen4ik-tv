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
    '#EXTINF:-1 group-title="Детские",Мульт\nhttp://p/3\n#EXTINF:-1 group-title="Новости",Новости 24\nhttp://p/4\n')
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


class Build(unittest.TestCase):
    def test_client_files(self):
        out = build(rules())
        a = out["A"]
        self.assertNotIn("Кино 1", a, "выключенный канал")
        self.assertIn("http://my/m.m3u8", a, "своя ссылка")
        self.assertIn("Взрослый", a, "добавлен из основного")
        self.assertIn("Свой канал", a, "свой канал")
        self.assertTrue(a.split("\n")[1].endswith(",Новости 24"), "свой порядок: первым")
        self.assertIn("http://p/1", out["B"], "правки A не задевают B")
        self.assertNotIn("Новости 24", out["B"], "личный плейлист A не достаётся B")
        self.assertIsNone(out["Выкл"]); self.assertIsNone(out["Старый"])

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
        self.assertEqual(hashlib.sha256(open(inst, "rb").read()).hexdigest(), ver.group(2), "get.sh: SHA-256 установщика")
        self.assertIn(f'E4_VER="{ver.group(1)}"', open(inst, encoding="utf-8").read(), "get.sh: версия")

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
