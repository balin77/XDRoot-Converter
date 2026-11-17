# XDROOT-Converter

## English summary

Converts a Sony XDROOT card recorded as **XAVC-Intra** (e.g. PMW-F5/F55, HD 50p) into a card with the
same structure but **XAVC-L** (H.264 High 4:2:2, Long GOP, 10 bit, 50 Mbit/s). Some Sony cameras only
offer XAVC-I at 50p, while delivery often requires XAVC-L. The tool re-encodes the video with FFmpeg,
re-wraps it with bmx (`raw2bmx`), keeps UMIDs, duration, timecode, audio (bit-exact, compared per track
against the source), ANC camera metadata, thumbnails and all other files, and patches only the codec fields in
`MEDIAPRO.XML` and `Clip/*M01.XML`. Unanalysable `.MXF` files (garbage, 0 bytes, aborted recordings) are copied
unchanged with a warning. Python 3.11+, no pip packages; GUI (tkinter) and CLI. The user interface and
documentation are in German. Status (November 2026): tested by the author on Windows with real camera material: 19 clips
from a Sony PMW-F5 card and a complete 76-clip F55 card (XAVC-I HD 50p, 104 GB) were converted and passed
`--verify --source --deep` without deviations; the MXF headers were compared set by set against a real Sony FX6 XAVC-L
clip (remaining differences are listed in `docs/NAECHSTE-SCHRITTE.md`). Ctrl+C, resuming, a hard kill, two parallel runs
and an unplanned machine crash were exercised on Windows. After every conversion the target card is verified automatically
against the source (full decode, full per-track audio comparison) and clips with deviations are converted again. The converted F55 card was imported by the author into DaVinci
Resolve and Sony Catalyst Browse (card recognised, playback and audio tracks fine; timecode display not compared).
**Not tested:** other NLEs and Sony tools, frame rates other than 50p with camera files, and (beyond a single diagnostic run on one Legion model) real temperature readings
and fan control of the optional temperature monitor (needs administrator rights and a Lenovo Legion). By default the monitor only measures and runs the fans at full speed; throttling/pausing on heat is opt-in (`--thermal-throttle`), because on the tested machine it slowed a run down without protecting anything (the CPU regulates itself). **Please note:** the MXF
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
  (die GUI startet ohne Konsolenfenster über `pyw`, sonst über das per `where python` gefundene `pythonw.exe`; startet der
  Doppelklick nicht, hilft `python -m xdroot_converter` in einer Konsole)
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
Clips, die nicht in `MEDIAPRO.XML` stehen, einstellen, außerdem die Temperaturüberwachung („Temperatur überwachen“, „Lüfter
voll ab“, „Bei Hitze drosseln und pausieren“, „Pause ab“) und „CPU-Kerne begrenzen“ (siehe „Temperaturüberwachung und Schonmodus“); Hinweise zu den Einstellungen
stehen in der Oberfläche. Enthält der
Zielordner schon ein XDROOT, fragt das Programm vor dem Start „Fortsetzen?“ (weitere Rückfragen beim Start, z. B. zu
abweichenden Einstellungen). **Abbrechen** beendet alle laufenden Prozesse und räumt temporäre Dateien auf; ein erneuter Lauf mit
demselben Ziel setzt fort (siehe unten). Am Ende erscheint ein **Abschlussfenster** (Fertig / Fertig mit Fehlern / Abgebrochen /
fehlgeschlagen) mit einer Zählzeile (umgewandelt, bereits fertig, kopiert, Fehler), dem **Fehlergrund je Clip** und einem Hinweis,
wenn die Zielkarte dadurch **unvollständig** ist. Beim Schließen während eines Laufs wird nachgefragt.

Nach der Umwandlung läuft automatisch die **Abschlussprüfung**: Die Zielkarte wird gründlich gegen die Quelle geprüft, und
Clips mit einer Abweichung werden neu umgewandelt (Standard: bis zu zwei Wiederholungsrunden). Das Abschlussfenster nennt
dann „Geprüft: N Clips, M ohne Abweichung“ sowie wiederholte und weiterhin fehlerhafte Clips mit Grund. In den Einstellungen
lässt sich die Prüfung abschalten oder auf die schnelle Variante stellen (siehe „Abschlussprüfung“ unten).

#### Auftragsliste: mehrere Karten nacheinander

Mit **Zur Liste hinzufügen** (statt „Konvertieren“) wandert der aktuelle Auftrag in eine Liste mit den Spalten Quelle, Ziel,
Einstellungen und Status; **Alle konvertieren** arbeitet die wartenden Aufträge in Listenreihenfolge nacheinander ab, z. B. drei
Karten über Nacht. Die Liste erscheint erst, wenn sie Aufträge enthält.

- Jeder Auftrag ist eine Momentaufnahme: Quelle, Ziel und alle Einstellungen werden beim Hinzufügen kopiert. Später geänderte
  Felder wirken nur auf neue Aufträge.
- Die Prüfungen und Rückfragen (z. B. „Fortsetzen?“) kommen schon beim Hinzufügen, nicht nachts; während des Listenlaufs gibt es
  keine Rückfrage. Abgelehnt werden ein Ziel, das schon in der Liste steht, und eine Quelle, die Ziel eines anderen Auftrags ist
  (und umgekehrt); verglichen werden die aufgelösten Pfade (Kurznamen, Verknüpfungen und Langpfad-Schreibweise zählen als derselbe
  Ordner). Dasselbe gilt gegen Quelle und Ziel eines gerade laufenden Einzellaufs. Auch „Konvertieren“ lehnt ein Ziel ab, das in oder auf einer Quelle der Liste liegt. Steht dieselbe Quelle schon in
  der Liste, gibt es eine Rückfrage. Reicht der freie Platz für die geschätzten Zielgrößen aller wartenden, abgebrochenen und des
  laufenden Auftrags auf einem Laufwerk nicht, gibt es eine Warnung mit Rückfrage (die Schätzung wird bei Bedarf im Hintergrund
  nachgetragen; solange sie fehlt, entfällt die Warnung).
- Kartenwechsel: Beim Hinzufügen wird ein Fingerabdruck der Quelle gespeichert (MEDIAPRO.XML sowie Namen und Größen der
  Clip-Dateien; die Clips selbst werden nicht gelesen). Beim Start des Auftrags wird er verglichen. Hat sich die Quelle geändert
  (andere Karte im selben Kartenleser, Karte entfernt), endet der Auftrag sofort mit „Fehler“, ohne etwas zu schreiben.
- Jeder Auftrag ist ein normaler Lauf (Abschlussprüfung, Temperaturüberwachung, Protokolldatei im Zielordner). Ein Fehler,
  Abweichungen oder ein Hitze-Abbruch halten die Liste nicht an; der nächste Auftrag startet. **Abbrechen** (oder Schließen des
  Fensters) beendet den laufenden Auftrag und den Listenlauf; die übrigen bleiben „wartet“, ein späteres „Alle konvertieren“
  nimmt den abgebrochenen Auftrag wieder auf. Aufträge mit „Fehler“ werden nicht automatisch wiederholt: zum Fortsetzen den
  Auftrag entfernen und erneut hinzufügen (fertige Clips werden übersprungen). Erledigte Aufträge werden nicht erneut gestartet
  und bleiben stehen, bis sie entfernt werden.
- Während eines Laufs bleiben Quelle, Ziel, „Durchsuchen“ und die Einstellungen bedienbar; sie wirken nur auf neue Aufträge, der
  laufende Auftrag arbeitet mit seiner Momentaufnahme. Gesperrt sind nur „Konvertieren“ und „Alle konvertieren“; „Zur Liste
  hinzufügen“ ist weiter möglich (der Auftrag wird am Ende mit abgearbeitet).
- Der Rechner geht während des ganzen Listenlaufs nicht in den Energiesparmodus (die Zeile steht auch in der Protokolldatei jedes
  Auftrags). Am Ende erscheint ein einziges Abschlussfenster mit einer Ergebniszeile je Auftrag und den Summen (auch nach einem
  Abbruch, sofern mindestens ein Auftrag gelaufen ist).
- Grenzen: Die Liste wird nicht gespeichert (nach dem Schließen des Programms ist sie leer; ein unterbrochener Auftrag lässt sich
  durch erneutes Hinzufügen fortsetzen). Es läuft immer nur ein Auftrag gleichzeitig. Die Kommandozeile kennt keine Liste.
  Schließen während des Listenlaufs verhält sich wie beim Einzellauf (Rückfrage, dann Abbruch).

### Kommandozeile

```
python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium] [--max-frames N]
                               [--unlisted convert|skip] [--only TEXT] [--embed-xml auto|always|never] [--no-anc]
                               [--no-sony-header] [--verbose]
                               [--no-auto-verify] [--auto-verify-quick] [--retries N]
python -m xdroot_converter.cli --verify <ziel> [--source <quelle>] [--deep]
python -m xdroot_converter.cli --mxf-info <datei.MXF>
python -m xdroot_converter.cli --thermal-test
```

Weitere Optionen zu Temperatur und Schonmodus (`--threads`, `--thermal`, `--temp-fan`, `--thermal-throttle`, `--temp-pause`) stehen in der Tabelle.

| Option | Bedeutung |
|---|---|
| `<quelle>` | Quellordner (XDROOT oder Elternordner) |
| `<ziel>` | Zielordner, darin entsteht `XDROOT` |
| `--bitrate` | Video-Bitrate in Mbit/s (Standard 50) |
| `--preset` | x264-Preset (Standard `medium`) |
| `--max-frames N` | nur Test: nur die ersten N Frames je Clip; Dauer-Angaben bleiben original, Ausgabe wird mit `TESTAUSGABE.txt` gekennzeichnet; ersetzt nie vollständige Clips im Ziel |
| `--unlisted convert` / `skip` | Clips, die auf der Karte liegen, aber nicht in `MEDIAPRO.XML` stehen: umwandeln (Standard) oder überspringen |
| `--only TEXT` | nur Clips, deren Name `TEXT` enthält (mehrfach möglich; alle anderen werden nicht übernommen) |
| `--embed-xml auto` / `always` / `never` | `M01.XML` in die MXF einbetten (`auto`: nur wenn die Quelle es auch hat) |
| `--no-anc` | ANC-Datenspur (Kamera-Metadaten) nicht übernehmen |
| `--no-sony-header` | MXF-Kopfdaten **nicht** an die Quelle angleichen (Standard: Identification, Track-IDs usw. werden aus der Quell-MXF übernommen); die Ausgabe trägt dann bmx-Kennungen, und `--verify` ohne `--source` meldet dafür Abweichungen |
| `--threads N` | Schonmodus: `ffmpeg` nutzt höchstens N Kerne (weniger Wärme, dauert länger); wirkt auch bei `--verify --deep` |
| `--no-auto-verify` | keine Abschlussprüfung nach der Umwandlung (Standard: Prüfung wie `--verify --source --deep`, fehlerhafte Clips werden neu umgewandelt) |
| `--auto-verify-quick` | Abschlussprüfung ohne vollständiges Dekodieren und ohne vollständigen Tonvergleich (schneller, weniger gründlich) |
| `--retries N` | Wiederholungsrunden für fehlerhafte Clips (0 bis 5, Standard 2) |
| `--thermal auto` / `off` | Temperaturüberwachung (Standard `auto`: messen, anzeigen, „Lüfter voll“; ohne nutzbare Messung läuft alles wie gewohnt; `off` schaltet alles ab), siehe unten |
| `--temp-fan GRAD` | Schwelle für „Lüfter voll“ (Standard 85 °C, erlaubt 50–110; die Lüfter bleiben dann bis Laufende voll) |
| `--thermal-throttle` | Bei Hitze die Last drosseln und notfalls pausieren (Standard **aus**; die CPU regelt sich selbst) |
| `--temp-pause GRAD` | nur mit `--thermal-throttle` (sonst nur ein Hinweis): Schwelle für Drosseln/Pause (Standard 97 °C, erlaubt 60–120, höher als die Lüfter-Schwelle); ab dieser Temperatur wird zuerst die Last gedrosselt, die Pause ist nur die letzte Stufe (oder 5 °C darüber); weiter geht es 12 °C darunter |
| `--thermal-test` | Diagnose der Temperaturmessung und Lüftersteuerung (braucht Administratorrechte) |
| `--verbose` | auch Einzelheiten ausgeben (z. B. jede Änderung der MXF-Kopfdaten); sie stehen immer in `konvertierung.log` |
| `--mxf-info DATEI` | Header-Metadaten einer MXF-Datei ausgeben (Diagnose, liest nur) |
| `--verify ZIEL` | Ziel-XDROOT (oder Elternordner) prüfen, siehe unten |
| `--source QUELLE` | mit `--verify`: zusätzlich Vergleich mit der Quelle |
| `--deep` | mit `--verify`: jeden Clip komplett dekodieren (Abbruch beim ersten Decodierfehler) und mit `--source` den Ton jeder Spur vollständig mit der Quelle vergleichen |

**Exit-Codes:** 0 fertig (mit Abschlussprüfung: geprüft, ohne Abweichung), 1 Fehler oder Abweichung, 2 falscher Aufruf,
3 Rechte-Erhöhung abgelehnt, 130 abgebrochen (auch Temperatur-Abbruch). Die letzte Ausgabezeile eines Laufs beginnt immer mit
`Ergebnis:`.

### Dateien im Zielordner, Fortsetzen, Schutzmechanismen

Neben `XDROOT\` entstehen im Zielordner:

- `konvertierung.log` – Protokoll (wird bei Folgeläufen angehängt)
- `konvertierung_einstellungen.json` – Einstellungen (Bitrate, Preset, Kopfdaten-Angleichung, XML einbetten, ANC, Programmversionen)
  je fertigem Clip; außerdem, wenn ANC-Spur oder eingebettetes XML bei einem Clip bewusst weggelassen werden mussten
  (Rückfall), das je Clip, und `--unlisted skip`/`--only` des Laufs (`auswahl`). Eine unlesbare/kaputte Einstellungsdatei wird
  nicht überschrieben, sondern als `konvertierung_einstellungen.json.defekt` gesichert (mit Warnung)
- `_xdroot_converter.lock` – Sperrdatei (PID + Prozess-Startzeit), nur während des Laufs; ein zweiter Lauf mit demselben Ziel
  bricht ab. Eine verwaiste Sperre (Prozess existiert nicht mehr bzw. andere Startzeit; die Prozess-Startzeit hat Vorrang vor der
  bloßen PID) wird übernommen
- ein temporärer Arbeitsordner, der nach dem Lauf bzw. Abbruch entfernt wird

**Fortsetzen** (erneuter Lauf mit demselben Ziel): Vorhandene Ziel-Clips werden geprüft (Dateistruktur: Footer, RIP, Größe –
dadurch werden abgeschnittene Clips erkannt –, MXF-Felder, H.264-Eigenschaften, zusätzlich ein Decode-Test am Clip-Ende).
Fertige Clips werden übersprungen; weichen
nur die Kopfdaten ab (z. B. Ausgabe einer älteren Version), werden sie **nachgezogen**, nicht neu transkodiert; defekte oder
unvollständige Clips werden neu erzeugt – ein bereits fertiger XAVC-L-Ziel-Clip wird dabei **nie durch eine Kopie ersetzt**,
und ein vorhandener Ziel-Clip wird erst nach erfolgreicher Neuerzeugung ersetzt. Wurden fertige Clips mit **anderen Einstellungen** erzeugt (Bitrate, Preset, …), gibt
es eine Warnung; sie werden trotzdem nicht neu erzeugt. `MEDIAPRO.XML` und `M01.XML` werden aus dem Ist-Zustand des Ziels
abgeleitet und atomar geschrieben, ein Folgelauf fällt also nicht auf XAVC-I zurück.

**Fremde Karte im Ziel:** Enthält der Zielordner ein XDROOT einer anderen Karte oder fremde Daten, bricht das Tool vor jeder
Änderung ab. Ein `--max-frames`-Testlauf ersetzt keine vollständigen Clips (er wird abgelehnt, wenn das Ziel solche enthält).
Unter Windows wird bei gesperrten Dateien (z. B. Virenscanner, Indexdienst) das Verschieben/Ersetzen mehrfach wiederholt.

**`--verify`** prüft pro Clip: Dateistruktur, MXF-Felder (Codec, Labels, Dauer, Bildrate, UMIDs, Timecode, Audiospuren), ANC-Spur,
Konsistenz von `MEDIAPRO.XML` und `M01.XML` mit der MXF, Toninhalt je Spur (Vergleich mit der Quelle), H.264-Eigenschaften (Profil, Level, GOP-Länge), einen Decode-Test am
Clip-Ende und die Sony-Kopfdaten. Ohne `--source` werden die Kopfdaten gegen das Sony-Muster geprüft (auch eine mit
`--no-sony-header` erzeugte Ausgabe wird dann als abweichend gemeldet). Mit `--source` zusätzlich: Vergleich mit der Quell-MXF
(Dauer, UMIDs, Timecode, Kopfdaten, ANC, Toninhalt: erste Sekunde je Spur, bei Stille weitere Fenster), alle nicht geänderten
Nicht-MXF-Dateien byte-gleich, **fehlende Dateien sind Abweichungen**; ebenso nicht in `MEDIAPRO.XML` gelistete Quell-Clips, die im
Ziel fehlen, es sei denn, `auswahl` in `konvertierung_einstellungen.json` belegt `--unlisted skip` bzw. `--only`. Ein nicht
lesbarer Ziel-Clip ist ohne `--source` immer eine Abweichung; mit `--source` nur ein Hinweis, wenn er byte-gleich mit der Quelle
ist (die Quelle war schon nicht analysierbar und wurde unverändert kopiert). Mit `--deep` wird jeder Clip komplett dekodiert
(`ffmpeg -xerror`: ein beschädigtes Bild ist ein Befund, dazu die Bildzahl) und mit `--source` der Ton jeder Spur vollständig
mit der Quelle verglichen (Prüfsumme je Spur); das dauert.

**Abschlussprüfung** (Standard nach jeder Umwandlung, GUI und Kommandozeile): Nach dem letzten Clip wird die Zielkarte wie mit
`--verify --source --deep` geprüft. Clips mit einer Abweichung werden neu umgewandelt und erneut geprüft, höchstens `--retries`
Runden (Standard 2); der vorhandene Ziel-Clip wird erst ersetzt, wenn die neue Datei die gründliche Prüfung bestanden hat.
Fehlende oder abweichende Begleitdateien werden neu kopiert. Nicht wiederholt, aber als Abweichung gemeldet werden: Clips, die
im Ziel liegen, in diesem Lauf aber nicht ausgewählt waren (`--only`, `--unlisted skip`), Abweichungen in den XML-Dateien und
Befunde, deren Ursache die Quelle ist (z. B. Quelle nicht lesbar). Liegt im Ziel eine Testausgabe (`TESTAUSGABE.txt`), wird
nicht geprüft und das gemeldet; `--max-frames`-Läufe haben keine Abschlussprüfung. „Fertig – geprüft, ohne Abweichung“ (Exit 0)
gibt es nur, wenn die Prüfung vollständig lief und nichts fand; konnte sie nicht laufen oder wurde sie abgebrochen, sagt das
Ergebnis das ausdrücklich. Die Prüfung beachtet Abbrechen, Temperaturpause und `--threads`.

**Energiesparmodus:** Während einer Umwandlung (samt Abschlussprüfung) und bei `--verify` verhindert das Tool unter Windows, dass
der Rechner wegen Inaktivität in Standby/Ruhezustand geht; der Bildschirm darf ausgehen. Nach dem Lauf (auch nach Abbruch oder
Fehler) gilt wieder die normale Einstellung. Das Protokoll nennt es in einer Zeile. Nicht verhindert werden: Zuklappen des Deckels
(Notebook), Druck auf den Netzschalter, kritischer Akkustand und manuelles „Energie sparen“ im Startmenü – dann pausiert die
Umwandlung mit dem Rechner und läuft nach dem Aufwachen weiter bzw. lässt sich fortsetzen. In der Windows-Energieeinstellung
bleibt der Deckel-/Netzschalter-Verhalten Sache des Nutzers.

## Was wird umgewandelt, kopiert, angepasst?

- **Umgewandelt:** das Video jedes Clips (nur XAVC-I, 1920x1080 progressiv) nach H.264 High 4:2:2,
  10 Bit, Long GOP (bei 50p N=24, M=3), Bildrate aus der Quelle. Wrapping als MXF OP1a mit bmx.
- **Unverändert übernommen:** UMIDs, Dauer, Timecode (auch zusätzliche Timecode-Tracks der Quelle), Audio (Mono PCM 24 Bit/48 kHz, bitgenau;
  die Zuordnung ffmpeg-Audiostream ↔ MXF-Track wird ausdrücklich aus dem Material Package der Quelle bestimmt, nie geraten),
  ANC-Datenspur mit Kamera-Metadaten, Erzeugungsdatum, `CUEUP.XML`, `DISCMETA.XML`, `Take/*`,
  `*.BIM`, Thumbnails und alle übrigen Dateien (1:1 kopiert, auch leere Ordner).
- **Angepasst (nur Codec-Angaben):** `Material@videoType` in `MEDIAPRO.XML` und `VideoFrame@videoCodec`
  in `Clip/*M01.XML` (z. B. `AVC100CBG_1920_1080_H422IP@L42` wird zu `AVC50_1920_1080_H422P@L42`).
  Die XML-Dateien werden textbasiert geändert, Formatierung und Zeilenenden bleiben erhalten.
- **Kopfdaten der MXF (seit Runde 2):** Nach dem Wrappen gleicht ein Python-Patcher die MXF-Header-Metadaten an die
  Quell-MXF an (Identification, Track-IDs, Track-Namen, Sound-Deskriptoren, MIME-Typ des eingebetteten XML). Bei mehreren
  Identifications der Quelle ist die **erste** maßgeblich; die GenerationUID der Sound-Deskriptoren bleibt die des Ziels, `AvgBps`
  wie in der Quelle, unbekannte Verweis-Arrays werden nicht übernommen. `raw2bmx` bekommt `--kag-size-512` (wenn die Quelle KAG 512 hat),
  `--head-fill 65536` und `--seq-off` (aus der Quelle). Schlägt das
  fehl, gilt der Clip als Fehler. Ein vorhandener Ziel-Clip, bei dem nur die Kopfdaten abweichen (z. B. Ausgabe einer
  älteren Version), wird beim erneuten Lauf nur nachgezogen, nicht neu transkodiert.
- **Nur kopiert (mit Hinweis im Protokoll):** 4K, Halbbilder (Interlaced), Clips, die bereits
  Long GOP oder ein anderer Codec sind, Clips mit **Ton, der nicht Mono / 24 Bit / 48 kHz ist** (nur dieses Format wird umgewandelt)
  und Clips mit unbekannter Dauer.
- **Nicht analysierbare `.MXF`-Dateien** (Müll, 0 Byte, abgebrochene Aufnahmen) **und Clips, deren Ton-Zuordnung nicht eindeutig
  bestimmbar ist**: werden nie umgewandelt, sondern unverändert kopiert und mit Warnung („nicht analysierbar, unveraendert
  kopiert: …“) gemeldet, kein Fehler. Liegt im Ziel schon ein XAVC-L-Clip oder eine andere Datei dieses Namens, ist das ein
  **Fehler** und das Ziel bleibt unangetastet. Dateien, deren Name mit `.`
  beginnt (macOS `._*.MXF`), gelten nicht als Clips, sondern werden wie Begleitdateien kopiert.
- **Clips ohne Eintrag in `MEDIAPRO.XML` und ohne `M01.XML`:** werden umgewandelt (Standard), wenn die MXF eindeutig XAVC-I ist; es
  gibt dann kein XML anzupassen, das H.264-Level folgt der Bildrate (bis 30 fps 4.1, darüber 4.2). Mit `--unlisted skip` werden
  nicht gelistete Clips übersprungen.
- Das Quell-XDROOT wird nie verändert. Das Ziel muss ein anderer Ordner sein.

Details und Messwerte: [docs/SPEC.md](docs/SPEC.md).

## Getesteter Stand

**Bitte ehrlich lesen** (Stand 16.11.2026, Einzelheiten und Zahlen: [docs/NAECHSTE-SCHRITTE.md](docs/NAECHSTE-SCHRITTE.md)):

- **Mit echtem Kameramaterial unter Windows getestet** (XAVC-I HD 50p):
  - Sony PMW-F5: 19 Clips (18,3 GB) umgewandelt, `--verify --source --deep` ohne Abweichung.
  - F55-Karte: alle 76 Clips (104 GB) umgewandelt, `--verify --source --deep` ohne Abweichung, alle Begleitdateien verglichen.
    Ein Clip schlug im ersten Durchgang nach einem Hänger beim Lesen vom Kartenleser fehl, wurde als Fehler gemeldet und im
    Fortsetzungslauf fehlerfrei umgewandelt.
  - Beide Karten wurden nach der Verschärfung der Prüfung (Decode mit `-xerror`, Ton je Spur vollständig verglichen) erneut
    geprüft: 19 von 19 und 76 von 76 Clips ohne Abweichung (die F55-Ausgabe gegen eine Kopie der Originalkarte).
  - Eine weitere F55-Karte (6 Clips, 67,9 GB) mit einem **52,7-GB-Clip** (31:42 min, WAV-Dateien über 4 GB) lief in der GUI
    durch; die automatische Abschlussprüfung meldete 6 von 6 Clips ohne Abweichung.
  - **Abschlussprüfung:** Ein gezielt beschädigter Ziel-Clip (Bilddaten bzw. Ton überschrieben) wird gefunden, automatisch neu
    umgewandelt und danach ohne Abweichung geprüft; ein beschädigter, im Lauf nicht ausgewählter Clip führt zu Exit-Code 1.
  - Parser und Kopfdaten-Patcher lesen echte F5-, F55- und FX6-Dateien fehlerfrei; der Toninhalts-Vergleich funktioniert mit
    echtem Sony-Ton.
  - Die Quell-Laufwerke blieben unverändert (Vergleich aller Dateien vor und nach den Tests).
- **Vergleich mit echtem XAVC-L (Sony FX6):** `scripts/mxf_vergleich.py` mit Quelle, Ziel und FX6-Referenz
  ([docs/analysis/vergleich_f55_ziel_fx6.md](docs/analysis/vergleich_f55_ziel_fx6.md)). Hersteller-Eintrag, Track-IDs, Spuraufbau
  und Ton-Deskriptoren stimmen mit der Quelle überein; zur Quelle bleiben 20 Abweichungen, alle durch den Codec-Wechsel.
  Gegenüber der FX6 bleiben: bmx-Partitionslayout (vier statt zwei Partitionen), einzelne Angaben im `AVCSubDescriptor`
  (beschreiben den x264-Datenstrom), ein Sony-eigener Unter-Deskriptor, und das eingebettete XML (nur mit `--embed-xml always`).
- **Robustheit unter Windows getestet:** Strg+C (Exit-Code 130, aufgeräumt), Fortsetzen, hartes Beenden mit Wiederaufnahme,
  zwei Läufe in dasselbe Ziel (der zweite bricht ab), Fortsetzen nach einem Absturz des Rechners, Nachziehen der Kopfdaten
  ohne neues Transkodieren, Start über `XDROOT-Converter.bat`.
- **Import geprüft (vom Autor, von Hand):** Die umgewandelte F55-Karte wird in **DaVinci Resolve** und in **Sony Catalyst
  Browse** als Karte erkannt; Wiedergabe und Tonspuren sind in Ordnung. Die Timecode-Anzeige in diesen Programmen wurde dabei
  nicht verglichen (in den Dateien stimmt der Timecode laut `--verify --source` mit der Quelle überein).
- **Nicht getestet:** andere Schnittprogramme (Avid, Premiere, …) und andere Sony-Werkzeuge, die GUI-Bedienung von Hand in allen
  Wegen und andere Bildraten als 50p mit echten Kameraclips (25p, 29.97p, 59.94p nur synthetisch).
- **Temperaturüberwachung:** Logik, Pause und der Weg ohne Administratorrechte sind getestet. Auf einem **Lenovo Legion Pro 7
  16IRX8H** zeigte `--thermal-test` mit Administratorrechten: Temperatur (75–86 °C, schwankend) und Lüfterdrehzahl sind über
  `GetFeatureValue` lesbar, „Lüfter voll“ wirkt (3600 → 7300 U/min) und wird zurückgestellt. Die älteren Abfragen
  (`GetCPUTemp`, `GetFan1Speed`) und die Windows-Thermalzonen liefern auf diesem Modell nichts. Andere Modelle: unbelegt.
  Noch nicht beobachtet: ein ganzer Lauf mit Überwachung, die Pause bei echter Überhitzung.
- **Synthetische Tests (Linux):** Umwandlung, Fortsetzen, `--embed-xml always`, nicht gelistete Clips, unlesbare `.MXF`-Dateien
  mit einer mit bmx erzeugten Testkarte (`scripts/make_test_card.py`). `MXFDump` meldet bei KAG 512 gelegentlich „Warning :
  Length is zero (following KLVFill key …)“: bekannte, harmlose Eigenheit von bmx/libMXF.
- **Review-Stand:** Jede Runde wurde von Opus-Reviewern geprüft, bis keine groben Fehler mehr gemeldet wurden. Das sind
  Code-Reviews, keine Tests mit Sony-Werkzeugen.

## Temperaturüberwachung und Schonmodus

Lange Umwandlungen lasten die CPU voll aus. Zwei Hilfen:

- **Schonmodus (jeder Rechner):** „CPU-Kerne begrenzen“ in den Einstellungen bzw. `--threads N`. Weniger Wärme, längere Laufzeit.
- **Temperaturüberwachung (derzeit nur Lenovo Legion, nur mit Administratorrechten):** Start über
  `XDROOT-Converter (Administrator).bat`. Die Oberfläche zeigt CPU-Temperatur und Lüfterdrehzahl und fordert ab 85 °C volle Lüfter
  an; **die Lüfter bleiben dann bis zum Ende des Laufs voll** (kein Hin und Her) und werden erst am Laufende zurückgestellt.
  **Drosseln und Pausieren sind seit dem 17.11.2026 standardmäßig aus** (siehe „Befund“ unten) und nur mit dem Schalter „Bei
  Hitze drosseln und pausieren“ bzw. `--thermal-throttle` aktiv; ohne ihn wird nur gemessen, angezeigt, der Lüfter geschaltet
  und die höchste gemessene Temperatur in der Zusammenfassung genannt. Mit dem Schalter gilt: Ab 97 °C wird **zuerst die Last gedrosselt**: `ffmpeg` darf nur noch 75 %, 50 %, 35 % und zuletzt 25 % der logischen Kerne
  benutzen (mindestens 2; nach einem Schritt nach unten frühestens nach 30 s der nächste, nach einem Lockern darf sofort wieder gedrosselt werden; die Anzeige nennt z. B. „gedrosselt auf 24 von 32 Kernen“).
  Neu gestartete Hilfsprogramme laufen mindestens 3 s Laufzeit ungedrosselt (bis zur nächsten Messung, also etwa 3–8 s), damit `ffmpeg`/x264 ihre Threadzahl wie ohne
  Drosselung wählen; die Drosselung soll den Datenstrom nicht beeinflussen (Beleg mit echtem Material steht aus). Pausenzeit
  zählt nicht zur Verzögerung. Grenze (unbelegt): Läuft eine Quelle sehr langsam an, könnte der Encoder später starten.
  Bleibt die CPU auch auf der niedrigsten Stufe an der Schwelle, oder liegt sie 5 °C darüber (beim Standardwert 102 °C, über
  der Abschaltgrenze vieler CPUs, daher praktisch nie erreicht), wird die Umwandlung als letzte Stufe angehalten, bis die CPU
  unter 85 °C abgekühlt ist; danach geht es eine Stufe tiefer weiter. Bleibt sie mindestens 8 °C unter der Schwelle (60 s am Stück), wird
  stufenweise wieder gelockert; eine Stufe, die kurz nach dem Lockern wieder zu heiß wurde, wird 15 Minuten lang nicht mehr versucht. Kühlt die
  CPU nach 20 Minuten Pause nicht ab, wird der Lauf sauber abgebrochen und kann später fortgesetzt werden. Das Drosseln
  ändert nur die Kernzuteilung der vom Tool gestarteten Hilfsprogramme (`ffmpeg`, `ffprobe`, `raw2bmx`, `mxf2raw`; Prozess-Affinität,
  endet mit dem Prozess) und soll das Ergebnis nicht beeinflussen (Beleg mit echtem Material steht aus); ist es
  nicht möglich (z. B. mehr als 64 Kerne), wird wie früher sofort pausiert. Die Zusammenfassung nennt die gedrosselte und die
  pausierte Zeit. Ohne Administratorrechte oder auf anderen Geräten steht dort „Temperatur nicht verfügbar“, und die
  Umwandlung läuft wie gewohnt (ohne Messung wird nie gedrosselt).
- **Befund vom 17.11.2026 (Legion Pro 7 16IRX8H, Leistungsmodus):** Mit eingeschaltetem Drosseln erreichte die Messung auch auf
  8 von 32 Kernen wieder 97 °C; der Lauf endete in einer Kette von mehr als 20 Pausen und kam kaum voran. Ohne Überwachung lief
  derselbe Clip mit voller Leistung durch (Lenovo Vantage zeigte etwa 87 °C). Der Messwert lag in 8 Vergleichsablesungen 4 bis
  14 °C (meist etwa 10 °C) über der Anzeige von Vantage (nicht exakt gleichzeitig abgelesen). Die CPU arbeitet unter Volllast
  bestimmungsgemäß nahe ihrer Grenze und regelt sich selbst; der Schalter ist nur nötig, wenn der Rechner unter Volllast abstürzt.
- **Vor dem ersten Einsatz:** `Temperatur-Test (Administrator).bat` ausführen. Der Test zeigt, ob Messung und „Lüfter voll“ auf
  dem Gerät wirken. Das Tool probiert mehrere nur lesende Temperaturquellen der Reihe nach (Lenovo `GetCPUTemp`, Lenovo
  `GetFeatureValue`, ACPI-Thermalzone, Windows-Leistungszähler) und nimmt die erste mit plausiblem Wert; grobe Quellen
  (Thermalzonen) werden in der Anzeige als solche gekennzeichnet. Die Lenovo-Kennungen stammen aus einem Open-Source-Projekt
  und sind nur auf dem oben genannten Modell bestätigt; ohne messbare Wirkung zeigt das Tool nur „Lüfter voll angefordert“.
- **Andere Geräte:** In `xdroot_converter/thermal.py` lässt sich ein eigener Anbieter ergänzen (Schnittstelle und Beispiel im
  Modul). Der Lenovo-Leistungsmodus wird bewusst nie geschaltet.
- Wird das Programm hart beendet oder stürzt der Rechner ab, während die Lüfter voll laufen, stellt das Tool den vorherigen
  Zustand wieder her, sobald es das nächste Mal mit Administratorrechten und Temperaturüberwachung läuft.
  Dafür merkt es sich den Zustand in `%LOCALAPPDATA%\XDROOT-Converter\fan_state.json` (wird nach dem Zurückstellen gelöscht).
- Mit Administratorrechten sieht das Programm Netzlaufwerke des normalen Benutzers in der Regel nicht.

Kommandozeile: `--thermal auto|off`, `--temp-fan GRAD`, `--thermal-throttle`, `--temp-pause GRAD`, `--threads N`, `--thermal-test`.

## Einschränkungen

- Das Ergebnis ist **nicht bitgenau** wie eine Kameraaufnahme: Die Kopfdaten (Identification, Track-IDs, ...) werden
  zwar aus der Quelle übernommen, die Datei bleibt aber eine mit bmx erzeugte MXF (z. B. neue Generation-UID,
  bmx-Partitionslayout, Fill-Größen; KAG 512 nur, wenn die Quelle KAG 512 hat). Felder, die `mxf2raw` nicht zeigt und die der Patcher nicht kennt
  (Sony-eigene Sets u. Ä.), werden nicht übernommen.
- Import und Wiedergabe wurden nur in **DaVinci Resolve** und **Sony Catalyst Browse** geprüft (von Hand, eine Karte); andere
  Schnittprogramme (Avid, Premiere, ...) und Sony-Prüfwerkzeuge nicht.
- Geschwindigkeit: etwa Echtzeit bei HD 50p mit Preset `medium` auf einem aktuellen Notebook (gemessen: 11:01 min Material in
  11:47 min). Hardware-Encoder der Grafikkarte werden nicht benutzt; H.264 mit 4:2:2 und 10 Bit bieten nur wenige an.
- 4K, Interlaced und Ton, der nicht Mono/24 Bit/48 kHz ist, werden nur kopiert, nicht umgewandelt.
- Clips, deren Tonspuren unterschiedlichen `SequenceOffset` haben, werden nicht umgewandelt (nur kopiert, Hinweis). Lässt sich die
  Zuordnung ffmpeg-Audiostream ↔ MXF-Track nicht eindeutig bestimmen, wird der Clip **nicht umgewandelt**, sondern unverändert
  kopiert (Warnung, kein Raten, wegen der Gefahr vertauschter Tonspuren); ein vorhandener Ziel-Clip führt zum Fehler.
- Andere Bildraten als 50p nur synthetisch getestet (siehe oben).
- Zwei Läufe gleichzeitig in dasselbe Ziel sind gesperrt.
- `--only` ist ein Testwerkzeug: `MEDIAPRO.XML` im Ziel listet weiter alle Clips der Quelle.
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
