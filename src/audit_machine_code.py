"""Static machine-code & split-ELF audit on the built DO400 PEQ firmware.

Checks every instruction in pcm_gate (0x91000), webhid_set_hook (0x91480),
webhid_get_hook (0x91E00), all 4 stock patch sites, 4x branch targets, 32-bit
4-byte alignment, atomic SET_ENABLE store order, and xobjdump --split exactness.
"""
import argparse, hashlib, re, shutil, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
from xu_image_an import xtc_env, XTC
import webhid_hooks as WH
import build_fw as B

DP0 = 0x8B220


def parse_dis(path):
    insns = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*(?:\.\S+)?\s*0x([0-9a-f]{8}):\s+([0-9a-f ]+?):\s+(\S+)\s+\([^)]*\)\s*(.*)$", line)
        if not m:
            continue
        addr = int(m.group(1), 16)
        raw = bytes.fromhex(m.group(2).replace(" ", ""))
        op = m.group(3)
        ops = re.sub(r"\s*<[^>]+>\s*$", "", m.group(4)).strip()
        insns.append((addr, raw, op, ops))
    return insns


def br_target_4x(addr, op, ops):
    if op not in ("bu", "bt", "bf", "bl"):
        return None
    m = re.search(r"(-?0x[0-9a-fA-F]+)$", ops)
    if not m:
        return None
    off = int(m.group(1), 16)
    return addr + 4 + 4 * off


def main():
    ap = argparse.ArgumentParser(description="Static XS3A machine-code audit on built DO400 PEQ firmware")
    ap.add_argument("--build-dir", default=str(REPO / "build"), help="Build directory containing logs/tile0.dis")
    args = ap.parse_args()
    build_dir = Path(args.build_dir).resolve()
    dis_path = build_dir / "logs" / "tile0.dis"
    insns = parse_dis(dis_path)
    by_addr = {a: (r, op, ops) for a, r, op, ops in insns}
    ok = True
    out = []

    def chk(name, cond, detail=""):
        nonlocal ok
        out.append(f"   [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
        if not cond:
            ok = False

    out.append(f"=== DO400 PEQ static machine-code audit ({len(insns)} insns parsed)")

    # 1. Stock 4 patch sites (4x branch targets)
    for addr, want_op, want_tgt in [
        (0x80908, "bl", WH.ADDR_SET),
        (0x809B4, "bl", WH.ADDR_GET),
        (0x82B34, "bu", WH.ADDR_GATE),
    ]:
        r, op, ops = by_addr[addr]
        tgt = br_target_4x(addr, op, ops)
        chk(f"site {addr:#x} -> {want_tgt:#x} ({want_op}, 4x)",
            op == want_op and tgt == want_tgt and len(r) == 4 and addr % 4 == 0,
            f"got {op} -> {hex(tgt) if tgt else None}")

    # 2. Forbidden ops & 32-bit alignment across all 3 shipping code blocks
    for name, lo, hi, allowed_exits in [
        ("pcm_gate", WH.ADDR_GATE, WH.ADDR_SET, {0x82B38, 0x82B58}),
        ("webhid_set_hook", WH.ADDR_SET, WH.ADDR_GET, {0x86E7C}),
        ("webhid_get_hook", WH.ADDR_GET, 0x91F00, {0x8AD04}),
    ]:
        blk = [(a, r, op, ops) for a, r, op, ops in insns if lo <= a < hi and not (r == b"\x00\x00")]
        mis32 = [hex(a) for a, r, op, ops in blk if len(r) == 4 and (a % 4) != 0]
        badop = [(hex(a), op) for a, r, op, ops in blk if op in ("bru", "mul", "bl", "bla", "bau", "entsp", "dualentsp")]
        exits = []
        for a, r, op, ops in blk:
            t = br_target_4x(a, op, ops)
            if t is not None and not (lo <= t < hi):
                exits.append((hex(a), op, hex(t)))
        chk(f"{name}: zero 32-bit insn at 2 mod 4", len(mis32) == 0, str(mis32))
        chk(f"{name}: zero bru/mul/bl/bla/bau/entsp", len(badop) == 0, str(badop))
        chk(f"{name}: all external branches land on exact stock entries {sorted(hex(x) for x in allowed_exits)}",
            {int(t, 16) for _, _, t in exits} == allowed_exits, str(exits))

    # 3. Atomic SET_ENABLE verification: ALL 4 stores (PCM_ON, MQA_ON, PCM_MASK, MQA_MASK) must occur
    #    AFTER the last rejection branch to .Lwsparam inside .Lwsc2!
    set_blk = [(a, r, op, ops) for a, r, op, ops in insns if WH.ADDR_SET <= a < WH.ADDR_GET and r != b"\x00\x00"]
    dp_targets = {
        WH.PCM_ON: "PCM_ON", WH.MQA_ON: "MQA_ON", WH.PCM_MASK: "PCM_MASK", WH.MQA_MASK: "MQA_MASK",
    }
    en_stores = []
    for a, r, op, ops in set_blk:
        if op == "stw":
            m = re.search(r"dp\[(0x[0-9a-fA-F]+)\]", ops)
            if m:
                da = DP0 + 4 * int(m.group(1), 16)
                if da in dp_targets:
                    en_stores.append((a, dp_targets[da]))
    # Find .Lwsparam address (ldc r8, 1 followed by ldaw r7, dp[WH_RESP])
    wsparam_addr = None
    for i, (a, r, op, ops) in enumerate(set_blk[:-1]):
        if op == "ldc" and ops == "r8, 0x1" and set_blk[i + 1][2] == "ldaw" and hex(WH.d(WH.WH_RESP)) in set_blk[i + 1][3]:
            wsparam_addr = a
            break
    chk("SET_ENABLE: found all 4 target stores",
        [n for _, n in en_stores] == ["PCM_ON", "MQA_ON", "PCM_MASK", "MQA_MASK"],
        [(hex(a), n) for a, n in en_stores])
    chk("SET_ENABLE: found .Lwsparam", wsparam_addr is not None, hex(wsparam_addr) if wsparam_addr else None)
    # Check no branch to .Lwsparam lies between first and last SET_ENABLE store
    if en_stores and wsparam_addr:
        first_st, last_st = en_stores[0][0], en_stores[-1][0]
        interleaved = [hex(a) for a, r, op, ops in set_blk
                       if first_st < a < last_st and br_target_4x(a, op, ops) == wsparam_addr]
        chk("SET_ENABLE: 100% atomic (zero .Lwsparam rejects after first store)",
            len(interleaved) == 0, f"stores={hex(first_st)}..{hex(last_st)} rejects_between={interleaved}")

    # 4. Optional xobjdump --split check when sim.xe is present
    xe_path = build_dir / "img_fw" / "sim.xe"
    if xe_path.is_file():
        sp = build_dir / "logs" / "xesplit"
        if sp.exists():
            shutil.rmtree(sp)
        sp.mkdir(parents=True)
        p = subprocess.run([str(Path(XTC) / "bin" / "xobjdump.exe"), "-s", "--split-dir", str(sp),
                            str(xe_path)], env=xtc_env(), capture_output=True)
        chk("xobjdump --split on img_fw/sim.xe exited 0", p.returncode == 0)
        t0_sha = hashlib.sha256((build_dir / "img_fw" / "tile0.elf").read_bytes()).hexdigest()
        t1_sha = hashlib.sha256((build_dir / "img_fw" / "tile1.elf").read_bytes()).hexdigest()
        sp0 = hashlib.sha256((sp / "image_n0c0.elf").read_bytes()).hexdigest()
        sp1 = hashlib.sha256((sp / "image_n0c1.elf").read_bytes()).hexdigest()
        chk("split image_n0c0.elf == img_fw/tile0.elf", sp0 == t0_sha)
        chk("split image_n0c1.elf == img_fw/tile1.elf (Factory 3.23)", sp1 == t1_sha)

    out.append("verdict: " + ("PASS" if ok else "FAIL"))
    text = "\n".join(out)
    (build_dir / "logs" / "audit_machine_code.txt").write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
