"""Ablaufsteuerung: Box finden -> Sprache/Land -> Internetanbieter -> Heimnetz -> Firmware -> Info-LED.

Alle Schritte bilden exakt die Formulare der FRITZ!Box-Weboberfläche nach (FRITZ!OS 7.x/8.x)
und prüfen das Ergebnis anschließend über query.lua bzw. TR-064 nach.
"""
from __future__ import annotations

import csv
import datetime as _dt
import ipaddress
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import netutil
from .boxinfo import BoxInfo, compare_versions, detect, read_boxinfo
from .config import INFOLED_REASONS, JobConfig, firmware_dir, logs_dir
from .firmware import FirmwareRepo, ImageInfo, SingleFileServer, inspect_image, parse_firmwarecfg_response
from .httpclient import HttpClient, HttpError
from .netutil import Cancelled
from .session import ApplyError, FritzSession, LoginError, TwoFactorCancelled, TwoFactorRequest, parse_json_body
from .tr064 import S_LANHOST, S_UI, Tr064Client, Tr064Error

STEPS: List[Tuple[str, str]] = [
    ("connect", "FRITZ!Box finden & anmelden"),
    ("locale", "Sprache & Land"),
    ("provider", "Internetanbieter KONVERTO"),
    ("network", "Heimnetz: Gateway & DHCP-Pool"),
    ("firmware", "Firmware-Update"),
    ("infoled", "Info-LED"),
]

# Seiten-IDs / direkte Pfade (FRITZ!OS 8.x; data.lua-IDs sind seit 7.x unverändert)
PAGE_LOCALE = ("basic_first", "/assis/basic_first.lua")
PAGE_DSL = ("cred", "/internet/internet_settings.lua")
PAGE_FIBER = ("fiberSet", "/internet/fiber_settings.lua")
PAGE_ATA = ("ataSet", "/internet/ata_settings.lua")
AUTO_PROVIDER = {"dsl": "raiffeisen", "fiber": "raiffeisen_fiber", "ata": "raiffeisen_lan1"}
KIND_LABEL = {"dsl": "DSL", "fiber": "Glasfaser", "ata": "Nicht-DSL"}
PAGE_NET = ("boxnet", "/net/boxnet.lua")
# Info-LED: FRITZ!OS 8.x speichert über die REST-API (js3 system/leds), ältere Oberfläche über infoled.lua
REST_BOX = "generic/box"
PAGE_INFOLED = "/system/infoled.lua"
VAR_INFOLED = "box:settings/infoled_reason"


class StepFailed(Exception):
    pass


class StepSkipped(Exception):
    pass


class StepWarning(Exception):
    """Schritt im Wesentlichen erledigt, aber mit Hinweis."""


@dataclass
class Callbacks:
    log: Callable[[str, str], None] = lambda lvl, msg: print(f"[{lvl}] {msg}", flush=True)
    step: Callable[[str, str, str], None] = lambda sid, state, detail: None
    progress: Callable[[str, Optional[float]], None] = lambda text, frac: None
    twofactor: Optional[Callable[[TwoFactorRequest, Callable[[], Optional[bool]]], bool]] = None


@dataclass
class RunResult:
    ok: bool = False
    states: Dict[str, str] = field(default_factory=dict)
    details: Dict[str, str] = field(default_factory=dict)
    box_before: Optional[BoxInfo] = None
    box_after: Optional[BoxInfo] = None
    host: str = ""
    logfile: str = ""


def parse_characteristic(s: str) -> Dict[str, str]:
    res = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k:
                res[k.strip()] = v.strip()
    return res


def ip_fields(prefix: str, ip: str) -> List[Tuple[str, str]]:
    parts = (ip or "").split(".")
    if len(parts) != 4:
        parts = ["", "", "", ""]
    return [(f"{prefix}{i}", parts[i]) for i in range(4)]


class Runner:
    def __init__(self, cfg: JobConfig, cb: Optional[Callbacks] = None, cancel: Optional[threading.Event] = None,
                 repo: Optional[FirmwareRepo] = None, reboot_timeouts: Tuple[float, float] = (90, 420),
                 fw_timeouts: Tuple[float, float] = (420, 900)):
        self.cfg = cfg
        self.cb = cb or Callbacks()
        self.cancel = cancel or threading.Event()
        self.result = RunResult()
        self.host = ""
        self.http: Optional[HttpClient] = None
        self.sess: Optional[FritzSession] = None
        self.box: Optional[BoxInfo] = None
        self._logbuf: List[str] = []
        self.repo = repo or FirmwareRepo(firmware_dir(), self.log)
        self.reboot_timeouts = reboot_timeouts
        self.fw_timeouts = fw_timeouts
        self.is_dsl_box: Optional[bool] = None

    # --------------------------------------------------------------- Logging
    def log(self, level: str, msg: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        self._logbuf.append(f"{ts} [{level.upper():5}] {msg}")
        self.cb.log(level, msg)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    # ------------------------------------------------------------ Sitzung
    def _connect_session(self, host: str) -> None:
        self.host = host
        self.http = HttpClient(host, timeout=20)
        self.sess = FritzSession(self.http, self.cfg.password, self.cfg.username or None, log=self.log,
                                 twofactor_handler=self.cb.twofactor, lang=self.cfg.language or "de")
        self.sess.login()

    def _relogin(self, host: Optional[str] = None) -> None:
        h = host or self.host
        for attempt in range(6):
            self._check_cancel()
            try:
                self._connect_session(h)
                return
            except (HttpError, LoginError) as e:
                if attempt == 5:
                    raise StepFailed(f"Erneute Anmeldung fehlgeschlagen: {e}")
                netutil.sleep_cancellable(5, self.cancel)

    def _query(self, **vars: str) -> Dict[str, object]:
        assert self.sess
        try:
            return self.sess.query(**vars)
        except HttpError:
            self.sess.ensure()
            return self.sess.query(**vars)

    def _tr064(self) -> Tr064Client:
        assert self.sess
        return Tr064Client(self.host, self.sess.used_username or self.cfg.username, self.cfg.password,
                           port=self.cfg.tr064_port)

    def _reboot_pending(self) -> Optional[bool]:
        """True/False laut box:status/rebooting, None wenn nicht ermittelbar (Box evtl. schon weg)."""
        try:
            assert self.sess
            v = str(self.sess.query(r="box:status/rebooting").get("r", ""))
            return None if v == "" else v != "0"
        except (HttpError, AssertionError):
            return None

    def _wait_reboot(self, why: str, require_down: bool = False, down_timeout: Optional[float] = None) -> bool:
        down_timeout = down_timeout or self.reboot_timeouts[0]
        if not require_down and self._reboot_pending() is False:
            down_timeout = min(down_timeout, 6)
        self.cb.progress(f"Warte auf Neustart ({why}) ...", None)
        rebooted = netutil.wait_for_reboot(self.host, self.cancel, self.log,
                                           down_timeout=down_timeout,
                                           up_timeout=self.reboot_timeouts[1], require_down=require_down)
        self.cb.progress("", None)
        if rebooted:
            self.log("ok", "Box ist wieder erreichbar")
            self._relogin()
        return rebooted

    # ================================================================= Ablauf
    def run(self) -> RunResult:
        start = _dt.datetime.now()
        plan = [("connect", True), ("locale", self.cfg.set_locale), ("provider", self.cfg.set_provider),
                ("network", self.cfg.set_network), ("firmware", self.cfg.update_firmware),
                ("infoled", self.cfg.set_infoled)]
        for sid, enabled in plan:
            self.cb.step(sid, "pending" if enabled else "off", "")
        failed = False
        for sid, enabled in plan:
            if not enabled:
                self.result.states[sid] = "off"
                continue
            if failed:
                self.cb.step(sid, "skip", "übersprungen (vorheriger Fehler)")
                self.result.states[sid] = "skip"
                continue
            self.cb.step(sid, "running", "")
            title = dict(STEPS)[sid]
            self.log("step", f"--- {title} ---")
            try:
                self._check_cancel()
                detail = getattr(self, f"step_{sid}")() or "erledigt"
                state = "ok"
            except StepSkipped as e:
                state, detail = "skip", str(e)
            except StepWarning as e:
                state, detail = "warn", str(e)
            except Cancelled:
                state, detail = "error", "abgebrochen"
                failed = True
                self.log("error", "Vorgang abgebrochen")
            except TwoFactorCancelled as e:
                state, detail, failed = "error", str(e), True
            except (StepFailed, ApplyError, LoginError, HttpError, Tr064Error, TimeoutError, ValueError) as e:
                state, detail, failed = "error", str(e), True
            except Exception as e:  # unerwartet - nie die GUI abstürzen lassen
                import traceback
                self._logbuf.append(traceback.format_exc())
                state, detail, failed = "error", f"Interner Fehler: {e!r}", True
            level = {"ok": "ok", "skip": "info", "warn": "warn", "error": "error"}[state]
            self.log(level, f"{title}: {detail}")
            self.cb.step(sid, state, detail)
            self.result.states[sid] = state
            self.result.details[sid] = detail
        self.result.ok = not failed
        self.result.host = self.host
        if self.sess:
            self.sess.logout()
        dur = int((_dt.datetime.now() - start).total_seconds())
        self.log("ok" if self.result.ok else "error",
                 ("FERTIG - Box erfolgreich vorkonfiguriert" if self.result.ok else "BEENDET MIT FEHLER")
                 + f" (Dauer {dur // 60}:{dur % 60:02d} min)")
        self._write_reports(start)
        return self.result

    # ------------------------------------------------------------- Schritt 1
    def step_connect(self) -> str:
        cands = [self.cfg.host] if self.cfg.host.strip() else list(self.cfg.hosts)
        if self.cfg.set_network and self.cfg.lan_ip and self.cfg.lan_ip not in cands:
            cands.append(self.cfg.lan_ip)  # Box evtl. schon umkonfiguriert
        self.cb.progress("Suche FRITZ!Box ...", None)
        deadline = time.monotonic() + 120
        while True:
            try:
                host, info = detect(cands)
                break
            except HttpError as e:
                if time.monotonic() > deadline:
                    raise StepFailed(str(e))
                self.cb.progress("Suche FRITZ!Box ... (Box noch im Startvorgang?)", None)
                netutil.sleep_cancellable(3, self.cancel)
        self.cb.progress("", None)
        self.box = info
        self.result.box_before = info
        self.log("info", f"Gefunden unter {host}: {info.summary()}")
        if not self.cfg.password:
            raise StepFailed("Kein Kennwort angegeben (FRITZ!Box-Kennwort vom Aufkleber auf der Unterseite)")
        try:
            self._connect_session(host)
        except LoginError as e:
            raise StepFailed(str(e))
        assert self.sess
        self.log("ok", "Anmeldung erfolgreich" + (f" als '{self.sess.used_username}'" if self.sess.used_username else ""))
        return f"{info.name or 'FRITZ!Box'} @ {host}, FRITZ!OS {info.fw_display}"

    # ------------------------------------------------------------- Schritt 2
    def _country_id(self) -> str:
        want = self.cfg.country.lstrip("0")
        try:
            data = self._query(clist="country:settings/country/list(id,name)")
            for entry in data.get("clist") or []:
                cid = str(entry.get("id", ""))
                if cid.lstrip("0") == want:
                    return cid
        except HttpError:
            pass
        return "0" + want if len(want) <= 3 else want

    def _read_locale(self) -> Dict[str, str]:
        d = self._query(lang="language:settings/current_language", country="country:settings/current_country",
                        langset="language:settings/IsLanguageSet", countryset="country:settings/IsCountrySet",
                        annex="sar:settings/Annex")
        return {k: str(v) for k, v in d.items()}

    def step_locale(self) -> str:
        assert self.sess
        cur = self._read_locale()
        target_country = self._country_id()
        lang = self.cfg.language
        self.log("info", f"Aktuell: Sprache '{cur.get('lang')}', Land '{cur.get('country')}' - Ziel: '{lang}', '{target_country}'")
        if (cur.get("lang") == lang and cur.get("country", "").lstrip("0") == target_country.lstrip("0")
                and cur.get("countryset") == "1" and cur.get("langset") != "0"):
            raise StepSkipped(f"bereits eingestellt ({lang}, Land {target_country})")
        if self.cfg.dry_run:
            raise StepSkipped("Testmodus - keine Änderung")
        fields = [("language", lang), ("country", target_country), ("needed", ""), ("forward", "")]
        method = ""
        try:
            r = self.sess.data_lua(PAGE_LOCALE[0], fields, timeout=60, follow_redirects=False)
            if r.status >= 400:
                raise HttpError(f"HTTP {r.status}")
            method = "Weboberfläche (Assistent Grundeinstellungen)"
        except HttpError as e:
            # Box kann während der Antwort bereits neu starten -> Verbindung bricht ab
            self.log("info", f"Antwort beim Speichern: {e} (normal, wenn die Box sofort neu startet)")
            method = "Weboberfläche"
        rebooted = self._wait_reboot("Ländereinstellung", require_down=False, down_timeout=45)
        cur = self._read_locale()
        if not (cur.get("lang") == lang and cur.get("country", "").lstrip("0") == target_country.lstrip("0")):
            self.log("warn", f"Weboberfläche hat nicht übernommen (Sprache '{cur.get('lang')}', Land '{cur.get('country')}') - versuche TR-064")
            tr = self._tr064()
            info = tr.call(S_UI, "X_AVM-DE_GetInternationalConfig")
            annex = info.get("NewX_AVM-DE_Annex", "")
            tr.call(S_UI, "X_AVM-DE_SetInternationalConfig", **{
                "NewX_AVM-DE_Language": lang, "NewX_AVM-DE_Country": target_country, "NewX_AVM-DE_Annex": annex})
            method = "TR-064"
            rebooted = self._wait_reboot("Ländereinstellung", require_down=False, down_timeout=45) or rebooted
            cur = self._read_locale()
            if not (cur.get("lang") == lang and cur.get("country", "").lstrip("0") == target_country.lstrip("0")):
                raise StepFailed(f"Sprache/Land nicht übernommen (ist: '{cur.get('lang')}', '{cur.get('country')}')")
        return f"Sprache {lang}, Land {target_country} gesetzt via {method}" + (" (Box neu gestartet)" if rebooted else "")

    # ------------------------------------------------------------- Schritt 3
    def _provider_list(self) -> List[dict]:
        d = self._query(plist="providerlist:settings/providerlist/list(Id,providername,displayId,medium)")
        lst = d.get("plist") or []
        return [e for e in lst if isinstance(e, dict)]

    def _characteristic(self, entry: dict) -> Dict[str, str]:
        node = entry.get("_node")
        if not node:
            return {}
        d = self._query(ch=f"providerlist:settings/{node}/characteristic")
        return parse_characteristic(str(d.get("ch") or ""))

    def _detect_provider_page(self) -> Tuple[str, str, str]:
        """Liefert (data.lua-Seite, direkter Pfad, Art 'dsl'|'fiber'|'ata')."""
        assert self.sess
        for kind, (page, direct) in (("dsl", PAGE_DSL), ("fiber", PAGE_FIBER), ("ata", PAGE_ATA)):
            if self.sess.page_available(page):
                return page, direct, kind
        raise StepFailed("Seite 'Internet > Zugangsdaten' nicht gefunden (Modell/FRITZ!OS nicht unterstützt?)")

    def step_provider(self) -> str:
        assert self.sess
        page, direct, kind = self._detect_provider_page()
        is_dsl = kind == "dsl"
        is_fiber = kind == "fiber"
        self.is_dsl_box = is_dsl
        pid = self.cfg.provider
        if pid == "auto":
            pid = AUTO_PROVIDER[kind]
        self.log("info", f"{KIND_LABEL[kind]}-Modell erkannt - Anbieterprofil '{pid}' (Seite '{page}')")
        plist = self._provider_list()
        entry = next((e for e in plist if str(e.get("Id")) == pid), None)
        if entry is None and self.cfg.dry_run:
            raise StepSkipped(f"Testmodus - Anbieter '{pid}' erscheint erst nach der Ländereinstellung")
        if entry is None:
            names =", ".join(sorted({str(e.get('Id')) for e in plist})[:60])
            raise StepFailed(f"Anbieter '{pid}' nicht in der Anbieterliste der Box - Land richtig gesetzt? (verfügbar: {names})")
        ch = self._characteristic(entry)
        self.log("info", f"Profil '{entry.get('providername') or pid}': Opmode {ch.get('connections:opmode', '?')}, "
                         f"Standard-Benutzer {ch.get('connections:connection0/username', '?')}")
        medium = str(entry.get("medium") or "").upper()
        fields: List[Tuple[str, str]] = [("provider", pid)]
        if is_dsl and ("ADSL" in medium or "VDSL" in medium or not medium or "UNDEFINED" in medium) and "ATA" not in medium:
            fields.append((f"medium:{pid}", "dsl"))
        if ch.get("connections:connection0/stay_always_online", "1") == "0":
            fields.append((f"connmode:{pid}", "on_demand"))
            if ch.get("connections:connection0/inactivity_timeout"):
                fields.append((f"idle:{pid}", ch["connections:connection0/inactivity_timeout"]))
        else:
            fields.append((f"connmode:{pid}", "always_online"))
        # Glasfaser-Seite kennt weder medium- noch useprevention-Feld
        if not is_fiber and ch.get("connections:connection0/disconnect_prevention_enable", "0") == "1":
            fields.append((f"useprevention:{pid}", "on"))
            hour = ch.get("connections:connection0/disconnect_prevention_hour", "")
            if hour.isdigit():
                fields.append((f"prevention:{pid}", hour))
        if self.cfg.custom_credentials:
            if not self.cfg.ppp_user:
                raise StepFailed("Eigene Zugangsdaten gewählt, aber kein Benutzername angegeben")
            fields += [(f"user:{pid}", self.cfg.ppp_user), (f"pwd:{pid}", self.cfg.ppp_password)]
            self.log("info", f"Verwende eigene Zugangsdaten (Benutzer {self.cfg.ppp_user})")
        elif is_fiber:
            # fiberSet übernimmt die Profil-Vorgabe nicht selbst ("notfound, user:<pid>") -> explizit senden
            user = ch.get("connections:connection0/username", "")
            pwd = ch.get("connections:connection0/password", "")
            if not user:
                raise StepFailed(f"Profil '{pid}' liefert keinen Standard-Benutzer - bitte eigene Zugangsdaten angeben")
            fields.append((f"user:{pid}", user))
            if pwd:
                fields.append((f"pwd:{pid}", pwd))
            self.log("info", f"Zugangsdaten: Vorgabe des Anbieterprofils (Dummy-Daten, Benutzer {user}) - explizit gesendet")
        else:
            self.log("info", "Zugangsdaten: Vorgabe des Anbieterprofils (Dummy-Daten)")
        if self.cfg.dry_run:
            self.log("info", "Testmodus - würde senden: " + ", ".join(f"{k}={v}" for k, v in fields if not k.startswith("pwd")))
            raise StepSkipped("Testmodus - keine Änderung")
        try:
            answer = self.sess.apply(page, fields)
        except ApplyError as e:
            if "nicht verarbeitet" not in str(e):
                raise
            self.log("warn", f"{e} - versuche direkten Seitenaufruf {direct}")
            answer = self.sess.apply(None, fields, direct_path=direct)
        self.log("ok", "Box hat die Internet-Einstellungen gespeichert")
        redirect = answer.get("redirect") or {}
        if (isinstance(redirect, dict) and redirect.get("page") == "rootReboot") or "rootReboot" in str(answer):
            self._wait_reboot("Umstellung Internetzugang", require_down=True)
        else:
            # Manche Umstellungen (DSL <-> LAN1) starten verzögert neu
            self._wait_reboot("ggf. Umstellung", require_down=False, down_timeout=20)
        v = self._query(prov="connections:settings/connection0/providername",
                        user="connections:settings/connection0/username",
                        acs="tr069:settings/url", opmode="connections:settings/opmode")
        prov, user = str(v.get("prov", "")), str(v.get("user", ""))
        self.log("info", f"Prüfung: Anbieter '{prov}', Benutzer '{user}', Betriebsart '{v.get('opmode')}', ACS '{v.get('acs')}'")
        if prov and prov != pid:
            raise StepFailed(f"Box meldet Anbieter '{prov}' statt '{pid}'")
        want_user = self.cfg.ppp_user if self.cfg.custom_credentials else ch.get("connections:connection0/username", "")
        if want_user and user and user != want_user:
            raise StepWarning(f"Anbieter gesetzt, aber Benutzername ist '{user}' (erwartet '{want_user}')")
        return f"{entry.get('providername') or pid} eingerichtet (Benutzer {user or want_user})"

    # ------------------------------------------------------------- Schritt 4
    def _read_lan(self) -> Dict[str, str]:
        d = self._query(ip="interfaces:settings/lan0/ipaddr", mask="interfaces:settings/lan0/netmask",
                        dhcp="interfaces:settings/lan0/dhcpserver", start="interfaces:settings/lan0/dhcpstart",
                        end="interfaces:settings/lan0/dhcpend", lease="box:settings/dhcpserver_lease_time",
                        dns="dnscfg:settings/ipv4_dhcpserver_lan_dns",
                        glease="box:settings/dhcpserver_guest_lease_time",
                        snet="box:settings/static_net_enabled", sprefix="box:settings/static_net_prefix",
                        smask="box:settings/static_net_netmask")
        return {k: str(v) for k, v in d.items()}

    def step_network(self) -> str:
        assert self.sess
        ip, mask, start, end = netutil.validate_lan_settings(self.cfg.lan_ip, self.cfg.lan_mask,
                                                             self.cfg.dhcp_start, self.cfg.dhcp_end)
        cur = self._read_lan()
        self.log("info", f"Aktuell: Box {cur['ip']}/{cur['mask']}, DHCP {'an' if cur['dhcp'] == '1' else 'aus'} "
                         f"{cur['start']} - {cur['end']}")
        if (cur["ip"], cur["mask"], cur["start"], cur["end"], cur["dhcp"]) == (ip, mask, start, end, "1"):
            raise StepSkipped(f"bereits eingestellt ({ip}, DHCP {start} - {end})")
        if self.cfg.dry_run:
            raise StepSkipped(f"Testmodus - würde setzen: {ip}/{mask}, DHCP {start} - {end}")
        old_ip = cur["ip"]
        dns = cur["dns"]
        if not dns or dns in ("0.0.0.0", old_ip):
            dns = ip
        else:
            try:
                if ipaddress.IPv4Address(dns) not in ipaddress.IPv4Network(f"{ip}/{mask}", strict=False):
                    dns = ip
            except ValueError:
                dns = ip
        lease_days = max(1, min(3650, round(int(cur["lease"] or 0) / 86400) or 10))
        guest_hours = max(1, min(999, round(int(cur["glease"] or 0) / 3600) or 24))
        fields: List[Tuple[str, str]] = []
        fields += ip_fields("Ip_all", ip)
        fields += ip_fields("Netmask_all", mask)
        fields.append(("Dhcp_all", "on"))
        fields += ip_fields("Start_all", start)
        fields += ip_fields("End_all", end)
        fields.append(("lease_time", str(lease_days)))
        fields += ip_fields("Dns_all", dns)
        if cur.get("snet") == "1":
            fields += ip_fields("Ip_static_net", cur.get("sprefix", ""))
            fields += ip_fields("Netmask_static_net", cur.get("smask", ""))
        else:
            fields += [(f"Ip_static_net{i}", "") for i in range(4)]
        fields.append(("guest_lease_time", str(guest_hours)))
        ip_changes = ip != old_ip
        try:
            self.sess.apply(PAGE_NET[0], fields)
        except ApplyError as e:
            if "nicht verarbeitet" not in str(e):
                raise
            self.sess.apply(None, fields, direct_path=PAGE_NET[1])
        except HttpError as e:
            if not ip_changes:
                raise
            self.log("info", f"Verbindung während IP-Wechsel unterbrochen ({e}) - erwartet")
        self.log("ok", f"Gespeichert: Box {ip}/{mask}, DHCP {start} - {end}")
        if ip_changes:
            self._follow_ip_change(old_ip, ip, mask)
        v = self._read_lan()
        if (v["ip"], v["mask"], v["start"], v["end"]) != (ip, mask, start, end):
            raise StepFailed(f"Box meldet {v['ip']}/{v['mask']} DHCP {v['start']} - {v['end']} - nicht wie gewünscht")
        return f"Box-IP {ip}/{mask}, DHCP-Pool {start} - {end}"

    def _follow_ip_change(self, old_ip: str, new_ip: str, mask: str) -> None:
        self.log("info", f"Box wechselt die IP-Adresse {old_ip} -> {new_ip} ...")
        self.cb.progress("Warte auf neue Box-Adresse ...", None)
        netutil.sleep_cancellable(netutil.IP_CHANGE_SETTLE_SECONDS, self.cancel)
        net = ipaddress.IPv4Network(f"{new_ip}/{mask}", strict=False)
        local_before = netutil.local_ip_towards(old_ip)
        alias = netutil.adapter_alias_for_ip(local_before) if local_before else None

        def reachable() -> bool:
            return netutil.is_fritzbox_reachable(new_ip, 2.5)

        if netutil.wait_until(reachable, 20, 2, self.cancel):
            pass
        else:
            renewed = False
            if self.cfg.auto_renew_pc_ip:
                renewed = netutil.renew_dhcp(alias, self.log)
            if not netutil.wait_until(reachable, 90 if renewed else 120, 3, self.cancel):
                local = netutil.local_ip_towards(new_ip)
                hint = ""
                if local and ipaddress.IPv4Address(local) not in net:
                    hint = (f" Der PC hat die Adresse {local} (nicht im neuen Netz {net}). "
                            "Netzwerkadapter auf 'IP-Adresse automatisch beziehen' stellen oder Kabel kurz ziehen.")
                raise StepFailed(f"Box unter neuer Adresse {new_ip} nicht erreichbar.{hint}")
        self.cb.progress("", None)
        self.log("ok", f"Box unter {new_ip} erreichbar")
        self._relogin(new_ip)

    # ------------------------------------------------------------- Schritt 5
    def _select_image(self, box: BoxInfo) -> Optional[ImageInfo]:
        if self.cfg.firmware_file:
            img = inspect_image(self.cfg.firmware_file)
            if img.hw and box.hw and img.hw != box.hw:
                raise StepFailed(f"Gewähltes Image ist für Hardware {img.hw} ({img.product}), Box ist HW {box.hw} ({box.name})")
            if img.oems and box.oem and box.oem not in img.oems:
                raise StepFailed(f"Gewähltes Image unterstützt OEM '{box.oem}' nicht (nur {' '.join(img.oems)})")
            return img
        best = self.repo.best_cached(box)
        if self.cfg.firmware_online:
            self.cb.progress("Prüfe aktuelle Firmware auf download.avm.de ...", None)
            try:
                online = self.repo.download_latest(
                    box, progress=lambda d, t: self.cb.progress(f"Lade Firmware ... {d // 1048576} MB", d / t if t else None),
                    cancel=self.cancel.is_set)
                if online and online.oems and box.oem and box.oem not in online.oems:
                    self.log("warn", f"Online-Image unterstützt OEM '{box.oem}' nicht ({' '.join(online.oems)}) - ignoriert")
                elif online and online.hw == box.hw and (best is None or online.vtuple >= best.vtuple):
                    best = online
                elif online and online.hw != box.hw:
                    self.log("warn", f"Online-Image ist für HW {online.hw}, Box ist HW {box.hw} - ignoriert")
            except HttpError as e:
                self.log("warn", f"download.avm.de nicht erreichbar ({e}) - verwende lokalen Firmware-Ordner")
            finally:
                self.cb.progress("", None)
        return best

    def step_firmware(self) -> str:
        assert self.sess and self.http
        box = read_boxinfo(self.http)
        self.box = box
        if not box.hw:
            raise StepFailed("Hardware-ID der Box unbekannt - Firmware-Update aus Sicherheitsgründen abgebrochen")
        if box.lab:
            self.log("warn", f"Auf der Box läuft eine Labor-Version ({box.lab})")
        img = self._select_image(box)
        if img is None:
            raise StepWarning(f"Keine Firmware für {box.name} (HW {box.hw}) verfügbar - online nicht erreichbar und "
                              "nichts im Ordner 'firmware'. Tipp: vorher 'Firmware-Cache aktualisieren'.")
        self.log("info", f"Box: FRITZ!OS {box.fw_display} - Image: {os.path.basename(img.path)} (FRITZ!OS {img.display}, HW {img.hw})")
        if img.vtuple <= box.fw_tuple[:2] and not self.cfg.firmware_file:
            raise StepSkipped(f"bereits aktuell (FRITZ!OS {box.fw_display})")
        if img.vtuple < box.fw_tuple[:2]:
            raise StepFailed(f"Gewähltes Image {img.display} ist älter als installierte {box.fw_display} (Downgrade nicht unterstützt)")
        if img.vtuple == box.fw_tuple[:2]:
            raise StepSkipped(f"Image entspricht bereits installierter Version {box.fw_display}")
        if self.cfg.dry_run:
            raise StepSkipped(f"Testmodus - würde {img.display} installieren")
        with open(img.path, "rb") as fh:
            data = fh.read()
        self.sess.ensure()
        self.log("info", f"Übertrage {len(data) / 1048576:.1f} MB zur Box ... (Strom/Kabel jetzt nicht trennen!)")
        uploaded_ok, msg, via_tr064 = False, "", False
        try:
            r = self.http.post_multipart(
                "/cgi-bin/firmwarecfg", [("sid", self.sess.sid)], "UploadFile", os.path.basename(img.path), data,
                timeout=900, headers={"Accept": "application/json", "Authorization": f"AVM-SID {self.sess.sid}"},
                progress=lambda d, t: self.cb.progress(f"Firmware-Upload {d * 100 // t} %", d / t))
            uploaded_ok, msg = parse_firmwarecfg_response(r.text)
            if r.status >= 400:
                uploaded_ok, msg = False, f"HTTP {r.status}"
        except HttpError as e:
            # Box kann die Verbindung nach dem Empfang bereits schließen
            msg = str(e)
            uploaded_ok = True
            self.log("info", f"Verbindung nach Upload beendet ({e}) - prüfe, ob die Box das Update installiert")
        finally:
            self.cb.progress("", None)
        if not uploaded_ok:
            if "2FA" in msg or "3008" in msg:
                raise StepFailed("Box verlangt eine zusätzliche Bestätigung für das Firmware-Update: " + msg)
            self.log("warn", f"Upload abgelehnt: {msg} - versuche TR-064 (Box lädt Image vom PC)")
            self._firmware_via_tr064(img)
            via_tr064 = True
        else:
            self.log("ok", f"Upload abgeschlossen: {msg}")
        self.log("info", "Box installiert die Firmware und startet neu (3-8 Minuten) - NICHT ausschalten!")
        self.cb.progress("Firmware wird installiert ...", None)
        rebooted = netutil.wait_for_reboot(self.host, self.cancel, self.log, down_timeout=self.fw_timeouts[0],
                                           up_timeout=self.fw_timeouts[1], require_down=True,
                                           already_down=via_tr064)
        self.cb.progress("", None)
        if not rebooted:
            raise StepFailed("Box hat das Update nicht gestartet (kein Neustart erkannt)")
        after = read_boxinfo(HttpClient(self.host, timeout=10))
        self.result.box_after = after
        if after.fw_tuple[:2] != img.vtuple:
            raise StepWarning(f"Box meldet nach Neustart FRITZ!OS {after.fw_display}, erwartet {img.display}")
        try:
            self._relogin()
        except StepFailed:
            pass
        return f"FRITZ!OS {box.fw_display} -> {after.fw_display}"

    def _firmware_via_tr064(self, img: ImageInfo) -> None:
        local_ip = netutil.local_ip_towards(self.host)
        if not local_ip:
            raise StepFailed("Eigene IP-Adresse unbekannt - TR-064-Update nicht möglich")
        tr = self._tr064()
        with SingleFileServer(img.path, local_ip) as srv:
            self.log("info", f"Stelle Image bereit unter {srv.url} (ggf. Windows-Firewall-Abfrage bestätigen)")
            tr.call(S_UI, "X_AVM-DE_DoManualUpdate", **{"NewX_AVM-DE_DownloadURL": srv.url})
            # Box lädt das Image; Server läuft weiter, bis die Box neu startet
            went_down = netutil.wait_until(lambda: not netutil.is_fritzbox_reachable(self.host, 2.5),
                                           self.fw_timeouts[0], 2, self.cancel)
            if not went_down:
                raise StepFailed("Box hat das Image über TR-064 nicht installiert")

    # ------------------------------------------------- Schritt 6 (GUI: 5.)
    def _read_infoled(self) -> str:
        """Aktueller Wert von box:settings/infoled_reason ('' = nicht lesbar)."""
        val = ""
        try:
            val = str(self._query(led=VAR_INFOLED).get("led", "") or "")
        except HttpError:
            pass
        if val == "":
            assert self.sess
            try:
                d = self.sess.rest("GET", REST_BOX)
                if isinstance(d, dict) and d.get("infoled_reason") not in (None, ""):
                    val = str(d["infoled_reason"])
            except HttpError:
                pass
        return val

    def step_infoled(self) -> str:
        assert self.sess
        # nach einem Firmware-Update ist die alte Sitzung ungültig
        try:
            self.sess.ensure()
        except (HttpError, LoginError):
            self._relogin()
        want = str(self.cfg.infoled_reason)
        text = INFOLED_REASONS.get(want, want)
        cur = self._read_infoled()
        self.log("info", f"Aktuell: Info-LED '{INFOLED_REASONS.get(cur, cur or '?')}' - Ziel: '{text}'")
        if cur == want:
            raise StepSkipped(f"bereits eingestellt ({text})")
        if self.cfg.dry_run:
            raise StepSkipped(f"Testmodus - würde setzen: {text}")
        method = ""
        try:
            # wie System > Tasten und LEDs > LED-Anzeigen (FRITZ!OS 8.x): PUT /api/v0/generic/box
            self.sess.rest("PUT", REST_BOX, {"infoled_reason": int(want)})
            method = "LED-Anzeigen (REST)"
        except HttpError as e:
            self.log("warn", f"REST-Schnittstelle: {e} - versuche Seite 'Info-Anzeige' ({PAGE_INFOLED})")
            try:
                self.sess.apply(None, [("led_change_state_event", want)], direct_path=PAGE_INFOLED)
                method = "Info-Anzeige"
            except (ApplyError, HttpError) as e2:
                self.log("warn", f"Info-Anzeige: {e2}")
        got = self._read_infoled()
        if got != want:
            if not method:
                raise StepFailed("Info-LED konnte nicht gespeichert werden (Box unterstützt weder REST noch Info-Anzeige)")
            raise StepFailed(f"Box meldet Info-LED '{INFOLED_REASONS.get(got, got or '?')}' statt '{text}'")
        self.log("ok", f"Info-LED gespeichert via {method}")
        return text

    # ------------------------------------------------------------- Berichte
    def _write_reports(self, start: _dt.datetime) -> None:
        try:
            d = logs_dir()
            serial = (self.result.box_before.serial if self.result.box_before else "") or "unbekannt"
            fname = f"{start:%Y%m%d_%H%M%S}_{serial.replace(':', '')}.log"
            path = os.path.join(d, fname)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self._logbuf) + "\n")
            self.result.logfile = path
            csv_path = os.path.join(d, "protokoll.csv")
            _migrate_csv(csv_path)
            new = not os.path.exists(csv_path)
            b, a = self.result.box_before, self.result.box_after
            with open(csv_path, "a", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh, delimiter=";")
                if new:
                    w.writerow(CSV_HEADER)
                w.writerow([start.strftime("%Y-%m-%d %H:%M:%S"), "OK" if self.result.ok else "FEHLER",
                            b.name if b else "", b.serial if b else "", b.fw_display if b else "",
                            a.fw_display if a else "", self.result.host,
                            self.result.states.get("locale", ""), self.result.states.get("provider", ""),
                            self.result.states.get("network", ""), self.result.states.get("firmware", ""),
                            self.result.states.get("infoled", ""), fname])
        except OSError as e:
            self.cb.log("warn", f"Protokoll konnte nicht geschrieben werden: {e}")


CSV_HEADER = ["Zeit", "Ergebnis", "Modell", "MAC/Serial", "FW vorher", "FW nachher", "Box-IP",
              "Sprache/Land", "Anbieter", "Heimnetz", "Firmware", "Info-LED", "Logdatei"]


def _migrate_csv(path: str) -> None:
    """Ältere protokoll.csv (ohne Spalte 'Info-LED') um die leere Spalte ergänzen."""
    if not os.path.exists(path):
        return
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    if not rows or "Info-LED" in rows[0] or "Logdatei" not in rows[0]:
        return
    idx = rows[0].index("Logdatei")
    for row in rows:
        if len(row) > idx:
            row.insert(idx, "Info-LED" if row is rows[0] else "")
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        csv.writer(fh, delimiter=";").writerows(rows)
    os.replace(tmp, path)
