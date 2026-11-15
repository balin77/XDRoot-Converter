# Mitwirken

Danke für das Interesse! Fehlerberichte, Testergebnisse und Pull Requests sind willkommen.

## Fehler melden

Bitte ein Issue auf GitHub öffnen und angeben:

- Kamera und Firmware-Version, Aufnahmeformat (Codec, Auflösung, Bildrate)
- Betriebssystem, Python-Version, verwendete Versionen von FFmpeg und bmx
- Ausgabe von `mxf2raw -i <clip>.MXF` und `ffprobe -show_streams -show_format <clip>.MXF` für einen betroffenen Clip
- `MEDIAPRO.XML` und die `M01.XML` des Clips
- `konvertierung.log` aus dem Zielordner
- Was erwartet wurde und was passiert ist

**Wichtig:** Vor dem Hochladen Seriennummern (z. B. `serialNo="..."` in den XML-Dateien) und persönliche
Angaben (Benutzernamen, Ordner- und Dateipfade, z. B. `file_uri`-Zeilen) entfernen oder durch Platzhalter ersetzen.

## Tests ausführen

```
python -m unittest discover -s tests
```

Die Tests prüfen nur Logik anhand der Fixtures in `tests/fixtures` und brauchen weder Medien noch FFmpeg/bmx.
Echte Umwandlungen lassen sich mit `python -m xdroot_converter.cli --verify <ziel> --source <quelle>` prüfen.

## Pull Requests

Pull Requests sind willkommen. Bitte ein Thema pro PR, Tests ergänzen bzw. grün halten und bei Änderungen
an Transcode, Wrapping oder XML-Anpassung die Ausgabe gegen die Referenzen in `docs/analysis` vergleichen
(siehe `docs/SPEC.md`). Nur die Python-Standardbibliothek verwenden; die Oberfläche bleibt deutsch.

## Besonders gesucht

- Tests mit anderen Kameras und anderen Bildraten (25p, 29.97p, 59.94p ...)
- Rückmeldung, ob **Avid, Premiere, DaVinci Resolve, Sony Catalyst** (oder andere Software/Geräte) die
  umgewandelten Karten annehmen und abspielen, mit Angabe der Version
- Erfahrungen mit Interlaced- und 4K-Material (wird bisher nur kopiert)
- Tests unter Linux/macOS
