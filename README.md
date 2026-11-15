# XDROOT-Converter

## English summary

Converts a Sony XDROOT card recorded as **XAVC-Intra** (e.g. PMW-F5/F55, HD 50p) into a card with the
same structure but **XAVC-L** (H.264 High 4:2:2, Long GOP, 10 bit, 50 Mbit/s). Some Sony cameras only
offer XAVC-I at 50p, while delivery often requires XAVC-L. The tool re-encodes the video with FFmpeg,
re-wraps it with bmx (`raw2bmx`), keeps UMIDs, duration, timecode, audio (bit-exact), ANC camera
metadata, thumbnails and all other files, and patches only the codec fields in `MEDIAPRO.XML` and
`Clip/*M01.XML`. Python 3.11+, no pip packages; GUI (tkinter) and CLI. The user interface and
documentation are in German. Status: tested on Windows with F5/F55 XAVC-I HD 50p sources and an FX6
XAVC-L reference. Version 1 was tested locally by the author with real F5 material. Everything added afterwards (header
patcher, new checks, GUI changes, start script) was tested **only on Linux with synthetic material** (an AVC-Intra source
generated with bmx); it has **not** been run on Windows or with real camera files yet. **Please note:** the MXF
headers of the output carry the **Sony identifiers of the source camera** (company/product/version
copied from the source file, Sony track IDs 2, 3, ...), but the files were **created by this tool, not by a
camera**. They are not bit-identical to a camera recording, and playback in NLEs and Sony tools is
untested (see "Einschränkungen"). MIT license.

## Was macht das Tool und warum?

Manche Sony-Kameras (z. B. PMW-F5/F55) nehmen bei 50p in HD nur **XAVC-I** (Intra, 100 Mbit/s) auf.
Für die Lieferung wird aber oft **XAVC-L** (Long GOP, 50 Mbit/s) verlangt. Der XDROOT-Converter
wandelt eine Sony-XDROOT-Karte in ein strukturgleiches XDROOT mit XAVC-L um, so dass das Ergebnis
wie eine direkt in XAVC-L aufgenommene Karte aussieht (Vorbild: eine Sony FX6).

## Wichtiger Hinweis zu den erzeugten Dateien

Die erzeugten MXF-Dateien tragen in den Kopfdaten die **Sony-Kennungen der Quellkamera**: Hersteller, Produkt,
Version und Produkt-UID der Identification werden aus der Quelldatei übernommen (z. B. `Sony` / `Mem ` / `2.00`),
ebenso Track-IDs (Video 2, Audio 3 ...), Track-Namen, Audio-Deskriptoren und der MIME-Typ eines eingebetteten XML.
Die Dateien wurden aber **von diesem Tool erzeugt (FFmpeg + bmx + eigener Header-Patcher) und nicht von einer
Kamera**. Wer das weitergibt oder archiviert, sollte das wissen; die Kennungen sollen nur dafür sorgen, dass
Werkzeuge die Dateien wie Kamera-XAVC-L behandeln, sie sind keine Herkunftsangabe. Mit `--no-sony-header` entfällt
die Angleichung (die Dateien tragen dann bmx-Kennungen).

## Voraussetzungen

- Windows (Hauptziel) oder Linux (mit synthetischem Material getestet, siehe unten)
- Python 3.11 oder neuer (nur Standardbibliothek, keine pip-Pakete; tkinter gehört zur Standardinstallation).
  `XDROOT-Converter.bat` prüft beim Start, ob Python ≥ 3.11 **mit tkinter** vorhanden ist, und zeigt sonst eine Anleitung
- FFmpeg und bmx (werden vom Setup-Skript geladen, siehe unten)

## Installation

1. Repository klonen (oder als ZIP herunterladen und entpacken):
   ```
   git clone https://github.com/balin77/XDRoot-Converter.git
   ```
2. `setup_tools.bat` doppelklicken. Das Skript lädt FFmpeg (essentials build von
   [GyanD/codexffmpeg](https://github.com/GyanD/codexffmpeg/releases)) und bmx
   ([bbc/bmx](https://github.com/bbc/bmx/releases), Windows-Binary) nach `tools\ffmpeg` bzw. `tools\bmx`
   und prüft, dass `ffmpeg.exe`, `ffprobe.exe`, `raw2bmx.exe` und `mxf2raw.exe` gefunden werden
   (`MXFDump.exe` aus dem bmx-Paket wird als optional gemeldet; es wird nur für `scripts/mxf_vergleich.py`
   gebraucht). Bereits vorhandene Programme werden übersprungen. Alternativ FFmpeg und bmx selbst installieren
   und in den `PATH` legen.
3. `XDROOT-Converter.bat` doppelklicken.

### Linux (mit synthetischem Material getestet) / macOS (ungetestet)

Das Tool sucht die Programme auch im `PATH`: FFmpeg über den Paketmanager installieren (z. B. `apt install ffmpeg`,
`brew install ffmpeg`), bmx v1.6 selbst bauen ([bbc/bmx](https://github.com/bbc/bmx)) und `raw2bmx`, `mxf2raw`
(optional `MXFDump`) in den `PATH` legen, dann `python -m xdroot_converter` (GUI) bzw. die Kommandozeile starten.
Unter Linux wurden Umwandlung, Fortsetzen, Abbruch und `--verify` mit einer **synthetischen** XAVC-I-Testkarte
(`scripts/make_test_card.py`) durchgespielt, nicht mit echten Kameradateien. macOS wurde nicht getestet.

## Bedienung

### GUI

`XDROOT-Converter.bat` starten, **Quellordner** (XDROOT oder dessen Elternordner) und **Zielordner**
wählen, **Konvertieren**. Im Zielordner entstehen `XDROOT\` und `konvertierung.log`.
Unter „Einstellungen" lassen sich Bitrate (Standard 50 Mbit/s), x264-Preset und das Überspringen von
Clips, die nicht in `MEDIAPRO.XML` stehen, einstellen. Enthält der Zielordner schon ein XDROOT, fragt das Programm vor dem
Start „Fortsetzen?“. **Abbrechen** beendet alle laufenden Prozesse und räumt temporäre Dateien auf; ein erneuter Lauf mit
demselben Ziel setzt fort (siehe unten). Am Ende erscheint ein **Abschlussfenster** (Fertig / Fertig mit Fehlern / Abgebrochen /
fehlgeschlagen) mit den Zahlen (umgewandelt, bereits fertig, kopiert, Fehler) und den fehlgeschlagenen Clips. Beim Schließen
während eines Laufs wird nachgefragt.

### Kommandozeile

```
python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium] [--max-frames N]
                               [--unlisted convert|skip] [--only TEXT] [--embed-xml auto|always|never] [--no-anc]
                               [--no-sony-header] [--verbose]
python -m xdroot_converter.cli --verify <ziel> [--source <quelle>] [--deep]
python -m xdroot_converter.cli --mxf-info <datei.MXF>
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
| `--no-sony-header` | MXF-Kopfdaten **nicht** an die Quelle angleichen (Standard: Identification, Track-IDs usw. werden aus der Quell-MXF übernommen); die Ausgabe trägt dann bmx-Kennungen, und `--verify` ohne `--source` meldet dafür Abweichungen |
| `--verbose` | auch Einzelheiten ausgeben (z. B. jede Änderung der MXF-Kopfdaten); sie stehen immer in `konvertierung.log` |
| `--mxf-info DATEI` | Header-Metadaten einer MXF-Datei ausgeben (Diagnose, liest nur) |
| `--verify ZIEL` | Ziel-XDROOT (oder Elternordner) prüfen, siehe unten |
| `--source QUELLE` | mit `--verify`: zusätzlich Vergleich mit der Quelle |
| `--deep` | mit `--verify`: jeden Clip komplett dekodieren |

### Dateien im Zielordner, Fortsetzen, Schutzmechanismen

Neben `XDROOT\` entstehen im Zielordner:

- `konvertierung.log` – Protokoll (wird bei Folgeläufen angehängt)
- `konvertierung_einstellungen.json` – Einstellungen (Bitrate, Preset, Kopfdaten-Angleichung, XML einbetten, ANC, Programmversionen)
  je fertigem Clip
- `_xdroot_converter.lock` – Sperrdatei, nur während des Laufs; ein zweiter Lauf mit demselben Ziel bricht ab. Eine verwaiste
  Sperre (Prozess existiert nicht mehr) wird übernommen
- ein temporärer Arbeitsordner, der nach dem Lauf bzw. Abbruch entfernt wird

**Fortsetzen** (erneuter Lauf mit demselben Ziel): Vorhandene Ziel-Clips werden geprüft (Dateistruktur: Footer, RIP, Größe –
dadurch werden abgeschnittene Clips erkannt –, MXF-Felder, H.264-Eigenschaften). Fertige Clips werden übersprungen; weichen
nur die Kopfdaten ab (z. B. Ausgabe einer älteren Version), werden sie **nachgezogen**, nicht neu transkodiert; defekte oder
unvollständige Clips werden neu erzeugt. Wurden fertige Clips mit **anderen Einstellungen** erzeugt (Bitrate, Preset, …), gibt
es eine Warnung; sie werden trotzdem nicht neu erzeugt. `MEDIAPRO.XML` und `M01.XML` werden aus dem Ist-Zustand des Ziels
abgeleitet und atomar geschrieben, ein Folgelauf fällt also nicht auf XAVC-I zurück.

**Fremde Karte im Ziel:** Enthält der Zielordner ein XDROOT einer anderen Karte oder fremde Daten, bricht das Tool vor jeder
Änderung ab. Ein `--max-frames`-Testlauf wird abgelehnt, wenn das Ziel schon vollständige Clips enthält.

**`--verify`** prüft pro Clip: Dateistruktur, MXF-Felder (Codec, Labels, Dauer, Bildrate, UMIDs, Timecode, Audiospuren), ANC-Spur,
Konsistenz von `MEDIAPRO.XML` und `M01.XML` mit der MXF, H.264-Eigenschaften (Profil, Level, GOP-Länge), einen Decode-Test am
Clip-Ende und die Sony-Kopfdaten. Ohne `--source` werden die Kopfdaten gegen das Sony-Muster geprüft (auch eine mit
`--no-sony-header` erzeugte Ausgabe wird dann als abweichend gemeldet). Mit `--source` zusätzlich: Vergleich mit der Quell-MXF
(Dauer, UMIDs, Timecode, Kopfdaten, ANC), alle nicht geänderten Nicht-MXF-Dateien byte-gleich, **fehlende Dateien sind
Abweichungen**. Mit `--deep` wird jeder Clip komplett dekodiert (dauert).

## Was wird umgewandelt, kopiert, angepasst?

- **Umgewandelt:** das Video jedes Clips (nur XAVC-I, 1920x1080 progressiv) nach H.264 High 4:2:2,
  10 Bit, Long GOP (bei 50p N=24, M=3), Bildrate aus der Quelle. Wrapping als MXF OP1a mit bmx.
- **Unverändert übernommen:** UMIDs, Dauer, Timecode, Audio (Mono PCM 24 Bit/48 kHz, bitgenau),
  ANC-Datenspur mit Kamera-Metadaten, Erzeugungsdatum, `CUEUP.XML`, `DISCMETA.XML`, `Take/*`,
  `*.BIM`, Thumbnails und alle übrigen Dateien (1:1 kopiert, auch leere Ordner).
- **Angepasst (nur Codec-Angaben):** `Material@videoType` in `MEDIAPRO.XML` und `VideoFrame@videoCodec`
  in `Clip/*M01.XML` (z. B. `AVC100CBG_1920_1080_H422IP@L42` wird zu `AVC50_1920_1080_H422P@L42`).
  Die XML-Dateien werden textbasiert geändert, Formatierung und Zeilenenden bleiben erhalten.
- **Kopfdaten der MXF (seit Runde 2):** Nach dem Wrappen gleicht ein Python-Patcher die MXF-Header-Metadaten an die
  Quell-MXF an (Identification, Track-IDs, Track-Namen, Sound-Deskriptoren, MIME-Typ des eingebetteten XML). Schlägt das
  fehl, gilt der Clip als Fehler. Ein vorhandener Ziel-Clip, bei dem nur die Kopfdaten abweichen (z. B. Ausgabe einer
  älteren Version), wird beim erneuten Lauf nur nachgezogen, nicht neu transkodiert.
- **Nur kopiert (mit Hinweis im Protokoll):** 4K, Halbbilder (Interlaced), Clips, die bereits
  Long GOP oder ein anderer Codec sind, und Clips mit **Ton, der nicht Mono / 24 Bit / 48 kHz ist** (nur dieses Format wird umgewandelt).
- **Clips ohne Eintrag in `MEDIAPRO.XML` und ohne `M01.XML`:** werden umgewandelt (Standard), wenn die MXF eindeutig XAVC-I ist; es
  gibt dann kein XML anzupassen, das H.264-Level folgt der Bildrate (bis 30 fps 4.1, darüber 4.2). Mit `--unlisted skip` werden
  nicht gelistete Clips übersprungen.
- Das Quell-XDROOT wird nie verändert. Das Ziel muss ein anderer Ordner sein.

Details und Messwerte: [docs/SPEC.md](docs/SPEC.md).

## Getesteter Stand

**Bitte ehrlich lesen:**

- **Version 1** (Umwandlung mit FFmpeg + bmx, XML-Anpassung, einfache Prüfung) wurde vom Autor **lokal mit echtem F5-Material**
  getestet (Windows, XAVC-I HD 50p, `--verify` ohne Abweichung). Referenz für das Ziel: echtes XAVC-L einer Sony FX6
  (`mxf2raw`-Ausgaben in `docs/analysis`).
- **Alles aus Runde 2 und den Fix-Runden** – Kopfdaten-Patcher (Identification, Track-IDs, Sound-Deskriptoren, KAG 512), neue
  Prüfungen in `--verify`, Fortsetzen/Sperre/Einstellungsdatei, GUI-Änderungen (Abschlussfenster, Rückfrage), Startskript
  `.bat` – ist bisher **nur unter Linux mit synthetischem Material** getestet (eine mit bmx erzeugte AVC-Intra-Quelle,
  `scripts/make_test_card.py`; erfundene UMIDs, 8 Tonspuren, ANC). Dort wurden Umwandlung, Fortsetzen, Abbruch per Strg+C,
  `--embed-xml always`, nicht gelistete Clips und `--verify --deep` durchgespielt; `MXFDump` und `mxf2raw` zeigen keine
  Auffälligkeiten. Das ersetzt **keinen** Test mit Kameradaten.
- **Unter Windows ungetestet:** `XDROOT-Converter.bat`, GUI, Strg+C in der Kommandozeile, Sperrdatei, `setup_tools.bat` in der
  jetzigen Fassung. Mit **echten Kameradateien ungetestet:** der Kopfdaten-Patcher (der Parser wurde nie auf eine echte
  Sony-MXF angewendet) und die neuen Prüfungen.
- `scripts/mxf_vergleich.py` vergleicht die Header-Metadaten zweier/dreier MXF-Dateien Satz für Satz
  (Ergebnis und Grenzen: [docs/MXF-VERGLEICH.md](docs/MXF-VERGLEICH.md)). Echte Sony-Dateien wurden damit noch
  **nicht** verglichen (im Repository liegen von ihnen nur `mxf2raw`-Ausgaben). Dort steht auch, welche Befehle der Autor lokal
  ausführen muss; Stand und nächste Schritte: [docs/NAECHSTE-SCHRITTE.md](docs/NAECHSTE-SCHRITTE.md).
- Andere Bildraten (25p, 29.97p, 59.94p usw.) wurden nur synthetisch getestet, nicht mit echten Kameraclips.

## Einschränkungen

- Das Ergebnis ist **nicht bitgenau** wie eine Kameraaufnahme: Die Kopfdaten (Identification, Track-IDs, ...) werden
  zwar aus der Quelle übernommen, die Datei bleibt aber eine mit bmx erzeugte MXF (z. B. neue Generation-UID,
  bmx-Partitionslayout, KAG 1 und Fill-Größen). Felder, die `mxf2raw` nicht zeigt und die der Patcher nicht kennt
  (Sony-eigene Sets u. Ä.), werden nicht übernommen.
- Es wurde **kein Sony-Prüf- oder Importwerkzeug** getestet (z. B. Sony Catalyst).
- Die **Wiedergabe in Schnittprogrammen** (Avid, Premiere, Resolve, ...) wurde nicht geprüft.
- 4K, Interlaced und Ton, der nicht Mono/24 Bit/48 kHz ist, werden nur kopiert, nicht umgewandelt.
- Clips, deren Tonspuren unterschiedlichen `SequenceOffset` haben, werden nicht umgewandelt.
- Andere Bildraten nur synthetisch getestet (siehe oben).
- Zwei Läufe gleichzeitig in dasselbe Ziel sind gesperrt; die Sperre wurde nur unter Linux getestet.
- Oberfläche und Meldungen sind deutsch.

Rückmeldungen und Tests mit anderen Kameras sind willkommen, siehe [CONTRIBUTING.md](CONTRIBUTING.md).

## Tests

```
python -m unittest discover -s tests
```

Reine Logik, ohne Medien; pytest wird nicht benötigt. Für Läufe mit synthetischem Material siehe
`scripts/make_test_card.py` (Testkarte nach `_test_out/`) und `scripts/mxf_vergleich.py`.

## Lizenz

[MIT](LICENSE). FFmpeg und bmx sind eigenständige Programme mit eigenen Lizenzen (siehe die jeweiligen
Projekte). Sie sind **nicht** Teil dieses Repositories; `setup_tools.bat` lädt sie auf Wunsch aus den
offiziellen Release-Seiten herunter.
