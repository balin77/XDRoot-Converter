# Stand und nächste Schritte

Stand: Version 1 ist fertig und wurde lokal mit echtem Material getestet (19 Clips XAVC-I HD 50p einer Sony F5, `--verify` ohne
Abweichung). Runde 2 und die Fix-Runden wurden in einer Cloud-Session (Linux, ohne Kamera- und Windows-Zugang) umgesetzt und
**nur mit synthetischem Material** belegt. Details zur Beleglage: [README.md](../README.md), „Getesteter Stand“.

## Was in der Cloud-Session erledigt wurde

- Umgebung unter Linux (FFmpeg per Paketmanager, bmx 1.6 aus dem Quelltext) und synthetische XAVC-I-Testkarte
  (`scripts/make_test_card.py`, Standard KAG 512) für echte End-to-End-Läufe.
- Runde 2: MXF-Kopfdaten wie die Quelle (Python-Patcher `mxfheader.py`: Identification, Track-IDs, Spurpaarung über TrackNumber,
  Sound-Deskriptoren, TextObject, KAG 512), `--seq-off`, `--no-sony-header`, `--mxf-info`, `scripts/mxf_vergleich.py`.
- Drei Opus-Reviews (Metadaten, Ablauf/Robustheit, GUI) und eine Fix-Runde: XML aus dem Ist-Zustand, atomares Schreiben,
  Abbruch bei fremdem Ziel, Sperrdatei, Strukturprüfung und End-Decode-Test, Einstellungsdatei, erweitertes `--verify`
  (mit/ohne `--source`, `--deep`), GUI-Abschlussfenster und Rückfrage, Startskript mit Python-Prüfung, Strg+C unter Windows.
- Kleinigkeit zuletzt: Clips ohne Codec-String bekommen das H.264-Level aus der Bildrate (4.1 bis 30 fps, sonst 4.2).
- Alle Tests laufen (`python -m unittest discover -s tests`).

## Was der Autor lokal tun muss

1. `git pull`, danach `setup_tools.bat` (lädt FFmpeg und bmx; prüft auch `MXFDump.exe`).
2. **Abschlusstest mit echtem Material** (Details und Befehle: [MXF-VERGLEICH.md](MXF-VERGLEICH.md), Abschnitt 6):
   - `python -m xdroot_converter.cli --mxf-info <F55-Clip.MXF>` – versteht der Parser echte Sony-Kopfdaten (KAG, Track-IDs,
     Track-Namen, Sound-Deskriptor-Items)?
   - F5-Testkarte umwandeln, danach `--verify <ziel> --source <quelle> --deep`.
   - `python scripts/mxf_vergleich.py <F55-Quelle> <Ziel> --referenz <FX6-Clip> -o bericht.md` und den Bericht durchsehen.
   - den **56-GB-Clip der F55** (31 min) umwandeln: Laufzeit, Speicher, WAV > 4 GB, Strukturprüfung.
   - umgewandelte Clips in einem **Schnittprogramm** und in **Sony Catalyst** öffnen und abspielen.
3. **Unter Windows:** `XDROOT-Converter.bat` (Python-Prüfung, GUI-Start ohne Konsolenfenster), GUI-Lauf mit Abbrechen und
   Fortsetzen, Abschlussfenster, Strg+C in der Kommandozeile (Aufräumen, Exit-Code), **zwei parallele Läufe** in dasselbe Ziel
   (der zweite muss abbrechen) und Wiederaufnahme nach hartem Beenden (verwaiste Sperre).
4. Ergebnisse (und Abweichungen aus dem Vergleichsbericht) zurückmelden. Keine Seriennummern oder privaten Pfade einchecken
   (öffentliches Repo).

## Bekannte offene Punkte

- Echte Sony-Dateien wurden nie mit dem Parser/Patcher gelesen; Felder, die weder `mxf2raw` zeigt noch der Patcher kennt
  (Sony-eigene Sets, Fill-Größen, Partitionslayout), werden nicht übernommen.
- Das Ziel hat KAG 1 (so schreibt `raw2bmx`); ob Sony KAG 512 nutzt, ist nicht belegt.
- Wiedergabe in Schnittprogrammen und Sony-Werkzeugen ungeprüft.
- Andere Bildraten (25p, 29.97p, 59.94p) nur synthetisch; 4K, Interlaced und Ton ≠ Mono/24 Bit/48 kHz werden nur kopiert.
- Fortsetzen übernimmt abweichende Einstellungen nicht (nur Warnung); Sperrdatei und Strg+C nur unter Linux getestet.
- GUI-Klicks sind nicht automatisiert getestet.
