"""Grafische Oberfläche (Tkinter, in Python für Windows enthalten)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Optional

from . import APP_NAME, __version__
from .boxinfo import detect
from .config import (COUNTRIES, DEFAULT_HOSTS, INFOLED_REASONS, LANGUAGES, PROVIDERS, UI_THEMES, JobConfig,
                     firmware_dir, icon_path, load_settings, logs_dir, save_settings)
from .firmware import FirmwareRepo
from .httpclient import HttpError
from .netutil import suggest_pool, validate_lan_settings
from .runner import STEPS, Callbacks, Runner
from .session import TwoFactorRequest

ICONS = {"off": "–", "pending": "○", "running": "▶", "ok": "✔", "skip": "↷", "warn": "⚠", "error": "✖"}
_WIN = sys.platform == "win32"

# Farben je Darstellung. Hell = bisheriges Aussehen (Windows-Theme "vista"), Dunkel = "clam" umgefärbt.
PALETTES: Dict[str, dict] = {
    "light": {
        "dark": False,
        "fg": "#202124", "hint": "#5f6368", "bad": "#d93025", "good": "#188038",
        "field": "SystemWindow" if _WIN else "#ffffff", "field_fg": "SystemWindowText" if _WIN else "#000000",
        "select": "SystemHighlight" if _WIN else "#cfe0fc", "select_fg": "SystemHighlightText" if _WIN else "#202124",
        "text_bg": "#fafafa", "text_fg": "#202124", "text_select": "#cfe0fc",
        "steps": {"off": "#9aa0a6", "pending": "#5f6368", "running": "#1a73e8", "ok": "#188038", "skip": "#5f6368",
                  "warn": "#b06000", "error": "#d93025"},
        "log": {"ok": "#188038", "error": "#d93025", "warn": "#b06000", "step": "#1a73e8", "info": "#202124"},
        "banner": {"info": ("#e8f0fe", "#174ea6"), "ok": ("#e6f4ea", "#137333"), "warn": ("#fef7e0", "#b06000"),
                   "error": ("#fce8e6", "#c5221f")},
    },
    "dark": {
        "dark": True,
        "bg": "#202124", "fg": "#e8eaed", "hint": "#9aa0a6", "bad": "#f28b82", "good": "#81c995",
        "field": "#2d2e31", "field_fg": "#e8eaed", "border": "#4a4d51", "disabled": "#6f7378",
        "button": "#35363a", "button_active": "#43454a", "button_pressed": "#505357",
        "accent": "#8ab4f8", "accent_active": "#aecbfa", "on_accent": "#202124",
        "select": "#3b4a63", "select_fg": "#e8eaed",
        "text_bg": "#18191b", "text_fg": "#e8eaed", "text_select": "#3b4a63",
        "steps": {"off": "#6f7378", "pending": "#9aa0a6", "running": "#8ab4f8", "ok": "#81c995", "skip": "#9aa0a6",
                  "warn": "#fdd663", "error": "#f28b82"},
        "log": {"ok": "#81c995", "error": "#f28b82", "warn": "#fdd663", "step": "#8ab4f8", "info": "#e8eaed"},
        "banner": {"info": ("#1f2b3e", "#aecbfa"), "ok": ("#1c3326", "#81c995"), "warn": ("#3a3220", "#fdd663"),
                   "error": ("#3c2220", "#f28b82")},
    },
}


def windows_prefers_dark() -> bool:
    """True, wenn Windows auf 'Dunkel' für Apps eingestellt ist."""
    if not _WIN:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except (OSError, ImportError):
        return False


def set_titlebar_dark(win: tk.Misc, dark: bool) -> None:
    """Dunkle Fenster-Titelleiste (Windows 10 20H1+/11), sonst ohne Wirkung."""
    if not _WIN:
        return
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        if not hwnd:
            return
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (neu / vor 20H1)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
        # Rahmen neu zeichnen: SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except Exception:
        pass


def open_path(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as e:
        messagebox.showerror(APP_NAME, f"Kann {path} nicht öffnen: {e}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} {__version__} – FRITZ!Box Vorkonfiguration")
        self.minsize(1000, 740)
        self.geometry("1120x800")
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.cancel = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.tfa_win: Optional[tk.Toplevel] = None
        self.tfa_cancel: Optional[threading.Event] = None
        self.cfg = load_settings()
        self.theme_mode = self.cfg.ui_theme
        self.theme_hooks: list = []          # Callbacks für Zusatzfenster (Farbwechsel)
        self.step_state: Dict[str, str] = {}
        self.banner_kind = "info"
        self._sys_dark = windows_prefers_dark()
        ico = icon_path()
        if ico:
            try:
                self.iconbitmap(default=ico)  # gilt auch für alle Dialoge
            except tk.TclError:
                pass
        self._style()
        self._apply_theme()   # vor _build: Widgets werden mit der Palette erzeugt
        self._build()
        self._load_cfg(self.cfg)
        self.after(100, self._drain)
        self.after(3000, self._watch_system_theme)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI
    def _style(self):
        base = ("Segoe UI", 10) if _WIN else ("DejaVu Sans", 10)
        self.option_add("*Font", base)
        # ttk-Labels sollen die Schrift aus ihrem Stil nehmen (Überschrift 15 pt, Hinweise 9 pt),
        # sonst überschreibt "*Font" die Stil-Schrift
        self.option_add("*TLabel.Font", "")
        self.base_font = base
        self._light_theme = next((t for t in ("vista", "alt", "default") if t in ttk.Style(self).theme_names()), "default")

    def _is_dark(self) -> bool:
        return self.theme_mode == "dark" or (self.theme_mode == "system" and self._sys_dark)

    def _apply_theme(self):
        """Hell/Dunkel anwenden (auch zur Laufzeit)."""
        pal = PALETTES["dark" if self._is_dark() else "light"]
        self.pal = pal
        st = ttk.Style(self)
        base = self.base_font
        if pal["dark"]:
            st.theme_use("clam")
            self._style_dark(st, pal)
        else:
            st.theme_use(self._light_theme)
        bg = st.lookup(".", "background") or "#f0f0f0"
        pal = dict(pal, bg=pal.get("bg", bg))
        self.pal = pal
        st.configure("TLabel", font=base)
        st.configure("Head.TLabel", font=(base[0], 15, "bold"))
        st.configure("Sub.TLabel", foreground=pal["hint"])
        st.configure("Sec.TLabelframe.Label", font=(base[0], 10, "bold"))
        st.configure("Big.TButton", font=(base[0], 12, "bold"), padding=(18, 8))
        st.configure("Hint.TLabel", foreground=pal["hint"], font=(base[0], 9))
        st.configure("Bad.TLabel", foreground=pal["bad"], font=(base[0], 9))
        st.configure("Good.TLabel", foreground=pal["good"], font=(base[0], 9))
        # Auswahlliste der Comboboxen (Popdown) - neue und bereits erzeugte
        for opt, key in (("background", "field"), ("foreground", "field_fg"),
                         ("selectBackground", "select"), ("selectForeground", "select_fg")):
            self.option_add(f"*TCombobox*Listbox.{opt}", pal[key])
        for cb in getattr(self, "comboboxes", []):
            try:
                pd = self.tk.call("ttk::combobox::PopdownWindow", str(cb))
                self.tk.call(f"{pd}.f.l", "configure", "-background", pal["field"], "-foreground", pal["field_fg"],
                             "-selectbackground", pal["select"], "-selectforeground", pal["select_fg"])
            except tk.TclError:
                pass
        self.configure(bg=pal["bg"])
        if hasattr(self, "txt"):
            self.txt.configure(bg=pal["text_bg"], fg=pal["text_fg"], insertbackground=pal["text_fg"],
                               selectbackground=pal["text_select"], selectforeground=pal["text_fg"])
            for k, c in pal["log"].items():
                self.txt.tag_configure(k, foreground=c)
            for sid, (icon, _) in self.step_widgets.items():
                icon.configure(bg=pal["bg"], fg=pal["steps"].get(self.step_state.get(sid, "pending"), pal["fg"]))
            self._banner(self.banner.cget("text"), self.banner_kind)
        for hook in list(self.theme_hooks):
            try:
                hook(pal)
            except tk.TclError:
                self.theme_hooks.remove(hook)  # Fenster bereits geschlossen
        self._titlebar(self)

    def _titlebar(self, win):
        dark = self.pal["dark"]
        set_titlebar_dark(win, dark)
        win.after(60, lambda: set_titlebar_dark(win, dark))  # erst nach dem Anzeigen sicher wirksam

    @staticmethod
    def _style_dark(st: ttk.Style, p: dict):
        bg, fg, field, border, dis = p["bg"], p["fg"], p["field"], p["border"], p["disabled"]
        btn, act, prs, acc = p["button"], p["button_active"], p["button_pressed"], p["accent"]
        st.configure(".", background=bg, foreground=fg, fieldbackground=field, bordercolor=border,
                     lightcolor=bg, darkcolor=bg, troughcolor=field, selectbackground=p["select"],
                     selectforeground=p["select_fg"], insertcolor=fg, arrowcolor=fg, focuscolor=acc)
        st.map(".", foreground=[("disabled", dis)], background=[("disabled", bg)])
        # clam ist von Haus aus höher als vista -> Abstände wie im hellen Modus
        st.configure("TButton", background=btn, foreground=fg, bordercolor=border, lightcolor=btn, darkcolor=btn,
                     focuscolor=fg, padding=(8, 1))
        st.map("TButton", background=[("disabled", bg), ("pressed", prs), ("active", act)],
               lightcolor=[("disabled", bg), ("pressed", prs), ("active", act)],
               darkcolor=[("disabled", bg), ("pressed", prs), ("active", act)],
               foreground=[("disabled", dis)], bordercolor=[("focus", acc)])
        st.configure("Big.TButton", background=acc, foreground=p["on_accent"], bordercolor=acc, lightcolor=acc,
                     darkcolor=acc, focuscolor=p["on_accent"])
        st.map("Big.TButton", background=[("disabled", btn), ("pressed", p["accent_active"]), ("active", p["accent_active"])],
               lightcolor=[("disabled", btn), ("pressed", p["accent_active"]), ("active", p["accent_active"])],
               darkcolor=[("disabled", btn), ("pressed", p["accent_active"]), ("active", p["accent_active"])],
               foreground=[("disabled", dis)], bordercolor=[("disabled", border)])
        st.configure("TEntry", fieldbackground=field, foreground=fg, insertcolor=fg, bordercolor=border,
                     lightcolor=field, darkcolor=field, padding=(3, 1))
        st.map("TEntry", fieldbackground=[("disabled", bg)], foreground=[("disabled", dis)],
               bordercolor=[("focus", acc)], lightcolor=[("focus", field)])
        st.configure("TCombobox", fieldbackground=field, background=btn, foreground=fg, arrowcolor=fg,
                     bordercolor=border, lightcolor=field, darkcolor=field, selectbackground=field,
                     selectforeground=fg, padding=(3, 1))
        st.map("TCombobox", fieldbackground=[("disabled", bg), ("readonly", field)],
               foreground=[("disabled", dis), ("readonly", fg)], background=[("disabled", bg), ("active", act)],
               selectbackground=[("readonly", field)], selectforeground=[("readonly", fg)],
               arrowcolor=[("disabled", dis)], bordercolor=[("focus", acc)])
        st.configure("TCheckbutton", background=bg, foreground=fg, indicatorbackground=field,
                     indicatorforeground=fg, upperbordercolor=border, lowerbordercolor=border, padding=(1, 1))
        st.map("TCheckbutton", background=[("active", bg)], foreground=[("disabled", dis)],
               indicatorbackground=[("disabled", bg), ("pressed", act)], indicatorforeground=[("disabled", dis)])
        st.configure("TLabelframe", background=bg, bordercolor=border, lightcolor=bg, darkcolor=bg)
        st.configure("TLabelframe.Label", background=bg, foreground=fg)
        st.configure("TSeparator", background=border)
        st.configure("TScrollbar", background=btn, troughcolor=bg, bordercolor=bg, arrowcolor=fg,
                     lightcolor=btn, darkcolor=btn)
        st.map("TScrollbar", background=[("active", act), ("pressed", prs)])
        st.configure("Horizontal.TProgressbar", background=acc, troughcolor=field, bordercolor=border,
                     lightcolor=acc, darkcolor=acc)

    def _set_theme_mode(self, mode: str):
        if mode not in UI_THEMES or mode == self.theme_mode:
            return
        self.theme_mode = mode
        self._sys_dark = windows_prefers_dark()
        self._apply_theme()

    def _watch_system_theme(self):
        """Bei 'System' einem Wechsel der Windows-Einstellung folgen."""
        if self.theme_mode == "system":
            now = windows_prefers_dark()
            if now != self._sys_dark:
                self._sys_dark = now
                self._apply_theme()
        self.after(3000, self._watch_system_theme)

    def themed_toplevel(self, win: tk.Toplevel, extra=None):
        """Zusatzfenster an die Darstellung anbinden (auch bei späterem Wechsel)."""
        def hook(pal):
            win.configure(bg=pal["bg"])
            if extra:
                extra(pal)
            self._titlebar(win)
        hook(self.pal)
        self.theme_hooks.append(hook)

    def _build(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=0, minsize=430)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(head, text="FRITZ!Box Vorkonfiguration", style="Head.TLabel").pack(side="left")
        ttk.Label(head, text="  Sprache/Land · KONVERTO · Heimnetz · Firmware · Info-LED",
                  style="Sub.TLabel").pack(side="left", pady=(6, 0))
        self.v_theme = tk.StringVar(value=UI_THEMES.get(self.theme_mode, UI_THEMES["system"]))
        self.c_theme = ttk.Combobox(head, textvariable=self.v_theme, state="readonly", width=8,
                                    values=list(UI_THEMES.values()))
        self.c_theme.pack(side="right")
        self.c_theme.bind("<<ComboboxSelected>>", lambda e: self._set_theme_mode(
            next(k for k, v in UI_THEMES.items() if v == self.v_theme.get())))
        ttk.Label(head, text="Darstellung:", style="Sub.TLabel").pack(side="right", padx=(0, 6))

        left = ttk.Frame(outer)
        left.grid(row=1, column=0, sticky="new", padx=(0, 12))
        right = ttk.Frame(outer)
        right.grid(row=1, column=1, sticky="nsew")
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._build_left(left)
        self._build_right(right)
        self._build_bottom(bottom)

    def _row(self, parent, r, label, widget, hint: str = ""):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=2)
        widget.grid(row=r, column=1, sticky="ew", pady=2)
        if hint:
            ttk.Label(parent, text=hint, style="Hint.TLabel").grid(row=r, column=2, sticky="w", padx=(6, 0))

    def _build_left(self, f):
        f.columnconfigure(0, weight=1)
        # --- Box
        box = ttk.LabelFrame(f, text="FRITZ!Box", padding=(8, 4), style="Sec.TLabelframe")
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)
        self.v_pw = tk.StringVar()
        pwf = ttk.Frame(box)
        self.e_pw = ttk.Entry(pwf, textvariable=self.v_pw, show="•", width=24)
        self.e_pw.pack(side="left", fill="x", expand=True)
        self.v_showpw = tk.BooleanVar(value=False)
        ttk.Checkbutton(pwf, text="anzeigen", variable=self.v_showpw,
                        command=lambda: self.e_pw.configure(show="" if self.v_showpw.get() else "•")).pack(side="left", padx=4)
        self._row(box, 0, "Kennwort (Aufkleber):", pwf)
        self.v_host = tk.StringVar()
        hf = ttk.Frame(box)
        ttk.Entry(hf, textvariable=self.v_host, width=18).pack(side="left", fill="x", expand=True)
        self.b_detect = ttk.Button(hf, text="Box suchen", command=self._detect)
        self.b_detect.pack(side="left", padx=(4, 0))
        self._row(box, 1, "Adresse:", hf)
        self.l_box = ttk.Label(box, text="Adresse leer = automatisch suchen (" + ", ".join(DEFAULT_HOSTS[:3]) + " …)",
                               style="Hint.TLabel", wraplength=400)
        self.l_box.grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # --- Sprache/Land
        loc = ttk.LabelFrame(f, text="1. Sprache & Land", padding=(8, 4), style="Sec.TLabelframe")
        loc.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        loc.columnconfigure(1, weight=1)
        self.v_locale = tk.BooleanVar()
        ttk.Checkbutton(loc, text="einstellen (Box startet danach neu)", variable=self.v_locale,
                        command=self._update_states).grid(row=0, column=0, columnspan=2, sticky="w")
        lr = ttk.Frame(loc)
        self.v_lang = tk.StringVar()
        self.c_lang = ttk.Combobox(lr, textvariable=self.v_lang, state="readonly", width=14,
                                   values=[f"{v} ({k})" for k, v in LANGUAGES.items()])
        self.c_lang.pack(side="left")
        ttk.Label(lr, text="  Land:").pack(side="left")
        self.v_country = tk.StringVar()
        self.c_country = ttk.Combobox(lr, textvariable=self.v_country, state="readonly", width=18,
                                      values=[f"{v} (+{k})" for k, v in COUNTRIES.items()])
        self.c_country.pack(side="left", fill="x", expand=True)
        self._row(loc, 1, "Sprache:", lr)

        # --- Provider
        prov = ttk.LabelFrame(f, text="2. Internetanbieter", padding=(8, 4), style="Sec.TLabelframe")
        prov.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        prov.columnconfigure(1, weight=1)
        self.v_prov_on = tk.BooleanVar()
        ttk.Checkbutton(prov, text="KONVERTO einrichten", variable=self.v_prov_on,
                        command=self._update_states).grid(row=0, column=0, columnspan=2, sticky="w")
        self.v_prov = tk.StringVar()
        self.c_prov = ttk.Combobox(prov, textvariable=self.v_prov, state="readonly", width=40,
                                   values=list(PROVIDERS.values()))
        self._row(prov, 1, "Profil:", self.c_prov)
        self.v_owncred = tk.BooleanVar()
        ttk.Checkbutton(prov, text="eigene Zugangsdaten statt Dummy-Daten (nome@xdsl.rol)", variable=self.v_owncred,
                        command=self._update_states).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.v_pppuser = tk.StringVar()
        self.v_ppppw = tk.StringVar()
        self.cred_row = ttk.Frame(prov)
        ttk.Label(self.cred_row, text="Benutzer:").pack(side="left")
        self.e_pppuser = ttk.Entry(self.cred_row, textvariable=self.v_pppuser, width=20)
        self.e_pppuser.pack(side="left", fill="x", expand=True, padx=(4, 8))
        ttk.Label(self.cred_row, text="Kennwort:").pack(side="left")
        self.e_ppppw = ttk.Entry(self.cred_row, textvariable=self.v_ppppw, show="•", width=14)
        self.e_ppppw.pack(side="left", padx=(4, 0))
        self.cred_row.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(2, 0))

        # --- Netz
        net = ttk.LabelFrame(f, text="3. Heimnetz (optional)", padding=(8, 4), style="Sec.TLabelframe")
        net.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        net.columnconfigure(1, weight=1)
        self.v_net = tk.BooleanVar()
        ttk.Checkbutton(net, text="Gateway / DHCP-Pool abweichend vom Standard setzen", variable=self.v_net,
                        command=self._update_states).grid(row=0, column=0, columnspan=2, sticky="w")
        self.v_ip, self.v_mask, self.v_s, self.v_e = (tk.StringVar() for _ in range(4))
        self.net_entries = []
        r1, r2 = ttk.Frame(net), ttk.Frame(net)
        for frame, a, b in ((r1, self.v_ip, self.v_mask), (r2, self.v_s, self.v_e)):
            e1 = ttk.Entry(frame, textvariable=a, width=15)
            e1.pack(side="left")
            ttk.Label(frame, text=" / " if frame is r1 else " – ").pack(side="left")
            e2 = ttk.Entry(frame, textvariable=b, width=15)
            e2.pack(side="left")
            self.net_entries += [e1, e2]
        self.b_pool = ttk.Button(r2, text="Vorschlag", command=self._suggest_pool, width=9)
        self.b_pool.pack(side="left", padx=(6, 0))
        for var in (self.v_ip, self.v_mask, self.v_s, self.v_e):
            var.trace_add("write", lambda *a: self._validate_net())
        self._row(net, 1, "Box-IP / Maske:", r1)
        self._row(net, 2, "DHCP-Pool:", r2)
        self.l_net = ttk.Label(net, text="", style="Hint.TLabel", wraplength=400)
        self.l_net.grid(row=3, column=0, columnspan=2, sticky="w")

        # --- Firmware
        fw = ttk.LabelFrame(f, text="4. Firmware (optional, zum Schluss)", padding=(8, 4), style="Sec.TLabelframe")
        fw.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        fw.columnconfigure(0, weight=1)
        self.v_fw = tk.BooleanVar()
        ttk.Checkbutton(fw, text="neueste FRITZ!OS-Version aufspielen", variable=self.v_fw,
                        command=self._update_states).grid(row=0, column=0, columnspan=3, sticky="w")
        self.v_fw_online = tk.BooleanVar()
        self.cb_fw_online = ttk.Checkbutton(fw, text="online bei AVM prüfen/laden (sonst nur Ordner 'firmware')",
                                            variable=self.v_fw_online)
        self.cb_fw_online.grid(row=1, column=0, columnspan=3, sticky="w")
        bf = ttk.Frame(fw)
        bf.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(bf, text="Firmware-Cache aktualisieren …", command=self._prefetch_dialog).pack(side="left")
        ttk.Button(bf, text="Ordner öffnen", command=lambda: (os.makedirs(firmware_dir(), exist_ok=True),
                                                              open_path(firmware_dir()))).pack(side="left", padx=4)
        ttk.Button(bf, text="Image wählen …", command=self._choose_image).pack(side="left")
        self.v_fwfile = tk.StringVar()
        self.l_fwfile = ttk.Label(fw, textvariable=self.v_fwfile, style="Hint.TLabel", wraplength=400)
        self.l_fwfile.grid(row=3, column=0, columnspan=3, sticky="w")
        self.v_fwfile.trace_add("write", lambda *a: self.l_fwfile.grid() if self.v_fwfile.get() else self.l_fwfile.grid_remove())

        # --- Info-LED
        led = ttk.LabelFrame(f, text="5. Info-LED", padding=(8, 4), style="Sec.TLabelframe")
        led.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        led.columnconfigure(0, weight=1)
        self.v_led = tk.BooleanVar()
        reason = INFOLED_REASONS.get(self.cfg.infoled_reason, self.cfg.infoled_reason)
        ttk.Checkbutton(led, text=f"„{reason}“", variable=self.v_led).grid(row=0, column=0, sticky="w")
        ttk.Label(led, text="wie System › Tasten und LEDs › LED-Anzeigen › Info-LED",
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w")

        self.comboboxes = [self.c_lang, self.c_country, self.c_prov, self.c_theme]

    def _build_bottom(self, bot):
        ttk.Separator(bot).pack(fill="x", pady=(0, 8))
        self.v_dry = tk.BooleanVar()
        ttk.Checkbutton(bot, text="Testmodus (nur prüfen, nichts ändern)", variable=self.v_dry).pack(side="left")
        self.b_stop = ttk.Button(bot, text="Abbrechen", command=self._stop, state="disabled")
        self.b_stop.pack(side="right", padx=(6, 0))
        self.b_start = ttk.Button(bot, text="▶  Start", style="Big.TButton", command=self._start, width=18)
        self.b_start.pack(side="right")
        self.bind("<Return>", lambda e: self._start() if self.worker is None else None)

    def _build_right(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)
        self.banner = tk.Label(f, text="Bereit – Box per LAN anschließen, Kennwort eingeben, Start",
                               bg=self.pal["banner"]["info"][0], fg=self.pal["banner"]["info"][1], anchor="w", justify="left", padx=10, pady=8,
                               font=(self.base_font[0], 11, "bold"))
        self.banner.bind("<Configure>", lambda e: self.banner.configure(wraplength=max(200, e.width - 24)))
        self.banner.grid(row=0, column=0, sticky="ew")
        steps = ttk.Frame(f, padding=(0, 8))
        steps.grid(row=1, column=0, sticky="ew")
        steps.columnconfigure(1, weight=1)
        self.step_widgets: Dict[str, tuple] = {}
        for i, (sid, title) in enumerate(STEPS):
            icon = tk.Label(steps, text=ICONS["pending"], fg=self.pal["steps"]["pending"], bg=self.pal["bg"], width=2,
                            font=(self.base_font[0], 13, "bold"))
            icon.grid(row=2 * i, column=0, rowspan=2, sticky="nw")
            ttk.Label(steps, text=title, font=(self.base_font[0], 10, "bold")).grid(row=2 * i, column=1, sticky="w")
            det = ttk.Label(steps, text="", style="Hint.TLabel")
            det.grid(row=2 * i + 1, column=1, sticky="w", pady=(0, 3))
            self.step_widgets[sid] = (icon, det)
        steps.bind("<Configure>", lambda e: [d.configure(wraplength=max(200, e.width - 40))
                                             for _, d in self.step_widgets.values()])
        pf = ttk.Frame(f)
        pf.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        pf.columnconfigure(0, weight=1)
        self.pbar = ttk.Progressbar(pf, mode="determinate", maximum=1000)
        self.pbar.grid(row=0, column=0, sticky="ew")
        self.l_prog = ttk.Label(pf, text="", style="Hint.TLabel")
        self.l_prog.grid(row=1, column=0, sticky="w")
        lf = ttk.Frame(f)
        lf.grid(row=3, column=0, sticky="nsew")
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        self.txt = tk.Text(lf, wrap="word", state="disabled", relief="flat", bg=self.pal["text_bg"], padx=8, pady=6,
                           fg=self.pal["text_fg"], highlightthickness=0, borderwidth=0,
                           font=("Consolas", 9) if sys.platform == "win32" else ("DejaVu Sans Mono", 9))
        sb = ttk.Scrollbar(lf, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        for k, c in self.pal["log"].items():
            self.txt.tag_configure(k, foreground=c)
        self.txt.tag_configure("step", font=(self.base_font[0], 9, "bold"))
        bb = ttk.Frame(f)
        bb.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(bb, text="Log-Ordner", command=lambda: open_path(logs_dir())).pack(side="left")
        ttk.Button(bb, text="Protokoll (CSV)", command=self._open_csv).pack(side="left", padx=4)
        ttk.Button(bb, text="Log leeren", command=self._clear_log).pack(side="left")

    # ------------------------------------------------------------ Werte
    def _load_cfg(self, c: JobConfig):
        self.v_host.set(c.host)
        self.v_locale.set(c.set_locale)
        self.v_lang.set(f"{LANGUAGES.get(c.language, c.language)} ({c.language})")
        self.v_country.set(f"{COUNTRIES.get(c.country, c.country)} (+{c.country})")
        self.v_prov_on.set(c.set_provider)
        self.v_prov.set(PROVIDERS.get(c.provider, PROVIDERS["auto"]))
        self.v_owncred.set(c.custom_credentials)
        self.v_pppuser.set(c.ppp_user)
        self.v_net.set(c.set_network)
        self.v_ip.set(c.lan_ip)
        self.v_mask.set(c.lan_mask)
        self.v_s.set(c.dhcp_start)
        self.v_e.set(c.dhcp_end)
        self.v_fw.set(c.update_firmware)
        self.v_fw_online.set(c.firmware_online)
        self.v_fwfile.set(c.firmware_file)
        if not c.firmware_file:
            self.l_fwfile.grid_remove()
        self.v_led.set(c.set_infoled)
        self.v_dry.set(c.dry_run)
        self._update_states()
        self.e_pw.focus_set()

    def _collect(self) -> JobConfig:
        c = JobConfig()
        c.hosts = self.cfg.hosts
        c.tr064_port = self.cfg.tr064_port
        c.username = self.cfg.username
        c.auto_renew_pc_ip = self.cfg.auto_renew_pc_ip
        c.host = self.v_host.get().strip()
        c.password = self.v_pw.get()
        c.set_locale = self.v_locale.get()
        c.language = self.v_lang.get().rsplit("(", 1)[-1].rstrip(")")
        c.country = self.v_country.get().rsplit("+", 1)[-1].rstrip(")")
        c.set_provider = self.v_prov_on.get()
        c.provider = next((k for k, v in PROVIDERS.items() if v == self.v_prov.get()), "auto")
        c.custom_credentials = self.v_owncred.get()
        c.ppp_user = self.v_pppuser.get().strip()
        c.ppp_password = self.v_ppppw.get()
        c.set_network = self.v_net.get()
        c.lan_ip, c.lan_mask = self.v_ip.get().strip(), self.v_mask.get().strip()
        c.dhcp_start, c.dhcp_end = self.v_s.get().strip(), self.v_e.get().strip()
        c.update_firmware = self.v_fw.get()
        c.firmware_online = self.v_fw_online.get()
        c.firmware_file = self.v_fwfile.get().strip()
        c.set_infoled = self.v_led.get()
        c.infoled_reason = self.cfg.infoled_reason
        c.dry_run = self.v_dry.get()
        c.ui_theme = self.theme_mode
        return c

    def _update_states(self):
        def st(widget, on):
            widget.configure(state=("readonly" if isinstance(widget, ttk.Combobox) else "normal") if on else "disabled")
        st(self.c_lang, self.v_locale.get())
        st(self.c_country, self.v_locale.get())
        st(self.c_prov, self.v_prov_on.get())
        own = self.v_prov_on.get() and self.v_owncred.get()
        st(self.e_pppuser, own)
        st(self.e_ppppw, own)
        if own:
            self.cred_row.grid()
        else:
            self.cred_row.grid_remove()
        for e in self.net_entries:
            st(e, self.v_net.get())
        st(self.b_pool, self.v_net.get())
        st(self.cb_fw_online, self.v_fw.get())
        self._validate_net()

    def _validate_net(self) -> bool:
        if not self.v_net.get():
            self.l_net.configure(text="Standard der Box bleibt (192.168.178.1, DHCP .20 – .200)", style="Hint.TLabel")
            return True
        try:
            ip, mask, s, e = validate_lan_settings(self.v_ip.get(), self.v_mask.get(), self.v_s.get(), self.v_e.get())
            self.l_net.configure(text=f"✔ Box {ip}, Clients erhalten {s} – {e}", style="Good.TLabel")
            return True
        except ValueError as ex:
            self.l_net.configure(text=f"✖ {ex}", style="Bad.TLabel")
            return False

    def _suggest_pool(self):
        try:
            s, e = suggest_pool(self.v_ip.get(), self.v_mask.get() or "255.255.255.0")
            self.v_s.set(s)
            self.v_e.set(e)
        except ValueError:
            messagebox.showwarning(APP_NAME, "Bitte zuerst eine gültige Box-IP und Subnetzmaske eingeben.")

    def _choose_image(self):
        p = filedialog.askopenfilename(title="FRITZ!OS-Image wählen", initialdir=firmware_dir() if os.path.isdir(firmware_dir()) else None,
                                       filetypes=[("FRITZ!OS-Image", "*.image"), ("Alle Dateien", "*.*")])
        self.v_fwfile.set(p or "")
        if p:
            self.v_fw.set(True)
            self._update_states()

    # ------------------------------------------------------------ Aktionen
    def _detect(self):
        host = self.v_host.get().strip()
        cands = [host] if host else list(self.cfg.hosts)
        self.l_box.configure(text="Suche …")
        self.b_detect.configure(state="disabled")

        def work():
            try:
                h, info = detect(cands)
                self.q.put(("boxinfo", f"✔ {info.summary()}  @ {h}"))
            except HttpError as e:
                self.q.put(("boxinfo", f"✖ {e}"))
            self.q.put(("detect_done",))
        threading.Thread(target=work, daemon=True).start()

    def _start(self):
        if self.worker is not None:
            return
        cfg = self._collect()
        if not cfg.password:
            messagebox.showwarning(APP_NAME, "Bitte das FRITZ!Box-Kennwort vom Aufkleber (Geräteunterseite) eingeben.")
            self.e_pw.focus_set()
            return
        if cfg.set_network and not self._validate_net():
            messagebox.showwarning(APP_NAME, "Heimnetz-Einstellungen sind ungültig:\n" + self.l_net.cget("text"))
            return
        if cfg.custom_credentials and cfg.set_provider and not cfg.ppp_user:
            messagebox.showwarning(APP_NAME, "Bitte Benutzername für die eigenen Zugangsdaten eingeben.")
            return
        if not any((cfg.set_locale, cfg.set_provider, cfg.set_network, cfg.update_firmware, cfg.set_infoled)):
            messagebox.showinfo(APP_NAME, "Es ist kein Schritt ausgewählt.")
            return
        save_settings(cfg)
        self.cfg = cfg
        self.cancel = threading.Event()
        self._clear_log()
        self._banner("Läuft … Box nicht ausschalten oder abstecken", "info")
        self.b_start.configure(state="disabled")
        self.b_stop.configure(state="normal")
        cb = Callbacks(
            log=lambda lvl, msg: self.q.put(("log", lvl, msg)),
            step=lambda sid, state, det: self.q.put(("step", sid, state, det)),
            progress=lambda text, frac: self.q.put(("progress", text, frac)),
            twofactor=self._twofactor_worker,
        )

        def work():
            try:
                res = Runner(cfg, cb, self.cancel).run()
                self.q.put(("done", res))
            except Exception as e:  # pragma: no cover
                self.q.put(("log", "error", f"Unerwarteter Fehler: {e!r}"))
                self.q.put(("done", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _stop(self):
        if self.worker and messagebox.askyesno(APP_NAME, "Vorgang abbrechen?\n\nWährend eines Firmware-Updates wird "
                                               "nur das Warten abgebrochen – die Box darf trotzdem NICHT ausgeschaltet werden."):
            self.cancel.set()
            if self.tfa_cancel:
                self.tfa_cancel.set()

    def _twofactor_worker(self, req: TwoFactorRequest, poll) -> bool:
        """Läuft im Worker-Thread: Dialog öffnen und Box abfragen."""
        ev = threading.Event()
        self.tfa_cancel = ev
        self.q.put(("tfa_open", req))
        end = time.monotonic() + 180
        result = False
        try:
            while time.monotonic() < end and not ev.is_set() and not self.cancel.is_set():
                r = poll()
                if r is not None:
                    result = r
                    break
                self.q.put(("tfa_tick", int(end - time.monotonic())))
                time.sleep(1)
        finally:
            self.q.put(("tfa_close",))
            self.tfa_cancel = None
        return result

    # ------------------------------------------------------------ Events
    def _drain(self):
        try:
            while True:
                ev = self.q.get_nowait()
                getattr(self, "_ev_" + ev[0])(*ev[1:])
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _ev_log(self, lvl, msg):
        ts = time.strftime("%H:%M:%S")
        self.txt.configure(state="normal")
        self.txt.insert("end", f"{ts}  ", "info")
        self.txt.insert("end", msg + "\n", lvl if lvl in self.pal["log"] else "info")
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _ev_step(self, sid, state, detail):
        icon, det = self.step_widgets[sid]
        self.step_state[sid] = state
        icon.configure(text=ICONS.get(state, "?"), fg=self.pal["steps"].get(state, self.pal["fg"]))
        det.configure(text=detail if state != "off" else "nicht ausgewählt")

    def _ev_progress(self, text, frac):
        self.l_prog.configure(text=text)
        if frac is None:
            if text:
                if str(self.pbar.cget("mode")) != "indeterminate":
                    self.pbar.configure(mode="indeterminate")
                    self.pbar.start(15)
            else:
                self.pbar.stop()
                self.pbar.configure(mode="determinate", value=0)
        else:
            if str(self.pbar.cget("mode")) != "determinate":
                self.pbar.stop()
                self.pbar.configure(mode="determinate")
            self.pbar.configure(value=int(frac * 1000))

    def _ev_call(self, fn):
        try:
            fn()
        except tk.TclError:
            pass  # Dialog inzwischen geschlossen

    def _ev_boxinfo(self, text):
        self.l_box.configure(text=text)

    def _ev_detect_done(self):
        self.b_detect.configure(state="normal")

    def _ev_done(self, res):
        self.worker = None
        self.b_start.configure(state="normal")
        self.b_stop.configure(state="disabled")
        self._ev_progress("", None)
        if res is not None and res.ok:
            warn = any(s == "warn" for s in res.states.values())
            self._banner("✔ FERTIG – Box kann abgesteckt werden" + ("  (mit Hinweisen, siehe Log)" if warn else ""),
                         "warn" if warn else "ok")
            if res.box_before:
                self.l_box.configure(text=f"Zuletzt: {res.box_before.summary()}")
            self.v_pw.set("")
            self.e_pw.focus_set()
        else:
            self._banner("✖ Fehler – Details im Log. Box prüfen und erneut starten.", "error")
        self.bell()

    def _ev_tfa_open(self, req: TwoFactorRequest):
        self.bell()
        w = tk.Toplevel(self)
        w.title("Bestätigung an der FRITZ!Box")
        w.transient(self)
        self.themed_toplevel(w)
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Die FRITZ!Box verlangt eine Bestätigung", style="Head.TLabel").pack(anchor="w")
        ttk.Label(frm, text="Bitte jetzt " + req.describe() + ".", wraplength=420, justify="left").pack(anchor="w", pady=(10, 4))
        self.tfa_lbl = ttk.Label(frm, text="", style="Hint.TLabel")
        self.tfa_lbl.pack(anchor="w")
        pb = ttk.Progressbar(frm, mode="indeterminate")
        pb.pack(fill="x", pady=8)
        pb.start(12)
        ttk.Button(frm, text="Abbrechen", command=lambda: self.tfa_cancel and self.tfa_cancel.set()).pack(anchor="e")
        w.protocol("WM_DELETE_WINDOW", lambda: self.tfa_cancel and self.tfa_cancel.set())
        self.tfa_win = w
        w.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - w.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - w.winfo_height()) // 3
        w.geometry(f"+{max(0, x)}+{max(0, y)}")
        w.lift()
        w.focus_force()

    def _ev_tfa_tick(self, secs):
        if self.tfa_win:
            self.tfa_lbl.configure(text=f"Warte auf Bestätigung … noch {secs} s")

    def _ev_tfa_close(self):
        if self.tfa_win:
            self.tfa_win.destroy()
            self.tfa_win = None

    # ------------------------------------------------------------ Diverses
    def _banner(self, text, kind="info"):
        self.banner_kind = kind
        bg, fg = self.pal["banner"].get(kind, self.pal["banner"]["info"])
        self.banner.configure(text=text, bg=bg, fg=fg)

    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        for sid, _ in STEPS:
            self._ev_step(sid, "pending", "")

    def _open_csv(self):
        p = os.path.join(logs_dir(), "protokoll.csv")
        if os.path.exists(p):
            open_path(p)
        else:
            messagebox.showinfo(APP_NAME, "Noch kein Protokoll vorhanden.")

    def _on_close(self):
        if self.worker and not messagebox.askyesno(APP_NAME, "Ein Vorgang läuft noch. Trotzdem beenden?"):
            return
        self.cancel.set()
        try:
            save_settings(self._collect())
        except Exception:
            pass
        self.destroy()

    def _prefetch_dialog(self):
        PrefetchDialog(self)


class PrefetchDialog(tk.Toplevel):
    """Firmware für ausgewählte Modelle vorab herunterladen (solange der PC Internet hat)."""

    def __init__(self, app: App):
        super().__init__(app)
        self.app = app
        self.title("Firmware-Cache aktualisieren")
        self.transient(app)
        self.geometry("460x520")
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Modelle wählen (Strg/Shift für Mehrfachauswahl).\n"
                            "Lädt die aktuelle internationale FRITZ!OS-Version in den Ordner 'firmware'.",
                  wraplength=430, justify="left").pack(anchor="w")
        self.v_filter = tk.StringVar()
        ent = ttk.Entry(frm, textvariable=self.v_filter)
        ent.pack(fill="x", pady=6)
        self.v_filter.trace_add("write", lambda *a: self._fill())
        lf = ttk.Frame(frm)
        lf.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lf, selectmode="extended", activestyle="none", highlightthickness=1, relief="flat")
        app.themed_toplevel(self, lambda pal: self.lb.configure(
            bg=pal["field"], fg=pal["field_fg"], selectbackground=pal["select"], selectforeground=pal["select_fg"],
            highlightbackground=pal.get("border", pal["bg"]), highlightcolor=pal.get("accent", pal["select"])))
        sb = ttk.Scrollbar(lf, command=self.lb.yview)
        self.lb.configure(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.status = ttk.Label(frm, text="Lade Modellliste von download.avm.de …", style="Hint.TLabel")
        self.status.pack(anchor="w", pady=4)
        bf = ttk.Frame(frm)
        bf.pack(fill="x")
        self.b_go = ttk.Button(bf, text="Herunterladen", command=self._go, state="disabled")
        self.b_go.pack(side="right")
        ttk.Button(bf, text="Schließen", command=self.destroy).pack(side="right", padx=6)
        self.models: list = []
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            models = FirmwareRepo(firmware_dir(), lambda l, m: None).list_online_models()
            self.app.q.put(("call", lambda: self._loaded(models, "")))
        except HttpError as e:
            msg = str(e)
            self.app.q.put(("call", lambda: self._loaded([], msg)))

    def _loaded(self, models, err):
        self.models = models
        self._fill()
        self.status.configure(text=err or f"{len(models)} Modelle verfügbar")
        self.b_go.configure(state="normal" if models else "disabled")

    def _fill(self):
        flt = self.v_filter.get().lower().strip()
        self.lb.delete(0, "end")
        for m in self.models:
            if flt in m.lower():
                self.lb.insert("end", m)

    def _go(self):
        sel = [self.lb.get(i) for i in self.lb.curselection()]
        if not sel:
            messagebox.showinfo(APP_NAME, "Bitte mindestens ein Modell wählen.", parent=self)
            return
        self.b_go.configure(state="disabled")
        self.status.configure(text=f"Lade {len(sel)} Image(s) … (Fortschritt im Hauptfenster-Log)")
        q = self.app.q

        def work():
            repo = FirmwareRepo(firmware_dir(), lambda l, m: q.put(("log", l, m)))
            n = repo.prefetch_models(sel, lambda l, m: q.put(("log", l, m)))
            q.put(("log", "ok", f"Firmware-Cache: {n} von {len(sel)} Image(s) bereit in {firmware_dir()}"))
            q.put(("call", lambda: (self.status.configure(text=f"Fertig: {n} von {len(sel)}"),
                                    self.b_go.configure(state="normal"))))
        threading.Thread(target=work, daemon=True).start()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # scharfe Schrift bei HiDPI
        except Exception:
            pass
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_NAME)  # Taskleisten-Symbol
        except Exception:
            pass
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
