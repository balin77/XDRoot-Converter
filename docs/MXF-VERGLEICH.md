# MXF-Kopfdaten-Vergleich (Runde 2, Stand nach den Fix-Runden, neu gemessen)

> **Wichtig zur Beleglage (bitte zuerst lesen)**
>
> Im Repository liegen von den **echten Kameradateien nur `mxf2raw`-Ausgaben**
> (`docs/analysis/f55_mxf2raw.txt`, `fx6_mxf2raw.txt`), **keine `MXFDump`-Ausgaben**. Der
> Satz-für-Satz-Vergleich mit `MXFDump`/`scripts/mxf_vergleich.py` wurde deshalb **nur gegen eine
> SYNTHETISCHE Quelle** belegt: eine mit bmx erzeugte Datei (`scripts/make_test_card.py`), die
> nachträglich auf Sony-Track-IDs (2, 3..10, 11) und Sony-Identification gesetzt wurde.
>
> Was **echte** Sony-Dateien in den von `mxf2raw` **nicht** gezeigten Feldern enthalten (Track-Namen,
> Track-ID des Timecode-Tracks, weitere Deskriptor-Items, Sony-eigene Sets, Fill-Größen,
> Partitionslayout) ist hier **NICHT geprüft**. Das Tool übernimmt die von `mxfheader.adopt_from_source`
> behandelten Felder (Identification, Track-IDs, Track-Namen, Sound-Deskriptor-Items, MIME-Typ/Description
> des TextObjects) zur Laufzeit aus der echten Quell-MXF; alles darüber hinaus wird nicht angefasst.
>
> **Die synthetische Quelle ist nicht die echte F55.** Sie zeigt Eigenschaften, die bei der echten F55 laut
> `docs/analysis/f55_mxf2raw.txt` **anders** sind (an den Stellen unten mit „synth.“ gekennzeichnet): z. B.
> `stored_height` 1080 (synthetisch) gegen 1088 (echte F55), Partitionsreihenfolge (Index vor Essenz, vom
> Generator so gebaut), Spurzahl/Track-Namen, die Art des Video-Deskriptor-Sets (die echte F55 wurde nie mit `MXFDump`
> geöffnet; `mxf2raw` unterscheidet MPEG-Video- und CDCI-Deskriptor nicht). Keine Zeile der Quell-Spalte unten ist
> eine Aussage über Sony.
>
> Die auf der echten Hardware offenen Prüfungen stehen in Abschnitt 6 („Was der Autor lokal prüfen muss“).
>
> Zahlen und Tabellen unten wurden mit einer frisch erzeugten Testkarte neu gemessen (Linux, bmx 1.6, Ziel jetzt **KAG 512**,
> `--head-fill 65536`). Die Zahl der Abweichungen (117 / 30 / 45) ist gegenüber der letzten Messung unverändert.

## 1. Methode und Befehle

```
# Testkarte (Quelle ist "Sony-aehnlich": Track-IDs 2/3..10/11, Identification Sony/"Mem "/2.00;
# Standard KAG 512, --kag1 waehlt KAG 1; das Ziel uebernimmt das KAG der Quelle)
python scripts/make_test_card.py _test_out/synth_card

# Umwandlung (Runde 2: Kopfdaten werden aus der Quell-MXF uebernommen)
python -m xdroot_converter.cli _test_out/synth_card _test_out/out2 --embed-xml always

# Pruefung (mit und ohne Quelle)
python -m xdroot_converter.cli --verify _test_out/out2 --source _test_out/synth_card --deep
python -m xdroot_converter.cli --verify _test_out/out2

# Satz-fuer-Satz-Vergleich (eigener Parser + MXFDump-Gegenprobe, falls MXFDump im PATH/tools/ liegt;
# ohne --embed-xml always fehlen im Ziel DMSchemes und das TextObject, die Zahl der Abweichungen sinkt von 45 auf 30)
python scripts/mxf_vergleich.py "_test_out/synth_card/XDROOT/Clip/<clip>.MXF" \
                                "_test_out/out2/XDROOT/Clip/<clip>.MXF" -o vergleich.md
# dasselbe fuer eine Ausgabe der Version 1 (--no-sony-header) zeigt den Unterschied:
python -m xdroot_converter.cli _test_out/synth_card _test_out/out_v1 --no-sony-header
python scripts/mxf_vergleich.py "<quelle>.MXF" "_test_out/out_v1/XDROOT/Clip/<clip>.MXF" -o vergleich_v1.md

# Diagnose einer einzelnen Datei
python -m xdroot_converter.cli --mxf-info <datei>.MXF
mxf2raw -i <datei>.MXF
```

Ablauf in der Umwandlung: `raw2bmx --prod-info <Company> <Product> 0.0.0.0.0 <VersionString> <UID>` (Werte aus der
Quell-MXF, Rückfall Sony / `Mem ` / 2.00 / `cede1104-8280-11de-8a39-08004678031c`), `--kag-size-512` (wenn die Quelle KAG 512 hat),
`--head-fill 65536` (Fill-Reserve in der Header-Region) und `--seq-off` (aus der Quelle), danach
`mxfheader.adopt_from_source(ziel, quelle)` (Python-Patcher, schreibt die Header-Metadaten in place,
Länge der Header-Region bleibt unverändert, alle Offsets bleiben gültig), danach die Prüfung
(`verify.check_clip(..., source=quelle)` = bisherige Prüfungen + `mxfheader.compare_with_source` +
mxf2raw-Felder).

## 2. Abweichungen bmx-Version 1 → Runde 2

Grundlage: Vergleich Quelle (**synth.**, Sony-ähnlich, KAG 512) gegen Ausgabe Version 1 (`--no-sony-header`: 117 Abweichungen in
45 Sätzen) bzw. Runde 2 (Standard, ohne eingebettetes XML: 30 Abweichungen in 3 Sätzen, alle im Video-Deskriptor; mit
`--embed-xml always` 45 Abweichungen in 10 Sätzen, die zusätzlichen 15 sind das gewollte TextObject, siehe Abschnitt 4).
Zahlen neu gemessen (Linux, bmx 1.6, 3-Clip-Testkarte, ein Clip verglichen).

| Bereich | bmx-Version 1 | Runde 2 | Status | Begründung |
|---|---|---|---|---|
| Identification `CompanyName` / `ProductName` | `BBC` / `bmx` | `Sony` / `Mem ` (Leerzeichen am Ende) | behoben | `raw2bmx --prod-info` aus der Quelle; der Patcher gleicht zusätzlich ab |
| Identification `VersionString` / `ProductUID` | `1.6.0 (scm v1.6)` / bmx-UID | `2.00` / `cede1104-8280-11de-8a39-08004678031c` | behoben | wie oben |
| Identification `ProductVersion` | `1.6.0.0.0` | fehlt (wie Sony) | behoben | `--prod-info ... 0.0.0.0.0` lässt es weg |
| Identification `ToolkitVersion`, `Platform` | `1.6.0.0.0`, `libMXF (Linux 64-bit)` | fehlen (wie Sony) | behoben | in raw2bmx nicht abschaltbar; der Patcher entfernt die Items |
| Identification `ThisGenerationUID` | zufällig | zufällig (neu je Datei) | bewusst belassen | dateispezifisch; auch Sony vergibt je Datei eine eigene. Eine fremde Generation-UID zu kopieren würde zwei verschiedene Dateien als gleiche Erzeugung ausgeben |
| Identification `ModificationDate` | Erzeugungszeit | Datum der Quell-Identification | behoben (schon V1) | `--create-date` |
| Track-IDs Material- und File Package | 901 (TC), 1001 (V), 2001..2008 (A), 3001 (ANC) | wie Quelle (TC 1, V 2, A 3..10, ANC 11) | behoben | Track-IDs sind in bmx (`OP1AFile.cpp`) fest verdrahtet; Patcher setzt `TrackID` und zieht `SourceTrackID` (SourceClips), `LinkedTrackID` (Deskriptoren) und alle Verweise nach |
| Track-Reihenfolge | bmx-Reihenfolge | Reihenfolge der Quelle | behoben | Patcher sortiert `GenericPackage.Tracks` |
| Track-Namen (`TrackName`) | `TC1`, `V1`, `A1`.., `D1` | wie Quelle (in der synthetischen Quelle: keine) | behoben (für synth. Quelle), echte Namen **nicht geprüft** | Patcher übernimmt/entfernt je nach Quelle |
| Sound-Deskriptor-Items | `AvgBps` vorhanden, `SequenceOffset` fehlt | wie Quelle (`SequenceOffset`, kein `AvgBps`) | behoben (für synth. Quelle), echte Items **nicht geprüft** | Patcher übernimmt die Item-Liste des Quell-Deskriptors, behält nur `InstanceUID`, `ContainerDuration`, `SampleRate`, `EssenceContainer` des Ziels |
| Sound-Deskriptor-Felder (mxf2raw) | gleich | gleich (sampling_rate 48000/1, 24 Bit, block_align 3, 1 Kanal, locked, ref 0) | identisch | `--aes-3`, `--locked`, `--audio-ref` aus der Quelle |
| Track-Nummern der Tonspuren | `0x1608 03xx` | gleich | identisch | `--aes-3` |
| TextObject `mime_type` | `application/xml` | `text/xml` (bzw. Wert der Quelle) | behoben | Patcher; in raw2bmx nicht setzbar |
| TextObject `description` | bmx-Wert | leer wie FX6 (bzw. Wert der Quelle) | behoben | Patcher |
| Video-Deskriptor | MPEG-Video-Deskriptor (**synth.** Quelle; Art des Sets bei der echten F55 ungeprüft) | `CDCIEssenceDescriptor` + `AVCSubDescriptor`, `PictureEssenceCoding` `…0d.04010202.01316001`, StoredHeight 1088 u. a. | bewusst belassen | das ist der Zweck der Umwandlung (XAVC-L); entspricht der FX6-Referenz (`stored_height 1088`, `display_x/y_offset 0`). Die synth. Quelle hat 1080, die **echte F55 laut `mxf2raw` 1088**, dort wird dieser Unterschied also nicht auftreten |
| Partitionslayout / Index / KAG | Header, Essenz-Body, Index-Body, Footer (bmx), KAG 1 | unverändert (Patcher verschiebt nichts), **KAG wie die Quelle (hier 512)** | bewusst belassen, echte Sony-Werte **nicht geprüft** | `mxf2raw` zeigt das Layout nicht. Die synth. Quelle hat KAG 512 und die Reihenfolge Index vor Essenz (Generator), das Ziel dank `--kag-size-512` ebenfalls KAG 512, aber Essenz vor Index (so schreibt `raw2bmx` hier); beides ist kein Beleg für Sony. Der Patcher *liest und schreibt* KAG 512 (Fill zwischen Partition Pack und Primer bleibt byte-genau) |
| KLV-Fill (Padding) | bmx-Größen | `--head-fill 65536` reserviert Platz; nur der letzte Fill der Header-Region wird gekürzt/verlängert (hier nach dem Patchen 364 Byte gegen 176 Byte in der Quelle); bei KAG > 1 bleibt der Fill vor dem Primer unverändert | bewusst belassen | hält `HeaderByteCount` und alle Offsets/Indizes stabil; Sony-Fill-Größen sind unbekannt |
| MXFDump-Warnung „Length is zero“ | – | bei KAG 512 gelegentlich „Warning : Length is zero (following KLVFill key …)“ für Füll-KLVs in der Essenz | bekannt, harmlos | Lücke von genau 20 Byte, stammt aus bmx/libMXF; gültiges KLV, `mxf2raw` und `ffmpeg` lesen fehlerfrei. Gemessen: bei 2-s-Clips (6 Dateien, Quelle und Ziel) nie, bei einem von zwei 20-s-Zielclips dreimal. Die Quelle (bmx erzeugt) zeigte sie in keiner Messung |
| Footer-Partition | keine Header-Metadaten im Footer | gleich | nicht nötig | der Patcher bearbeitet alle Partitionen mit `HeaderByteCount > 0` (hier nur die Header-Partition) |
| Sony-eigene Sets / weitere Items echter Dateien | – | nicht übernommen | nicht geprüft | ohne MXFDump-Ausgabe echter Dateien nicht beurteilbar (siehe Hinweis oben) |
| Timecode-Track (ID, Name) | 901 / `TC1` | wie Quelle (ID 1) | behoben (für synth. Quelle) | Patcher paart nach Art und Reihenfolge |

## 3. mxf2raw: Ziel gegen die echten Referenzen

Ziel = Ausgabe der synthetischen Karte (`--embed-xml always` für das TextObject), Referenzen = `docs/analysis/*_mxf2raw.txt`.
Verglichen wurde die normalisierte `mxf2raw -i`-Ausgabe (ohne `file_uri`, Package-UIDs, Datumswerte, Dauern). Das Ziel ist hier
ein am Stand nach den Fix-Runden frisch erzeugter Clip; ein `diff` gegen die F55-Ausgabe zeigt außer den gelisteten, gewollten
Unterschieden nur Zeitstempel, Generation-UID und das TextObject.

**Gegen F55 (XAVC-I, 8 Tonspuren, ANC mit 1 Element)** – gleiche Spurzahl, daher vollständig vergleichbar:

| Feld | Ergebnis |
|---|---|
| Identification `company_name`, `product_name` (`Mem `), `version_string`, `product_uid`, kein toolkit/platform | identisch |
| `op_label`, `frame_wrapped`, `mxf_version` | identisch |
| Track-IDs Material / File Package: Video 2, Audio 3..10, ANC 11 | identisch |
| Track-Nummern (Video `0x15010500`, Audio `0x160803xx`, ANC) | identisch |
| Audio: `essence_type`, `ec_label` (`…02060300`), `sampling_rate`, `bits_per_sample`, `block_align`, `channel_count`, `locked`, `audio_ref_level` | identisch |
| Video: `ec_label`, `signal_standard`, `frame_layout`, `stored_width/height` (1088), `display_*`, `aspect_ratio`, `component_depth`, Subsampling, `color_siting` | identisch |
| Video `essence_type` | **abweichend, gewollt**: `AVCI_100_1080p` → `AVC_High_422` |
| Video `coding_label` | **abweichend, gewollt**: `…0401010a.04010202.01323104` → `…0401010d.04010202.01316001` |
| Video `avci_header` | **abweichend, gewollt**: entfällt bei XAVC-L |
| ANC-Manifest (1 Element, DID 0x43 / SDID 0x05) | identisch |
| TextObject | F55 hat keins; bei `--embed-xml auto` (Standard) entsteht auch keins; mit `--embed-xml always` entsteht eins mit `text/xml`, `und`, leerer description, `scheme_id` `urn:uuid:786d4d22-8280-11de-8a39-08004678031c`, Track-ID = Audiospuren + 4 (hier 12) |

**Gegen FX6 (XAVC-L, 4 Tonspuren, ANC mit 13 Elementen)** – echtes XAVC-L, anderes Spurlayout:

| Feld | Ergebnis |
|---|---|
| Identification (alle Felder), `op_label`, `frame_wrapped` | identisch |
| Video-Spur komplett (`essence_type`, `ec_label`, `coding_label`, Deskriptor, Track-ID 2, Track-Nummer) | identisch |
| Audio-Felder (`essence_type`, `ec_label`, Deskriptor) | identisch |
| Audio-Track-Nummern | `0x160403xx` (FX6, 4 Kanäle) gegen `0x160803xx` (Ziel, 8 Kanäle wie die F55-Quelle): hängt von der Kanalzahl ab, gleich zur F55 |
| Spurzahl, Audio-Track-IDs (3..6 gegen 3..10), ANC-Track-ID (7 gegen 11) | Muster gleich (Audio ab 3 fortlaufend, ANC danach), Werte folgen der Kanalzahl |
| ANC-Manifest | FX6 13 Elemente, Ziel so viele wie die Quelle (F55: 1) – die ANC-Spur wird byte-identisch aus der Quelle übernommen |
| Start-Timecode, Dauer, UMIDs | dateispezifisch |
| TextObject (`mime_type text/xml`, `lang_code und`, leere `description`, Encoding UTF8) | identisch (bei `--embed-xml always`) |

## 4. Generierter Vergleichsbericht (Quelle synthetisch, gekürzt)

Erzeugt mit `scripts/mxf_vergleich.py`, neu gemessen (Ausschnitte; der vollständige Bericht hat rund 1100 Zeilen). Die Quell-Spalte ist **synthetisch**. Alle nicht aufgeführten
Sätze (Preface, ContentStorage, EssenceContainerData, Material Package inkl. Tracks/Sequences/SourceClips, File Package inkl.
Tracks, 8 Sound-Deskriptoren, ANC-Deskriptor) sind in Quelle und Ziel identisch.

### 4.1 Zusammenfassung der Abweichungen Quelle ≠ Ziel
Abweichungen Quelle ≠ Ziel (Standardlauf ohne eingebettetes XML): **30** in 3 Sätzen – alle im Video-Deskriptor (XAVC-I → XAVC-L, gewollt) und in der Liste der Unter-Deskriptoren. Mit `--embed-xml always` kommen 15 weitere in 7 Sätzen dazu: `Preface.DMSchemes`, ein zusätzlicher `StaticTrack` (Track-ID 12) mit `DMSegment`/`TextBasedDMFramework`/`UTF8TextBasedSet` (`text/xml`, `und`, leere Beschreibung) – das ist das gewollte eingebettete XML; die Quelle hat keins.

| Satz | Item | Quelle | Ziel |
|---|---|---|---|
| File Source Package / Deskriptor MultipleDescriptor | SubDescriptorUIDs | -> MPEGVideoDescriptor, 8x AES3AudioDescriptor, ANCDataDescriptor | -> CDCIEssenceDescriptor, 8x AES3AudioDescriptor, ANCDataDescriptor |
| File Source Package / Deskriptor Picture #1 | (Set-Klasse) | MPEGVideoDescriptor | CDCIEssenceDescriptor |
| File Source Package / Deskriptor Picture #1 | PictureEssenceCoding | 060e2b34.0401010a.04010202.01323104 | 060e2b34.0401010d.04010202.01316001 |
| File Source Package / Deskriptor Picture #1 | BlackRefLevel | 64 | (fehlt) |
| File Source Package / Deskriptor Picture #1 | WhiteReflevel | 940 | (fehlt) |
| File Source Package / Deskriptor Picture #1 | ColorRange | 897 | (fehlt) |
| File Source Package / Deskriptor Picture #1 | StoredHeight | 1080 | 1088 |
| File Source Package / Deskriptor Picture #1 | VideoLineMap | 00000002000000040000002a00000000 | 00000002000000040000000100000000 |
| File Source Package / Deskriptor Picture #1 | SampledHeight | 1080 | 1088 |
| File Source Package / Deskriptor Picture #1 | SubDescriptors | (fehlt) | -> AVCSubDescriptor |
| File Source Package / Deskriptor Picture #1 | DisplayXOffset | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 | DisplayYOffset | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 | SampledXOffset | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 | SampledYOffset | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 | ImageStartOffset | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 | PaddingBits | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCProfile | (fehlt) | 122 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCProfileConstraint | (fehlt) | 0 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCLevel | (fehlt) | 42 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCDecodingDelay | (fehlt) | 1 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCConstantBPictureFlag | (fehlt) | false |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCCodedContentKind | (fehlt) | 1 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCClosedGOPIndicator | (fehlt) | true |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCIdenticalGOPIndicator | (fehlt) | true |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCMaximumGOPSize | (fehlt) | 24 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCMaximumBPictureCount | (fehlt) | 2 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCMaximumBitrate | (fehlt) | 50000000 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCMaximumRefFrames | (fehlt) | 3 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCSequenceParameterSetFlag | (fehlt) | 176 |
| File Source Package / Deskriptor Picture #1 / AVCSubDescriptor | AVCPictureParameterSetFlag | (fehlt) | 176 |

### 4.2 Partitionen
**Quelle (synth.; Standard KAG 512, Index vor Essenz – Generator, kein Sony-Beleg)**

| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |
|---|---|---|---|---|---|---|
| 0 | header | 4 | 512 | ja | 0 | 0 |
| 1 | body | 4 | 512 | nein | 0 | 1 |
| 2 | body | 4 | 512 | nein | 2 | 0 |
| 3 | footer | 4 | 512 | nein | 0 | 0 |

**Ziel**

| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |
|---|---|---|---|---|---|---|
| 0 | header | 4 | 512 | ja | 0 | 0 |
| 1 | body | 4 | 512 | nein | 2 | 0 |
| 2 | body | 4 | 512 | nein | 0 | 1 |
| 3 | footer | 4 | 512 | nein | 0 | 0 |

Laut MXFDump (Partition Packs): Quelle `Header (KAG 512, Header-Metadaten ja, BodySID 0, IndexSID 0); Body (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 1); Body (KAG 512, Header-Metadaten nein, BodySID 2, IndexSID 0); Footer (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 0)`; Ziel `Header (KAG 512, Header-Metadaten ja, BodySID 0, IndexSID 0); Body (KAG 512, Header-Metadaten nein, BodySID 2, IndexSID 0); Body (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 1); Footer (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 0)`.
MXFDump meldet für Quelle und Ziel dieser 2-s-Testkarte keine Auffälligkeiten (keine Zeilen mit error/warning/invalid/unexpected/mismatch; zur „Length is zero“-Warnung bei längeren Clips siehe Abschnitt 2);
die Satzzählung von MXFDump stimmt mit dem eigenen Parser überein.

### 4.3 Identification
Klasse: `Identification`

| Item | Quelle | Ziel | Abw. |
|---|---|---|---|
| ThisGenerationUID | (dateispezifisch) | (dateispezifisch) |  |
| ModificationDate | (dateispezifisch) | (dateispezifisch) |  |
| CompanyName | 'Sony' | 'Sony' |  |
| ProductName | 'Mem ' | 'Mem ' |  |
| VersionString | '2.00' | '2.00' |  |
| ProductUID | cede1104.828011de.8a390800.4678031c | cede1104.828011de.8a390800.4678031c |  |

### 4.4 Tonspur 1 (Material Package Track und File-Package-Deskriptor)
Klasse: `Track`

| Item | Quelle | Ziel | Abw. |
|---|---|---|---|
| TrackID | 3 | 3 |  |
| TrackNumber | 0 | 0 |  |
| EditRate | 50/1 | 50/1 |  |
| Origin | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence |  |

Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Abw. |
|---|---|---|---|
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 |  |
| SampleRate | 50/1 | 50/1 |  |
| AudioSamplingRate | 48000/1 | 48000/1 |  |
| Locked | true | true |  |
| ChannelCount | 1 | 1 |  |
| QuantizationBits | 24 | 24 |  |
| AudioRefLevel | 0 | 0 |  |
| BlockAlign | 3 | 3 |  |
| SequenceOffset | 0 | 0 |  |
| LinkedTrackID | 3 | 3 |  |
| ContainerDuration | (dateispezifisch) | (dateispezifisch) |  |

### 4.5 Video-Deskriptor (gewollte Abweichungen; Quelle synth.)
**Set-Klasse weicht ab:** Quelle `MPEGVideoDescriptor`, Ziel `CDCIEssenceDescriptor`.

| Item | Quelle | Ziel | Abw. |
|---|---|---|---|
| EssenceContainer | 060e2b34.0401010a.0d010301.02106001 | 060e2b34.0401010a.0d010301.02106001 |  |
| SampleRate | 50/1 | 50/1 |  |
| AspectRatio | 16/9 | 16/9 |  |
| SignalStandard | 4 | 4 |  |
| ColorSiting | 0 | 0 |  |
| PictureEssenceCoding | 060e2b34.0401010a.04010202.01323104 | **060e2b34.0401010d.04010202.01316001** | Q≠Z |
| FrameLayout | 0 | 0 |  |
| ComponentDepth | 10 | 10 |  |
| BlackRefLevel | 64 | **–** | Q≠Z |
| WhiteReflevel | 940 | **–** | Q≠Z |
| ColorRange | 897 | **–** | Q≠Z |
| StoredWidth | 1920 | 1920 |  |
| StoredHeight | 1080 | **1088** | Q≠Z |
| VideoLineMap | 00000002000000040000002a00000000 | **00000002000000040000000100000000** | Q≠Z |
| DisplayWidth | 1920 | 1920 |  |
| DisplayHeight | 1080 | 1080 |  |
| SampledWidth | 1920 | 1920 |  |
| SampledHeight | 1080 | **1088** | Q≠Z |
| HorizontalSubsampling | 2 | 2 |  |
| VerticalSubsampling | 1 | 1 |  |
| LinkedTrackID | 2 | 2 |  |
| ColorPrimaries | 060e2b34.04010106.04010101.03030000 | 060e2b34.04010106.04010101.03030000 |  |
| CaptureGamma | 060e2b34.04010101.04010101.01020000 | 060e2b34.04010101.04010101.01020000 |  |
| CodingEquations | 060e2b34.04010101.04010101.02020000 | 060e2b34.04010101.04010101.02020000 |  |
| ContainerDuration | (dateispezifisch) | (dateispezifisch) |  |
| SubDescriptors | – | **-> AVCSubDescriptor** | Q≠Z |
| DisplayXOffset | – | **0** | Q≠Z |
| DisplayYOffset | – | **0** | Q≠Z |
| SampledXOffset | – | **0** | Q≠Z |
| SampledYOffset | – | **0** | Q≠Z |
| ImageStartOffset | – | **0** | Q≠Z |
| PaddingBits | – | **0** | Q≠Z |

## 5. Grenzen

- Quelle des Vergleichs ist eine synthetische Datei; sie bestätigt die **Mechanik** (Track-IDs, Verweise, Strings, Deskriptor-Items
  werden konsistent geändert; KAG 512 wird gelesen und geschrieben; mxf2raw und ffmpeg lesen das Ergebnis ohne Auffälligkeiten, MXFDump bis auf die harmlose „Length is zero“-Warnung bei längeren Clips (Abschnitt 2);
  ffmpeg dekodiert es vollständig), nicht den Inhalt echter Sony-Dateien.
- Das Ziel übernimmt das KAG der Quelle (512 mit `--kag-size-512`; bei KAG 1 der Quelle bmx-Standard); ob Sony-XAVC-L (FX6) und XAVC-I (F55) tatsächlich KAG 512 nutzen, ist nicht belegt (`mxf2raw` zeigt es nicht) – `--mxf-info` auf einen echten Clip klärt das (Abschnitt 6). Nicht-nachbildbare KAG-Werte (weder 1 noch 512) werden mit Hinweis auf KAG 1 gesetzt.
- Nicht geprüft: Verhalten von Schnittprogrammen und Sony-Software mit den Dateien.

## 6. Was der Autor lokal prüfen muss

Mit den echten Dateien (lokal, Pfade nach Bedarf einsetzen; Berichte enthalten keine Pfade, UIDs/Zeiten nur als „dateispezifisch“ –
`--rohwerte` und `--dump-dir` nicht für Veröffentlichungen verwenden):

1. **Versteht der Parser echte Sony-Kopfdaten?** (liest nur, ändert nichts)
   ```
   python -m xdroot_converter.cli --mxf-info <F55-Clip.MXF>
   ```
   Zu prüfen: Wird die Datei fehlerfrei gelesen? KAG (Partitionsliste oben in der Ausgabe), Track-IDs (auch der Timecode-Track),
   Track-Namen, Items der Sound-Deskriptoren (z. B. `SequenceOffset`, `AvgBps`), Art des Video-Deskriptor-Sets, Sony-eigene Sets,
   Anzahl der Identifications. Vor dem Weitergeben Seriennummern/UMIDs/Pfade entfernen.
2. **Umwandlung der F5-Testkarte und Prüfung gegen die Quelle** (inkl. Toninhalts-Vergleich je Spur)
   ```
   python -m xdroot_converter.cli <F5-Testkarte> <Ziel-Ordner>
   python -m xdroot_converter.cli --verify <Ziel-Ordner> --source <F5-Testkarte> --deep
   ```
3. **Satz-für-Satz-Vergleich mit echter Quelle und echter Referenz**
   ```
   python scripts/mxf_vergleich.py <F55-Quelle.MXF> <Ziel-Clip.MXF> --referenz <FX6-Clip.MXF> -o bericht.md
   ```
   Abweichungen `Q≠Z`, die nicht in der Tabelle in Abschnitt 2 stehen, und alle `R≠Z` sind neue Befunde.
4. **Öffnen in Schnittprogramm und Sony Catalyst** (Import, Abspielen, Spulen, Tonspuren, Timecode, Metadatenanzeige) mit einem
   umgewandelten Clip und zum Vergleich einem FX6-Clip.
