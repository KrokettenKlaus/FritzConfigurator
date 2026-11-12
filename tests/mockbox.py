"""Simulierte FRITZ!Box für Tests (Verhalten nachgebildet nach FRITZ!OS 8.50-Quellen).

Startet einen HTTP-Server (Port 80, alle Loopback-Adressen) und einen TR-064-Server.
Die Box antwortet nur auf Verbindungen an ihre aktuelle LAN-IP (127.0.0.x), simuliert
Neustarts (Verbindungen werden getrennt) und IP-Wechsel.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import tarfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

PW = "geheim123"
USER = "fritz4711"

CH_RAIFFEISEN = ("connections:opmode=opmode_pppoe;connections:connection0/username=nome@xdsl.rol;"
                 "connections:connection0/password=pass22xdsl;connections:connection0/stay_always_online=1;"
                 "connections:connection0/disconnect_prevention_enable=1;connections:connection0/disconnect_prevention_hour=3")
CH_RAIFFEISEN_LAN1 = ("connections:opmode=opmode_eth_pppoe;connections:connection0/username=nome@xdsl.rol;"
                      "connections:connection0/password=pass22xdsl;connections:connection0/stay_always_online=1;"
                      "connections:connection0/disconnect_prevention_enable=1;connections:connection0/disconnect_prevention_hour=4")
CH_RAIFFEISEN_FIBER = ("connections:opmode=opmode_fiber_pppoe;connections:connection0/username=nome@xdsl.rol;"
                       "connections:connection0/password=pass22xdsl;connections:connection0/stay_always_online=1;"
                       "connections:connection0/disconnect_prevention_enable=1;connections:connection0/disconnect_prevention_hour=4")
CHARACTERISTICS = {"raiffeisen": CH_RAIFFEISEN, "raiffeisen_lan1": CH_RAIFFEISEN_LAN1,
                   "raiffeisen_fiber": CH_RAIFFEISEN_FIBER}


def make_image(path: str, hw: str, version: str, product: str = "FRITZ!Box 7530 AX") -> str:
    content = (f'Product=Fritz_Box_HW{hw} ("{product}")\nType=Release\nVersion={version}\nBuild=137523\n'
               f'OEMs=avm 1und1 avme\nCountries=049 039\nFIRMWAREVERSION={hw}.{version},,137523,\n')
    with tarfile.open(path, "w") as tf:
        for name, data in (("./var/content", content.encode()), ("./var/tmp/fit-image", os.urandom(4096))):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return path


class MockBox:
    def __init__(self, ip: str = "127.0.0.1", dsl: bool = True, hw: str = "256", version: str = "256.07.57",
                 require_2fa: bool = False, require_confirm: bool = False, tr064_port: int = 49000,
                 reboot_seconds: float = 3.0, web_locale_broken: bool = False, reject_web_upload: bool = False, name: str = "FRITZ!Box 7530 AX",
                 fiber: bool = False, rest_api: bool = True, legacy_fiber_ids: bool = False):
        self.lock = threading.RLock()
        self.ip = ip
        self.dsl = dsl and not fiber
        self.fiber = fiber
        self.kind = "fiber" if fiber else ("dsl" if dsl else "ata")
        self.hw = hw
        self.name = name
        self.version = version
        self.require_2fa = require_2fa
        self.require_confirm = require_confirm
        self.web_locale_broken = web_locale_broken
        self.reject_web_upload = reject_web_upload
        self.reboot_seconds = reboot_seconds
        self.rest_api = rest_api
        self.legacy_fiber_ids = legacy_fiber_ids  # wie 5530 Fiber mit FRITZ!OS 7.81
        self.rest_calls: List[tuple] = []
        self.down_until = 0.0
        self.sids: Dict[str, float] = {}
        self.challenge = ""
        self.tfa_polls = 0
        self.tfa_pending = False
        self.posts: List[dict] = []
        self.reboots = 0
        self.vars: Dict[str, str] = {
            "language:settings/current_language": "en",
            "language:settings/IsLanguageSet": "0",
            "country:settings/current_country": "099",
            "country:settings/IsCountrySet": "0",
            "sar:settings/Annex": "B",
            "box:status/rebooting": "0",
            "connections:settings/connection0/providername": "",
            "connections:settings/connection0/username": "",
            "connections:settings/connection0/password": "",
            "connections:settings/opmode": "opmode_standard",
            "connections:settings/connection0/disconnect_prevention_enable": "0",
            "connections:settings/connection0/disconnect_prevention_hour": "",
            "tr069:settings/url": "",
            "interfaces:settings/lan0/ipaddr": ip,
            "interfaces:settings/lan0/netmask": "255.255.255.0",
            "interfaces:settings/lan0/dhcpserver": "1",
            "interfaces:settings/lan0/dhcpstart": ip.rsplit(".", 1)[0] + ".20",
            "interfaces:settings/lan0/dhcpend": ip.rsplit(".", 1)[0] + ".200",
            "box:settings/dhcpserver_lease_time": "864000",
            "dnscfg:settings/ipv4_dhcpserver_lan_dns": ip,
            "box:settings/dhcpserver_guest_lease_time": "86400",
            "box:settings/static_net_enabled": "0",
            "box:settings/static_net_prefix": "",
            "box:settings/static_net_netmask": "",
            "box:settings/infoled_reason": "0",
        }
        self.tr064_port = tr064_port
        self.http = ThreadingHTTPServer(("0.0.0.0", 80), self._handler())
        self.http.daemon_threads = True
        self.tr = ThreadingHTTPServer(("0.0.0.0", tr064_port), self._tr_handler())
        self.tr.daemon_threads = True
        for srv in (self.http, self.tr):
            threading.Thread(target=srv.serve_forever, daemon=True).start()

    def close(self):
        for srv in (self.http, self.tr):
            srv.shutdown()
            srv.server_close()

    # --------------------------------------------------------------- Helfer
    @property
    def down(self) -> bool:
        return time.time() < self.down_until

    def reboot(self, delay: float = 0.5, duration: Optional[float] = None, then=None):
        def go():
            time.sleep(delay)
            with self.lock:
                self.vars["box:status/rebooting"] = "1"
                self.down_until = time.time() + (duration or self.reboot_seconds)
                self.sids.clear()
                self.reboots += 1
                if then:
                    then()
            time.sleep((duration or self.reboot_seconds) + 0.05)
            with self.lock:
                self.vars["box:status/rebooting"] = "0"
        with self.lock:
            self.vars["box:status/rebooting"] = "1"
        threading.Thread(target=go, daemon=True).start()

    def country_list(self):
        return [{"id": "049", "name": "Deutschland", "_node": "country0"},
                {"id": "039", "name": "Italien", "_node": "country1"},
                {"id": "043", "name": "Österreich", "_node": "country2"}]

    def provider_list(self):
        if self.vars["country:settings/current_country"] == "039" and self.legacy_fiber_ids:
            # Anbieterliste einer 5530 Fiber mit FRITZ!OS 7.81 (Glasfaser-Seite), KONVERTO heißt hier 'raiffeisen'
            ids = ["bruneck_fiber", "conv_fiber", "isiline_fiber", "ofppp_fiber", "other_fiber", "raiffeisen",
                   "telweb_fiber", "tim_fiber"]
            return [{"Id": i, "providername": "Raiffeisen OnLine" if i == "raiffeisen" else i.split("_")[0].title(),
                     "displayId": "", "medium": "FIBER", "_node": f"provider{n}"} for n, i in enumerate(ids)]
        if self.vars["country:settings/current_country"] == "039":
            return [{"Id": "tim_vdsl", "providername": "TIM", "displayId": "", "medium": "VDSL", "_node": "provider0"},
                    {"Id": "raiffeisen", "providername": "KONVERTO DSL", "displayId": "", "medium": "", "_node": "provider1"},
                    {"Id": "raiffeisen_lan1", "providername": "KONVERTO WAN", "displayId": "", "medium": "ATA", "_node": "provider2"},
                    {"Id": "raiffeisen_fiber", "providername": "KONVERTO", "displayId": "", "medium": "FIBER", "_node": "provider4"},
                    {"Id": "other", "providername": "Anderer Anbieter", "displayId": "", "medium": "", "_node": "provider3"}]
        return [{"Id": "tonline", "providername": "Telekom", "displayId": "", "medium": "VDSL", "_node": "provider0"},
                {"Id": "other", "providername": "Anderer Anbieter", "displayId": "", "medium": "", "_node": "provider1"}]

    def query_var(self, name: str):
        if name == "country:settings/country/list(id,name)":
            return self.country_list()
        if name.startswith("providerlist:settings/providerlist/list("):
            return self.provider_list()
        m = re.match(r"providerlist:settings/(provider\d+)/characteristic", name)
        if m:
            for p in self.provider_list():
                if p["_node"] == m.group(1):
                    if self.legacy_fiber_ids and p["Id"] == "raiffeisen":
                        return "connections:opmode=opmode_fiber_pppoe;connections:connection0/stay_always_online=1"
                    return CHARACTERISTICS.get(p["Id"], "")
            return ""
        return self.vars.get(name, "")

    def boxinfo_xml(self) -> str:
        return ('<j:BoxInfo xmlns:j="http://jason.avm.de/updatecheck/">'
                f"<j:Name>{self.name}</j:Name><j:HW>{self.hw}</j:HW><j:Version>{self.version}</j:Version>"
                "<j:Revision>1</j:Revision><j:Serial>3C3712AABBCC</j:Serial><j:OEM>avme</j:OEM>"
                f"<j:Lang>{self.vars['language:settings/current_language']}</j:Lang><j:Annex>B</j:Annex><j:Lab/>"
                f"<j:Country>{self.vars['country:settings/current_country']}</j:Country></j:BoxInfo>")

    # --------------------------------------------------------------- HTTP
    def _handler(box):  # noqa: N805
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _reject(self) -> bool:
                local = self.connection.getsockname()[0]
                if box.down or local != box.ip:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(2)
                    except OSError:
                        pass
                    return True
                return False

            def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
                data = body.encode("utf-8") if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, obj):
                self._send(200, json.dumps(obj), "application/json; charset=utf-8")

            def _params(self) -> Dict[str, str]:
                q = urllib.parse.urlparse(self.path).query
                params = dict(urllib.parse.parse_qsl(q, keep_blank_values=True))
                if self.command == "POST" and "multipart" not in self.headers.get("Content-Type", ""):
                    n = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(n).decode()
                    self._pairs = urllib.parse.parse_qsl(body, keep_blank_values=True)
                    params.update(dict(self._pairs))
                return params

            def do_GET(self):  # noqa: N802
                self._dispatch()

            def do_POST(self):  # noqa: N802
                self._dispatch()

            def do_PUT(self):  # noqa: N802
                self._dispatch()

            def _rest(self, path):
                # wie FRITZ!OS 8.50 /api/v0/generic/box (js3 rest-helper: AUTHORIZATION: AVM-SID <sid>)
                auth = self.headers.get("Authorization", "")
                if not box.rest_api or path != "/api/v0/generic/box":
                    return self._send(404, json.dumps({"errors": [{"code": "NOT-FOUND"}]}), "application/json")
                if not auth.startswith("AVM-SID ") or auth[8:] not in box.sids:
                    return self._send(403, json.dumps({"errors": [{"code": "INVALID-SID"}]}), "application/json")
                if self.command == "PUT":
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n).decode() or "{}")
                    box.rest_calls.append((self.command, path, body))
                    if "infoled_reason" in body:
                        v = int(body["infoled_reason"])
                        if not 0 <= v <= 17:
                            return self._send(400, json.dumps({"errors": [{"code": "VALIDATION"}]}), "application/json")
                        box.vars["box:settings/infoled_reason"] = str(v)
                return self._json({"infoled_reason": box.vars["box:settings/infoled_reason"], "led_display": "0"})

            def _infoled(self, p):
                # ältere Oberfläche: system/infoled.lua (Feld led_change_state_event, newval.validate_and_save)
                if "apply" not in p:
                    return self._json({"load": "ok", "selected_led_value": int(box.vars["box:settings/infoled_reason"])})
                num = p.get("led_change_state_event", "")
                if not num.isdigit() or not 0 <= int(num) < 17:
                    return self._json({"apply": "valerror", "valerror": "wrong_led_state"})
                box.vars["box:settings/infoled_reason"] = num
                return self._json({"apply": "ok"})

            def _dispatch(self):
                if self._reject():
                    return
                path = urllib.parse.urlparse(self.path).path
                with box.lock:
                    if path == "/jason_boxinfo.xml":
                        return self._send(200, box.boxinfo_xml(), "text/xml")
                    if path == "/login_sid.lua":
                        return self._login(self._params())
                    if path == "/cgi-bin/firmwarecfg":
                        return self._firmware()
                    if path.startswith("/api/"):
                        return self._rest(path)
                    p = self._params()
                    if p.get("sid") not in box.sids:
                        return self._send(403, "Forbidden")
                    if path == "/query.lua":
                        return self._json({k: box.query_var(v) for k, v in p.items() if k != "sid"})
                    if path == "/data.lua":
                        return self._data(p)
                    if path == "/twofactor.lua":
                        return self._twofactor(p)
                    if path == "/system/infoled.lua":
                        box.posts.append(dict(p))
                        return self._infoled(p)
                    self._send(404, "not found")

            # ---- Login (v2 PBKDF2)
            def _login(self, p):
                if p.get("sid") in box.sids:
                    return self._session(p["sid"])
                if "response" in p:
                    salt2, _, h = p["response"].partition("$")
                    _, it1, s1, it2, s2 = box.challenge.split("$")
                    h1 = hashlib.pbkdf2_hmac("sha256", PW.encode(), bytes.fromhex(s1), int(it1))
                    h2 = hashlib.pbkdf2_hmac("sha256", h1, bytes.fromhex(s2), int(it2))
                    if p.get("username") == USER and salt2 == s2 and h == h2.hex():
                        sid = secrets.token_hex(8)
                        box.sids[sid] = time.time()
                        return self._session(sid)
                    return self._session("0000000000000000")
                if p.get("logout"):
                    box.sids.pop(p.get("sid", ""), None)
                return self._session("0000000000000000")

            def _session(self, sid):
                box.challenge = f"2$100${secrets.token_hex(16)}$50${secrets.token_hex(16)}"
                xml = (f"<?xml version=\"1.0\" encoding=\"utf-8\"?><SessionInfo><SID>{sid}</SID>"
                       f"<Challenge>{box.challenge}</Challenge><BlockTime>0</BlockTime><Rights></Rights>"
                       f"<Users><User last=\"1\">{USER}</User></Users></SessionInfo>")
                self._send(200, xml, "text/xml")

            # ---- data.lua
            def _data(self, p):
                page = p.get("page")
                box.posts.append(dict(p))
                if page == "basic_first":
                    return self._basic_first(p)
                if page in ("cred", "fiberSet", "ataSet"):
                    if {"cred": "dsl", "fiberSet": "fiber", "ataSet": "ata"}[page] != box.kind:
                        return self._json({"pid": "overview", "data": {}})
                    if "apply" not in p:
                        return self._send(200, "<html><form name='mainform'>internet settings</form></html>")
                    return self._cred(p)
                if page == "boxnet":
                    if "apply" not in p:
                        return self._send(200, "<html>boxnet</html>")
                    return self._boxnet(p)
                return self._json({"pid": "overview", "data": {}})

            def _basic_first(self, p):
                if box.web_locale_broken:
                    return self._send(303, "", headers={"Location": "/data.lua?page=firstAssist"})
                lang, country = p.get("language"), p.get("country")
                ids = [c["id"] for c in box.country_list()]
                assert country in ids, country
                changed = country != box.vars["country:settings/current_country"]
                box.vars["language:settings/current_language"] = lang
                box.vars["language:settings/IsLanguageSet"] = "1"
                box.vars["country:settings/current_country"] = country
                box.vars["country:settings/IsCountrySet"] = "1"
                if country == "039":
                    box.vars["sar:settings/Annex"] = "A"
                if changed:
                    box.reboot()
                    return self._send(303, "", headers={"Location": "/reboot.lua?extern_reboot=1"})
                return self._send(303, "", headers={"Location": "/data.lua?page=firstAssist"})

            def _cred(self, p):
                pid = p.get("provider")
                ids = {e["Id"]: e for e in box.provider_list()}
                if pid not in ids:
                    return self._json({"data": {"apply": "valerror", "valerror": {"provider": "unknown"}}})
                if box.require_confirm and "confirmed" not in p:
                    return self._json({"data": {"apply": "confirm", "confirm": ["Wirklich?"]}})
                if box.require_2fa and "twofactor" not in p:
                    box.tfa_pending, box.tfa_polls = True, 0
                    return self._json({"data": {"apply": "twofactor", "twofactor": "button,dtmf;5918"}})
                if box.require_2fa and box.tfa_pending:
                    return self._json({"data": {"apply": "saveerror", "saveerror": {"err": 90}}})
                vars_ = {}
                for k, v in p.items():
                    if ":" not in k:
                        vars_[k] = v
                    elif k.endswith(":" + pid):
                        vars_[k[:-len(pid) - 1]] = v
                if p.get("page") == "fiberSet" and any(k.startswith(("medium:", "useprevention:")) for k in p):
                    return self._json({"data": {"apply": "valerror", "valerror": "unknown field on fiberSet"}})
                if p.get("page") == "fiberSet" and f"user:{pid}" not in p:
                    # wie FRITZ!OS 8.19 (5690): Profil-Vorgabe wird nicht automatisch übernommen
                    return self._json({"data": {"apply": "valerror", "valerror": {
                        "ok": False, "tomark": [f"user:{pid}"], "result": "notfound",
                        "alert": f"Es ist ein Fehler aufgetreten. (notfound, user:{pid})"}}})
                ch = dict(x.split("=", 1) for x in box.query_var(
                    "providerlist:settings/" + ids[pid]["_node"] + "/characteristic").split(";") if "=" in x)
                user = vars_.get("user")
                if user is None:
                    if not ch.get("connections:connection0/username"):
                        return self._json({"data": {"apply": "valerror", "valerror": "username empty"}})
                    user, pwd = ch["connections:connection0/username"], ch["connections:connection0/password"]
                else:
                    if not user:
                        return self._json({"data": {"apply": "valerror", "valerror": "username empty"}})
                    pwd = vars_.get("pwd", "")
                if vars_.get("useprevention") and not re.fullmatch(r"\d{1,2}", vars_.get("prevention", "3")):
                    return self._json({"data": {"apply": "valerror", "valerror": "prevention"}})
                box.vars["connections:settings/connection0/providername"] = pid
                box.vars["connections:settings/connection0/username"] = user
                box.vars["connections:settings/connection0/password"] = pwd
                box.vars["connections:settings/opmode"] = ch.get("connections:opmode", "opmode_pppoe")
                box.vars["connections:settings/connection0/disconnect_prevention_enable"] = "1" if vars_.get("useprevention") else "0"
                box.vars["connections:settings/connection0/disconnect_prevention_hour"] = vars_.get("prevention", "")
                box.vars["tr069:settings/url"] = "http://acs.raiffeisen.net" if pid.startswith("raiffeisen") else ""
                box.last_cred = vars_
                return self._json({"data": {"apply": "ok", "redirect": {"params": {"oldpage": "/internet/isp_change.lua"}}}})

            def _boxnet(self, p):
                def ipf(prefix):
                    return ".".join(p.get(f"{prefix}{i}", "") for i in range(4))
                try:
                    ip = ipaddress.IPv4Address(ipf("Ip_all"))
                    mask = ipf("Netmask_all")
                    net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                    s, e = ipaddress.IPv4Address(ipf("Start_all")), ipaddress.IPv4Address(ipf("End_all"))
                    ipaddress.IPv4Address(ipf("Dns_all"))
                    assert s in net and e in net and int(s) <= int(e) and not (int(s) <= int(ip) <= int(e))
                    assert 1 <= int(p["lease_time"]) <= 3650 and 1 <= int(p["guest_lease_time"]) <= 999
                    assert p.get("Dhcp_all") == "on"
                except Exception as ex:  # noqa: BLE001
                    return self._json({"data": {"apply": "valerror", "valerror": str(ex)}})
                old = box.vars["interfaces:settings/lan0/ipaddr"]
                box.vars.update({
                    "interfaces:settings/lan0/ipaddr": str(ip), "interfaces:settings/lan0/netmask": mask,
                    "interfaces:settings/lan0/dhcpstart": str(s), "interfaces:settings/lan0/dhcpend": str(e),
                    "dnscfg:settings/ipv4_dhcpserver_lan_dns": ipf("Dns_all"),
                    "box:settings/dhcpserver_lease_time": str(int(p["lease_time"]) * 86400),
                })
                box.last_boxnet = dict(p)
                self._json({"data": {"apply": "ok"}})
                if str(ip) != old:
                    def switch():
                        time.sleep(0.5)
                        with box.lock:
                            box.ip = str(ip)
                    threading.Thread(target=switch, daemon=True).start()

            def _twofactor(self, p):
                if "tfa_active" in p:
                    box.tfa_polls += 1
                    if box.tfa_polls >= 3:
                        box.tfa_pending = False
                        return self._json({"done": True, "active": True})
                    return self._json({"done": False})
                if "tfa_cancel" in p:
                    box.tfa_pending = False
                return self._json({})

            # ---- firmwarecfg
            def _firmware(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                m = re.search(rb'name="sid"\r\n\r\n([0-9a-f]+)\r\n', body)
                if not m or m.group(1).decode() not in box.sids:
                    return self._json({"reboot": 0, "errors": [{"code": 3001, "message": "permission"}]})
                fm = re.search(rb'name="UploadFile"; filename="[^"]*"\r\nContent-Type: [^\r]*\r\n\r\n', body)
                assert fm, "UploadFile fehlt"
                assert body.index(b'name="sid"') < fm.start(), "sid muss vor UploadFile stehen"
                data = body[fm.end():body.rindex(b"\r\n--")]
                with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                    content = tf.extractfile("./var/content").read().decode()
                hw = re.search(r"HW(\d+)", content).group(1)
                if box.reject_web_upload:
                    return self._json({"reboot": 0, "errors": [{"code": 20006, "message": "other"}]})
                if hw != box.hw:
                    return self._json({"reboot": 0, "errors": [{"code": 20002, "message": "wrong hardware"}]})
                m = re.search(r"FIRMWAREVERSION=([^,\n]+)", content)
                newver = m.group(1) if m else f"{hw}." + re.search(r"Version=([\d.]+)", content).group(1)
                self._json({"reboot": 1})

                def apply_fw():
                    box.version = newver
                box.reboot(delay=1.0, then=apply_fw)

        return H

    # --------------------------------------------------------------- TR-064
    def _tr_handler(box):  # noqa: N805
        realm = "F!Box SOAP-Auth"

        class T(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, body, headers=None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if box.down:
                    self.connection.close()
                    return
                if self.path == "/tr64desc.xml":
                    svc = ("<service><serviceType>urn:dslforum-org:service:UserInterface:1</serviceType>"
                           "<controlURL>/upnp/control/userif</controlURL></service>")
                    return self._send(200, f"<root><device>{svc}</device></root>")
                self._send(404, "")

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                if box.down:
                    self.connection.close()
                    return
                auth = self.headers.get("Authorization", "")
                if not self._check_digest(auth):
                    nonce = secrets.token_hex(8)
                    return self._send(401, "", {"WWW-Authenticate": f'Digest realm="{realm}", nonce="{nonce}", algorithm=MD5, qop="auth"'})
                action = self.headers.get("SOAPAction", "").strip('"').split("#")[-1]
                args = dict(re.findall(r"<(New[\w-]+)>(.*?)</\1>", body))
                with box.lock:
                    out = self._action(action, args)
                inner = "".join(f"<{k}>{v}</{k}>" for k, v in out.items())
                self._send(200, f'<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                                f'<s:Body><u:{action}Response xmlns:u="x">{inner}</u:{action}Response></s:Body></s:Envelope>')

            def _check_digest(self, auth: str) -> bool:
                if not auth.startswith("Digest "):
                    return False
                f = dict(re.findall(r'(\w+)="?([^",]*)"?', auth[7:]))
                ha1 = hashlib.md5(f"{f.get('username')}:{realm}:{PW}".encode()).hexdigest()
                ha2 = hashlib.md5(f"POST:{f.get('uri')}".encode()).hexdigest()
                exp = hashlib.md5(f"{ha1}:{f.get('nonce')}:{f.get('nc')}:{f.get('cnonce')}:{f.get('qop')}:{ha2}".encode()).hexdigest()
                return f.get("username") == USER and f.get("response") == exp

            def _action(self, action, a):
                v = box.vars
                if action == "X_AVM-DE_GetInternationalConfig":
                    return {"NewX_AVM-DE_Language": v["language:settings/current_language"],
                            "NewX_AVM-DE_Country": v["country:settings/current_country"],
                            "NewX_AVM-DE_Annex": v["sar:settings/Annex"]}
                if action == "X_AVM-DE_SetInternationalConfig":
                    changed = a["NewX_AVM-DE_Country"] != v["country:settings/current_country"]
                    v["language:settings/current_language"] = a["NewX_AVM-DE_Language"]
                    v["country:settings/current_country"] = a["NewX_AVM-DE_Country"]
                    v["country:settings/IsCountrySet"] = v["language:settings/IsLanguageSet"] = "1"
                    if changed:
                        box.reboot()
                    return {}
                if action == "X_AVM-DE_DoManualUpdate":
                    url = a["NewX_AVM-DE_DownloadURL"]

                    def fetch():
                        data = urllib.request.urlopen(url, timeout=10).read()
                        with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                            content = tf.extractfile("./var/content").read().decode()
                        newver = re.search(r"FIRMWAREVERSION=([^,\n]+)", content).group(1)

                        def apply_fw():
                            box.version = newver
                        box.reboot(delay=0.5, then=apply_fw)
                    threading.Thread(target=fetch, daemon=True).start()
                    return {}
                return {}

        return T
