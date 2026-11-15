# Stand und nächste Schritte

Stand: Version 1 ist fertig und wurde lokal mit echtem Material getestet (19 Clips XAVC-I HD 50p einer Sony F5, `--verify` ohne
Abweichung). Runde 2 und die Fix-Runden wurden in einer Cloud-Session (Linux, ohne Kamera- und Windows-Zugang) umgesetzt und
**nur mit synthetischem Material** belegt. Details zur Beleglage: [README.md](../README.md), „Getesteter Stand“.

## Was in der Cloud-Session erledigt wurde

- Umgebung unter Linux (FFmpeg per Paketmanager, bmx 1.6 aus dem Quelltext) und synthetische XAVC-I-Testkarte
  (`scripts/make_test_card.py`, Standard KAG 512) für echte End-to-End-Läufe.
- Runde 2: MXF-Kopfdaten wie die Quelle (Python-Patcher `mxfheader.py`: Identification, Track-IDs, Spurpaarung über TrackNumber,
  Sound-Deskriptoren, TextObject, zusätzliche Timecode-Tracks), `--seq-off`, `--kag-size-512` (Ziel hat KAG 512, wenn die Quelle
  es hat), `--head-fill 65536`, `--no-sony-header`, `--mxf-info`, `scripts/mxf_vergleich.py`.
- Tonspuren: ausdrückliche Zuordnung ffmpeg-Audiostream ↔ MXF-Track aus dem Material Package der Quelle; `--verify` und Ablauf
  vergleichen den Toninhalt je Spur mit der Quelle.
- Drei Runden paralleler Opus-Reviews (Metadaten/MXF, Ablauf/Robustheit, Oberfläche) mit Fixes; die dritte Runde meldete keine
  groben Fehler mehr. Ergebnis u. a.: XML aus dem Ist-Zustand, atomares Schreiben, Abbruch bei fremdem Ziel, Sperrdatei mit
  Prozess-Startzeit, Strukturprüfung und End-Decode-Test (auch beim Fortsetzen), Einstellungsdatei mit Rückfall je Clip (ANC/XML),
  fertige XAVC-L-Clips werden nie durch eine Kopie ersetzt, `--max-frames` ersetzt keine vollständigen Clips, Wiederholungen bei
  gesperrten Dateien (Windows), Quellen mit unbekannter Dauer werden nur kopiert, nicht analysierbare `.MXF` und Clips mit unklarer Ton-Zuordnung
  werden nie umgewandelt, sondern unverändert kopiert (Warnung; mit vorhandenem Ziel-Clip Fehler), `._*.MXF` gelten als Begleitdateien, erweitertes `--verify` (mit/ohne `--source`, `--deep`), GUI-Abschlussfenster (Zählzeile, Fehlergrund je Clip, Hinweis auf
  unvollständige Zielkarte), Rückfragen beim Start, Startskript mit Python-Prüfung, Strg+C unter Windows.
- Alle Tests laufen (`python -m unittest discover -s tests`).

## Was der Autor lokal tun muss

1. `git pull`, danach `setup_tools.bat` (lädt FFmpeg und bmx; prüft auch `MXFDump.exe`).
2. **Zuerst: Unter Windows `XDROOT-Converter.bat` per Doppelklick starten.** Das Startskript wurde umgeschrieben und noch **nie
   unter Windows ausgeführt** (Python-Suche `py -3`/`python`, tkinter-Prüfung, `pythonw.exe` über `where python`, GUI-Start ohne
   Konsolenfenster). Startet der Doppelklick nicht: Ausweichweg `python -m xdroot_converter` in einer Konsole (im Projektordner),
   und die Meldung zurückgeben.
3. **Zuerst nur lesend, auf einen echten F55-/F5-Clip:**
   `python -m xdroot_converter.cli --mxf-info <Clip.MXF>` – zeigt KAG, Track-IDs (auch Timecode-Track), Track-Namen und
   Sound-Deskriptor-Items echter Sony-Dateien. Versteht der Parser sie fehlerfrei?
4. **F5-Testkarte umwandeln**, danach `--verify <ziel> --source <quelle> --deep` (Details und Befehle:
   [MXF-VERGLEICH.md](MXF-VERGLEICH.md), Abschnitt 6). Insbesondere den Toninhalts-Vergleich mit echtem Sony-AES3 beobachten.
5. `python scripts/mxf_vergleich.py <F55-Quelle> <Ziel> --referenz <FX6-Clip> -o bericht.md` und den Bericht durchsehen.
6. Den **56-GB-Clip der F55** (31 min) umwandeln: Laufzeit, freier Speicher (Video-Elementarstrom und WAV liegen temporär im Ziel),
   WAV > 4 GB, Strukturprüfung.
7. **Strg+C in der Kommandozeile** (Aufräumen, Exit-Code) und **zwei parallele Läufe** in dasselbe Ziel (der zweite muss abbrechen);
   dazu GUI-Lauf mit Abbrechen und Fortsetzen, Abschlussfenster, Wiederaufnahme nach hartem Beenden (verwaiste Sperre).
8. Umgewandelte Clips in einem **Schnittprogramm** und in **Sony Catalyst** öffnen und abspielen (zum Vergleich ein FX6-Clip).
9. Ergebnisse (und Abweichungen aus dem Vergleichsbericht) zurückmelden. Keine Seriennummern oder privaten Pfade einchecken
   (öffentliches Repo).

## Woran man ein Problem erkennt, welche Dateien helfen

- Hinweise: Zusammenfassung am Ende des Laufs (Zahl „Fehler“, Fehlergrund je Clip), Abschlussfenster der GUI, `--verify` mit
  „ABWEICHUNG“-Zeilen, eine Warnung zu „bmx-Kennungen“ oder zur Ton-Zuordnung.
- Hilfreich sind: `konvertierung.log` aus dem Zielordner (enthält alle Kopfdaten-Änderungen), `konvertierung_einstellungen.json`,
  die Ausgabe von `--mxf-info <Clip>` (Quelle und Ziel) und die `--verify`-Ausgabe.
- **Vor dem Weitergeben** Seriennummern (`serialNo="..."`), UMIDs und private Pfade (`file_uri`, Benutzernamen) entfernen oder
  durch Platzhalter ersetzen.

## Bekannte offene Punkte und Annahmen

- Echte Sony-Dateien wurden nie mit dem Parser/Patcher gelesen; Felder, die weder `mxf2raw` zeigt noch der Patcher kennt
  (Sony-eigene Sets, Fill-Größen, Partitionslayout), werden nicht übernommen.
- Track-ID des Timecode-Tracks und Track-Namen echter Sony-Dateien sind unbekannt; sie werden zur Laufzeit aus der Quelle übernommen.
- Ob echte Sony-Dateien KAG 512 nutzen, ist nicht belegt; das Ziel übernimmt das KAG der Quelle (512 oder 1).
- Der Toninhalts-Vergleich (erste Sekunde je Spur, bei Stille weitere Fenster) wurde nur mit synthetischem Ton geprüft; bei echtem
  Sony-AES3 ungeprüft.
- Ungewöhnliche Spurzuordnung (Tonspuren nicht eindeutig den ffmpeg-Audiostreams zuzuordnen) führt nicht zu geratener Zuordnung: der Clip
  wird nicht umgewandelt, sondern unverändert kopiert (Warnung); liegt im Ziel schon ein Clip dieses Namens, ist es ein Fehler.
- `MXFDump` meldet bei KAG 512 gelegentlich „Length is zero (following KLVFill key …)“: bekannte, harmlose Eigenheit von bmx/libMXF
  (siehe MXF-VERGLEICH.md, Abschnitt 2).
- Wiedergabe in Schnittprogrammen und Sony-Werkzeugen ungeprüft; kein Sony-Prüfwerkzeug getestet.
- Andere Bildraten (25p, 29.97p, 59.94p) nur synthetisch; 4K, Interlaced und Ton ≠ Mono/24 Bit/48 kHz werden nur kopiert.
- Fortsetzen übernimmt abweichende Einstellungen nicht (nur Warnung); Sperrdatei, Strg+C und Wiederholungen bei gesperrten Dateien
  nur unter Linux bzw. mit Attrappen getestet.
- GUI-Klicks sind nicht automatisiert getestet.
