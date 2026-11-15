# Spezifikation XDROOT-Converter

## Ziel

Der Nutzer wählt in einer GUI einen **Quellordner** und einen **Zielordner** und klickt
**Konvertieren**. Der Quellordner ist ein Sony-XDROOT (oder dessen Elternordner) mit
XAVC-Intra-Clips. Im Zielordner entsteht ein `XDROOT` mit identischer Struktur, in dem
jeder Clip als XAVC-L (H.264 High 4:2:2, Long GOP, 10 Bit, 50 Mbit/s) vorliegt und alle
Metadaten konsistent sind – so, als hätte eine Kamera direkt XAVC-L aufgezeichnet.

## Gemessene Fakten (aus `docs/analysis/`, bitte dort nachschlagen)

### Kartenstruktur (F55 und FX6 gleich)

```
XDROOT/
  Clip/      <name>.MXF, <name>M01.XML (NonRealTimeMeta), optional <name>R01.BIM
  Take/      *.SMI, *M01.XML   (optional)
  Sub/ Edit/ General/ Thmbnl/ UserData/   (oft leer; Thmbnl kann <name>T01.JPG enthalten)
  MEDIAPRO.XML  CUEUP.XML  DISCMETA.XML
```

Dateinamen können Leerzeichen enthalten (`PDW F5_0152.MXF`).

### Unterschiede Quelle (XAVC-I) → Ziel (XAVC-L)

| Stelle | Quelle F55 | Ziel (wie FX6) |
|---|---|---|
| `MEDIAPRO.XML` `Material@videoType` | `AVC100CBG_1920_1080_H422IP@L42` | `AVC50_1920_1080_H422P@L42` |
| `Clip/*M01.XML` `VideoFrame@videoCodec` | `AVC100CBG_1920_1080_H422IP@L42` | `AVC50_1920_1080_H422P@L42` |
| MXF essence_type (mxf2raw) | `AVCI_100_1080p` | `AVC_High_422` |
| MXF coding_label | `…0401010a.04010202.01323104` | `urn:smpte:ul:060e2b34.0401010d.04010202.01316001` |
| MXF ec_label | `…0d010301.02106001` | identisch |
| H.264-Profil | High 4:2:2 Intra, L4.2 | High 4:2:2, L4.2, yuv422p10le, bt709 |
| GOP (FX6 gemessen) | nur I | N=24, M=3 (I BB P BB P …), ~47–50 Mbit/s |

Alles andere bleibt **unverändert**: UMIDs, Dauer (`dur`, `Duration`), Timecode
(`LtcChangeTable`), fps, Audio (N × Mono PCM 24 Bit/48 kHz, `LPCM24`), Device, Lens,
`CUEUP.XML`, `DISCMETA.XML`, `Take/*`, `*.BIM`, Thumbnails.

### MXF-Details, die übernommen werden müssen

- OP1a, frame-wrapped. Material-Package-UMID = `umid` aus MEDIAPRO.XML (= mxf2raw
  `MaterialPackage.package_uid`); File-Source-Package-UMID aus der Quell-MXF (mxf2raw).
  Beide per `raw2bmx --mp-uid/--fp-uid` exakt übernehmen (aus der Quell-MXF lesen,
  nicht raten).
- Start-Timecode aus der Quell-MXF (`StartTimecodes.material`) → `raw2bmx -y`.
- Jede Audiospur der Quelle als eigene Mono-Spur, gleiche Reihenfolge, 24 Bit/48 kHz,
  `--locked true --audio-ref 0` (wie Referenz).
- Die Quelle enthält eine ANC-Datenspur (ST 436, RDD18 Kamera-Metadaten) und ein
  eingebettetes XML (TextObject, scheme_id `urn:uuid:786d4d22-8280-11de-8a39-08004678031c`,
  = NonRealTimeMeta). Übernahme ist **Best Effort**:
  - ANC: per `mxf2raw --ess-out … --wrap-klv` extrahieren und per `raw2bmx --klv … --anc`
    einbinden, falls das zuverlässig funktioniert; sonst weglassen und im Log warnen.
  - XML: das (angepasste) `M01.XML` per `--xml-scheme-id … --embed-xml` einbetten.
- Erzeugungsdatum: `--create-date` aus dem Quell-MXF übernehmen (falls möglich).
- Descriptor-Angaben wie Referenz setzen, soweit raw2bmx es erlaubt
  (`--signal-std st274`, `--color-siting cositing`, Seitenverhältnis 16:9).

### Bewährter Kern (Proof of Concept, funktioniert)

```
ffmpeg -i SRC.MXF -map 0:v:0 -c:v libx264 -profile:v high422 -pix_fmt yuv422p10le
  -level:v 4.2 -preset medium -b:v 50M -maxrate 50M -bufsize 50M -g 24 -bf 2
  -x264-params "aud=1:nal-hrd=vbr:keyint=24:min-keyint=24:scenecut=0:b-pyramid=none:colorprim=bt709:transfer=bt709:colormatrix=bt709:force-cfr=1"
  -f h264 v.h264   [+ je Audiospur: -map 0:a:N -c:a pcm_s24le aN.wav]
raw2bmx -t op1a -o OUT.MXF -f 50 -y <tc> --mp-uid <umid> --fp-uid <umid>
  --avc_high_422 v.h264 --wave a0.wav --wave a1.wav …
```

Ergebnis laut mxf2raw: `AVC_High_422`, coding_label und ec_label identisch zur FX6.
(raw2bmx meldet „AVC support is work-in-progress" – nur eine Warnung.)

## Funktionsumfang

### Ablauf pro Karte

1. Quelle prüfen: `XDROOT` finden (gewählter Ordner selbst oder Unterordner),
   `MEDIAPRO.XML` lesen. Ziel darf nicht in/gleich Quelle liegen. Freien Speicher schätzen
   (≈ 25 % der Quell-MXF-Größe + temporär dasselbe nochmals) und bei Mangel abbrechen.
2. Zielstruktur anlegen, alle Nicht-MXF-Dateien 1:1 kopieren (inkl. leerer Ordner,
   Zeitstempel erhalten).
3. Je `Clip/*.MXF`:
   - Mit ffprobe/mxf2raw analysieren. **Nur umwandeln, wenn** Video = H.264 Intra
     (XAVC-I) 1920×1080. Die Bildrate wird aus der Quelle übernommen (25p/50p/29.97p/59.94p
     usw.; GOP = ca. 0,5 s, bei 50p N=24). Clips, die schon Long GOP oder ein anderer Codec
     sind (z. B. `MPEG2HD50…`, 4K-XAVC), werden **unverändert kopiert** und im Log genannt.
     4K-Umwandlung ist nicht Teil von Version 1. Dasselbe gilt für Quellen mit unbekannter Dauer. **Nicht analysierbare**
     `.MXF`-Dateien (Müll, 0 Byte, abgebrochene Aufnahme) und Clips mit nicht eindeutig bestimmbarer Ton-Zuordnung
     (`plan.error`) werden nie umgewandelt: ohne Ziel-Clip unverändert kopiert, Warnung „nicht analysierbar, unveraendert kopiert:
     …“, kein Fehler; liegt im Ziel ein XAVC-L-Clip oder eine andere Datei dieses Namens, ist es ein Fehler und das Ziel bleibt
     unangetastet. Dateien, deren Name mit `.` beginnt (macOS
     `._*.MXF`), sind keine Clips und werden wie Begleitdateien kopiert.
   - Transcode + Wrap in einen temporären Ordner im Ziel (`_tmp_xdroot_converter`),
     dann atomar nach `Clip/` verschieben; temporäre Dateien löschen. Große Clips
     (56 GB, 31 min) müssen funktionieren: Video-Elementarstream liegt temporär auf
     der Zielplatte (~12 GB), das ist akzeptiert.
   - Verifizieren: Dauer (Frames) Ziel == Quelle, UMIDs gleich, Start-TC gleich,
     Anzahl Audiospuren gleich, Toninhalt je Spur gleich (siehe „Tonspuren“ unten), essence_type `AVC_High_422`. Bei Abweichung: Clip als
     fehlgeschlagen markieren, Fehler klar melden.
4. XML anpassen (textbasiert, damit Formatierung/Namespaces/Zeilenenden der Kamera
   erhalten bleiben – kein Re-Serialisieren mit ElementTree):
   - `MEDIAPRO.XML`: `videoType` nur bei den tatsächlich umgewandelten `Material`-Einträgen.
   - `Clip/<name>M01.XML`: `videoCodec`.
   - Ziel-Codec-String aus Quell-String ableiten: `AVC100CBG_1920_1080_H422IP@L42` →
     `AVC50_1920_1080_H422P@L42`; Level aus Quelle übernehmen (`@L41` bei ≤30p → `@L41`).
     Unbekannte Muster: nicht raten, Clip kopieren statt wandeln und warnen.
   - Clip ohne Codec-String (weder `M01.XML` noch `MEDIAPRO.XML`-Eintrag), MXF aber eindeutig XAVC-I: wird umgewandelt,
     es gibt kein XML anzupassen. Das H.264-Level kommt aus der **Bildrate** (bis einschließlich 30 fps 4.1, darüber 4.2);
     das ffprobe-Level der Quelle wird nicht verwendet (eine synthetische Quelle meldet dort z. B. 50, kein gültiges Sony-Format).
     Die Prüfung (`ClipExpect.level`) erwartet dann dasselbe Level; `--verify` prüft das Level nur, wenn ein Codec-String vorhanden ist.
5. Abschlussbericht: pro Clip Status, Größe vorher/nachher, Gesamtersparnis, Log-Datei
   `konvertierung.log` im Zielordner (neben `XDROOT`).

### Robustheit

- Abbrechen-Knopf: laufende Prozesse beenden, temporäre Dateien entfernen; bereits
  fertige Clips bleiben. Erneuter Lauf überspringt fertige, verifizierte Clips
  (Fortsetzen möglich).
- Fehler eines Clips stoppen nicht die ganze Karte; am Ende klare Zusammenfassung.
- Subprozesse ohne aufblitzende Konsolenfenster (`CREATE_NO_WINDOW`), Argumente als
  Liste (keine Shell), Pfade mit Leerzeichen/Umlauten funktionieren.
- GUI bleibt bedienbar (Arbeit im Thread, Fortschritt per Queue). Fortschritt: Balken
  pro Clip (aus ffmpeg `-progress`) und gesamt, Restzeit grob.

### GUI (tkinter, Deutsch)

- Felder „Quellordner" und „Zielordner" mit „Durchsuchen…", Knopf **Konvertieren**,
  Knopf „Abbrechen", Fortschrittsbalken, Log-Fenster.
- Einstellungen (eingeklappt/Defaults sinnvoll): Bitrate (Standard 50 Mbit/s),
  x264-Preset (Standard `medium`).
- Nach Quellwahl kurze Übersicht: Kamera (`systemKind`), Anzahl Clips, davon umzuwandeln,
  Gesamtgröße, geschätzte Zielgröße.
- Start per Doppelklick: `XDROOT-Converter.bat` (startet die GUI ohne Konsole: `pyw -3`, sonst `pythonw.exe` neben dem per `where python` gefundenen Python; Ausweichweg
  `python -m xdroot_converter`).

### Projektstruktur

```
xdroot_converter/   __init__.py, __main__.py (GUI-Start), gui.py, cli.py,
                    converter.py (Ablauf), card.py (XDROOT/XML lesen+patchen),
                    mxfinfo.py (ffprobe/mxf2raw-Parsing), tools.py (Programme finden),
                    mxfheader.py (MXF-Header-Metadaten lesen/ändern, Runde 2),
                    verify.py (Prüfung Einzelclip + Karte)
setup_tools.bat/.ps1  FFmpeg + bmx nach tools/ laden
scripts/            make_test_card.py (synthetische XAVC-I-Testkarte),
                    mxf_vergleich.py (Satz-für-Satz-Vergleich der Header-Metadaten)
tests/              unittest (ohne Medien; Fixtures: Kopien aus docs/analysis, Hilfen in helpers.py):
                    test_card, test_converter, test_run (Ablauf mit Attrappen für ffmpeg/raw2bmx),
                    test_verify, test_mxfheader, test_mxfinfo, test_gui
docs/               SPEC.md, MXF-VERGLEICH.md, NAECHSTE-SCHRITTE.md, analysis/
XDROOT-Converter.bat (prüft Python ≥ 3.11 + tkinter, startet die GUI), README.md (Deutsch), CONTRIBUTING.md, .gitignore
```

CLI: `python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium]
[--max-frames N] [--unlisted convert|skip] [--only TEXT] [--embed-xml auto|always|never] [--no-anc]
[--no-sony-header] [--verbose]`, `--verify <ziel-xdroot> [--source <quell-xdroot>] [--deep]` und
`--mxf-info <datei.MXF>` (Header-Metadaten ausgeben).
`--max-frames` (nur Test) wandelt nur die ersten N Frames und passt dann
`dur`/`Duration` **nicht** an – Ausgabe als Testausgabe kennzeichnen.

Nur Python-Standardbibliothek (Python 3.11), keine pip-Abhängigkeiten (Tests mit `unittest`, pytest ist nicht installiert).

## Abnahme

- `python -m unittest discover -s tests` grün.
- Echte Umwandlung der lokalen F5-Testkarte (Pfad siehe `local_paths.md`, nicht im Repo; F5, XAVC-I HD 50p, 4 Clips) nach
  `_test_out/` läuft durch; `--verify` meldet keine Abweichungen; mxf2raw-Ausgabe eines
  Ziel-Clips stimmt in den oben genannten Feldern mit `docs/analysis/fx6_mxf2raw.txt`
  überein.

## Umsetzungsstand / Messwerte (Version 1.0)

- Audio wird mit `raw2bmx --aes-3` gewickelt: ec_label `…0d010301.02060300` und Track-Nummern `0x16NN03xx` wie bei Sony.
- ANC-Spur: per `mxf2raw --ess-out … --wrap-klv d --disable-audio --disable-video` + `raw2bmx --klv s --anc` byte-identisch übernommen (auch 13-Elemente-Manifest der FX6 bleibt erhalten).
- Eingebettetes XML (`--embed-xml`, `--xml-scheme-id`, `--xml-lang und`) funktioniert, wird aber nur eingebettet, wenn die Quelle auch ein TextObject hat (`--embed-xml auto`, Standard). F5/F55-Quellen haben keins.
- `--create-date` (Format `YYYY-MM-DDThh:mm:ss:qm`) übernimmt das Datum der Quell-Identification; `--signal-std st274`, `--color-siting cositing` werden aus der Quelle abgeleitet.
- Mit raw2bmx allein (Version 1) nicht erreichbar, seit Runde 2 per Nachbearbeitung gelöst (siehe „Umsetzungsstand Runde 2"): Identification (bmx statt Sony „Mem "), Track-IDs (1001/2001/3001 statt 2,3,…), mime_type des TextObject (`application/xml` statt `text/xml`), TextObject-description.
- Seit den Fix-Runden zusätzlich an `raw2bmx`: `--kag-size-512` (wenn die Quelle KAG 512 hat; sonst bmx-Standard KAG 1), `--head-fill 65536`
  (Fill-Reserve für den Patcher) und `--seq-off` (aus der Quelle).
- ffmpeg braucht `-color_range tv` (sonst markiert x264 den Strom als Full Range).
- Auf der Testkarte liegen 147 MXF, `MEDIAPRO.XML` listet nur 19. Option `--unlisted skip` bzw. GUI-Haken.

## Runde 2: MXF-Kopfdaten wie Sony (Anforderung des Nutzers, verbindlich)

Die Ziel-MXF soll in den Kopfdaten so aussehen wie eine Sony-Kameradatei, nicht wie
eine bmx-Datei. Maßstab ist `docs/analysis/fx6_mxf2raw.txt` (Ziel) bzw. die Quell-MXF.

1. **Identification wie Sony**: `company_name` = `Sony`, `product_name` = `Mem ` (mit
   Leerzeichen am Ende), `version_string` = `2.00`, `product_uid` =
   `cede1104-8280-11de-8a39-08004678031c` – jeweils aus der **Quell-MXF** übernehmen
   (nicht fest verdrahten; Fallback auf diese Werte). Kein „BBC/bmx" mehr sichtbar
   (auch toolkit/platform prüfen und an die Referenz angleichen, soweit vorhanden).
   Erster Versuch: `raw2bmx --prod-info`.
2. **Spuren wie Sony**: Track-IDs wie in der Quelle/Referenz (Video 2, Audio 3…N,
   ANC danach; siehe `track_id` in `docs/analysis/f55_mxf2raw.txt`) statt
   1001/2001/3001. Track-Nummern, Reihenfolge und Audio-Descriptor-Felder exakt wie
   in der Quell-MXF. Die Tonspuren dürfen in keinem von mxf2raw/ffprobe/MXFDump
   sichtbaren Feld von der Quelle abweichen (außer wo Sony selbst zwischen XAVC-I und
   XAVC-L unterscheidet).
3. **Weg dorthin** (in dieser Reihenfolge prüfen):
   a) vorhandene raw2bmx-Optionen;
   b) Nachbearbeitung der fertigen MXF durch einen eigenen Python-Patcher
      (Header-/Footer-Metadaten sind KLV mit festen Feldlängen: Track-ID,
      SourceTrackID, LinkedTrackID, Identification-Strings bei gleicher Länge bzw.
      mit sauberem Neuschreiben der Partition) – muss Header- **und** Footer-Partition
      sowie alle Verweise konsistent ändern;
   c) bmx (BSD-Lizenz, https://github.com/bbc/bmx) forken, anpassen und selbst
      bauen; der Fork wird dann von `setup_tools` bezogen.
   Die einfachste Variante wählen, die ein strukturell sauberes MXF ergibt.
4. **Vollständiger Strukturvergleich**: Mit `MXFDump` (liegt bei bmx) die
   Header-Metadaten von Quelle (F55), Referenz (FX6) und Ziel Satz für Satz
   vergleichen und jede verbleibende Abweichung in `docs/MXF-VERGLEICH.md`
   auflisten und begründen (behoben / bewusst belassen / nicht machbar).
   `verify` prüft die neuen Felder mit.
5. README: offen dokumentieren, dass die Dateien Sony-Kennungen der Quellkamera
   tragen, aber von diesem Tool erzeugt wurden.

## Umsetzungsstand Runde 2

Gewählter Weg (Reihenfolge a → b → c aus Punkt 3):

- **a) vorhandene raw2bmx-Optionen – reicht nicht.** `--prod-info` setzt Company, Product, Version-String und
  Product-UID (und lässt mit `0.0.0.0.0` die Product-Version weg) und wird genutzt. Aber: `ToolkitVersion` und
  `Platform` der Identification lassen sich nicht abschalten (bmx schreibt sie immer), und die Track-IDs sind in bmx fest
  verdrahtet (`OP1AFile.cpp`: 901 Timecode, 1001 Video, 2001.. Audio, 3001 ANC).
- **b) Python-Patcher – gewählt.** `xdroot_converter/mxfheader.py` parst die Header-Metadaten (Primer + Local Sets) der
  fertigen MXF, ändert sie und schreibt sie in place zurück. Die Länge der Header-Region bleibt gleich (der letzte KLV-Fill
  wird gekürzt/verlängert), daher bleiben `HeaderByteCount`, alle Offsets und der Index gültig; alle Partitionen mit Header-Metadaten
  werden behandelt; vor dem Schreiben wird erneut geparst und validiert (bei Fehler bleibt die Datei unverändert).
  `adopt_from_source(ziel, quelle)` gleicht an die Quell-MXF an (Quelle wird nur gelesen): Identification (inkl. Entfernen von
  Toolkit/Platform), Track-IDs mit allen Verweisen (SourceTrackID, LinkedTrackID), Track-Reihenfolge und -Namen, Sound-Deskriptor-Items,
  MIME-Typ/Description des TextObjects. `compare_with_source` ist der zugehörige Trockenlauf für `verify`.
- **c) bmx-Fork – nicht nötig.** Es bleibt beim offiziellen bbc/bmx-Windows-Paket (`setup_tools.ps1` unverändert bis auf die
  optionale Prüfung von `MXFDump.exe`); es ist **kein eigener Windows-Build** nötig.

Ablauf pro Clip (`converter.py`):

1. `raw2bmx` mit `--prod-info` aus der Quell-MXF (Rückfall Sony / `Mem ` / 2.00 / `cede1104-8280-11de-8a39-08004678031c`; das
   Leerzeichen im Produktnamen bleibt erhalten).
2. `mxfheader.adopt_from_source(out.mxf, quelle)`; Änderungen werden als Info geloggt, Einträge mit „WARNUNG:" als Warnung.
   Schlägt es fehl (`HeaderPatchError`, `MxfParseError`, `OSError`), gilt der Clip als **FEHLER** („MXF-Kopfdaten konnten nicht
   an die Quelle angeglichen werden: …"), er wird nicht stillschweigend übernommen.
3. Prüfung `verify.check_clip(..., source=quelle)`: bisherige Prüfungen plus `check_header` (`compare_with_source` und
   mxf2raw-Felder: Identification, Track-IDs Material/File Package, Track-Nummern der Tonspuren, Audio-Deskriptor-Felder).
4. `Options.sony_header = False` / `--no-sony-header` schaltet 1.–3. ab (Warnung im Log: Datei trägt bmx-Kennungen).

Fortsetzen: Besteht ein vorhandener Ziel-Clip alle bisherigen Prüfungen und weichen nur die Kopfdaten ab (z. B. Ausgabe von
Version 1), wird er **nicht** neu transkodiert, sondern `adopt_from_source` direkt auf den Ziel-Clip angewendet, erneut
geprüft, als „Kopfdaten nachgezogen" geloggt und übersprungen. Schlägt das fehl, wird neu erzeugt.

`verify`:

- mit `--source`: dieselben Kopfdaten-Vergleiche je umgewandeltem Clip.
- ohne `--source`: Plausibilität gegen das Sony-Muster (Company nicht `BBC`, Product nicht `bmx`, kein Toolkit/Platform,
  Video-Track-ID 2, Audio 3..N+2, ANC N+3 wie `fx6_mxf2raw.txt`); Abweichung = ABWEICHUNG. Eine bewusst mit `--no-sony-header`
  erzeugte Ausgabe hat keine Markierung und wird daher ohne `--source` ebenfalls als abweichend gemeldet.
- Fehlalarm behoben: Ein umgewandelter Clip, der nicht in `MEDIAPRO.XML` steht, wird für den XML-Vergleich anhand seiner
  Ziel-`M01.XML` (videoCodec XAVC-L) als umgewandelt erkannt und gegen `patch_clip_xml(quelle, codec)` verglichen. Fehlt die
  `M01.XML` auch in der Quelle, ist das nur ein Hinweis.

Messwerte (synthetische Karte, 3 Clips, Details und Vergleichsbericht in [MXF-VERGLEICH.md](MXF-VERGLEICH.md)):
Ausgabe von Version 1: 117 Abweichungen zur Quelle in 45 Sätzen; Runde 2: 30 Abweichungen in 3 Sätzen, alle im Video-Deskriptor
(gewollt). `mxf2raw` des Ziels: Identification Sony/`Mem `/2.00/UID, kein Toolkit/Platform, Track-IDs 2, 3..10, 11.

Grenzen: Belegt nur gegen eine **synthetische** Quelle (mit bmx erzeugt, auf Sony-Track-IDs gesetzt) und gegen die
`mxf2raw`-Ausgaben echter Dateien; `MXFDump`-Ausgaben echter Kameradateien liegen nicht im Repository. Siehe Hinweis oben in
`docs/MXF-VERGLEICH.md`. Fehlt noch: lokaler Lauf von `scripts/mxf_vergleich.py` mit F55-Quelle, FX6-Referenz und Ziel.

## Umsetzungsstand nach den Fix-Runden (Runde 2, ergänzt)

Alles unten ist nur mit **synthetischem Material unter Linux** belegt (siehe `README.md`, „Getesteter Stand“). Die Testanzahl
steht bewusst nicht hier; maßgeblich ist die Ausgabe von `python -m unittest discover -s tests`.

### Patcher (`mxfheader.py`)

- **KAG 512:** Bei KAG > 1 steht zwischen Partition Pack und Primer ein KLV-Fill. `HeaderByteCount` zählt ab dem Primer; der
  Fill vor dem Primer bleibt byte-genau unverändert. Ohne RIP und bei offenem Header (FooterPartition = 0) wird nur gelesen.
- **Regeln der Übernahme** (`adopt_from_source`, Quelle wird nur gelesen):
  - Identification: Company, Product, Version, Product-UID aus der Quelle; Toolkit/Platform entfallen; `ThisGenerationUID` bleibt je Datei neu.
  - Spurpaarung zwischen Quelle und Ziel über die **TrackNumber** (nicht nur über die Reihenfolge); Track-IDs, Reihenfolge und
    Namen kommen aus der Quelle, `SourceTrackID`/`LinkedTrackID` werden mitgezogen.
  - Sound-Deskriptor: Struktur (Item-Liste) wie die Quelle. Die **essenzbeschreibenden Items** (Abtastrate, Bits, Kanäle,
    Block-Align, Locked, Ref-Level usw.) müssen in Quelle und Ziel **übereinstimmen**, sonst ist es ein Fehler (der Clip gilt
    als fehlgeschlagen, die Datei bleibt unverändert). `AvgBps` wie in der Quelle (nur wenn dort vorhanden). Verweise auf andere
    Sets werden **nicht** übernommen. Die Set-Klasse wird nur bei gleichem `EssenceContainer` übernommen.
  - TextObject: MIME-Typ und Description wie Quelle bzw. `text/xml` / leer.
  - Vor dem Schreiben wird erneut geparst und validiert; bei Fehler bleibt die Datei unverändert.
- `--seq-off`: Die `SequenceOffset`-Werte der Quell-Tonspuren (relevant bei 29,97/59,94 fps) werden aus der Quelle gelesen und
  an `raw2bmx` gegeben; unterschiedliche Offsets in den Spuren eines Clips → Clip wird nicht umgewandelt (nur kopiert, Hinweis).

### Robustheit (`converter.py`)

- `MEDIAPRO.XML` und `M01.XML` werden aus dem **Ist-Zustand des Ziels** abgeleitet (welcher Clip ist tatsächlich XAVC-L) und
  **atomar** geschrieben (Temp-Datei + Ersetzen); ein Folgelauf fällt nicht auf XAVC-I zurück. Vorhandene Ziel-Dateien werden
  erst nach erfolgreicher Neuerzeugung ersetzt.
- **Fremdes Ziel:** Gehört ein vorhandenes Ziel-XDROOT nicht zur Quelle (andere `systemId`/`mediaId`, fremde Clips/Dateien,
  MXF ohne `MEDIAPRO.XML`), Abbruch vor jeder Änderung. Quelle = Ziel (oder darin enthalten) wird abgelehnt.
- **Sperre:** `_xdroot_converter.lock` im Zielordner (PID + Prozess-Startzeit, `O_EXCL`); ein zweiter Lauf bricht ab; eine verwaiste
  Sperre (Prozess weg, andere Startzeit oder älter als der letzte Systemstart) wird übernommen; die Prozess-Startzeit hat Vorrang
  vor der PID.
- **Strukturprüfung** (`verify.check_structure`): Header-Partition geschlossen, FooterPartition-Offset zeigt auf ein
  Partition Pack, RIP am Dateiende konsistent, Dateigröße = Ende des RIP, Mindestgröße aus Frames × Tonbytes. Erkennt
  abgeschnittene Clips beim Fortsetzen und in `--verify`; dazu ein Decode-Test am Clip-Ende (auch beim Fortsetzen).
- **Einstellungsdatei** `konvertierung_einstellungen.json` im Zielordner: Einstellungen je fertigem Clip; beim Fortsetzen
  Warnung bei Abweichung, kein Neuerzeugen. Ein Rückfall (ANC-Spur oder eingebettetes XML mussten für den Clip weggelassen werden,
  `anc_dropped`/`xml_dropped`) wird **je Clip** gemerkt, damit Fortsetzen und `--verify` ihn nicht als Fehler werten; ebenso
  `--unlisted skip`/`--only` des Laufs (`auswahl`). Eine kaputte Einstellungsdatei wird nicht überschrieben, sondern als
  `konvertierung_einstellungen.json.defekt` gesichert (Warnung).
- **Ersetzen von Ziel-Clips:** Ein fertiger XAVC-L-Ziel-Clip wird nie durch eine Kopie (z. B. einer als nicht umwandelbar eingestuften
  Quelle) ersetzt; ein vorhandener Clip wird erst nach erfolgreicher Neuerzeugung ersetzt. `--max-frames` ersetzt keine vollständigen
  Clips. Verschieben/Ersetzen wird bei gesperrten Dateien (Windows: Virenscanner, Indexdienst) mehrfach wiederholt (`card.replace_retry`).
- Außerdem: Speicherschätzung vor dem Start, Fortschritt je Clip und gesamt, Strg+C unter Windows (CLI), `--max-frames`-Schutz
  (Testlauf nie über fertige Clips), Ton ≠ Mono/24 Bit/48 kHz nur kopiert, fehlende Nicht-MXF-Dateien sind in `--verify --source`
  Abweichungen.

### Tonspuren

- **Zuordnung:** ffmpeg nummeriert die Audiostreams in der Reihenfolge des Tracks-Arrays des Material Packages, mxf2raw/bmx
  sortieren anders. `converter.audio_stream_map` ordnet über die Material-Package-Track-ID ausdrücklich zu (`read_audio_order` liest
  die Reihenfolge aus dem Header der Quelle). Nicht eindeutig (Anzahl abweichend, doppelte/fehlende Track-IDs) → Fehler für den Clip,
  kein Raten.
- **Inhaltsvergleich** (`verify.check_audio_content`, in `--verify --source` und im Ablauf): je Tonspur die erste Sekunde von Ziel und
  Quelle dekodieren und vergleichen; bei Stille weitere Fenster. Abweichung → Meldung, ggf. mit Hinweis auf vertauschte Spuren.
  Nur mit synthetischem Ton belegt.

### Identification, Header-Details (Ergänzungen)

- Mehrere Identifications in der Quelle: die **erste** ist maßgeblich.
- `GenerationUID` der Sound-Deskriptoren bleibt die des Ziels; `AvgBps` wie in der Quelle; unbekannte Verweis-Arrays werden nicht
  übernommen; zusätzliche Timecode-Tracks der Quelle werden mit übernommen.

### KAG und MXFDump-Hinweis

Hat die Quelle KAG 512, hat auch das Ziel KAG 512 (`--kag-size-512`); die frühere Aussage „Ziel hat KAG 1“ gilt nur noch für Quellen
mit KAG 1. `MXFDump` meldet bei KAG 512 gelegentlich „Warning : Length is zero (following KLVFill key …)“ für Füll-KLVs in der Essenz
(Lücke von genau 20 Byte; stammt aus bmx/libMXF). Das KLV ist gültig, `mxf2raw` und `ffmpeg` lesen fehlerfrei. Gemessen: bei den
synthetischen 2-s-Clips nie, bei einem von zwei 20-s-Clips dreimal. Bekannte, harmlose Eigenheit.

### Nicht analysierbare Dateien, fehlende Clips in --verify

- Nicht analysierbare `.MXF`-Dateien (und Clips mit unklarer Ton-Zuordnung): siehe Ablauf Punkt 3. Beobachtet mit Müll- und
  0-Byte-Datei: Kopie mit Warnung, Zählung „kopiert“. Dateien mit Namen `._*` sind Begleitdateien (1:1 kopiert, in `--verify --source`
  byte-gleich verglichen).
- `--verify`: Ein nicht lesbarer Ziel-Clip ist ohne `--source` eine Abweichung (MXF nicht lesbar + Struktur). Mit `--source` ist er nur ein
  Hinweis („nicht analysierbar, unveraendert uebernommen“), wenn er byte-gleich mit dem Quell-Clip ist (`_same_content`); sonst Abweichung.
- `--verify --source` meldet im Ziel fehlende Quell-Clips, die weder gelistet sind noch durch die Einstellungsdatei als bewusst
  übersprungen (`--unlisted skip`/`--only`, `auswahl` in der Einstellungsdatei) belegt sind, als Abweichung. Geprüft: mit
  passender `auswahl` nur Hinweis, ohne sie oder bei `auswahl` ohne Belegung (`--unlisted convert`, kein passendes `--only`)
  Abweichung.

### GUI / Start

Abschlussfenster nach jedem Lauf (Fertig / mit Fehlern / abgebrochen / fehlgeschlagen) mit Zählzeile, Fehlergrund je Clip und Hinweis
auf eine unvollständige Zielkarte, Rückfragen beim Start (u. a. „Fortsetzen?“ bei vorhandenem Ziel), Hinweise in der Oberfläche,
robustes Polling und Schließen während des Laufs. `XDROOT-Converter.bat` sucht Python ≥ 3.11 mit tkinter (`py -3`, dann
`python`; Start per `pyw`, sonst `pythonw.exe` aus `where python`, sonst mit Konsole) und zeigt sonst eine Anleitung.

### Noch nicht belegt

Verhalten gegenüber echten Kameradateien (Parser, Patcher, Prüfungen, Toninhalts-Vergleich mit echtem AES3), Windows-Pfade (.bat, GUI,
Sperre, Strg+C, Wiederholungen bei gesperrten Dateien), 56-GB-Clips (WAV > 4 GB), Schnittprogramme und Sony-Werkzeuge. Review-Stand:
drei Runden paralleler Opus-Reviews (Metadaten/MXF, Ablauf/Robustheit, Oberfläche), die dritte ohne grobe Befunde. Siehe `docs/NAECHSTE-SCHRITTE.md`.
