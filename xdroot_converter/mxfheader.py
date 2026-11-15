"""MXF-Header-Metadaten lesen, aendern und an Ort und Stelle zurueckschreiben (reines Python).

Zweck (SPEC "Runde 2"): Eine mit raw2bmx erzeugte XAVC-L-MXF soll in den Kopfdaten wie die
Sony-Quelldatei aussehen (Identification, Track-IDs, Track-Namen, Sound-Deskriptoren,
MIME-Typ des eingebetteten XML).  Dafuer wird die Header-Metadaten-Region der Datei
(Primer + Local Sets + abschliessender KLV-Fill) geparst, geaendert und so zurueckgeschrieben,
dass ``HeaderByteCount`` und damit alle Offsets der Datei exakt gleich bleiben: der letzte
KLV-Fill wird entsprechend gekuerzt oder verlaengert.

Oeffentliche API:
    read_header(path) -> HeaderMetadata
    adopt_from_source(target, source) -> list[str]
    compare_with_source(target, source) -> list[str]
    describe(path) -> str
    HeaderPatchError

Low-Level (z. B. fuer scripts/make_test_card.py): find_partitions, patch_header,
parse_header_metadata, HeaderMetadata/MxfSet, resolve, set_track_ids, relink_after_renumber.

Bei KAG > 1 steht zwischen Partition Pack und Primer ein KLV-Fill; ``HeaderByteCount`` zaehlt ab dem
Primer-Key (``Partition.pre_fill`` / ``meta_offset``), der Fill bleibt byte-genau unveraendert.
Ohne RIP und mit FooterPartition = 0 (offener Header) wird nur gelesen, nie geschrieben.

Es werden nur kleine Bereiche der Datei gelesen (Partition Packs, RIP, Header-Metadaten);
die Quelldatei wird ausschliesslich im Modus 'rb' geoeffnet.  Nur Standardbibliothek.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Union

__all__ = [
    "HeaderPatchError", "MxfParseError", "Partition", "MxfSet", "HeaderMetadata",
    "find_partitions", "parse_header_metadata", "read_header", "patch_header",
    "resolve", "adopt_from_source", "compare_with_source", "describe",
    "adopt_headers", "compare_headers", "set_track_ids", "relink_after_renumber",
    "utf16", "item_ul", "set_name", "verify_references", "format_value", "describe_metadata",
]


class MxfParseError(ValueError):
    """Datei bzw. Metadaten sind kein gueltiges MXF (soweit hier geprueft)."""


class HeaderPatchError(Exception):
    """Header-Metadaten konnten nicht angepasst werden; die Datei bleibt unveraendert."""


# --------------------------------------------------------------------------- Namenstabellen
_SET_NAMES: dict[bytes, str] = {}       # normierter Set-Key -> Name
_SET_KEYS: dict[str, bytes] = {}        # Name -> normierter Key
_ITEM_INFO: dict[bytes, tuple[str, str]] = {}   # normierte UL -> (Set.Item, Typ)
_ITEM_UL: dict[str, bytes] = {}         # "Set.Item" -> normierte UL
_STATIC_TAG_OF_UL: dict[bytes, int] = {}
_STATIC_UL_OF_TAG: dict[int, bytes] = {}
_ITEM_FULL: dict[bytes, bytes] = {}      # normierte UL -> UL mit Versionsbyte des Datenmodells


def _norm(ul: bytes) -> bytes:
    """Registry-Versionsbyte (Index 7) ausblenden: Sony/bmx schreiben unterschiedliche Versionen."""
    return ul[:7] + b"\x00" + ul[8:]


def _load_names() -> None:
    for line in _NAMES_TXT.splitlines():
        p = line.split()
        if not p:
            continue
        if p[0] == "S":
            key = _norm(bytes.fromhex(p[1]))
            _SET_NAMES.setdefault(key, p[2])
            _SET_KEYS.setdefault(p[2], key)
        elif p[0] == "I":
            ul = _norm(bytes.fromhex(p[1]))
            _ITEM_FULL.setdefault(ul, bytes.fromhex(p[1]))
            _ITEM_INFO.setdefault(ul, (p[3], p[4]))
            _ITEM_UL.setdefault(p[3], ul)
            if p[2] != "-":
                tag = int(p[2], 16)
                _STATIC_TAG_OF_UL.setdefault(ul, tag)
                _STATIC_UL_OF_TAG.setdefault(tag, ul)


def item_ul(name: str) -> bytes:
    """Normierte UL eines Items nach Namen ('Identification.CompanyName')."""
    return _ITEM_UL[name]


def set_name(key: bytes) -> str:
    return _SET_NAMES.get(_norm(key), "Set(" + key.hex() + ")")


def utf16(text: str) -> bytes:
    """MXF-UTF16-String (Big Endian, mit abschliessendem NUL wie bei bmx/Sony)."""
    return text.encode("utf-16-be") + b"\x00\x00"


def _dec16(value: bytes) -> str:
    try:
        return value.decode("utf-16-be").rstrip("\x00")
    except UnicodeDecodeError:
        return value.hex()


# --------------------------------------------------------------------------- KLV / BER
_FILL_PREFIX = bytes.fromhex("060e2b34010101")          # + Version + 03010210 01000000
_FILL_TAIL = bytes.fromhex("03010210" "01000000")
FILL_KEY_LEGACY = bytes.fromhex("060e2b34010101010301021001000000")
_PRIMER_KEY_N = _norm(bytes.fromhex("060e2b34020501010d01020101050100"))
_PARTITION_PREFIX = bytes.fromhex("060e2b34020501010d01020101")      # + Art + Status + 00
_RIP_KEY_N = _norm(bytes.fromhex("060e2b34020501010d01020101110100"))


def _is_fill(key: bytes) -> bool:
    return key[:7] == _FILL_PREFIX and key[8:] == _FILL_TAIL


def read_ber(buf: bytes, pos: int) -> tuple[int, int]:
    """(Laenge, Anzahl BER-Bytes)."""
    if pos >= len(buf):
        raise MxfParseError("BER-Laenge hinter dem Puffer")
    b = buf[pos]
    if b < 0x80:
        return b, 1
    n = b & 0x7F
    if n == 0 or n > 8 or pos + 1 + n > len(buf):
        raise MxfParseError("ungueltige BER-Laenge")
    return int.from_bytes(buf[pos + 1:pos + 1 + n], "big"), 1 + n


def enc_ber(length: int, size: int = 0) -> bytes:
    """BER-Laenge kodieren; size = gewuenschte Gesamtzahl Bytes (0 = minimal)."""
    if length < 0x80 and size <= 1:
        return bytes([length])
    need = max(1, (length.bit_length() + 7) // 8)
    n = max(size - 1, need)
    if n > 8:
        raise HeaderPatchError("BER-Laenge zu gross")
    return bytes([0x80 | n]) + length.to_bytes(n, "big")


# --------------------------------------------------------------------------- Partitionen
@dataclass
class Partition:
    offset: int
    key: bytes
    kind: str                   # 'header' | 'body' | 'footer'
    status: int                 # 1 offen/unvollstaendig .. 4 geschlossen/vollstaendig
    ber_size: int
    value_len: int
    major: int = 1
    minor: int = 3
    kag_size: int = 1
    this_partition: int = 0
    previous_partition: int = 0
    footer_partition: int = 0
    header_byte_count: int = 0
    index_byte_count: int = 0
    index_sid: int = 0
    body_offset: int = 0
    body_sid: int = 0
    operational_pattern: bytes = b""
    essence_containers: list = field(default_factory=list)
    pre_fill: int = 0           # Bytes KLV-Fill zwischen Partition Pack und Primer (KAG > 1)

    @property
    def pack_end(self) -> int:
        return self.offset + 16 + self.ber_size + self.value_len

    @property
    def meta_offset(self) -> int:
        """Beginn der Header-Metadaten = Primer Pack.  HeaderByteCount zaehlt ab dem Primer-Key;
        zwischen Partition Pack und Primer kann ein KLV-Fill bis zur KAG-Grenze stehen
        (libMXF ueberspringt ihn mit mxf_read_next_nonfiller_kl)."""
        return self.pack_end + self.pre_fill

    @property
    def complete(self) -> bool:
        return self.status in (3, 4)


def _is_partition_key(key: bytes) -> bool:
    return key[:13] == _PARTITION_PREFIX and key[13] in (2, 3, 4) and key[14] in (1, 2, 3, 4) and key[15] == 0


def _skip_fill(f, pos: int) -> int:
    """Gesamtgroesse der KLV-Fill-Elemente ab ``pos`` (0, wenn dort gleich ein anderer Key steht)."""
    total = 0
    for _ in range(64):
        f.seek(pos + total)
        head = f.read(16 + 9)
        if len(head) < 17 or not _is_fill(head[:16]):
            break
        try:
            ln, nb = read_ber(head, 16)
        except MxfParseError:
            break
        total += 16 + nb + ln
    return total


def _read_partition(f, offset: int) -> Partition:
    f.seek(offset)
    head = f.read(16 + 9)
    if len(head) < 17:
        raise MxfParseError("Partition Pack ausserhalb der Datei (Offset %d)" % offset)
    key = head[:16]
    if not _is_partition_key(key):
        raise MxfParseError("kein Partition Pack an Offset %d" % offset)
    ln, nb = read_ber(head, 16)
    if ln < 88 or ln > 1 << 20:
        raise MxfParseError("unplausible Laenge des Partition Packs")
    f.seek(offset + 16 + nb)
    v = f.read(ln)
    if len(v) != ln:
        raise MxfParseError("Partition Pack abgeschnitten")
    major, minor, kag = struct.unpack(">HHI", v[:8])
    this, prev, footer, hbc, ibc = struct.unpack(">QQQQQ", v[8:48])
    index_sid, body_offset, body_sid = struct.unpack(">IQI", v[48:64])
    op = v[64:80]
    cnt, isz = struct.unpack(">II", v[80:88])
    ecs = []
    if isz == 16 and 88 + cnt * 16 <= len(v):
        ecs = [v[88 + 16 * i:104 + 16 * i] for i in range(cnt)]
    pre_fill = _skip_fill(f, offset + 16 + nb + ln) if hbc > 0 else 0
    return Partition(
        offset=offset, key=key, kind={2: "header", 3: "body", 4: "footer"}[key[13]], status=key[14],
        ber_size=nb, value_len=ln, major=major, minor=minor, kag_size=kag, this_partition=this,
        previous_partition=prev, footer_partition=footer, header_byte_count=hbc,
        index_byte_count=ibc, index_sid=index_sid, body_offset=body_offset, body_sid=body_sid,
        operational_pattern=op, essence_containers=ecs, pre_fill=pre_fill)


def _rip_offsets(f, size: int) -> Optional[list[int]]:
    if size < 40:
        return None
    f.seek(size - 4)
    (rip_len,) = struct.unpack(">I", f.read(4))
    if rip_len < 16 + 1 + 4 or rip_len > size:
        return None
    f.seek(size - rip_len)
    raw = f.read(rip_len)
    if len(raw) != rip_len or _norm(raw[:16]) != _RIP_KEY_N:
        return None
    try:
        ln, nb = read_ber(raw, 16)
    except MxfParseError:
        return None
    body = raw[16 + nb:16 + nb + ln]
    if len(body) != ln or ln < 4 or (ln - 4) % 12 != 0 or 16 + nb + ln != rip_len:
        return None
    return [struct.unpack(">Q", body[i + 4:i + 12])[0] for i in range(0, ln - 4, 12)]


def find_partitions(path: Union[str, Path]) -> list[Partition]:
    """Alle Partitionen einer MXF-Datei (RIP; Rueckfall: Footer-Offset + PreviousPartition-Kette)."""
    with open(path, "rb") as f:
        return _find_partitions_f(f)


def _find_partitions_f(f) -> list[Partition]:
    return _find_partitions_ex(f)[0]


def _find_partitions_ex(f) -> tuple:
    """(Partitionen, vollstaendig).  ``vollstaendig`` ist False, wenn weder ein RIP vorhanden ist
    noch der Header-Pack einen FooterPartition-Offset hat (offener Header, z. B. nach
    Stromausfall): dann sind nur Partitionen gesichert, die man nicht auffinden kann."""
    f.seek(0, 2)
    size = f.tell()
    first = _read_partition(f, 0)       # Datei muss mit dem Header-Partition-Pack beginnen
    if first.kind != "header":
        raise MxfParseError("erste Partition ist keine Header-Partition")
    offs = _rip_offsets(f, size)
    parts: list[Partition] = []
    complete = True
    if offs:
        try:
            parts = [first if o == 0 else _read_partition(f, o) for o in sorted(set(offs))]
            if parts[0].offset != 0:
                parts.insert(0, first)
        except MxfParseError:
            parts = []
    if not parts:
        parts = [first]
        complete = bool(first.footer_partition)
        if first.footer_partition and first.footer_partition < size:
            chain = []
            cur = first.footer_partition
            seen = set()
            while cur and cur not in seen and len(chain) < 100000:
                seen.add(cur)
                p = _read_partition(f, cur)
                chain.append(p)
                if p.offset == 0:
                    break
                cur = p.previous_partition
            chain.reverse()
            parts = [first] + [p for p in chain if p.offset != 0]
    for p in parts:
        if p.this_partition != p.offset:
            raise MxfParseError("ThisPartition (%d) passt nicht zum Offset (%d)" % (p.this_partition, p.offset))
    return parts, complete


# --------------------------------------------------------------------------- Datenmodell
class MxfSet:
    """Ein Local Set (16-Byte-Key, Items mit 2-Byte-Tag und 2-Byte-Laenge), Reihenfolge erhalten."""

    def __init__(self, key: bytes, items: list, ber_size: int = 4):
        self.key = key
        self.items: list[tuple[int, bytes]] = items
        self.ber_size = ber_size

    def get(self, tag: int) -> Optional[bytes]:
        for t, v in self.items:
            if t == tag:
                return v
        return None

    def set(self, tag: int, value: bytes) -> None:
        for i, (t, _) in enumerate(self.items):
            if t == tag:
                self.items[i] = (tag, bytes(value))
                return
        self.items.append((tag, bytes(value)))

    def remove(self, tag: int) -> bool:
        n = len(self.items)
        self.items = [(t, v) for t, v in self.items if t != tag]
        return len(self.items) != n

    @property
    def instance_uid(self) -> Optional[bytes]:
        return self.get(0x3C0A)

    def serialize(self) -> bytes:
        body = bytearray()
        for t, v in self.items:
            if len(v) > 0xFFFF:
                raise HeaderPatchError("Item 0x%04x ist zu lang fuer ein Local Set" % t)
            body += struct.pack(">HH", t, len(v)) + v
        return self.key + enc_ber(len(body), self.ber_size) + bytes(body)


class _Fill:
    def __init__(self, key: bytes, ber_size: int, value: bytes):
        self.key, self.ber_size, self.value = key, ber_size, value

    def serialize(self) -> bytes:
        return self.key + enc_ber(len(self.value), self.ber_size) + self.value


class _Raw(_Fill):
    """Unbekanntes KLV (kein Local Set), byte-genau durchgereicht."""


class _PrimerMark:
    def __init__(self, key: bytes, ber_size: int):
        self.key, self.ber_size = key, ber_size


class HeaderMetadata:
    """Primer + Sets (+ Fill) der Header-Metadaten einer Partition."""

    def __init__(self) -> None:
        self.primer: dict[int, bytes] = {}
        self.elements: list = []
        self._rev: Optional[dict[bytes, int]] = None
        self._idx: Optional[dict[bytes, MxfSet]] = None

    # -- Grundzugriff
    @property
    def sets(self) -> list[MxfSet]:
        return [e for e in self.elements if isinstance(e, MxfSet)]

    def touch(self) -> None:
        """Nach strukturellen Aenderungen (Sets/Primer) Zwischenspeicher verwerfen."""
        self._rev = None
        self._idx = None

    def by_uid(self) -> dict[bytes, MxfSet]:
        if self._idx is None:
            self._idx = {s.instance_uid: s for s in self.sets if s.instance_uid is not None}
        return self._idx

    def name(self, s: MxfSet) -> str:
        return set_name(s.key)

    def find_sets(self, name: str) -> list[MxfSet]:
        return [s for s in self.sets if self.name(s) == name]

    def remove_set(self, s: MxfSet) -> None:
        self.elements = [e for e in self.elements if e is not s]
        self.touch()

    # -- Primer / Tags
    def _reverse(self) -> dict[bytes, int]:
        if self._rev is None:
            rev: dict[bytes, int] = {}
            for t, ul in self.primer.items():
                rev.setdefault(_norm(ul), t)
            self._rev = rev
        return self._rev

    def raw_ul_of_tag(self, tag: int) -> Optional[bytes]:
        """UL eines Tags mit Versionsbyte, wie im Primer eingetragen."""
        return self.primer.get(tag) or _ITEM_FULL.get(_STATIC_UL_OF_TAG.get(tag, b""))

    def ul_of_tag(self, tag: int) -> Optional[bytes]:
        """Normierte UL zu einem lokalen Tag (Primer, sonst bekannte statische Tags)."""
        ul = self.primer.get(tag)
        if ul is not None:
            return _norm(ul)
        return _STATIC_UL_OF_TAG.get(tag)

    def tag_for_ul(self, ul: bytes, create: bool = False, full_ul: Optional[bytes] = None) -> Optional[int]:
        """Lokaler Tag zu einer UL.  Mit create=True wird ein fehlender Primer-Eintrag angelegt
        (statischer Tag des Datenmodells, falls frei, sonst freier dynamischer Tag); full_ul ist
        die UL mit Versionsbyte, wie sie in den Primer geschrieben wird."""
        full = full_ul if full_ul is not None and len(full_ul) == 16 else (
            ul if len(ul) == 16 and ul[7] != 0 else None)
        ul = _norm(ul)
        t = self._reverse().get(ul)
        if t is not None:
            return t
        st = _STATIC_TAG_OF_UL.get(ul)
        if not create:
            return st if (st is not None and st not in self.primer) else None
        if st is not None and st not in self.primer:
            tag = st
        else:
            tag = 0xFFFF
            while tag in self.primer:
                tag -= 1
                if tag < 0x8000:
                    raise HeaderPatchError("kein freier dynamischer Tag im Primer")
        self.primer[tag] = full or _ITEM_FULL.get(ul) or (ul[:7] + b"\x01" + ul[8:])
        self.touch_primer()
        return tag

    def touch_primer(self) -> None:
        self._rev = None

    def _resolve_ul(self, ref: Union[str, bytes]) -> bytes:
        return _norm(ref) if isinstance(ref, (bytes, bytearray)) else _ITEM_UL[ref]

    # -- Items per UL/Name
    def get(self, s: MxfSet, ref: Union[str, bytes]) -> Optional[bytes]:
        tag = self.tag_for_ul(self._resolve_ul(ref))
        return None if tag is None else s.get(tag)

    def put(self, s: MxfSet, ref: Union[str, bytes], value: bytes, after: Optional[list] = None) -> bool:
        """Item setzen (vorhandenes ersetzen, sonst einfuegen). True, wenn sich etwas aenderte."""
        ul = self._resolve_ul(ref)
        tag = self.tag_for_ul(ul, create=True, full_ul=bytes(ref) if isinstance(ref, (bytes, bytearray)) else None)
        cur = s.get(tag)
        if cur is not None:
            if cur == value:
                return False
            s.set(tag, value)
            return True
        pos = len(s.items)
        if after:
            wanted = {self._resolve_ul(a) for a in after}
            for i, (t, _) in enumerate(s.items):
                if self.ul_of_tag(t) in wanted:
                    pos = i + 1
        s.items.insert(pos, (tag, bytes(value)))
        return True

    def drop(self, s: MxfSet, ref: Union[str, bytes]) -> bool:
        tag = self.tag_for_ul(self._resolve_ul(ref))
        return False if tag is None else s.remove(tag)

    def u32(self, s: MxfSet, ref: Union[str, bytes]) -> Optional[int]:
        v = self.get(s, ref)
        return struct.unpack(">I", v)[0] if v is not None and len(v) == 4 else None

    def ref(self, s: MxfSet, ref: Union[str, bytes]) -> Optional[MxfSet]:
        v = self.get(s, ref)
        if v is None or len(v) != 16:
            return None
        return self.by_uid().get(v)

    def refs(self, s: MxfSet, ref: Union[str, bytes]) -> list[MxfSet]:
        v = self.get(s, ref)
        if v is None:
            return []
        out = []
        idx = self.by_uid()
        for u in uid_list(v):
            t = idx.get(u)
            if t is not None:
                out.append(t)
        return out

    def item_name(self, tag: int) -> str:
        ul = self.ul_of_tag(tag)
        if ul is None:
            return "Tag 0x%04x" % tag
        info = _ITEM_INFO.get(ul)
        return info[0] if info else "UL " + ul.hex()

    def item_type(self, tag: int) -> str:
        ul = self.ul_of_tag(tag)
        info = _ITEM_INFO.get(ul) if ul else None
        return info[1] if info else ""

    # -- Serialisierung
    def primer_bytes(self) -> bytes:
        mark = next((e for e in self.elements if isinstance(e, _PrimerMark)), None)
        body = struct.pack(">II", len(self.primer), 18) + b"".join(
            struct.pack(">H", t) + ul for t, ul in self.primer.items())
        key = mark.key if mark else bytes.fromhex("060e2b34020501010d01020101050100")
        return key + enc_ber(len(body), mark.ber_size if mark else 4) + body

    def to_bytes(self, total_len: Optional[int] = None) -> bytes:
        """Serialisieren.  Mit total_len wird der letzte KLV-Fill so angepasst, dass genau
        total_len Bytes entstehen (sonst HeaderPatchError)."""
        els = self.elements
        trailing = els[-1] if (total_len is not None and els and isinstance(els[-1], _Fill)
                               and not isinstance(els[-1], _Raw) and _is_fill(els[-1].key)) else None
        parts = []
        for e in (els[:-1] if trailing is not None else els):
            parts.append(self.primer_bytes() if isinstance(e, _PrimerMark) else e.serialize())
        body = b"".join(parts)
        if total_len is None:
            return body + (trailing.serialize() if trailing is not None else b"")
        rest = total_len - len(body)
        if rest < 0:
            raise HeaderPatchError(
                "Neue Header-Metadaten passen nicht in den vorhandenen Platz (%d Bytes zu viel)" % (-rest))
        if rest == 0:
            return body
        if trailing is not None and 16 + len(enc_ber(len(trailing.value), trailing.ber_size)) + len(trailing.value) == rest:
            return body + trailing.serialize()     # unveraendert (auch bei Nicht-Null-Fuellung)
        key = trailing.key if trailing is not None else next(
            (e.key for e in els if isinstance(e, _Fill) and _is_fill(e.key)), FILL_KEY_LEGACY)
        pref = trailing.ber_size if trailing is not None else 4
        for b in (pref, 4, 3, 5, 8, 9, 1, 2):
            vl = rest - 16 - b
            if vl < 0:
                continue
            if b == 1 and vl >= 0x80:
                continue
            if b > 1 and vl >= 1 << (8 * (b - 1)):
                continue
            return body + _Fill(key, b, bytes(vl)).serialize()
        raise HeaderPatchError(
            "Fuer den Rest (%d Bytes) ist kein gueltiger KLV-Fill moeglich - Header-Metadaten passen nicht" % rest)

    def copy(self) -> "HeaderMetadata":
        return parse_header_metadata(self.to_bytes())


def uid_list(batch: bytes) -> list[bytes]:
    """Inhalt einer Referenz-Batch/-Array (u32 Anzahl, u32 Groesse 16, UIDs)."""
    if len(batch) < 8:
        return []
    n, sz = struct.unpack(">II", batch[:8])
    if sz != 16 or 8 + n * 16 > len(batch):
        return []
    return [batch[8 + 16 * i:24 + 16 * i] for i in range(n)]


def _uid_batch(uids: list) -> bytes:
    return struct.pack(">II", len(uids), 16) + b"".join(uids)


def parse_header_metadata(buf: bytes) -> HeaderMetadata:
    """Header-Metadaten-Bereich (hinter dem Partition Pack, HeaderByteCount Bytes) parsen."""
    hm = HeaderMetadata()
    pos, n = 0, len(buf)
    have_primer = False
    while pos < n:
        if pos + 17 > n:
            raise MxfParseError("abgeschnittenes KLV im Header-Metadaten-Bereich")
        key = buf[pos:pos + 16]
        ln, nb = read_ber(buf, pos + 16)
        vs = pos + 16 + nb
        if vs + ln > n:
            raise MxfParseError("KLV laenger als der Header-Metadaten-Bereich")
        val = buf[vs:vs + ln]
        if _is_fill(key):
            hm.elements.append(_Fill(key, nb, val))
        elif _norm(key) == _PRIMER_KEY_N:
            if have_primer:
                raise MxfParseError("mehrere Primer Packs")
            have_primer = True
            if ln < 8:
                raise MxfParseError("Primer zu kurz")
            cnt, sz = struct.unpack(">II", val[:8])
            if sz != 18 or 8 + cnt * 18 != ln:
                raise MxfParseError("Primer-Laengen passen nicht")
            for i in range(cnt):
                t = struct.unpack(">H", val[8 + 18 * i:10 + 18 * i])[0]
                if t in hm.primer:
                    raise MxfParseError("doppelter Tag im Primer")
                hm.primer[t] = val[10 + 18 * i:26 + 18 * i]
            hm.elements.append(_PrimerMark(key, nb))
        elif key[4] == 0x02 and key[5] == 0x53:
            items = []
            p = 0
            while p < ln:
                if p + 4 > ln:
                    raise MxfParseError("abgeschnittenes Item in einem Local Set")
                t, l2 = struct.unpack(">HH", val[p:p + 4])
                if p + 4 + l2 > ln:
                    raise MxfParseError("Item laenger als das Local Set")
                items.append((t, val[p + 4:p + 4 + l2]))
                p += 4 + l2
            hm.elements.append(MxfSet(key, items, nb))
        else:
            hm.elements.append(_Raw(key, nb, val))
        pos = vs + ln
    if not have_primer:
        raise MxfParseError("kein Primer Pack in den Header-Metadaten")
    return hm


# --------------------------------------------------------------------------- Lesen / Schreiben
def _read_region(f, p: Partition) -> bytes:
    f.seek(p.meta_offset)
    buf = f.read(p.header_byte_count)
    if len(buf) != p.header_byte_count:
        raise MxfParseError("Header-Metadaten laenger als die Datei")
    return buf


def read_header(path: Union[str, Path]) -> HeaderMetadata:
    """Header-Metadaten lesen (Datei nur 'rb').  Bevorzugt die letzte vollstaendige Partition
    mit Metadaten (Footer, falls dort welche stehen), sonst die letzte mit Metadaten."""
    with open(path, "rb") as f:
        parts = _find_partitions_f(f)
        cands = [p for p in parts if p.header_byte_count > 0]
        if not cands:
            raise MxfParseError("keine Header-Metadaten in der Datei")
        done = [p for p in cands if p.complete]
        use = (done or cands)[-1]
        return parse_header_metadata(_read_region(f, use))


def patch_header(path: Union[str, Path], edit: Callable[[HeaderMetadata], list], dry_run: bool = False) -> list:
    """Header-Metadaten aller Partitionen mit HeaderByteCount > 0 mit ``edit`` aendern und
    in place zurueckschreiben.  Alles wird zuerst im Speicher gebaut und durch erneutes Parsen
    validiert; bei Fehlern (HeaderPatchError) bleibt die Datei unveraendert.  Geschrieben wird
    je Region ein einziges write + flush + fsync.  Gibt die Meldungen von ``edit`` zurueck."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            parts, complete = _find_partitions_ex(f)
            regions = [(p, _read_region(f, p)) for p in parts if p.header_byte_count > 0]
    except MxfParseError as e:
        raise HeaderPatchError("MXF-Struktur nicht lesbar: %s" % e) from e
    if not complete:
        raise HeaderPatchError(
            "Partitionen nicht vollstaendig auffindbar (kein RIP und FooterPartition = 0, offener Header): "
            "Datei wird nicht veraendert")
    if not regions:
        raise HeaderPatchError("Datei enthaelt keine Header-Metadaten")
    msgs: list = []
    changed = []
    for i, (p, buf) in enumerate(regions):
        try:
            hm = parse_header_metadata(buf)
        except MxfParseError as e:
            raise HeaderPatchError("Header-Metadaten (Partition bei %d) nicht lesbar: %s" % (p.offset, e)) from e
        m = edit(hm)
        if i == 0:
            msgs = list(m)
        new = hm.to_bytes(p.header_byte_count)
        if len(new) != len(buf):
            raise HeaderPatchError("interner Fehler: Laenge der Header-Metadaten weicht ab")
        try:
            chk = parse_header_metadata(new)
        except MxfParseError as e:
            raise HeaderPatchError("Validierung der neuen Header-Metadaten fehlgeschlagen: %s" % e) from e
        if chk.to_bytes() != new:
            raise HeaderPatchError("Validierung: erneutes Serialisieren ist nicht identisch")
        if new != buf:
            changed.append((p, new))
    if dry_run or not changed:
        return msgs
    with open(path, "r+b") as f:
        for p, new in changed:
            f.seek(p.meta_offset)
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
    return msgs


# --------------------------------------------------------------------------- Struktur
_DD_KINDS = {
    _norm(bytes.fromhex("060e2b34040101010103020201000000")): "picture",
    _norm(bytes.fromhex("060e2b34040101010103020202000000")): "sound",
    _norm(bytes.fromhex("060e2b34040101010103020101000000")): "timecode",
    _norm(bytes.fromhex("060e2b34040101010103020203000000")): "data",
    _norm(bytes.fromhex("060e2b34040101010103020110000000")): "dm",
    _norm(bytes.fromhex("807d00600814 3e6f6f3c8ce16cef11d2".replace(" ", ""))): "picture",
    _norm(bytes.fromhex("807d00600814 3e6f78e1ebe16cef11d2".replace(" ", ""))): "sound",
    _norm(bytes.fromhex("807f00600814 3e6f7f275e8177e511d2".replace(" ", ""))): "timecode",
}
KINDS = ("timecode", "picture", "sound", "data", "dm", "other")


@dataclass
class TrackView:
    set: MxfSet
    kind: str
    track_id: Optional[int]
    track_number: Optional[int]
    name: Optional[bytes]
    sequence: Optional[MxfSet]
    clips: list


@dataclass
class PackageView:
    set: MxfSet
    role: str                      # 'material' | 'source'
    uid: bytes
    tracks: list
    descriptor: Optional[MxfSet] = None

    def kind_tracks(self, kind: str) -> list:
        return [t for t in self.tracks if t.kind == kind]


@dataclass
class Structure:
    hm: HeaderMetadata
    preface: Optional[MxfSet]
    material: Optional[PackageView]
    file: Optional[PackageView]
    packages: list
    identifications: list
    text_objects: list

    def file_descriptors(self) -> list:
        """Alle Datei-Deskriptoren des File Packages (MultipleDescriptor aufgeloest)."""
        if self.file is None or self.file.descriptor is None:
            return []
        d = self.file.descriptor
        if self.hm.name(d) == "MultipleDescriptor":
            return self.hm.refs(d, "MultipleDescriptor.SubDescriptorUIDs")
        return [d]

    def descriptor_by_track(self) -> dict:
        """id(Track-Set) -> Deskriptor (ueber LinkedTrackID)."""
        out: dict = {}
        if self.file is None:
            return out
        descs = self.file_descriptors()
        for t in self.file.tracks:
            for d in descs:
                if t.track_id is not None and self.hm.u32(d, "FileDescriptor.LinkedTrackID") == t.track_id:
                    out[id(t.set)] = d
                    break
        if len(descs) == 1 and len(self.file.tracks) >= 1 and self.hm.get(descs[0], "FileDescriptor.LinkedTrackID") is None:
            # Einzel-Deskriptor ohne LinkedTrackID: gehoert zur (einzigen) Essenz-Spur
            ess = [t for t in self.file.tracks if t.kind in ("picture", "sound", "data")]
            if len(ess) == 1:
                out[id(ess[0].set)] = descs[0]
        return out


def _track_view(hm: HeaderMetadata, ts: MxfSet) -> TrackView:
    seq = hm.ref(ts, "GenericTrack.Sequence")
    kind = "other"
    clips: list = []
    if seq is not None:
        dd = hm.get(seq, "StructuralComponent.DataDefinition")
        if dd is not None:
            kind = _DD_KINDS.get(_norm(dd), "other")
        if hm.name(seq) == "Sequence":
            comps = hm.refs(seq, "Sequence.StructuralComponents")
        else:
            comps = [seq]
        clips = [c for c in comps if hm.name(c) == "SourceClip"]
    if kind == "other" and hm.name(ts) == "StaticTrack":
        kind = "dm"
    return TrackView(ts, kind, hm.u32(ts, "GenericTrack.TrackID"), hm.u32(ts, "GenericTrack.TrackNumber"),
                     hm.get(ts, "GenericTrack.TrackName"), seq, clips)


def _package_view(hm: HeaderMetadata, ps: MxfSet, role: str) -> PackageView:
    tracks = [_track_view(hm, t) for t in hm.refs(ps, "GenericPackage.Tracks")]
    desc = hm.ref(ps, "SourcePackage.Descriptor") if role == "source" else None
    return PackageView(ps, role, hm.get(ps, "GenericPackage.PackageUID") or b"", tracks, desc)


def resolve(hm: HeaderMetadata) -> Structure:
    """Preface -> ContentStorage -> Packages -> Tracks -> Sequence -> SourceClips aufloesen."""
    prefs = hm.find_sets("Preface")
    preface = prefs[0] if prefs else None
    cs = hm.ref(preface, "Preface.ContentStorage") if preface else None
    pkg_sets = hm.refs(cs, "ContentStorage.Packages") if cs else []
    if not pkg_sets:
        pkg_sets = hm.find_sets("MaterialPackage") + hm.find_sets("SourcePackage")
    mats = [_package_view(hm, p, "material") for p in pkg_sets if hm.name(p) == "MaterialPackage"]
    srcs = [_package_view(hm, p, "source") for p in pkg_sets if hm.name(p) == "SourcePackage"]
    material = mats[0] if mats else None
    file_pkg = None
    if material is not None:
        linked = {c_uid for t in material.tracks for c in t.clips
                  if (c_uid := hm.get(c, "SourceClip.SourcePackageID")) is not None}
        for sp in srcs:
            if sp.uid in linked and sp.descriptor is not None:
                file_pkg = sp
                break
    if file_pkg is None:
        file_pkg = next((sp for sp in srcs if sp.descriptor is not None), srcs[0] if srcs else None)
    ids: list = []
    if preface is not None:
        ids = hm.refs(preface, "Preface.Identifications")
    if not ids:
        ids = hm.find_sets("Identification")
    texts = [s for s in hm.sets if hm.get(s, "TextBasedObject.TextMIMEMediaType") is not None]
    return Structure(hm, preface, material, file_pkg, mats + srcs, ids, texts)


# --------------------------------------------------------------------------- Track-IDs
def set_track_ids(hm: HeaderMetadata, pkg: PackageView, new_ids: list) -> dict:
    """TrackID der Tracks von ``pkg`` setzen (new_ids parallel zu pkg.tracks).
    Gibt {alte ID: neue ID} zurueck.  Verweise (SourceClips, Deskriptoren) werden hier NICHT
    angepasst - dafuer relink_after_renumber()."""
    if len(new_ids) != len(pkg.tracks):
        raise HeaderPatchError("interner Fehler: Anzahl neuer Track-IDs passt nicht")
    mapping: dict = {}
    for t, nid in zip(pkg.tracks, new_ids):
        if t.track_id is not None:
            mapping[t.track_id] = nid
        hm.put(t.set, "GenericTrack.TrackID", struct.pack(">I", nid),
               after=["InterchangeObject.InstanceUID"])
        t.track_id = nid
    return mapping


def _map_u32_batch(hm: HeaderMetadata, s: MxfSet, ref: str, mapping: dict) -> bool:
    v = hm.get(s, ref)
    if v is None or len(v) < 8:
        return False
    n, sz = struct.unpack(">II", v[:8])
    if sz != 4 or 8 + 4 * n != len(v):
        return False
    old = list(struct.unpack(">%dI" % n, v[8:]))
    new = [mapping.get(x, x) for x in old]
    if new == old:
        return False
    hm.put(s, ref, v[:8] + struct.pack(">%dI" % n, *new))
    return True


def relink_after_renumber(hm: HeaderMetadata, st: Structure, fp_map: dict, mp_map: Optional[dict] = None) -> None:
    """Verweise nach einer Umnummerierung nachziehen: SourceClip.SourceTrackID im Material
    Package (zeigt auf File-Package-Tracks), LinkedTrackID(s) der Deskriptoren, TrackIDs der
    DM-Segmente.  fp_map/mp_map: {alte ID: neue ID} des File- bzw. Material Packages."""
    fp = st.file
    if fp is None:
        return
    for pkg in st.packages:
        if pkg.role != "material":
            continue
        for t in pkg.tracks:
            for c in t.clips:
                if hm.get(c, "SourceClip.SourcePackageID") != fp.uid:
                    continue
                old = hm.u32(c, "SourceClip.SourceTrackID")
                if old is None or old == 0:
                    continue
                if old not in fp_map:
                    raise HeaderPatchError("SourceClip im Material Package verweist auf unbekannten Track %d" % old)
                hm.put(c, "SourceClip.SourceTrackID", struct.pack(">I", fp_map[old]))
    for d in st.file_descriptors():
        for ref in ("FileDescriptor.LinkedTrackID", "AES3AudioDescriptor.LinkedTimecodeTrackID"):
            old = hm.u32(d, ref)
            if old is not None and old in fp_map:
                hm.put(d, ref, struct.pack(">I", fp_map[old]))
    for pkg, mp in ((st.material, mp_map), (st.file, fp_map)):
        if pkg is None or not mp:
            continue
        for t in pkg.tracks:
            if t.sequence is None:
                continue
            comps = hm.refs(t.sequence, "Sequence.StructuralComponents") if hm.name(t.sequence) == "Sequence" else [t.sequence]
            for c in comps:
                if hm.name(c) == "DMSegment":
                    _map_u32_batch(hm, c, "DMSegment.TrackIDs", mp)
                elif hm.name(c) == "DMSourceClip":
                    _map_u32_batch(hm, c, "DMSourceClip.DMSourceClipTrackIDs", mp)


def verify_references(hm: HeaderMetadata) -> None:
    """Track-IDs je Package eindeutig; jeder SourceClip-/LinkedTrackID-Verweis zeigt auf einen
    existierenden Track.  Sonst HeaderPatchError."""
    st = resolve(hm)
    by_uid = {}
    for pkg in st.packages:
        ids = [t.track_id for t in pkg.tracks]
        if any(i is None for i in ids):
            raise HeaderPatchError("Track ohne TrackID gefunden")
        if len(set(ids)) != len(ids):
            raise HeaderPatchError("Track-IDs sind nicht eindeutig (%s)" % ids)
        by_uid[pkg.uid] = set(ids)
    for pkg in st.packages:
        for t in pkg.tracks:
            for c in t.clips:
                pid = hm.get(c, "SourceClip.SourcePackageID")
                tid = hm.u32(c, "SourceClip.SourceTrackID")
                if pid in by_uid and tid not in by_uid[pid]:
                    raise HeaderPatchError("SourceClip verweist auf nicht vorhandenen Track %s" % tid)
    if st.file is not None:
        ids = by_uid[st.file.uid]
        for d in st.file_descriptors():
            for ref in ("FileDescriptor.LinkedTrackID", "AES3AudioDescriptor.LinkedTimecodeTrackID"):
                v = hm.u32(d, ref)
                if v is not None and v not in ids:
                    raise HeaderPatchError("%s zeigt auf nicht vorhandenen Track %d" % (ref.split(".")[1], v))


# --------------------------------------------------------------------------- Uebernahme
_IDENT_FIELDS = ["CompanyName", "ProductName", "VersionString", "ProductUID", "ProductVersion",
                 "ToolkitVersion", "Platform"]
_SONY_FALLBACK = {
    "CompanyName": utf16("Sony"),
    "ProductName": utf16("Mem "),
    "VersionString": utf16("2.00"),
    "ProductUID": bytes.fromhex("cede1104828011de8a3908004678031c"),
    "ToolkitVersion": None,
    "Platform": None,
}
_SOUND_KEEP = ("InterchangeObject.InstanceUID", "FileDescriptor.ContainerDuration",
               "FileDescriptor.SampleRate", "FileDescriptor.EssenceContainer")
_SOUND_REFS = ("GenericDescriptor.SubDescriptors", "GenericDescriptor.Locators")
_XML_MIME = utf16("text/xml")


def _show(hm_name: str, v: Optional[bytes]) -> str:
    if v is None:
        return "(fehlt)"
    if hm_name in ("CompanyName", "ProductName", "VersionString", "Platform", "TrackName",
                   "TextMIMEMediaType", "TextDataDescription"):
        return "'" + _dec16(v) + "'"
    return v.hex()


def _last_ident(st: Structure) -> Optional[MxfSet]:
    return st.identifications[-1] if st.identifications else None


def _expected_ident(src: HeaderMetadata) -> dict:
    s = _last_ident(resolve(src))
    if s is None:
        return dict(_SONY_FALLBACK)
    return {f: src.get(s, "Identification." + f) for f in _IDENT_FIELDS}


def _adopt_identification(tgt: HeaderMetadata, src: HeaderMetadata, log: list) -> None:
    ts = _last_ident(resolve(tgt))
    if ts is None:
        raise HeaderPatchError("Ziel hat keine Identification")
    exp = _expected_ident(src)
    order = ["Identification." + f for f in _IDENT_FIELDS]
    after_base = ["InterchangeObject.InstanceUID", "Identification.ThisGenerationUID", "Identification.ModificationDate"]
    for f, want in exp.items():
        q = "Identification." + f
        cur = tgt.get(ts, q)
        if want is None:
            if cur is not None:
                tgt.drop(ts, q)
                log.append("Identification.%s: %s entfernt" % (f, _show(f, cur)))
        elif cur != want:
            prev = [x for x in order[:order.index(q)]]
            tgt.put(ts, q, want, after=prev or after_base)
            log.append("Identification.%s: %s -> %s" % (f, _show(f, cur), _show(f, want)))


_ESSENCE_KINDS = ("picture", "sound", "data")


def _pairs(tp: PackageView, sp: PackageView) -> list:
    """(Ziel-Track, Quell-Track oder None): nach Art und laufender Nummer innerhalb der Art."""
    out = []
    for kind in KINDS:
        tl, sl = tp.kind_tracks(kind), sp.kind_tracks(kind)
        for i, t in enumerate(tl):
            out.append((t, sl[i] if i < len(sl) else None))
    return out


def _fmt_nums(tracks: list) -> str:
    return ", ".join("0x%08x" % (t.track_number or 0) for t in tracks)


def _pairs_file(tp: PackageView, sp: PackageView, log: list) -> list:
    """Paarung der Tracks des File Source Packages.  Essenz-Tracks (Bild/Ton/Daten) werden ueber
    die TrackNumber gepaart, wenn in Quelle und Ziel alle Nummern der Art != 0 und eindeutig sind
    und die Mengen uebereinstimmen; sonst nach Position je Art (bei mehreren Spuren mit WARNUNG)."""
    out = []
    for kind in KINDS:
        tl, sl = tp.kind_tracks(kind), sp.kind_tracks(kind)
        pairs = None
        if kind in _ESSENCE_KINDS and tl and sl:
            tn, sn = [t.track_number for t in tl], [s.track_number for s in sl]
            usable = all(tn) and all(sn) and len(set(tn)) == len(tn) and len(set(sn)) == len(sn)
            if usable and set(tn) == set(sn):
                by_num = {s.track_number: s for s in sl}
                pairs = [(t, by_num[t.track_number]) for t in tl]
            elif usable and (len(tl) > 1 or len(sl) > 1):
                log.append("WARNUNG: File Package, %s: TrackNumbers von Quelle (%s) und Ziel (%s) passen nicht "
                           "zusammen - Spuren werden nach Position gepaart" % (kind, _fmt_nums(sl), _fmt_nums(tl)))
        if pairs is None:
            pairs = [(t, sl[i] if i < len(sl) else None) for i, t in enumerate(tl)]
        out.extend(pairs)
    return out


def _pairs_material(tgt: HeaderMetadata, src: HeaderMetadata, ts: Structure, ss: Structure,
                    fp_pairs: list) -> list:
    """Paarung der Material-Package-Tracks: Essenz-Tracks ueber ihre SourceClips (SourceTrackID ->
    File-Package-Track -> dessen Quell-Partner -> Material-Track der Quelle, der darauf verweist);
    Rueckfall (und alle anderen Arten): Position je Art."""
    tp, sp = ts.material, ss.material
    fp_by_id = {t.track_id: t for t in ts.file.tracks}
    partner = {id(t.set): s for t, s in fp_pairs}
    # Quelle: (File-Package-Track-ID der Quelle) -> Material-Track
    src_mp: dict = {}
    for st in sp.tracks:
        for c in st.clips:
            if src.get(c, "SourceClip.SourcePackageID") == ss.file.uid:
                src_mp.setdefault(src.u32(c, "SourceClip.SourceTrackID"), []).append(st)
    out = []
    for kind in KINDS:
        tl, sl = tp.kind_tracks(kind), sp.kind_tracks(kind)
        pairs = None
        if kind in _ESSENCE_KINDS and tl and sl:
            cand = []
            for t in tl:
                found = None
                for c in t.clips:
                    if tgt.get(c, "SourceClip.SourcePackageID") != ts.file.uid:
                        continue
                    ft = fp_by_id.get(tgt.u32(c, "SourceClip.SourceTrackID"))
                    fs = partner.get(id(ft.set)) if ft is not None else None
                    if fs is not None:
                        lst = src_mp.get(fs.track_id, [])
                        if len(lst) == 1 and lst[0].kind == kind:
                            found = lst[0]
                        break
                cand.append(found)
            if all(x is not None for x in cand) and len({id(x.set) for x in cand}) == len(cand):
                pairs = list(zip(tl, cand))
        if pairs is None:
            pairs = [(t, sl[i] if i < len(sl) else None) for i, t in enumerate(tl)]
        out.extend(pairs)
    return out


def _adopt_tracks(tgt: HeaderMetadata, src: HeaderMetadata, ts: Structure, ss: Structure, log: list) -> dict:
    """Gibt {id(Ziel-Track-Set): Quell-Track} der gepaarten Tracks des File Packages zurueck (fuer Regel c)."""
    if ts.material is None or ts.file is None:
        raise HeaderPatchError("Ziel hat kein Material Package oder File Source Package")
    if ss.material is None or ss.file is None:
        raise HeaderPatchError("Quelle hat kein Material Package oder File Source Package")
    for (tp, sp, what) in ((ts.material, ss.material, "Material Package"), (ts.file, ss.file, "File Package")):
        if len(tp.kind_tracks("sound")) != len(sp.kind_tracks("sound")):
            raise HeaderPatchError("Anzahl Tonspuren weicht ab (%s: Quelle %d, Ziel %d)" % (
                what, len(sp.kind_tracks("sound")), len(tp.kind_tracks("sound"))))
    fp_pairs = _pairs_file(ts.file, ss.file, log)
    mp_pairs = _pairs_material(tgt, src, ts, ss, fp_pairs)
    sound_map = {id(t.set): s for t, s in fp_pairs if t.kind == "sound" and s is not None}
    fp_old = {t.track_id: t for t in ts.file.tracks}
    # --- File Package zuerst, dann Material Package (unzugeordnete MP-Tracks erben ggf. die FP-ID)
    fp_new: dict = {}
    mp_new: dict = {}
    for pairs, store, pkg_name in ((fp_pairs, fp_new, "File Package"), (mp_pairs, mp_new, "Material Package")):
        for t, s in pairs:
            if s is not None:
                if s.track_id is None:
                    raise HeaderPatchError("Quell-Track ohne TrackID")
                store[id(t.set)] = s.track_id
        mapped = set(store.values())
        top = max(mapped, default=0)
        for t, s in pairs:
            if s is not None:
                continue
            cand = None
            if pkg_name == "Material Package":
                for c in t.clips:
                    if tgt.get(c, "SourceClip.SourcePackageID") == ts.file.uid:
                        ft = fp_old.get(tgt.u32(c, "SourceClip.SourceTrackID"))
                        if ft is not None and fp_new.get(id(ft.set)) is not None \
                                and fp_new[id(ft.set)] not in mapped:
                            cand = fp_new[id(ft.set)]
                            break
            if cand is None:
                cand = top + 1
            while cand in mapped:
                cand += 1
            store[id(t.set)] = cand
            mapped.add(cand)
            top = max(top, cand)
    # --- anwenden
    fp_map: dict = {}
    mp_map: dict = {}
    for pkg, store, mp, label in ((ts.file, fp_new, fp_map, "File Package"), (ts.material, mp_new, mp_map, "Material Package")):
        spairs = dict((id(t.set), s) for t, s in (fp_pairs if pkg is ts.file else mp_pairs))
        id_changes: list = []
        name_changes: list = []
        for t in pkg.tracks:
            nid = store[id(t.set)]
            if t.track_id is not None:
                mp[t.track_id] = nid
            if t.track_id != nid:
                id_changes.append("%s->%d" % (t.track_id, nid))
            tgt.put(t.set, "GenericTrack.TrackID", struct.pack(">I", nid), after=["InterchangeObject.InstanceUID"])
            s = spairs.get(id(t.set))
            if s is not None:
                if s.name is None:
                    if t.name is not None:
                        tgt.drop(t.set, "GenericTrack.TrackName")
                        name_changes.append("%s entfernt" % _show("TrackName", t.name))
                elif t.name != s.name:
                    tgt.put(t.set, "GenericTrack.TrackName", s.name,
                            after=["GenericTrack.TrackID"])
                    name_changes.append("%s -> %s" % (_show("TrackName", t.name), _show("TrackName", s.name)))
                if s.track_number is not None and t.track_number is not None and s.track_number != t.track_number:
                    log.append("WARNUNG: %s, Spur %d (%s): TrackNumber 0x%08x im Ziel, 0x%08x in der Quelle (nicht geaendert)" % (
                        label, nid, t.kind, t.track_number, s.track_number))
        if id_changes:
            log.append("%s: TrackIDs geaendert (alt->neu): %s" % (label, ", ".join(id_changes)))
        if name_changes:
            log.append("%s: TrackName an Quelle angeglichen (%d): %s" % (label, len(name_changes), ", ".join(name_changes)))
        # Track-Reihenfolge wie Quelle
        src_pkg = ss.file if pkg is ts.file else ss.material
        src_pos = {id(s.set): i for i, s in enumerate(src_pkg.tracks)}
        pos_of = {id(t.set): (src_pos[id(spairs[id(t.set)].set)] if spairs.get(id(t.set)) is not None else 10 ** 6 + i)
                  for i, t in enumerate(pkg.tracks)}
        ordered = sorted(pkg.tracks, key=lambda t: pos_of[id(t.set)])
        if [id(t.set) for t in ordered] != [id(t.set) for t in pkg.tracks]:
            uids = [t.set.instance_uid for t in ordered]
            tgt.put(pkg.set, "GenericPackage.Tracks", _uid_batch(uids))
            log.append("%s: Track-Reihenfolge wie in der Quelle" % label)
    # Verweise nachziehen (Struktur neu aufloesen, weil IDs geaendert wurden)
    st2 = resolve(tgt)
    relink_after_renumber(tgt, st2, fp_map, mp_map)
    return sound_map


# Essenzbeschreibende Items des Sound-Deskriptors: Widerspruch zwischen Quelle und Ziel = Fehler
_SOUND_ESSENCE = ("GenericSoundEssenceDescriptor.QuantizationBits", "WaveAudioDescriptor.BlockAlign",
                  "GenericSoundEssenceDescriptor.ChannelCount", "GenericSoundEssenceDescriptor.AudioSamplingRate",
                  "WaveAudioDescriptor.AvgBps")
_SEQ_OFFSET = "WaveAudioDescriptor.SequenceOffset"
# Klassenhierarchie der Sound-Deskriptoren (Klasse -> Praefixe der erlaubten Item-Namen)
_SOUND_ANCESTORS = {
    "AES3AudioDescriptor": ("AES3AudioDescriptor", "WaveAudioDescriptor", "GenericSoundEssenceDescriptor",
                            "FileDescriptor", "GenericDescriptor", "InterchangeObject"),
    "WaveAudioDescriptor": ("WaveAudioDescriptor", "GenericSoundEssenceDescriptor",
                            "FileDescriptor", "GenericDescriptor", "InterchangeObject"),
    "GenericSoundEssenceDescriptor": ("GenericSoundEssenceDescriptor", "FileDescriptor",
                                      "GenericDescriptor", "InterchangeObject"),
}


def _int(v: bytes) -> int:
    return int.from_bytes(v, "big")


def _sound_value_text(name: str, v: Optional[bytes]) -> str:
    if v is None:
        return "(fehlt)"
    if name.endswith("AudioSamplingRate") and len(v) == 8:
        n, d = struct.unpack(">ii", v)
        return "%d/%d" % (n, d)
    return str(_int(v)) if len(v) <= 4 else v.hex()


def _sound_conflicts(tgt: HeaderMetadata, src: HeaderMetadata, td: MxfSet, sd: MxfSet) -> list:
    """Widersprueche zwischen essenzbeschreibenden Items von Quell- und Ziel-Deskriptor:
    Liste (Feldname, Quellwert-Text, Zielwert-Text, Hinweis)."""
    out = []
    for q in _SOUND_ESSENCE:
        sv, tv = src.get(sd, q), tgt.get(td, q)
        if sv is None or tv is None:
            continue
        same = sv == tv
        if q.endswith("AudioSamplingRate") and len(sv) == len(tv) == 8:
            sn, sdn = struct.unpack(">ii", sv)
            tn, tdn = struct.unpack(">ii", tv)
            same = sdn != 0 and tdn != 0 and sn * tdn == tn * sdn
        if not same:
            out.append((q.split(".")[1], _sound_value_text(q, sv), _sound_value_text(q, tv), ""))
    so = src.get(sd, _SEQ_OFFSET)
    if so is not None:
        to = tgt.get(td, _SEQ_OFFSET)
        if _int(so) != (_int(to) if to is not None else 0):
            out.append(("SequenceOffset", str(_int(so)), str(_int(to)) if to is not None else "(fehlt, gilt als 0)",
                        " - raw2bmx muss mit --seq-off %d aufgerufen werden" % _int(so)))
    return out


def _is_ref_value(hm: HeaderMetadata, v: bytes) -> bool:
    """Wert sieht wie Verweis auf ein Set aus (16-Byte-UID bzw. Array/Batch aus solchen UIDs)."""
    idx = hm.by_uid()
    if len(v) == 16:
        return v in idx
    uids = uid_list(v)
    return bool(uids) and len(v) == 8 + 16 * len(uids) and all(u in idx for u in uids)


def _ec_equal(a: Optional[bytes], b: Optional[bytes]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return len(a) == 16 and len(b) == 16 and _norm(a) == _norm(b)


def _adopt_sound(tgt: HeaderMetadata, src: HeaderMetadata, ts: Structure, ss: Structure,
                 sound_map: dict, log: list) -> None:
    tdesc = ts.descriptor_by_track()
    sdesc = ss.descriptor_by_track()
    keep_uls = {_ITEM_UL[k] for k in _SOUND_KEEP}
    lt_ul = _ITEM_UL["FileDescriptor.LinkedTrackID"]
    iu_ul = _ITEM_UL["InterchangeObject.InstanceUID"]
    ref_uls = {_ITEM_UL[k] for k in _SOUND_REFS}
    pairs = []
    for n, t in enumerate(ts.file.kind_tracks("sound"), 1):
        s = sound_map.get(id(t.set))
        td, sd = tdesc.get(id(t.set)), (sdesc.get(id(s.set)) if s is not None else None)
        if td is None or sd is None or s is None:
            raise HeaderPatchError("Tonspur %d: Sound-Deskriptor nicht eindeutig zuordenbar (LinkedTrackID)" % n)
        pairs.append((n, t, td, sd))
    # --- Vorpruefung: Widerspruch bei essenzbeschreibenden Items -> Fehler, nichts wird geaendert
    problems = []
    for n, t, td, sd in pairs:
        for field_, sv, tv, hint in _sound_conflicts(tgt, src, td, sd):
            problems.append("Tonspur %d: %s weicht ab (Quelle %s, Ziel %s)%s" % (n, field_, sv, tv, hint))
    if problems:
        raise HeaderPatchError("Audio-Daten von Quelle und Ziel passen nicht zusammen - " + "; ".join(problems))
    for n, t, td, sd in pairs:
        before = (td.key, list(td.items))
        keep = {q: tgt.get(td, q) for q in _SOUND_KEEP}
        s_ec, t_ec = src.get(sd, "FileDescriptor.EssenceContainer"), keep["FileDescriptor.EssenceContainer"]
        same_ec = _ec_equal(s_ec, t_ec)
        if not same_ec:
            log.append("WARNUNG: Tonspur %d: EssenceContainer-Label weicht von der Quelle ab (Ziel %s, Quelle %s) - "
                       "Deskriptor-Klasse (%s) und EssenceContainer des Ziels bleiben" % (
                           n, t_ec.hex() if t_ec else "-", s_ec.hex() if s_ec else "-", set_name(td.key)))
        new_key = sd.key if same_ec else td.key
        allowed = _SOUND_ANCESTORS.get(set_name(new_key))
        new_items: list = []
        dropped_refs: list = []
        dropped_cls: list = []
        used_keep = set()
        for tag, val in sd.items:
            ul = src.ul_of_tag(tag)
            if ul is None:
                raise HeaderPatchError("Quell-Deskriptor enthaelt Tag 0x%04x ohne Primer-Eintrag" % tag)
            if ul == lt_ul:
                new_items.append((tgt.tag_for_ul(ul, create=True, full_ul=src.raw_ul_of_tag(tag)),
                                  struct.pack(">I", t.track_id)))
                used_keep.add("lt")
                continue
            if ul in keep_uls:
                q = next(k for k in _SOUND_KEEP if _ITEM_UL[k] == ul)
                used_keep.add(q)
                if keep[q] is not None:
                    new_items.append((tgt.tag_for_ul(ul, create=True, full_ul=src.raw_ul_of_tag(tag)), keep[q]))
                continue
            if ul in ref_uls or _is_ref_value(src, val):
                dropped_refs.append(src.item_name(tag))
                continue
            if not same_ec and allowed is not None and ul in _ITEM_INFO \
                    and _ITEM_INFO[ul][0].split(".")[0] not in allowed:
                dropped_cls.append(src.item_name(tag))
                continue
            new_items.append((tgt.tag_for_ul(ul, create=True, full_ul=src.raw_ul_of_tag(tag)), val))
        for q in _SOUND_KEEP:
            if q not in used_keep and keep[q] is not None:
                new_items.append((tgt.tag_for_ul(_ITEM_UL[q], create=True), keep[q]))
        if "lt" not in used_keep:
            new_items.append((tgt.tag_for_ul(lt_ul, create=True), struct.pack(">I", t.track_id)))
        # Verweise, die das Ziel bisher hatte und die entfallen: untergeordnete Zielsets entfernen
        old_refs = []
        for tag, val in td.items:
            ul = tgt.ul_of_tag(tag)
            if ul in (iu_ul, lt_ul) or ul in keep_uls or not (ul in ref_uls or _is_ref_value(tgt, val)):
                continue
            old_refs.append((tag, val))
        td.key = new_key
        td.items = new_items
        for tag, val in old_refs:
            for u in ([val] if len(val) == 16 else uid_list(val)):
                o = tgt.by_uid().get(u)
                if o is not None and (tgt.name(o).endswith("SubDescriptor") or "Locator" in tgt.name(o)):
                    tgt.remove_set(o)
            log.append("Tonspur %d: Verweis %s im Ziel entfernt (Quelle hat keinen solchen Verweis)" % (
                n, tgt.item_name(tag)))
        if dropped_refs:
            log.append("HINWEIS: Tonspur %d: Verweise der Quelle (%s) nicht uebernommen" % (
                n, ", ".join(sorted(set(dropped_refs)))))
        if dropped_cls:
            log.append("HINWEIS: Tonspur %d: Items der Quell-Klasse (%s) passen nicht zur Ziel-Klasse und wurden "
                       "nicht uebernommen" % (n, ", ".join(sorted(set(dropped_cls)))))
        after = (td.key, list(td.items))
        if after != before:
            log.append("Tonspur %d: Sound-Deskriptor an Quelle angeglichen (%s)" % (
                n, _items_diff(tgt, before, after)))


def _items_diff(hm: HeaderMetadata, before: tuple, after: tuple) -> str:
    def to_map(items):
        m = {}
        for t, v in items:
            ul = hm.ul_of_tag(t)
            m[ul] = v
        return m
    bk, bi = before
    ak, ai = after
    b, a = to_map(bi), to_map(ai)
    parts = []
    if _norm(bk) != _norm(ak) or bk != ak:
        parts.append("Klasse %s -> %s" % (set_name(bk), set_name(ak)))
    nm = lambda ul: (_ITEM_INFO.get(ul) or ("UL " + ul.hex() if ul else "?", ""))[0].split(".")[-1]
    for ul in a:
        if ul not in b:
            parts.append("+" + nm(ul))
        elif a[ul] != b[ul]:
            parts.append("~" + nm(ul))
    for ul in b:
        if ul not in a:
            parts.append("-" + nm(ul))
    if not parts and [t for t, _ in bi] != [t for t, _ in ai]:
        parts.append("Reihenfolge/Tags")
    return ", ".join(parts) if parts else "Tags"


def _adopt_text(tgt: HeaderMetadata, src: HeaderMetadata, ts: Structure, ss: Structure, log: list) -> None:
    if not ts.text_objects:
        return
    srcobj = ss.text_objects[0] if ss.text_objects else None
    for o in ts.text_objects:
        if srcobj is not None:
            want_m = src.get(srcobj, "TextBasedObject.TextMIMEMediaType")
            want_d = src.get(srcobj, "TextBasedObject.TextDataDescription")
        else:
            want_m, want_d = _XML_MIME, None
        cur_m = tgt.get(o, "TextBasedObject.TextMIMEMediaType")
        if want_m is not None and cur_m != want_m:
            tgt.put(o, "TextBasedObject.TextMIMEMediaType", want_m, after=["TextBasedObject.TextBasedMetadataPayloadSchemaID"])
            log.append("TextObject: MIME-Typ %s -> %s" % (_show("TextMIMEMediaType", cur_m), _show("TextMIMEMediaType", want_m)))
        cur_d = tgt.get(o, "TextBasedObject.TextDataDescription")
        if srcobj is None:
            # FX6-Referenz: description leer
            if cur_d is not None and _dec16(cur_d) != "":
                tgt.put(o, "TextBasedObject.TextDataDescription", utf16(""))
                log.append("TextObject: Description %s -> leer" % _show("TextDataDescription", cur_d))
        elif want_d is None:
            if cur_d is not None:
                tgt.drop(o, "TextBasedObject.TextDataDescription")
                log.append("TextObject: Description %s entfernt" % _show("TextDataDescription", cur_d))
        elif cur_d != want_d:
            tgt.put(o, "TextBasedObject.TextDataDescription", want_d,
                    after=["TextBasedObject.RFC5646TextLanguageCode", "TextBasedObject.TextMIMEMediaType"])
            log.append("TextObject: Description %s -> %s" % (_show("TextDataDescription", cur_d), _show("TextDataDescription", want_d)))


def adopt_headers(tgt: HeaderMetadata, src: HeaderMetadata) -> list:
    """Ziel-Header-Metadaten (im Speicher) an die Quelle angleichen, Regeln a)-d) der SPEC.
    Gibt deutsche Kurzbeschreibungen der Aenderungen zurueck (Warnungen beginnen mit 'WARNUNG:',
    bewusst nicht uebernommene Items mit 'HINWEIS:')."""
    log: list = []
    _adopt_identification(tgt, src, log)
    ts, ss = resolve(tgt), resolve(src)
    sound_map = _adopt_tracks(tgt, src, ts, ss, log)
    ts = resolve(tgt)
    # sound_map verweist auf Track-Sets des Ziels (Set-Objekte bleiben gleich) -> id() stabil
    _adopt_sound(tgt, src, ts, ss, sound_map, log)
    ts = resolve(tgt)
    _adopt_text(tgt, src, ts, ss, log)
    verify_references(tgt)
    return log


def compare_headers(tgt: HeaderMetadata, src: HeaderMetadata) -> list:
    """Abweichungen des Ziels von der Quelle in den Feldern aus a)-d) (Trockenlauf von adopt)."""
    work = tgt.copy()
    try:
        msgs = adopt_headers(work, src)
    except HeaderPatchError as e:
        return ["Abweichung: %s" % e]
    return ["Abweichung: " + m for m in msgs if not m.startswith(("WARNUNG:", "HINWEIS:"))]


def adopt_from_source(target: Union[str, Path], source: Union[str, Path]) -> list:
    """Header-Metadaten der Ziel-MXF (raw2bmx-Ausgabe) an die Quell-MXF (Sony) angleichen.
    Die Quelle wird nur gelesen.  Rueckgabe: kurze deutsche Beschreibungen der Aenderungen."""
    src = read_header(source)
    return patch_header(target, lambda hm: adopt_headers(hm, src))


def compare_with_source(target: Union[str, Path], source: Union[str, Path]) -> list:
    """Abweichungen des Ziels von der Quelle (Felder a-d) als deutsche Saetze; leer = ok."""
    src = read_header(source)
    return compare_headers(read_header(target), src)


# --------------------------------------------------------------------------- Textausgabe
def _hexdots(b: bytes) -> str:
    return ".".join(b[i:i + 4].hex() for i in range(0, len(b), 4))


def format_value(hm: HeaderMetadata, typ: str, v: bytes) -> str:
    try:
        if typ == "UTF16STRING":
            return repr(_dec16(v))
        if typ in ("UINT8", "UINT16", "UINT32", "UINT64") and len(v) == int(typ[4:]) // 8:
            return str(int.from_bytes(v, "big"))
        if typ in ("INT8", "INT16", "INT32", "INT64") and len(v) == int(typ[3:]) // 8:
            return str(int.from_bytes(v, "big", signed=True))
        if typ in ("LENGTH", "POSITION") and len(v) == 8:
            return str(int.from_bytes(v, "big", signed=True))
        if typ == "BOOLEAN" and len(v) == 1:
            return "true" if v[0] else "false"
        if typ == "RATIONAL" and len(v) == 8:
            n, d = struct.unpack(">ii", v)
            return "%d/%d" % (n, d)
        if typ == "TIMESTAMP" and len(v) == 8:
            y, mo, d, h, mi, s, q = struct.unpack(">HBBBBBB", v)
            return "%04d-%02d-%02d %02d:%02d:%02d.%02d" % (y, mo, d, h, mi, s, q)
        if typ == "PRODUCTVERSION" and len(v) == 10:
            return ".".join(str(x) for x in struct.unpack(">5H", v))
        if typ == "VERSIONTYPE" and len(v) == 2:
            return "%d.%d" % (v[0], v[1])
        if typ in ("UL", "UUID", "UMID", "PACKAGEID", "AUID", "UID", "WEAKREF"):
            return _hexdots(v)
        if typ in ("STRONGREF", "STRONGREFARRAY", "STRONGREFBATCH", "WEAKREFARRAY", "WEAKREFBATCH"):
            uids = [v] if typ == "STRONGREF" else uid_list(v)
            idx = hm.by_uid()
            return "-> " + (", ".join(hm.name(idx[u]) if u in idx else "?" for u in uids) or "(leer)")
        if typ in ("UINT32BATCH", "UINT32ARRAY") and len(v) >= 8:
            n = struct.unpack(">I", v[:4])[0]
            if 8 + 4 * n == len(v):
                return "[" + ", ".join(str(x) for x in struct.unpack(">%dI" % n, v[8:])) + "]"
        if typ in ("ULBATCH", "ULARRAY") and len(v) >= 8:
            return "[" + ", ".join(_hexdots(x) for x in uid_list(v)) + "]"
    except (struct.error, ValueError):
        pass
    return v.hex()


def describe_metadata(hm: HeaderMetadata) -> str:
    lines = []
    for e in hm.elements:
        if isinstance(e, _PrimerMark):
            lines.append("Primer: %d Eintraege" % len(hm.primer))
        elif isinstance(e, MxfSet):
            lines.append(hm.name(e))
            for tag, val in e.items:
                lines.append("    %s = %s" % (hm.item_name(tag), format_value(hm, hm.item_type(tag), val)))
        elif _is_fill(e.key) and isinstance(e, _Fill) and not isinstance(e, _Raw):
            lines.append("KLV-Fill: %d Bytes" % (16 + len(enc_ber(len(e.value), e.ber_size)) + len(e.value)))
        else:
            lines.append("KLV %s (%d Bytes)" % (e.key.hex(), len(e.value)))
    return "\n".join(lines) + "\n"


def describe(path: Union[str, Path]) -> str:
    """Lesbare, stabile Textausgabe aller Sets/Items der Header-Metadaten (Partition-Uebersicht
    ohne Dateioffsets, danach Sets mit Namen aus dem Datenmodell, sonst UL)."""
    with open(path, "rb") as f:
        parts = _find_partitions_f(f)
    out = []
    for i, p in enumerate(parts):
        out.append("Partition %d: %s, Status %d, KAG %d, HeaderByteCount>0: %s, BodySID %d" % (
            i, p.kind, p.status, p.kag_size, "ja" if p.header_byte_count else "nein", p.body_sid))
    out.append("")
    out.append(describe_metadata(read_header(path)))
    return "\n".join(out)


_NAMES_TXT = """\
S 00000000000000000000000000000000 root
S 060e2b34025301010d01010101010100 InterchangeObject
S 060e2b34025301010d01010101012f00 Preface
S 060e2b34025301010d01010101013000 Identification
S 060e2b34025301010d01010101011800 ContentStorage
S 060e2b34025301010d01010101012300 EssenceContainerData
S 060e2b34025301010d01010101013400 GenericPackage
S 060e2b34025301010d01010101013100 Locator
S 060e2b34025301010d01010101013200 NetworkLocator
S 060e2b34025301010d01010101013300 TextLocator
S 060e2b34025301010d01010101013800 GenericTrack
S 060e2b34025301010d01010101013a00 StaticTrack
S 060e2b34025301010d01010101013b00 Track
S 060e2b34025301010d01010101013900 EventTrack
S 060e2b34025301010d01010101010200 StructuralComponent
S 060e2b34025301010d01010101010f00 Sequence
S 060e2b34025301010d01010101011400 TimecodeComponent
S 060e2b34025301010d01010101010400 EdgeCode
S 060e2b34025301010d01010101011100 SourceClip
S 060e2b34025301010d01010101014100 DMSegment
S 060e2b34025301010d01010101014500 DMSourceClip
S 060e2b34025301010d01010101013600 MaterialPackage
S 060e2b34025301010d01010101013700 SourcePackage
S 060e2b34025301010d01010101012400 GenericDescriptor
S 060e2b34025301010d01010101012500 FileDescriptor
S 060e2b34025301010d01010101012700 GenericPictureEssenceDescriptor
S 060e2b34025301010d01010101012800 CDCIEssenceDescriptor
S 060e2b34025301010d01010101015100 MPEGVideoDescriptor
S 060e2b34025301010d01010101012900 RGBAEssenceDescriptor
S 060e2b34025301010d01010101014200 GenericSoundEssenceDescriptor
S 060e2b34025301010d01010101014300 GenericDataEssenceDescriptor
S 060e2b34025301010d01010101015b00 VBIDataDescriptor
S 060e2b34025301010d01010101015c00 ANCDataDescriptor
S 060e2b34025301010d01010101014400 MultipleDescriptor
S 060e2b34025301010d01010101014800 WaveAudioDescriptor
S 060e2b34025301010d01010101014700 AES3AudioDescriptor
S 060e2b34025301010d01040100000000 DMFramework
S 060e2b34025301010d01040000000000 DMSet
S 060e2b34025301010d01010101015900 SubDescriptor
S 060e2b34025301010d01010101016e00 AVCSubDescriptor
S 060e2b34025301010d01040104010100 TextBasedDMFramework
S 060e2b34025301010d01040104030100 TextBasedObject
S 060e2b34025301010d01040104020100 GenericStreamTextBasedSet
S 060e2b34025301010d01040104020200 UTF8TextBasedSet
S 060e2b34025301010d01040104020300 UTF16TextBasedSet
S 060e2b34025301010d01010101016a00 MCALabelSubDescriptor
S 060e2b34025301010d01010101016b00 AudioChannelLabelSubDescriptor
S 060e2b34025301010d01010101016c00 SoundfieldGroupLabelSubDescriptor
S 060e2b34025301010d01010101016d00 GroupOfSoundfieldGroupsLabelSubDescriptor
S 060e2b34025301010d01010101017400 VC2SubDescriptor
S 060e2b34025301010d01010101016400 DCTimedTextDescriptor
S 060e2b34025301010d01010101016500 DCTimedTextResourceSubDescriptor
S 060e2b34025301010d01010101015a00 JPEG2000SubDescriptor
S 060e2b34025301010d01010101016700 ContainerConstraintsSubDescriptor
S 060e2b34025301010d01010101018102 JPEGXSSubDescriptor
S 060e2b34025301010d0101010101810d RIFFChunkDefinitionSubDescriptor
S 060e2b34025301010d0101010101810e ADM_CHNASubDescriptor
S 060e2b34025301010d0101010101810f ADMChannelMapping
S 060e2b34025301010d01010101018110 RIFFChunkReferencesSubDescriptor
S 060e2b34025301010d01010101018111 ADMAudioMetadataSubDescriptor
S 060e2b34025301010d01010101018112 ADMSoundfieldGroupLabelSubDescriptor
S 060e2b34025301010d01010101018106 MGASoundEssenceDescriptor
S 060e2b34025301010d01010101018107 MGAAudioMetadataSubDescriptor
S 060e2b34025301010d01010101018109 SADMAudioMetadataSubDescriptor
S 060e2b34025301010d01010101018108 MGASoundfieldGroupLabelSubDescriptor
I 060e2b34010101010101150200000000 3c0a InterchangeObject.InstanceUID UUID
I 060e2b34010101020520070108000000 0102 InterchangeObject.GenerationUID UUID
I 060e2b34010101020702011002040000 3b02 Preface.LastModifiedDate TIMESTAMP
I 060e2b34010101020301020105000000 3b05 Preface.Version VERSIONTYPE
I 060e2b34010101020301020104000000 3b07 Preface.ObjectModelVersion UINT32
I 060e2b34010101040601010401080000 3b08 Preface.PrimaryPackage WEAKREF
I 060e2b34010101020601010406040000 3b06 Preface.Identifications STRONGREFARRAY
I 060e2b34010101020601010402010000 3b03 Preface.ContentStorage STRONGREF
I 060e2b34010101050102020300000000 3b09 Preface.OperationalPattern UL
I 060e2b34010101050102021002010000 3b0a Preface.EssenceContainers ULBATCH
I 060e2b34010101050102021002020000 3b0b Preface.DMSchemes ULBATCH
I 060e2b34010101020520070101000000 3c09 Identification.ThisGenerationUID UUID
I 060e2b34010101020520070102010000 3c01 Identification.CompanyName UTF16STRING
I 060e2b34010101020520070103010000 3c02 Identification.ProductName UTF16STRING
I 060e2b34010101020520070104000000 3c03 Identification.ProductVersion PRODUCTVERSION
I 060e2b34010101020520070105010000 3c04 Identification.VersionString UTF16STRING
I 060e2b34010101020520070107000000 3c05 Identification.ProductUID UUID
I 060e2b34010101020702011002030000 3c06 Identification.ModificationDate TIMESTAMP
I 060e2b3401010102052007010a000000 3c07 Identification.ToolkitVersion PRODUCTVERSION
I 060e2b34010101020520070106010000 3c08 Identification.Platform UTF16STRING
I 060e2b34010101020601010405010000 1901 ContentStorage.Packages STRONGREFBATCH
I 060e2b34010101020601010405020000 1902 ContentStorage.EssenceContainerData STRONGREFBATCH
I 060e2b34010101020601010601000000 2701 EssenceContainerData.LinkedPackageUID UMID
I 060e2b34010101040103040500000000 3f06 EssenceContainerData.IndexSID UINT32
I 060e2b34010101040103040400000000 3f07 EssenceContainerData.BodySID UINT32
I 060e2b34010101010101151000000000 4401 GenericPackage.PackageUID UMID
I 060e2b34010101010103030201000000 4402 GenericPackage.Name UTF16STRING
I 060e2b34010101020702011001030000 4405 GenericPackage.PackageCreationDate TIMESTAMP
I 060e2b34010101020702011002050000 4404 GenericPackage.PackageModifiedDate TIMESTAMP
I 060e2b34010101020601010406050000 4403 GenericPackage.Tracks STRONGREFARRAY
I 060e2b34010101010102010101000000 4001 NetworkLocator.URLString UTF16STRING
I 060e2b34010101020104010201000000 4101 TextLocator.LocatorName UTF16STRING
I 060e2b34010101020107010100000000 4801 GenericTrack.TrackID UINT32
I 060e2b34010101020104010300000000 4804 GenericTrack.TrackNumber UINT32
I 060e2b34010101020107010201000000 4802 GenericTrack.TrackName UTF16STRING
I 060e2b34010101020601010402040000 4803 GenericTrack.Sequence STRONGREF
I 060e2b34010101020530040500000000 4b01 Track.EditRate RATIONAL
I 060e2b34010101020702010301030000 4b02 Track.Origin POSITION
I 060e2b34010101020530040200000000 4901 EventTrack.EventEditRate RATIONAL
I 060e2b340101010507020103010b0000 4902 EventTrack.EventOrigin POSITION
I 060e2b34010101020407010000000000 0201 StructuralComponent.DataDefinition UL
I 060e2b34010101020702020101030000 0202 StructuralComponent.Duration LENGTH
I 060e2b34010101020601010406090000 1001 Sequence.StructuralComponents STRONGREFARRAY
I 060e2b34010101020404010102060000 1502 TimecodeComponent.RoundedTimecodeBase UINT16
I 060e2b34010101020702010301050000 1501 TimecodeComponent.StartTimecode POSITION
I 060e2b34010101010404010105000000 1503 TimecodeComponent.DropFrame BOOLEAN
I 060e2b34010101020702010301040000 1201 SourceClip.StartPosition POSITION
I 060e2b34010101020601010301000000 1101 SourceClip.SourcePackageID PACKAGEID
I 060e2b34010101020601010302000000 1102 SourceClip.SourceTrackID UINT32
I 060e2b34010101020702010303030000 0601 DMSegment.EventStartPosition POSITION
I 060e2b34010101020530040401000000 0602 DMSegment.EventComment UTF16STRING
I 060e2b34010101040107010500000000 6102 DMSegment.TrackIDs UINT32BATCH
I 060e2b340101010506010104020c0000 6101 DMSegment.DMFramework STRONGREF
I 060e2b34010101050107010600000000 6103 DMSourceClip.DMSourceClipTrackIDs UINT32BATCH
I 060e2b34010101020601010402030000 4701 SourcePackage.Descriptor STRONGREF
I 060e2b34010101020601010406030000 2f01 GenericDescriptor.Locators STRONGREFARRAY
I 060e2b34010101050601010305000000 3006 FileDescriptor.LinkedTrackID UINT32
I 060e2b34010101010406010100000000 3001 FileDescriptor.SampleRate RATIONAL
I 060e2b34010101010406010200000000 3002 FileDescriptor.ContainerDuration LENGTH
I 060e2b34010101020601010401020000 3004 FileDescriptor.EssenceContainer UL
I 060e2b34010101020601010401030000 3005 FileDescriptor.Codec UL
I 060e2b34010101050405011300000000 3215 GenericPictureEssenceDescriptor.SignalStandard UINT8
I 060e2b34010101010401030104000000 320c GenericPictureEssenceDescriptor.FrameLayout UINT8
I 060e2b34010101010401050202000000 3203 GenericPictureEssenceDescriptor.StoredWidth UINT32
I 060e2b34010101010401050201000000 3202 GenericPictureEssenceDescriptor.StoredHeight UINT32
I 060e2b34010101050401030208000000 3216 GenericPictureEssenceDescriptor.StoredF2Offset INT32
I 060e2b34010101010401050108000000 3205 GenericPictureEssenceDescriptor.SampledWidth UINT32
I 060e2b34010101010401050107000000 3204 GenericPictureEssenceDescriptor.SampledHeight UINT32
I 060e2b34010101010401050109000000 3206 GenericPictureEssenceDescriptor.SampledXOffset INT32
I 060e2b3401010101040105010a000000 3207 GenericPictureEssenceDescriptor.SampledYOffset INT32
I 060e2b3401010101040105010b000000 3208 GenericPictureEssenceDescriptor.DisplayHeight UINT32
I 060e2b3401010101040105010c000000 3209 GenericPictureEssenceDescriptor.DisplayWidth UINT32
I 060e2b3401010101040105010d000000 320a GenericPictureEssenceDescriptor.DisplayXOffset INT32
I 060e2b3401010101040105010e000000 320b GenericPictureEssenceDescriptor.DisplayYOffset INT32
I 060e2b34010101050401030207000000 3217 GenericPictureEssenceDescriptor.DisplayF2Offset INT32
I 060e2b34010101010401010101000000 320e GenericPictureEssenceDescriptor.AspectRatio RATIONAL
I 060e2b34010101050401030209000000 3218 GenericPictureEssenceDescriptor.ActiveFormatDescriptor UINT8
I 060e2b34010101020401030205000000 320d GenericPictureEssenceDescriptor.VideoLineMap INT32ARRAY
I 060e2b34010101020520010200000000 320f GenericPictureEssenceDescriptor.AlphaTransparency UINT8
I 060e2b34010101020401020101010200 3210 GenericPictureEssenceDescriptor.CaptureGamma UL
I 060e2b34010101020418010100000000 3211 GenericPictureEssenceDescriptor.ImageAlignmentOffset UINT32
I 060e2b34010101020418010200000000 3213 GenericPictureEssenceDescriptor.ImageStartOffset UINT32
I 060e2b34010101020418010300000000 3214 GenericPictureEssenceDescriptor.ImageEndOffset UINT32
I 060e2b34010101020401030106000000 3212 GenericPictureEssenceDescriptor.FieldDominance UINT8
I 060e2b34010101020401060100000000 3201 GenericPictureEssenceDescriptor.PictureEssenceCoding UL
I 060e2b34010101020401020101030100 321a GenericPictureEssenceDescriptor.CodingEquations UL
I 060e2b34010101090401020101060100 3219 GenericPictureEssenceDescriptor.ColorPrimaries UL
I 060e2b340101010e0420040101010000 - GenericPictureEssenceDescriptor.MasteringDisplayPrimaries THREE_COLOR_PRIMARIES
I 060e2b340101010e0420040101020000 - GenericPictureEssenceDescriptor.MasteringDisplayWhitePointChromaticity COLOR_PRIMARY
I 060e2b340101010e0420040101030000 - GenericPictureEssenceDescriptor.MasteringDisplayMaximumLuminance UINT32
I 060e2b340101010e0420040101040000 - GenericPictureEssenceDescriptor.MasteringDisplayMinimumLuminance UINT32
I 060e2b3401010102040105030a000000 3301 CDCIEssenceDescriptor.ComponentDepth UINT32
I 060e2b34010101010401050105000000 3302 CDCIEssenceDescriptor.HorizontalSubsampling UINT32
I 060e2b34010101020401050110000000 3308 CDCIEssenceDescriptor.VerticalSubsampling UINT32
I 060e2b34010101010401050106000000 3303 CDCIEssenceDescriptor.ColorSiting UINT8
I 060e2b3401010105030102010a000000 330b CDCIEssenceDescriptor.ReversedByteOrder BOOLEAN
I 060e2b34010101020418010400000000 3307 CDCIEssenceDescriptor.PaddingBits INT16
I 060e2b34010101020401050307000000 3309 CDCIEssenceDescriptor.AlphaSampleDepth UINT32
I 060e2b34010101010401050303000000 3304 CDCIEssenceDescriptor.BlackRefLevel UINT32
I 060e2b34010101010401050304000000 3305 CDCIEssenceDescriptor.WhiteReflevel UINT32
I 060e2b34010101020401050305000000 3306 CDCIEssenceDescriptor.ColorRange UINT32
I 060e2b34010101050401060201020000 - MPEGVideoDescriptor.SingleSequence BOOLEAN
I 060e2b34010101050401060201030000 - MPEGVideoDescriptor.ConstantBFrames BOOLEAN
I 060e2b34010101050401060201040000 - MPEGVideoDescriptor.CodedContentType CODED_CONTENT_TYPE
I 060e2b34010101050401060201050000 - MPEGVideoDescriptor.LowDelay BOOLEAN
I 060e2b34010101050401060201060000 - MPEGVideoDescriptor.ClosedGOP BOOLEAN
I 060e2b34010101050401060201070000 - MPEGVideoDescriptor.IdenticalGOP BOOLEAN
I 060e2b34010101050401060201080000 - MPEGVideoDescriptor.MaxGOP UINT16
I 060e2b34010101050401060201090000 - MPEGVideoDescriptor.MaxBPictureCount UINT16
I 060e2b340101010504010602010b0000 - MPEGVideoDescriptor.BitRate UINT32
I 060e2b340101010504010602010a0000 - MPEGVideoDescriptor.ProfileAndLevel UINT8
I 060e2b3401010105040105030b000000 3406 RGBAEssenceDescriptor.ComponentMaxRef UINT32
I 060e2b3401010105040105030c000000 3407 RGBAEssenceDescriptor.ComponentMinRef UINT32
I 060e2b3401010105040105030d000000 3408 RGBAEssenceDescriptor.AlphaMaxRef UINT32
I 060e2b3401010105040105030e000000 3409 RGBAEssenceDescriptor.AlphaMinRef UINT32
I 060e2b34010101050401040401000000 3405 RGBAEssenceDescriptor.ScanningDirection ORIENTATION
I 060e2b34010101020401050306000000 3401 RGBAEssenceDescriptor.PixelLayout RGBALAYOUT
I 060e2b34010101020401050308000000 3403 RGBAEssenceDescriptor.Palette DATAVALUE
I 060e2b34010101020401050309000000 3404 RGBAEssenceDescriptor.PaletteLayout RGBALAYOUT
I 060e2b34010101050402030101010000 3d03 GenericSoundEssenceDescriptor.AudioSamplingRate RATIONAL
I 060e2b34010101040402030104000000 3d02 GenericSoundEssenceDescriptor.Locked BOOLEAN
I 060e2b34010101010402010103000000 3d04 GenericSoundEssenceDescriptor.AudioRefLevel INT8
I 060e2b34010101010402010101000000 3d05 GenericSoundEssenceDescriptor.ElectroSpatialFormulation UINT8
I 060e2b34010101050402010104000000 3d07 GenericSoundEssenceDescriptor.ChannelCount UINT32
I 060e2b34010101040402030304000000 3d01 GenericSoundEssenceDescriptor.QuantizationBits UINT32
I 060e2b34010101050402070100000000 3d0c GenericSoundEssenceDescriptor.DialNorm INT8
I 060e2b34010101020402040200000000 3d06 GenericSoundEssenceDescriptor.SoundEssenceCompression UL
I 060e2b340101010e0402010106000000 - GenericSoundEssenceDescriptor.ReferenceImageEditRate RATIONAL
I 060e2b340101010e0402010107000000 - GenericSoundEssenceDescriptor.ReferenceAudioAlignmentLevel INT8
I 060e2b34010101050403030200000000 3e01 GenericDataEssenceDescriptor.DataEssenceCoding UL
I 060e2b340101010406010104060b0000 3f01 MultipleDescriptor.SubDescriptorUIDs STRONGREFARRAY
I 060e2b34010101050402030201000000 3d0a WaveAudioDescriptor.BlockAlign UINT16
I 060e2b34010101050402030202000000 3d0b WaveAudioDescriptor.SequenceOffset UINT8
I 060e2b34010101050402030305000000 3d09 WaveAudioDescriptor.AvgBps UINT32
I 060e2b34010101070402010105000000 3d32 WaveAudioDescriptor.ChannelAssignment UL
I 060e2b34010101080402030106000000 3d29 WaveAudioDescriptor.PeakEnvelopeVersion UINT32
I 060e2b34010101080402030107000000 3d2a WaveAudioDescriptor.PeakEnvelopeFormat UINT32
I 060e2b34010101080402030108000000 3d2b WaveAudioDescriptor.PointsPerPeakValue UINT32
I 060e2b34010101080402030109000000 3d2c WaveAudioDescriptor.PeakEnvelopeBlockSize UINT32
I 060e2b3401010108040203010a000000 3d2d WaveAudioDescriptor.PeakChannels UINT32
I 060e2b3401010108040203010b000000 3d2e WaveAudioDescriptor.PeakFrames UINT32
I 060e2b3401010108040203010c000000 3d2f WaveAudioDescriptor.PeakOfPeaksPosition POSITION
I 060e2b3401010108040203010d000000 3d30 WaveAudioDescriptor.PeakEnvelopeTimestamp TIMESTAMP
I 060e2b3401010108040203010e000000 3d31 WaveAudioDescriptor.PeakEnvelopeData STREAM
I 060e2b34010101050402050106000000 3d0d AES3AudioDescriptor.Emphasis UINT8
I 060e2b34010101050402030203000000 3d0f AES3AudioDescriptor.BlockStartOffset UINT16
I 060e2b34010101050402050101000000 3d08 AES3AudioDescriptor.AuxiliaryBitsMode UINT8
I 060e2b34010101050402050102000000 3d10 AES3AudioDescriptor.ChannelStatusMode UINT8ARRAY
I 060e2b34010101050402050103000000 3d11 AES3AudioDescriptor.FixedChannelStatusData AES3_FIXED_DATA_ARRAY
I 060e2b34010101050402050104000000 3d12 AES3AudioDescriptor.UserDataMode UINT8ARRAY
I 060e2b34010101050402050105000000 3d13 AES3AudioDescriptor.FixedUserData AES3_FIXED_DATA_ARRAY
I 060e2b34010101050402050107000000 - AES3AudioDescriptor.LinkedTimecodeTrackID UINT32
I 060e2b34010101050402050108000000 - AES3AudioDescriptor.SMPTE377MDataStreamNumber UINT8
I 060e2b340101010e04010606010e0000 - AVCSubDescriptor.AVCDecodingDelay UINT8
I 060e2b340101010e0401060601030000 - AVCSubDescriptor.AVCConstantBPictureFlag BOOLEAN
I 060e2b340101010e0401060601040000 - AVCSubDescriptor.AVCCodedContentKind UINT8
I 060e2b340101010e0401060601060000 - AVCSubDescriptor.AVCClosedGOPIndicator BOOLEAN
I 060e2b340101010e0401060601070000 - AVCSubDescriptor.AVCIdenticalGOPIndicator BOOLEAN
I 060e2b340101010e0401060601080000 - AVCSubDescriptor.AVCMaximumGOPSize UINT16
I 060e2b340101010e0401060601090000 - AVCSubDescriptor.AVCMaximumBPictureCount UINT16
I 060e2b340101010e04010606010b0000 - AVCSubDescriptor.AVCMaximumBitrate UINT32
I 060e2b340101010e0401060601140000 - AVCSubDescriptor.AVCAverageBitrate UINT32
I 060e2b340101010e04010606010a0000 - AVCSubDescriptor.AVCProfile UINT8
I 060e2b340101010e04010606010c0000 - AVCSubDescriptor.AVCProfileConstraint UINT8
I 060e2b340101010e04010606010d0000 - AVCSubDescriptor.AVCLevel UINT8
I 060e2b340101010e04010606010f0000 - AVCSubDescriptor.AVCMaximumRefFrames UINT8
I 060e2b340101010e0401060601100000 - AVCSubDescriptor.AVCSequenceParameterSetFlag UINT8
I 060e2b340101010e0401060601110000 - AVCSubDescriptor.AVCPictureParameterSetFlag UINT8
I 060e2b340101010d0601010405410100 - TextBasedDMFramework.TextBasedObject STRONGREF
I 060e2b340101010d0406080600000000 - TextBasedObject.TextBasedMetadataPayloadSchemaID UL
I 060e2b340101010d0409020200000000 - TextBasedObject.TextMIMEMediaType UTF16STRING
I 060e2b340101010d0301010202140000 - TextBasedObject.RFC5646TextLanguageCode UTF16STRING
I 060e2b340101010d0302010603020000 - TextBasedObject.TextDataDescription UTF16STRING
I 060e2b340101010d0103040800000000 - GenericStreamTextBasedSet.GenericStreamSID UINT32
I 060e2b340101010d0301022003010000 - UTF8TextBasedSet.UTF8TextData RAW
I 060e2b340101010d0301022003020000 - UTF16TextBasedSet.UTF16TextData RAW
I 060e2b340101010e0103040a00000000 - MCALabelSubDescriptor.MCAChannelID UINT32
I 060e2b340101010e0103070101000000 - MCALabelSubDescriptor.MCALabelDictionaryID UL
I 060e2b340101010e0103070102000000 - MCALabelSubDescriptor.MCATagSymbol UTF16STRING
I 060e2b340101010e0103070103000000 - MCALabelSubDescriptor.MCATagName UTF16STRING
I 060e2b340101010e0103070105000000 - MCALabelSubDescriptor.MCALinkID UUID
I 060e2b340101010e0104010500000000 - MCALabelSubDescriptor.MCAPartitionKind UTF16STRING
I 060e2b340101010e0104010600000000 - MCALabelSubDescriptor.MCAPartitionNumber UTF16STRING
I 060e2b340101010e0105100000000000 - MCALabelSubDescriptor.MCATitle UTF16STRING
I 060e2b340101010e0105110000000000 - MCALabelSubDescriptor.MCATitleVersion UTF16STRING
I 060e2b340101010e0105120000000000 - MCALabelSubDescriptor.MCATitleSubVersion UTF16STRING
I 060e2b340101010e0105130000000000 - MCALabelSubDescriptor.MCAEpisode UTF16STRING
I 060e2b340101010d0301010203150000 - MCALabelSubDescriptor.RFC5646SpokenLanguage ISO7STRING
I 060e2b340101010e0302010220000000 - MCALabelSubDescriptor.MCAAudioContentKind UTF16STRING
I 060e2b340101010e0302010221000000 - MCALabelSubDescriptor.MCAAudioElementKind UTF16STRING
I 060e2b340101010e0302010222000000 - MCALabelSubDescriptor.MCAContent UTF16STRING
I 060e2b340101010e0302010223000000 - MCALabelSubDescriptor.MCAUseClass UTF16STRING
I 060e2b340101010e0302010224000000 - MCALabelSubDescriptor.MCAContentSubtype UTF16STRING
I 060e2b340101010e0302010225000000 - MCALabelSubDescriptor.MCAContentDifferentiator UTF16STRING
I 060e2b340101010e0302010226000000 - MCALabelSubDescriptor.MCASpokenLanguageAttribute ISO7STRING
I 060e2b340101010e0302010227000000 - MCALabelSubDescriptor.RFC5646AdditionalSpokenLanguages ISO7STRING
I 060e2b340101010e0302010228000000 - MCALabelSubDescriptor.MCAAdditionalLanguageAttributes ISO7STRING
I 060e2b340101010e0103070106000000 - AudioChannelLabelSubDescriptor.SoundfieldGroupLinkID UUID
I 060e2b340101010e0103070104000000 - SoundfieldGroupLabelSubDescriptor.GroupOfSoundfieldGroupsLinkID UUIDARRAY
I 060e2b340101010e0401060701000000 - VC2SubDescriptor.VC2MajorVersion UINT8
I 060e2b340101010e0401060702000000 - VC2SubDescriptor.VC2MinorVersion UINT8
I 060e2b340101010e0401060703000000 - VC2SubDescriptor.VC2Profile UINT8
I 060e2b340101010e0401060704000000 - VC2SubDescriptor.VC2Level UINT8
I 060e2b340101010e0401060705000000 - VC2SubDescriptor.VC2WaveletFilters UINT8BATCH
I 060e2b340101010e0401060706000000 - VC2SubDescriptor.VC2SequenceHeadersIdentical BOOLEAN
I 060e2b340101010e0401060707000000 - VC2SubDescriptor.VC2EditUnitsAreCompleteSequences BOOLEAN
I 060e2b340101010c0101151200000000 - DCTimedTextDescriptor.ResourceID UUID
I 060e2b34010101080102010501000000 - DCTimedTextDescriptor.NamespaceURI UTF16STRING
I 060e2b340101010e0301010202160000 - DCTimedTextDescriptor.RFC5646LanguageTagList UTF16STRING
I 060e2b340101010c0409050000000000 - DCTimedTextDescriptor.UCSEncoding UTF16STRING
I 060e2b340101010c0101151300000000 - DCTimedTextResourceSubDescriptor.AncillaryResourceID UUID
I 060e2b34010101070409020100000000 - DCTimedTextResourceSubDescriptor.MIMEType UTF16STRING
I 060e2b34010101040103040400000000 3f07 DCTimedTextResourceSubDescriptor.EssenceStreamID UINT32
I 060e2b340101010a0401060301000000 - JPEG2000SubDescriptor.Rsiz UINT16
I 060e2b340101010a0401060302000000 - JPEG2000SubDescriptor.Xsiz UINT32
I 060e2b340101010a0401060303000000 - JPEG2000SubDescriptor.Ysiz UINT32
I 060e2b340101010a0401060304000000 - JPEG2000SubDescriptor.XOsiz UINT32
I 060e2b340101010a0401060305000000 - JPEG2000SubDescriptor.YOsiz UINT32
I 060e2b340101010a0401060306000000 - JPEG2000SubDescriptor.XTsiz UINT32
I 060e2b340101010a0401060307000000 - JPEG2000SubDescriptor.YTsiz UINT32
I 060e2b340101010a0401060308000000 - JPEG2000SubDescriptor.XTOsiz UINT32
I 060e2b340101010a0401060309000000 - JPEG2000SubDescriptor.YTOsiz UINT32
I 060e2b340101010a040106030a000000 - JPEG2000SubDescriptor.Csiz UINT16
I 060e2b340101010a040106030b000000 - JPEG2000SubDescriptor.PictureComponentSizing J2K_COMPONENT_SIZING_ARRAY
I 060e2b340101010a040106030c000000 - JPEG2000SubDescriptor.CodingStyleDefault DATAVALUE
I 060e2b340101010a040106030d000000 - JPEG2000SubDescriptor.QuantizationDefault DATAVALUE
I 060e2b340101010e040106030e000000 - JPEG2000SubDescriptor.J2CLayout RGBALAYOUT
I 060e2b340101010e040106030f000000 - JPEG2000SubDescriptor.J2KExtendedCapabilities J2K_EXTENDED_CAPABILITIES
I 060e2b340101010e0401060310000000 - JPEG2000SubDescriptor.J2KProfile UINT16ARRAY
I 060e2b340101010e0401060311000000 - JPEG2000SubDescriptor.J2KCorrespondingProfile UINT16ARRAY
I 060e2b340101010e0401060b01000000 - JPEGXSSubDescriptor.JPEGXSPpih UINT16
I 060e2b340101010e0401060b02000000 - JPEGXSSubDescriptor.JPEGXSPlev UINT16
I 060e2b340101010e0401060b03000000 - JPEGXSSubDescriptor.JPEGXSWf UINT16
I 060e2b340101010e0401060b04000000 - JPEGXSSubDescriptor.JPEGXSHf UINT16
I 060e2b340101010e0401060b05000000 - JPEGXSSubDescriptor.JPEGXSNc UINT8
I 060e2b340101010e0401060b06000000 - JPEGXSSubDescriptor.JPEGXSComponentTable UINT8ARRAY
I 060e2b340101010e0401060b07000000 - JPEGXSSubDescriptor.JPEGXSCw UINT16
I 060e2b340101010e0401060b08000000 - JPEGXSSubDescriptor.JPEGXSHsl UINT16
I 060e2b340101010e0401060b09000000 - JPEGXSSubDescriptor.JPEGXSMaximumBitRate UINT32
I 060e2b340101010e0402030801000000 - RIFFChunkDefinitionSubDescriptor.RIFFChunkStreamID UINT32
I 060e2b340101010e0402030802000000 - RIFFChunkDefinitionSubDescriptor.RIFFChunkID RIFF_CHUNK_ID
I 060e2b340101010e0402030803000000 - RIFFChunkDefinitionSubDescriptor.RIFFChunkUUID UUID
I 060e2b340101010e0402030804000000 - RIFFChunkDefinitionSubDescriptor.RIFFChunkHashSHA1 DATAVALUE
I 060e2b340101010e0402030901000000 - ADM_CHNASubDescriptor.NumLocalChannels UINT16
I 060e2b340101010e0402030902000000 - ADM_CHNASubDescriptor.NumADMAudioTrackUIDs UINT16
I 060e2b340101010e0402030903000000 - ADM_CHNASubDescriptor.ADMChannelMappingsArray STRONGREFARRAY
I 060e2b340101010e0402030904000000 - ADMChannelMapping.LocalChannelID UINT32
I 060e2b340101010e0402030905000000 - ADMChannelMapping.ADMAudioTrackUID UTF16STRING
I 060e2b340101010e0402030906000000 - ADMChannelMapping.ADMAudioTrackChannelFormatID UTF16STRING
I 060e2b340101010e0402030907000000 - ADMChannelMapping.ADMAudioPackFormatID UTF16STRING
I 060e2b340101010e0402030806000000 - RIFFChunkReferencesSubDescriptor.RIFFChunkStreamIDsArray UINT32ARRAY
I 060e2b340101010e0402030a01000000 - ADMAudioMetadataSubDescriptor.RIFFChunkStreamID_link1 UINT32
I 060e2b340101010e0402030a02000000 - ADMAudioMetadataSubDescriptor.ADMProfileLevelULBatch ULBATCH
I 060e2b340101010e0402030b01000000 - ADMSoundfieldGroupLabelSubDescriptor.RIFFChunkStreamID_link2 UINT32
I 060e2b340101010e0402030b02000000 - ADMSoundfieldGroupLabelSubDescriptor.ADMAudioProgrammeID_ST2131 UTF16STRING
I 060e2b340101010e0402030b03000000 - ADMSoundfieldGroupLabelSubDescriptor.ADMAudioContentID_ST2131 UTF16STRING
I 060e2b340101010e0402030b04000000 - ADMSoundfieldGroupLabelSubDescriptor.ADMAudioObjectID_ST2131 UTF16STRING
I 060e2b340101010e0402030401000000 - MGASoundEssenceDescriptor.MGASoundEssenceBlockAlign UINT16
I 060e2b340101010e0402030402000000 - MGASoundEssenceDescriptor.MGASoundEssenceAverageBytesPerSecond UINT32
I 060e2b340101010e0402030403000000 - MGASoundEssenceDescriptor.MGASoundEssenceSequenceOffset UINT8
I 060e2b340101010e0402030501000000 - MGAAudioMetadataSubDescriptor.MGALinkID UUID
I 060e2b340101010e0402030502000000 - MGAAudioMetadataSubDescriptor.MGAAudioMetadataIndex UINT8
I 060e2b340101010e0402030503000000 - MGAAudioMetadataSubDescriptor.MGAAudioMetadataIdentifier UINT8
I 060e2b340101010e0402030504000000 - MGAAudioMetadataSubDescriptor.MGAAudioMetadataPayloadULArray ULARRAY
I 060e2b340101010e0402030701000000 - SADMAudioMetadataSubDescriptor.SADMMetadataSectionLinkID UUID
I 060e2b340101010e0402030702000000 - SADMAudioMetadataSubDescriptor.SADMProfileLevelULBatch ULBATCH
I 060e2b340101010e0402030601000000 - MGASoundfieldGroupLabelSubDescriptor.MGAMetadataSectionLinkID UUID
I 060e2b340101010e0402030602000000 - MGASoundfieldGroupLabelSubDescriptor.ADMAudioProgrammeID UTF16STRING
I 060e2b340101010e0402030603000000 - MGASoundfieldGroupLabelSubDescriptor.ADMAudioContentID UTF16STRING
I 060e2b340101010e0402030604000000 - MGASoundfieldGroupLabelSubDescriptor.ADMAudioObjectID UTF16STRING
S 060e2b34025301010d01010101010900 Filler
S 060e2b34025301010d01010101013f00 TaggedValue
S 060e2b34025301010d01010101010500 EssenceGroup
S 060e2b34025301010d01010101014900 PhysicalDescriptor
S 060e2b34025301010d01010101012e00 TapeDescriptor
S 060e2b34025301010d01010101014a00 ImportDescriptor
S 060e2b34025301010d01010101014b00 RecordingDescriptor
I 060e2b3401010102030201020c000000 4406 GenericPackage.UserComments STRONGREFARRAY
I 060e2b34010101020302010209010000 5001 TaggedValue.Name UTF16STRING
I 060e2b3401010102030201020a010000 5003 TaggedValue.Value INDIRECT
I 060e2b34010101020601010406010000 0501 EssenceGroup.Choices STRONGREFBATCH
I 060e2b34010101020601010402080000 0502 EssenceGroup.StillFrame STRONGREF
I 060e2b34010101090601010406100000 - GenericDescriptor.SubDescriptors STRONGREFARRAY
"""
_load_names()
