"""TR-064 (SOAP) Client mit Digest-Authentifizierung - nur Standardbibliothek."""
from __future__ import annotations

import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from typing import Dict, Optional
from xml.sax.saxutils import escape

from .httpclient import USER_AGENT, HttpError, build_opener, host_for_url

S_UI = "urn:dslforum-org:service:UserInterface:1"
S_DEVINFO = "urn:dslforum-org:service:DeviceInfo:1"
S_DEVCONFIG = "urn:dslforum-org:service:DeviceConfig:1"
S_LANHOST = "urn:dslforum-org:service:LANHostConfigManagement:1"
S_PPP = "urn:dslforum-org:service:WANPPPConnection:1"
S_MGMT = "urn:dslforum-org:service:ManagementServer:1"

DEFAULT_CONTROL_URLS = {
    S_UI: "/upnp/control/userif",
    S_DEVINFO: "/upnp/control/deviceinfo",
    S_DEVCONFIG: "/upnp/control/deviceconfig",
    S_LANHOST: "/upnp/control/lanhostconfigmgm",
    S_PPP: "/upnp/control/wanpppconn1",
    S_MGMT: "/upnp/control/mgmsrv",
}


class Tr064Error(Exception):
    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


class Tr064Client:
    def __init__(self, host: str, username: str, password: str, port: int = 49000, timeout: float = 20.0):
        self.host = host
        self.port = port
        self.username = username or ""
        self.password = password
        self.timeout = timeout
        self.base = f"http://{host_for_url(host)}:{port}"
        pwmgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pwmgr.add_password(None, self.base, self.username, password)
        self._opener = build_opener(False, extra_handlers=[urllib.request.HTTPDigestAuthHandler(pwmgr)])
        self._control_urls: Dict[str, str] = {}

    def discover(self) -> Dict[str, str]:
        """Liest tr64desc.xml (ohne Anmeldung) und merkt sich die controlURLs."""
        req = urllib.request.Request(self.base + "/tr64desc.xml", headers={"User-Agent": USER_AGENT})
        try:
            with build_opener(False).open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as e:
            raise Tr064Error(f"TR-064 nicht erreichbar ({e})") from e
        urls = {}
        for svc in re.findall(r"<service>(.*?)</service>", text, re.S):
            st = re.search(r"<serviceType>(.*?)</serviceType>", svc)
            cu = re.search(r"<controlURL>(.*?)</controlURL>", svc)
            if st and cu:
                urls.setdefault(st.group(1).strip(), cu.group(1).strip())
        self._control_urls = urls
        return urls

    def has_service(self, service: str) -> bool:
        if not self._control_urls:
            try:
                self.discover()
            except Tr064Error:
                return False
        return service in self._control_urls

    def call(self, service: str, action: str, **args: object) -> Dict[str, str]:
        control = self._control_urls.get(service) or DEFAULT_CONTROL_URLS.get(service)
        if not control:
            raise Tr064Error(f"Unbekannter TR-064-Dienst {service}")
        body_args = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service}">{body_args}</u:{action}></s:Body></s:Envelope>'
        ).encode("utf-8")
        req = urllib.request.Request(
            self.base + control, data=envelope, method="POST",
            headers={"Content-Type": 'text/xml; charset="utf-8"',
                     "SOAPAction": f'"{service}#{action}"',
                     "User-Agent": USER_AGENT})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            code = _find(detail, "errorCode")
            desc = _find(detail, "errorDescription")
            if e.code == 401:
                raise Tr064Error("TR-064: Anmeldung abgelehnt (Benutzer/Kennwort)", "401") from e
            raise Tr064Error(f"TR-064 {action}: HTTP {e.code} {code or ''} {desc or ''}".strip(), code) from e
        except Exception as e:
            raise Tr064Error(f"TR-064 {action} fehlgeschlagen: {e}") from e
        return _parse_response(text, action)


def _find(text: str, tag: str) -> Optional[str]:
    m = re.search(rf"<(?:\w+:)?{tag}>(.*?)</(?:\w+:)?{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def _parse_response(text: str, action: str) -> Dict[str, str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise Tr064Error(f"TR-064 {action}: ungültige Antwort") from e
    result: Dict[str, str] = {}
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == f"{action}Response":
            for child in el:
                result[child.tag.split("}")[-1]] = (child.text or "").strip()
            return result
    raise Tr064Error(f"TR-064 {action}: keine Antwortdaten")


__all__ = ["Tr064Client", "Tr064Error", "S_UI", "S_DEVINFO", "S_DEVCONFIG", "S_LANHOST", "S_PPP", "S_MGMT",
           "HttpError"]
