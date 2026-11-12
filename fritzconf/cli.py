"""Kommandozeile: python -m fritzconf --password XXXX [Optionen]"""
from __future__ import annotations

import argparse
import getpass
import sys
import threading

from .config import PROVIDERS, JobConfig, firmware_dir, load_settings
from .firmware import FirmwareRepo
from .runner import Callbacks, Runner

COLORS = {"ok": "\033[32m", "error": "\033[31m", "warn": "\033[33m", "step": "\033[36m"}


def main(argv=None) -> int:
    base = load_settings()
    p = argparse.ArgumentParser(prog="fritzconf", description="FRITZ!Box vorkonfigurieren (Sprache/Land, KONVERTO, Netz, Firmware, Info-LED)")
    p.add_argument("--password", "-p", help="FRITZ!Box-Kennwort (Aufkleber). Ohne Angabe wird gefragt.")
    p.add_argument("--user", default=base.username, help="Benutzername (Standard: automatisch)")
    p.add_argument("--host", default=base.host, help="Adresse der Box (Standard: automatisch suchen)")
    p.add_argument("--language", default=base.language)
    p.add_argument("--country", default=base.country, help="Ländervorwahl, z.B. 39 = Italien")
    p.add_argument("--provider", default=base.provider, choices=list(PROVIDERS))
    p.add_argument("--ppp-user", help="eigene Zugangsdaten statt Dummy-Daten")
    p.add_argument("--ppp-password")
    p.add_argument("--lan", metavar="IP[/MASKE]", help="Box-IP (Gateway) setzen, z.B. 192.168.10.1/255.255.255.0")
    p.add_argument("--dhcp", metavar="START-ENDE", help="DHCP-Pool, z.B. 192.168.10.20-192.168.10.200")
    p.add_argument("--firmware", action="store_true", help="neueste Firmware aufspielen")
    p.add_argument("--firmware-file", help="bestimmtes .image verwenden")
    p.add_argument("--offline", action="store_true", help="Firmware nur aus lokalem Ordner 'firmware'")
    p.add_argument("--skip-locale", action="store_true")
    p.add_argument("--skip-provider", action="store_true")
    p.add_argument("--skip-infoled", action="store_true",
                   help="Info-LED nicht einstellen (Standard: 'Internetverbindung besteht - LED leuchtet')")
    p.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts ändern")
    p.add_argument("--prefetch", nargs="*", metavar="MODELL",
                   help="Firmware für Modelle (z.B. fritzbox-7530-ax) in den Cache laden und beenden")
    a = p.parse_args(argv)

    if a.prefetch is not None:
        repo = FirmwareRepo(firmware_dir(), _log)
        models = a.prefetch or repo.list_online_models()
        n = repo.prefetch_models(models, _log)
        print(f"{n} Image(s) im Cache")
        return 0

    cfg = JobConfig(**{**base.to_json()})
    cfg.host = a.host or ""
    cfg.username = a.user or ""
    cfg.password = a.password or getpass.getpass("FRITZ!Box-Kennwort (Aufkleber): ")
    cfg.language, cfg.country, cfg.provider = a.language, a.country, a.provider
    cfg.set_locale = not a.skip_locale
    cfg.set_provider = not a.skip_provider
    cfg.set_infoled = cfg.set_infoled and not a.skip_infoled
    if a.ppp_user:
        cfg.custom_credentials, cfg.ppp_user, cfg.ppp_password = True, a.ppp_user, a.ppp_password or ""
    cfg.set_network = bool(a.lan or a.dhcp)
    if a.lan:
        ip, _, mask = a.lan.partition("/")
        cfg.lan_ip, cfg.lan_mask = ip, (mask or "255.255.255.0")
        if mask.isdigit():
            import ipaddress
            cfg.lan_mask = str(ipaddress.IPv4Network(f"0.0.0.0/{mask}").netmask)
    if a.dhcp:
        cfg.dhcp_start, _, cfg.dhcp_end = a.dhcp.partition("-")
    elif a.lan:
        from .netutil import suggest_pool
        cfg.dhcp_start, cfg.dhcp_end = suggest_pool(cfg.lan_ip, cfg.lan_mask)
    cfg.update_firmware = bool(a.firmware or a.firmware_file)
    cfg.firmware_file = a.firmware_file or ""
    cfg.firmware_online = not a.offline
    cfg.dry_run = a.dry_run

    cancel = threading.Event()
    runner = Runner(cfg, Callbacks(log=_log), cancel)
    try:
        res = runner.run()
    except KeyboardInterrupt:
        cancel.set()
        return 130
    print(f"Logdatei: {res.logfile}")
    return 0 if res.ok else 1


def _log(level: str, msg: str) -> None:
    color = COLORS.get(level, "") if sys.stdout.isatty() else ""
    reset = "\033[0m" if color else ""
    print(f"{color}[{level.upper():5}] {msg}{reset}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
