"""Erkennung der FRITZ!Box (ohne Anmeldung) über /jason_boxinfo.xml bzw. /juis_boxinfo.xml."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from .httpclient import HttpClient, HttpError


@dataclass
class BoxInfo:
    name: str = ""          # "FRITZ!Box 7530 AX"
    hw: str = ""            # "256"
    version: str = ""       # "256.08.50"
    revision: str = ""
    serial: str = ""        # MAC
    oem: str = ""           # avm / avme / 1und1
    lang: str = ""
    country: str = ""       # "049", "039"
    annex: str = ""
    lab: str = ""
    host: str = ""

    @property
    def fw_tuple(self) -> Tuple[int, ...]:
        return version_tuple(self.version)

    @property
    def fw_display(self) -> str:
        t = self.fw_tuple
        return "{}.{:02d}".format(t[0], t[1]) if len(t) >= 2 else (self.version or "?")

    def summary(self) -> str:
        parts = [self.name or "FRITZ!Box", f"FRITZ!OS {self.fw_display}"]
        if self.lab:
            parts.append(f"Labor {self.lab}")
        if self.oem:
            parts.append(f"OEM {self.oem}")
        if self.serial:
            parts.append(f"MAC {self.serial}")
        return ", ".join(parts)


def version_tuple(version: str) -> Tuple[int, ...]:
    """"256.08.50" -> (8, 50); "08.50" -> (8, 50); "7.57" -> (7, 57); "256.08.50-12345" -> (8, 50, 12345)."""
    if not version:
        return ()
    v = version.strip()
    build = None
    m = re.match(r"^(.*?)-(\d+)$", v)
    if m:
        v, build = m.group(1), int(m.group(2))
    nums = [int(x) for x in re.findall(r"\d+", v)]
    if len(nums) >= 3:
        nums = nums[-2:]  # Hardware-Präfix entfernen
    res = tuple(nums[:2])
    if build is not None:
        res = res + (build,)
    return res


def compare_versions(a: str, b: str) -> int:
    ta, tb = version_tuple(a)[:2], version_tuple(b)[:2]
    return (ta > tb) - (ta < tb)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def parse_boxinfo(xml_text: str) -> BoxInfo:
    root = ET.fromstring(xml_text.strip())
    vals = {}
    for el in root.iter():
        vals[_local(el.tag).lower()] = (el.text or "").strip()
    info = BoxInfo(
        name=vals.get("name", ""),
        hw=vals.get("hw", ""),
        version=vals.get("version", ""),
        revision=vals.get("revision", ""),
        serial=vals.get("serial", ""),
        oem=vals.get("oem", ""),
        lang=vals.get("lang", ""),
        country=vals.get("country", ""),
        annex=vals.get("annex", ""),
        lab=vals.get("lab", ""),
    )
    if not info.version and vals.get("major"):
        # juis_boxinfo.xml: Major/Minor/Patch
        info.version = f"{vals.get('major')}.{int(vals.get('minor', '0')):02d}.{int(vals.get('patch', '0')):02d}"
    return info


def read_boxinfo(http: HttpClient) -> BoxInfo:
    last_err: Optional[Exception] = None
    for path in ("/jason_boxinfo.xml", "/juis_boxinfo.xml"):
        try:
            r = http.get(path, timeout=6)
            if r.status == 200 and r.body.strip().startswith(b"<"):
                info = parse_boxinfo(r.text)
                if info.name or info.version:
                    info.host = http.host
                    return info
        except (HttpError, ET.ParseError) as e:
            last_err = e
    raise HttpError(f"Keine Geräteinfo von {http.host} ({last_err})")


def is_fritzbox_reachable(host: str, timeout: float = 3.0) -> bool:
    try:
        r = HttpClient(host, timeout=timeout).get("/login_sid.lua", params={"version": "2"}, timeout=timeout)
        return b"<SessionInfo>" in r.body or b"<SID>" in r.body
    except HttpError:
        return False


def detect(candidates: Iterable[str], timeout: float = 3.0) -> Tuple[str, BoxInfo]:
    """Findet die erste erreichbare FRITZ!Box aus der Kandidatenliste."""
    tried: List[str] = []
    for host in candidates:
        host = host.strip()
        if not host or host in tried:
            continue
        tried.append(host)
        if not is_fritzbox_reachable(host, timeout):
            continue
        try:
            info = read_boxinfo(HttpClient(host, timeout=timeout + 3))
        except HttpError:
            info = BoxInfo(name="FRITZ!Box (unbekanntes Modell)", host=host)
        info.host = host
        return host, info
    raise HttpError("Keine FRITZ!Box gefunden (versucht: " + ", ".join(tried) + "). "
                    "LAN-Kabel an LAN-Port der Box? Netzwerkadapter auf DHCP?")
