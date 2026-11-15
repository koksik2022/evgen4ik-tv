"""Настройки Evgen4ik TV: читаются из /etc/evgen4ik-tv/config.json (путь можно сменить переменной E4TV_CONF).
Файл создаёт установщик. Здесь только значения по умолчанию и производные пути."""
import json, os

PATH = os.environ.get("E4TV_CONF", "/etc/evgen4ik-tv/config.json")


def load():
    with open(PATH, encoding="utf-8") as f:
        c = json.load(f)
    c.setdefault("mode", "direct")                 # direct — без HLS Proxy, hls — через HLS Proxy
    c.setdefault("data", "/var/lib/evgen4ik-tv")
    c.setdefault("pl_dir", "/var/lib/evgen4ik-tv-pl")
    c.setdefault("hls_dir", "/opt/hls-proxy")
    c.setdefault("listen", "127.0.0.1")
    c.setdefault("port", 8095)
    c.setdefault("hosts", [])
    c.setdefault("auth_file", "/etc/evgen4ik-tv/auth.json")
    c.setdefault("access", "login")                # login — логин и пароль, tunnel — только через SSH-туннель
    c.setdefault("epg_url", "http://epg.it999.ru/edem.xml.gz")
    c.setdefault("public_base", "")
    c.setdefault("main_token", "")
    c.setdefault("editor_group", "e4tv")
    d = c["data"]
    c["editor_dir"] = d + "/editor"
    c["build_dir"] = d + "/build"
    c["free_dir"] = d + "/free"
    c["combined"] = (c["hls_dir"] + "/combined.m3u") if c["mode"] == "hls" else (d + "/combined.m3u")
    c["uploads"] = c["editor_dir"] + "/uploads"
    return c


C = load()
