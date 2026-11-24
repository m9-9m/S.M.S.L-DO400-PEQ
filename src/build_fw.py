"""DO400 Hardware PEQ Firmware Builder (standalone).

Built DIRECTLY from user-supplied Factory 3.23 (extracted from do400_dump.bin):
  * Tile 1: 100% BYTE-EXACT unmodified Factory 3.23 (ZERO hooks).
  * Tile 0: Factory 3.23 with EXACTLY FIVE (5) stock-span patch sites (17 bytes total):
      1. 0x80908  bl webhid_set_hook (0x91480)   WebHID PEQ1 v1 SET_REPORT (atomic SET_ENABLE)
      2. 0x809B4  bl webhid_get_hook (0x91E00)   WebHID PEQ1 v1 GET_REPORT (tail-jump to 0x8AD04)
      3. 0x82B34  bu pcm_gate        (0x91000)   Direct PCM I2S 10-band Q29 PEQ + 3-layer DoP guard
      4. 0x8AF98  0x00080000                     512 KiB DFU upgrade limit
      5. 0x8B3EB  0x02 (was 0x01)                HID Feature Report Data,Var,Abs (WebHID on Windows)
  * ZERO simulator EP0 stubs (0x80C54/0x81E00/0x82840/0x83A48 are 100% Factory 3.23 XUD_*).
  * ZERO 192 kHz boot-default override (0x82D28/0x8D2AC are 100% Factory 3.23 44.1 kHz).
  * Shipping DFU container = 200,896 B (196.19 KiB < 200 KiB stock DFU limit!).

usage:
  python build_fw.py --dump /path/to/do400_dump.bin [--out-dir build]
  python build_fw.py --factory-dir /path/to/factory_dir [--out-dir build]
"""
import argparse, hashlib, json, os, shutil, struct, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))

import peq_ref as PEQ                       # noqa: E402
import pack_image1 as PK                    # noqa: E402
import webhid_hooks as WH                   # noqa: E402
from xu_image_an import mkelf               # noqa: E402

BASE = 0x54
IMAGE_BASE = 0x80000
DP0 = 0x8B220
IMAGE1_FLASH_OFF = 0x2E000

H_IN = {
    "img1_tile0.bin": "a886b01701a5058fb30a51ead719e5810c19bfd0692bce3b21f16cd45f6a0913",
    "img1_tile1.bin": "fb21e2df579fe6bfc3cb7b4fa76366b77766413879b9794828d0a0186ec25b15",
}


def extract_factory_from_dump(dump_path: Path, factory_dir: Path):
    """Extract Factory 3.23 image1 sections (swsetup, tile0, tile1) from a 2 MiB SPI dump."""
    factory_dir.mkdir(parents=True, exist_ok=True)
    raw = dump_path.read_bytes()
    if len(raw) < IMAGE1_FLASH_OFF + 0x100:
        raise ValueError(f"Dump too small: {len(raw)} B")
    logical = PK.nibble_swap(raw[IMAGE1_FLASH_OFF:])
    magic = struct.unpack_from("<I", logical, 0x00)[0]
    if magic != PK.IMAGE_MAGIC:
        raise ValueError(f"Unexpected magic at 0x{IMAGE1_FLASH_OFF:X}: 0x{magic:08X}")
    size = struct.unpack_from("<I", logical, 0x10)[0]
    img = logical[:size]
    d = PK.describe_image(img)
    sw = d["switch_setup"]
    c0, c1 = d["cores"][0], d["cores"][1]
    sw_bytes = sw.get("size", sw["words"] * 4)
    (factory_dir / "image1_swsetup.bin").write_bytes(img[sw["offset"]:sw["offset"] + sw_bytes])
    t0 = img[c0["offset"]:c0["offset"] + c0["size"]]
    t1 = img[c1["offset"]:c1["offset"] + c1["size"]]
    (factory_dir / "img1_tile0.bin").write_bytes(t0)
    (factory_dir / "img1_tile1.bin").write_bytes(t1)
    mkelf(t1, str(factory_dir / "img1_tile1.elf"))
    return factory_dir

SITE_SET = 0x80908
SITE_GET = 0x809B4
SITE_GATE = 0x82B34
SITE_BYPASS_RET = 0x82B38
SITE_PEQ_RESUME = 0x82B58
DFU_LIMIT = 0x8AF98
SITE_HID_DESC = 0x8B3E8  # 09 01 B1 01 -> 09 01 B1 02 (Feature Data,Var,Abs for Windows WebHID)

EXPECTED_FACTORY_SITES = {
    SITE_SET: bytes.fromhex("06f05cd1"),       # bl 0x86E7C (stock MQA HID SET)
    SITE_GET: bytes.fromhex("0af0d3d0"),       # bl 0x8AD04 (stock XUD_DoGetRequest)
    SITE_GATE: bytes.fromhex("01f06b6c"),      # ldw r1, cp[0x6b]
    DFU_LIMIT: struct.pack("<I", 0x00032000),  # 200 KiB stock DFU limit
    SITE_HID_DESC: bytes.fromhex("0901b101"),  # Usage(1), Feature(Const,Arr,Abs)
}

# Forbidden simulator-stub sites (MUST remain 100% Factory 3.23 in shipping firmware!)
FORBIDDEN_STUB_SITES = [0x80C54, 0x80C64, 0x81018, 0x81E00, 0x82840, 0x829E0,
                        0x82AA0, 0x82D28, 0x83A48, 0x83F0C, 0x8D2AC]

# Shipping Tile 0 total payload size = 0x1302C (includes 4-byte trailing section CRC slot,
# aligns DFU container 0x1F80 + 0x1302C + 0x1C114 = 0x310C0 = 200,896 B < 200 KiB, % 64 == 0).
SHIP_TILE0_SIZE = 0x1302C


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def d(a):
    return WH.d(a)


def enc_bu(pc, target):
    return WH.enc_bu_(pc, target)


# ======================================================================== pcm_gate (0x91000)
def pcm_gate_source():
    """Standalone, self-contained PCM-only I2S PEQ gate placed at ADDR_GATE (0x91000).

    Entered via `bu 0x91000` at 0x82B34 (only reached when r4 == 0 = PCM; Native DSD r4==1/2
    already branched past 0x82B34 in stock code).

    * PCM_ON == 0: 9-instruction fast bypass using dead r1 -> `ldw r1, cp[0x6b]; bu 0x82B38`
      (100% Factory 3.23 I2S output, works up to 768 kHz with ~90 ns overhead).
    * PCM_ON != 0:
      - Saves r0..r11 to PCM_STATE[0..11].
      - RATEW > PEQ_RATE_MAX (192 kHz): resets PEQ_OPEN/DEC/DoP, restores r0..r11, jumps to
        Factory 0x82B38 (bit-exact bypass for 352.8 / 384 / 768 kHz).
      - 3-layer DoP guard (RATEW >= 176400, 16-pair strict 0x05/0xFA alternation, hold=32):
        while DOP_HOLD != 0, skips filter bit-exactly while preserving PEQ_OPEN.
      - Auto-FIRST reset: PEQ_OPEN == 0 or RATEW != PEQ_LAST_RATE -> DEC = 2 (FIRST, clears all
        10 bands' DF1 state), else DEC = 1 (NEXT).
      - Runs 10-band DF1 Q29 + Q28 Preamp (identical arithmetic to peq_ref.py, 0 LSB error),
        records OBS_L/OBS_R/PCM_SEQ, outputs I2S L & R, restores r0..r11, jumps to 0x82B58.
    """
    D = d(WH.PCM_STATE)
    L = [
        ".text", ".issue_mode dual", ".align 4",
        ".global pcm_gate", "pcm_gate:",
        # ---- Fast stock bypass when PCM_ON == 0 (touches ONLY dead r1!)
        f"ldw r1,dp[{d(WH.PCM_ON)}]",
        "bt r1,.Lpeq_enter",
        f"stw r1,dp[{d(WH.PEQ_OPEN)}]",
        f"stw r1,dp[{d(WH.DOP_HOLD)}]",
        f"stw r1,dp[{d(WH.DOP_RUN)}]",
        f"stw r1,dp[{d(WH.DOP_PREV)}]",
        f"stw r1,dp[{d(WH.DEC)}]",
        "ldw r1,cp[107]",
        ".align 4",
        ".global gate_fast_ret", "gate_fast_ret:", "nop", ".space 2",

        ".align 4",
        ".Lpeq_enter:",
    ]
    L += [f"stw r{i},dp[{D + i}]" for i in range(12)]
    L += [
        f"ldw r3,dp[{d(WH.RATEW)}]",
        f"ldw r1,dp[{d(WH.PEQ_RATE_MAX)}]",
        "lsu r1,r1,r3",
        "bf r1,.Lrate_ok",
        "ldc r0,0",
        f"stw r0,dp[{d(WH.PEQ_OPEN)}]",
        f"stw r0,dp[{d(WH.DEC)}]",
        f"stw r0,dp[{d(WH.DOP_HOLD)}]",
        f"stw r0,dp[{d(WH.DOP_RUN)}]",
        f"stw r0,dp[{d(WH.DOP_PREV)}]",
    ]
    L += [f"ldw r{i},dp[{D + i}]" for i in range(12)]
    L += [
        "ldw r1,cp[107]",
        ".align 4",
        ".global gate_rate_ret", "gate_rate_ret:", "nop", ".space 2",
        ".align 4",
        ".Lrate_ok:",
    ]
    # ---- 3-layer DoP guard
    L += WH.dop_guard_source()
    # ---- Auto-FIRST / NEXT decision (with PCM_MASK)
    L += [
        f"ldw r0,dp[{d(WH.PEQ_OPEN)}]",
        "bf r0,.Lfirst",
        f"ldw r3,dp[{d(WH.RATEW)}]",
        f"ldw r1,dp[{d(WH.PEQ_LAST_RATE)}]",
        "eq r1,r3,r1",
        "bf r1,.Lfirst",
        "ldc r0,1",
        "bu .Ldecision",
        ".Lfirst:",
        f"ldw r3,dp[{d(WH.RATEW)}]",
        f"stw r3,dp[{d(WH.PEQ_LAST_RATE)}]",
        "ldc r0,2",
        ".Ldecision:",
        f"ldw r4,dp[{d(WH.PCM_MASK)}]",
        "and r0,r0,r4",
        f"stw r0,dp[{d(WH.DEC)}]",
        "eq r1,r0,0",
        "eq r1,r1,0",
        f"stw r1,dp[{d(WH.PEQ_OPEN)}]",
    ]

    # ---- Channel 0 (L) and Channel 1 (R) 10-band DF1 Q29 + Q28 Preamp
    for ch, raw_addr, band_off in ((0, WH.L_WORD, WH.BAND0), (1, WH.R_WORD, WH.BAND0 + WH.CH_STRIDE)):
        band_dp = d(WH.PCM_STATE + band_off)
        L += [
            f"ldw r3,dp[{d(WH.DEC)}]",
            f"ldw r0,dp[{d(raw_addr)}]",
            f"bf r3,.Lready{ch}",
            f"ldw r5,dp[{d(WH.DOP_HOLD)}]",
            f"bt r5,.Lready{ch}",
            f"ldaw r1,dp[{band_dp}]",
            "eq r4,r3,2",
            f"bf r4,.Lscale{ch}",
            "ldc r2,10",
            "ldc r4,0",
            f".Lclear{ch}:",
            "stw r4,r1[5]", "stw r4,r1[6]", "stw r4,r1[7]", "stw r4,r1[8]", "stw r4,r1[9]",
            "ldaw r1,r1[10]",
            "sub r2,r2,1",
            f"bt r2,.Lclear{ch}",
            f"ldaw r1,dp[{band_dp}]",
            f".Lscale{ch}:",
            "ashr r0,r0,6",
            "ldc r4,0",
            "ldc r5,0",
            f"ldw r6,dp[{d(WH.PCM_PREAMP)}]",
            "maccs r4,r5,r0,r6",
            "ldc r3,28",
            "lextract r0,r4,r5,r3,32",
            "ldc r2,10",
            "ldc r3,29",
            f".Lband{ch}:",
            "ldd r5,r4,r1[0]",
            "ldd r7,r6,r1[3]",
            "ldd r9,r8,r1[2]",
            "ldc r10,0",
            "maccs r10,r9,r4,r0",
            "maccs r10,r9,r5,r6",
            "std r6,r0,r1[3]",
            "ldd r5,r4,r1[1]",
            "maccs r10,r9,r4,r7",
            "ldd r7,r6,r1[4]",
            "maccs r10,r9,r5,r6",
            "maccs r10,r9,r8,r7",
            "mov r11,r9",
            "zext r11,r3",
            "lsats r10,r9,r3",
            "lextract r0,r10,r9,r3,32",
            "std r6,r0,r1[4]",
            "{sub r2,r2,1; stw r11,r1[5]}",
            "ldaw r1,r1[10]",
            f"bt r2,.Lband{ch}",
            f".Lclip{ch}:",
            f"ldw r4,dp[{d(WH.CLIP_HI)}]",
            "lss r5,r4,r0",
            f"bf r5,.Llow{ch}",
            "mov r0,r4",
            f".Llow{ch}:",
            f"ldw r4,dp[{d(WH.CLIP_LO)}]",
            "lss r5,r0,r4",
            f"bf r5,.Lshift{ch}",
            "mov r0,r4",
            f".Lshift{ch}:",
            "shl r0,r0,6",
            f".Lready{ch}:",
        ]
        if ch == 0:
            L += [
                f"stw r0,dp[{d(WH.OBS_L)}]",
                "bitrev r5,r0",
                f"ldw r10,dp[{D + 10}]",
                "ldw r0,cp[107]",
                "out res[r10],r0",
                "ldw r1,sp[5]",
                "out res[r1],r5",
            ]
        else:
            L += [
                f"stw r0,dp[{d(WH.OBS_R)}]",
                f"ldw r1,dp[{d(WH.PCM_SEQ)}]",
                "add r1,r1,1",
                f"stw r1,dp[{d(WH.PCM_SEQ)}]",
                "bitrev r5,r0",
                f"ldw r10,dp[{D + 10}]",
                "ldw r0,cp[184]",
                "out res[r10],r0",
                "ldw r1,sp[5]",
                f"stw r5,dp[{d(WH.R_OUT_TMP)}]",
            ]

    # ---- Restore registers exactly matching Factory 3.23 state at 0x82B58
    for reg in (0, 2, 3, 4, 6, 8, 9, 11):
        L += [f"ldw r{reg},dp[{D + reg}]"]
    L += [
        f"ldw r7,dp[{d(WH.L_WORD)}]",
        f"ldw r5,dp[{d(WH.R_WORD)}]",
        f"ldw r10,dp[{d(WH.R_OUT_TMP)}]",
        ".align 4",
        ".global gate_peq_ret", "gate_peq_ret:", "nop", ".space 2",
        ".global gate_end", "gate_end:",
    ]
    return L


def build_gate_block(outdir):
    sites = ["gate_fast_ret", "gate_rate_ret", "gate_peq_ret"]

    def mk(pad):
        out = []
        for line in pcm_gate_source():
            out.append(line)
            s = line.rstrip(":")
            if s in pad and pad[s]:
                out.append(f".space {pad[s]}")
        return out

    code, lab = WH._assemble_aligned(outdir, "pcm_gate", mk, "pcm_gate", "gate_end", sites)
    for name, target in (("gate_fast_ret", SITE_BYPASS_RET),
                         ("gate_rate_ret", SITE_BYPASS_RET),
                         ("gate_peq_ret", SITE_PEQ_RESUME)):
        at = lab[name]
        assert code[at:at + 2] == b"\xff\x17", (name, code[at:at + 2].hex())
        code[at:at + 4] = enc_bu(WH.ADDR_GATE + at, target)
    assert WH.ADDR_GATE + len(code) <= WH.ADDR_SET, (hex(WH.ADDR_GATE + len(code)), hex(WH.ADDR_SET))
    return bytes(code)


# ======================================================================== Shipping Tile 0 builder
def flat_taps(nbands):
    """Identity (flat 0 dB) Q29 biquad taps: b0=1.0 (0x20000000), b1=b2=a1=a2=0."""
    return [(1 << 29, 0, 0, 0, 0) for _ in range(nbands)]


def build_shipping_tile0(fac0_path: Path, out_path: Path, work_dir: Path,
                         pcm_taps=None, mqa_taps=None):
    fac0 = fac0_path.read_bytes()
    data = bytearray(fac0)
    # Verify Factory 3.23 bytes at all 4 hook sites
    for addr, want in EXPECTED_FACTORY_SITES.items():
        got = bytes(data[addr - IMAGE_BASE:addr - IMAGE_BASE + 4])
        assert got == want, f"Factory site {addr:#x}: {got.hex()} != {want.hex()}"

    orig_set = WH.dec_bl(SITE_SET, bytes(data[SITE_SET - IMAGE_BASE:SITE_SET - IMAGE_BASE + 4]))
    orig_get = WH.dec_bl(SITE_GET, bytes(data[SITE_GET - IMAGE_BASE:SITE_GET - IMAGE_BASE + 4]))
    assert orig_set == 0x86E7C, hex(orig_set)
    assert orig_get == 0x8AD04, hex(orig_get)

    gate_blob = build_gate_block(work_dir / "logs" / "asm")
    hooks = WH.build_hooks(work_dir / "logs" / "asm_wh", orig_set, orig_get, test=False)

    assert len(data) == 0xEBAC
    # Clear old trailing CRC word? Wait: crt0 zeros 0x8EBA8..0x909C4 (BSS), keep 0x8EBA8..0x8EBAC
    # untouched so stock span 0x80000..0x8EBAC matches Factory 3.23 except the 4 sites!
    data.extend(bytes(SHIP_TILE0_SIZE - len(data)))

    # ---- Patch ONLY the 5 stock-span sites
    data[SITE_SET - IMAGE_BASE:SITE_SET - IMAGE_BASE + 4] = WH.enc_bl(SITE_SET, WH.ADDR_SET)
    data[SITE_GET - IMAGE_BASE:SITE_GET - IMAGE_BASE + 4] = WH.enc_bl(SITE_GET, WH.ADDR_GET)
    data[SITE_GATE - IMAGE_BASE:SITE_GATE - IMAGE_BASE + 4] = enc_bu(SITE_GATE, WH.ADDR_GATE)
    struct.pack_into("<I", data, DFU_LIMIT - IMAGE_BASE, 0x00080000)
    data[SITE_HID_DESC - IMAGE_BASE:SITE_HID_DESC - IMAGE_BASE + 4] = bytes.fromhex("0901b102")

    # ---- Place new code blocks (0x91000..0x91F00)
    data[WH.ADDR_GATE - IMAGE_BASE:WH.ADDR_GATE - IMAGE_BASE + len(gate_blob)] = gate_blob
    for addr, blob in hooks.items():
        if isinstance(addr, int):
            data[addr - IMAGE_BASE:addr - IMAGE_BASE + len(blob)] = blob

    # ---- Place PCM_STATE (0x92000) and MQA_STATE (0x92780)
    if pcm_taps is None:
        pcm_taps = flat_taps(10)
    if mqa_taps is None:
        mqa_taps = flat_taps(12)

    pcm = PEQ.Body(10, mask=0x3FF)
    for ch in (0, 1):
        for i, taps in enumerate(pcm_taps):
            pcm.sections[ch][i][0:5] = taps
    pcmw = pcm.state_words()
    assert pcmw[17] == 0x10000000 and pcmw[24] == 0x3FF
    # Zero out FIRSTW/NEXTW (words 15..16) since Tile-1 tags are not used
    pcmw[15] = 0
    pcmw[16] = 0
    for i, w in enumerate(pcmw):
        struct.pack_into("<I", data, WH.PCM_STATE - IMAGE_BASE + 4 * i, w & 0xFFFFFFFF)

    mqa = PEQ.Body(12, mask=0xFFF)
    for ch in (0, 1):
        for i, taps in enumerate(mqa_taps):
            mqa.sections[ch][i][0:5] = taps
    mqaw = mqa.state_words()
    assert mqaw[17] == 0x10000000 and mqaw[24] == 0xFFF
    for i, w in enumerate(mqaw):
        struct.pack_into("<I", data, WH.MQA_STATE - IMAGE_BASE + 4 * i, w & 0xFFFFFFFF)

    # ---- Control words (all boot-safe defaults: PCM_ON=0, MQA_ON=0)
    for addr, val in (
        (WH.PCM_ON, 0),
        (WH.MQA_ON, 0),
        (WH.OCC_CUR, 0),
        (WH.PEQ_RATE_MAX, WH.PEQ_RATE_MAX_DEFAULT),
        (WH.PEQ_LAST_RATE, 0),
        (WH.WH_MAGIC, 0x31514550),
        (WH.WH_BUILD, WH.WH_BUILD_VAL),
        (WH.WH_XORC, 0x5A5A5A5A),
        (WH.DOP_RATE_MIN, WH.DOP_RATE_MIN_DEFAULT),
        (WH.DOP_THRESH, WH.DOP_THRESH_DEFAULT),
    ):
        struct.pack_into("<I", data, addr - IMAGE_BASE, val)

    # ---- HARD ANTI-CONTAMINATION ASSERTIONS ON SHIPPING TILE 0 ----
    verify_shipping_tile0_bytes(bytes(data), fac0)

    mkelf(bytes(data), str(out_path))
    return bytes(data), {"gate_len": len(gate_blob), "set_len": len(hooks[WH.ADDR_SET]),
                         "get_len": len(hooks[WH.ADDR_GET])}


def verify_shipping_tile0_bytes(ship, fac0):
    """Hard release gate: fail loudly if ANY unauthorized byte differs from Factory 3.23."""
    assert len(ship) == SHIP_TILE0_SIZE, hex(len(ship))
    allowed = set()
    for base in EXPECTED_FACTORY_SITES:
        for j in range(4):
            allowed.add(base - IMAGE_BASE + j)
    diffs = [i for i in range(len(fac0)) if ship[i] != fac0[i]]
    bad = [hex(IMAGE_BASE + i) for i in diffs if i not in allowed]
    assert not bad, f"UNAUTHORIZED STOCK DIFFS IN SHIPPING TILE 0: {bad}"
    assert set(diffs) <= allowed
    # Double-check all forbidden simulator-stub sites are 100% Factory 3.23
    for addr in FORBIDDEN_STUB_SITES:
        off = addr - IMAGE_BASE
        assert ship[off:off + 4] == fac0[off:off + 4], f"STUB SITE CONTAMINATED: {addr:#x}"
    # BSS gap 0x8EBAC..0x91000 MUST be all zero
    assert all(b == 0 for b in ship[0xEBAC:WH.ADDR_GATE - IMAGE_BASE]), "BSS gap non-zero!"
    # Tail 0x92F40..0x9302C MUST be all zero (section CRC placeholder + padding)
    assert all(b == 0 for b in ship[0x12F40:SHIP_TILE0_SIZE]), "Trailer padding non-zero!"
    # Boot defaults
    assert struct.unpack_from("<I", ship, WH.PCM_ON - IMAGE_BASE)[0] == 0
    assert struct.unpack_from("<I", ship, WH.MQA_ON - IMAGE_BASE)[0] == 0
    assert struct.unpack_from("<I", ship, DFU_LIMIT - IMAGE_BASE)[0] == 0x00080000
    assert ship[SITE_HID_DESC - IMAGE_BASE:SITE_HID_DESC - IMAGE_BASE + 4] == bytes.fromhex("0901b102")
    assert struct.unpack_from("<I", ship, WH.WH_BUILD - IMAGE_BASE)[0] == WH.WH_BUILD_VAL


def main():
    ap = argparse.ArgumentParser(description="Build standalone DO400 PEQ Tile 0 + Tile 1 ELFs from Factory 3.23")
    ap.add_argument("--dump", default=os.environ.get("DO400_DUMP_PATH"),
                    help="Path to your own 2 MiB SPI NOR dump (do400_dump.bin)")
    ap.add_argument("--factory-dir", default=None,
                    help="Directory containing extracted img1_tile0.bin / img1_tile1.bin / image1_swsetup.bin")
    ap.add_argument("--out-dir", default=str(REPO / "build"), help="Output build directory")
    args = ap.parse_args()

    out_dir = Path(args.out_dir).resolve()
    fac_dir = Path(args.factory_dir).resolve() if args.factory_dir else (out_dir / "factory")
    if args.dump:
        extract_factory_from_dump(Path(args.dump).resolve(), fac_dir)

    f_t0 = fac_dir / "img1_tile0.bin"
    f_t1 = fac_dir / "img1_tile1.bin"
    if not f_t0.is_file() or not f_t1.is_file():
        raise SystemExit(
            "Factory 3.23 binaries not found. Pass --dump /path/to/do400_dump.bin "
            "or --factory-dir /path/to/factory_dir"
        )

    for name, want in H_IN.items():
        p = fac_dir / name
        got = sha256(p)
        assert got == want, f"input hash mismatch {p}: {got} != {want}"

    (out_dir / "logs").mkdir(parents=True, exist_ok=True)
    ship_dir = out_dir / "img_fw"
    ship_dir.mkdir(parents=True, exist_ok=True)

    # Shipping image ships with FLAT 0 dB PEQ bands + PCM_ON=0 (safe boot default)
    _ship_t0_bytes, sizes = build_shipping_tile0(
        f_t0, ship_dir / "tile0.elf", out_dir,
        pcm_taps=flat_taps(10), mqa_taps=flat_taps(12))
    # Tile 1 is 100% unmodified Factory 3.23
    mkelf(f_t1.read_bytes(), str(ship_dir / "tile1.elf"))

    manifest = {
        "version": "DO400_PEQ_1.0",
        "wh_build": hex(WH.WH_BUILD_VAL),
        "tile0_shipping_size": SHIP_TILE0_SIZE,
        "tile1_factory_identical": True,
        "stock_sites_patched": [hex(a) for a in sorted(EXPECTED_FACTORY_SITES)],
        "forbidden_stub_sites_verified_factory": [hex(a) for a in FORBIDDEN_STUB_SITES],
        "block_sizes": sizes,
        "hashes": {
            "img_fw/tile0.elf": sha256(ship_dir / "tile0.elf"),
            "img_fw/tile1.elf": sha256(ship_dir / "tile1.elf"),
        },
    }
    (out_dir / "logs" / "build_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
