"""Diagnose: welche data.lua-Seite nutzt die Box für 'Internet > Zugangsdaten'?

Aufruf (im Programmordner):  py -3 tools\\probe_provider_page.py KENNWORT [HOST]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fritzconf.httpclient import HttpClient  # noqa: E402
from fritzconf.session import FritzSession, parse_json_body  # noqa: E402

CANDIDATES = ["cred", "fiberSet", "ataSet", "dslSet", "internetSet", "inetSet", "accessSet", "netCnf",
              "wanSet", "providerSet", "mediumSet", "dslFiberSet", "fiberCred", "inetCred", "zugang"]


def short(obj, n=400):
    s = json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj
    return s[:n] + ("..." if len(s) > n else "")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    pw = sys.argv[1]
    host = sys.argv[2] if len(sys.argv) > 2 else "192.168.178.1"
    http = HttpClient(host, timeout=20)
    sess = FritzSession(http, pw, log=lambda lvl, msg: print(f"[{lvl}] {msg}"))
    sess.login()
    print(f"Angemeldet an {host}, SID {sess.sid[:4]}...")

    print("\n=== Kandidaten (data.lua page=...) ===")
    for page in CANDIDATES:
        r = sess.data_lua(page)
        obj = parse_json_body(r.text) if r.text.lstrip().startswith("{") else None
        pid = obj.get("pid") if isinstance(obj, dict) else None
        keys = sorted((obj.get("data") or {}).keys())[:25] if isinstance(obj, dict) and isinstance(obj.get("data"), dict) else []
        print(f"{page:14s} HTTP {r.status}  pid={pid!r}  data-keys={keys}")

    print("\n=== Menü (Seiten-IDs aus overview / Startseite) ===")
    found = set()
    for extra in ([], [("xhrId", "all")], [("xhrId", "first"), ("noMenuRef", "1")]):
        r = sess.data_lua("overview", extra)
        found |= set(re.findall(r'"(?:pid|page|lua)"\s*:\s*"([^"]+)"', r.text))
        if '"menu"' in r.text:
            obj = parse_json_body(r.text)
            if isinstance(obj, dict) and obj.get("menu"):
                print("menu-Schlüssel gefunden:", short(obj.get("menu"), 3000))
    r = http.get("/", params={"sid": sess.sid})
    found |= set(re.findall(r'["\'](?:pid|page)["\']\s*[:=]\s*["\']([A-Za-z0-9_]+)', r.text))
    print("Seiten-IDs:", ", ".join(sorted(found)) or "(keine)")

    print("\n=== Box-Werte ===")
    for name, var in [("opmode", "connections:settings/opmode"),
                      ("provider", "connections:settings/connection0/providername"),
                      ("medium", "connections:settings/connection0/medium"),
                      ("annex", "sar:settings/Annex"),
                      ("fiber", "fiber:settings/enabled"),
                      ("wanmode", "box:settings/wan_mode")]:
        try:
            print(f"{name:10s}", short(sess.query(v=var).get("v")))
        except Exception as e:  # noqa: BLE001
            print(f"{name:10s} FEHLER {e}")
    try:
        d = sess.query(plist="providerlist:settings/providerlist/list(Id,providername,displayId,medium)")
        ids = [f"{e.get('Id')}[{e.get('medium')}]" for e in d.get("plist") or [] if isinstance(e, dict)]
        print("Anbieter:", ", ".join(i for i in ids if "raiff" in i.lower() or "konverto" in i.lower()) or short(ids, 800))
    except Exception as e:  # noqa: BLE001
        print("Anbieterliste FEHLER", e)
    sess.logout()
    return 0


if __name__ == "__main__":
    sys.exit(main())
