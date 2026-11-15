# XDROOT-Converter

Wandelt Sony-XDROOT-Karten mit XAVC-I (z. B. PMW-F55/F5, HD 50p) in ein
strukturgleiches XDROOT mit XAVC-L (H.264 Long GOP 4:2:2 10 Bit, 50 Mbit/s) um.
Grundlage: FFmpeg (Transcode) + bmx/raw2bmx (MXF-Wrapping). GUI: Python + tkinter.

Maßgebliche Spezifikation: [docs/SPEC.md](docs/SPEC.md).

## Arbeitsweise (verbindlich)

1. **Der Haupt-Agent (Claude in der Session) führt.** Er plant, schreibt/aktualisiert
   die Spezifikation, verteilt Aufgaben und prüft Ergebnisse. Er programmiert die
   Features nicht selbst.
2. **Sonnet-Agenten programmieren alles** (Agent-Tool mit `model: "sonnet"`).
3. **Opus-Agenten reviewen** (Agent-Tool mit `model: "opus"`). Der Reviewer bekommt
   den Code und die Spezifikation, nicht die Schlussfolgerungen des Programmierers.
4. **Review-Befunde werden von Sonnet-Agenten gefixt.** Danach neues Opus-Review.
5. **Schleife wiederholen, bis das Opus-Review keine groben Fehler mehr findet**
   (grob = falsche/kaputte Ausgabe, Datenverlust, Absturz, Metadaten-Inkonsistenz).
   Kleinigkeiten dürfen als Notiz bleiben.
6. **Mehrere Agenten parallel sind erwünscht**, wenn die Arbeit sich sauber teilen
   lässt: mehrere Sonnet-Agenten für unabhängige Module/Fixes (jeder mit eigenen
   Dateien – nie zwei Schreiber an derselben Datei) und mehrere Opus-Reviewer mit
   unterschiedlichem Blickwinkel (z. B. Metadaten/MXF-Korrektheit, Ablauf/Robustheit,
   GUI/Bedienung). Der Haupt-Agent führt die Ergebnisse zusammen.

## Metadaten immer vergleichen

Bei jeder Änderung an Transcode, Wrapping oder XML-Anpassung die Ausgabe gegen die
Referenzen prüfen, nicht aus dem Gedächtnis arbeiten:

- Referenz Ziel (echtes XAVC-L, Sony FX6): `docs/analysis/fx6_*`
- Referenz Quelle (XAVC-I, Sony F55): `docs/analysis/f55_*`
- Werkzeuge: `mxf2raw -i <datei>` (bmx) und `ffprobe -show_streams -show_format`
- Echte Karten (lokale Testkarten des Autors, existieren nur auf dessen Rechner; Pfade
  stehen in `local_paths.md`, nicht im Repo): FX6-Referenz (SSD), F55-Karte (Chip) und
  eine kleine F5-Testkarte (XAVC-I HD 50p). **Quell-Laufwerke nur lesen, nie beschreiben.**

## Regeln

- Das Quell-XDROOT wird niemals verändert. Ziel muss ein anderer Ordner sein.
- Testausgaben nur nach `_poc/` oder `_test_out/` (beide in `.gitignore`).
- Externe Programme liegen in `tools/` (nicht einchecken; `setup_tools.bat` lädt sie); Pfade werden vom Tool
  automatisch gefunden (`tools/**/ffmpeg.exe`, `raw2bmx.exe`, `mxf2raw.exe`).
- Tests: `python -m unittest discover -s tests` (reine Logik, ohne Medien; pytest ist nicht installiert) und
  `python -m xdroot_converter.cli --verify <ziel>` für echte Ausgaben.
- Sprache der Oberfläche: Deutsch.
