FritzConfigurator 1.2.0 - Kurzanleitung
=======================================

Konfiguriert FRITZ!Boxen automatisch vor - die Schritte, die sonst von Hand in der
Weboberflaeche erledigt werden. Jeder Schritt wird nach dem Speichern von der Box
zurueckgelesen und geprueft. Bereits erledigte Schritte werden uebersprungen, das Tool
kann also jederzeit gefahrlos erneut gestartet werden.


1. WAS DAS TOOL EINSTELLT
------------------------

  1) Sprache & Land       z.B. Deutsch / Italien (Box startet danach neu, das Tool wartet)
  2) Internetanbieter     KONVERTO-Profil, Zugangsdaten = Dummy-Daten nome@xdsl.rol / pass22xdsl
                          (optional eigene Zugangsdaten)
  3) Heimnetz (optional)  Box-IP, Subnetzmaske, DHCP-Bereich
  4) Firmware (optional)  neueste FRITZ!OS-Version fuer genau dieses Modell
  5) Info-LED             "Internetverbindung besteht - LED leuchtet"

  Anbieterprofil je Modell (Einstellung "Automatisch"):

    DSL-Modelle (z.B. 7530 AX, 7590 AX)      KONVERTO DSL        (raiffeisen)
    Glasfaser (5690, 5530 Fiber)             KONVERTO            (raiffeisen_fiber bzw. raiffeisen)
    Ohne DSL (z.B. 4060)                     KONVERTO WAN        (raiffeisen_lan1)
    Glasfaser + DSL (5690 Pro, FRITZ!OS 7.62)  Reiter "Glasfaser": KONVERTO (raiffeisen_fiber)

  Bei der 5690 Pro bestimmt das gewaehlte Profil den Reiter der Seite "Zugangsdaten":
    "Automatisch" oder "KONVERTO (Glasfaser)"  -> Reiter Glasfaser
    "KONVERTO DSL" oder "KONVERTO WAN"          -> Reiter DSL / WAN


2. VORAUSSETZUNGEN UND START
----------------------------

  Variante A - mit Python (empfohlen am Arbeitsplatz)
    Python einmalig installieren (Eingabeaufforderung oder PowerShell):
        winget install -e --id Python.Python.3.12
    Danach start.bat doppelklicken.

  Variante B - EXE ohne Python (fuer andere PCs)
    Einmalig auf einem PC mit Python build_exe.bat doppelklicken.
    Die erzeugte FritzConfigurator.exe kann auf jeden Windows-PC kopiert werden.

  Desktop-Symbol
    Desktop-Verknuepfung.bat doppelklicken - legt "FritzConfigurator" auf den Desktop.

  WICHTIG: Nach einem Update des Programms das Fenster schliessen und neu starten.
  Die aktuelle Version steht in der Titelleiste (z.B. "FritzConfigurator 1.2.0").


3. ABLAUF PRO BOX
-----------------

  1. Box auf Werkseinstellungen, per LAN-Kabel mit dem PC verbinden
     (Netzwerkadapter des PCs auf "IP-Adresse automatisch beziehen").
  2. Kennwort vom Aufkleber (Geraeteunterseite, "FRITZ!Box-Kennwort") eintippen.
  3. "Start" klicken. Das Tool sucht die Box selbst (192.168.178.1, 169.254.1.1,
     192.168.188.1) und wartet bis zu 2 Minuten, falls sie noch bootet.
  4. Verlangt die Box eine zusaetzliche Bestaetigung, erscheint ein Fenster:
     kurz eine Taste an der Box druecken.
  5. Gruene Meldung "FERTIG - Box kann abgesteckt werden" -> naechste Box.

  Achtung: Ein falsches Kennwort sperrt die Anmeldung an der Box fuer einige
  Sekunden bis Minuten. Beim naechsten Start wartet das Tool eine noch laufende
  Sperre automatisch ab (bis 2 Minuten).

  Die Einstellungen (ohne Kennwoerter) werden in settings.json gespeichert.


4. FIRMWARE
-----------

  - Mit Internet: aktuelle Version wird von download.avm.de geladen.
  - Ohne Internet: vorher im Programm "Firmware-Cache aktualisieren ..." klicken oder
    .image-Dateien von Hand in den Ordner "firmware" legen.
  - Geprueft wird: Image passt zur Hardware, kein Downgrade, Version nach dem Neustart.
  - Waehrend des Updates (3-8 Minuten) die Box NICHT ausschalten.


5. PROTOKOLLE
-------------

  logs\protokoll.csv           eine Zeile pro Box (Zeit, Modell, MAC, Firmware vorher/nachher,
                               Ergebnis je Schritt) - laesst sich mit Excel oeffnen
  logs\<Datum>_<MAC>.log       ausfuehrliches Log je Box - bei Problemen bitte dieses schicken


6. FEHLERSUCHE
--------------

  "Anmeldung fehlgeschlagen - Kennwort falsch?"
      Kennwort vom Aufkleber pruefen (Gross-/Kleinschreibung). Wurde die Box schon einmal
      eingerichtet, gilt das dort vergebene Kennwort.

  "Keine FRITZ!Box gefunden"
      LAN-Kabel pruefen, Box fertig booten lassen (Power-LED leuchtet dauerhaft),
      PC-Netzwerkadapter auf DHCP. Notfalls die Adresse im Feld "Adresse" eintragen.

  "Seite 'Internet > Zugangsdaten' nicht gefunden (...) - Antwort der Box: ..."
      Das Modell bzw. die FRITZ!OS-Version nutzt eine noch unbekannte Seite fuer die
      Zugangsdaten. Bitte das Diagnose-Skript ausfuehren (siehe unten) und die Ausgabe
      zusammen mit dem Log schicken.

  "Kein Raiffeisen/KONVERTO-Profil in der Anbieterliste der Box"
      Land ist nicht auf Italien gestellt - Schritt "Sprache & Land" aktivieren.


  DIAGNOSE-SKRIPT (tools\probe_provider_page.py)
  ----------------------------------------------
  Meldet sich an der Box an und zeigt, welche Seite die Box fuer "Internet > Zugangsdaten"
  verwendet, welche Box-Werte gesetzt sind und welche KONVERTO-Profile vorhanden sind.
  Es aendert NICHTS an der Box.

  Aufruf im Programmordner (Eingabeaufforderung oder PowerShell):

      cd C:\Users\Administrator\Desktop\FritzConfigurator
      py -3 tools\probe_provider_page.py KENNWORT
      py -3 tools\probe_provider_page.py KENNWORT 169.254.1.1

  Das zweite Argument ist die Adresse der Box (Standard 192.168.178.1). Wenn die Box unter
  192.168.178.1 nicht antwortet, 169.254.1.1 verwenden.

  Ausgabe in eine Datei schreiben (zum Verschicken):

      py -3 tools\probe_provider_page.py KENNWORT > diagnose.txt 2>&1

  Wichtig: Kennwort genau eintippen - bei einem Tippfehler sperrt die Box die Anmeldung
  fuer einige Minuten.


7. KOMMANDOZEILE (OPTIONAL)
---------------------------

  Alle Befehle im Programmordner ausfuehren. KENNWORT = Kennwort vom Aufkleber.

  Komplett vorkonfigurieren (Einstellungen aus settings.json):
      py -3 -m fritzconf -p KENNWORT

  Nur anzeigen, was gemacht wuerde (Testmodus, keine Aenderung):
      py -3 -m fritzconf -p KENNWORT --dry-run

  Anderes Anbieterprofil (auto, raiffeisen, raiffeisen_fiber, raiffeisen_lan1):
      py -3 -m fritzconf -p KENNWORT --provider raiffeisen

  Bestimmte Box-Adresse verwenden:
      py -3 -m fritzconf -p KENNWORT --host 169.254.1.1

  Heimnetz und Firmware mit einstellen:
      py -3 -m fritzconf -p KENNWORT --lan 192.168.10.1/24 --dhcp 192.168.10.20-192.168.10.200 --firmware

  Einzelne Schritte auslassen:
      py -3 -m fritzconf -p KENNWORT --skip-locale --skip-provider --skip-infoled

  Eigene Zugangsdaten statt Dummy-Daten:
      py -3 -m fritzconf -p KENNWORT --ppp-user kunde@xdsl.rol --ppp-password GEHEIM

  Firmware fuer bestimmte Modelle vorab in den Ordner "firmware" laden:
      py -3 -m fritzconf --prefetch fritzbox-7530-ax fritzbox-7590-ax fritzbox-4060
  (Modellnamen wie die Ordner auf download.avm.de; ohne Modellangabe werden alle geladen)

  Alle Optionen anzeigen:
      py -3 -m fritzconf --help


8. SELBSTTEST (FUER ENTWICKLER)
-------------------------------

  run_tests.bat doppelklicken oder:
      py -3 -m unittest tests.test_flow -v

  Testet alle Ablaeufe gegen eine simulierte FRITZ!Box (34 Tests, ca. 4 Minuten).
  Port 80 und 49000 auf 127.0.0.x muessen frei sein.
