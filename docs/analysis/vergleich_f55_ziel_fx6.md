# MXF-Kopfdaten-Vergleich

Erzeugt mit `scripts/mxf_vergleich.py` am 2026-11-16 09:37 UTC.  Spalten: Quelle, Ziel, Referenz.  "Abw.": `Q≠Z` = Ziel weicht von der Quelle ab, `R≠Z` = Ziel weicht von der Referenz ab.  `(dateispezifisch)` = Wert ist je Datei verschieden (UIDs, UMIDs, Zeiten, Dauern, Namen, Daten) und wird weder angezeigt noch verglichen.

## Zusammenfassung

Abweichungen Quelle ≠ Ziel: **20** (in 3 Saetzen).  Sie sind unten je Satz markiert.

| Satz | Item | Quelle | Ziel |
|---|---|---|---|
| File Source Package / Deskriptor MultipleDescriptor | SubDescriptorUIDs | -> MPEGVideoDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, ANCDataDescriptor | -> CDCIEssenceDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, ANCDataDescriptor |
| File Source Package / Deskriptor Picture #1 | (Set-Klasse) | MPEGVideoDescriptor | CDCIEssenceDescriptor |
| File Source Package / Deskriptor Picture #1 | PictureEssenceCoding | 060e2b34.0401010a.04010202.01323104 | 060e2b34.0401010d.04010202.01316001 |
| File Source Package / Deskriptor Picture #1 | SubDescriptors | (fehlt) | -> AVCSubDescriptor |
| File Source Package / Deskriptor Picture #1 | ColorPrimaries | (fehlt) | 060e2b34.04010106.04010101.03030000 |
| File Source Package / Deskriptor Picture #1 | CodingEquations | (fehlt) | 060e2b34.04010101.04010101.02020000 |
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

## Partitionen

**Quelle**

| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |
|---|---|---|---|---|---|---|
| 0 | header | 4 | 512 | ja | 2 | 1 |
| 1 | footer | 4 | 512 | nein | 0 | 0 |

**Ziel**

| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |
|---|---|---|---|---|---|---|
| 0 | header | 4 | 512 | ja | 0 | 0 |
| 1 | body | 4 | 512 | nein | 2 | 0 |
| 2 | body | 4 | 512 | nein | 0 | 1 |
| 3 | footer | 4 | 512 | nein | 0 | 0 |

**Referenz**

| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |
|---|---|---|---|---|---|---|
| 0 | header | 4 | 512 | ja | 2 | 1 |
| 1 | footer | 4 | 512 | nein | 0 | 0 |

## Satz fuer Satz

### Preface
Klasse: `Preface`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LastModifiedDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| Version | 1.3 | 1.3 | 1.3 |  |
| Identifications | -> Identification | -> Identification | -> Identification |  |
| ContentStorage | -> ContentStorage | -> ContentStorage | -> ContentStorage |  |
| OperationalPattern | 060e2b34.04010101.0d010201.01010900 | 060e2b34.04010101.0d010201.01010900 | 060e2b34.04010101.0d010201.01010900 |  |
| EssenceContainers | [060e2b34.0401010a.0d010301.02106001, 060e2b34.04010101.0d010301.02060300, 060e2b34.04010109.0d010301.020e0000, 060e2b34.04010103.0d010301.027f0100] | [060e2b34.0401010a.0d010301.02106001, 060e2b34.04010101.0d010301.02060300, 060e2b34.04010109.0d010301.020e0000, 060e2b34.04010103.0d010301.027f0100] | [060e2b34.0401010a.0d010301.02106001, 060e2b34.04010101.0d010301.02060300, 060e2b34.04010109.0d010301.020e0000, 060e2b34.04010103.0d010301.027f0100] |  |
| DMSchemes | [] | [] | [060e2b34.0401010c.0d010401.04010100] | R≠Z |

### Identification #1
Klasse: `Identification`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| ThisGenerationUID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| CompanyName | 'Sony' | 'Sony' | 'Sony' |  |
| ProductName | 'Mem ' | 'Mem ' | 'Mem ' |  |
| VersionString | '2.00' | '2.00' | '2.00' |  |
| ProductUID | cede1104.828011de.8a390800.4678031c | cede1104.828011de.8a390800.4678031c | cede1104.828011de.8a390800.4678031c |  |
| ModificationDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |

### ContentStorage
Klasse: `ContentStorage`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| Packages | -> MaterialPackage, SourcePackage | -> MaterialPackage, SourcePackage | -> MaterialPackage, SourcePackage |  |
| EssenceContainerData | -> EssenceContainerData | -> EssenceContainerData | -> EssenceContainerData |  |

### EssenceContainerData #1
Klasse: `EssenceContainerData`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedPackageUID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| IndexSID | 1 | 1 | 1 |  |
| BodySID | 2 | 2 | 2 |  |

### Material Package
Klasse: `MaterialPackage`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| PackageUID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| PackageCreationDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| PackageModifiedDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| Tracks | -> Track, Track, Track, Track, Track, Track, Track, Track, Track, Track, Track | -> Track, Track, Track, Track, Track, Track, Track, Track, Track, Track, Track | -> Track, Track, Track, Track, Track, Track, Track, StaticTrack | R≠Z |

### Material Package / Track Timecode #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 1 | 1 | 1 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Timecode #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> TimecodeComponent | -> TimecodeComponent | -> TimecodeComponent |  |

### Material Package / Track Timecode #1 / TimecodeComponent
Klasse: `TimecodeComponent`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| RoundedTimecodeBase | 50 | 50 | 50 |  |
| StartTimecode | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| DropFrame | false | false | false |  |

### Material Package / Track Picture #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 2 | 2 | 2 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Picture #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Picture #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 2 | 2 | 2 |  |

### Material Package / Track Sound #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 3 | 3 | 3 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Sound #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Sound #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 3 | 3 | 3 |  |

### Material Package / Track Sound #2
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 4 | 4 | 4 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Sound #2 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Sound #2 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 4 | 4 | 4 |  |

### Material Package / Track Sound #3
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 5 | 5 | 5 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Sound #3 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Sound #3 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 5 | 5 | 5 |  |

### Material Package / Track Sound #4
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 6 | 6 | 6 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Sound #4 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Sound #4 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 6 | 6 | 6 |  |

### Material Package / Track Sound #5
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 7 | 7 | – | R≠Z |
| TrackNumber | 0 | 0 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### Material Package / Track Sound #5 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### Material Package / Track Sound #5 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 7 | 7 | – | R≠Z |

### Material Package / Track Sound #6
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 8 | 8 | – | R≠Z |
| TrackNumber | 0 | 0 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### Material Package / Track Sound #6 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### Material Package / Track Sound #6 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 8 | 8 | – | R≠Z |

### Material Package / Track Sound #7
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 9 | 9 | – | R≠Z |
| TrackNumber | 0 | 0 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### Material Package / Track Sound #7 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### Material Package / Track Sound #7 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 9 | 9 | – | R≠Z |

### Material Package / Track Sound #8
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 10 | 10 | – | R≠Z |
| TrackNumber | 0 | 0 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### Material Package / Track Sound #8 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### Material Package / Track Sound #8 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 10 | 10 | – | R≠Z |

### Material Package / Track Data #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 11 | 11 | 7 | R≠Z |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### Material Package / Track Data #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### Material Package / Track Data #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 11 | 11 | 7 | R≠Z |

### File Source Package
Klasse: `SourcePackage`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| PackageUID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| PackageCreationDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| PackageModifiedDate | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| Tracks | -> Track, Track, Track, Track, Track, Track, Track, Track, Track, Track, Track | -> Track, Track, Track, Track, Track, Track, Track, Track, Track, Track, Track | -> Track, Track, Track, Track, Track, Track, Track | R≠Z |
| Descriptor | -> MultipleDescriptor | -> MultipleDescriptor | -> MultipleDescriptor |  |

### File Source Package / Track Timecode #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 1 | 1 | 1 |  |
| TrackNumber | 0 | 0 | 0 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Timecode #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> TimecodeComponent | -> TimecodeComponent | -> TimecodeComponent |  |

### File Source Package / Track Timecode #1 / TimecodeComponent
Klasse: `TimecodeComponent`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 | 060e2b34.04010101.01030201.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| RoundedTimecodeBase | 50 | 50 | 50 |  |
| StartTimecode | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| DropFrame | false | false | false |  |

### File Source Package / Track Picture #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 2 | 2 | 2 |  |
| TrackNumber | 352388352 | 352388352 | 352388352 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Picture #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Picture #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 | 060e2b34.04010101.01030202.01000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Track Sound #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 3 | 3 | 3 |  |
| TrackNumber | 369623808 | 369623808 | 369361664 | R≠Z |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Sound #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Sound #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Track Sound #2
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 4 | 4 | 4 |  |
| TrackNumber | 369623809 | 369623809 | 369361665 | R≠Z |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Sound #2 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Sound #2 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Track Sound #3
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 5 | 5 | 5 |  |
| TrackNumber | 369623810 | 369623810 | 369361666 | R≠Z |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Sound #3 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Sound #3 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Track Sound #4
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 6 | 6 | 6 |  |
| TrackNumber | 369623811 | 369623811 | 369361667 | R≠Z |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Sound #4 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Sound #4 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Track Sound #5
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 7 | 7 | – | R≠Z |
| TrackNumber | 369623812 | 369623812 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### File Source Package / Track Sound #5 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### File Source Package / Track Sound #5 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 0 | 0 | – | R≠Z |

### File Source Package / Track Sound #6
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 8 | 8 | – | R≠Z |
| TrackNumber | 369623813 | 369623813 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### File Source Package / Track Sound #6 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### File Source Package / Track Sound #6 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 0 | 0 | – | R≠Z |

### File Source Package / Track Sound #7
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 9 | 9 | – | R≠Z |
| TrackNumber | 369623814 | 369623814 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### File Source Package / Track Sound #7 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### File Source Package / Track Sound #7 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 0 | 0 | – | R≠Z |

### File Source Package / Track Sound #8
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 10 | 10 | – | R≠Z |
| TrackNumber | 369623815 | 369623815 | – | R≠Z |
| EditRate | 50/1 | 50/1 | – | R≠Z |
| Origin | 0 | 0 | – | R≠Z |
| Sequence | -> Sequence | -> Sequence | – | R≠Z |

### File Source Package / Track Sound #8 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| StructuralComponents | -> SourceClip | -> SourceClip | – | R≠Z |

### File Source Package / Track Sound #8 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.02000000 | 060e2b34.04010101.01030202.02000000 | – | R≠Z |
| StartPosition | 0 | 0 | – | R≠Z |
| Duration | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | – | R≠Z |
| SourceTrackID | 0 | 0 | – | R≠Z |

### File Source Package / Track Data #1
Klasse: `Track`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | 11 | 11 | 7 | R≠Z |
| TrackNumber | 385942017 | 385942017 | 385942017 |  |
| EditRate | 50/1 | 50/1 | 50/1 |  |
| Origin | 0 | 0 | 0 |  |
| Sequence | -> Sequence | -> Sequence | -> Sequence |  |

### File Source Package / Track Data #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| StructuralComponents | -> SourceClip | -> SourceClip | -> SourceClip |  |

### File Source Package / Track Data #1 / SourceClip
Klasse: `SourceClip`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 | 060e2b34.04010101.01030202.03000000 |  |
| StartPosition | 0 | 0 | 0 |  |
| Duration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourcePackageID | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| SourceTrackID | 0 | 0 | 0 |  |

### File Source Package / Deskriptor MultipleDescriptor
Klasse: `MultipleDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| SampleRate | 50/1 | 50/1 | 50/1 |  |
| EssenceContainer | 060e2b34.04010103.0d010301.027f0100 | 060e2b34.04010103.0d010301.027f0100 | 060e2b34.04010103.0d010301.027f0100 |  |
| SubDescriptorUIDs | -> MPEGVideoDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, ANCDataDescriptor | **-> CDCIEssenceDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, ANCDataDescriptor** | -> CDCIEssenceDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, AES3AudioDescriptor, ANCDataDescriptor | Q≠Z R≠Z |
| SubDescriptors | – | – | -> ContainerConstraintsSubDescriptor | R≠Z |

### File Source Package / Deskriptor Picture #1
**Set-Klasse weicht ab:** Quelle `MPEGVideoDescriptor`, Ziel `CDCIEssenceDescriptor`.

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 2 | 2 | 2 |  |
| SampleRate | 50/1 | 50/1 | 50/1 |  |
| ContainerDuration | (dateispezifisch) | (dateispezifisch) | (dateispezifisch) |  |
| EssenceContainer | 060e2b34.0401010a.0d010301.02106001 | 060e2b34.0401010a.0d010301.02106001 | 060e2b34.0401010a.0d010301.02106001 |  |
| SignalStandard | 4 | 4 | 4 |  |
| FrameLayout | 0 | 0 | 0 |  |
| StoredWidth | 1920 | 1920 | 1920 |  |
| StoredHeight | 1088 | 1088 | 1088 |  |
| SampledWidth | 1920 | 1920 | 1920 |  |
| SampledHeight | 1080 | 1080 | 1080 |  |
| SampledXOffset | 0 | 0 | 0 |  |
| SampledYOffset | 0 | 0 | 0 |  |
| DisplayHeight | 1080 | 1080 | 1080 |  |
| DisplayWidth | 1920 | 1920 | 1920 |  |
| DisplayXOffset | 0 | 0 | 0 |  |
| DisplayYOffset | 0 | 0 | 0 |  |
| AspectRatio | 16/9 | 16/9 | 16/9 |  |
| VideoLineMap | 00000002000000040000002a00000000 | 00000002000000040000002a00000000 | 00000002000000040000002a00000000 |  |
| CaptureGamma | 060e2b34.04010101.04010101.01020000 | 060e2b34.04010101.04010101.01020000 | 060e2b34.04010101.04010101.01020000 |  |
| ImageAlignmentOffset | 0 | 0 | 0 |  |
| ImageStartOffset | 0 | 0 | 0 |  |
| ImageEndOffset | 0 | 0 | 0 |  |
| PictureEssenceCoding | 060e2b34.0401010a.04010202.01323104 | **060e2b34.0401010d.04010202.01316001** | 060e2b34.0401010d.04010202.01316001 | Q≠Z |
| ComponentDepth | 10 | 10 | 10 |  |
| HorizontalSubsampling | 2 | 2 | 2 |  |
| VerticalSubsampling | 1 | 1 | 1 |  |
| ColorSiting | 0 | 0 | 0 |  |
| ReversedByteOrder | false | false | false |  |
| PaddingBits | 0 | 0 | 0 |  |
| BlackRefLevel | 64 | 64 | 64 |  |
| WhiteReflevel | 940 | 940 | 940 |  |
| ColorRange | 897 | 897 | 897 |  |
| SubDescriptors | – | **-> AVCSubDescriptor** | -> AVCSubDescriptor, Set(060e2b34025301010e060c0201020241) | Q≠Z R≠Z |
| ColorPrimaries | – | **060e2b34.04010106.04010101.03030000** | 060e2b34.04010106.04010101.03030000 | Q≠Z |
| CodingEquations | – | **060e2b34.04010101.04010101.02020000** | 060e2b34.04010101.04010101.02020000 | Q≠Z |

### File Source Package / Deskriptor Sound #1
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 3 | 3 | 3 |  |
| SampleRate | 48000/1 | 48000/1 | 48000/1 |  |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 |  |
| AudioSamplingRate | 48000/1 | 48000/1 | 48000/1 |  |
| Locked | true | true | true |  |
| AudioRefLevel | 0 | 0 | 0 |  |
| ChannelCount | 1 | 1 | 1 |  |
| QuantizationBits | 24 | 24 | 24 |  |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 |  |
| BlockAlign | 3 | 3 | 3 |  |
| AvgBps | 144000 | 144000 | 144000 |  |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | 000000010000000101 |  |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 |  |

### File Source Package / Deskriptor Sound #2
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 4 | 4 | 4 |  |
| SampleRate | 48000/1 | 48000/1 | 48000/1 |  |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 |  |
| AudioSamplingRate | 48000/1 | 48000/1 | 48000/1 |  |
| Locked | true | true | true |  |
| AudioRefLevel | 0 | 0 | 0 |  |
| ChannelCount | 1 | 1 | 1 |  |
| QuantizationBits | 24 | 24 | 24 |  |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 |  |
| BlockAlign | 3 | 3 | 3 |  |
| AvgBps | 144000 | 144000 | 144000 |  |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | 000000010000000101 |  |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 |  |

### File Source Package / Deskriptor Sound #3
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 5 | 5 | 5 |  |
| SampleRate | 48000/1 | 48000/1 | 48000/1 |  |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 |  |
| AudioSamplingRate | 48000/1 | 48000/1 | 48000/1 |  |
| Locked | true | true | true |  |
| AudioRefLevel | 0 | 0 | 0 |  |
| ChannelCount | 1 | 1 | 1 |  |
| QuantizationBits | 24 | 24 | 24 |  |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 |  |
| BlockAlign | 3 | 3 | 3 |  |
| AvgBps | 144000 | 144000 | 144000 |  |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | 000000010000000101 |  |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 |  |

### File Source Package / Deskriptor Sound #4
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 6 | 6 | 6 |  |
| SampleRate | 48000/1 | 48000/1 | 48000/1 |  |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 |  |
| AudioSamplingRate | 48000/1 | 48000/1 | 48000/1 |  |
| Locked | true | true | true |  |
| AudioRefLevel | 0 | 0 | 0 |  |
| ChannelCount | 1 | 1 | 1 |  |
| QuantizationBits | 24 | 24 | 24 |  |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 |  |
| BlockAlign | 3 | 3 | 3 |  |
| AvgBps | 144000 | 144000 | 144000 |  |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | 000000010000000101 |  |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 |  |

### File Source Package / Deskriptor Sound #5
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 7 | 7 | – | R≠Z |
| SampleRate | 48000/1 | 48000/1 | – | R≠Z |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | – | R≠Z |
| AudioSamplingRate | 48000/1 | 48000/1 | – | R≠Z |
| Locked | true | true | – | R≠Z |
| AudioRefLevel | 0 | 0 | – | R≠Z |
| ChannelCount | 1 | 1 | – | R≠Z |
| QuantizationBits | 24 | 24 | – | R≠Z |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | – | R≠Z |
| BlockAlign | 3 | 3 | – | R≠Z |
| AvgBps | 144000 | 144000 | – | R≠Z |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | – | R≠Z |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | – | R≠Z |

### File Source Package / Deskriptor Sound #6
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 8 | 8 | – | R≠Z |
| SampleRate | 48000/1 | 48000/1 | – | R≠Z |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | – | R≠Z |
| AudioSamplingRate | 48000/1 | 48000/1 | – | R≠Z |
| Locked | true | true | – | R≠Z |
| AudioRefLevel | 0 | 0 | – | R≠Z |
| ChannelCount | 1 | 1 | – | R≠Z |
| QuantizationBits | 24 | 24 | – | R≠Z |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | – | R≠Z |
| BlockAlign | 3 | 3 | – | R≠Z |
| AvgBps | 144000 | 144000 | – | R≠Z |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | – | R≠Z |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | – | R≠Z |

### File Source Package / Deskriptor Sound #7
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 9 | 9 | – | R≠Z |
| SampleRate | 48000/1 | 48000/1 | – | R≠Z |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | – | R≠Z |
| AudioSamplingRate | 48000/1 | 48000/1 | – | R≠Z |
| Locked | true | true | – | R≠Z |
| AudioRefLevel | 0 | 0 | – | R≠Z |
| ChannelCount | 1 | 1 | – | R≠Z |
| QuantizationBits | 24 | 24 | – | R≠Z |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | – | R≠Z |
| BlockAlign | 3 | 3 | – | R≠Z |
| AvgBps | 144000 | 144000 | – | R≠Z |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | – | R≠Z |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | – | R≠Z |

### File Source Package / Deskriptor Sound #8
Klasse: `AES3AudioDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 10 | 10 | – | R≠Z |
| SampleRate | 48000/1 | 48000/1 | – | R≠Z |
| EssenceContainer | 060e2b34.04010101.0d010301.02060300 | 060e2b34.04010101.0d010301.02060300 | – | R≠Z |
| AudioSamplingRate | 48000/1 | 48000/1 | – | R≠Z |
| Locked | true | true | – | R≠Z |
| AudioRefLevel | 0 | 0 | – | R≠Z |
| ChannelCount | 1 | 1 | – | R≠Z |
| QuantizationBits | 24 | 24 | – | R≠Z |
| SoundEssenceCompression | 060e2b34.0401010a.04020201.01000000 | 060e2b34.0401010a.04020201.01000000 | – | R≠Z |
| BlockAlign | 3 | 3 | – | R≠Z |
| AvgBps | 144000 | 144000 | – | R≠Z |
| ChannelStatusMode | 000000010000000101 | 000000010000000101 | – | R≠Z |
| FixedChannelStatusData | 0000000100000018850000000000000000000000000000000000000000000000 | 0000000100000018850000000000000000000000000000000000000000000000 | – | R≠Z |

### File Source Package / Deskriptor Data #1
Klasse: `ANCDataDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| LinkedTrackID | 11 | 11 | 7 | R≠Z |
| SampleRate | 50/1 | 50/1 | 50/1 |  |
| EssenceContainer | 060e2b34.04010109.0d010301.020e0000 | 060e2b34.04010109.0d010301.020e0000 | 060e2b34.04010109.0d010301.020e0000 |  |

### File Source Package / Deskriptor Picture #1 / AVCSubDescriptor
**Set nur im Ziel** (in der Quelle nicht vorhanden).
Klasse: `AVCSubDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| AVCProfile | – | **122** | 122 | Q≠Z |
| AVCProfileConstraint | – | **0** | 0 | Q≠Z |
| AVCLevel | – | **42** | 42 | Q≠Z |
| AVCDecodingDelay | – | **1** | 1 | Q≠Z |
| AVCConstantBPictureFlag | – | **false** | false | Q≠Z |
| AVCCodedContentKind | – | **1** | 1 | Q≠Z |
| AVCClosedGOPIndicator | – | **true** | false | Q≠Z R≠Z |
| AVCIdenticalGOPIndicator | – | **true** | false | Q≠Z R≠Z |
| AVCMaximumGOPSize | – | **24** | 24 | Q≠Z |
| AVCMaximumBPictureCount | – | **2** | 2 | Q≠Z |
| AVCMaximumBitrate | – | **50000000** | 49999872 | Q≠Z R≠Z |
| AVCMaximumRefFrames | – | **3** | 2 | Q≠Z R≠Z |
| AVCSequenceParameterSetFlag | – | **176** | 48 | Q≠Z R≠Z |
| AVCPictureParameterSetFlag | – | **176** | 32 | Q≠Z R≠Z |

### Material Package / Track DM (StaticTrack) #1
Klasse: `StaticTrack`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TrackID | – | – | 8 | R≠Z |
| TrackNumber | – | – | 0 | R≠Z |
| Sequence | – | – | -> Sequence | R≠Z |

### Material Package / Track DM (StaticTrack) #1 / Sequence
Klasse: `Sequence`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | – | – | 060e2b34.04010101.01030201.10000000 | R≠Z |
| StructuralComponents | – | – | -> DMSegment | R≠Z |

### Material Package / Track DM (StaticTrack) #1 / DMSegment
Klasse: `DMSegment`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| DataDefinition | – | – | 060e2b34.04010101.01030201.10000000 | R≠Z |
| DMFramework | – | – | -> TextBasedDMFramework | R≠Z |

### Material Package / Track DM (StaticTrack) #1 / DMSegment / TextBasedDMFramework
Klasse: `TextBasedDMFramework`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TextBasedObject | – | – | -> UTF8TextBasedSet | R≠Z |

### Material Package / Track DM (StaticTrack) #1 / DMSegment / TextBasedDMFramework / UTF8TextBasedSet
Klasse: `UTF8TextBasedSet`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| TextBasedMetadataPayloadSchemaID | – | – | 8a390800.4678031c.786d4d22.828011de | R≠Z |
| TextMIMEMediaType | – | – | 'text/xml' | R≠Z |
| RFC5646TextLanguageCode | – | – | 'und' | R≠Z |
| UTF8TextData | – | – | (dateispezifisch) | R≠Z |

### File Source Package / Deskriptor Picture #1 / Set(060e2b34025301010e060c0201020241)
Klasse: `Set(060e2b34025301010e060c0201020241)`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|
| UL 060e2b34010101000e06042002030301 | – | – | (Rohdaten, 1 Bytes) | R≠Z |
| UL 060e2b34010101000e06042002030311 | – | – | (Rohdaten, 7862 Bytes) | R≠Z |

### Sonstige: ContainerConstraintsSubDescriptor #1
Klasse: `ContainerConstraintsSubDescriptor`

| Item | Quelle | Ziel | Referenz | Abw. |
|---|---|---|---|---|

## Anhang: MXFDump-Zusammenfassung

Aus der Ausgabe von `MXFDump` (nur Zaehlungen, Partition-Felder, Auffaelligkeiten; IDs und Pfade entfernt).  Gegenprobe zum eigenen Parser: Satzanzahl je Typ.

### Quelle

Exit-Code: 0

| MXFDump-Typ | MXFDump | eigener Parser |
|---|---|---|
| Essence Element | 1080 | |
| Footer | 1 | |
| Header | 1 | |
| IndexTableSegment | 1 | |
| KLVFill | 1193 | |
| MXFAES3PCMDescriptor | 8 | 8 |
| MXFContentStorage | 1 | 1 |
| MXFEssenceContainerData | 1 | 1 |
| MXFIdentification | 1 | 1 |
| MXFMPEG2VideoDescriptor | 1 | 1 |
| MXFMaterialPackage | 1 | 1 |
| MXFMultipleDescriptor | 1 | 1 |
| MXFPreface | 1 | 1 |
| MXFSequence | 22 | 22 |
| MXFSourceClip | 20 | 20 |
| MXFSourcePackage | 1 | 1 |
| MXFTimecodeComponent | 2 | 2 |
| MXFTrack | 22 | 22 |
| Primer | 1 | |
| RandomIndexMetadata | 1 | |
| SystemMetadata | 108 | – |

Partition Packs laut MXFDump: Header (KAG 512, Header-Metadaten ja, BodySID 2, IndexSID 1); Footer (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 0)

Keine Auffaelligkeiten (keine Zeilen mit error/warning/invalid/unexpected/mismatch).

### Ziel

Exit-Code: 0

| MXFDump-Typ | MXFDump | eigener Parser |
|---|---|---|
| Body | 2 | |
| Essence Element | 1080 | |
| Footer | 1 | |
| Header | 1 | |
| IndexTableSegment | 1 | |
| KLVFill | 1086 | |
| MXFAES3PCMDescriptor | 8 | 8 |
| MXFCDCIEssenceDescriptor | 1 | 1 |
| MXFContentStorage | 1 | 1 |
| MXFEssenceContainerData | 1 | 1 |
| MXFIdentification | 1 | 1 |
| MXFMaterialPackage | 1 | 1 |
| MXFMultipleDescriptor | 1 | 1 |
| MXFPreface | 1 | 1 |
| MXFSequence | 22 | 22 |
| MXFSourceClip | 20 | 20 |
| MXFSourcePackage | 1 | 1 |
| MXFTimecodeComponent | 2 | 2 |
| MXFTrack | 22 | 22 |
| Primer | 1 | |
| RandomIndexMetadata | 1 | |

Partition Packs laut MXFDump: Header (KAG 512, Header-Metadaten ja, BodySID 0, IndexSID 0); Body (KAG 512, Header-Metadaten nein, BodySID 2, IndexSID 0); Body (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 1); Footer (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 0)

Keine Auffaelligkeiten (keine Zeilen mit error/warning/invalid/unexpected/mismatch).

### Referenz

Exit-Code: 0

| MXFDump-Typ | MXFDump | eigener Parser |
|---|---|---|
| Essence Element | 1932 | |
| Footer | 1 | |
| Header | 1 | |
| IndexTableSegment | 1 | |
| KLVFill | 2259 | |
| MXFAES3PCMDescriptor | 4 | 4 |
| MXFCDCIEssenceDescriptor | 1 | 1 |
| MXFContentStorage | 1 | 1 |
| MXFDMSegment | 1 | 1 |
| MXFEssenceContainerData | 1 | 1 |
| MXFIdentification | 1 | 1 |
| MXFMaterialPackage | 1 | 1 |
| MXFMultipleDescriptor | 1 | 1 |
| MXFPreface | 1 | 1 |
| MXFSequence | 15 | 15 |
| MXFSourceClip | 12 | 12 |
| MXFSourcePackage | 1 | 1 |
| MXFStaticTrack | 1 | 1 |
| MXFTimecodeComponent | 2 | 2 |
| MXFTrack | 14 | 14 |
| Primer | 1 | |
| RandomIndexMetadata | 1 | |
| SystemMetadata | 322 | – |

Partition Packs laut MXFDump: Header (KAG 512, Header-Metadaten ja, BodySID 2, IndexSID 1); Footer (KAG 512, Header-Metadaten nein, BodySID 0, IndexSID 0)

Keine Auffaelligkeiten (keine Zeilen mit error/warning/invalid/unexpected/mismatch).

