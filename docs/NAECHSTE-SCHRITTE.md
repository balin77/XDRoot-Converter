# Nächste Schritte (Auftrag für die Cloud-Session)

Stand: Version 1 ist fertig, auf `main`, lokal mit echtem Material getestet
(19 Clips XAVC-I HD 50p einer Sony F5: verify ohne Abweichung). Noch kein Review.
Die echten Testmedien und die Windows-Programme in `tools/` gibt es nur auf dem
Windows-Rechner des Autors, nicht in der Cloud. Sprache mit dem Nutzer: Deutsch.

Vorgehen gemäß `CLAUDE.md`: Der Haupt-Agent führt, Sonnet-Agenten programmieren,
Opus-Agenten reviewen, Fixes wieder durch Sonnet – bis die Opus-Reviews keine groben
Fehler mehr finden. Mehrere Agenten parallel sind erwünscht.

1. **Umgebung**: FFmpeg per Paketmanager installieren, bmx v1.6
   (https://github.com/bbc/bmx) aus dem Quelltext bauen (`raw2bmx`, `mxf2raw`,
   `MXFDump`). Synthetische XAVC-I-Testkarte erzeugen (H.264 High 4:2:2 Intra
   1920x1080 50p, 8 Mono-PCM-Spuren 24 Bit/48 kHz, `MEDIAPRO.XML`/`M01.XML` nach dem
   Muster von `docs/analysis/f55_*`) für echte End-to-End-Läufe. Unit-Tests
   (`python -m unittest discover -s tests`, 32 Stück) müssen grün sein; das Tool muss
   unter Linux mit Programmen aus dem PATH laufen.
2. **Runde 2 aus `docs/SPEC.md`** (ausdrücklicher Wunsch des Nutzers): Identification
   der Ziel-MXF wie Sony (aus der Quell-MXF übernommen), Spuren – vor allem die
   Tonspuren – exakt wie bei Sony (Track-IDs 2, 3, … statt 1001/2001/3001, kein
   bmx-typisches Format). Reihenfolge der Wege: `raw2bmx --prod-info`, eigener
   Python-Patcher für Header- und Footer-Metadaten, sonst bmx forken/patchen (dann
   klären, wie der Windows-Build entsteht, z. B. GitHub Actions, und `setup_tools.ps1`
   anpassen). Satz-für-Satz-Vergleich per MXFDump nach `docs/MXF-VERGLEICH.md`;
   `verify` um die neuen Felder erweitern.
3. **Drei parallele Opus-Reviews** des gesamten Codes: (a) Metadaten/MXF-Korrektheit
   gegen `docs/analysis`; (b) Ablauf/Robustheit: Abbruch, Fortsetzen, Fehler,
   Speicherplatz, sehr große Clips (56 GB/31 min, WAV > 4 GB/RF64?), Clips, die nicht
   in `MEDIAPRO.XML` stehen; (c) GUI/Bedienung und Thread-Sicherheit.
   Bekannte offene Punkte: Fortsetzen prüft keine H.264-Eigenschaften (z. B.
   color_range); Rückfall-Logik ohne ANC/XML nie ausgelöst; eingebettetes XML hat
   `application/xml` statt `text/xml`; GUI-Klicks ungetestet.
4. Nach jeder Fix-Runde Tests + End-to-End-Lauf, dann committen und nach `main`
   pushen. Keine Seriennummern oder privaten Pfade einchecken (öffentliches Repo).

**Fertig**, wenn Runde 2 per `mxf2raw`/`MXFDump` belegt ist, die Opus-Reviews keine
groben Fehler mehr melden und alles auf `main` liegt. Abschlussmeldung auf Deutsch:
was geändert wurde, was nur synthetisch getestet ist, und dass der Autor lokal
`git pull` + `setup_tools.bat` ausführen und einen Abschlusstest mit echtem Material
fahren muss (F5-Testkarte, 56-GB-Clip der F55, Öffnen in einem Schnittprogramm).
