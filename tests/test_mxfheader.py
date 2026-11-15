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


def build_file(meta, *, footer_meta=None, body=b"\xab" * 777):
    """Header-Pack + Metadaten + Body (Dummy) + Footer-Pack (+ Metadaten) + RIP."""
    hdr_len = len(partition_pack(2, 4, 0, 0, 0, len(meta)))
    footer_off = hdr_len + len(meta) + len(body)
    out = partition_pack(2, 4, 0, 0, footer_off, len(meta)) + meta + body
    out += partition_pack(4, 4, footer_off, 0, footer_off, len(footer_meta) if footer_meta else 0)
    if footer_meta:
        out += footer_meta
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
        ident = st.identifications[-1]
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
        i = st.identifications[-1]
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


if __name__ == "__main__":
    unittest.main()
