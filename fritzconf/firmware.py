"""Firmware: Suche auf download.avm.de, lokaler Cache, Prüfung des Images und Upload zur Box.

Upload wie Weboberfläche (System > Update > FRITZ!OS-Datei):
  POST /cgi-bin/firmwarecfg  multipart: sid, UploadFile=<image>
Antwort (FRITZ!OS 7.5x/8.x, Accept: application/json):
  {"reboot": 1} bzw. {"reboot":0,"errors":[{"code":20002,"message":"..."}]}
"""
from __future__ import annotations

import http.server
import json
import os
import re
import socketserver
import tarfile
import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from .boxinfo import BoxInfo, version_tuple
from .httpclient import HttpError, download_to_file, fetch_url

AVM_BASE = "https://download.avm.de/fritzbox/"

FIRMWARECFG_ERRORS = {
    3001: "Keine Berechtigung", 3004: "Ungültige Variable", 3008: "Zusätzliche Bestätigung (2FA) nötig",
    20002: "Image passt nicht zur Hardware", 20003: "Kernel-Prüfsumme falsch",
    20004: "Dateisystem-Prüfsumme falsch", 20005: "Urlader-Prüfsumme falsch", 20006: "Sonstiger Installationsfehler",
    20007: "Firmware-Version nicht installierbar", 20008: "Downgrade nötig/nicht erlaubt",
    20012: "Content-Length-Fehler", 20013: "Datei zu klein", 20014: "Datei zu groß",
    20026: "Firmware nicht signiert", 20027: "Download-Fehler",
}


@dataclass
class ImageInfo:
    path: str
    hw: str = ""            # "256"
    product: str = ""       # "FRITZ!Box 7530 AX"
    version: str = ""       # "08.50"
    fwversion: str = ""     # "256.08.50"
    build: str = ""
    oems: Tuple[str, ...] = ()
    countries: Tuple[str, ...] = ()
    release_type: str = ""  # Release / Labor ...

    @property
    def vtuple(self) -> Tuple[int, ...]:
        return version_tuple(self.fwversion or self.version)[:2]

    @property
    def display(self) -> str:
        t = self.vtuple
        return "{}.{:02d}".format(*t) if len(t) == 2 else (self.version or "?")


def inspect_image(path: str) -> ImageInfo:
    """Liest ./var/content aus dem Image (tar-Archiv)."""
    info = ImageInfo(path=path)
    try:
        with tarfile.open(path, "r:") as tf:
            member = None
            for m in tf.getmembers():
                if m.isfile() and m.name.lstrip("./") == "var/content":
                    member = m
                    break
            if member is None:
                raise ValueError("var/content fehlt")
            fh = tf.extractfile(member)
            content = fh.read().decode("utf-8", "replace") if fh else ""
    except (tarfile.TarError, OSError, ValueError) as e:
        raise ValueError(f"{os.path.basename(path)} ist kein gültiges FRITZ!OS-Image ({e})") from None
    kv: Dict[str, str] = {}
    for line in content.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    prod = kv.get("Product", "")
    m = re.search(r"HW(\d+)", prod)
    info.hw = m.group(1) if m else ""
    m = re.search(r'\("([^"]+)"\)', prod)
    info.product = m.group(1) if m else ""
    info.version = kv.get("Version", "")
    fw = kv.get("FIRMWAREVERSION", "")
    parts = fw.split(",")
    info.fwversion = parts[0] if parts else ""
    info.build = kv.get("Build", parts[2] if len(parts) > 2 else "")
    info.oems = tuple(kv.get("OEMs", "").split())
    info.countries = tuple(kv.get("Countries", "").split())
    info.release_type = kv.get("Type", "")
    if not info.fwversion and info.hw and info.version:
        info.fwversion = f"{info.hw}.{info.version}"
    return info


def model_slug_candidates(box_name: str) -> List[str]:
    """"FRITZ!Box 7530 AX" -> ["fritzbox-7530-ax", ...]."""
    n = box_name.strip().lower()
    n = n.replace("fritz!box", "fritzbox").replace("fritz! box", "fritzbox")
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    cands = [n]
    if not n.startswith("fritzbox-"):
        cands.append("fritzbox-" + n.replace("fritzbox", "").strip("-"))
    return cands


def region_candidates(oem: str) -> List[str]:
    oem = (oem or "").lower()
    if oem == "avm":
        return ["deutschland", "other"]
    return ["other", "deutschland"]


def _hrefs(html: str) -> List[str]:
    return re.findall(r'href="([^"?#]+)"', html)


class FirmwareRepo:
    def __init__(self, cache_dir: str, log: Callable[[str, str], None]):
        self.cache_dir = cache_dir
        self.log = log
        os.makedirs(cache_dir, exist_ok=True)
        self._inspect_cache: Dict[Tuple[str, float, int], ImageInfo] = {}

    # ----------------------------------------------------------- lokaler Cache
    def cached_images(self) -> List[ImageInfo]:
        res = []
        for root, _dirs, files in os.walk(self.cache_dir):
            for f in files:
                if not f.lower().endswith(".image"):
                    continue
                p = os.path.join(root, f)
                try:
                    st = os.stat(p)
                    key = (p, st.st_mtime, st.st_size)
                    if key not in self._inspect_cache:
                        self._inspect_cache[key] = inspect_image(p)
                    res.append(self._inspect_cache[key])
                except (OSError, ValueError) as e:
                    self.log("warn", str(e))
        return res

    def best_cached(self, box: BoxInfo) -> Optional[ImageInfo]:
        cands = [i for i in self.cached_images() if i.hw and i.hw == box.hw and _oem_ok(i, box)]
        cands = [i for i in cands if not i.release_type or i.release_type.lower() == "release"] or cands
        if not cands:
            return None
        return max(cands, key=lambda i: i.vtuple)

    # ------------------------------------------------------------- AVM online
    def find_online(self, box: BoxInfo, timeout: float = 20) -> Optional[Tuple[str, str]]:
        """Liefert (URL, Dateiname) des aktuellen Images für das Modell, oder None."""
        index = fetch_url(AVM_BASE, timeout=timeout).text
        models = {h.strip("/").lower(): h.strip("/") for h in _hrefs(index) if h.endswith("/") and h.startswith("fritzbox")}
        slug = None
        for c in model_slug_candidates(box.name):
            if c in models:
                slug = models[c]
                break
        if not slug:
            self.log("warn", f"Modell '{box.name}' nicht auf download.avm.de gefunden")
            return None
        model_url = AVM_BASE + slug + "/"
        regions = [h.strip("/") for h in _hrefs(fetch_url(model_url, timeout=timeout).text) if h.endswith("/") and not h.startswith("..")]
        for region in region_candidates(box.oem):
            if region not in regions:
                continue
            url = f"{model_url}{region}/fritz.os/"
            try:
                listing = fetch_url(url, timeout=timeout).text
            except HttpError:
                continue
            images = [h for h in _hrefs(listing) if h.lower().endswith(".image")]
            if not images:
                continue
            images.sort(key=lambda f: version_tuple(re.sub(r"\.image$", "", f.split("-")[-1])))
            name = images[-1]
            return url + name, name
        self.log("warn", f"Kein Firmware-Image für {box.name} gefunden (Regionen: {', '.join(regions)})")
        return None

    def download_latest(self, box: BoxInfo, progress: Optional[Callable[[int, int], None]] = None,
                        cancel: Optional[Callable[[], bool]] = None) -> Optional[ImageInfo]:
        found = self.find_online(box)
        if not found:
            return None
        url, name = found
        slug_dir = os.path.join(self.cache_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", url.split("/fritzbox/")[-1].split("/fritz.os/")[0]))
        os.makedirs(slug_dir, exist_ok=True)
        dest = os.path.join(slug_dir, name)
        if os.path.exists(dest):
            try:
                return inspect_image(dest)
            except ValueError:
                os.remove(dest)
        self.log("info", f"Lade {url} ...")
        download_to_file(url, dest, timeout=60, progress=progress, cancel=cancel)
        return inspect_image(dest)

    def prefetch_models(self, slugs: List[str], progress_log: Callable[[str, str], None],
                        cancel: Optional[Callable[[], bool]] = None) -> int:
        """Lädt für die angegebenen Modell-Ordner (z.B. 'fritzbox-7530-ax') die aktuelle Firmware (Region other)."""
        count = 0
        for slug in slugs:
            if cancel and cancel():
                break
            box = BoxInfo(name=slug.replace("fritzbox-", "FRITZ!Box ").replace("-", " "), oem="avme")
            try:
                img = self.download_latest(box, cancel=cancel)
                if img:
                    progress_log("ok", f"{slug}: {os.path.basename(img.path)} (FRITZ!OS {img.display})")
                    count += 1
            except (HttpError, ValueError) as e:
                progress_log("error", f"{slug}: {e}")
        return count

    def list_online_models(self) -> List[str]:
        index = fetch_url(AVM_BASE, timeout=20).text
        return sorted({h.strip("/") for h in _hrefs(index) if h.startswith("fritzbox") and h.endswith("/")})


def _oem_ok(img: ImageInfo, box: BoxInfo) -> bool:
    if not img.oems or not box.oem:
        return True
    return box.oem in img.oems


def parse_firmwarecfg_response(text: str) -> Tuple[bool, str]:
    """(ok, Meldung) aus der Antwort von /cgi-bin/firmwarecfg."""
    t = text.strip()
    if t.startswith("{"):
        try:
            obj = json.loads(t)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            errs = obj.get("errors") or []
            if errs:
                e = errs[0] if isinstance(errs[0], dict) else {}
                code = e.get("code")
                msg = FIRMWARECFG_ERRORS.get(code if isinstance(code, int) else -1, e.get("message", ""))
                return False, f"Fehler {code}: {msg}"
            return True, "Box übernimmt Firmware" + (" und startet neu" if obj.get("reboot") else "")
    low = t.lower()
    for code, msg in FIRMWARECFG_ERRORS.items():
        if str(code) in t and ("error" in low or "fehler" in low):
            return False, f"Fehler {code}: {msg}"
    if "error" in low or "fehler" in low:
        return False, re.sub(r"<[^>]+>", " ", t)[:300]
    return True, "Upload angenommen"


class SingleFileServer:
    """Kleiner HTTP-Server, der genau eine Datei ausliefert (für TR-064 X_AVM-DE_DoManualUpdate)."""

    def __init__(self, path: str, bind_ip: str):
        self.path = path
        filename = os.path.basename(path)
        file_path = path

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0].lstrip("/") != filename:
                    self.send_error(404)
                    return
                size = os.path.getsize(file_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(file_path, "rb") as fh:
                    while True:
                        buf = fh.read(256 * 1024)
                        if not buf:
                            break
                        self.wfile.write(buf)

            def log_message(self, *a):  # still
                pass

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = Server((bind_ip, 0), Handler)
        self.url = f"http://{bind_ip}:{self.httpd.server_address[1]}/{filename}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
