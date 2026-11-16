# Stand und nächste Schritte

Stand 16.11.2026: Der Stand nach Runde 2 und den Fix-Runden wurde **lokal unter Windows mit echtem Kameramaterial** getestet
(Sony PMW-F5 und eine F55-Karte als Quelle, Sony FX6 als XAVC-L-Referenz). Dabei kam ein weiterer Fix an den Kopfdaten dazu
(Deskriptoren wie echte Sony-Dateien). Die umgewandelte F55-Karte wurde vom Autor in **DaVinci Resolve** und **Sony Catalyst
Browse** importiert: Karte erkannt, Wiedergabe und Tonspuren in Ordnung; die Timecode-Anzeige wurde dort nicht verglichen.
Die Temperaturüberwachung (Runde 3) ist neu und nur teilweise getestet. Neu in Runde 4: die **Abschlussprüfung** nach jeder
Umwandlung mit automatischer Neu-Umwandlung fehlerhafter Clips (siehe unten).

## Was lokal mit echtem Material geprüft wurde

Rechner: Windows 11, Python 3.11/3.14, FFmpeg 9.0.2 (essentials), bmx 1.6. Quell-Laufwerke wurden nur gelesen; ein Vergleich
aller Quellordner (Namen, Größen, Änderungszeiten) vor und nach den Tests zeigte keinen Unterschied.

| Test | Ergebnis |
|---|---|
| `setup_tools.bat`, Unit-Tests unter Windows | Programme gefunden, alle Tests bestanden |
| `--mxf-info` auf je einen echten F5-, F55- und FX6-Clip | fehlerfrei gelesen; Befunde unten |
| F5-Karte, 19 in `MEDIAPRO.XML` gelistete Clips (18,33 GB, 11:01 min Material) | 19 umgewandelt, 0 Fehler, 4,62 GB, 11:47 min; `--verify --source --deep`: 19 ohne Abweichung |
| F55-Karte, alle 76 Clips (104,18 GB, 62:39 min Material) | 76 umgewandelt, 26,42 GB; `--verify --source --deep`: 76 ohne Abweichung, 155 Begleitdateien verglichen |
| Beide Karten erneut mit der **verschärften** Prüfung (Runde 4: Decode mit `-xerror`, Ton je Spur vollständig) | F5: 19 von 19 ohne Abweichung (2:11 min); F55: 76 von 76 ohne Abweichung, 155 Begleitdateien verglichen (gegen eine Kopie der Originalkarte auf Festplatte, die Karte selbst war inzwischen neu bespielt) |
| Erster Lauf des Autors in der GUI (16.11.2026): weitere F55-Karte, 6 Clips, 67,90 GB, darunter **ein Clip mit 52,68 GB** (95 082 Bilder, 31:42 min, WAV-Dateien über 4 GB) | 6 umgewandelt, 17,17 GB, 0 Fehler, 1:39 h; Abschlussprüfung (gründlich, 5:07 min): 6 von 6 ohne Abweichung. Der Rechner ging während des langen Clips in den Energiesparmodus und lief nach dem Aufwachen weiter; das Ergebnis war trotzdem fehlerfrei. Daraus entstand „Energiesparmodus verhindern“ |
| Abschlussprüfung mit gezielt beschädigtem Ziel-Clip (Bilddaten bzw. Ton überschrieben) | Schaden gemeldet, Clip in Wiederholungsrunde 1 neu umgewandelt, „Ergebnis: Fertig – geprüft, ohne Abweichung“; beschädigter, nicht ausgewählter Clip: Exit-Code 1, „Fertig mit Fehlern“ |
| `scripts/mxf_vergleich.py` Quelle / Ziel / FX6-Referenz | Quelle ≠ Ziel: 20 Abweichungen, alle durch den Codec-Wechsel; Bericht: `docs/analysis/vergleich_f55_ziel_fx6.md` |
| Strg+C in der Kommandozeile | Exit-Code 130 nach 1,4 s, Arbeitsordner und Sperre entfernt, keine verwaisten Prozesse |
| Fortsetzen nach Strg+C | Clip neu erzeugt, `--verify --source` für den Clip ohne Abweichung |
| Hartes Beenden (Prozessbaum) und Wiederaufnahme | verwaiste Sperre übernommen, Arbeitsordner aufgeräumt, Clip fertig |
| Zwei Läufe in dasselbe Ziel | zweiter Lauf bricht nach 2 s mit klarer Meldung ab (Exit-Code 1), erster läuft durch |
| Absturz des Rechners mitten im Lauf (ungeplant, Überhitzung) | Fortsetzen: 17 fertige Clips erkannt und übersprungen, Rest umgewandelt |
| Kopfdaten nachziehen (alte Ausgabe, neuer Patcher) | 17 F55- und 19 F5-Clips ohne neues Transkodieren angeglichen (1–3 s je Clip) |
| `XDROOT-Converter.bat` | startet, findet Python über `py -3`, Fenster erscheint ohne Konsole |
| Import der umgewandelten F55-Karte in DaVinci Resolve und Sony Catalyst Browse (von Hand) | Karte erkannt, Wiedergabe und Tonspuren in Ordnung; Timecode-Anzeige nicht verglichen |

Bei der F55-Karte schlug **ein Clip im ersten Durchgang fehl**: `ffmpeg` stand 28 Minuten und lieferte danach 1582 statt 2218
Bilder („Invalid data“). Das Tool meldete den Clip als Fehler und legte nichts Unvollständiges ins Ziel. Die Quelldatei ließ sich
danach vollständig und fehlerfrei lesen, der Fortsetzungslauf wandelte den Clip in 44 s um. Ursache war vermutlich ein
vorübergehender Aussetzer beim Lesen vom Kartenleser; belegt ist das nicht.

### Was echte Sony-Dateien enthalten (bisher unbekannt)

- **KAG 512** bei F5, F55 und FX6. Zwei Partitionen (Header mit Essenz und Index, Footer).
- **Track-IDs:** Timecode 1, Video 2, Ton ab 3 fortlaufend, ANC danach (F5/F55 mit 8 Tonspuren: 3–10 und 11; FX6 mit 4: 3–6
  und 7). **Keine Track-Namen.**
- **Sound-Deskriptor (AES3):** `SampleRate` 48000/1 (nicht die Bildrate), **keine `ContainerDuration`**, `AvgBps` 144000,
  `SoundEssenceCompression`, `ChannelStatusMode`, `FixedChannelStatusData`; kein `SequenceOffset` bei 50p.
- **Video-Deskriptor:** F5/F55 als `MPEGVideoDescriptor`, FX6 als `CDCIEssenceDescriptor` mit `AVCSubDescriptor` und einem
  Sony-eigenen Satz. Beide mit `StoredHeight` 1088, `SampledHeight` 1080, `VideoLineMap` [42, 0], Schwarz-/Weißpegel 64/940.
- Der Parser las alle drei Dateitypen ohne Fehler; der Toninhalts-Vergleich funktioniert mit echtem Sony-AES3.

### Fix aus dem lokalen Test

Der Vergleich zeigte Items, bei denen Quelle und FX6-Referenz übereinstimmen und nur das Ziel abwich. Der Patcher übernimmt sie
jetzt aus der Quelle: Ton-`SampleRate` und das Fehlen der `ContainerDuration` (Ton, ANC, MultipleDescriptor), die
codec-unabhängigen Items des Video-Deskriptors (u. a. `SampledHeight`, `VideoLineMap`, Bild-Offsets, Schwarz-/Weißpegel,
`ColorRange`) und die Reihenfolge von `Preface.EssenceContainers`. Abweichungen Quelle ≠ Ziel: vorher 47, jetzt 20. Zwei
Opus-Reviews, keine groben Befunde.

## Verbleibende Abweichungen zur FX6-Referenz

Alle im Bericht `docs/analysis/vergleich_f55_ziel_fx6.md` (Spalte `R≠Z`):

| Abweichung | Grund | Bewertung |
|---|---|---|
| Spurzahl, Ton-Track-Nummern, ANC-Track-ID (11 statt 7) | Ziel hat 8 Tonspuren wie die F55-Quelle, die FX6 nur 4 | gewollt, folgt der Quelle |
| Eingebettetes XML (`DMSchemes`, `StaticTrack`, TextObject) | FX6 bettet `M01.XML` ein, F5/F55 nicht | gewollt (`--embed-xml auto`); mit `--embed-xml always` entsteht es |
| Partitionslayout: Header, Essenz-Body, Index-Body, Footer statt Header + Footer | so schreibt `raw2bmx` | stört DaVinci Resolve und Catalyst Browse nicht (Import geprüft); für andere Werkzeuge offen, möglicher Hebel `raw2bmx --min-part` |
| `AVCSubDescriptor`: `AVCClosedGOPIndicator` true (FX6 false), `AVCIdenticalGOPIndicator` true (false), `AVCMaximumRefFrames` 3 (2), `AVCMaximumBitrate` 50000000 (49999872), SPS-/PPS-Flags 176/176 (48/32) | beschreibt den tatsächlich von x264 erzeugten Datenstrom | bewusst belassen (die Angaben müssen zum Datenstrom passen); ein Angleichen hieße, die x264-Einstellungen zu ändern |
| Sony-eigener Unter-Deskriptor am Video-Deskriptor (7862 Byte) und leerer `ContainerConstraintsSubDescriptor` | nur in der FX6, Inhalt undokumentiert | nicht nachbildbar |
| `ThisGenerationUID`, Zeitstempel, Fill-Größen | dateispezifisch | bewusst belassen |

## Was der Autor noch tun muss

1. **Timecode und Metadatenanzeige** in Resolve bzw. Catalyst Browse mit dem Original und einem FX6-Clip vergleichen (Import,
   Wiedergabe und Tonspuren sind geprüft). Weitere Programme (Avid, Premiere) bei Bedarf.
2. **Oberfläche von Hand:** Lauf mit Abbrechen und Fortsetzen, Abschlussfenster (jetzt mit „Geprüft: N Clips, M ohne
   Abweichung“), Rückfragen beim Start, Schließen während eines Laufs, die neuen Einstellungen zur Abschlussprüfung
   (Checkliste in der Rückmeldung des lokalen Tests).
3. **Temperaturüberwachung:** Der Gerätetest ist bestanden (siehe unten). Offen: einen ganzen Lauf mit Überwachung beobachten
   (Anzeige, Schalten der Lüfter bei 85 °C).
4. Ein sehr langer Clip ist inzwischen getestet (siehe Tabelle oben: 52,68 GB, 31:42 min). Offen sind noch längere Clips und
   Karten mit mehreren sehr langen Clips.

## Temperaturüberwachung und Schonmodus (Runde 3)

Spezifikation: [SPEC.md](SPEC.md), Abschnitt „Temperaturüberwachung und Schonmodus“. Getestet sind die Logik (Unit-Tests mit
Attrappen), das Anhalten und Fortsetzen von Prozessen unter Windows (mit Hilfsprozessen) und der Weg ohne Administratorrechte
(„Temperatur nicht verfügbar“). Der Schonmodus (`--threads N`) funktioniert ohne Messung auf jedem Rechner.

**Gerätetest 16.11.2026 (Lenovo Legion Pro 7 16IRX8H, Administratorrechte, `--thermal-test`):**

| Quelle | Ergebnis |
|---|---|
| `LENOVO_GAMEZONE_DATA.GetCPUTemp` | antwortet, liefert aber 0 (unbrauchbar) |
| `LENOVO_OTHER_METHOD.GetFeatureValue(0x05040000)` | 80, 86, 79, 76, 75 °C in Folge: plausibel und schwankend, wird als Temperaturquelle gewählt |
| `MSAcpi_ThermalZoneTemperature` | „Nicht unterstützt“ |
| Leistungszähler Thermalzone | keine Zone gemeldet |
| `GetFan1Speed` / `GetFan2Speed` / `GetFanMaxSpeed` | Fehler „Ungültiges Objekt“ |
| `GetFeatureValue(0x04030001 / 0x04030002)` | 3500 / 3600 U/min |
| `SetFeatureValue(0x04020000, 1)` („Lüfter voll“) | wirkt: 7200 / 7300 U/min; zurückgestellt und zurückgelesen (0). Die Drehzahl direkt nach dem Zurückstellen war noch 7100 (sofort gemessen) |
| `GetSmartFanMode` | 255 (nur gelesen, wird nie geschaltet) |

Der erste Test desselben Tages scheiterte an `GetCPUTemp` = 0; daraus entstand die Quellenkette (SPEC, „Temperaturquellen“).
**Noch nicht beobachtet:** ein ganzer Umwandlungslauf mit laufender Überwachung, das automatische Schalten bei 85 °C im Lauf,
die Pause bei echter Überhitzung, andere Legion-Modelle.

**Erster ganzer Lauf mit Überwachung (16.11.2026) und Runde 5:** Messung, „Lüfter voll“ (5900 → 7100 U/min), Pause und
Fortsetzen arbeiteten im echten Lauf. Die CPU erreichte unter Last aber in Sekunden 97–98 °C, so dass der Lauf zwischen Pause und
Volllast pendelte und die Lüfter hoch- und runterschalteten. Daraus entstand Runde 5 (SPEC, „Lüfter halten und Last drosseln
statt pendeln“): Lüfter bleiben bis zum Laufende voll, die Last wird in Stufen über die Prozess-Affinität gedrosselt, Pause nur
noch als letzte Stufe. Vier Opus-Reviews, zuletzt ohne grobe Fehler. **Am Gerät noch nicht belegt:** ob das Drosseln die
Temperatur dieser CPU wirklich senkt (in der Simulation mit angenommenen Werten pendelt sich der Regler auf 24 von 32 Kernen
ein), und ob der H.264-Datenstrom trotz Drosselung derselbe bleibt wie ohne (neue Prozesse laufen deshalb die ersten Sekunden
ungedrosselt; ein Vergleich mit echtem Material steht aus). Offene Notizen dazu: Der Wechsel zwischen Pause und niedrigster Stufe
hat keinen Endzustand, wenn Drosseln nicht hilft (der Lauf kommt aber voran); ein theoretischer Ausnahmepfad beim Aufheben der
Drosselung könnte die alte Stufe stehen lassen; in `_set_fan` steht noch ein kosmetischer `if True:`-Block.

Offene Notizen aus dem letzten Review der Temperaturüberwachung und des Energiesparschutzes (keine groben Fehler; der Zustand
ist in allen Fällen sicher, d. h. `ffmpeg` läuft weiter und die Lüfter werden zurückgestellt):

- Drehzahl- und Temperaturquelle teilen sich das Kontingent von drei Neustarts des Hilfsprozesses. Nach mehreren Hängern der
  Drehzahlquelle kann deshalb die ganze Überwachung für den Rest des Laufs ausfallen.
- Ein einzelner Fehler der Art „Quelle meldet nichts“ (z. B. leere Thermalzonen-Antwort) führt sofort zum Ausfall der
  Überwachung; nur Fehlermeldungen des Hilfsprozesses werden dreimal toleriert.
- Die Statuszeile nennt die Lenovo-Quelle `GetFeatureValue` weiterhin „Quelle unbestätigt“, obwohl sie auf dem getesteten Modell
  bestätigt ist; zwei Pausen-Protokollzeilen nennen die Quelle nicht.
- Einzelne Meldungen des Anbieters können verloren gehen, wenn die letzte Messung scheitert.
- Unter Linux/macOS schreibt jeder Lauf eine Warnzeile, dass der Energiesparmodus nicht verhindert werden kann.
- Nicht belegt: dass Windows bei einem stundenlangen Lauf wirklich nicht einschläft (nur der Rückgabewert des Aufrufs ist getestet).

## Abschlussprüfung mit automatischer Wiederholung (Runde 4)

Spezifikation: [SPEC.md](SPEC.md), Abschnitt „Abschlussprüfung mit automatischer Wiederholung“. Nach der Umwandlung wird die
Zielkarte wie mit `--verify --source --deep` geprüft; Clips mit Abweichung werden neu umgewandelt (Standard 2 Runden), der alte
Clip wird erst ersetzt, wenn die neue Datei die gründliche Prüfung bestanden hat.

Das erste Review fand zwei grobe Lücken der **Prüfung selbst**, die auch `--verify --deep` betrafen: Ein beschädigtes Bild
führte bei `ffmpeg` nur zu einer Warnung (jetzt `-xerror`, Rückgabewert ≠ 0 ist ein Befund), und Ton wurde nur in Ausschnitten
verglichen (jetzt mit `--deep` und `--source` vollständig je Spur per Prüfsumme). Beide Testkarten wurden danach erneut geprüft
(Tabelle oben). Das zweite Review fand keine groben Fehler mehr. Belegt ist der vollständige Tonvergleich mit F5/F55-Material
(HD 50p, 8 Mono-Spuren).

Offene Notizen aus dem zweiten Review (keine groben Fehler, jeweils Fehlalarm in die sichere Richtung oder Abweichung von der
Spezifikation):

- Ein Clip **ganz ohne Tonspur** führt in der gründlichen Prüfung zu „Tonspuren-Inhalt nicht pruefbar (Quelle)“ und damit zu
  „Fertig mit Fehlern“, obwohl nichts fehlt (nur synthetisch nachgestellt; Sony-Clips haben Ton).
- Mit der schnellen Abschlussprüfung (`--auto-verify-quick`) wird ein neu umgewandelter Clip ohne komplettes Dekodieren
  ersetzt; die Spezifikation verlangt die gründliche Prüfung vor jedem Ersetzen.
- Der vollständige Tonvergleich ordnet die Spuren nach der Reihenfolge im Material Package zu. Mit `--no-sony-header` und einer
  Quelle mit ungeordnetem Tracks-Array wären Fehlalarme möglich (nur aus dem Code gelesen, bei den Testkarten nicht der Fall).
- Kopfdaten-Befunde, die beim Lesen der Quelle entstehen, lösen eine (nutzlose) Wiederholung aus.
- Nicht getestet: die Oberfläche von Hand, die Temperaturpause während der Prüfung, Strg+C im Terminal während der Prüfung
  (nur über das Abbruch-Objekt), eine wirklich unlesbare Quelle.
- Der Test `test_thermal.TokenPauseTests.test_paused_time_accumulates` ist zeitabhängig und schlägt unter Windows gelegentlich
  knapp fehl (0,187 statt ≥ 0,19 s).

## Bekannte offene Punkte und Annahmen

- Import nur in DaVinci Resolve und Sony Catalyst Browse geprüft; andere Schnittprogramme und Sony-Werkzeuge ungeprüft.
- Getestet wurde nur HD 50p. Andere Bildraten (25p, 29.97p, 59.94p) nur synthetisch; 4K, Interlaced und Ton ≠ Mono/24 Bit/48 kHz
  werden nur kopiert.
- `ffmpeg` kann beim Lesen von einem Kartenleser hängen bleiben (siehe oben); das Tool erkennt das Ergebnis als Fehler, hat aber
  keine eigene Erkennung für einen Stillstand.
- Nach einem Absturz des Rechners war `konvertierung.log` im Ziel leer (0 Byte); das Protokoll des abgebrochenen Laufs ging
  verloren, die Clips nicht.
- Mit `--only` enthält `MEDIAPRO.XML` im Ziel weiter alle Clips der Quelle, auch die nicht übernommenen; `--verify --source`
  meldet sie als fehlend. `--only` ist damit ein Testwerkzeug, keine Art, eine Teil-Karte zu liefern.
- Die Quell-Metadaten der getesteten „F55“-Karte nennen als Gerät `PMW-F5`; ob sich echte F55-Dateien in Kopfdaten-Details
  unterscheiden, ist damit nicht belegt.
- Fortsetzen übernimmt abweichende Einstellungen nicht (nur Warnung).
- GUI-Klicks sind nicht automatisiert getestet.

### Kleine Notizen aus den Reviews (nicht behoben, keine groben Fehler)

- Speicherschätzung: Ein nicht analysierbarer Quell-Clip, dessen umgewandelter Ziel-Clip schon existiert, wird mitgezählt, obwohl
  nichts geschrieben wird. Bei einer sehr großen defekten Quelle kann ein Fortsetzungslauf deshalb unnötig mit „Zu wenig freier
  Speicher“ abbrechen (kein Datenverlust).
- `--verify --source`: Für einen nicht lesbaren, mit der Quelle byte-gleichen Ziel-Clip entfällt die Prüfung, ob `M01.XML` und
  `MEDIAPRO.XML` ihn fälschlich als XAVC-L führen. Der Byte-Vergleich liest beide Dateien komplett und ist nicht abbrechbar.
- Hat sich eine nicht lesbare Quelldatei zwischen zwei Läufen geändert, meldet die Zusammenfassung die ältere eigene Kopie im Ziel
  als „fremde Datei“; der Clip bleibt Fehler, bis die Zieldatei gelöscht wird.
- Ein vorübergehender Lesefehler der Einstellungsdatei führt ebenfalls zum Umbenennen nach `.defekt`; eine vorhandene
  `.defekt`-Datei wird überschrieben.
- Die Übersicht meldet für nicht umwandelbare Clips immer „wird unverändert kopiert“, auch wenn im Ziel schon ein umgewandelter
  Clip liegt (dann ist es tatsächlich ein Fehler mit erhaltenem Ziel).
- Meldungen aus dem Ablauf (Protokoll, Teile der Abschlussfenster) sind in ASCII-Umschrift („ae/oe/ue“), die übrige Oberfläche
  hat echte Umlaute.
- Die übernommenen Bild-Offsets (`ImageStartOffset` u. Ä.) beschreiben streng genommen die Quell-Essenz; bei Sony sind sie 0.

## Woran man ein Problem erkennt, welche Dateien helfen

- Hinweise: Zusammenfassung am Ende des Laufs (Zahl „Fehler“, Fehlergrund je Clip), Abschlussfenster der GUI, `--verify` mit
  „ABWEICHUNG“-Zeilen, eine Warnung zu „bmx-Kennungen“ oder zur Ton-Zuordnung.
- Hilfreich sind: `konvertierung.log` aus dem Zielordner (enthält alle Kopfdaten-Änderungen), `konvertierung_einstellungen.json`,
  die Ausgabe von `--mxf-info <Clip>` (Quelle und Ziel) und die `--verify`-Ausgabe.
- **Vor dem Weitergeben** Seriennummern (`serialNo="..."`), UMIDs und private Pfade (`file_uri`, Benutzernamen) entfernen oder
  durch Platzhalter ersetzen.
