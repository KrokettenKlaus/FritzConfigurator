"""Minimaler HTTP-Client (nur Standardbibliothek).

Wichtig: Für Zugriffe auf die FRITZ!Box wird ein eventuell im System
konfigurierter Proxy bewusst ignoriert (Windows übernimmt sonst den
IE/Edge-Proxy auch für 192.168.178.1).
"""
from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple, Union

USER_AGENT = "FritzConfigurator/1.0"

FieldValue = Union[str, int, None]
Fields = Union[Mapping[str, FieldValue], Sequence[Tuple[str, FieldValue]]]


class HttpError(Exception):
    """Netzwerk- oder HTTP-Fehler."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    status: int
    headers: Dict[str, str]
    body: bytes
    url: str

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str, default: str = "") -> str:
        lname = name.lower()
        for k, v in self.headers.items():
            if k.lower() == lname:
                return v
        return default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _items(fields: Optional[Fields]) -> list:
    if not fields:
        return []
    if isinstance(fields, Mapping):
        items = list(fields.items())
    else:
        items = list(fields)
    return [(k, "" if v is None else str(v)) for k, v in items]


def encode_form(fields: Optional[Fields]) -> bytes:
    return urllib.parse.urlencode(_items(fields)).encode("utf-8")


def build_opener(use_system_proxy: bool, follow_redirects: bool = True,
                 extra_handlers: Iterable[urllib.request.BaseHandler] = ()) -> urllib.request.OpenerDirector:
    handlers: list = []
    if not use_system_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if not follow_redirects:
        handlers.append(_NoRedirect())
    handlers.extend(extra_handlers)
    return urllib.request.build_opener(*handlers)


def host_for_url(host: str) -> str:
    """IPv6-Adressen in eckige Klammern setzen."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


class HttpClient:
    """Einfacher Client für eine Basis-URL (http://host[:port])."""

    def __init__(self, host: str, port: int = 80, scheme: str = "http", timeout: float = 15.0,
                 use_system_proxy: bool = False):
        self.host = host
        self.port = port
        self.scheme = scheme
        self.timeout = timeout
        self.use_system_proxy = use_system_proxy
        self._opener = build_opener(use_system_proxy, follow_redirects=True)
        self._opener_noredir = build_opener(use_system_proxy, follow_redirects=False)

    @property
    def base_url(self) -> str:
        default = 80 if self.scheme == "http" else 443
        port = "" if self.port == default else f":{self.port}"
        return f"{self.scheme}://{host_for_url(self.host)}{port}"

    def url(self, path: str, params: Optional[Fields] = None) -> str:
        if not path.startswith("/"):
            path = "/" + path
        u = self.base_url + path
        if params:
            u += ("&" if "?" in u else "?") + urllib.parse.urlencode(_items(params))
        return u

    def request(self, method: str, path: str, params: Optional[Fields] = None,
                data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                timeout: Optional[float] = None, follow_redirects: bool = True) -> Response:
        url = self.url(path, params)
        hdrs = {"User-Agent": USER_AGENT}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        opener = self._opener if follow_redirects else self._opener_noredir
        try:
            with opener.open(req, timeout=timeout or self.timeout) as resp:
                body = resp.read()
                return Response(resp.status, dict(resp.headers.items()), body, resp.geturl())
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read()
            except Exception:  # pragma: no cover
                pass
            # Redirects bei follow_redirects=False landen hier
            if 300 <= e.code < 400:
                return Response(e.code, dict(e.headers.items()) if e.headers else {}, body, url)
            raise HttpError(f"HTTP {e.code} bei {method} {path}", e.code) from e
        except (urllib.error.URLError, socket.timeout, ConnectionError, http.client.HTTPException,
                ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise HttpError(f"Keine Verbindung zu {self.base_url} ({reason})") from e

    def get(self, path: str, params: Optional[Fields] = None, **kw) -> Response:
        return self.request("GET", path, params=params, **kw)

    def post_form(self, path: str, fields: Fields, params: Optional[Fields] = None, **kw) -> Response:
        headers = kw.pop("headers", {}) or {}
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        return self.request("POST", path, params=params, data=encode_form(fields), headers=headers, **kw)

    def post_multipart(self, path: str, fields: Fields, file_field: str, file_name: str,
                       file_data: bytes, timeout: Optional[float] = None,
                       progress: Optional[Callable[[int, int], None]] = None,
                       headers: Optional[Dict[str, str]] = None) -> Response:
        """Multipart-Upload, Datei als *letztes* Feld (AVM firmwarecfg erwartet das).

        Wird über http.client gestreamt, damit ein Fortschritt gemeldet werden kann.
        """
        boundary = "----FritzConfigurator" + uuid.uuid4().hex
        head = bytearray()
        for k, v in _items(fields):
            head += f"--{boundary}\r\n".encode()
            head += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
            head += v.encode("utf-8") + b"\r\n"
        head += f"--{boundary}\r\n".encode()
        head += (f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        total = len(head) + len(file_data) + len(tail)

        if self.scheme == "https":  # pragma: no cover - FRITZ!Box im LAN: http
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout or self.timeout,
                context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout or self.timeout)
        try:
            conn.putrequest("POST", path)
            conn.putheader("User-Agent", USER_AGENT)
            conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
            conn.putheader("Content-Length", str(total))
            for hk, hv in (headers or {}).items():
                conn.putheader(hk, hv)
            conn.endheaders()
            sent = 0
            conn.send(bytes(head))
            sent += len(head)
            chunk = 256 * 1024
            view = memoryview(file_data)
            for off in range(0, len(file_data), chunk):
                part = view[off:off + chunk]
                conn.send(part)
                sent += len(part)
                if progress:
                    progress(sent, total)
            conn.send(tail)
            sent += len(tail)
            if progress:
                progress(sent, total)
            resp = conn.getresponse()
            body = resp.read()
            return Response(resp.status, dict(resp.getheaders()), body, self.url(path))
        except (OSError, http.client.HTTPException) as e:
            raise HttpError(f"Upload fehlgeschlagen: {e}") from e
        finally:
            conn.close()


def fetch_url(url: str, timeout: float = 30.0, use_system_proxy: bool = True) -> Response:
    """Allgemeiner GET (z.B. AVM-Downloadserver, über System-Proxy)."""
    opener = build_opener(use_system_proxy)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return Response(resp.status, dict(resp.headers.items()), resp.read(), resp.geturl())
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code} bei {url}", e.code) from e
    except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
        raise HttpError(f"Download fehlgeschlagen ({url}): {getattr(e, 'reason', e)}") from e


def download_to_file(url: str, dest_path: str, timeout: float = 60.0,
                     progress: Optional[Callable[[int, int], None]] = None,
                     cancel: Optional[Callable[[], bool]] = None) -> None:
    opener = build_opener(True)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest_path + ".part"
    try:
        with opener.open(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                if cancel and cancel():
                    raise HttpError("Download abgebrochen")
                buf = resp.read(256 * 1024)
                if not buf:
                    break
                fh.write(buf)
                done += len(buf)
                if progress:
                    progress(done, total)
        import os
        os.replace(tmp, dest_path)
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code} bei {url}", e.code) from e
    except (urllib.error.URLError, socket.timeout, OSError, http.client.HTTPException) as e:
        raise HttpError(f"Download fehlgeschlagen ({url}): {getattr(e, 'reason', e)}") from e
    finally:
        import os
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
