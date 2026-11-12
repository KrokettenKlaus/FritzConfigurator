"""Netzwerk-Hilfsfunktionen: Warten auf Box, IP-Berechnungen, DHCP-Erneuerung unter Windows."""
from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Callable, Optional, Tuple

from .boxinfo import is_fritzbox_reachable


class Cancelled(Exception):
    pass


# Wartezeit nach dem Wiederauftauchen der Box, bis alle Dienste laufen (Tests setzen 0)
BOOT_SETTLE_SECONDS = 8.0
IP_CHANGE_SETTLE_SECONDS = 6.0


def sleep_cancellable(seconds: float, cancel: Optional[threading.Event]) -> None:
    if cancel is None:
        time.sleep(seconds)
        return
    if cancel.wait(seconds):
        raise Cancelled()


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 2.0,
               cancel: Optional[threading.Event] = None) -> bool:
    end = time.monotonic() + timeout
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if predicate():
            return True
        if time.monotonic() >= end:
            return False
        sleep_cancellable(interval, cancel)


def wait_for_reboot(host: str, cancel: Optional[threading.Event], log: Callable[[str, str], None],
                    down_timeout: float = 90, up_timeout: float = 420, require_down: bool = True,
                    already_down: bool = False) -> bool:
    """Wartet, bis die Box offline geht und wieder erreichbar ist. True = Neustart beobachtet."""
    went_down = already_down or wait_until(lambda: not is_fritzbox_reachable(host, 2.5), down_timeout, 2.0, cancel)
    if not went_down:
        if require_down:
            log("warn", "Box ist nicht offline gegangen - kein Neustart erkannt")
        return False
    log("info", "Box startet neu - warte bis sie wieder erreichbar ist (dauert 1-4 Minuten) ...")
    if not wait_until(lambda: is_fritzbox_reachable(host, 3.0), up_timeout, 4.0, cancel):
        raise TimeoutError(f"Box ist nach {int(up_timeout)} s nicht wieder erreichbar")
    # Weboberfläche braucht nach dem Start noch etwas, bis alle Dienste laufen
    sleep_cancellable(BOOT_SETTLE_SECONDS, cancel)
    return True


def parse_ipv4(value: str, what: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise ValueError(f"{what}: '{value}' ist keine gültige IPv4-Adresse") from None


def validate_lan_settings(gw: str, mask: str, start: str, end: str) -> Tuple[str, str, str, str]:
    """Prüft GW/Maske/DHCP-Pool wie die FRITZ!Box (net/boxnet.lua) und gibt normierte Werte zurück."""
    ip = parse_ipv4(gw, "Gateway/Box-IP")
    try:
        net = ipaddress.IPv4Network(f"{ip}/{mask.strip()}", strict=False)
    except ValueError:
        raise ValueError(f"Subnetzmaske '{mask}' ist ungültig") from None
    if net.prefixlen > 30 or net.prefixlen < 8:
        raise ValueError("Subnetzmaske muss zwischen /8 und /30 liegen")
    if ip in (net.network_address, net.broadcast_address):
        raise ValueError("Box-IP darf nicht Netz- oder Broadcastadresse sein")
    if not ip.is_private:
        raise ValueError("Box-IP sollte eine private Adresse sein (10.x, 172.16-31.x, 192.168.x)")
    s = parse_ipv4(start, "DHCP-Start")
    e = parse_ipv4(end, "DHCP-Ende")
    for addr, what in ((s, "DHCP-Start"), (e, "DHCP-Ende")):
        if addr not in net:
            raise ValueError(f"{what} {addr} liegt nicht im Netz {net}")
        if addr in (net.network_address, net.broadcast_address):
            raise ValueError(f"{what} {addr} ist Netz- oder Broadcastadresse")
    if int(s) > int(e):
        raise ValueError("DHCP-Start muss kleiner/gleich DHCP-Ende sein")
    if int(s) <= int(ip) <= int(e):
        raise ValueError(f"Box-IP {ip} liegt innerhalb des DHCP-Pools {s} - {e}")
    return str(ip), str(net.netmask), str(s), str(e)


def suggest_pool(gw: str, mask: str = "255.255.255.0") -> Tuple[str, str]:
    """Vorschlag analog FRITZ!Box-Standard (.20 - .200) im Netz der Box-IP."""
    ip = ipaddress.IPv4Address(gw.strip())
    net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
    base = int(net.network_address)
    last = int(net.broadcast_address) - 1
    if net.prefixlen <= 24:
        start, end = base + 20, base + 200
    else:
        start, end = base + 2, last
    # Box-IP nicht im Pool
    if start <= int(ip) <= end:
        if int(ip) - base < 20:
            start = int(ip) + 1
        else:
            end = int(ip) - 1
    return str(ipaddress.IPv4Address(start)), str(ipaddress.IPv4Address(min(end, last)))


def local_ip_towards(host: str) -> Optional[str]:
    """Eigene IP-Adresse, die für die Verbindung zu host verwendet wird."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 80))
            return s.getsockname()[0]
    except OSError:
        return None


def _run(cmd: list, timeout: float = 60) -> Tuple[int, str]:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
        out = p.stdout.decode("cp850", "replace") + p.stderr.decode("cp850", "replace")
        return p.returncode, out
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)


def adapter_alias_for_ip(local_ip: str) -> Optional[str]:
    if sys.platform != "win32" or not local_ip:
        return None
    rc, out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"(Get-NetIPAddress -IPAddress '{local_ip}' -ErrorAction SilentlyContinue).InterfaceAlias"], 30)
    alias = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else ""
    return alias or None


def renew_dhcp(adapter_alias: Optional[str], log: Callable[[str, str], None]) -> bool:
    """Erneuert die DHCP-Adresse (nur Windows). Gibt True zurück, wenn der Befehl lief."""
    if sys.platform != "win32":
        log("info", "Automatische DHCP-Erneuerung nur unter Windows - bitte Netzwerkadresse manuell erneuern")
        return False
    target = [adapter_alias] if adapter_alias else []
    log("info", "Erneuere IP-Adresse des PCs (ipconfig /renew" + (f' "{adapter_alias}"' if adapter_alias else "") + ") ...")
    rc, out = _run(["ipconfig", "/renew"] + target, 90)
    if rc != 0:
        log("warn", "ipconfig /renew meldet: " + " ".join(out.split())[:300])
    return True


def is_windows() -> bool:
    return os.name == "nt"
