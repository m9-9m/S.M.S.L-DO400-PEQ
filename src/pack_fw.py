"""DO400 Hardware PEQ - Offline Flash Packing & Anti-Contamination Verification.

Packs ONLY img_fw/{tile0.elf, tile1.elf} ->
  * do400_peq_dfu.bin        (200,896 B = 196.19 KiB < 200 KiB!)
  * do400_peq_flash_2mb.bin  (2,097,152 B full 2 MiB SPI NOR image, when --dump is given)

usage:
  python pack_fw.py --build-dir build [--dump /path/to/do400_dump.bin] [--out-dir build/out]
"""
import argparse, json, os, shutil, struct, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "dfu_app"))
sys.dont_write_bytecode = True

import pack_image1 as PK                      # noqa: E402
import fw_validator as FV                     # noqa: E402
from xu_image_an import xtc_env, XTC          # noqa: E402
import build_fw as B                          # noqa: E402
import webhid_hooks as WH                     # noqa: E402

IMAGE_OFF = 0x2E000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-dir", default=str(REPO / "build"), help="Directory produced by build_fw.py")
    ap.add_argument("--factory-dir", default=None, help="Directory with extracted Factory 3.23 binaries")
    ap.add_argument("--dump", default=os.environ.get("DO400_DUMP_PATH"),
                    help="Optional 2 MiB SPI NOR dump (to also emit 2MB flash image & run xflash --analyze)")
    ap.add_argument("--out-dir", default=None, help="Output directory (default: <build-dir>/out)")
    args = ap.parse_args()

    build_dir = Path(args.build_dir).resolve()
    fac_dir = Path(args.factory_dir).resolve() if args.factory_dir else (build_dir / "factory")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else (build_dir / "out")
    out_dir.mkdir(parents=True, exist_ok=True)

    t0 = build_dir / "img_fw" / "tile0.elf"
    t1 = build_dir / "img_fw" / "tile1.elf"
    sw = fac_dir / "image1_swsetup.bin"
    dfu_path = out_dir / "do400_peq_dfu.bin"

    sec_report = []
    img_logical = PK.pack_image1_from_elfs(str(t0), str(t1), str(sw), version=1,
                                           flash_order=False, section_report=sec_report)
    img_flash = PK.nibble_swap(img_logical)
    dfu_path.write_bytes(img_flash)

    flash_path = None
    base = flash = None
    if args.dump and Path(args.dump).is_file():
        base = Path(args.dump).read_bytes()
        flash = PK.build_flash_image(base, img_flash, image_offset=IMAGE_OFF)
        flash_path = out_dir / "do400_peq_flash_2mb.bin"
        flash_path.write_bytes(flash)

    L = []
    P = L.append
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        P(f"   [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            ok = False

    P("=== DO400 PEQ real-hardware flash packing & anti-contamination gate")
    P(f"packed DFU image: {len(img_logical):,} B (0x{len(img_logical):X}, {len(img_logical)/1024:.2f} KiB)")
    chk("DFU container <= 200 KiB (1-step stock DFU compatible)",
        len(img_logical) <= 204800, f"{len(img_logical)} <= 204800")
    chk("DFU container multiple of 64 B", len(img_logical) % 64 == 0, str(len(img_logical) % 64))

    d = PK.describe_image(img_logical)
    chk("magic 0x00FF51DE / version 1 / 2 cores",
        d["magic"] == 0x00FF51DE and d["version"] == 1 and d["num_cores"] == 2)
    chk("page CRC & image CRC", d["page_crc_ok"] and d["image_crc_ok"])
    chk("switch_setup section CRC", d["switch_setup"]["crc_ok"], hex(d["switch_setup"]["crc"]))
    for i, c in enumerate(d["cores"]):
        chk(f"core {i} section CRC", c["crc_ok"], f"{hex(c['crc'])} size {hex(c['size'])}")

    core0 = img_logical[d["cores"][0]["offset"]:d["cores"][0]["offset"] + d["cores"][0]["size"]]
    core1 = img_logical[d["cores"][1]["offset"]:d["cores"][1]["offset"] + d["cores"][1]["size"]]
    fac0 = (fac_dir / "img1_tile0.bin").read_bytes()
    fac1 = (fac_dir / "img1_tile1.bin").read_bytes()

    chk("PACKED Core 1 == Factory 3.23 img1_tile1.bin (100% byte-exact)",
        core1 == fac1, f"{len(core1)} vs {len(fac1)}")

    allowed = set()
    for base_addr in B.EXPECTED_FACTORY_SITES:
        for j in range(4):
            allowed.add(base_addr - B.IMAGE_BASE + j)
    diffs0 = [i for i in range(len(fac0)) if core0[i] != fac0[i]]
    bad0 = [hex(B.IMAGE_BASE + i) for i in diffs0 if i not in allowed]
    chk("PACKED Core 0 stock span differs ONLY at {0x80908, 0x809B4, 0x82B34, 0x8AF98, 0x8B3EB}",
        len(bad0) == 0 and len(diffs0) > 0, f"diff_bytes={len(diffs0)} unauthorized={bad0}")

    stubs_ok = all(core0[a - B.IMAGE_BASE:a - B.IMAGE_BASE + 4] == fac0[a - B.IMAGE_BASE:a - B.IMAGE_BASE + 4]
                   for a in B.FORBIDDEN_STUB_SITES)
    chk("ZERO xsim EP0 stubs in PACKED Core 0 (all 11 sites == Factory 3.23)", stubs_ok)

    chk("in-firmware DFU limit word (0x8AF98) == 0x00080000",
        struct.unpack_from("<I", core0, B.DFU_LIMIT - B.IMAGE_BASE)[0] == 0x00080000)
    chk("HID Report Descriptor (0x8B3E8) == 09 01 B1 02 (Feature Data,Var,Abs)",
        core0[B.SITE_HID_DESC - B.IMAGE_BASE:B.SITE_HID_DESC - B.IMAGE_BASE + 4] == bytes.fromhex("0901b102"))
    chk(f"boot defaults PCM_ON=0, MQA_ON=0, WH_BUILD=0x{WH.WH_BUILD_VAL:08X}, DOP_RATE_MIN=176400, DOP_THRESH=16, PEQ_RATE_MAX=192000",
        struct.unpack_from("<I", core0, WH.PCM_ON - B.IMAGE_BASE)[0] == 0
        and struct.unpack_from("<I", core0, WH.MQA_ON - B.IMAGE_BASE)[0] == 0
        and struct.unpack_from("<I", core0, WH.WH_BUILD - B.IMAGE_BASE)[0] == WH.WH_BUILD_VAL
        and struct.unpack_from("<I", core0, WH.DOP_RATE_MIN - B.IMAGE_BASE)[0] == 176400
        and struct.unpack_from("<I", core0, WH.DOP_THRESH - B.IMAGE_BASE)[0] == 16
        and struct.unpack_from("<I", core0, WH.PEQ_RATE_MAX - B.IMAGE_BASE)[0] == 192000)

    if flash is not None and base is not None:
        chk("loader + factory image0 untouched (0x000000..0x2E000)", flash[:IMAGE_OFF] == base[:IMAGE_OFF])
        chk("flash tail beyond image is 0xFF", all(b == 0xFF for b in flash[IMAGE_OFF + len(img_flash):]))

    fv = FV.validate_file(str(dfu_path))
    chk("fw_validator.py ALL GOOD (0 failures, 0 warnings, 1-step direct)",
        fv.ok and fv.write_allowed and len(fv.failures) == 0 and len(fv.warnings) == 0
        and not fv.requires_bootstrap,
        f"verdict={fv.verdict} fail={len(fv.failures)} warn={len(fv.warnings)} class={fv.size_class}")

    goods = fails = None
    xflash_exe = Path(XTC) / "bin" / "xflash.exe"
    if flash_path is not None and xflash_exe.is_file():
        adir = out_dir / "analyze"
        if adir.exists():
            shutil.rmtree(adir)
        adir.mkdir()
        p = subprocess.run([str(xflash_exe), "--analyze",
                            str(flash_path), "--noinq", "--factory-version", "15.1"],
                           cwd=adir, env=xtc_env(), capture_output=True)
        txt = (p.stdout + p.stderr).decode(errors="replace")
        (out_dir / "xflash_analyze.txt").write_text(txt, encoding="utf-8")
        goods = txt.count("(GOOD)")
        fails = txt.count("(FAIL)")
        chk("xflash --analyze --noinq: 11 GOOD, 0 FAIL", goods == 11 and fails == 0,
            f"GOOD={goods} FAIL={fails}")

    man = dict(
        dfu=dict(path=str(dfu_path), size=dfu_path.stat().st_size, sha256=PK.sha256(dfu_path.read_bytes())),
        flash=(dict(path=str(flash_path), size=len(flash), sha256=PK.sha256(flash)) if flash_path else None),
        xflash=(dict(good=goods, fail=fails) if goods is not None else None),
        fw_validator=dict(verdict=fv.verdict, fails=len(fv.failures), warns=len(fv.warnings),
                          requires_bootstrap=fv.requires_bootstrap),
        core_sizes=[hex(c["size"]) for c in d["cores"]],
        ok=ok,
    )
    (out_dir / "pack_result.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    P("verdict: " + ("PASS" if ok else "FAIL"))
    print("\n".join(L))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
