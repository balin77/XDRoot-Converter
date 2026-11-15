# XDROOT-Converter

## English summary

Converts a Sony XDROOT card recorded as **XAVC-Intra** (e.g. PMW-F5/F55, HD 50p) into a card with the
same structure but **XAVC-L** (H.264 High 4:2:2, Long GOP, 10 bit, 50 Mbit/s). Some Sony cameras only
offer XAVC-I at 50p, while delivery often requires XAVC-L. The tool re-encodes the video with FFmpeg,
re-wraps it with bmx (`raw2bmx`), keeps UMIDs, duration, timecode, audio (bit-exact), ANC camera
metadata, thumbnails and all other files, and patches only the codec fields in `MEDIAPRO.XML` and
`Clip/*M01.XML`. Windows only (tested), Python 3.11+, no pip packages; GUI (tkinter) and CLI. The user
interface and documentation are in German. Status: tested with F5/F55 XAVC-I HD 50p sources and an FX6
XAVC-L reference; **not** bit-identical to a camera recording, playback in NLEs and Sony tools is
untested (see "Einschränkungen"). MIT license.

## Was macht das Tool und warum?

Manche Sony-Kameras (z. B. PMW-F5/F55) nehmen bei 50p in HD nur **XAVC-I** (Intra, 100 Mbit/s) auf.
Für die Lieferung wird aber oft **XAVC-L** (Long GOP, 50 Mbit/s) verlangt. Der XDROOT-Converter
wandelt eine Sony-XDROOT-Karte in ein strukturgleiches XDROOT mit XAVC-L um, so dass das Ergebnis
wie eine direkt in XAVC-L aufgenommene Karte aussieht (Vorbild: eine Sony FX6).

## Voraussetzungen

- Windows
- Python 3.11 oder neuer (nur Standardbibliothek, keine pip-Pakete; tkinter gehört zur Standardinstallation)
- FFmpeg und bmx (werden vom Setup-Skript geladen, siehe unten)

## Installation

1. Repository klonen (oder als ZIP herunterladen und entpacken):
   ```
   git clone https://github.com/balin77/XDRoot-Converter.git
   ```
2. `setup_tools.bat` doppelklicken. Das Skript lädt FFmpeg (essentials build von
   [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg/releases)) und bmx
   ([bbc/bmx](https://github.com/bbc/bmx/releases), Windows-Binary) nach `tools\ffmpeg` bzw. `tools\bmx`
   und prüft, dass `ffmpeg.exe`, `ffprobe.exe`, `raw2bmx.exe` und `mxf2raw.exe` gefunden werden.
   Bereits vorhandene Programme werden übersprungen. Alternativ FFmpeg und bmx selbst installieren
   und in den `PATH` legen.
3. `XDROOT-Converter.bat` doppelklicken.

### Linux / macOS (ungetestet)

Das Tool sucht die Programme auch im `PATH`. Theoretisch sollte es daher auch dort laufen:
FFmpeg über den Paketmanager installieren (z. B. `apt install ffmpeg`, `brew install ffmpeg`),
bmx selbst bauen ([bbc/bmx](https://github.com/bbc/bmx)) und `raw2bmx`/`mxf2raw` in den `PATH` legen,
dann `python -m xdroot_converter` (GUI) bzw. die Kommandozeile starten. Das wurde **nicht getestet**.

## Bedienung

### GUI

`XDROOT-Converter.bat` starten, **Quellordner** (XDROOT oder dessen Elternordner) und **Zielordner**
wählen, **Konvertieren**. Im Zielordner entstehen `XDROOT\` und `konvertierung.log`.
Unter „Einstellungen" lassen sich Bitrate (Standard 50 Mbit/s), x264-Preset und das Überspringen von
Clips, die nicht in `MEDIAPRO.XML` stehen, einstellen. **Abbrechen** beendet alle laufenden Prozesse und
räumt temporäre Dateien auf; ein erneuter Lauf mit demselben Ziel überspringt bereits fertige,
verifizierte Clips.

### Kommandozeile

```
python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium] [--max-frames N]
                               [--unlisted convert|skip] [--only TEXT] [--embed-xml auto|always|never] [--no-anc]
python -m xdroot_converter.cli --verify <ziel> [--source <quelle>] [--deep]
```

| Option | Bedeutung |
|---|---|
| `<quelle>` | Quellordner (XDROOT oder Elternordner) |
| `<ziel>` | Zielordner, darin entsteht `XDROOT` |
| `--bitrate` | Video-Bitrate in Mbit/s (Standard 50) |
| `--preset` | x264-Preset (Standard `medium`) |
| `--max-frames N` | nur Test: nur die ersten N Frames je Clip; Dauer-Angaben bleiben original, Ausgabe wird mit `TESTAUSGABE.txt` gekennzeichnet |
| `--unlisted convert` / `skip` | Clips, die auf der Karte liegen, aber nicht in `MEDIAPRO.XML` stehen: umwandeln (Standard) oder überspringen |
| `--only TEXT` | nur Clips, deren Name `TEXT` enthält (mehrfach möglich; alle anderen werden nicht übernommen) |
| `--embed-xml auto` / `always` / `never` | `M01.XML` in die MXF einbetten (`auto`: nur wenn die Quelle es auch hat) |
| `--no-anc` | ANC-Datenspur (Kamera-Metadaten) nicht übernehmen |
| `--verify ZIEL` | Ziel-XDROOT (oder Elternordner) prüfen: MXF-Felder, UMIDs, Dauer, Timecode, Audio, XML-Konsistenz, H.264-Profil/GOP |
| `--source QUELLE` | mit `--verify`: zusätzlich Vergleich mit der Quelle (alle nicht geänderten Dateien byte-gleich) |
| `--deep` | mit `--verify`: jeden Clip komplett dekodieren |

## Was wird umgewandelt, kopiert, angepasst?

- **Umgewandelt:** das Video jedes Clips (nur XAVC-I, 1920x1080 progressiv) nach H.264 High 4:2:2,
  10 Bit, Long GOP (bei 50p N=24, M=3), Bildrate aus der Quelle. Wrapping als MXF OP1a mit bmx.
- **Unverändert übernommen:** UMIDs, Dauer, Timecode, Audio (Mono PCM 24 Bit/48 kHz, bitgenau),
  ANC-Datenspur mit Kamera-Metadaten, Erzeugungsdatum, `CUEUP.XML`, `DISCMETA.XML`, `Take/*`,
  `*.BIM`, Thumbnails und alle übrigen Dateien (1:1 kopiert, auch leere Ordner).
- **Angepasst (nur Codec-Angaben):** `Material@videoType` in `MEDIAPRO.XML` und `VideoFrame@videoCodec`
  in `Clip/*M01.XML` (z. B. `AVC100CBG_1920_1080_H422IP@L42` wird zu `AVC50_1920_1080_H422P@L42`).
  Die XML-Dateien werden textbasiert geändert, Formatierung und Zeilenenden bleiben erhalten.
- **Nur kopiert (mit Hinweis im Protokoll):** 4K, Halbbilder (Interlaced), Clips, die bereits
  Long GOP oder ein anderer Codec sind.
- Das Quell-XDROOT wird nie verändert. Das Ziel muss ein anderer Ordner sein.

Details und Messwerte: [docs/SPEC.md](docs/SPEC.md).

## Getesteter Stand

- Quellen: Sony F5 und F55, XAVC-I HD 50p. Referenz für das Ziel: echtes XAVC-L einer Sony FX6.
- Ergebnis laut `mxf2raw`: `AVC_High_422`, coding_label und ec_label wie bei der FX6; Audio mit
  `--aes-3` gewickelt (Track-Nummern und ec_label wie Sony); ANC-Spur byte-identisch übernommen.
- Unit-Tests (`python -m unittest discover -s tests`) und `--verify` an echten Umwandlungen.
- Andere Bildraten (25p, 29.97p, 59.94p usw.) wurden nur synthetisch getestet, nicht mit echten Kameraclips.

## Einschränkungen

- Das Ergebnis ist **nicht bitgenau** wie eine Kameraaufnahme: Die MXF-Identification nennt bmx statt
  Sony, die Track-IDs unterscheiden sich (1001/2001/3001 statt 2, 3, ...), mime_type und description
  des eingebetteten XML-TextObjects lassen sich mit raw2bmx nicht setzen.
- Es wurde **kein Sony-Prüf- oder Importwerkzeug** getestet.
- Die **Wiedergabe in Schnittprogrammen** (Avid, Premiere, Resolve, ...) wurde nicht geprüft.
- 4K und Interlaced werden nur kopiert, nicht umgewandelt.
- Andere Bildraten nur synthetisch getestet (siehe oben).
- Nur unter Windows getestet; Oberfläche und Meldungen sind deutsch.

Rückmeldungen und Tests mit anderen Kameras sind willkommen, siehe [CONTRIBUTING.md](CONTRIBUTING.md).

## Tests

```
python -m unittest discover -s tests
```

Reine Logik, ohne Medien; pytest wird nicht benötigt.

## Lizenz

[MIT](LICENSE). FFmpeg und bmx sind eigenständige Programme mit eigenen Lizenzen (siehe die jeweiligen
Projekte). Sie sind **nicht** Teil dieses Repositories; `setup_tools.bat` lädt sie auf Wunsch aus den
offiziellen Release-Seiten herunter.
