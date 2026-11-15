#!/usr/bin/env python3
"""Erzeugt eine synthetische Sony-XDROOT-Karte mit XAVC-I (AVC-Intra Class 100, 1080p50).

Aufruf:  python scripts/make_test_card.py [zielordner] [--clips 2] [--seconds 2]

Standardziel: _test_out/synth_card.  Es entsteht <zielordner>/XDROOT/ mit Clip/, Take/, Sub/,
Edit/, General/, Thmbnl/, UserData/, MEDIAPRO.XML, CUEUP.XML, DISCMETA.XML.

Alle Werte (UMIDs, System-ID, Medien-IDs, Daten) sind erfunden, es werden keine echten
Seriennummern verwendet.  Nur Python-Standardbibliothek; benoetigt ffmpeg und raw2bmx
(gefunden ueber xdroot_converter/tools.py bzw. PATH).
"""

from __future__ import annotations

import argparse
import hashlib
import math
import shutil
import struct
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    from xdroot_converter import tools as _tools  # noqa: E402
except Exception:  # pragma: no cover
    _tools = None
from xdroot_converter import mxfheader  # noqa: E402

FPS = 50
N_AUDIO = 8
SAMPLES_PER_FRAME = 48000 // FPS
SYSTEM_ID = "08004600DEAD0001"  # erfunden
CODEC = "AVC100CBG_1920_1080_H422IP@L42"
NRT_SCHEME = "urn:uuid:786d4d22-8280-11de-8a39-08004678031c"
ANC_FRAME_SIZE = 36  # 2 (Zeilenzahl) + 14 (Zeilenkopf) + 20 (4+16 Nutzdaten, 4er-Ausrichtung)
BASE_LOCAL = datetime(2025, 6, 1, 10, 0, 0)  # lokale Zeit (+02:00)
TZ = "+02:00"

START_TCS = ["00:00:00:00", "11:22:33:07", "12:00:00:00", "09:15:59:49"]
UNLISTED_TC = "13:05:10:01"
TONES = [440, 523, 659, 784, 880, 988, 1175, 1319]  # Hz je Spur


# --------------------------------------------------------------------------- Werkzeuge
def tool(name: str) -> str:
    if _tools is not None:
        return _tools.find_tool(name)
    p = shutil.which(name)
    if not p:
        raise SystemExit(f"{name} nicht gefunden")
    return p


def run(args: list) -> str:
    r = subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"Fehler ({r.returncode}): {' '.join(map(str, args))}\n{r.stdout[-2000:]}")
    return r.stdout


def crlf(lines: list[str]) -> bytes:
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def seeded(*parts: str) -> bytes:
    return hashlib.sha256("|".join(parts).encode()).digest()


# --------------------------------------------------------------------------- Timecode / UMID
def tc_to_frames(tc: str) -> int:
    h, m, s, f = (int(x) for x in tc.split(":"))
    return ((h * 60 + m) * 60 + s) * FPS + f


def frames_to_tc(n: int) -> tuple[int, int, int, int]:
    f = n % FPS
    s = (n // FPS) % 60
    m = (n // (FPS * 60)) % 60
    h = (n // (FPS * 3600)) % 24
    return h, m, s, f


def ltc_value(frame50: int) -> str:
    """LTC-Wert wie in NonRealTimeMeta bei 50p: FF SS MM HH (BCD, 25 fps), Bit 0x80 im Stundenbyte = ungerades 50p-Bild."""
    h, m, s, f = frames_to_tc(frame50)
    half = f & 1
    f25 = f // 2

    def bcd(v: int) -> int:
        return ((v // 10) << 4) | (v % 10)

    return "%02X%02X%02X%02X" % (bcd(f25), bcd(s), bcd(m), bcd(h) | (0x80 if half else 0))


def make_umid(kind: int, key: str) -> str:
    """Sony-Muster: 060A2B34 01010105 01010D43 13000000 + [kind 3 Byte-Zufall 4 Byte-Zeit 8 Byte-SystemID]."""
    h = seeded("umid", key)
    mat = bytes([kind]) + h[:3] + h[3:7] + bytes.fromhex(SYSTEM_ID)
    return ("060A2B340101010501010D4313000000" + mat.hex()).upper()


def umid_variant(umid: str, type_byte: str) -> str:
    return umid[:24] + type_byte + umid[26:]


def hex_id(key: str, n: int = 32) -> str:
    return seeded("id", key).hex().upper()[:n]


def fmt_local(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + TZ


# --------------------------------------------------------------------------- XML-Vorlagen (CRLF, Tabs wie Sony)
def clip_xml(name: str, umid: str, frames: int, start_tc: str, dt_local: datetime) -> bytes:
    s = tc_to_frames(start_tc)
    when0 = hex_id(name + "w0", 8) + "591306A5"
    when1 = hex_id(name + "w1", 8) + "591306A5"
    last_update = fmt_local(dt_local + timedelta(minutes=1))
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00" '
        'xmlns:lib="urn:schemas-professionalDisc:lib:ver.2.00" lastUpdate="%s">' % last_update,
        '\t<TargetMaterial umidRef="%s"/>' % umid,
        '\t<Duration value="%d"/>' % frames,
        '\t<LtcChangeTable tcFps="25" halfStep="true">',
        '\t\t<LtcChange frameCount="0" value="%s" status="increment"/>' % ltc_value(s),
        '\t\t<LtcChange frameCount="%d" value="%s" status="end"/>' % (frames - 1, ltc_value(s + frames - 1)),
        '\t</LtcChangeTable>',
        '\t<CreationDate value="%s"/>' % fmt_local(dt_local),
        '\t<TypicalUbit value="0000000000000000"/>',
        '\t<KeyFrame frameCount="0"/>',
        '\t<BodyUmidBasicChangeTable>',
        '\t\t<BodyUmidBasicChange frameCount="0" value="%s" status="start"/>' % umid_variant(umid, "33"),
        '\t</BodyUmidBasicChangeTable>',
        '\t<BodyUmidWhenChangeTable>',
        '\t\t<BodyUmidWhenChange frameCount="0" value="%s" status="increment"/>' % when0,
        '\t\t<BodyUmidWhenChange frameCount="%d" value="%s" status="end"/>' % (frames - 1, when1),
        '\t</BodyUmidWhenChangeTable>',
        '\t<BodyUmidWhereChangeTable>',
        '\t\t<BodyUmidWhereChange frameCount="0" value="000000000000000000000000" status="start"/>',
        '\t</BodyUmidWhereChangeTable>',
        '\t<BodyUmidWhoChangeTable>',
        '\t\t<BodyUmidWhoChange frameCount="0" value="000000000000000000000000" status="start"/>',
        '\t</BodyUmidWhoChangeTable>',
        '\t<KlvPacketTable>',
        '\t\t<KlvPacket key="060E2B34010101050301020A02000000" frameCount="0" '
        'lengthValue="095F5265635374617274" status="spot"/>',
        '\t</KlvPacketTable>',
        '\t<VideoFormat>',
        '\t\t<VideoRecPort port="DIRECT"/>',
        '\t\t<VideoFrame videoCodec="%s" captureFps="50.00p" formatFps="50p"/>' % CODEC,
        '\t\t<VideoLayout pixel="1920" numOfVerticalLine="1080" aspectRatio="16:9"/>',
        '\t</VideoFormat>',
        '\t<AudioFormat numOfChannel="8">',
        '\t\t<AudioRecPort port="ANALOG" audioCodec="LPCM24" trackDst="CH1"/>',
        '\t\t<AudioRecPort port="ANALOG" audioCodec="LPCM24" trackDst="CH2"/>',
    ] + ['\t\t<AudioRecPort port="OTHERS" audioCodec="LPCM24" trackDst="CH%d"/>' % i for i in range(3, 9)] + [
        '\t</AudioFormat>',
        '\t<Device manufacturer="Sony" modelName="PMW-F55" serialNo="0000000">',
        '\t</Device>',
        '\t<Lens modelName="XK6X20-SAM                    "/>',
        '\t<RecordingMode type="normal" cacheRec="false"/>',
        '</NonRealTimeMeta>',
    ]
    return crlf(lines)


def mediapro_xml(clips: list[dict], created: datetime) -> bytes:
    media_id = hex_id("media", 32)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile" createdAt="%s" version="2.10">' % fmt_local(created),
        '\t<Properties>',
        '\t\t<System systemId="%s" systemKind="PMW-F55 ver.9.000"/>' % SYSTEM_ID,
        '\t\t<Attached mediaId="%s" mediaKind="ProfessionalDisc"' % media_id,
        '\t\t\tmediaName=""/>',
        '\t</Properties>',
        '\t<Contents>',
    ]
    for c in clips:
        n, frames = c["name"], c["frames"]
        lines += [
            '\t\t<Material uri="./Clip/%s.MXF" type="MXF" offset="0" dur="%d" fps="50p" aspectRatio="16:9" ch="8"' % (n, frames),
            '\t\t\tvideoType="%s" audioType="LPCM24"' % CODEC,
            '\t\t\tumid="%s"' % c["umid"],
            '\t\t\tstatus="none">',
            '\t\t\t<RelevantInfo uri="./Clip/%sM01.XML" type="XML"/>' % n,
            '\t\t\t<RelevantInfo uri="./Clip/%sR01.BIM" type="BiM"/>' % n,
            '\t\t</Material>',
        ]
        if c.get("take"):
            lines += [
                '\t\t<Material uri="./Take/%sU01.SMI" type="PD-EDL" offset="0" dur="%d" fps="50p" aspectRatio="16:9" ch="8"' % (n, frames),
                '\t\t\tumid="%s"' % umid_variant(make_umid(0x41, n + "take"), "13"),
                '\t\t\tstatus="none">',
                '\t\t\t<Component uri="./Clip/%s.MXF" type="MXF"' % n,
                '\t\t\t\tumid="%s"/>' % c["umid"],
                '\t\t\t<Component mediaId="%s" mediaKind="ProfessionalDisc" mediaName=""' % hex_id(n + "cm", 32),
                '\t\t\t\turi="file:///XDROOT/Take/%s.SMI" type="PD-EDL"' % n,
                '\t\t\t\toffset="%d"' % frames,
                '\t\t\t\tumid="%s"/>' % umid_variant(make_umid(0x41, n + "cmp"), "13"),
                '\t\t\t<RelevantInfo uri="./Take/%sU01M01.XML" type="XML"/>' % n,
                '\t\t</Material>',
            ]
    lines += ['\t</Contents>', '</MediaProfile>']
    return crlf(lines)


# --------------------------------------------------------------------------- Medien
def write_wav24(path: Path, freq: float, frames: int, amp: float = 0.25) -> None:
    n = frames * SAMPLES_PER_FRAME
    data = bytearray()
    peak = int(amp * (2 ** 23 - 1))
    for i in range(n):
        v = int(peak * math.sin(2 * math.pi * freq * i / 48000.0))
        data += struct.pack("<i", v)[:3]
    hdr = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, 48000, 48000 * 3, 3, 24) + b"data" + struct.pack("<I", len(data))
    path.write_bytes(hdr + bytes(data))


def make_video(path: Path, frames: int, hue: int) -> None:
    run([tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate={FPS}",
         "-vf", f"hue=h={hue},format=yuv422p10le", "-frames:v", frames,
         "-pix_fmt", "yuv422p10le", "-color_range", "tv", "-c:v", "libx264",
         "-x264-params", "avcintra-class=100:colorprim=bt709:transfer=bt709:colormatrix=bt709:chromaloc=0:force-cfr=1",
         "-f", "h264", path])


def make_anc(path: Path, frames: int) -> None:
    """ST 436, je Frame eine Zeile 11: DID 0x43, SDID 0x05 (RDD18), DC=16, 8-Bit-Luma, VANC progressive frame."""
    out = bytearray()
    for i in range(frames):
        udw = bytes((i + k) & 0xFF for k in range(16))
        pkt = bytes([0x43, 0x05, 16]) + udw
        pkt += bytes([sum(pkt) & 0xFF])  # vereinfachte Pruefsumme
        aligned = (len(pkt) + 3) & ~3
        # Zeilenzahl, dann je Zeile: Zeilennr, Wrapping, Sample-Coding, Samples, Array-Header (Laenge, Elementgroesse 1)
        el = struct.pack(">H", 1) + struct.pack(">HBBH", 11, 4, 4, len(pkt)) + struct.pack(">II", aligned, 1)
        el += pkt + bytes(aligned - len(pkt))
        assert len(el) == ANC_FRAME_SIZE, len(el)
        out += el
    path.write_bytes(bytes(out))


def make_thumbnail(path: Path) -> None:
    try:
        run([tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=160x90:rate=1", "-frames:v", 1, path])
    except SystemExit:
        # minimales JPEG (SOI + EOI) als Platzhalter
        path.write_bytes(bytes.fromhex("FFD8FFD9"))


def wrap_clip(out: Path, video: Path, wavs: list[Path], anc: Path, start_tc: str, frames: int,
              mp_uid: str, fp_uid: str, create_date: str, prod_info: list[str], extra: list[str]) -> str:
    args = [tool("raw2bmx"), "-t", "op1a", "-o", out, "-f", FPS, "-y", start_tc,
            "--mp-uid", mp_uid, "--fp-uid", fp_uid, "--dur", frames, "--aes-3",
            "--create-date", create_date, "--prod-info", *prod_info,
            "--signal-std", "st274", "--color-siting", "cositing", *extra,
            "--avci100_1080p", video]
    for w in wavs:
        args += ["--locked", "true", "--audio-ref", "0", "--wave", w]
    args += ["--anc-const", ANC_FRAME_SIZE, "--anc", anc]
    return run(args)


def sonyfy_header(path: Path) -> list[str]:
    """Macht die bmx-Kopfdaten einer Quell-MXF "Sony-aehnlich" (nur fuer den Test der
    Uebernahme in xdroot_converter.mxfheader; sonst waere die Uebernahme ein No-op).

    ANNAHMEN (nicht an echten Sony-Dateien belegt, ausser wo vermerkt):
    - Track-IDs Video 2, Audio 3..10, ANC 11 in beiden Packages: wie docs/analysis/f55_mxf2raw.txt.
    - Timecode-Track-ID 1: ANNAHME, nicht belegt.
    - ToolkitVersion/Platform der Identification entfernt: mxf2raw zeigt sie bei echten Sony-Dateien nicht.
    - Track-Namen (TC1/V1/A1..) entfernt: ANNAHME zum Testen.
    - Sound-Deskriptoren: AvgBps entfernt und SequenceOffset (3d.0b) = 0 ergaenzt: reine
      TEST-ANNAHME, damit Regel c (Deskriptor wie Quelle) tatsaechlich etwas zu tun hat.
    """
    def edit(hm):
        st = mxfheader.resolve(hm)
        first = {"timecode": 1, "picture": 2, "sound": 3, "data": 11}
        maps = {}
        for pkg in (st.file, st.material):
            used = {k: 0 for k in first}
            extra = 12
            ids = []
            for t in pkg.tracks:
                if t.kind in first:
                    ids.append(first[t.kind] + used[t.kind])
                    used[t.kind] += 1
                else:
                    ids.append(extra)
                    extra += 1
            maps[pkg.role] = mxfheader.set_track_ids(hm, pkg, ids)
            for t in pkg.tracks:
                hm.drop(t.set, "GenericTrack.TrackName")
        mxfheader.relink_after_renumber(hm, st, maps["source"], maps["material"])
        for ident in st.identifications:
            hm.drop(ident, "Identification.ToolkitVersion")
            hm.drop(ident, "Identification.Platform")
        for d in st.file_descriptors():
            if hm.get(d, "GenericSoundEssenceDescriptor.ChannelCount") is not None:
                hm.drop(d, "WaveAudioDescriptor.AvgBps")
                hm.put(d, "WaveAudioDescriptor.SequenceOffset", b"\x00",
                       after=["WaveAudioDescriptor.BlockAlign"])
        mxfheader.verify_references(hm)
        return []
    return mxfheader.patch_header(path, edit)


# --------------------------------------------------------------------------- Hauptprogramm
def main() -> int:
    ap = argparse.ArgumentParser(description="Synthetische XAVC-I-Testkarte (XDROOT) erzeugen")
    ap.add_argument("target", nargs="?", default="_test_out/synth_card")
    ap.add_argument("--clips", type=int, default=2, help="Anzahl gelisteter Clips (Standard 2)")
    ap.add_argument("--seconds", type=float, default=2, help="Laenge je Clip in Sekunden (Standard 2)")
    ap.add_argument("--unlisted-no-xml", action="store_true",
                    help="nicht gelisteter Clip (PDW F5_0099) bekommt keine M01.XML (Standard: mit M01.XML)")
    a = ap.parse_args()

    frames = max(1, int(round(a.seconds * FPS)))
    target = Path(a.target)
    xr = target / "XDROOT"
    if xr.exists():
        shutil.rmtree(xr)
    for d in ("Clip", "Take", "Sub", "Edit", "General", "Thmbnl", "UserData"):
        (xr / d).mkdir(parents=True)
    work = target / "_work"
    work.mkdir(exist_ok=True)

    wavs = []
    for i in range(N_AUDIO):
        w = work / f"a{i}.wav"
        write_wav24(w, TONES[i], frames)
        wavs.append(w)
    anc = work / "anc.raw"
    make_anc(anc, frames)

    prod_info = ["Sony", "Mem ", "0.0.0.0.0", "2.00", "cede1104-8280-11de-8a39-08004678031c"]

    clips = []
    names = [f"PDW F5_{i + 1:04d}" for i in range(a.clips)]
    first_video = None
    for idx, name in enumerate(names + ["PDW F5_0099"]):
        listed = idx < a.clips
        start_tc = START_TCS[idx % len(START_TCS)] if listed else UNLISTED_TC
        dt_local = BASE_LOCAL + timedelta(minutes=10 * idx)
        dt_utc = dt_local - timedelta(hours=2)
        create_date = dt_utc.strftime("%Y-%m-%dT%H:%M:%S") + ":00"
        mp = make_umid(0x41, name)
        fp = mp[:32] + "42" + mp[34:]  # File Package: erstes Byte der Materialnummer 0x42 statt 0x41
        if listed or first_video is None:
            v = work / f"v{idx}.h264"
            make_video(v, frames, hue=40 * idx)
            first_video = first_video or v
        else:
            v = first_video
        out = xr / "Clip" / f"{name}.MXF"
        wrap_clip(out, v, wavs, anc, start_tc, frames, mp, fp, create_date, prod_info, [])
        sonyfy_header(out)
        print(f"Clip {name}.MXF  start_tc={start_tc}  frames={frames}  listed={listed}")
        if not listed and not a.unlisted_no_xml:
            (xr / "Clip" / f"{name}M01.XML").write_bytes(clip_xml(name, mp, frames, start_tc, dt_local))
        if listed:
            (xr / "Clip" / f"{name}M01.XML").write_bytes(clip_xml(name, mp, frames, start_tc, dt_local))
            (xr / "Clip" / f"{name}R01.BIM").write_bytes(b"BiM\x00" + seeded("bim", name)[:60])
            make_thumbnail(xr / "Thmbnl" / f"{name}T01.JPG")
            c = {"name": name, "umid": mp, "frames": frames, "take": idx == 0}
            clips.append(c)
            if c["take"]:
                (xr / "Take" / f"{name}U01.SMI").write_bytes(crlf([
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<smil xmlns="http://www.w3.org/ns/SMIL"><head/><body><par>',
                    '\t<video src="./Clip/%s.MXF" clipBegin="0" clipEnd="%d"/>' % (name, frames),
                    '</par></body></smil>']))
                (xr / "Take" / f"{name}U01M01.XML").write_bytes(crlf([
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">',
                    '\t<Duration value="%d"/>' % frames,
                    '</NonRealTimeMeta>']))

    created = BASE_LOCAL + timedelta(minutes=5)
    (xr / "MEDIAPRO.XML").write_bytes(mediapro_xml(clips, created))
    (xr / "CUEUP.XML").write_bytes(crlf([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<CueUpInfo xmlns="http://xmlns.sony.net/pro/metadata/cueupinfo" version="1.00">',
        '</CueUpInfo>']))
    (xr / "DISCMETA.XML").write_bytes(crlf([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<DiscMeta xmlns="urn:schemas-professionalDisc:discMeta:ver.1.00">',
        '\t<Title>Synthetische Testkarte</Title>',
        '</DiscMeta>']))
    shutil.rmtree(work)
    print("Fertig:", xr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
