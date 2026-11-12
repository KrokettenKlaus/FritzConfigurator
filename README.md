# FritzConfigurator

Konfiguriert FRITZ!Boxen automatisch vor – genau die Schritte, die sonst von Hand in der Weboberfläche erledigt werden:

1. **Sprache & Land** – Deutsch / Italien (Box startet danach neu, das Tool wartet und meldet sich neu an)
2. **Internetanbieter KONVERTO** – Anbieterprofil wie in der Liste „Internetanbieter“ ausgewählt
   - DSL-Modelle: **KONVERTO DSL** (intern `raiffeisen`)
   - Modelle ohne DSL (z. B. 4060): **KONVERTO WAN** (intern `raiffeisen_lan1`)
   - Die Profil-ID unterscheidet sich je Modell/FRITZ!OS (z. B. 5530 Fiber mit 7.81: `raiffeisen` statt `raiffeisen_fiber`). Fehlt die gewählte ID, nimmt das Tool automatisch das passende Raiffeisen/KONVERTO-Profil der Box
   - Zugangsdaten = immer die Dummy-Daten `nome@xdsl.rol` / `pass22xdsl` (werden gesendet, falls das Profil sie nicht selbst vorgibt), optional eigene
3. **Heimnetz (optional)** – Box-IP (Gateway), Subnetzmaske, DHCP-Pool
4. **Firmware (optional)** – neueste FRITZ!OS-Version für genau dieses Modell
5. **Info-LED** – *System › Tasten und LEDs › LED-Anzeigen*: „Internetverbindung besteht – LED leuchtet“

Jeder Schritt wird nach dem Speichern **von der Box zurückgelesen und geprüft**. Bereits erledigte Schritte werden übersprungen (das Tool kann also gefahrlos erneut gestartet werden).

## Start

| Variante | Voraussetzung |
|---|---|
| `start.bat` doppelklicken | Python 3.8+ von python.org (enthält tkinter) |
| `FritzConfigurator.exe` | keine – einmalig mit `build_exe.bat` bauen, danach auf jeden PC kopierbar |
| Desktop-Symbol | `Desktop-Verknuepfung.bat` doppelklicken – legt „FritzConfigurator“ mit eigenem Symbol auf den Desktop (startet die EXE, falls vorhanden, sonst Python ohne Konsolenfenster) |

**Darstellung:** oben rechts *System* (folgt der Windows-Einstellung), *Hell* oder *Dunkel*. Die Wahl wird gespeichert.

Python installieren (einmalig): `winget install -e --id Python.Python.3.12`

## Ablauf pro Box

1. Box auf Werkseinstellungen, per **LAN-Kabel** mit dem PC verbinden (Netzwerkadapter auf DHCP).
2. **Kennwort vom Aufkleber** (Geräteunterseite, „FRITZ!Box-Kennwort“) eintippen.
3. **Start** – das Tool sucht die Box selbst (192.168.178.1, fritz.box, 169.254.1.1, 192.168.188.1) und wartet bis zu 2 Minuten, falls sie noch bootet.
4. Falls die Box eine **zusätzliche Bestätigung** verlangt, erscheint ein Dialog: kurz eine Taste an der Box drücken.
5. Grüne Meldung „FERTIG – Box kann abgesteckt werden“. Das Kennwortfeld wird geleert → nächste Box.

Die Einstellungen (ohne Kennwörter) werden in `settings.json` gespeichert.

## Firmware

- Mit Internet: das Tool holt die aktuelle Version von `download.avm.de` (Ordner `other` = international bzw. `deutschland` für deutsche Geräte).
- Ohne Internet (PC hängt nur an der Box): Images aus dem Ordner `firmware` werden verwendet.
  → Vorher **„Firmware-Cache aktualisieren …“** klicken und die benötigten Modelle laden, oder `.image`-Dateien manuell in `firmware\` legen.
- Sicherheitsprüfungen: Hardware-ID im Image (`var/content`) muss zur Box passen, kein Downgrade, Version nach dem Neustart wird kontrolliert.
- Upload wie Weboberfläche (`/cgi-bin/firmwarecfg`); falls abgelehnt, Rückfall auf TR-064 (Box lädt das Image vom PC – evtl. Windows-Firewall-Abfrage bestätigen).
- **Während des Updates die Box nicht ausschalten** (3–8 Minuten).

## Info-LED

Setzt die Info-LED wie in *System › Tasten und LEDs › LED-Anzeigen › Info-LED* auf **„Internetverbindung besteht – LED leuchtet“** (`box:settings/infoled_reason` = 4).
Läuft als letzter Schritt – nach einem Firmware-Update meldet sich das Tool dafür neu an. Ist der Wert schon gesetzt, wird der Schritt übersprungen.

## Heimnetz

Box-IP + Maske + DHCP-Pool werden wie in *Heimnetz › Netzwerk › IPv4-Einstellungen* gemeinsam gesetzt und vorher geprüft (Pool im Netz, Box-IP nicht im Pool …). „Vorschlag“ füllt `.20 – .200` aus.
Nach dem IP-Wechsel erneuert das Tool die IP-Adresse des PCs (`ipconfig /renew`) und arbeitet unter der neuen Adresse weiter. Hat der PC eine feste IP, muss sie angepasst werden.

## Protokoll

- `logs\protokoll.csv` – eine Zeile pro Box (Zeit, Modell, MAC, Firmware vorher/nachher, Ergebnis je Schritt inkl. Info-LED; eine ältere Datei wird automatisch um die Spalte ergänzt)
- `logs\<Datum>_<MAC>.log` – ausführliches Log je Box (bei Problemen bitte dieses schicken)

## Kommandozeile (optional)

```bat
py -3 -m fritzconf -p KENNWORT
py -3 -m fritzconf -p KENNWORT --lan 192.168.10.1/24 --dhcp 192.168.10.20-192.168.10.200 --firmware
py -3 -m fritzconf -p KENNWORT --dry-run
py -3 -m fritzconf -p KENNWORT --skip-infoled
py -3 -m fritzconf --prefetch fritzbox-7530-ax fritzbox-7590-ax fritzbox-4060
```

## Technischer Hintergrund

Die Abläufe wurden aus den Original-Quellen von FRITZ!OS 8.50 (7530 AX, 4060) abgeleitet; das Tool sendet dieselben Formulare wie die Weboberfläche:

| Schritt | Weg (primär) | Rückfall | Kontrolle |
|---|---|---|---|
| Anmeldung | `login_sid.lua?version=2` (PBKDF2), MD5 für ältere FRITZ!OS | – | – |
| Sprache/Land | `data.lua` page=`basic_first` | TR-064 `X_AVM-DE_SetInternationalConfig` | `query.lua` |
| Anbieter | `data.lua` page=`cred` (DSL) bzw. `ataSet` | direkter Seitenaufruf | `query.lua` (Anbieter, Benutzer, ACS) |
| Heimnetz | `data.lua` page=`boxnet` | direkter Seitenaufruf | `query.lua` |
| Firmware | `/cgi-bin/firmwarecfg` (sid, UploadFile) | TR-064 `X_AVM-DE_DoManualUpdate` | `jason_boxinfo.xml` |
| Info-LED | REST `PUT /api/v0/generic/box` `{"infoled_reason": 4}` (wie LED-Anzeigen, FRITZ!OS 8) | `/system/infoled.lua` (`led_change_state_event`) | `query.lua` |

Bestätigungsabfragen der Box werden wie ein Klick auf „OK“ beantwortet; die Zwei-Faktor-Bestätigung (Taste/Telefon) wird abgefragt und abgewartet.

`run_tests.bat` startet den Selbsttest gegen eine simulierte FRITZ!Box (26 Tests).

Das Programmsymbol (`FritzConfigurator.ico`) erzeugt `py -3 tools\make_icon.py` neu.
