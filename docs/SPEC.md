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

#### Energiesparmodus während des Laufs

Während einer Umwandlung (inkl. Abschlussprüfung) und bei `--verify` verhindert das Tool unter Windows den Standby/Ruhezustand
wegen Inaktivität (`keepawake.py`: `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, beim Verlassen
`ES_CONTINUOUS`). Der Bildschirm darf ausgehen (kein `ES_DISPLAY_REQUIRED`). Der Zustand gilt je Thread; deshalb wird er im
äußersten Laufrahmen (`Converter.run`, im Arbeits-Thread der GUI; bzw. im `--verify`-Weg der CLI) gesetzt und im `finally`
(Abbruch, Fehler, Temperatur-Abbruch) im selben Thread zurückgenommen. Testläufe (`--max-frames`) setzen ihn nicht. Verschachtelung ist
harmlos; auf anderen Systemen oder bei einem Fehler passiert nichts außer einer Hinweiszeile im Protokoll. Nicht verhindert werden:
Zuklappen des Deckels, Netzschalter, kritischer Akkustand, manuelles „Energie sparen“.

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
`docs/MXF-VERGLEICH.md`. Der lokale Lauf von `scripts/mxf_vergleich.py` mit echter F55-Quelle, FX6-Referenz und Ziel ist am
16.11.2026 nachgeholt: `docs/analysis/vergleich_f55_ziel_fx6.md` (20 Abweichungen Quelle ≠ Ziel, alle im Video-Deskriptor).

## Umsetzungsstand nach den Fix-Runden (Runde 2, ergänzt)

Alles unten wurde zuerst mit **synthetischem Material unter Linux** belegt; was davon inzwischen mit echten Kameradateien unter
Windows geprüft ist, steht unter „Beleglage“ am Ende und in `README.md`, „Getesteter Stand“. Die Testanzahl
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
    `FileDescriptor.SampleRate` wie in der Quelle (echte Sony-Dateien: 48000/1). `ContainerDuration` ist im Ziel genau dann
    vorhanden, wenn die Quelle sie hat (echte Sony-Dateien: nicht); ändert sich die Rate, wird der Wert exakt umgerechnet, sonst
    abgeleitet oder mit Hinweis weggelassen, nie geraten.
  - Daten-(ANC-)Deskriptor und MultipleDescriptor: `ContainerDuration` nur, wenn die Quelle sie hat (nur entfernen).
  - Bild-Deskriptor: Die **codec-unabhängigen** Items kommen aus der Quelle, wenn sie dort vorhanden sind und `StoredWidth`,
    `StoredHeight`, `FrameLayout` übereinstimmen: `SampledWidth/Height`, `SampledX/YOffset`, `DisplayWidth/Height`,
    `DisplayX/YOffset`, `VideoLineMap`, `ImageAlignmentOffset`, `ImageStartOffset`, `ImageEndOffset`, `SignalStandard`,
    `AspectRatio`, `CaptureGamma`, `PaddingBits`, `ReversedByteOrder`, `BlackRefLevel`, `WhiteReflevel`, `ColorRange`
    (die CDCI-Items nur bei Zielklasse CDCI/MPEG-Video). Nie geändert: Set-Klasse, `PictureEssenceCoding`, `EssenceContainer`,
    `SubDescriptors`, `ComponentDepth`, Subsampling, `ColorSiting`, `ColorPrimaries`, `CodingEquations`; nichts wird gelöscht.
  - `Preface.EssenceContainers`: Reihenfolge wie in der Quelle, Menge unverändert.
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

### Temperaturüberwachung und Schonmodus (Runde 3, Spezifikation)

Anlass: Beim lokalen Test stürzte der Rechner des Autors (ein Lenovo-Legion-Notebook) während einer langen Umwandlung wegen
Überhitzung ab. Ziel: Die Umwandlung soll die CPU-Temperatur anzeigen, bei Bedarf die Lüfter auf volle Drehzahl schalten und
notfalls pausieren. Nichts davon darf die Umwandlung behindern, wenn die Hardware es nicht unterstützt.

- **Modul `xdroot_converter/thermal.py`**, getrennt in
  - **Anbieter** (hardwareabhängig): liefert `Messung(cpu_temp_c, lüfter_drehzahlen)` und kann „Lüfter voll“ ein-/ausschalten.
    Mitgeliefert wird ein Anbieter für **Lenovo Legion** über WMI (`root\wmi`: `LENOVO_GAMEZONE_DATA.GetCPUTemp`,
    `GetFan1Speed`/`GetFan2Speed`/`GetFanMaxSpeed`, `GetSmartFanMode` nur lesend; `LENOVO_OTHER_METHOD.GetFeatureValue`/
    `SetFeatureValue` für „Lüfter voll“).
    Der Zugriff braucht **Administratorrechte** (ohne sie „Zugriff verweigert“). Da nur die Standardbibliothek erlaubt ist, läuft
    der WMI-Zugriff über **einen** dauerhaft laufenden, versteckten PowerShell-Hilfsprozess (Befehle über stdin, Antworten
    zeilenweise über stdout), nicht über einen neuen Prozess je Messung. Andere Geräte: neuen Anbieter ergänzen (Schnittstelle
    und ein kurzes Beispiel sind im Modul dokumentiert).
  - **Regler** (hardwareunabhängig, mit Attrappen-Anbieter testbar): Schwellen mit Hysterese.
    Standard: Lüfter voll ab **85 °C**, zurück unter **75 °C**; Pause ab **97 °C**, weiter unter **85 °C**. Alle Schwellen
    einstellbar. *(Durch Runde 5 ersetzt: Die Lüfter bleiben nach „voll“ bis zum Laufende, es gibt keine Rückschalt-Schwelle
    für sie mehr, und ab der Pausen-Schwelle wird zuerst gedrosselt; siehe „Lüfter halten und Last drosseln statt pendeln“.)* Mehrere Messungen in Folge entscheiden (kein Schalten wegen eines einzelnen Ausreißers).
- **Verfügbarkeit:** Ist kein Anbieter nutzbar (anderes Gerät, keine Adminrechte, PowerShell fehlt, Messung unplausibel
  ≤ 0 °C oder > 125 °C), zeigt die Oberfläche „Temperatur nicht verfügbar“ mit dem Grund, und die Umwandlung läuft unverändert.
  Fällt der Anbieter während des Laufs aus, gilt dasselbe; ein Fehler der Überwachung ist **nie** ein Fehler der Umwandlung.
- **Lüfter voll:** Der vorherige Zustand wird vor dem ersten Schalten gelesen und **am Ende des Laufs, bei Abbruch und bei
  Strg+C wiederhergestellt**. Kann der Zustand nicht gelesen werden, wird nicht geschaltet.
- **Pause:** Der laufende `ffmpeg`-Prozess wird angehalten (Windows: `NtSuspendProcess`/`NtResumeProcess`, POSIX:
  `SIGSTOP`/`SIGCONT`) und nach dem Abkühlen fortgesetzt. Zeitüberwachungen dürfen einen angehaltenen Prozess nicht als hängend
  abbrechen. Abbrechen (GUI, Strg+C) muss auch während einer Pause sofort wirken (erst fortsetzen, dann beenden). Pausen stehen
  im Protokoll und in der Zusammenfassung (Anzahl, Gesamtdauer). Die Pause wirkt auch ohne Lüftersteuerung, braucht aber eine
  Temperaturmessung.
- **Schonmodus (ohne Messung, überall):** `--threads N` bzw. Einstellung „CPU-Kerne begrenzen“ gibt `ffmpeg` für Decoder und
  Encoder höchstens N Threads (Standard: unbegrenzt). Senkt die Wärme, kostet Zeit. Der Wert steht in der Einstellungsdatei nur
  zur Information und löst beim Fortsetzen keine Warnung „andere Einstellungen“ aus (das Ergebnis hängt nicht davon ab; falls
  sich das wegen x264-Slice/Thread-Verhalten nicht halten lässt, als Einstellung wie Preset behandeln und das begründen).
- **Bedienung:** GUI-Statuszeile während des Laufs, z. B. „CPU 87 °C · Lüfter 4800/5100 U/min · Lüfter voll“ bzw. „Pause wegen
  Temperatur (98 °C), weiter unter 85 °C“; Einstellungen für Überwachung an/aus, beide Schwellen und Kernbegrenzung.
  CLI: `--thermal auto|off` (Standard `auto`), `--temp-fan GRAD`, `--temp-pause GRAD`, `--threads N` und
  `--thermal-test` (Diagnose: Anbieter, Rechte, 5 Messungen, Lüfter 10 s voll und zurück, mit klarer Ausgabe je Schritt).
- **Start mit Adminrechten:** `XDROOT-Converter (Administrator).bat` startet die GUI mit UAC-Abfrage; die normale `.bat` bleibt
  unverändert. Ohne Adminrechte weist die GUI einmal dezent darauf hin, wie man die Überwachung bekommt.
- **Grenzen:** Welche Kennung „Lüfter voll“ auslöst, ist geräteabhängig (Legion-Generationen unterscheiden sich) und muss am
  Gerät mit `--thermal-test` belegt werden. Die Schwellen sind Vorgaben, keine Garantie gegen Abstürze.

Ergänzungen nach den Reviews (verbindlich):

- **Kein Leistungsmodus:** Der Lenovo-„SmartFanMode“ wird nur gelesen und angezeigt, **nie geschaltet** (der Leistungsmodus hebt
  die CPU-Leistungsgrenzen an und erzeugt mehr Wärme). „Lüfter voll“ läuft ausschließlich über die Feature-Kennung; ist sie nicht
  nutzbar, wird nur gemessen und pausiert.
- **Wirkung belegen:** Die Anzeige „Lüfter voll“ erscheint nur, wenn die Drehzahl nach dem Schalten messbar gestiegen ist; sonst
  „Lüfter voll angefordert (keine Wirkung messbar)“. `--thermal-test` vergleicht die Drehzahl vorher/während/nachher und endet
  mit einer eindeutigen Ergebniszeile.
- **Sicherheit unter Adminrechten:** PowerShell wird nur über den festen Pfad im Windows-Systemverzeichnis gestartet (keine
  Suche im Arbeitsordner oder `PATH`), das CIM-Modul ausdrücklich aus `$PSHOME` geladen. In den PowerShell-Text gelangen nur
  Konstanten und Zahlen.
- **Zurückstellen ist robust:** Antwortet die Schnittstelle nicht, wird der Hilfsprozess neu gestartet und das Zurückstellen
  wiederholt. Vor dem ersten Schalten wird der vorherige Zustand in eine Zustandsdatei im Benutzerprofil geschrieben
  (`%LOCALAPPDATA%\XDROOT-Converter\`); nach einem Absturz stellt der nächste Start ihn wieder her. Hält ein anderer laufender
  Prozess die Datei, misst diese Instanz nur. Der vorherige Zustand wird unmittelbar vor dem Schalten gelesen.
- **Pause:** Hält alle laufenden Hilfsprogramme des Laufs an (praktisch `ffmpeg`), während der Pause sind die Lüfter voll.
  Alle 2 Minuten eine Protokollzeile. Kühlt die CPU nach **20 Minuten** Pause nicht unter die Fortsetz-Schwelle, wird der
  **Lauf sauber abgebrochen** (fortsetzbar) mit eigenem Grund; das ist sicherer als unter Übertemperatur weiterzurechnen.
- **Schwellen:** Einstellbar sind „Lüfter voll ab“ (50–110 °C) und „Pause ab“ (60–120 °C, größer als die Lüfter-Schwelle); die
  Rückschalt-Schwellen sind abgeleitet (−10 °C bzw. −12 °C). Zum Einschalten zählen 2, zum Zurückschalten 3 Messungen in Folge
  (Messabstand 5 s). *(Für die Lüfter durch Runde 5 ersetzt: keine Rückschalt-Schwelle, „voll“ bleibt bis Laufende; für die Pause
  gilt −12 °C weiter.)*
- **Intern** ist die Überwachung aus (`Options.thermal = "off"`); CLI und GUI schalten sie ein (`auto`).
- **`--threads`:** x264 erzeugt mit anderer Kernzahl einen gleichwertigen, aber nicht bitgleichen Datenstrom. Für das Fortsetzen
  ist das unerheblich, weil jeder Clip für sich steht. Gilt auch für `--verify --deep`.
- **Start mit Adminrechten:** Die Rechte-Erhöhung macht Python selbst (`--als-administrator`, `ShellExecuteW` mit `runas`),
  nicht die `.bat`; gestartet wird ein Startskript über seinen absoluten Pfad, das Startfehler in einem Fenster zeigt.
  Zusätzlich `Temperatur-Test (Administrator).bat` für die Diagnose.
- **Zustandsdatei, Einzelheiten:** Sie wird exklusiv angelegt (zwei gleichzeitige Läufe können sich den Zustand nicht
  gegenseitig überschreiben; der zweite misst nur) und nur vom Besitzer gelöscht. Beim Lesen gelten nur die bekannte Kennung
  und die Werte 0/1; alles andere gilt als beschädigt, wird gelöscht und **nie in die Hardware geschrieben**. Scheitert das
  Wiederherstellen dreimal, wird die Datei mit deutlicher Warnung entfernt, damit die Steuerung nicht dauerhaft gesperrt bleibt.
- **Wirkungsregel:** „Lüfter voll“ gilt als belegt, wenn die Drehzahl binnen 20 s um mindestens 15 % steigt oder (bei
  plausibler Maximaldrehzahl und vorher niedrigerer Drehzahl) 90 % des Maximums erreicht. Ohne lesbare Drehzahl: „Lüfter voll
  angefordert“.
- **`--thermal-test`:** nennt die Kennung ausdrücklich unbestätigt, verlangt vor dem Schalten eine Bestätigung mit Enter, zeigt
  Drehzahl vorher/während/nachher und endet immer mit einer Zeile `ERGEBNIS: …`; Strg+C stellt zurück und endet ohne Traceback.
- **Programme:** Externe Programme werden nur über absolute Pfade gestartet (`tools/`, danach absolute `PATH`-Einträge, unter
  Windows nur `.exe`), nie aus dem Arbeitsordner.
- **Startskripte:** `start_gui.pyw` und `start_thermal_test.py` im Programmordner werden über ihren absoluten Pfad gestartet.
  Startfehler der GUI erscheinen in einem Fenster und in `start_fehler.txt`; der Temperatur-Test zeigt Fehler in seinem
  Konsolenfenster, das bis zur Bestätigung mit Enter offen bleibt.
- **Nicht abgedeckt:** Wird das Programm hart beendet (Task-Manager) oder stürzt der Rechner ab, bleiben die Lüfter voll, bis
  das Tool das nächste Mal **mit Temperaturüberwachung und Administratorrechten** läuft (oder `--thermal-test` gestartet wird).
  Ein erhöhter Prozess sieht Netzlaufwerke des normalen Benutzers in der Regel nicht.

#### Temperaturquellen (Ergänzung nach dem Gerätetest 16.11.2026)

Befund am Lenovo Legion Pro 7 16IRX8H (mit Administratorrechten): `LENOVO_GAMEZONE_DATA.GetCPUTemp` antwortet, liefert aber
`Data=0` (unplausibel, deshalb „nicht verfügbar“). Der Legion-Anbieter hat deshalb eine **Quellenkette**; die erste Quelle mit
plausiblem Wert (über 0 bis 125 °C; bei der Quellenwahl zusätzlich mindestens 20 °C, siehe „Untergrenze der Quellenwahl“) bei der Öffnung gilt für den ganzen Lauf und wird einmal im Protokoll genannt
(„Temperaturquelle: …“). Fällt sie im Lauf aus, gilt das normale Verhalten bei Messausfällen (kein stilles Umschalten).
Alle Quellen sind **nur lesend**; es kommt kein neuer Schreibaufruf hinzu (`SetFeatureValue` weiter nur für `0x04020000` mit 0/1).

1. `LENOVO_GAMEZONE_DATA.GetCPUTemp` (bisher).
2. `LENOVO_OTHER_METHOD.GetFeatureValue(IDs=0x05040000)` – aus dem Projekt „Lenovo Legion Toolkit“ übernommen, für dieses
   Gerät **unbestätigt**.
3. `root\wmi` `MSAcpi_ThermalZoneTemperature.CurrentTemperature` (Zehntel-Kelvin, °C = Wert/10 − 273,15), höchster plausibler
   Wert aller Zonen; „ACPI-Thermalzone (grob, oft träge)“.
4. `root\cimv2` `Win32_PerfFormattedData_Counters_ThermalZoneInformation` (`Temperature` in Kelvin bzw.
   `HighPrecisionTemperature` in Zehntel-Kelvin, was vorhanden ist), höchster plausibler Wert; ebenfalls grob.

**Untergrenze der Quellenwahl:** Beim Öffnen gilt eine Quelle nur ab `TEMP_SOURCE_MIN_C` = 20 °C als brauchbar (alle Quellen a–d);
GetFeatureValue liefert je Kennung beliebige Zahlen, und Werte wie 1 oder 2 sind Schalter, keine Temperaturen. Im Lauf gilt
unverändert die Grenze über 0 bis 125 °C.

**Schutz eingeschränkt bei b, c, d:** Bei c/d (Thermalzonen) zeigt die Statuszeile „Thermalzone 48 °C (grob, nicht CPU)“, bei b
„CPU 71 °C (Quelle unbestätigt)“. Bei c/d ist die Protokollzeile zur Quelle eine **Warnung** („Schwellen gelten für die CPU; die
Zone liegt meist darunter – Schutz eingeschränkt, ggf. CPU-Kerne begrenzen“), und `--thermal-test` hängt „nur grobe
Thermalzone – Schutz eingeschränkt“ (c/d) bzw. „Temperaturquelle unbestätigt“ (b) an das Urteil. **Konstant-Wächter** (nur b, c,
d): Liefert die gewählte Quelle 36 Messungen in Folge (3 min) exakt denselben Wert, gilt sie **nur dann** als ausgefallen
(Lüfter zurück, „Temperatur nicht verfügbar: Temperaturquelle liefert einen konstanten Wert“, Umwandlung läuft weiter), wenn
(1) sich der Wert seit dem Öffnen der Quelle **nie** geändert hat (toter Festwert/falsch gedeutetes Flag) **und** (2) gerade
weder eine Pause noch „Lüfter voll“ aktiv ist **und** (3) der Wert unter der Lüfter-Schwelle liegt. Hat sich der Wert schon
einmal geändert, oder liegt er über der Schwelle bzw. ist Pause/Lüfter aktiv, löst der Wächter **keinen** Ausfall aus (Pause
und Lüfter bleiben, die normalen Regeln inkl. maximaler Pausendauer gelten), sondern schreibt höchstens **eine** Warnzeile je Lauf
(„Temperaturquelle liefert seit 3 min denselben Wert – träge Quelle?“); die Überwachung läuft weiter.
**Beschriftung:** Meldungen nennen die Quelle einheitlich als „CPU“ (a), „CPU (Quelle unbestätigt)“ (b) bzw. „Thermalzone
(grob, nicht CPU)“ (c/d) – in der Pause-Statuszeile, den Pausen-Protokollzeilen, dem Überhitzungs-Abbruch („… kühlt nicht ab“)
und in `--thermal-test`.

**Robustheit/Sicherheit:** Hängt oder stirbt der Hilfsprozess bei einer Quelle oder der Drehzahl-Ausweichquelle, wird er neu
gestartet und mit der nächsten Quelle weitergemacht; ein Fehler der Drehzahl-Ausweichquelle macht eine funktionierende
Temperaturquelle nie unverfügbar. **Drehzahl im Lauf:** Hängt oder fehlert das Lesen der Drehzahl (Hauptquelle oder Ausweichquelle)
im Lauf, wird die schon gelesene Temperatur **nicht verworfen**: Der Hilfsprozess wird (innerhalb von `MAX_HELPER_RESTARTS`) neu
gestartet und die Messung geht ohne Drehzahl weiter; beim 2. Mal wird die Drehzahlquelle für den Rest des Laufs abgeschaltet
(eine Info-Zeile im Protokoll). **Lüftersteuerung beim Öffnen:** Hängt/fehlert das Lesen von `GetFeatureValue(0x04020000)` beim
Öffnen, wird der Hilfsprozess neu gestartet und einmal wiederholt; scheitert das erneut, bleibt der Anbieter verfügbar, nur die
Lüftersteuerung entfällt (Status „Lüftersteuerung nicht verfügbar (Hilfsprozess antwortete nicht beim Lesen der Kennung)“;
Anzeige und Pause funktionieren). **Einzelne Fehler im Lauf:** Eine Fehlerantwort der Temperaturquelle (`HelperRemoteError`) zählt
wie ein unplausibler Wert (`bad_limit`, Standard 3 in Folge), erst danach gilt die Überwachung als ausgefallen. **Zeitlimit:**
`READ PERF` darf bei der Öffnungsprüfung der Quelle d 20 s brauchen (im Lauf 10 s). Das PowerShell-Skript (läuft als Administrator) hat eine **feste Erlaubnisliste**:
`LENOVO_GAMEZONE_DATA` {GetCPUTemp, GetFan1Speed, GetFan2Speed, GetFanMaxSpeed, GetSmartFanMode}; `LENOVO_OTHER_METHOD.GetFeatureValue`
nur mit `IDs` ∈ {0x04020000, 0x05040000, 0x04030001, 0x04030002}; `SetFeatureValue` nur mit `IDs=0x04020000` und `value` 0 oder 1.
Alles andere endet vor jedem CIM-Aufruf mit `BadArgs`; Parameternamen gelten streng und gross/klein-genau (nur exakt `IDs` und
`value`, jeder höchstens einmal).

Der Hilfsprozess kennt dafür zwei feste lesende Befehle (`READ ACPI`, `READ PERF`). **Drehzahl:** liefern `GetFan1Speed`/
`GetFan2Speed` keinen plausiblen Wert (100–12000 U/min), dient `GetFeatureValue(0x04030001/0x04030002)` (unbestätigt) als
Ausweichquelle; ohne Drehzahl bleibt es bei „Lüfter voll angefordert (keine Wirkung messbar)“.
**Diagnose (`--thermal-test`):** Schritt 1 gibt alle Kandidaten roh aus (Rohwert, umgerechnet, plausibel ja/nein, Fehlertext),
dazu `GetSmartFanMode` und den Zustand `GetFeatureValue(0x04020000)` (nur gelesen), dann die gewählte Quelle. Ist keine Quelle
plausibel, steht die Liste trotzdem in `thermal_test.txt`; es wird nichts geschaltet.

#### Lüfter halten und Last drosseln statt pendeln (Runde 5, Ergänzung nach dem ersten echten Lauf 16.11.2026)

**Befund am Gerät:** Die CPU springt unter voller Last in wenigen Sekunden auf 97–98 °C. Dadurch fielen „Lüfter voll“ und Pause
zusammen; in der Pause kühlte die CPU auf ca. 70 °C, die Lüfter wurden zurückgestellt, `ffmpeg` lief weiter, und nach kurzer Zeit
begann alles von vorn (Pendeln zwischen Pause und Volllast, Lüfter 5900 ↔ 7100 U/min). Die bisherigen Regeln zum Zurückschalten
und zur sofortigen Pause werden deshalb ersetzt:

1. **Lüfter halten:** Hat der Regler in einem Lauf einmal „Lüfter voll“ geschaltet, bleibt es bis zum **Ende des Laufs** dabei
   (auch während Pausen, Wrappen, Kopieren und der Abschlussprüfung). Zurückgestellt wird nur am Laufende (jeder Endpfad:
   fertig, Fehler, Abbruch, Hitze-Abbruch, Ausfall der Messung) – wie bisher über den gemerkten vorherigen Zustand. Die
   Rückschalt-Schwelle für den Lüfter entfällt.
2. **Last drosseln vor Pause (Stufen):** Statt `ffmpeg` sofort anzuhalten, begrenzt der Regler die Zahl der logischen Kerne, die
   die laufenden Hilfsprogramme benutzen dürfen (Windows: Prozess-Affinität; die Thread-Zahl von x264 wird nicht verändert, die
   Drosselung soll den Datenstrom nicht beeinflussen – Beleg mit echtem Material steht aus). Stufen in Prozent der verfügbaren logischen Kerne: 100 → 75 → 50 → 35 → 25
   (mindestens 2 Kerne).
   - **Stufe runter** (weniger Kerne): Temperatur an der Pausen-Schwelle oder darüber, 2 Messungen in Folge; danach frühestens
     nach 30 s die nächste Stufe (die Wirkung braucht Zeit).
   - **Stufe hoch** (mehr Kerne): Temperatur mindestens 8 °C unter der Pausen-Schwelle, 60 s am Stück; je Schritt eine Stufe.
     Nach einem Hochschalten, das innerhalb von 2 min wieder zum Runterschalten (oder zur Pause) führte, wird diese höhere
     Stufe **15 min lang** (`THROTTLE_FLOOR_EXPIRE_S`, nur die Zeit zählt, nicht der Clip-Wechsel) nicht mehr versucht; danach
     darf sie wieder versucht werden, fällt sie erneut binnen 2 min zurück, gilt die Sperre wieder 15 min (kein Pendeln, aber
     auch kein Festnageln auf 25 % für einen ganzen mehrstündigen Lauf). Die 30-s-Sperre gilt nur nach Schritten nach UNTEN;
     nach einem Hochschalten darf sofort wieder heruntergeschaltet werden, sobald 2 Messungen an der Schwelle liegen.
   - **Nach jeder Pause** (Notfall oder letzte Stufe) wird eine Stufe tiefer fortgesetzt, sofern es eine gibt; die Sperrzeit
     läuft dann ab dem Fortsetzen neu. Gibt es keine tiefere Stufe mehr, gibt es auch keine Sperre: sobald wieder 2 Messungen an
     der Schwelle liegen, wird sofort erneut pausiert (die 30-s-Sperre gilt nur, wenn tatsächlich eine Stufe gewechselt wurde).
   - **Fehler beim Setzen:** Ein einzelner Fehlschlag (z. B. Prozess gerade beendet) wird mit der nächsten Messung erneut
     versucht und führt in dieser Messung nicht zur Pause (ausser Notfall); ein gerade beendeter Prozess zählt nicht als
     Fehlschlag. Erst „nicht unterstützt“ oder 3 gescheiterte Stufenwechsel in Folge (eigener Zähler, wird nur durch den Erfolg
     eines Schritts zurückgesetzt; ebenso 3 gescheiterte Nachzieh-Aufrufe) schalten die Drosselung für den Rest des Laufs ab
     (Stufe sofort auf 100 % zurück, eine Warnung). Bei gescheiterten Stufenwechseln folgt die Pause wie bisher in derselben
     Messung; nach dem Abschalten wegen Nachzieh-Fehlern gilt die normale Regel (Pause nach 2 Messungen an der Schwelle). Scheitert das Aufheben auf 100 %
     bei einem Prozess, wird es einmal sofort wiederholt (sonst Warnzeile) und mit jeder Messung nachgezogen, solange noch ein
     Prozess gedrosselt ist. Die Startverzögerung (3 s) zählt nur Laufzeit: Pausenzeit wird nicht angerechnet. Eine gescheiterte Stufe bleibt nie im Token hängen.
   - Die Stufe gilt für den ganzen Lauf weiter: neu gestartete Hilfsprogramme (nächster Clip, Prüfung) bekommen sie **nach
     einer Startverzögerung von mindestens 3 s Laufzeit** (nicht beim Registrieren; je nach Takt der nächsten Messung also etwa
     3–8 s; Pausenzeit zählt nicht, angehaltene Prozesse werden nicht gedrosselt): `ffmpeg`/x264 leiten ihre Threadzahl aus der
     Affinität beim Start ab und sollen sie wie ohne Drosselung bei voller Affinität wählen; erst danach wird eingeschränkt. Die
     Drosselung soll den Datenstrom nicht beeinflussen (Beleg steht aus). Grenze (unbelegt): Läuft eine Quelle sehr langsam an,
     könnte der Encoder erst nach dieser Zeit starten und seine Threadzahl schon bei gedrosselter Affinität wählen. Der Regler zieht die Stufe bei jeder Messung (alle 5 s), solange gedrosselt
     wird, über `apply_throttle()` nach; je Prozess wird die zuletzt gesetzte Stufe gemerkt (kein wiederholtes Setzen). Ist
     beim Start eines Prozesses eine Pause aktiv, gilt das Pausenverhalten unverändert. Eine feste Threadzahl wird bewusst
     NICHT vorgegeben (eine Messung am 16.11.2026 zeigte, dass explizite Werte den Automatikfall nicht reproduzieren).
   - Kerne werden so gewählt, dass zuerst gleichmäßig über alle Kerne ausgedünnt wird (keine Annahme über P-/E-Kerne); die
     Auswahl ist deterministisch und testbar.
3. **Pause nur noch als letzte Stufe:** `ffmpeg` wird erst angehalten, wenn auf der niedrigsten Stufe die Temperatur weiter an
   der Pausen-Schwelle liegt (2 Messungen, frühestens 30 s nach dem letzten Stufenwechsel) **oder** wenn die Temperatur die
   Pausen-Schwelle um 5 °C oder mehr überschreitet (Notfall, sofort nach 2 Messungen, unabhängig von der Stufe; beim
   Standard 97 °C liegt dieser Wert mit 102 °C über der Abschaltgrenze vieler CPUs und wird dann praktisch nie erreicht –
   den Schutz bietet die Stufenfolge) **oder** wenn
   sich die Affinität nicht setzen lässt (dann verhält sich der Regler wie bisher). Fortsetzen und maximale Pausendauer
   (20 min, dann sauberer, fortsetzbarer Abbruch) bleiben unverändert.
4. **Anzeige und Protokoll:** Statuszeile z. B. „CPU 93 °C · Lüfter 7100/7100 U/min · Lüfter voll · gedrosselt auf 24 von 32
   Kernen“. Jeder Stufenwechsel ist eine Protokollzeile („Last gedrosselt: 24 von 32 Kernen (97 °C)“, „Drosselung gelockert:
   32 von 32 Kernen (88 °C)“). Die Zusammenfassung nennt, wie lange gedrosselt und wie lange pausiert wurde.
5. **Grenzen:** Auf Systemen ohne Affinität (oder mehr als 64 logischen Kernen / mehreren Prozessorgruppen) entfällt die
   Drosselung; es gilt Punkt 3 letzter Fall. Der Schonmodus `--threads N` bleibt davon unabhängig. Ohne nutzbare
   Temperaturmessung wird nie gedrosselt.
6. Die Affinität wird nur für die vom Tool gestarteten Hilfsprogramme gesetzt (`ffmpeg`, `ffprobe`, `raw2bmx`, `mxf2raw`;
   nicht für das Tool selbst, nicht für andere Programme) und endet mit dem Prozess; es bleibt nichts am System verstellt.

#### Eingreifen bei Hitze nur noch auf Wunsch (Runde 6, Ergänzung nach dem zweiten echten Lauf 17.11.2026)

**Befund am Gerät (Legion Pro 7 16IRX8H, Leistungsmodus in Lenovo Vantage):**

- Mit dem Regler aus Runde 5 erreichte die Messung auch auf 8 von 32 Kernen wieder 97 °C; der Lauf endete in einer Kette
  von Pausen (mehr als 20 in Folge) und kam kaum voran. Weniger Kerne senken die Temperatur dieser CPU nicht ausreichend
  (vermutlich taktet sie die verbleibenden Kerne höher; nicht gemessen).
- Ohne Überwachung lief derselbe Clip mit voller Leistung durch; Lenovo Vantage zeigte dabei etwa 87 °C.
- Bei eingeschalteter Überwachung lag unser Messwert (Quelle b, `GetFeatureValue 0x05040000`) in 8 Vergleichsablesungen
  4 bis 14 °C über der Anzeige von Vantage, meist etwa 10 °C (Ablesungen nicht exakt gleichzeitig). Welche Anzeige der
  heisseste Kern ist, ist nicht belegt.
- Folgerung: Die CPU arbeitet unter Volllast bestimmungsgemäss nahe ihrer Grenze und regelt sich selbst. Eine Schwelle von
  97 °C auf dieser Quelle liegt im normalen Betriebsbereich; der Regler bremste den Lauf, ohne etwas zu schützen.

**Neue Regeln (ersetzen die Voreinstellung, nicht die Mechanik aus Runde 5):**

1. **Zwei getrennte Schalter.** „Temperatur überwachen“ (wie bisher, Standard an) bedeutet nur noch: messen, anzeigen,
   „Lüfter voll“ ab der Lüfter-Schwelle (bleibt bis zum Laufende), höchste Temperatur in der Zusammenfassung. Neu:
   „Bei Hitze drosseln und pausieren“ – **Standard aus**. Nur wenn dieser Schalter an ist, gelten Drosselstufen, Pause,
   Notfall und Hitze-Abbruch aus Runde 5 unverändert.
2. **Schalter aus:** Der Regler setzt nie eine Affinität, hält `ffmpeg` nie an und bricht nie wegen Hitze ab. Die
   Pausen-Schwelle wird nicht ausgewertet und nicht geprüft (auch nicht ihr Verhältnis zur Lüfter-Schwelle). Lüfter voll,
   Wirkungsprüfung an der Drehzahl, Wiederherstellen am Laufende, Ausfallbehandlung der Messung und die Konstanz-Wache
   bleiben wie bisher (die Bedingung „weder Pause noch Lüfter voll aktiv“ gilt unverändert).
3. **Kommandozeile:** neue Option `--thermal-throttle` (ohne Wert; schaltet Drosseln und Pause ein). `--temp-pause` wirkt nur
   zusammen mit `--thermal-throttle`; wird `--temp-pause` ohne sie angegeben, gibt es einen Hinweis (kein Fehler).
   `--thermal off` schaltet wie bisher alles ab.
4. **Oberfläche:** eigene Checkbox „Bei Hitze drosseln und pausieren“ unter „Temperatur überwachen“; das Feld „Pause ab
   (°C)“ ist nur bedienbar, wenn beide Schalter an sind. Hinweistext sinngemäss: „Die CPU schützt sich selbst; dieser Schalter
   ist nur nötig, wenn der Rechner unter Volllast abstürzt. Der Messwert kann rund 10 °C über der Anzeige in Lenovo Vantage
   liegen.“ Texte dürfen nicht abgeschnitten werden (Mindesthöhe des Fensters anpassen).
5. **Protokoll:** Die Startzeile der Überwachung nennt den Zustand, z. B. „Temperaturüberwachung aktiv (…); Lüfter voll ab
   85 °C (bleibt bis zum Laufende); kein Drosseln, keine Pause (die CPU regelt sich selbst).“ bzw. mit Schalter an der
   bisherige Text. Die Statuszeile zeigt ohne Schalter nur Temperatur, Drehzahl und „Lüfter voll“.
6. **Zusammenfassung:** höchste gemessene Temperatur auch dann, wenn nie pausiert wurde (CLI-Zusammenfassung und
   Abschlussfenster), z. B. „Höchste CPU-Temperatur 98 °C (Messwert des Geräts, kann über der Anzeige des Herstellers liegen)“.

#### Fortschritt beim Verpacken (Ergänzung 17.11.2026)

Bei einem 58-Minuten-Clip stand der Balken während `raw2bmx` minutenlang still (kein Absturz, die Datei wuchs mit etwa
50 MB/s). Während des Verpackens wird der Clip-Fortschritt deshalb aus der Grösse der entstehenden MXF-Datei im Verhältnis
zur erwarteten Grösse (Summe der Eingabedateien des Verpackens: Videostrom, WAV-Dateien, Zusatzdaten) geschätzt, höchstens
einmal pro Sekunde abgefragt und auf 99 % begrenzt; die Clip-Zeile zeigt z. B. „Verpacke MXF (raw2bmx) 63 %“. Fehler beim
Abfragen der Grösse werden ignoriert (dann bleibt die Anzeige wie bisher). Am Ablauf, an den Dateien und am Gesamtfortschritt
(Gewichtung der Schritte) ändert sich nichts.

### Auftragsliste: mehrere Karten nacheinander (Runde 7, Spezifikation 17.11.2026)

**Ziel:** Mehrere Karten in eine Liste legen und mit einem Klick nacheinander umwandeln lassen (z. B. drei Karten über Nacht),
ohne dass zwischendurch jemand am Rechner sitzen muss. Nur Oberfläche; die Kommandozeile bleibt unverändert.

1. **Bedienung:** Neben „Konvertieren“ (startet wie bisher sofort den einen Lauf) gibt es „Zur Liste hinzufügen“. Darunter
   eine Liste der Aufträge mit den Spalten Quelle, Ziel, Einstellungen (kurz, z. B. „50 Mbit/s, medium“) und Status, dazu
   „Entfernen“ (markierter Auftrag), „Liste leeren“ und „Alle konvertieren“. Die Liste ist nur sichtbar, wenn sie Aufträge
   enthält oder ein Listenlauf läuft; das Fenster darf dadurch keine Texte abschneiden (Mindesthöhe anpassen).
2. **Auftrag = Momentaufnahme:** Beim Hinzufügen werden Quelle, Ziel und alle Einstellungen (Bitrate, Preset, MEDIAPRO-Schalter,
   Temperatur-Schalter und -Schwellen, Kernbegrenzung, Abschlussprüfung) in den Auftrag kopiert. Spätere Änderungen in den
   Feldern wirken nur auf neue Aufträge.
3. **Prüfung beim Hinzufügen, nicht in der Nacht:** Es gelten dieselben Prüfungen und Rückfragen wie beim direkten Start
   (Quelle ist ein XDROOT, Ziel ungleich/ausserhalb der Quelle, Rückfrage bei nicht leerem Ziel bzw. Fortsetzen usw.). Die
   Antworten auf Rückfragen werden im Auftrag gespeichert; während des Listenlaufs erscheint **keine** Rückfrage mehr.
   Zusätzlich abgelehnt: ein Ziel, das schon in der Liste steht; eine Quelle, die Ziel eines anderen Auftrags ist (und
   umgekehrt). Speicherplatz: die geschätzten Zielgrössen aller wartenden Aufträge auf demselben Laufwerk werden addiert;
   reicht der freie Platz dafür nicht, gibt es beim Hinzufügen eine Warnung mit Rückfrage (kein hartes Verbot, die
   Schätzung ist grob). Die normale Platzprüfung jedes Laufs bleibt.
4. **Ablauf:** „Alle konvertieren“ arbeitet die wartenden Aufträge in Listenreihenfolge nacheinander ab, jeder Auftrag als
   normaler Lauf mit allem, was dazugehört (Abschlussprüfung, Temperaturüberwachung, Lüfter zurückstellen am Laufende,
   Protokolldatei). Status je Auftrag: wartet → läuft → fertig / fertig mit Abweichungen / Fehler / abgebrochen. Im Protokoll
   beginnt jeder Auftrag mit einer Trennzeile „Auftrag 2 von 3: …“. Der Gesamtbalken gilt für den laufenden Auftrag; die
   Statuszeile nennt zusätzlich „Auftrag 2 von 3“.
5. **Fehler halten die Liste nicht an:** Endet ein Auftrag mit Fehler oder Abweichungen (auch Hitze-Abbruch), wird der
   nächste trotzdem gestartet. **„Abbrechen“** bricht den laufenden Auftrag ab und beendet den Listenlauf; die übrigen
   bleiben „wartet“ und können später mit „Alle konvertieren“ fortgesetzt werden (der abgebrochene Auftrag wird dabei
   wieder aufgenommen; fertige Clips überspringt der Lauf wie gewohnt). Ein unerwarteter Programmfehler in einem Auftrag
   (Ausnahme) zählt als Fehler dieses Auftrags, nicht als Ende der Liste.
6. **Energiesparmodus:** Die Anforderung „nicht einschlafen“ gilt über den ganzen Listenlauf, auch zwischen zwei Aufträgen.
7. **Abschluss:** Erst am Ende der Liste erscheint **ein** Abschlussfenster: je Auftrag eine Zeile mit Ziel und Ergebniszeile
   („Ergebnis: …“), darunter die Summen (Clips, Grösse vorher/nachher, Dauer). Die Einzel-Abschlussfenster entfallen im
   Listenlauf. Erledigte Aufträge bleiben mit ihrem Status in der Liste stehen, bis sie entfernt werden; sie werden von
   „Alle konvertieren“ nicht erneut gestartet.
8. **Sperren:** Während eines Laufs (einzeln oder Liste) sind „Konvertieren“ und „Alle konvertieren“ gesperrt; „Zur Liste
   hinzufügen“ und „Entfernen“ für wartende Aufträge bleiben möglich (ein während des Listenlaufs hinzugefügter Auftrag wird
   am Ende mit abgearbeitet). Der laufende Auftrag kann nicht entfernt werden.
9. **Grenzen (bewusst):** Die Liste wird nicht gespeichert; nach dem Schliessen des Programms ist sie leer (ein
   unterbrochener Auftrag lässt sich durch erneutes Hinzufügen fortsetzen). Es läuft immer nur ein Auftrag gleichzeitig.
   Schliessen des Fensters während eines Listenlaufs verhält sich wie beim Einzellauf (Rückfrage, dann Abbruch).
10. **Quellen bleiben unberührt:** wie bei jedem Lauf wird aus den Quellen nur gelesen.

**Ergänzungen nach dem ersten Review (verbindlich):**

- **Abbruch-Signal je Auftrag:** Jeder Auftrag bekommt ein eigenes, frisches Abbruch-Signal (`CancelToken`). Nur der Nutzer
  („Abbrechen“, Schliessen des Fensters) beendet den Listenlauf – über ein eigenes Kennzeichen, das die Schleife prüft. Ein
  Hitze-Abbruch oder jeder andere Abbruch, den ein Lauf selbst auslöst, beendet nur diesen Auftrag (Status „Fehler“); der
  nächste startet.
- **Pfadvergleich über aufgelöste Pfade:** Alle Vergleiche von Quellen und Zielen (zwischen Aufträgen und gegen die Felder)
  arbeiten auf vollständig aufgelösten Pfaden (`Path.resolve()`, mit Rückfall auf `abspath`, wenn das scheitert), damit
  Kurznamen (8.3), Verknüpfungen/Junctions und `\\?\`-Schreibweisen denselben Ordner als denselben erkennen. Auch der
  direkte Start („Konvertieren“) lehnt ein Ziel ab, das in oder auf einer Quelle eines Auftrags der Liste liegt.
- **Quelle ist noch dieselbe Karte:** Beim Hinzufügen wird ein Fingerabdruck der Quelle gespeichert (Hash von
  `MEDIAPRO.XML`, falls vorhanden, sowie Namen und Grössen der Clip-Dateien; nur lesend, ohne die Clips zu lesen). Beim
  Start des Auftrags wird er neu gebildet; weicht er ab (andere Karte im selben Kartenleser, Karte entfernt), endet der
  Auftrag sofort als „Fehler“ mit der Meldung „Die Quelle hat sich seit dem Hinzufügen geändert“, ohne etwas zu schreiben.
  Steht dieselbe Quelle schon in einem wartenden Auftrag, gibt es beim Hinzufügen eine Rückfrage („Dieselbe Quelle steht
  schon in der Liste … trotzdem hinzufügen?“).
- **Hinzufügen während eines Laufs muss wirklich möglich sein:** Quelle, Ziel, „Durchsuchen“ und die Einstellungen bleiben
  während eines Laufs bedienbar (sie wirken nur auf neue Aufträge; der laufende Auftrag arbeitet mit seiner Momentaufnahme
  bzw. der Einzellauf mit den beim Start gelesenen Werten). Gesperrt sind während eines Laufs nur „Konvertieren“ und „Alle
  konvertieren“. Die Übersichtszeile („Kamera: …“) darf dabei die Karte aus dem Feld zeigen; Status und Balken gehören
  weiter dem laufenden Auftrag.
- **Kein Einfrieren beim Hinzufügen:** Liegt die geschätzte Zielgrösse noch nicht vor, wird der Auftrag ohne Schätzung
  aufgenommen und die Schätzung im Hintergrund nachgetragen (keine Analyse im Oberflächen-Thread, keine doppelte Analyse
  derselben Karte). Für diesen Auftrag entfällt dann die Platzwarnung beim Hinzufügen.
- **Platzbedarf:** In die Summe je Laufwerk gehen wartende **und abgebrochene** Aufträge ein sowie der laufende Auftrag.
- **Texte nach Abbruch:** Wird vor dem ersten oder zwischen zwei Aufträgen abgebrochen, sagen Statuszeile und Abschlussfenster
  „Listenlauf abgebrochen“ (nicht „fertig“) und nennen die nicht gestarteten Aufträge; es gibt auch dann genau ein
  Abschlussfenster, sofern mindestens ein Auftrag gelaufen ist. Einzahl/Mehrzahl werden ausgeschrieben („1 Auftrag“,
  „3 Aufträge“).
- **Hinweis bei „Fehler“:** Das Abschlussfenster nennt bei Aufträgen mit „Fehler“, dass sie nicht automatisch wiederholt
  werden und zum Fortsetzen entfernt und erneut hinzugefügt werden (fertige Clips werden übersprungen).
- **Protokolldatei:** Die Zeile „Energiesparmodus wird … verhindert“ steht im Listenlauf auch in der Protokolldatei jedes
  Auftrags.

### Abschlussprüfung mit automatischer Wiederholung (Runde 4, Spezifikation)

Anlass: Beim lokalen Test schlug ein Clip wegen eines Lesehängers fehl und musste von Hand per Fortsetzungslauf nachgeholt
werden; die gründliche Prüfung (`--verify --source --deep`) musste ebenfalls von Hand gestartet werden.

- **Ablauf:** Nach der Umwandlung folgt automatisch eine **Abschlussprüfung** der Zielkarte gegen die Quelle – dieselbe Prüfung
  wie `--verify <ziel> --source <quelle> --deep` (keine zweite Implementierung, sondern derselbe Code), mit Fortschrittsanzeige.
- **Wiederholung:** Clips, die in der Umwandlung als Fehler endeten oder in der Abschlussprüfung eine Abweichung haben, werden
  **neu umgewandelt** (erzwungen, auch wenn ein Ziel-Clip existiert) und danach erneut geprüft – höchstens **2 Wiederholungsrunden**
  (einstellbar). Es gilt weiter: Ein vorhandener Ziel-Clip wird erst nach erfolgreicher Neuerzeugung ersetzt; die Quelle wird
  nie verändert. In der Wiederholungsrunde werden nur die betroffenen Clips geprüft (wieder mit vollständigem Dekodieren),
  am Ende einmal die kartenweiten Punkte (`MEDIAPRO.XML`, Begleitdateien).
- **Was nicht wiederholt wird:** Clips, die bewusst nur kopiert wurden (4K, Interlaced, nicht analysierbar usw.), und Abweichungen,
  die keine Neu-Umwandlung beheben kann (z. B. Quelle selbst nicht lesbar). Begleitdateien, die byte-gleich sein müssten und es
  nicht sind, werden einmal neu kopiert und erneut verglichen. Bleibt danach etwas offen, steht es als Abweichung im Ergebnis.
- **Ergebnis:** Zusammenfassung, Protokoll und GUI-Abschlussfenster nennen: geprüfte Clips, Clips ohne Abweichung, wiederholte
  Clips (mit Grund), endgültig fehlerhafte Clips. Nur wenn die Abschlussprüfung ohne Abweichung endet, lautet der Abschluss
  „Fertig – geprüft“; sonst „Fertig mit Fehlern“ mit den betroffenen Clips. Exit-Code der CLI: 0 nur bei geprüfter Karte ohne
  Abweichung.
- **Einstellungen:** Standard **an**, mit vollständigem Dekodieren. GUI: Haken „Nach der Umwandlung prüfen und fehlerhafte Clips
  wiederholen“ und „gründlich (jeden Clip komplett dekodieren)“. CLI: `--no-auto-verify`, `--auto-verify-quick` (ohne
  vollständiges Dekodieren), `--retries N` (0–5, Standard 2).
- **Ausnahmen:** Bei `--max-frames` (Testausgabe) entfällt die Abschlussprüfung. Bei `--only`/`--unlisted skip` gelten die
  bewusst nicht ausgewählten Clips nicht als Fehler (wie bisher in `--verify --source`); die bekannte Meldung zu in
  `MEDIAPRO.XML` gelisteten, wegen `--only` fehlenden Clips führt nicht zu Wiederholungen.
- **Abbruch, Temperatur, Schonmodus:** Abbrechen (GUI, Strg+C) wirkt auch während der Abschlussprüfung; das Ergebnis heißt dann
  „abgebrochen, Prüfung unvollständig“. Temperaturüberwachung (Pause) und `--threads` gelten auch für die Prüfung.
- **Fortsetzen:** Ein Fortsetzungslauf, bei dem nichts mehr umzuwandeln ist, führt trotzdem die Abschlussprüfung aus (so lässt
  sich eine vorhandene Zielkarte nachträglich prüfen und reparieren).
- **Zeitbedarf:** Die gründliche Prüfung kostete im lokalen Test etwa 8 Minuten für 62 Minuten Material (rund 8 % der
  Umwandlungszeit), gemessen vor der Verschärfung unten (vollständiger Tonvergleich liest zusätzlich die Quelle).

Ergänzungen nach dem Review (verbindlich):

- **Was „gründlich“ prüft:** Das vollständige Dekodieren läuft mit `ffmpeg -xerror`; jede Decoder-Meldung über beschädigte
  Bilder ist eine Abweichung (ohne `-xerror` meldet `ffmpeg` beschädigte Bilder nur als Warnung und endet mit Erfolg – im
  Review an einem gezielt beschädigten Clip nachgewiesen). Der **Ton jeder Spur wird vollständig** gegen die Quelle verglichen
  (Prüfsumme je Spur), nicht nur in Ausschnitten. Die schnelle Prüfung vergleicht den Ton weiterhin nur in Ausschnitten und
  dekodiert nur das Clip-Ende; sie heißt in allen Ergebnistexten ausdrücklich „schnelle Prüfung“.
- **Nicht ausgewählte Clips:** Nur bewusst **fehlende**, nicht ausgewählte Clips sind ein Hinweis. Ein Clip, der im Ziel
  **liegt** und eine Abweichung hat, ist immer eine Abweichung (kein „geprüft“), auch wenn er im aktuellen Lauf nicht
  ausgewählt ist; er wird dann nicht wiederholt, der Text sagt, wie man ihn nachholt.
- **Ersetzen bei Wiederholung:** Eine erzwungen neu erzeugte Datei ersetzt den vorhandenen Ziel-Clip erst, nachdem sie selbst
  die gründliche Prüfung bestanden hat. Ein Rückfall ohne ANC-Spur oder ohne eingebettetes XML ersetzt keinen Clip, der diese
  Spur hat. Fehler beim Lesen der **Quelle** lösen keine Wiederholung aus (Abweichung mit Hinweis auf Karte/Kartenleser).
- **Testausgabe im Ziel:** Liegt im Ziel die Marke einer `--max-frames`-Testausgabe, gibt es keine Wiederholung; die Prüfung
  meldet das als Problem.
- **Nicht repariert, nur gemeldet:** Abweichungen in `MEDIAPRO.XML` und `M01.XML` (diese Dateien erzeugt das Tool selbst).
- **Zahlen:** „geprüft“ und „ohne Abweichung“ werden aus dem Endstand je Clip über alle Runden gebildet.
- **Ergebniszeile:** Die letzte Zeile der Zusammenfassung ist immer genau eine Zeile `Ergebnis: …`.
- **Exit-Codes der CLI:** 0 fertig (mit Abschlussprüfung: geprüft ohne Abweichung; auch bei ausgeschalteter Prüfung oder
  Testausgabe ohne Fehler), 1 Fehler oder Abweichung oder Prüfung nicht möglich, 2 falscher Aufruf, 3 Rechte-Erhöhung
  abgelehnt, 130 abgebrochen (auch Temperatur-Abbruch).
- **Intern** ist die Abschlussprüfung aus (`Options.auto_verify = False`); CLI und GUI schalten sie ein. Die Zahl der
  Wiederholungsrunden ist nur in der CLI einstellbar (GUI: 2).

### Beleglage (Stand 16.11.2026)

Mit echten Kameradateien unter Windows belegt: Parser, Patcher, Prüfungen und Toninhalts-Vergleich (19 F5-Clips und eine
F55-Karte mit 76 Clips, `--verify --source --deep` ohne Abweichung), Satz-für-Satz-Vergleich mit einer FX6-Referenz, `.bat`-Start,
Sperre, Strg+C, Fortsetzen, hartes Beenden, Fortsetzen nach einem Rechnerabsturz. Zahlen und verbleibende Abweichungen:
`docs/NAECHSTE-SCHRITTE.md`.

Von Hand geprüft: Import der umgewandelten F55-Karte in DaVinci Resolve und Sony Catalyst Browse (Karte erkannt, Wiedergabe,
Tonspuren; Timecode-Anzeige nicht verglichen).

Noch nicht belegt: andere Schnittprogramme und Sony-Werkzeuge, Clips über 10 GB (WAV > 4 GB), andere Bildraten als 50p mit
Kameradateien, Wiederholungen bei gesperrten Dateien, GUI-Bedienung von Hand, echte Temperaturmessung und Lüftersteuerung.
