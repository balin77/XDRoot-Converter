"""Tests fuer xdroot_converter.mxfheader mit kleinen synthetischen MXF-Dateien (ohne Medien/Programme)."""

import struct
import tempfile
import unittest
from pathlib import Path

from xdroot_converter import mxfheader as mh
from xdroot_converter.mxfheader import HeaderPatchError

FILL_V1 = bytes.fromhex("060e2b34010101010301021001000000")
FILL_V2 = bytes.fromhex("060e2b34010101020301021001000000")
DD = {
    "timecode": bytes.fromhex("060e2b34040101010103020101000000"),
    "picture": bytes.fromhex("060e2b34040101010103020201000000"),
    "sound": bytes.fromhex("060e2b34040101010103020202000000"),
    "data": bytes.fromhex("060e2b34040101010103020203000000"),
    "dm": bytes.fromhex("060e2b34040101010103020110000000"),
}
FAKE_UL = bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0f00")     # nicht im Datenmodell
EC_LABEL = bytes.fromhex("060e2b34040101010d01030102060300")
EC_LABEL_OTHER = bytes.fromhex("060e2b34040101010d010301027f0100")
UMID_MP = bytes.fromhex("060a2b340101010501010d4313000000" + "41" + "11" * 15)
UMID_FP = bytes.fromhex("060a2b340101010501010d4313000000" + "42" + "11" * 15)


def u16s(text):
    return mh.utf16(text)


def u32(n):
    return struct.pack(">I", n)


def batch(uids):
    return struct.pack(">II", len(uids), 16) + b"".join(uids)


def klv(key, value, ber=4):
    return key + mh.enc_ber(len(value), ber) + value


class Builder:
    """Baut Header-Metadaten (Primer + Sets + Fill) aus Namen des Datenmodells."""

    def __init__(self, dyn_start=0xFFFF, fake_tags=None):
        self.n = 0
        self.sets = []          # (Setname, [(UL-Name | UL-Bytes, Wert)], UID)
        self.dyn = dyn_start
        self.tagmap = {}        # UL -> Tag
        self.fake_tags = fake_tags or {}
        self.tagmap[mh._norm(mh.item_ul("InterchangeObject.InstanceUID"))] = 0x3C0A

    def uid(self):
        self.n += 1
        return bytes([self.n]) * 16

    def add(self, setname, items, uid=None, key_version=1):
        uid = uid or self.uid()
        self.sets.append((setname, items, uid, key_version))
        return uid

    def tag(self, ul):
        ul = mh._norm(ul)
        if ul in self.tagmap:
            return self.tagmap[ul]
        if ul in self.fake_tags:
            t = self.fake_tags[ul]
        elif ul in mh._STATIC_TAG_OF_UL:
            t = mh._STATIC_TAG_OF_UL[ul]
        else:
            t = self.dyn
            self.dyn -= 1
        self.tagmap[ul] = t
        return t

    def build(self, total_len, fill_key=FILL_V1, fill_ber=4, primer_ber=4, set_ber=4):
        body = b""
        for name, items, uid, kv in self.sets:
            key = bytearray(mh._SET_KEYS[name])
            key[7] = kv
            raw = struct.pack(">HH", 0x3C0A, 16) + uid
            for ref, val in items:
                ul = mh.item_ul(ref) if isinstance(ref, str) else ref
                raw += struct.pack(">HH", self.tag(ul), len(val)) + val
            body += klv(bytes(key), raw, set_ber)
        primer_body = struct.pack(">II", len(self.tagmap), 18) + b"".join(
            struct.pack(">H", t) + bytes(ul[:7]) + b"\x01" + ul[8:] for ul, t in self.tagmap.items())
        primer = klv(bytes.fromhex("060e2b34020501010d01020101050100"), primer_body, primer_ber)
        meta = primer + body
        rest = total_len - len(meta)
        assert rest == 0 or rest >= 16 + fill_ber, rest
        if rest:
            meta += klv(fill_key, bytes(rest - 16 - fill_ber), fill_ber)
        assert len(meta) == total_len
        return meta


def make_card(tracks, *, ident, sound_extra=None, text=None, fake_tags=None, total=6000,
              fill_key=FILL_V1, dyn_start=0xFFFF, include_text_in_fp=False):
    """tracks: Liste (Art, TrackID, TrackNumber, Name|None).  Gibt (Builder, Liste der Track-IDs) zurueck."""
    b = Builder(dyn_start=dyn_start, fake_tags=fake_tags)
    ident_uid = b.add("Identification", ident)
    cs_uid = b.uid()
    mp_uid_set, fp_uid_set = b.uid(), b.uid()
    b.add("Preface", [("Preface.Identifications", batch([ident_uid])), ("Preface.ContentStorage", cs_uid),
                      ("Preface.Version", bytes([1, 3]))])
    b.add("ContentStorage", [("ContentStorage.Packages", batch([mp_uid_set, fp_uid_set]))], uid=cs_uid)
    fp_tracks, mp_tracks, descs = [], [], []
    for kind, tid, num, name in tracks:
        for pkg in ("mp", "fp"):
            seq, comp, tr = b.uid(), b.uid(), b.uid()
            items = [("GenericTrack.TrackID", u32(tid)), ("GenericTrack.TrackNumber", u32(num if pkg == "fp" else 0)),
                     ("GenericTrack.Sequence", seq)]
            if name is not None:
                items.insert(0, ("GenericTrack.TrackName", u16s(name)))
            b.add("Track" if kind != "dm" else "StaticTrack", items, uid=tr)
            b.add("Sequence", [("StructuralComponent.DataDefinition", DD[kind]),
                               ("Sequence.StructuralComponents", batch([comp]))], uid=seq)
            if kind == "timecode":
                b.add("TimecodeComponent", [("StructuralComponent.DataDefinition", DD[kind])], uid=comp)
            elif kind == "dm":
                b.add("DMSegment", [("StructuralComponent.DataDefinition", DD[kind])], uid=comp)
            else:
                if pkg == "mp":
                    ci = [("StructuralComponent.DataDefinition", DD[kind]), ("SourceClip.SourcePackageID", UMID_FP),
                          ("SourceClip.SourceTrackID", u32(tid))]
                else:
                    ci = [("StructuralComponent.DataDefinition", DD[kind]),
                          ("SourceClip.SourcePackageID", bytes(32)), ("SourceClip.SourceTrackID", u32(0))]
                b.add("SourceClip", ci, uid=comp)
            (mp_tracks if pkg == "mp" else fp_tracks).append(tr)
        if kind == "sound":
            d_items = [("FileDescriptor.LinkedTrackID", u32(tid)),
                       ("FileDescriptor.SampleRate", struct.pack(">ii", 48000, 1)),
                       ("FileDescriptor.ContainerDuration", struct.pack(">q", 100)),
                       ("FileDescriptor.EssenceContainer", EC_LABEL),
                       ("GenericSoundEssenceDescriptor.QuantizationBits", u32(24)),
                       ("GenericSoundEssenceDescriptor.ChannelCount", u32(1)),
                       ("WaveAudioDescriptor.BlockAlign", struct.pack(">H", 3)),
                       ("WaveAudioDescriptor.AvgBps", u32(144000))]
            d_items += (sound_extra or [])
            descs.append(b.add("AES3AudioDescriptor", d_items))
        elif kind == "picture":
            descs.append(b.add("CDCIEssenceDescriptor", [("FileDescriptor.LinkedTrackID", u32(tid))]))
    md = b.add("MultipleDescriptor", [("MultipleDescriptor.SubDescriptorUIDs", batch(descs))])
    b.add("MaterialPackage", [("GenericPackage.PackageUID", UMID_MP),
                              ("GenericPackage.Tracks", batch(mp_tracks))], uid=mp_uid_set)
    b.add("SourcePackage", [("GenericPackage.PackageUID", UMID_FP), ("GenericPackage.Tracks", batch(fp_tracks)),
                            ("SourcePackage.Descriptor", md)], uid=fp_uid_set)
    if text is not None:
        b.add("UTF8TextBasedSet", [("TextBasedObject.TextMIMEMediaType", u16s(text[0])),
                                   ("TextBasedObject.TextDataDescription", u16s(text[1])),
                                   ("UTF8TextBasedSet.UTF8TextData", b"<x/>")])
    return b


def partition_pack(kind, status, this, prev, footer, hbc, body_sid=0):
    key = bytes.fromhex("060e2b34020501010d01020101") + bytes([kind, status, 0])
    v = struct.pack(">HHI", 1, 3, 1) + struct.pack(">QQQQQ", this, prev, footer, hbc, 0)
    v += struct.pack(">IQI", 0, 0, body_sid) + bytes.fromhex("060e2b34040101010d01020101010900")
    v += struct.pack(">II", 1, 16) + EC_LABEL
    return klv(key, v, 4)


def fill_klv(n):
    """KLV-Fill mit n Bytes Gesamtlaenge (n >= 20) bzw. nichts (n = 0): KAG-Fuellung vor dem Primer."""
    return klv(FILL_V1, bytes(n - 20), 4) if n else b""


def build_file(meta, *, footer_meta=None, body=b"\xab" * 777, kag_fill=0, rip=True, open_header=False):
    """Header-Pack + [Fill] + Metadaten + Body (Dummy) + Footer-Pack (+ Metadaten) + RIP.
    kag_fill: Bytes KLV-Fill zwischen Partition Pack und Primer (KAG > 1).  open_header: nur ein
    offener Header (FooterPartition = 0, kein Footer, kein RIP) wie nach einem Stromausfall."""
    pf = fill_klv(kag_fill)
    if open_header:
        return partition_pack(2, 2, 0, 0, 0, len(meta)) + pf + meta + body
    hdr_len = len(partition_pack(2, 4, 0, 0, 0, len(meta)))
    footer_off = hdr_len + len(pf) + len(meta) + len(body)
    out = partition_pack(2, 4, 0, 0, footer_off, len(meta)) + pf + meta + body
    out += partition_pack(4, 4, footer_off, 0, footer_off, len(footer_meta) if footer_meta else 0)
    if footer_meta:
        out += pf + footer_meta
    if rip:
        rip_body = struct.pack(">IQ", 0, 0) + struct.pack(">IQ", 0, footer_off)
        rip_len = 16 + 1 + len(rip_body) + 4
        out += klv(bytes.fromhex("060e2b34020501010d01020101110100"), rip_body, 1) + struct.pack(">I", rip_len)
    return out


SONY_IDENT = [
    ("Identification.ThisGenerationUID", b"\x77" * 16),
    ("Identification.ModificationDate", bytes.fromhex("07e90601080a0000")),
    ("Identification.CompanyName", u16s("Sony")),
    ("Identification.ProductName", u16s("Mem ")),
    ("Identification.VersionString", u16s("2.00")),
    ("Identification.ProductUID", bytes.fromhex("cede1104828011de8a3908004678031c")),
]
BMX_IDENT = [
    ("Identification.ThisGenerationUID", b"\x66" * 16),
    ("Identification.ModificationDate", bytes.fromhex("07ea0b0f110000" + "00")),
    ("Identification.CompanyName", u16s("BBC")),
    ("Identification.ProductName", u16s("bmx")),
    ("Identification.VersionString", u16s("1.6.0 (scm v1.6)")),
    ("Identification.ProductUID", bytes.fromhex("b8604d312e154799a3c6047ed0e6f9a1")),
    ("Identification.ToolkitVersion", bytes.fromhex("00010006000000000000")),
    ("Identification.Platform", u16s("libMXF (Linux 64-bit)")),
]
SRC_TRACKS = [("timecode", 1, 0, None), ("picture", 2, 0x15010500, None),
              ("sound", 3, 0x16080300, None), ("sound", 4, 0x16080301, None), ("data", 5, 0x17010201, None)]
TGT_TRACKS = [("timecode", 901, 0, "TC1"), ("picture", 1001, 0x15010500, "V1"),
              ("sound", 2001, 0x16080300, "A1"), ("sound", 2002, 0x16080301, "A2"), ("data", 3001, 0x17010201, "D1")]


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = Path(self._td.name)
        self.addCleanup(self._td.cleanup)

    def write(self, name, meta, **kw):
        p = self.dir / name
        p.write_bytes(build_file(meta, **kw))
        return p

    def pair(self, src_builder, tgt_builder, tgt_total=6000, src_total=6000, **kw):
        src = self.write("src.mxf", src_builder.build(src_total))
        tgt = self.write("tgt.mxf", tgt_builder.build(tgt_total, **kw))
        return src, tgt

    def std(self, **kw):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT, **kw)
        return s, t


def structure(path):
    hm = mh.read_header(path)
    return hm, mh.resolve(hm)


class ParseTests(Base):
    def test_partitions_and_roundtrip(self):
        s, t = self.std()
        meta = t.build(6000)
        p = self.write("a.mxf", meta)
        parts = mh.find_partitions(p)
        self.assertEqual([x.kind for x in parts], ["header", "footer"])
        self.assertEqual(parts[0].header_byte_count, 6000)
        hm = mh.read_header(p)
        self.assertEqual(hm.to_bytes(), meta)
        self.assertEqual(hm.to_bytes(6000), meta)
        self.assertEqual(len(hm.find_sets("Track")), 2 * 4 + 2)      # 4 Tracks + Daten je Package

    def test_roundtrip_fill_version_2_and_ber(self):
        s, t = self.std()
        for key in (FILL_V1, FILL_V2):
            meta = t.build(6000, fill_key=key, fill_ber=3, primer_ber=2, set_ber=2)
            hm = mh.parse_header_metadata(meta)
            self.assertEqual(hm.to_bytes(), meta)
            self.assertEqual(hm.to_bytes(6000), meta)

    def test_fallback_without_rip(self):
        s, t = self.std()
        data = bytearray(build_file(t.build(6000)))
        rip_len = struct.unpack(">I", bytes(data[-4:]))[0]
        p = self.dir / "norip.mxf"
        p.write_bytes(bytes(data[:-rip_len]))
        self.assertEqual([x.kind for x in mh.find_partitions(p)], ["header", "footer"])

    def test_resolve_structure(self):
        s, t = self.std()
        st = mh.resolve(mh.parse_header_metadata(t.build(6000)))
        self.assertEqual([x.kind for x in st.file.tracks], ["timecode", "picture", "sound", "sound", "data"])
        self.assertEqual(len(st.file_descriptors()), 3)
        self.assertEqual(len(st.identifications), 1)

    def test_describe_is_stable_text(self):
        s, t = self.std()
        p = self.write("d.mxf", t.build(6000))
        text = mh.describe(p)
        self.assertIn("Identification.CompanyName = 'BBC'", text)
        self.assertIn("GenericTrack.TrackID = 2001", text)
        self.assertEqual(text, mh.describe(p))

    def test_garbage_rejected(self):
        p = self.dir / "x.mxf"
        p.write_bytes(b"\x00" * 500)
        with self.assertRaises(mh.MxfParseError):
            mh.read_header(p)


class AdoptTests(Base):
    def test_identification_and_tracks(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        before = tgt.read_bytes()
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("Identification.CompanyName") for m in msgs))
        hm, st = structure(tgt)
        ident = st.identifications[0]
        g = lambda f: hm.get(ident, "Identification." + f)
        self.assertEqual(g("CompanyName"), u16s("Sony"))
        self.assertEqual(g("ProductName"), u16s("Mem "))
        self.assertEqual(g("VersionString"), u16s("2.00"))
        self.assertEqual(g("ProductUID"), bytes.fromhex("cede1104828011de8a3908004678031c"))
        self.assertIsNone(g("ToolkitVersion"))
        self.assertIsNone(g("Platform"))
        self.assertEqual(g("ThisGenerationUID"), b"\x66" * 16)                    # Ziel behalten
        self.assertEqual(g("ModificationDate"), dict(BMX_IDENT)["Identification.ModificationDate"])
        for pkg in (st.material, st.file):
            self.assertEqual([x.track_id for x in pkg.tracks], [1, 2, 3, 4, 5])
            self.assertTrue(all(x.name is None for x in pkg.tracks))
            self.assertEqual([x.track_number for x in pkg.tracks][1:], [0x15010500 if pkg is st.file else 0] +
                             ([0x16080300, 0x16080301, 0x17010201] if pkg is st.file else [0, 0, 0]))
        # Verweise
        for tr in st.material.tracks:
            for c in tr.clips:
                self.assertEqual(hm.u32(c, "SourceClip.SourceTrackID"), tr.track_id)
        self.assertEqual(sorted(hm.u32(d, "FileDescriptor.LinkedTrackID") for d in st.file_descriptors()),
                         [2, 3, 4])
        self.assertEqual(len(before), len(tgt.read_bytes()))

    def test_partition_pack_and_tail_unchanged(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        before = tgt.read_bytes()
        mh.adopt_from_source(tgt, src)
        after = tgt.read_bytes()
        p = mh.find_partitions(tgt)[0]
        end = p.meta_offset + p.header_byte_count
        self.assertEqual(len(before), len(after))
        self.assertEqual(before[:p.meta_offset], after[:p.meta_offset])
        self.assertEqual(before[end:], after[end:])
        self.assertNotEqual(before, after)
        # Fill fuellt exakt auf
        hm = mh.read_header(tgt)
        self.assertEqual(len(hm.to_bytes()), p.header_byte_count)

    def test_sound_descriptor_follows_source(self):
        extra_fake = [(FAKE_UL, b"\x05")]
        s = make_card(SRC_TRACKS, ident=SONY_IDENT, sound_extra=extra_fake)
        # Quelle ohne AvgBps, dafuer SequenceOffset + dynamisches Item
        s.sets = [(n, [i for i in items if i[0] != "WaveAudioDescriptor.AvgBps"] +
                   ([("WaveAudioDescriptor.SequenceOffset", b"\x00")] if n == "AES3AudioDescriptor" else []), u, k)
                  for n, items, u, k in s.sets]
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any("Sound-Deskriptor" in m for m in msgs))
        hm, st = structure(tgt)
        sd = [d for d in st.file_descriptors() if hm.get(d, "GenericSoundEssenceDescriptor.ChannelCount") is not None]
        self.assertEqual(len(sd), 2)
        for d in sd:
            self.assertIsNone(hm.get(d, "WaveAudioDescriptor.AvgBps"))
            self.assertEqual(hm.get(d, "WaveAudioDescriptor.SequenceOffset"), b"\x00")
            self.assertEqual(hm.get(d, FAKE_UL), b"\x05")                       # dynamischer Tag neu im Ziel-Primer
            self.assertEqual(hm.get(d, "FileDescriptor.ContainerDuration"), struct.pack(">q", 100))
        # Reihenfolge der Items wie in der Quelle (Quelle hat InstanceUID zuerst)
        shm, sst = structure(src)
        sdesc = [d for d in sst.file_descriptors() if shm.get(d, "GenericSoundEssenceDescriptor.ChannelCount") is not None][0]
        src_order = [shm.ul_of_tag(tg) for tg, _ in sdesc.items]
        tgt_order = [hm.ul_of_tag(tg) for tg, _ in sd[0].items]
        self.assertEqual(src_order, tgt_order)

    def test_dynamic_tag_reuses_existing_ul_in_target_primer(self):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT, sound_extra=[(FAKE_UL, b"\x05")], fake_tags={mh._norm(FAKE_UL): 0xFFF0})
        t = make_card(TGT_TRACKS, ident=BMX_IDENT, sound_extra=[(FAKE_UL, b"\x01")], fake_tags={mh._norm(FAKE_UL): 0xFFA0})
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual(hm.primer[0xFFA0][8:], FAKE_UL[8:])
        self.assertNotIn(0xFFF0, hm.primer)
        d = [x for x in st.file_descriptors() if hm.get(x, FAKE_UL) is not None][0]
        self.assertIsNotNone(d.get(0xFFA0))
        self.assertEqual(d.get(0xFFA0), b"\x05")

    def test_primer_extended_when_ul_missing(self):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT, sound_extra=[(FAKE_UL, b"\x05")], fake_tags={mh._norm(FAKE_UL): 0xFFF0})
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        n_before = len(mh.read_header(tgt).primer)
        mh.adopt_from_source(tgt, src)
        hm = mh.read_header(tgt)
        self.assertEqual(len(hm.primer), n_before + 1)
        self.assertIn(mh._norm(FAKE_UL), [mh._norm(u) for u in hm.primer.values()])
        new_tag = hm.tag_for_ul(FAKE_UL)
        self.assertGreaterEqual(new_tag, 0x8000)
        self.assertEqual(hm.primer[new_tag][7], 0x01)      # Versionsbyte wie im Quell-Primer (nicht 0 normiert)

    def test_sub_descriptor_refs_not_copied(self):
        sub = bytes([0xEE]) * 16
        s = make_card(SRC_TRACKS, ident=SONY_IDENT,
                      sound_extra=[("GenericDescriptor.SubDescriptors", batch([sub]))])
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any("nicht uebernommen" in m for m in msgs))
        hm, st = structure(tgt)
        for d in st.file_descriptors():
            self.assertIsNone(hm.get(d, "GenericDescriptor.SubDescriptors"))

    def test_essence_container_mismatch_warns(self):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        s.sets = [(n, [(r, EC_LABEL_OTHER if r == "FileDescriptor.EssenceContainer" else v) for r, v in items], u, k)
                  for n, items, u, k in s.sets]
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("WARNUNG:") and "EssenceContainer" in m for m in msgs))
        hm, st = structure(tgt)
        d = [x for x in st.file_descriptors() if hm.get(x, "FileDescriptor.EssenceContainer")][0]
        self.assertEqual(hm.get(d, "FileDescriptor.EssenceContainer"), EC_LABEL)       # Ziel behalten

    def test_track_number_mismatch_is_warning_only(self):
        s = make_card([(k, i, (n + 1 if k == "picture" else n), nm) for k, i, n, nm in SRC_TRACKS], ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("WARNUNG:") and "TrackNumber" in m for m in msgs))
        hm, st = structure(tgt)
        self.assertEqual(st.file.kind_tracks("picture")[0].track_number, 0x15010500)

    def test_track_names_copied_from_source(self):
        s = make_card([(k, i, n, "S%d" % i) for k, i, n, _ in SRC_TRACKS], ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual([x.name for x in st.file.tracks], [u16s("S%d" % i) for i in range(1, 6)])

    def test_track_order_follows_source(self):
        order = [SRC_TRACKS[1], SRC_TRACKS[2], SRC_TRACKS[3], SRC_TRACKS[0], SRC_TRACKS[4]]
        s = make_card(order, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual([x.track_id for x in st.file.tracks], [2, 3, 4, 1, 5])
        self.assertEqual([x.track_id for x in st.material.tracks], [2, 3, 4, 1, 5])

    def test_unmapped_xml_track_gets_next_id(self):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS + [("dm", 9001, 0, "X1")], ident=BMX_IDENT, text=("application/xml", "urn:x"))
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual([x.track_id for x in st.material.tracks][-1], 6)
        self.assertEqual(st.material.tracks[-1].kind, "dm")
        o = st.text_objects[0]
        self.assertEqual(hm.get(o, "TextBasedObject.TextMIMEMediaType"), u16s("text/xml"))
        self.assertEqual(hm.get(o, "TextBasedObject.TextDataDescription"), u16s(""))
        self.assertTrue(any(m.startswith("TextObject: MIME") for m in msgs))

    def test_text_object_copied_from_source(self):
        s = make_card(SRC_TRACKS + [("dm", 6, 0, None)], ident=SONY_IDENT, text=("application/x-sony", "desc"))
        t = make_card(TGT_TRACKS + [("dm", 9001, 0, "X1")], ident=BMX_IDENT, text=("application/xml", "urn:x"))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        o = st.text_objects[0]
        self.assertEqual(hm.get(o, "TextBasedObject.TextMIMEMediaType"), u16s("application/x-sony"))
        self.assertEqual(hm.get(o, "TextBasedObject.TextDataDescription"), u16s("desc"))
        self.assertEqual(st.material.tracks[-1].track_id, 6)

    def test_fallback_identification_without_source_ident(self):
        s = make_card(SRC_TRACKS, ident=SONY_IDENT)
        s.sets = [x for x in s.sets if x[0] != "Identification"]
        s.sets = [(n, [(r, batch([])) if r == "Preface.Identifications" else (r, v) for r, v in items], u, k)
                  for n, items, u, k in s.sets]
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        i = st.identifications[0]
        self.assertEqual(hm.get(i, "Identification.CompanyName"), u16s("Sony"))
        self.assertEqual(hm.get(i, "Identification.ProductName"), u16s("Mem "))
        self.assertIsNone(hm.get(i, "Identification.Platform"))

    def test_idempotent(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        once = tgt.read_bytes()
        mtime = tgt.stat().st_mtime_ns
        msgs = mh.adopt_from_source(tgt, src)
        self.assertEqual([m for m in msgs if not m.startswith("WARNUNG")], [])
        self.assertEqual(once, tgt.read_bytes())
        self.assertEqual(mtime, tgt.stat().st_mtime_ns)           # nicht erneut geschrieben

    def test_source_is_never_modified(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        sb = src.read_bytes()
        mh.adopt_from_source(tgt, src)
        mh.compare_with_source(tgt, src)
        self.assertEqual(sb, src.read_bytes())

    # ---- Identification: die ERSTE der Quelle ist maßgeblich
    def _two_idents(self, card):
        """Zweite Identification (Bearbeitungsprogramm) hinter die erste setzen; Preface listet beide."""
        ident2 = card.add("Identification", [
            ("Identification.ThisGenerationUID", b"\x55" * 16),
            ("Identification.ModificationDate", bytes.fromhex("07ea0b0f110000" + "00")),
            ("Identification.CompanyName", u16s("Adobe")),
            ("Identification.ProductName", u16s("Premiere")),
            ("Identification.VersionString", u16s("9.9")),
            ("Identification.ProductUID", bytes.fromhex("11" * 16)),
            ("Identification.ToolkitVersion", bytes.fromhex("00010006000000000000")),
        ])
        out = []
        for n, items, u, k in card.sets:
            if n == "Preface":
                first = dict(items)["Preface.Identifications"][8:24]
                items = [(r, batch([first, ident2]) if r == "Preface.Identifications" else v) for r, v in items]
            out.append((n, items, u, k))
        card.sets = out
        return ident2

    def test_first_identification_of_source_is_authoritative(self):
        s, t = self.std()
        self._two_idents(s)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        i = st.identifications[0]
        self.assertEqual(hm.get(i, "Identification.CompanyName"), u16s("Sony"))
        self.assertEqual(hm.get(i, "Identification.ProductName"), u16s("Mem "))
        self.assertIsNone(hm.get(i, "Identification.ToolkitVersion"))      # steht nur in der zweiten der Quelle
        self.assertTrue(any(m.startswith("Identification.CompanyName") for m in msgs))
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        # Gegenprobe: dieselbe Quelle ohne zweite Identification ergibt dasselbe Ziel
        s2, t2 = self.std()
        src2, tgt2 = self.pair(s2, t2)
        mh.adopt_from_source(tgt2, src2)
        self.assertEqual(tgt.read_bytes(), tgt2.read_bytes())

    # ---- GenerationUID der Sound-Deskriptoren bleibt die des Ziels
    def test_descriptor_generation_uid_not_copied_from_source(self):
        gen = "InterchangeObject.GenerationUID"
        src_gen, tgt_gen = b"\x55" * 16, b"\x66" * 16
        # Quelle hat GenerationUID (Verweis auf ihre Generation), Ziel keine -> Item entfaellt
        s, t = sound_pair(with_item(gen, src_gen), None)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual(len(sound_descs(hm, st)), 2)
        for d in sound_descs(hm, st):
            self.assertIsNone(hm.get(d, gen))
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        # Ziel hat eine eigene -> sie bleibt
        s, t = sound_pair(with_item(gen, src_gen), with_item(gen, tgt_gen))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertEqual(hm.get(d, gen), tgt_gen)
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        # Quelle ohne, Ziel mit -> bleibt ebenfalls (wie bei den anderen Keep-Items)
        s, t = sound_pair(None, with_item(gen, tgt_gen))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertEqual(hm.get(d, gen), tgt_gen)

    # ---- zusaetzlicher Timecode-Track der Quelle: keine ID-Kollision
    def test_extra_source_timecode_track_id_is_not_reused(self):
        src_tracks = SRC_TRACKS + [("timecode", 6, 0, None)]
        s = make_card(src_tracks, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS + [("dm", 9001, 0, "X1")], ident=BMX_IDENT, text=("application/xml", "urn:x"))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for pkg in (st.material, st.file):
            ids = [x.track_id for x in pkg.tracks]
            self.assertEqual(len(set(ids)), len(ids), ids)
            self.assertEqual([x.track_id for x in pkg.kind_tracks("timecode")], [1])
            self.assertEqual(pkg.kind_tracks("dm")[0].track_id, 7)      # 6 gehoert in der Quelle dem zweiten TC-Track
        self.assertEqual(mh.compare_with_source(tgt, src), [])


class CompareTests(Base):
    def test_compare_empty_after_adopt(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        diffs = mh.compare_with_source(tgt, src)
        self.assertTrue(diffs)
        self.assertTrue(all(d.startswith("Abweichung") for d in diffs))
        mh.adopt_from_source(tgt, src)
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_compare_does_not_write(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        before = tgt.read_bytes()
        mh.compare_with_source(tgt, src)
        self.assertEqual(before, tgt.read_bytes())

    def test_compare_reports_sound_count(self):
        s = make_card(SRC_TRACKS[:3] + SRC_TRACKS[4:], ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        d = mh.compare_with_source(tgt, src)
        self.assertTrue(any("Tonspuren" in x for x in d))


class FillAndErrorTests(Base):
    def test_not_enough_space_leaves_file_unchanged(self):
        s, t = self.std()
        hm = mh.parse_header_metadata(t.build(10 ** 5))
        used = len(hm.to_bytes()) - (16 + 4 + len(hm.elements[-1].value))     # Groesse ohne Fill
        tgt = self.write("tgt.mxf", t.build(used + 16 + 4 + 3))               # nur 3 Bytes Fill-Inhalt
        # Quelle mit langem Platform-String und grossem Deskriptor-Item: braucht mehr Platz
        s2 = make_card(SRC_TRACKS, ident=SONY_IDENT + [("Identification.Platform", u16s("x" * 400))],
                       sound_extra=[(FAKE_UL, b"\x05" * 300)])
        src = self.write("src.mxf", s2.build(9000))
        before = tgt.read_bytes()
        with self.assertRaises(HeaderPatchError) as cm:
            mh.adopt_from_source(tgt, src)
        self.assertIn("passen nicht", str(cm.exception))
        self.assertEqual(before, tgt.read_bytes())

    def test_fill_remainder_too_small_for_fill_klv(self):
        s, t = self.std()
        hm = mh.parse_header_metadata(t.build(6000))
        used = len(hm.to_bytes()) - (16 + 4 + len(hm.elements[-1].value))
        # Rest 1..16 Bytes laesst sich nicht als KLV-Fill darstellen (Key 16 + mind. 1 Byte Laenge)
        for rest in (1, 10, 16):
            with self.assertRaises(HeaderPatchError):
                hm.to_bytes(used + rest)
        self.assertEqual(len(hm.to_bytes(used + 17)), used + 17)     # Fill mit 1-Byte-BER
        self.assertEqual(len(hm.to_bytes(used + 20)), used + 20)
        self.assertEqual(len(hm.to_bytes(used)), used)               # ganz ohne Fill

    def test_fill_keeps_key_version_and_ber(self):
        s, t = self.std()
        meta = t.build(6000, fill_key=FILL_V2, fill_ber=4)
        hm = mh.parse_header_metadata(meta)
        hm.sets[0].items.append((0x3C0B, b"\x01" * 10))      # unbekanntes Item, wird durchgereicht
        out = hm.to_bytes(6000)
        self.assertEqual(len(out), 6000)
        chk = mh.parse_header_metadata(out)
        fill = chk.elements[-1]
        self.assertEqual(fill.key, FILL_V2)
        self.assertEqual(fill.ber_size, 4)
        self.assertEqual(chk.sets[0].get(0x3C0B), b"\x01" * 10)

    def test_unequal_sound_count_raises_and_keeps_file(self):
        s = make_card(SRC_TRACKS[:3] + SRC_TRACKS[4:], ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        before = tgt.read_bytes()
        with self.assertRaises(HeaderPatchError):
            mh.adopt_from_source(tgt, src)
        self.assertEqual(before, tgt.read_bytes())

    def test_footer_with_metadata_is_patched_too(self):
        s, t = self.std()
        meta = t.build(6000)
        src = self.write("src.mxf", s.build(6000))
        tgt = self.write("tgt.mxf", meta, footer_meta=meta)
        self.assertEqual(len([p for p in mh.find_partitions(tgt) if p.header_byte_count]), 2)
        mh.adopt_from_source(tgt, src)
        parts = mh.find_partitions(tgt)
        raw = tgt.read_bytes()
        regions = [raw[p.meta_offset:p.meta_offset + p.header_byte_count] for p in parts if p.header_byte_count]
        self.assertEqual(regions[0], regions[1])
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        # read_header liest die (vollstaendige) Footer-Partition
        hm = mh.read_header(tgt)
        self.assertEqual(hm.to_bytes(), regions[1])

    def test_footer_failure_leaves_everything_unchanged(self):
        s, t = self.std()
        src = self.write("src.mxf", s.build(6000))
        small = t.build(6000)
        # Footer-Metadaten ohne Platzreserve fuer die Aenderungen
        hm = mh.parse_header_metadata(t.build(6000))
        used = len(hm.to_bytes()) - (16 + 4 + len(hm.elements[-1].value))
        tight = t.build(used + 20)
        tgt = self.write("tgt.mxf", small, footer_meta=tight)
        s2 = make_card(SRC_TRACKS, ident=SONY_IDENT + [("Identification.Platform", u16s("y" * 300))])
        src = self.write("src2.mxf", s2.build(9000))
        before = tgt.read_bytes()
        with self.assertRaises(HeaderPatchError):
            mh.adopt_from_source(tgt, src)
        self.assertEqual(before, tgt.read_bytes())

    def test_source_opened_read_only(self):
        s, t = self.std()
        src, tgt = self.pair(s, t)
        src.chmod(0o444)
        try:
            mh.adopt_from_source(tgt, src)
        finally:
            src.chmod(0o644)


FAKE_UL2 = bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0e00")
EC_LABEL_V2 = EC_LABEL[:7] + b"\x02" + EC_LABEL[8:]       # nur das Registry-Versionsbyte anders


def sound_descs(hm, st):
    return [d for d in st.file_descriptors() if hm.get(d, "GenericSoundEssenceDescriptor.ChannelCount") is not None]


def edit_sound(card, fn, cls=None):
    """Items aller Sound-Deskriptoren einer Builder-Karte aendern: fn(items, linked_track_id) -> items;
    cls: Klassenname der Deskriptoren aendern."""
    out = []
    for n, items, u, k in card.sets:
        if n == "AES3AudioDescriptor":
            lt = struct.unpack(">I", dict(items)["FileDescriptor.LinkedTrackID"])[0]
            items = fn(list(items), lt)
            n = cls or n
        out.append((n, items, u, k))
    card.sets = out
    return card


def with_item(name, value):
    return lambda items, lt: [i for i in items if i[0] != name] + ([(name, value)] if value is not None else [])


def sound_pair(src_fn=None, tgt_fn=None, src_cls=None, tgt_cls=None):
    s = make_card(SRC_TRACKS, ident=SONY_IDENT)
    t = make_card(TGT_TRACKS, ident=BMX_IDENT)
    if src_fn or src_cls:
        edit_sound(s, src_fn or (lambda i, lt: i), src_cls)
    if tgt_fn or tgt_cls:
        edit_sound(t, tgt_fn or (lambda i, lt: i), tgt_cls)
    return s, t


class KagFillTests(Base):
    """KAG > 1: KLV-Fill zwischen Partition Pack und Primer; HeaderByteCount zaehlt ab dem Primer."""

    def test_pre_fill_is_skipped_when_parsing(self):
        s, t = self.std()
        meta = t.build(6000)
        for fill in (0, 20, 21, 340, 1000):
            p = self.write("k%d.mxf" % fill, meta, kag_fill=fill)
            part = mh.find_partitions(p)[0]
            self.assertEqual(part.pre_fill, fill)
            self.assertEqual(part.meta_offset, part.pack_end + fill)
            self.assertEqual(mh.read_header(p).to_bytes(), meta)

    def test_adopt_with_kag_fill_keeps_bytes_outside_header_metadata(self):
        s, t = self.std()
        for src_fill, tgt_fill in ((0, 340), (340, 0), (340, 508), (0, 0)):
            src = self.write("src.mxf", s.build(6000), kag_fill=src_fill)
            tgt = self.write("tgt.mxf", t.build(6000), kag_fill=tgt_fill, footer_meta=t.build(6000))
            before = tgt.read_bytes()
            msgs = mh.adopt_from_source(tgt, src)
            self.assertTrue(msgs)
            after = tgt.read_bytes()
            self.assertEqual(len(before), len(after))
            diff = [i for i in range(len(before)) if before[i] != after[i]]
            regions = [(p.meta_offset, p.meta_offset + p.header_byte_count)
                       for p in mh.find_partitions(tgt) if p.header_byte_count]
            self.assertEqual(len(regions), 2)
            self.assertTrue(diff)
            self.assertTrue(all(any(a <= i < b for a, b in regions) for i in diff))
            # der Fill vor dem Primer ist byte-genau unveraendert
            p0 = mh.find_partitions(tgt)[0]
            self.assertEqual(before[p0.pack_end:p0.meta_offset], after[p0.pack_end:p0.meta_offset])
            self.assertEqual(mh.compare_with_source(tgt, src), [])
            self.assertIn("Partition 0", mh.describe(tgt))

    def test_kag_fill_not_counted_in_header_byte_count(self):
        # Ohne Beruecksichtigung des Fills waere der Bereich um pre_fill Bytes verschoben
        s, t = self.std()
        p = self.write("a.mxf", t.build(6000), kag_fill=340)
        part = mh.find_partitions(p)[0]
        raw = p.read_bytes()
        self.assertEqual(raw[part.meta_offset:part.meta_offset + 16], bytes.fromhex("060e2b34020501010d01020101050100"))


class SoundRuleTests(Base):
    def conflict(self, src_fn, tgt_fn, *needles):
        s, t = sound_pair(src_fn, tgt_fn)
        src, tgt = self.pair(s, t)
        before = tgt.read_bytes()
        with self.assertRaises(HeaderPatchError) as cm:
            mh.adopt_from_source(tgt, src)
        for n in needles:
            self.assertIn(n, str(cm.exception))
        self.assertEqual(before, tgt.read_bytes())
        d = mh.compare_with_source(tgt, src)
        self.assertTrue(d and d[0].startswith("Abweichung"))
        return str(cm.exception)

    def test_essence_conflicts_raise_and_keep_file(self):
        self.conflict(with_item("GenericSoundEssenceDescriptor.QuantizationBits", u32(16)), None,
                      "Tonspur 1", "QuantizationBits", "16", "24")
        self.conflict(with_item("WaveAudioDescriptor.BlockAlign", struct.pack(">H", 2)), None, "BlockAlign")
        self.conflict(with_item("GenericSoundEssenceDescriptor.ChannelCount", u32(2)), None, "ChannelCount")
        self.conflict(with_item("GenericSoundEssenceDescriptor.AudioSamplingRate", struct.pack(">ii", 48000, 1)),
                      with_item("GenericSoundEssenceDescriptor.AudioSamplingRate", struct.pack(">ii", 44100, 1)),
                      "AudioSamplingRate", "48000/1", "44100/1")

    def test_avgbps_difference_adopts_source_with_hint(self):
        ab = "WaveAudioDescriptor.AvgBps"
        s, t = sound_pair(with_item(ab, u32(96000)), None)
        src, tgt = self.pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any("HINWEIS: Tonspur 1: AvgBps wie Quelle (96000 statt 144000)" in l for l in log), log)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, ab) for d in sound_descs(hm, st)}, {u32(96000)})
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_all_conflicts_listed_for_all_tracks(self):
        msg = self.conflict(with_item("GenericSoundEssenceDescriptor.QuantizationBits", u32(16)), None)
        self.assertIn("Tonspur 1", msg)
        self.assertIn("Tonspur 2", msg)

    def test_sampling_rate_other_representation_is_equal(self):
        s, t = sound_pair(with_item("GenericSoundEssenceDescriptor.AudioSamplingRate", struct.pack(">ii", 96000, 2)),
                          with_item("GenericSoundEssenceDescriptor.AudioSamplingRate", struct.pack(">ii", 48000, 1)))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)

    def test_sequence_offset_rules(self):
        so = "WaveAudioDescriptor.SequenceOffset"
        # Quelle 0, Ziel fehlt -> gleich
        s, t = sound_pair(with_item(so, b"\x00"), None)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, so) for d in sound_descs(hm, st)}, {b"\x00"})
        # Quelle 2, Ziel fehlt -> Fehler mit --seq-off-Hinweis
        msg = self.conflict(with_item(so, b"\x02"), None, "SequenceOffset", "--seq-off 2")
        self.assertIn("fehlt", msg)
        # Quelle 2, Ziel 0 -> Fehler;  Quelle 0, Ziel 1 -> Fehler;  Quelle 1, Ziel 1 -> ok
        self.conflict(with_item(so, b"\x02"), with_item(so, b"\x00"), "SequenceOffset")
        self.conflict(with_item(so, b"\x00"), with_item(so, b"\x01"), "SequenceOffset")
        s, t = sound_pair(with_item(so, b"\x01"), with_item(so, b"\x01"))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        # nur das Ziel hat den Wert (Quelle ohne): Strukturregel "wie Quelle" -> entfernt
        s, t = sound_pair(None, with_item(so, b"\x01"))
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, so) for d in sound_descs(hm, st)}, {None})

    def test_one_sided_item_follows_source(self):
        rate = struct.pack(">ii", 48000, 1)
        s, t = sound_pair(with_item("GenericSoundEssenceDescriptor.AudioSamplingRate", rate), None)
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, "GenericSoundEssenceDescriptor.AudioSamplingRate") for d in sound_descs(hm, st)}, {rate})

    def test_class_taken_from_source_only_with_same_essence_container(self):
        # gleiches Label (Versionsbyte darf abweichen): Klasse der Quelle
        s, t = sound_pair(with_item("FileDescriptor.EssenceContainer", EC_LABEL_V2), None, src_cls="WaveAudioDescriptor")
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.name(d) for d in sound_descs(hm, st)}, {"WaveAudioDescriptor"})
        self.assertFalse(any(m.startswith("WARNUNG:") for m in msgs))
        self.assertEqual({hm.get(d, "FileDescriptor.EssenceContainer") for d in sound_descs(hm, st)}, {EC_LABEL})
        # anderes Label: Ziel-Klasse bleibt, Warnung
        s, t = sound_pair(with_item("FileDescriptor.EssenceContainer", EC_LABEL_OTHER), None, src_cls="WaveAudioDescriptor")
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.name(d) for d in sound_descs(hm, st)}, {"AES3AudioDescriptor"})
        self.assertTrue(any(m.startswith("WARNUNG:") and "Klasse" in m and "EssenceContainer" in m for m in msgs))
        self.assertEqual(mh.compare_with_source(tgt, src), [])        # Warnung ist keine Abweichung

    def test_class_specific_items_not_copied_into_other_class(self):
        emph = "AES3AudioDescriptor.Emphasis"
        s, t = sound_pair(lambda items, lt: items + [(emph, b"\x01")] and with_item("FileDescriptor.EssenceContainer", EC_LABEL_OTHER)(
            items + [(emph, b"\x01")], lt), None, tgt_cls="WaveAudioDescriptor")
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertEqual(hm.name(d), "WaveAudioDescriptor")
            self.assertIsNone(hm.get(d, emph))
        self.assertTrue(any(m.startswith("HINWEIS:") and "Emphasis" in m for m in msgs))

    def test_reference_like_items_are_not_copied(self):
        s, t = sound_pair()
        src_ident_uid = s.sets[0][2]
        other_uid = bytes([0x99]) * 16
        plain16 = bytes(range(16))              # sieht nur aus wie eine UID, ist keine Referenz
        extra_ref = (FAKE_UL, src_ident_uid)
        extra_batch = (FAKE_UL2, batch([src_ident_uid, s.sets[1][2]]))
        extra_plain = (bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0d00"), plain16)
        extra_other = (bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0c00"), other_uid)    # unbekannte UID -> kein Set
        edit_sound(s, lambda items, lt: items + [extra_ref, extra_batch, extra_plain, extra_other])
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertIsNone(hm.get(d, FAKE_UL))
            self.assertIsNone(hm.get(d, FAKE_UL2))
            self.assertEqual(hm.get(d, extra_plain[0]), plain16)
            self.assertEqual(hm.get(d, extra_other[0]), other_uid)
        note = [m for m in msgs if m.startswith("HINWEIS:") and "nicht uebernommen" in m]
        self.assertEqual(len(note), 2)
        self.assertEqual(mh.compare_with_source(tgt, src), [])        # bewusst nicht uebernommen != Abweichung

    def test_unknown_uid_batch_is_not_copied_but_single_value_is(self):
        s, t = sound_pair()
        dangling = batch([bytes([0x98]) * 16, bytes([0x97]) * 16])         # zeigt auf keine Set der Quelle
        plain16 = bytes(range(16))
        ul_batch = bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0b00")
        ul_single = bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0d00")
        ul_empty = bytes.fromhex("060e2b34010101ff0f0f0f0f0f0f0a00")
        edit_sound(s, lambda items, lt: items + [(ul_batch, dangling), (ul_single, plain16), (ul_empty, batch([]))])
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertIsNone(hm.get(d, ul_batch))
            self.assertEqual(hm.get(d, ul_single), plain16)
            self.assertEqual(hm.get(d, ul_empty), batch([]))
        hints = [m for m in msgs if m.startswith("HINWEIS:") and "16-Byte-Elementen" in m]
        self.assertEqual(len(hints), 2)                                     # je Tonspur
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_target_sub_descriptor_removed_when_source_has_none(self):
        s, t = sound_pair()
        sub = t.add("AVCSubDescriptor", [("AVCSubDescriptor.AVCProfile", b"\x7a")])
        edit_sound(t, lambda items, lt: items + [("GenericDescriptor.SubDescriptors", batch([sub]))])
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertFalse([x for x in hm.sets if hm.name(x) == "AVCSubDescriptor"])
        self.assertTrue(any("im Ziel entfernt" in m for m in msgs))


class PairingTests(Base):
    SRC = [("timecode", 1, 0, None), ("picture", 2, 0x15010500, None),
           ("sound", 4, 0x16080301, None), ("sound", 3, 0x16080300, None), ("data", 5, 0x17010201, None)]

    def test_sound_paired_by_track_number_not_by_position(self):
        s = make_card(self.SRC, ident=SONY_IDENT)
        # Deskriptoren unterscheidbar machen: Marker = Quell-Track-ID
        edit_sound(s, lambda items, lt: items + [(FAKE_UL, bytes([lt]))])
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertFalse(any(m.startswith("WARNUNG:") for m in msgs))
        hm, st = structure(tgt)
        by_num = {x.track_number: x.track_id for x in st.file.kind_tracks("sound")}
        self.assertEqual(by_num, {0x16080300: 3, 0x16080301: 4})
        # Reihenfolge wie Quelle (4 vor 3), aber die IDs gehoeren zur TrackNumber
        self.assertEqual([x.track_id for x in st.file.kind_tracks("sound")], [4, 3])
        self.assertEqual(sorted((x.track_id for x in st.material.kind_tracks("sound"))), [3, 4])
        for mt in st.material.kind_tracks("sound"):
            self.assertEqual(hm.u32(mt.clips[0], "SourceClip.SourceTrackID"), mt.track_id)
        # Deskriptor gehoert zum richtigen Track (Marker = ID des Quell-Tracks gleicher TrackNumber)
        for d in sound_descs(hm, st):
            self.assertEqual(hm.get(d, FAKE_UL), bytes([hm.u32(d, "FileDescriptor.LinkedTrackID")]))
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_material_tracks_paired_via_source_clips(self):
        # Ziel: Material-Package-Tracks in anderer Reihenfolge als die File-Package-Tracks
        s = make_card(self.SRC, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        rev = []
        for n, items, u, k in t.sets:
            if n == "MaterialPackage":
                tracks = mh.uid_list(dict(items)["GenericPackage.Tracks"])
                # 2001/2002 im MP vertauschen
                mp = [x for x in tracks]
                sound_idx = [i for i, x in enumerate(mp) if i in (2, 3)]
                mp[2], mp[3] = mp[3], mp[2]
                items = [(r, (batch(mp) if r == "GenericPackage.Tracks" else v)) for r, v in items]
            rev.append((n, items, u, k))
        t.sets = rev
        src, tgt = self.pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        fp_ids = {x.track_id: x.track_number for x in st.file.tracks}
        for mt in st.material.kind_tracks("sound"):
            # Material-Track und der File-Package-Track, auf den er zeigt, tragen dieselbe ID
            self.assertEqual(hm.u32(mt.clips[0], "SourceClip.SourceTrackID"), mt.track_id)
            self.assertIn(mt.track_id, fp_ids)
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_number_sets_mismatch_falls_back_to_position_with_warning(self):
        src_tracks = [(k, i, (0x16080311 if i == 4 else n), nm) for k, i, n, nm in self.SRC]
        s = make_card(src_tracks, ident=SONY_IDENT)
        t = make_card(TGT_TRACKS, ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("WARNUNG:") and "Position" in m and "sound" in m for m in msgs))
        hm, st = structure(tgt)
        self.assertEqual([x.track_id for x in st.file.kind_tracks("sound")], [4, 3])      # wie Quell-Position

    def test_zero_track_numbers_pair_by_position_silently(self):
        zero = [(k, i, 0, nm) for k, i, n, nm in SRC_TRACKS]
        s = make_card(zero, ident=SONY_IDENT)
        t = make_card([(k, i, 0, nm) for k, i, n, nm in TGT_TRACKS], ident=BMX_IDENT)
        src, tgt = self.pair(s, t)
        msgs = mh.adopt_from_source(tgt, src)
        self.assertFalse(any(m.startswith("WARNUNG:") for m in msgs))


class OpenHeaderTests(Base):
    def test_target_without_rip_and_open_header_is_refused(self):
        s, t = self.std()
        src = self.write("src.mxf", s.build(6000))
        tgt = self.write("tgt.mxf", t.build(6000), open_header=True)
        before = tgt.read_bytes()
        with self.assertRaises(HeaderPatchError) as cm:
            mh.adopt_from_source(tgt, src)
        self.assertIn("nicht vollstaendig auffindbar", str(cm.exception))
        self.assertEqual(before, tgt.read_bytes())

    def test_source_without_rip_and_open_header_can_be_read(self):
        s, t = self.std()
        src = self.write("src.mxf", s.build(6000), open_header=True)
        tgt = self.write("tgt.mxf", t.build(6000))
        self.assertEqual(mh.read_header(src).to_bytes(), s.build(6000))
        mh.adopt_from_source(tgt, src)
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_closed_file_without_rip_but_with_footer_is_still_patchable(self):
        s, t = self.std()
        src = self.write("src.mxf", s.build(6000))
        tgt = self.write("tgt.mxf", t.build(6000), rip=False)
        mh.adopt_from_source(tgt, src)
        self.assertEqual(mh.compare_with_source(tgt, src), [])


# ----------------------------------------------------------------------------- Sony-Angleichung (Ton/ANC/Bild)
SR = "FileDescriptor.SampleRate"
CD = "FileDescriptor.ContainerDuration"
PIC = "GenericPictureEssenceDescriptor."
CODING_A = bytes.fromhex("060e2b34040101 0a04010202013231 04".replace(" ", ""))
CODING_B = bytes.fromhex("060e2b34040101 0d04010202013160 01".replace(" ", ""))


def rate(n, d=1):
    return struct.pack(">ii", n, d)


def length(n):
    return struct.pack(">q", n)


def edit_class(card, cls, fn, rename=None):
    """Items aller Sets einer Klasse einer Builder-Karte aendern: fn(items) -> items."""
    out = []
    for n, items, u, k in card.sets:
        if n == cls:
            items = fn(list(items))
            n = rename or n
        out.append((n, items, u, k))
    card.sets = out
    return card


def set_item(name, value):
    return lambda items: [i for i in items if i[0] != name] + ([(name, value)] if value is not None else [])


def add_anc(card, data_tid, duration=None):
    """ANC-Deskriptor in die Karte aufnehmen und im MultipleDescriptor eintragen."""
    items = [("FileDescriptor.LinkedTrackID", u32(data_tid)), (SR, rate(50)), ("FileDescriptor.EssenceContainer", EC_LABEL_OTHER)]
    if duration is not None:
        items.append((CD, length(duration)))
    uid = card.add("ANCDataDescriptor", items)
    for n, its, u, k in card.sets:
        if n == "MultipleDescriptor":
            old = dict(its)["MultipleDescriptor.SubDescriptorUIDs"]
            edit_class(card, "MultipleDescriptor", lambda i: [
                ("MultipleDescriptor.SubDescriptorUIDs", batch(mh.uid_list(old) + [uid]))])
            break
    return uid


def descriptors(hm, st, *names):
    return [d for d in st.file_descriptors() if hm.name(d) in names]


PIC_COMMON = [(PIC + "StoredWidth", u32(1920)), (PIC + "StoredHeight", u32(1088)), (PIC + "FrameLayout", b"\x00")]
SRC_PIC = PIC_COMMON + [
    (PIC + "SampledWidth", u32(1920)), (PIC + "SampledHeight", u32(1080)), (PIC + "SampledXOffset", u32(0)),
    (PIC + "SampledYOffset", u32(0)), (PIC + "DisplayWidth", u32(1920)), (PIC + "DisplayHeight", u32(1080)),
    (PIC + "VideoLineMap", bytes.fromhex("00000002000000040000002a00000000")),
    (PIC + "SignalStandard", b"\x04"), (PIC + "AspectRatio", rate(16, 9)),
    (PIC + "ImageAlignmentOffset", u32(0)), (PIC + "ImageEndOffset", u32(0)),
    ("CDCIEssenceDescriptor.ReversedByteOrder", b"\x00"), ("CDCIEssenceDescriptor.PaddingBits", struct.pack(">h", 0)),
    ("CDCIEssenceDescriptor.BlackRefLevel", u32(64)), ("CDCIEssenceDescriptor.WhiteReflevel", u32(940)),
    ("CDCIEssenceDescriptor.ColorRange", u32(897)),
    ("CDCIEssenceDescriptor.ComponentDepth", u32(10)), (PIC + "PictureEssenceCoding", CODING_A)]
TGT_PIC = PIC_COMMON + [
    (PIC + "SampledWidth", u32(1920)), (PIC + "SampledHeight", u32(1088)),
    (PIC + "VideoLineMap", bytes.fromhex("0000000200000004" + "0000000100000000")),
    ("CDCIEssenceDescriptor.ComponentDepth", u32(8)), (PIC + "PictureEssenceCoding", CODING_B)]


def pic_pair(src_items=SRC_PIC, tgt_items=TGT_PIC):
    s = make_card(SRC_TRACKS, ident=SONY_IDENT)
    t = make_card(TGT_TRACKS, ident=BMX_IDENT)
    edit_class(s, "CDCIEssenceDescriptor", lambda i: i + list(src_items), rename="MPEGVideoDescriptor")
    edit_class(t, "CDCIEssenceDescriptor", lambda i: i + list(tgt_items))
    return s, t


class SonyAdoptionTests(Base):
    def run_pair(self, s, t, total=9000):
        src, tgt = self.pair(s, t, tgt_total=total, src_total=total)
        return src, tgt

    def assert_idempotent(self, src, tgt):
        again = mh.adopt_from_source(tgt, src)
        self.assertEqual([m for m in again if not m.startswith(("WARNUNG:", "HINWEIS:"))], [])
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    # --- Ton
    def test_sound_sample_rate_from_source_and_duration_converted(self):
        s, t = sound_pair(None, None)
        edit_sound(t, lambda i, lt: [x for x in i if x[0] not in (SR, CD)] + [(SR, rate(50)), (CD, length(100))])
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any("Tonspur 1" in m and "SampleRate" in m for m in log), log)
        hm, st = structure(tgt)
        for d in sound_descs(hm, st):
            self.assertEqual(hm.get(d, SR), rate(48000))
            self.assertEqual(hm.get(d, CD), length(100 * 48000 // 50))
        self.assert_idempotent(src, tgt)

    def sound_cds(self, tgt):
        hm, st = structure(tgt)
        return [(hm.get(d, SR), hm.get(d, CD)) for d in sound_descs(hm, st)]

    def assert_hint(self, log, *words):
        self.assertTrue(any(m.startswith("HINWEIS:") and all(w in m for w in words) for m in log), log)

    def test_sound_duration_not_integer_is_omitted_with_hint(self):
        s, t = sound_pair(with_item(SR, rate(50)), None)       # Quelle 50/1, Ziel 48000/1, Dauer 100
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)                   # kein Abbruch
        self.assert_hint(log, "Tonspur 1", "ContainerDuration laesst sich nicht ganzzahlig", "weggelassen")
        self.assertEqual(set(self.sound_cds(tgt)), {(rate(50), None)})
        self.assert_idempotent(src, tgt)

    def test_sound_duration_ntsc_rate_exact_only_when_derivable(self):
        # Bild 60000/1001 -> Ton 48000/1: 800,8 Samples je Frame, ganzzahlig nur bei Vielfachen von 5 Frames
        for frames, want in ((10, 8008), (7, None)):
            s, t = sound_pair(None, with_item(SR, rate(60000, 1001)))
            edit_sound(t, with_item(CD, length(frames)))
            edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(SR, rate(60000, 1001)), (CD, length(frames))])
            src, tgt = self.run_pair(s, t)
            log = mh.adopt_from_source(tgt, src)
            self.assertEqual({c for _, c in self.sound_cds(tgt)}, {None if want is None else length(want)})
            if want is None:
                self.assert_hint(log, "ContainerDuration laesst sich nicht ganzzahlig")
            self.assert_idempotent(src, tgt)

    def test_sound_duration_not_convertible_but_derivable_from_picture(self):
        s, t = sound_pair(with_item(SR, rate(50)), None)       # 100 @ 48000 -> 50: nicht ganzzahlig
        edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(SR, rate(50)), (CD, length(10))])
        src, tgt = self.run_pair(s, t)
        mh.adopt_from_source(tgt, src)
        self.assertEqual(set(self.sound_cds(tgt)), {(rate(50), length(10))})
        self.assert_idempotent(src, tgt)

    def test_sound_broken_source_rate_leaves_target_unchanged(self):
        for bad in (rate(48000, 0), rate(0, 1), rate(-48000, 1), b"\x00\x00\x00\x01"):
            s, t = sound_pair(with_item(SR, bad), None)
            edit_sound(t, lambda i, lt: [x for x in i if x[0] not in (SR, CD)] + [(SR, rate(50)), (CD, length(100))])
            src, tgt = self.run_pair(s, t)
            log = mh.adopt_from_source(tgt, src)
            self.assert_hint(log, "Tonspur 1", "SampleRate der Quelle unbrauchbar - Ziel unveraendert")
            self.assertEqual(set(self.sound_cds(tgt)), {(rate(50), length(100))}, bad)
            self.assert_idempotent(src, tgt)

    def test_sound_target_rate_missing_or_broken_duration_not_left_in_old_unit(self):
        for bad in (None, rate(48000, 0), rate(-1, 1)):
            for pic, want in ((False, None), (True, length(10))):
                s, t = sound_pair(None, None)
                edit_sound(t, lambda i, lt: [x for x in i if x[0] not in (SR, CD)]
                           + ([(SR, bad)] if bad is not None else []) + [(CD, length(100))])
                if pic:
                    edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(SR, rate(48000)), (CD, length(10))])
                src, tgt = self.run_pair(s, t)
                log = mh.adopt_from_source(tgt, src)
                got = self.sound_cds(tgt)
                self.assertEqual({r for r, _ in got}, {rate(48000)})
                self.assertEqual({c for _, c in got}, {want}, (bad, pic))
                if not pic:
                    self.assert_hint(log, "ContainerDuration laesst sich nicht ganzzahlig")
                self.assert_idempotent(src, tgt)

    def test_sound_negative_duration_is_not_converted(self):
        s, t = sound_pair(None, None)
        edit_sound(t, lambda i, lt: [x for x in i if x[0] not in (SR, CD)] + [(SR, rate(50)), (CD, length(-5))])
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertEqual({c for _, c in self.sound_cds(tgt)}, {None})
        self.assert_hint(log, "ContainerDuration laesst sich nicht ganzzahlig")
        self.assert_idempotent(src, tgt)

    def test_sound_derive_conflicting_candidates_gives_nothing(self):
        s, t = sound_pair(None, with_item(CD, None))
        edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(SR, rate(50)), (CD, length(10))])
        edit_class(t, "Sequence", lambda i: i + [("StructuralComponent.Duration", length(11))])
        edit_class(t, "Track", lambda i: i + [("Track.EditRate", rate(50))])
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assert_hint(log, "Tonspur 1", "ContainerDuration", "nicht eindeutig")
        self.assertEqual({c for _, c in self.sound_cds(tgt)}, {None})
        self.assert_idempotent(src, tgt)

    def test_sound_duration_removed_when_source_has_none(self):
        s, t = sound_pair(with_item(CD, None), None)
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any("Tonspur 1" in m and "-ContainerDuration" in m for m in log), log)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, CD) for d in sound_descs(hm, st)}, {None})
        self.assert_idempotent(src, tgt)

    def test_sound_duration_kept_when_source_has_it_and_rate_unchanged(self):
        s, t = sound_pair(with_item(CD, length(555)), with_item(CD, length(777)))
        src, tgt = self.run_pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, CD) for d in sound_descs(hm, st)}, {length(777)})
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    def test_sound_duration_derived_from_picture_or_hint(self):
        # Ziel ohne ContainerDuration, Quelle mit: aus dem Ziel-Bild-Deskriptor (50/1, 10 Frames) abgeleitet
        s, t = sound_pair(None, with_item(CD, None))
        edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(SR, rate(50)), (CD, length(10))])
        src, tgt = self.run_pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, CD) for d in sound_descs(hm, st)}, {length(10 * 960)})
        self.assert_idempotent(src, tgt)
        # nicht ableitbar -> HINWEIS, weggelassen, keine Abweichung
        s, t = sound_pair(None, with_item(CD, None))
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("HINWEIS:") and "ContainerDuration" in m for m in log), log)
        hm, st = structure(tgt)
        self.assertEqual({hm.get(d, CD) for d in sound_descs(hm, st)}, {None})
        self.assertEqual(mh.compare_with_source(tgt, src), [])

    # --- ANC / MultipleDescriptor
    def test_data_and_multiple_descriptor_duration_follows_source(self):
        s, t = sound_pair()
        add_anc(s, 5)
        add_anc(t, 3001, duration=1632)
        edit_class(t, "MultipleDescriptor", lambda i: i + [(CD, length(1632))])
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("Datenspur 1: ContainerDuration") for m in log), log)
        self.assertTrue(any(m.startswith("MultipleDescriptor: ContainerDuration") for m in log), log)
        hm, st = structure(tgt)
        for d in descriptors(hm, st, "ANCDataDescriptor") + [st.file.descriptor]:
            self.assertIsNone(hm.get(d, CD))
        self.assertEqual(hm.get(descriptors(hm, st, "ANCDataDescriptor")[0], SR), rate(50))
        self.assert_idempotent(src, tgt)

    def test_data_duration_not_invented_and_kept_when_source_has(self):
        s, t = sound_pair()
        add_anc(s, 5, duration=40)
        add_anc(t, 3001)                                   # Ziel ohne
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("HINWEIS: Datenspur 1") for m in log), log)
        hm, st = structure(tgt)
        self.assertIsNone(hm.get(descriptors(hm, st, "ANCDataDescriptor")[0], CD))
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        s, t = sound_pair()
        add_anc(s, 5, duration=40)
        add_anc(t, 3001, duration=41)                      # beide haben: Zielwert bleibt
        src, tgt = self.run_pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        self.assertEqual(hm.get(descriptors(hm, st, "ANCDataDescriptor")[0], CD), length(41))

    # --- Bild
    def test_picture_items_adopted_but_codec_items_kept(self):
        s, t = pic_pair()
        src, tgt = self.run_pair(s, t)
        before_hm, before_st = structure(tgt)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("Bildspur 1: Bild-Deskriptor an Quelle angeglichen") for m in log), log)
        hm, st = structure(tgt)
        (d,) = descriptors(hm, st, "CDCIEssenceDescriptor")
        want = dict(SRC_PIC)
        for q in ("SampledHeight", "VideoLineMap", "ImageAlignmentOffset", "ImageEndOffset", "SampledXOffset",
                  "DisplayHeight", "SignalStandard", "AspectRatio"):
            self.assertEqual(hm.get(d, PIC + q), want[PIC + q], q)
        for q in ("ReversedByteOrder", "PaddingBits", "BlackRefLevel", "WhiteReflevel", "ColorRange"):
            self.assertEqual(hm.get(d, "CDCIEssenceDescriptor." + q), want["CDCIEssenceDescriptor." + q], q)
        self.assertEqual(hm.get(d, "CDCIEssenceDescriptor.ComponentDepth"), u32(8))      # codec-abhaengig: bleibt
        self.assertEqual(hm.get(d, PIC + "PictureEssenceCoding"), CODING_B)
        self.assertEqual(hm.name(d), "CDCIEssenceDescriptor")
        self.assert_idempotent(src, tgt)

    def test_picture_item_missing_in_source_stays_in_target(self):
        s, t = pic_pair(src_items=PIC_COMMON)
        edit_class(t, "CDCIEssenceDescriptor", lambda i: i + [(PIC + "ImageStartOffset", u32(7))])
        src, tgt = self.run_pair(s, t)
        mh.adopt_from_source(tgt, src)
        hm, st = structure(tgt)
        (d,) = descriptors(hm, st, "CDCIEssenceDescriptor")
        self.assertEqual(hm.get(d, PIC + "ImageStartOffset"), u32(7))

    def test_picture_precondition_violated_changes_nothing(self):
        for q, v in ((PIC + "StoredHeight", u32(1080)), (PIC + "StoredWidth", u32(1280)), (PIC + "FrameLayout", b"\x01")):
            s, t = pic_pair(tgt_items=[(k, (v if k == q else x)) for k, x in TGT_PIC])
            src, tgt = self.run_pair(s, t)
            hm0, st0 = structure(tgt)
            (d0,) = descriptors(hm0, st0, "CDCIEssenceDescriptor")
            items0 = [i for i in d0.items if i[0] != 0x3006]       # LinkedTrackID wird umnummeriert
            log = mh.adopt_from_source(tgt, src)
            self.assertTrue(any(m.startswith("HINWEIS: Bildspur 1") and q.split(".")[1] in m for m in log), (q, log))
            hm, st = structure(tgt)
            (d,) = descriptors(hm, st, "CDCIEssenceDescriptor")
            self.assertEqual([i for i in d.items if i[0] != 0x3006], items0)
            self.assertFalse([m for m in mh.compare_with_source(tgt, src) if "Bild" in m])

    def test_picture_cdci_items_only_for_cdci_or_mpeg_target_class(self):
        s, t = pic_pair(tgt_items=PIC_COMMON + [(PIC + "SampledHeight", u32(1088))])
        edit_class(t, "CDCIEssenceDescriptor", lambda i: i, rename="RGBAEssenceDescriptor")
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("HINWEIS: Bildspur 1") and "CDCI" in m for m in log), log)
        hm, st = structure(tgt)
        (d,) = descriptors(hm, st, "RGBAEssenceDescriptor")
        self.assertEqual(hm.get(d, PIC + "SampledHeight"), u32(1080))          # generische Items kommen
        for q in ("PaddingBits", "ReversedByteOrder", "BlackRefLevel", "WhiteReflevel", "ColorRange"):
            self.assertIsNone(hm.get(d, "CDCIEssenceDescriptor." + q), q)
        self.assert_idempotent(src, tgt)

    def test_picture_item_tag_collision_in_target_primer(self):
        name = "CDCIEssenceDescriptor.BlackRefLevel"
        ul = mh._norm(mh.item_ul(name))
        static = mh._STATIC_TAG_OF_UL[ul]
        for src_fake, tgt_fake in (({}, {mh._norm(FAKE_UL): static}), ({ul: 0x8123}, {}),
                                   ({ul: 0x8123}, {mh._norm(FAKE_UL): static})):
            s = make_card(SRC_TRACKS, ident=SONY_IDENT, fake_tags=src_fake)
            t = make_card(TGT_TRACKS, ident=BMX_IDENT, fake_tags=tgt_fake)
            edit_class(s, "CDCIEssenceDescriptor", lambda i: i + list(SRC_PIC), rename="MPEGVideoDescriptor")
            edit_class(t, "CDCIEssenceDescriptor", lambda i: i + list(TGT_PIC) + [(FAKE_UL, b"\x01\x02")])
            src, tgt = self.run_pair(s, t)
            mh.adopt_from_source(tgt, src)
            hm, st = structure(tgt)
            shm, sst = structure(src)
            (d,) = descriptors(hm, st, "CDCIEssenceDescriptor")
            self.assertEqual(hm.get(d, name), u32(64))
            tag = hm.tag_for_ul(ul)
            self.assertTrue(tag is not None and 0 < tag <= 0xFFFF)
            if tgt_fake:
                self.assertGreaterEqual(tag, 0x8000)
            self.assertEqual(mh._norm(hm.primer[tag]), ul)
            self.assertEqual(hm.raw_ul_of_tag(tag), shm.raw_ul_of_tag(shm.tag_for_ul(ul)))
            uls = [hm.ul_of_tag(tg) for tg, _ in d.items]
            self.assertEqual(len(uls), len(set(uls)), "UL doppelt im Deskriptor")
            self.assertEqual(len([1 for tg, _ in d.items if tg == tag]), 1)
            self.assertEqual(hm.get(d, FAKE_UL), b"\x01\x02")                 # fremdes Item bleibt
            self.assert_idempotent(src, tgt)

    def test_picture_source_without_stored_fields_hint_and_no_change(self):
        stored = {PIC + "StoredWidth", PIC + "StoredHeight", PIC + "FrameLayout"}
        s, t = pic_pair(src_items=[i for i in SRC_PIC if i[0] not in stored])
        src, tgt = self.run_pair(s, t)
        hm0, st0 = structure(tgt)
        (d0,) = descriptors(hm0, st0, "CDCIEssenceDescriptor")
        items0 = [i for i in d0.items if i[0] != 0x3006]
        log = mh.adopt_from_source(tgt, src)
        self.assertTrue(any(m.startswith("HINWEIS: Bildspur 1") and "StoredWidth/StoredHeight/FrameLayout" in m
                            for m in log), log)
        hm, st = structure(tgt)
        (d,) = descriptors(hm, st, "CDCIEssenceDescriptor")
        self.assertEqual([i for i in d.items if i[0] != 0x3006], items0)
        self.assert_idempotent(src, tgt)

    # --- Ziel ohne Datenspur (--no-anc)
    def test_target_without_data_track_source_with_anc(self):
        s, _ = sound_pair()
        add_anc(s, 5, duration=40)
        t = make_card([x for x in TGT_TRACKS if x[0] != "data"], ident=BMX_IDENT)
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertFalse([m for m in log if "Datenspur" in m or "ANC" in m or "MultipleDescriptor" in m], log)
        hm, st = structure(tgt)
        self.assertEqual(descriptors(hm, st, "ANCDataDescriptor"), [])
        self.assertEqual(st.file.kind_tracks("data"), [])
        self.assert_idempotent(src, tgt)

    # --- Preface.EssenceContainers
    def test_essence_containers_order_like_source(self):
        la = bytes.fromhex("060e2b34040101010d01030102106001")
        lb = bytes.fromhex("060e2b34040101010d01030102060300")
        lc = bytes.fromhex("060e2b34040101010d010301020e0000")
        ld = bytes.fromhex("060e2b34040101010d010301027f0100")
        lx = bytes.fromhex("060e2b34040101010d01030102ee0000")           # nur im Ziel
        s, t = sound_pair()
        for card, labels in ((s, [la, lb, lc, ld]), (t, [ld, lx, lc, lb[:7] + b"\x0a" + lb[8:], la])):
            for n, items, u, k in card.sets:
                if n == "Preface":
                    card.sets[card.sets.index((n, items, u, k))] = (
                        n, list(items) + [("Preface.EssenceContainers", batch(labels))], u, k)
        src, tgt = self.run_pair(s, t)
        log = mh.adopt_from_source(tgt, src)
        self.assertIn("Preface.EssenceContainers: Reihenfolge wie in der Quelle", log)
        hm, st = structure(tgt)
        got = mh.uid_list(hm.get(st.preface, "Preface.EssenceContainers"))
        self.assertEqual([mh._norm(x) for x in got], [mh._norm(x) for x in (la, lb, lc, ld, lx)])
        self.assertEqual(got[1], lb[:7] + b"\x0a" + lb[8:])               # Zielbytes (Versionsbyte) bleiben
        self.assert_idempotent(src, tgt)

    # --- alter Zustand
    def test_target_in_old_state_is_detected_and_pulled_up(self):
        """Ziel wie vor der Erweiterung (Ton 50/1 + ContainerDuration, ANC/Multiple mit Dauer, altes Bild)."""
        s, t = pic_pair()
        add_anc(s, 5)
        add_anc(t, 3001, duration=1632)
        edit_class(t, "MultipleDescriptor", lambda i: i + [(CD, length(1632))])
        edit_sound(s, lambda i, lt: [x for x in i if x[0] != CD])
        edit_sound(t, lambda i, lt: [x for x in i if x[0] not in (SR, CD)] + [(SR, rate(50)), (CD, length(1632))])
        src, tgt = self.run_pair(s, t)
        diff = mh.compare_with_source(tgt, src)
        self.assertTrue(any("SampleRate" in m for m in diff), diff)
        self.assertTrue(any("Bild-Deskriptor" in m for m in diff), diff)
        self.assertTrue(any("ContainerDuration" in m for m in diff), diff)
        mh.adopt_from_source(tgt, src)
        self.assertEqual(mh.compare_with_source(tgt, src), [])
        self.assertEqual(mh.adopt_from_source(tgt, src), [])


if __name__ == "__main__":
    unittest.main()
