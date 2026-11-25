"""Anmeldung an der FRITZ!Box-Weboberfläche und Aufruf von data.lua / query.lua.

Abgeleitet aus FRITZ!OS 8.50 (login_sid.lua, data.lua, newval.lua, twofactor.lua):

* Login: GET /login_sid.lua?version=2 -> Challenge; POST username + response.
  v2 = PBKDF2-SHA256 ("2$iter1$salt1$iter2$salt2"), Fallback v1 = MD5 (UTF-16LE).
* Seiten mit Speicher-Logik antworten auf "apply" mit JSON
  {"data": {"apply": "ok"|"confirm"|"valerror"|"saveerror"|"twofactor", ...}}
* "confirm"  -> erneut senden mit Feld "confirmed"
* "twofactor"-> Tastendruck/Telefon-Bestätigung abwarten (twofactor.lua, tfa_active),
                danach erneut senden mit "confirmed" und "twofactor".
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .httpclient import Fields, HttpClient, HttpError, Response

INVALID_SID = "0000000000000000"


class LoginError(Exception):
    pass


class ApplyError(Exception):
    def __init__(self, message: str, answer: Optional[dict] = None):
        super().__init__(message)
        self.answer = answer or {}


class TwoFactorCancelled(ApplyError):
    pass


@dataclass
class TwoFactorRequest:
    """Information für die Oberfläche: wie die Bestätigung erfolgen kann."""
    methods: List[str]          # z.B. ["button", "dtmf", "googleauth"]
    code: str = ""              # Telefoncode (bei dtmf: am Telefon *1<code> wählen)

    def describe(self) -> str:
        parts = []
        if "button" in self.methods:
            parts.append("eine beliebige Taste an der FRITZ!Box kurz drücken")
        if "dtmf" in self.methods and self.code:
            parts.append(f"an einem angeschlossenen Telefon *1{self.code} wählen")
        if "googleauth" in self.methods:
            parts.append("Code aus der Authenticator-App eingeben (im Browser)")
        if not parts:
            return "Bestätigung an der FRITZ!Box erforderlich"
        return " oder ".join(parts)


def calc_challenge_response(challenge: str, password: str) -> str:
    if challenge.startswith("2$"):
        _, iter1, salt1, iter2, salt2 = challenge.split("$")
        hash1 = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt1), int(iter1))
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iter2))
        return f"{salt2}${hash2.hex()}"
    # MD5-Verfahren (ältere FRITZ!OS): Zeichen > 255 werden durch '.' ersetzt
    pw = "".join(c if ord(c) <= 255 else "." for c in password)
    md5 = hashlib.md5(f"{challenge}-{pw}".encode("utf-16-le")).hexdigest()
    return f"{challenge}-{md5}"


@dataclass
class SessionInfo:
    sid: str
    challenge: str
    block_time: int
    users: List[str]
    last_user: Optional[str]
    rights: Dict[str, str] = field(default_factory=dict)


def parse_session_info(xml_text: str) -> SessionInfo:
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        raise LoginError(f"Unerwartete Antwort von login_sid.lua: {xml_text[:200]!r}") from e
    sid = (root.findtext("SID") or "").strip()
    challenge = (root.findtext("Challenge") or "").strip()
    try:
        block = int((root.findtext("BlockTime") or "0").strip() or 0)
    except ValueError:
        block = 0
    users, last = [], None
    users_el = root.find("Users")
    if users_el is not None:
        for u in users_el.findall("User"):
            name = (u.text or "").strip()
            if name:
                users.append(name)
                if u.get("last") == "1":
                    last = name
    rights = {}
    rights_el = root.find("Rights")
    if rights_el is not None:
        names = [n.text or "" for n in rights_el.findall("Name")]
        acc = [a.text or "" for a in rights_el.findall("Access")]
        rights = dict(zip(names, acc))
    return SessionInfo(sid, challenge, block, users, last, rights)


def parse_json_body(text: str) -> Optional[Any]:
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        pass
    # Manche Seiten geben vor dem JSON noch Whitespace/Kommentare aus
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


Logger = Callable[[str, str], None]  # (level, message)
TwoFactorHandler = Callable[[TwoFactorRequest, Callable[[], Optional[bool]]], bool]
"""Erhält die Anforderung und eine poll()-Funktion (None = noch offen, True = ok, False = abgelehnt).
Muss True zurückgeben, wenn erfolgreich bestätigt wurde, sonst False (Abbruch)."""


class FritzSession:
    def __init__(self, http: HttpClient, password: str, username: Optional[str] = None,
                 log: Optional[Logger] = None, twofactor_handler: Optional[TwoFactorHandler] = None,
                 lang: str = "de"):
        self.http = http
        self.password = password
        self.username = username
        self.sid: str = INVALID_SID
        self.lang = lang
        self.used_username: Optional[str] = None
        self.log: Logger = log or (lambda lvl, msg: None)
        self.twofactor_handler = twofactor_handler

    # ------------------------------------------------------------------ Login
    def session_info(self) -> SessionInfo:
        r = self.http.get("/login_sid.lua", params={"version": "2"})
        return parse_session_info(r.text)

    def login(self) -> str:
        info = self.session_info()
        if info.sid and info.sid != INVALID_SID:
            # Box ohne Kennwortschutz
            self.sid = info.sid
            return self.sid
        if info.block_time > 0:
            self.log("warn", f"Box blockiert Anmeldungen noch {info.block_time} s (falsches Kennwort zuvor) - warte ...")
            time.sleep(min(info.block_time, 120) + 1)
            info = self.session_info()
        username = self.username
        if not username:
            username = info.last_user or (info.users[0] if info.users else "")
        response = calc_challenge_response(info.challenge, self.password)
        r = self.http.post_form("/login_sid.lua", {"username": username, "response": response},
                                params={"version": "2"})
        res = parse_session_info(r.text)
        if not res.sid or res.sid == INVALID_SID:
            hint = f" (Benutzer '{username}')" if username else ""
            extra = f", Box sperrt jetzt für {res.block_time} s" if res.block_time else ""
            raise LoginError(f"Anmeldung fehlgeschlagen{hint} - Kennwort falsch?{extra}")
        self.sid = res.sid
        self.used_username = username
        return self.sid

    def logout(self) -> None:
        if self.sid and self.sid != INVALID_SID:
            try:
                self.http.post_form("/login_sid.lua", {"sid": self.sid, "logout": "1"},
                                    params={"version": "2"}, timeout=5)
            except HttpError:
                pass
        self.sid = INVALID_SID

    def is_valid(self) -> bool:
        if self.sid == INVALID_SID:
            return False
        try:
            r = self.http.get("/login_sid.lua", params={"version": "2", "sid": self.sid}, timeout=8)
            return parse_session_info(r.text).sid == self.sid
        except (HttpError, LoginError):
            return False

    def ensure(self) -> str:
        if not self.is_valid():
            self.login()
        return self.sid

    # ------------------------------------------------------------- Requests
    def _common(self, fields: Fields) -> List[Tuple[str, str]]:
        items = list(fields.items()) if isinstance(fields, dict) else list(fields)
        base = [("xhr", "1"), ("sid", self.sid), ("lang", self.lang)]
        keys = {k for k, _ in items}
        return [(k, v) for k, v in base if k not in keys] + [(k, "" if v is None else str(v)) for k, v in items]

    def data_lua(self, page: str, fields: Optional[Fields] = None, timeout: Optional[float] = None,
                 follow_redirects: bool = True) -> Response:
        items = [("page", page)] + (list(fields.items()) if isinstance(fields, dict) else list(fields or []))
        return self.http.post_form("/data.lua", self._common(items), timeout=timeout,
                                   follow_redirects=follow_redirects)

    def post_lua(self, path: str, fields: Fields, timeout: Optional[float] = None,
                 follow_redirects: bool = True) -> Response:
        return self.http.post_form(path, self._common(fields), timeout=timeout, follow_redirects=follow_redirects)

    def query(self, **webvars: str) -> Dict[str, Any]:
        """Liest Box-Variablen über query.lua (z.B. country="country:settings/current_country")."""
        params = [("sid", self.sid)] + list(webvars.items())
        r = self.http.get("/query.lua", params=params)
        data = parse_json_body(r.text)
        if not isinstance(data, dict):
            raise HttpError(f"query.lua lieferte keine gültigen Daten: {r.text[:200]!r}")
        return data

    def rest(self, method: str, path: str, body: Optional[Any] = None, timeout: Optional[float] = 30) -> Any:
        """REST-Schnittstelle der neuen Oberfläche (FRITZ!OS 8: /api/v0/..., wie js3/rest-helper.js)."""
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"AVM-SID {self.sid}", "Accept": "application/json",
                   "Client-Name": "WebGUI"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        r = self.http.request(method, "/api/v0/" + path.lstrip("/"), data=data, headers=headers, timeout=timeout)
        return parse_json_body(r.text)

    def page_available(self, page: str) -> bool:
        """Prüft, ob data.lua die Seite ausliefert (sonst kommt die Übersicht als JSON zurück)."""
        r = self.data_lua(page)
        data = parse_json_body(r.text) if r.text.lstrip().startswith("{") else None
        if isinstance(data, dict) and data.get("pid") and data.get("pid") != page:
            return False
        return r.status == 200

    def page_answer_pid(self, page: str) -> str:
        """Diagnose: Seiten-ID, mit der die Box auf eine data.lua-Anfrage antwortet."""
        try:
            r = self.data_lua(page)
        except HttpError as e:
            return f"Fehler {e}"
        data = parse_json_body(r.text) if r.text.lstrip().startswith("{") else None
        pid = data.get("pid") if isinstance(data, dict) else None
        return str(pid) if pid else f"HTTP {r.status}"

    # ------------------------------------------------------- apply-Workflow
    def apply(self, page: Optional[str], fields: Sequence[Tuple[str, str]], direct_path: Optional[str] = None,
              submit_name: str = "apply", max_rounds: int = 4) -> dict:
        """Sendet ein Formular wie die Weboberfläche (inkl. Bestätigung und 2FA)."""
        fields = list(fields)
        if not any(k == submit_name for k, _ in fields):
            fields.append((submit_name, ""))
        extra: List[Tuple[str, str]] = []
        for _ in range(max_rounds):
            payload = fields + extra
            if page:
                r = self.data_lua(page, payload, timeout=60)
            else:
                assert direct_path
                r = self.post_lua(direct_path, payload, timeout=60)
            obj = parse_json_body(r.text)
            if not isinstance(obj, dict):
                raise ApplyError(f"Unerwartete Antwort (keine JSON-Daten, HTTP {r.status}): {r.text[:300]!r}")
            data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
            state = data.get(submit_name)
            if state is None:
                pid = obj.get("pid")
                raise ApplyError(f"Seite '{page or direct_path}' hat das Formular nicht verarbeitet"
                                 + (f" (Antwort von Seite '{pid}')" if pid else ""), data)
            if state == "ok":
                return data
            if state == "confirm":
                self.log("info", "Box fragt nach Bestätigung - wird bestätigt (wie Klick auf OK): "
                         + _short(data.get("confirm")))
                if not any(k == "confirmed" for k, _ in extra):
                    extra.append(("confirmed", ""))
                continue
            if state == "twofactor":
                self._handle_twofactor(str(data.get("twofactor") or ""))
                for k in ("confirmed", "twofactor"):
                    if not any(e == k for e, _ in extra):
                        extra.append((k, ""))
                continue
            if state == "valerror":
                raise ApplyError("Box hat die Eingaben abgelehnt: " + _short(data.get("valerror")), data)
            if state == "saveerror":
                raise ApplyError("Box konnte nicht speichern: " + _short(data.get("saveerror")), data)
            raise ApplyError(f"Unbekannte Antwort '{state}'", data)
        raise ApplyError("Zu viele Bestätigungsrunden - abgebrochen")

    def _handle_twofactor(self, state: str) -> None:
        methods_part, _, code = state.partition(";")
        methods = [m for m in methods_part.split(",") if m]
        if "starterror" in methods:
            msg = {"91": "eine andere Sitzung wartet bereits auf eine Bestätigung",
                   "92": "zu viele Versuche - bitte später erneut"}.get(code, f"Fehler {code}")
            raise TwoFactorCancelled(f"Zusätzliche Bestätigung konnte nicht gestartet werden: {msg}")
        req = TwoFactorRequest(methods, code)
        self.log("warn", "Die FRITZ!Box verlangt eine zusätzliche Bestätigung: " + req.describe())

        def poll() -> Optional[bool]:
            r = self.http.post_form("/twofactor.lua", {"sid": self.sid, "tfa_active": ""}, timeout=10)
            d = parse_json_body(r.text) or {}
            if d.get("done"):
                return bool(d.get("active"))
            return None

        handler = self.twofactor_handler or _console_twofactor
        ok = handler(req, poll)
        if not ok:
            try:
                self.http.post_form("/twofactor.lua", {"sid": self.sid, "tfa_cancel": ""}, timeout=10)
            except HttpError:
                pass
            raise TwoFactorCancelled("Zusätzliche Bestätigung wurde nicht erteilt")
        self.log("ok", "Bestätigung an der Box erhalten")


def _console_twofactor(req: TwoFactorRequest, poll: Callable[[], Optional[bool]]) -> bool:
    print("\n>>> Bitte jetzt " + req.describe() + " (max. 3 Minuten) ...", flush=True)
    end = time.time() + 180
    while time.time() < end:
        res = poll()
        if res is not None:
            return res
        time.sleep(1)
    return False


def _short(obj: Any, limit: int = 300) -> str:
    if obj is None:
        return ""
    if isinstance(obj, (dict, list)):
        s = json.dumps(obj, ensure_ascii=False)
    else:
        s = str(obj)
    s = re.sub(r"\s+", " ", s)
    return s if len(s) <= limit else s[:limit] + " ..."
