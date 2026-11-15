#!/usr/bin/env python3
"""Vergleicht die MXF-Header-Metadaten von Quelle, Ziel und (optional) Referenz "Satz fuer Satz".

Aufruf:
    python scripts/mxf_vergleich.py QUELLE.MXF ZIEL.MXF [--referenz FX6.MXF] [-o bericht.md]
                                    [--mxfdump-timeout 600] [--rohwerte] [--dump-dir VERZEICHNIS]

Ausgabe: Markdown-Bericht (Standard: Standardausgabe).  Je Set-Typ (Preface, Identification,
ContentStorage, Packages, Tracks je Art, Sequence, SourceClip, TimecodeComponent, Deskriptoren,
Textobjekt ...) stehen die Items nebeneinander (Quelle | Ziel | Referenz), Abweichungen sind
markiert.  Grundlage ist der eigene Parser (xdroot_converter.mxfheader); zusaetzlich wird, wenn
MXFDump gefunden wird, eine Zusammenfassung seiner Ausgabe als Anhang aufgenommen (Satzzaehlung,
Partition Packs, Auffaelligkeiten) und mit dem eigenen Parser gegengeprueft.

Datenschutz: Der Bericht soll in ein oeffentliches Repository passen.  Dateispezifische Werte
(InstanceUIDs, UMIDs, Datums-/Zeitwerte, Dauern, Generation-UIDs, Paketnamen, Textdaten ...) werden
als "(dateispezifisch)" ausgegeben; Dateinamen und Pfade kommen nicht vor (nur "Quelle", "Ziel",
"Referenz"); Items ohne Eintrag im Datenmodell erscheinen nur mit UL und Laenge.  Mit --rohwerte
werden auch deren Rohbytes ausgegeben (dann NICHT veroeffentlichen).  Die Dateien werden nur im
Modus 'rb' gelesen.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from xdroot_converter import mxfheader as mh  # noqa: E402

try:
    from xdroot_converter import tools as _tools  # noqa: E402
except Exception:  # pragma: no cover
    _tools = None

VOLATILE = "(dateispezifisch)"
KIND_DE = {"timecode": "Timecode", "picture": "Picture", "sound": "Sound", "data": "Data",
           "dm": "DM (StaticTrack)", "other": "Sonstige"}
VOLATILE_NAMES = {
    "InterchangeObject.InstanceUID", "InterchangeObject.GenerationUID", "Identification.ThisGenerationUID",
    "GenericPackage.PackageUID", "GenericPackage.Name", "EssenceContainerData.LinkedPackageUID",
    "SourceClip.SourcePackageID", "StructuralComponent.Duration", "FileDescriptor.ContainerDuration",
    "TimecodeComponent.StartTimecode", "Preface.LastModifiedDate", "Identification.ModificationDate",
    "GenericPackage.PackageCreationDate", "GenericPackage.PackageModifiedDate",
    "UTF8TextBasedSet.UTF8TextData", "UTF16TextBasedSet.UTF16TextData", "DMSegment.EventComment",
    "GenericPackage.UserComments", "TaggedValue.Name", "TaggedValue.Value",
}
VOLATILE_TYPES = {"UMID", "PACKAGEID", "UUID", "TIMESTAMP"}
VOLATILE_TYPE_EXCEPTIONS = {"Identification.ProductUID"}      # Produktkennung, keine Dateikennung
REF_TYPES = {"STRONGREF", "STRONGREFARRAY", "STRONGREFBATCH", "WEAKREF", "WEAKREFARRAY", "WEAKREFBATCH"}


# --------------------------------------------------------------------------- Aufbereitung
def short_name(qname: str) -> str:
    return qname.split(".", 1)[1] if "." in qname else qname


def children(hm, s):
    out = []
    for tag, val in s.items:
        if hm.item_type(tag) in ("STRONGREF", "STRONGREFARRAY", "STRONGREFBATCH"):
            out += ([hm.by_uid().get(val)] if hm.item_type(tag) == "STRONGREF" else
                    [hm.by_uid().get(u) for u in mh.uid_list(val)])
    return [c for c in out if c is not None]


def entries(hm):
    """Geordnete (Bezeichnung, Set)-Liste: Struktur aus Preface/Packages/Tracks/Deskriptoren."""
    st = mh.resolve(hm)
    out, seen = [], set()

    def add(label, s):
        if s is None or id(s) in seen:
            return
        seen.add(id(s))
        out.append((label, s))

    add("Preface", st.preface)
    for i, s in enumerate(st.identifications, 1):
        add("Identification #%d" % i, s)
    cs = hm.ref(st.preface, "Preface.ContentStorage") if st.preface else None
    add("ContentStorage", cs)
    if cs:
        for i, s in enumerate(hm.refs(cs, "ContentStorage.EssenceContainerData"), 1):
            add("EssenceContainerData #%d" % i, s)
    for pkg in st.packages:
        pl = "Material Package" if pkg.role == "material" else "File Source Package"
        if pkg is not st.material and pkg is not st.file:
            pl += " (weiteres)"
        add(pl, pkg.set)
        count: dict = {}
        for t in pkg.tracks:
            count[t.kind] = count.get(t.kind, 0) + 1
            tl = "%s / Track %s #%d" % (pl, KIND_DE[t.kind], count[t.kind])
            add(tl, t.set)
            if t.sequence is not None:
                add(tl + " / Sequence", t.sequence)
                comps = (hm.refs(t.sequence, "Sequence.StructuralComponents")
                         if hm.name(t.sequence) == "Sequence" else [t.sequence])
                for j, c in enumerate(comps, 1):
                    cl = "%s / %s%s" % (tl, hm.name(c), " #%d" % j if len(comps) > 1 else "")
                    add(cl, c)
                    stack = [(cl, c)]
                    while stack:                                  # DM-Kette (DMSegment -> Framework -> Textobjekt)
                        lab, cur = stack.pop()
                        for ch in children(hm, cur):
                            cl2 = "%s / %s" % (lab, hm.name(ch))
                            add(cl2, ch)
                            stack.append((cl2, ch))
        if pkg.descriptor is not None:
            dl = pl + " / Deskriptor"
            add("%s %s" % (dl, hm.name(pkg.descriptor)), pkg.descriptor)
            dcount: dict = {}
            kinds = {t.track_id: t.kind for t in pkg.tracks}
            descs = (hm.refs(pkg.descriptor, "MultipleDescriptor.SubDescriptorUIDs")
                     if hm.name(pkg.descriptor) == "MultipleDescriptor" else [])
            for d in descs:
                kind = kinds.get(hm.u32(d, "FileDescriptor.LinkedTrackID"), "other")
                dcount[kind] = dcount.get(kind, 0) + 1
                dlab = "%s %s #%d" % (dl, KIND_DE[kind], dcount[kind])      # Klasse steht im Satzkopf
                add(dlab, d)
                for k, sub in enumerate(hm.refs(d, "GenericDescriptor.SubDescriptors"), 1):
                    add("%s / %s" % (dlab, hm.name(sub)), sub)
    rest: dict = {}
    for s in hm.sets:
        if id(s) not in seen:
            n = hm.name(s)
            rest[n] = rest.get(n, 0) + 1
            add("Sonstige: %s #%d" % (n, rest[n]), s)
    return out


class Cell:
    __slots__ = ("text", "key")

    def __init__(self, text, key):
        self.text, self.key = text, key


def make_cell(hm, tag, val, raw: bool) -> Cell:
    """Anzeigetext (anonymisiert) und Vergleichsschluessel (None = dateispezifisch, nicht vergleichen)."""
    ul = hm.ul_of_tag(tag)
    info = mh._ITEM_INFO.get(ul) if ul else None
    if info is None:
        if raw:
            return Cell("`%s`" % val.hex(), val)
        return Cell("(Rohdaten, %d Bytes)" % len(val), val)
    qname, typ = info
    if qname in VOLATILE_NAMES or (typ in VOLATILE_TYPES and qname not in VOLATILE_TYPE_EXCEPTIONS):
        return Cell(VOLATILE, None)
    text = mh.format_value(hm, typ, val)
    if typ in REF_TYPES:
        return Cell(text, text)          # aufgeloeste Set-Namen, nicht die UIDs
    if typ in ("RAW", "DATAVALUE", "OPAQUE", "INDIRECT", "STREAM") and len(val) > 32:
        return Cell("(%d Bytes)" % len(val), val)
    return Cell(text, val)


def item_name(hm, tag) -> str:
    n = hm.item_name(tag)
    return short_name(n) if "." in n else n


def md(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def section_rows(hms, sets, raw):
    """Zeilen (Item, [Zelle|None je Datei]) fuer ein Label; Items nach erstem Auftreten geordnet."""
    order, cells = [], []
    for hm, s in zip(hms, sets):
        d = {}
        if s is not None:
            for tag, val in s.items:
                n = item_name(hm, tag)
                if n == "InstanceUID":
                    continue
                if n not in d:
                    d[n] = make_cell(hm, tag, val, raw)
                if n not in order:
                    order.append(n)
        cells.append(d)
    return [(n, [c.get(n) for c in cells]) for n in order]


def differs(a: Cell | None, b: Cell | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    if a.key is None or b.key is None:
        return False
    return a.key != b.key


# --------------------------------------------------------------------------- MXFDump
_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2}[.\s-]){7,}[0-9a-fA-F]{2}|\b[0-9a-fA-F]{16,}\b")
_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/][\w .\\/-]*\.\w+")


def sanitize(line: str) -> str:
    return _PATH_RE.sub("<pfad>", _HEX_RE.sub("<id>", line)).strip()


def mxfdump_summary(path: Path, timeout: float, dump_to: Path | None):
    """(Exitcode | None, Satzzaehlung, Partition-Packs, Auffaelligkeiten, Fehlertext) oder Fehlertext."""
    try:
        exe = _tools.find_tool("MXFDump") if _tools else None
    except Exception as e:  # ToolNotFound
        return None, {}, [], [], str(e).splitlines()[0]
    if not exe:
        return None, {}, [], [], "MXFDump nicht gefunden"
    counts: dict = {}
    parts: list = []
    flags: list = []
    cur = None
    t0 = time.time()
    out = open(dump_to, "w", encoding="utf-8", errors="replace") if dump_to else None
    try:
        proc = subprocess.Popen([exe, str(path)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, errors="replace")
        n = 0
        for line in proc.stdout:
            n += 1
            if out:
                out.write(line)
            if n % 20000 == 0 and time.time() - t0 > timeout:
                proc.kill()
                return None, counts, parts, flags, "MXFDump-Zeitlimit (%ds) ueberschritten" % timeout
            m = re.match(r"\[ K = (.+?) \(", line)
            if m:
                name = m.group(1).strip()
                counts[name] = counts.get(name, 0) + 1
                if name in ("Header", "Body", "Footer"):
                    cur = {"Art": name}
                    parts.append(cur)
                else:
                    cur = None
                continue
            if cur is not None:
                m = re.match(r"\s*(KAGSize|HeaderByteCount|IndexByteCount|IndexSID|BodySID)\s*=\s*([0-9a-fA-F]+)", line)
                if m:
                    v = int(m.group(2), 16)
                    cur[m.group(1)] = (v > 0) if m.group(1).endswith("ByteCount") else v
            low = line.lower()
            if any(w in low for w in ("error", "warning", "invalid", "unexpected", "incorrect", "mismatch")):
                s = sanitize(line)
                if s and s not in flags and len(flags) < 25:
                    flags.append(s)
        proc.wait()
        return proc.returncode, counts, parts, flags, ""
    finally:
        if out:
            out.close()


# --------------------------------------------------------------------------- Bericht
def partition_table(path: Path) -> list:
    rows = []
    for i, p in enumerate(mh.find_partitions(path)):
        rows.append("| %d | %s | %d | %d | %s | %d | %d |" % (
            i, p.kind, p.status, p.kag_size, "ja" if p.header_byte_count else "nein", p.body_sid, p.index_sid))
    return rows


def build_report(paths: dict, raw: bool, timeout: float, dump_dir: Path | None) -> str:
    names = list(paths)                       # Quelle, Ziel, [Referenz]
    hms = [mh.read_header(paths[n]) for n in names]
    ents = [dict(entries(h)) for h in hms]
    order = []
    for e in ents:
        for lab in e:
            if lab not in order:
                order.append(lab)
    # Reihenfolge: nach Ziel/Quelle gemischt, wie sie zuerst auftreten (Quelle zuerst)
    iq, iz = 0, 1
    lines = []
    diffs = []                                 # (Label, Item, Q, Z)
    body = []
    for lab in order:
        sets = [e.get(lab) for e in ents]
        rows = section_rows(hms, sets, raw)
        head = "| Item | " + " | ".join(names) + " | Abw. |"
        sep = "|---|" + "---|" * len(names) + "---|"
        body.append("### %s" % lab)
        if sets[iq] is not None and sets[iz] is None:
            body.append("**Set fehlt im Ziel** (nur in der Quelle vorhanden).")
        if sets[iq] is None and sets[iz] is not None:
            body.append("**Set nur im Ziel** (in der Quelle nicht vorhanden).")
        if sets[iq] is not None and sets[iz] is not None and hms[iq].name(sets[iq]) != hms[iz].name(sets[iz]):
            body.append("**Set-Klasse weicht ab:** Quelle `%s`, Ziel `%s`." % (
                hms[iq].name(sets[iq]), hms[iz].name(sets[iz])))
            diffs.append((lab, "(Set-Klasse)", hms[iq].name(sets[iq]), hms[iz].name(sets[iz])))
        else:
            body.append("Klasse: `%s`" % next((hms[i].name(s) for i, s in enumerate(sets) if s is not None), "?"))
        body += ["", head, sep]
        for n, cs in rows:
            flag = []
            if differs(cs[iq], cs[iz]):
                flag.append("Q≠Z")
                diffs.append((lab, n, cs[iq].text if cs[iq] else "(fehlt)", cs[iz].text if cs[iz] else "(fehlt)"))
            if len(cs) > 2 and differs(cs[2], cs[iz]):
                flag.append("R≠Z")
            cells = []
            for i, c in enumerate(cs):
                t = md(c.text) if c else "–"
                if i == iz and "Q≠Z" in flag:
                    t = "**%s**" % t
                cells.append(t)
            body.append("| %s | %s | %s |" % (md(n), " | ".join(cells), " ".join(flag)))
        body.append("")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["# MXF-Kopfdaten-Vergleich", "",
              "Erzeugt mit `scripts/mxf_vergleich.py` am %s.  Spalten: %s.  "
              "\"Abw.\": `Q≠Z` = Ziel weicht von der Quelle ab, `R≠Z` = Ziel weicht von der Referenz ab.  "
              "`(dateispezifisch)` = Wert ist je Datei verschieden (UIDs, UMIDs, Zeiten, Dauern, Namen, Daten) "
              "und wird weder angezeigt noch verglichen." % (now, ", ".join(names)), ""]
    if raw:
        lines += ["**ACHTUNG:** mit --rohwerte erzeugt; unbekannte Items enthalten Rohbytes - nicht veroeffentlichen.", ""]
    lines += ["## Zusammenfassung", ""]
    if diffs:
        lines += ["Abweichungen Quelle ≠ Ziel: **%d** (in %d Saetzen).  Sie sind unten je Satz markiert." % (
            len(diffs), len({d[0] for d in diffs})), "",
                  "| Satz | Item | Quelle | Ziel |", "|---|---|---|---|"]
        for lab, n, q, z in diffs:
            lines.append("| %s | %s | %s | %s |" % (md(lab), md(n), md(q), md(z)))
    else:
        lines.append("Keine Abweichungen Quelle ≠ Ziel in den verglichenen (nicht dateispezifischen) Items.")
    lines += ["", "## Partitionen", ""]
    for n in names:
        lines += ["**%s**" % n, "", "| # | Art | Status | KAG | Header-Metadaten | BodySID | IndexSID |",
                  "|---|---|---|---|---|---|---|"] + partition_table(paths[n]) + [""]
    lines += ["## Satz fuer Satz", ""] + body
    # MXFDump-Anhang
    lines += ["## Anhang: MXFDump-Zusammenfassung", "",
              "Aus der Ausgabe von `MXFDump` (nur Zaehlungen, Partition-Felder, Auffaelligkeiten; "
              "IDs und Pfade entfernt).  Gegenprobe zum eigenen Parser: Satzanzahl je Typ.", ""]
    for i, n in enumerate(names):
        dump_to = (dump_dir / ("mxfdump_%s.txt" % n.lower())) if dump_dir else None
        rc, counts, parts, flags, err = mxfdump_summary(paths[n], timeout, dump_to)
        lines += ["### %s" % n, ""]
        if err:
            lines += ["MXFDump nicht auswertbar: %s" % md(err), ""]
            if not counts:
                continue
        lines.append("Exit-Code: %s" % ("?" if rc is None else rc))
        lines += ["", "| MXFDump-Typ | MXFDump | eigener Parser |", "|---|---|---|"]
        own: dict = {}
        for s in hms[i].sets:
            own[hms[i].name(s)] = own.get(hms[i].name(s), 0) + 1
        for k in sorted(counts):
            if k in ("Essence Element", "Header", "Body", "Footer", "KLVFill", "Primer", "RandomIndexMetadata",
                     "IndexTableSegment"):
                lines.append("| %s | %d | |" % (k, counts[k]))
                continue
            base = k[3:] if k.startswith("MXF") else k
            alt = {"AES3PCMDescriptor": "AES3AudioDescriptor", "MPEG2VideoDescriptor": "MPEGVideoDescriptor"}.get(base, base)
            ov = own.get(alt, own.get(base))
            lines.append("| %s | %d | %s |" % (k, counts[k], "–" if ov is None else ov))
        lines.append("")
        if parts:
            lines += ["Partition Packs laut MXFDump: " + "; ".join(
                "%s (KAG %s, Header-Metadaten %s, BodySID %s, IndexSID %s)" % (
                    p["Art"], p.get("KAGSize", "?"), "ja" if p.get("HeaderByteCount") else "nein",
                    p.get("BodySID", "?"), p.get("IndexSID", "?")) for p in parts), ""]
        if flags:
            lines += ["Auffaelligkeiten (Zeilen mit error/warning/invalid/...):", ""] + ["- `%s`" % f for f in flags] + [""]
        else:
            lines += ["Keine Auffaelligkeiten (keine Zeilen mit error/warning/invalid/unexpected/mismatch).", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="MXF-Header-Metadaten Satz fuer Satz vergleichen (Markdown-Bericht)")
    ap.add_argument("quelle", type=Path)
    ap.add_argument("ziel", type=Path)
    ap.add_argument("--referenz", type=Path, help="echte Referenz-MXF (z. B. Sony FX6, XAVC-L)")
    ap.add_argument("-o", "--output", type=Path, help="Berichtsdatei (Standard: Standardausgabe)")
    ap.add_argument("--mxfdump-timeout", type=float, default=600, help="Zeitlimit je MXFDump-Lauf in s (Standard 600)")
    ap.add_argument("--rohwerte", action="store_true", help="Rohbytes unbekannter Items ausgeben (nicht veroeffentlichen)")
    ap.add_argument("--dump-dir", type=Path, help="rohe MXFDump-Ausgaben hier ablegen (enthalten IDs, nicht veroeffentlichen)")
    a = ap.parse_args()
    paths = {"Quelle": a.quelle, "Ziel": a.ziel}
    if a.referenz:
        paths["Referenz"] = a.referenz
    for n, p in paths.items():
        if not p.is_file():
            print("%s nicht gefunden." % n, file=sys.stderr)
            return 2
    if a.dump_dir:
        a.dump_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = build_report(paths, a.rohwerte, a.mxfdump_timeout, a.dump_dir)
    except mh.MxfParseError as e:
        print("MXF nicht lesbar: %s" % e, file=sys.stderr)
        return 1
    if a.output:
        a.output.write_text(report, encoding="utf-8")
        print("Bericht geschrieben: %s" % a.output)
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
