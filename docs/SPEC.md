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
     4K-Umwandlung ist nicht Teil von Version 1.
   - Transcode + Wrap in einen temporären Ordner im Ziel (`_tmp_xdroot_converter`),
     dann atomar nach `Clip/` verschieben; temporäre Dateien löschen. Große Clips
     (56 GB, 31 min) müssen funktionieren: Video-Elementarstream liegt temporär auf
     der Zielplatte (~12 GB), das ist akzeptiert.
   - Verifizieren: Dauer (Frames) Ziel == Quelle, UMIDs gleich, Start-TC gleich,
     Anzahl Audiospuren gleich, essence_type `AVC_High_422`. Bei Abweichung: Clip als
     fehlgeschlagen markieren, Fehler klar melden.
4. XML anpassen (textbasiert, damit Formatierung/Namespaces/Zeilenenden der Kamera
   erhalten bleiben – kein Re-Serialisieren mit ElementTree):
   - `MEDIAPRO.XML`: `videoType` nur bei den tatsächlich umgewandelten `Material`-Einträgen.
   - `Clip/<name>M01.XML`: `videoCodec`.
   - Ziel-Codec-String aus Quell-String ableiten: `AVC100CBG_1920_1080_H422IP@L42` →
     `AVC50_1920_1080_H422P@L42`; Level aus Quelle übernehmen (`@L41` bei ≤30p → `@L41`).
     Unbekannte Muster: nicht raten, Clip kopieren statt wandeln und warnen.
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
- Start per Doppelklick: `XDROOT-Converter.bat` (startet `pythonw -m xdroot_converter`).

### Projektstruktur

```
xdroot_converter/   __init__.py, __main__.py (GUI-Start), gui.py, cli.py,
                    converter.py (Ablauf), card.py (XDROOT/XML lesen+patchen),
                    mxfinfo.py (ffprobe/mxf2raw-Parsing), tools.py (Programme finden),
                    mxfheader.py (MXF-Header-Metadaten lesen/ändern, Runde 2),
                    verify.py (Prüfung Einzelclip + Karte)
scripts/            make_test_card.py (synthetische XAVC-I-Testkarte),
                    mxf_vergleich.py (Satz-für-Satz-Vergleich der Header-Metadaten)
tests/              unittest für card.py/mxfinfo.py/mxfheader.py/verify.py/converter.py (ohne Medien;
                    Fixtures: Kopien aus docs/analysis)
docs/               SPEC.md, MXF-VERGLEICH.md, NAECHSTE-SCHRITTE.md, analysis/
XDROOT-Converter.bat, README.md (Deutsch, kurz), .gitignore
```

CLI: `python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium]
[--max-frames N] [--unlisted convert|skip] [--only TEXT] [--embed-xml auto|always|never] [--no-anc]
[--no-sony-header]`, `--verify <ziel-xdroot> [--source <quell-xdroot>] [--deep]` und
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
