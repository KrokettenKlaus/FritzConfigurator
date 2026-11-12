"""Einstellungen (settings.json neben dem Programm). Kennwörter werden nie gespeichert."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from typing import List

LANGUAGES = {"de": "Deutsch", "it": "Italiano", "en": "English"}

# Ländercode (ohne führende 0) -> Anzeigename. Die Box-ID wird zur Laufzeit aus der Länderliste ermittelt.
COUNTRIES = {
    "39": "Italien", "49": "Deutschland", "43": "Österreich", "41": "Schweiz", "99": "Andere Länder",
}

PROVIDERS = {
    "auto": "Automatisch (DSL: KONVERTO DSL, Glasfaser: KONVERTO, sonst KONVERTO WAN)",
    "raiffeisen": "KONVERTO DSL",
    "raiffeisen_fiber": "KONVERTO (Glasfaser)",
    "raiffeisen_lan1": "KONVERTO WAN (über LAN 1 / externes Modem)",
}

# Info-LED (box:settings/infoled_reason) - Texte wie in System > Tasten und LEDs > LED-Anzeigen (FRITZ!OS 8.50)
INFOLED_REASONS = {
    "1": "Freies Onlinevolumen / freie Onlinezeit aufgebraucht – LED blinkt",
    "4": "Internetverbindung besteht – LED leuchtet",
    "5": "Gerät über LAN angeschlossen – LED leuchtet",
    "7": "Gerät über USB angeschlossen – LED leuchtet",
    "8": "Klingelsperre aktiv – LED leuchtet",
    "11": "Neue Nachricht auf dem Anrufbeantworter – LED blinkt",
    "12": "DECT aktiv – LED leuchtet",
    "13": "Neue Nachricht auf dem Anrufbeantworter oder neues Fax – LED blinkt",
    "14": "Keine Auswahl – LED blinkt nur für fest eingestellte Ereignisse",
    "15": "WLAN-Gastzugang aktiv – LED leuchtet",
    "16": "Gerät am WLAN-Gastzugang angemeldet – LED leuchtet",
}
INFOLED_INTERNET = "4"

UI_THEMES = {"system": "System", "light": "Hell", "dark": "Dunkel"}

# 'fritz.box' bewusst nicht: der Name löst im Büronetz auf den dortigen Router auf.
DEFAULT_HOSTS = ["192.168.178.1", "169.254.1.1", "192.168.188.1"]
EXCLUDED_HOSTS = {"fritz.box"}


def app_dir() -> str:
    """Verzeichnis des Programms (bei PyInstaller-EXE: Ordner der EXE)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class JobConfig:
    # Verbindung
    host: str = ""                       # leer = automatisch suchen
    hosts: List[str] = field(default_factory=lambda: list(DEFAULT_HOSTS))
    username: str = ""                   # leer = automatisch (Benutzer aus login_sid.lua)
    password: str = ""                   # nie gespeichert
    tr064_port: int = 49000

    # Sprache / Land
    set_locale: bool = True
    language: str = "de"
    country: str = "39"

    # Internetanbieter
    set_provider: bool = True
    provider: str = "auto"
    custom_credentials: bool = False
    ppp_user: str = ""
    ppp_password: str = ""               # nie gespeichert

    # Heimnetz (optional)
    set_network: bool = False
    lan_ip: str = "192.168.178.1"
    lan_mask: str = "255.255.255.0"
    dhcp_start: str = "192.168.178.20"
    dhcp_end: str = "192.168.178.200"
    auto_renew_pc_ip: bool = True

    # Firmware (optional)
    update_firmware: bool = False
    firmware_online: bool = True
    firmware_file: str = ""              # manuell gewähltes Image (optional)

    # Info-LED
    set_infoled: bool = True
    infoled_reason: str = INFOLED_INTERNET   # "Internetverbindung besteht - LED leuchtet"

    # Sonstiges
    dry_run: bool = False
    ui_theme: str = "system"             # Oberfläche: system | light | dark

    SECRET_FIELDS = ("password", "ppp_password")

    def to_json(self) -> dict:
        d = asdict(self)
        for k in self.SECRET_FIELDS:
            d.pop(k, None)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "JobConfig":
        known = {f.name for f in fields(cls)}
        cfg = cls(**{k: v for k, v in d.items() if k in known and k not in cls.SECRET_FIELDS})
        # alte settings.json enthielt noch 'fritz.box' in der Suchliste
        cfg.hosts = [h for h in cfg.hosts if h.strip().lower() not in EXCLUDED_HOSTS] or list(DEFAULT_HOSTS)
        cfg.infoled_reason = str(cfg.infoled_reason)
        if cfg.infoled_reason not in INFOLED_REASONS:
            cfg.infoled_reason = INFOLED_INTERNET
        if cfg.ui_theme not in UI_THEMES:
            cfg.ui_theme = "system"
        return cfg


def settings_path() -> str:
    return os.path.join(app_dir(), "settings.json")


def load_settings() -> JobConfig:
    p = settings_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return JobConfig.from_json(json.load(fh))
    except (OSError, ValueError, TypeError):
        return JobConfig()


def save_settings(cfg: JobConfig) -> None:
    try:
        with open(settings_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg.to_json(), fh, indent=2, ensure_ascii=False)
    except OSError:
        pass


def icon_path() -> str:
    """Pfad zur FritzConfigurator.ico (neben dem Programm bzw. in der PyInstaller-EXE), sonst ''."""
    for base in (getattr(sys, "_MEIPASS", ""), app_dir()):
        p = os.path.join(base, "FritzConfigurator.ico") if base else ""
        if p and os.path.isfile(p):
            return p
    return ""


def firmware_dir() -> str:
    return os.path.join(app_dir(), "firmware")


def logs_dir() -> str:
    p = os.path.join(app_dir(), "logs")
    os.makedirs(p, exist_ok=True)
    return p
