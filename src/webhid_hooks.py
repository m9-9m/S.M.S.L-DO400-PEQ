"""DO400 real-hardware WebHID PEQ1 v1 hooks + 3-layer DoP guard.

Design notes:
  * Placed cleanly right after Tile 0 BSS (0x909C4) at 0x91480..0x92F40 so the entire 2-tile
    shipping DFU container (with 100% unmodified Factory 3.23 Tile 1) is 200,896 B (196.2 KiB),
    fitting under the stock 200 KiB limit while also setting 0x8AF98 = 512 KiB.
  * WH_BUILD = 0x38000101.
  * SET_ENABLE validates ALL requested fields BEFORE committing any store
    (100% atomic; invalid mask never mutates PCM_ON/MQA_ON).
  * CLEAR_HISTORY / SET_BAND(clear=1) on target 0 also clears PEQ_OPEN = 0 so the next PCM sample
    runs a clean FIRST reset.
  * Optional test-only driver/recorder live at 0x94000..0x95800 (outside the shipping 0x9302C
    boundary) and are never included in shipping builds.
"""
import struct

DP0 = 0x8B220

# Shipping code addresses (Tile 0, after BSS 0x8EBA8..0x909C4)
ADDR_GATE = 0x91000           # 0x91000..0x91480 (1152 B)
ADDR_SET = 0x91480            # 0x91480..0x91E00 (2432 B)
ADDR_GET = 0x91E00            # 0x91E00..0x91F00 (256 B)

# Shipping state blocks
PCM_STATE = 0x92000           # 0x92000..0x92780 (1920 B; 10 bands L+R end at 0x925B0)
PEQ_OPEN = 0x92034
DEC = 0x92038
PCM_PREAMP = 0x92044
CLIP_HI = 0x92048
CLIP_LO = 0x9204C
R_OUT_TMP = 0x92050
PCM_MASK = 0x92060

MQA_STATE = 0x92780           # 0x92780..0x92E00 (1664 B; protocol-compat storage, MQA_ON=0)
MQA_PREAMP = 0x927C4
MQA_MASK = 0x927E0

# Shipping control & WebHID buffer block (0x92E00..0x92F40)
PCM_ON = 0x92E00
MQA_ON = 0x92E04
PCM_SEQ = 0x92E08
OBS_L = 0x92E0C
OBS_R = 0x92E10
MQA_SEQ = 0x92E14
OCC_CUR = 0x92E18
PEQ_RATE_MAX = 0x92E1C
PEQ_LAST_RATE = 0x92E20

WH_MAGIC = 0x92E40
DOP_LAST = 0x92E44
DOP_RUN = 0x92E48
DOP_HOLD = 0x92E4C
DOP_HITS = 0x92E50
DOP_PREV = 0x92E54
WH_RESP_VALID = 0x92E58
WH_HITS = 0x92E5C
WH_ERRS = 0x92E60
WH_LASTCMD = 0x92E64
WH_BUILD = 0x92E68
WH_XORC = 0x92E6C
DOP_RATE_MIN = 0x92E70
DOP_THRESH = 0x92E74

WH_PKT = 0x92E80              # 16 words (64 B)
WH_RESP = 0x92EC0             # 16 words (64 B)
WH_SAVE = 0x92F00             # 16 words (64 B)

RATEW = 0x8D2F8
L_WORD = 0x90770
R_WORD = 0x90774

DOP_RATE_MIN_DEFAULT = 176400
DOP_THRESH_DEFAULT = 16
DOP_HOLD_PAIRS = 32
PEQ_RATE_MAX_DEFAULT = 192000
WH_BUILD_VAL = 0x38000101

BAND0 = 0x100                 # byte offset of band 0 inside a state block (word 64)
BAND_STRIDE = 40              # 10 words per band block
CH_STRIDE = 800               # 200 words per channel

# Test-only addresses (STRICTLY OUTSIDE shipping Tile 0 end 0x9302C!)
ADDR_DRV = 0x94000            # 0x94000..0x94300
ADDR_REC = 0x94300            # 0x94300..0x943C0
ADDR_WRAP = 0x943C0           # 0x943C0..0x94400 (calls wh_drv_start then bu ADDR_GATE)
WH_T = 0x94400
WH_SEEN = 0x94404
WH_N = 0x94408
WH_LEN = 0x9440C
WH_GREC = 0x94420             # 5 words
WH_GETBUF = 0x94440           # 16 words
FB_R0 = 0x94480               # 3 words
WH_SAVE2 = 0x944C0            # 16 words
WH_LOG = 0x94500              # 32 records x 32 words = 4096 B (0x94500..0x95500)
LOGW = 32
DUMP_LO = 0x92000
DUMP_HI = 0x95500


def d(a):
    assert (a - DP0) % 4 == 0 and 0 <= (a - DP0) // 4 < 0x4000, hex(a)
    return (a - DP0) // 4


def enc_bl(pc, target):
    """32-bit `bl`, dual-issue code: 20-bit field = delta/4."""
    delta = target - (pc + 4)
    assert delta % 4 == 0, (hex(pc), hex(target), delta)
    n = abs(delta) // 4
    assert n < (1 << 20), (hex(pc), hex(target), n)
    return struct.pack("<HH", 0xF000 | (n >> 10), 0xD000 | (0x0400 if delta < 0 else 0) | (n & 0x3FF))


def enc_bu_(pc, target):
    """32-bit unconditional `bu` (4-byte offset unit)."""
    delta = target - (pc + 4)
    assert delta % 4 == 0, (hex(pc), hex(target), delta)
    n = abs(delta) // 4
    assert n < 0x80000, (hex(pc), hex(target), n)
    return struct.pack("<HH", 0xF000 | (n >> 6), 0x7300 | (0x0400 if delta < 0 else 0) | (n & 0x3F))


def dec_bl(pc, at):
    hw0, hw1 = struct.unpack_from("<HH", at, 0)
    assert (hw0 & 0xF000) == 0xF000 and (hw1 & 0xF000) == 0xD000, (hex(hw0), hex(hw1))
    n = ((hw0 & 0x0FFF) << 10) | (hw1 & 0x03FF)
    delta = -n if (hw1 & 0x0400) else n
    return pc + 4 + 4 * delta


# --------------------------------------------------------------------------- DoP guard
def dop_guard_source():
    """3-layer DoP guard (rate gate >= 176400, 16-pair strict 0x05/0xFA alternation, hold=32).

    Runs inside pcm_gate AFTER r0..r11 are saved and PCM_ON != 0 & RATEW <= PEQ_RATE_MAX
    are verified.  Uses r0/r1/r3 only.
    """
    v = {k: d(a) for k, a in (
        ("L", L_WORD), ("R", R_WORD), ("LAST", DOP_LAST), ("RUN", DOP_RUN),
        ("HOLD", DOP_HOLD), ("HITS", DOP_HITS), ("PREV", DOP_PREV),
        ("RATEW", RATEW), ("RMIN", DOP_RATE_MIN), ("THRESH", DOP_THRESH))}
    return [
        f"ldw r3,dp[{v['RATEW']}]",
        f"ldw r1,dp[{v['RMIN']}]",
        "lsu r3,r3,r1",
        "bt r3,.Ldopno",
        f"ldw r3,dp[{v['L']}]",
        f"ldw r1,dp[{v['R']}]",
        "shr r3,r3,24",
        "shr r1,r1,24",
        "eq r0,r3,r1",
        "bf r0,.Ldopno",
        "eq r0,r3,5",
        "bt r0,.Ldopmark",
        "ldc r0,0xfa",
        "eq r0,r3,r0",
        "bf r0,.Ldopno",
        ".Ldopmark:",
        f"ldw r0,dp[{v['PREV']}]",
        "bf r0,.Ldopfirst",
        f"ldw r0,dp[{v['LAST']}]",
        "eq r0,r0,r3",
        "bt r0,.Ldopsame",
        f"ldw r0,dp[{v['RUN']}]",
        "add r0,r0,1",
        "bu .Ldopst",
        ".Ldopsame:",
        "ldc r0,1",
        "bu .Ldopst",
        ".Ldopfirst:",
        "ldc r0,1",
        ".Ldopst:",
        f"stw r0,dp[{v['RUN']}]",
        f"stw r3,dp[{v['LAST']}]",
        "ldc r1,1",
        f"stw r1,dp[{v['PREV']}]",
        f"ldw r1,dp[{v['THRESH']}]",
        "lsu r0,r0,r1",
        "bt r0,.Ldopdone",
        f"ldc r0,{DOP_HOLD_PAIRS}",
        f"stw r0,dp[{v['HOLD']}]",
        f"ldw r0,dp[{v['HITS']}]",
        "add r0,r0,1",
        f"stw r0,dp[{v['HITS']}]",
        "bu .Ldopdone",
        ".Ldopno:",
        "ldc r0,0",
        f"stw r0,dp[{v['PREV']}]",
        f"stw r0,dp[{v['RUN']}]",
        f"ldw r0,dp[{v['HOLD']}]",
        "bf r0,.Ldopdone",
        "sub r0,r0,1",
        f"stw r0,dp[{v['HOLD']}]",
        ".Ldopdone:",
    ]


# --------------------------------------------------------------------------- helpers
def _block_writes(base):
    out = []
    for i in range(5):
        out += [f"ldw r5,r0[{3 + i}]", f"stw r5,{base}[{i}]"]
    out += ["ldc r5,0"] + [f"stw r5,{base}[{5 + j}]" for j in range(5)]
    return out


def _resp_store(i, reg="r6"):
    if i <= 11:
        return [f"stw {reg},r7[{i}]"]
    return [f"ldc r4,{i}", f"stw {reg},r7[r4]"]


def _status_fill():
    out = [f"ldaw r7,dp[{d(WH_RESP)}]"]
    for i, addr in enumerate((PCM_ON, MQA_ON, PCM_MASK, MQA_MASK, RATEW, PCM_PREAMP,
                              MQA_PREAMP, PCM_SEQ, MQA_SEQ, DOP_HITS, OCC_CUR, WH_BUILD)):
        out += [f"ldw r6,dp[{d(addr)}]"] + _resp_store(3 + i)
    return out


def _save(area, n=11):
    return [f"stw r{i},dp[{d(area) + i}]" for i in range(n)]


def _restore(area, n=11):
    return [f"ldw r{i},dp[{d(area) + i}]" for i in range(n)]


def _restore_return():
    return _restore(WH_SAVE) + ["retsp 0"]


def _hdr_word1(cmd_reg="r6", flag_regs=("r8", "r9")):
    r8, r9 = flag_regs
    return [f"ldc {r8},1", f"shl {r8},{r8},16", f"or {cmd_reg},{cmd_reg},{r8}",
            f"shl {r9},{r9},24", f"or {cmd_reg},{cmd_reg},{r9}"]


# --------------------------------------------------------------------------- SET hook
def set_hook_source():
    """webhid_set_hook: r0 = buf, r1 = 64, r2 = wLength.  Frameless, atomic SET_ENABLE."""
    L = [".text", ".issue_mode dual", ".align 4",
         ".global webhid_set_hook", "webhid_set_hook:"]
    L += _save(WH_SAVE)
    # ---- magic + length gate
    L += ["ldw r5,r0[0]", f"ldw r6,dp[{d(WH_MAGIC)}]", "eq r5,r5,r6",
          "bf r5,wh_set_fallback",
          "ldc r6,64", "eq r5,r2,r6", "bf r5,wh_set_fallback"]
    # ---- clear response buffer
    L += [f"ldaw r7,dp[{d(WH_RESP)}]", "ldc r3,0", "ldc r4,0", ".Lwsz:",
          "stw r3,r7[r4]", "add r4,r4,1", "ldc r6,16", "eq r5,r4,r6", "bf r5,.Lwsz"]
    # ---- request checksum
    L += ["ldc r3,0", "ldc r4,0", ".Lwss:", "ldw r5,r0[r4]", "add r3,r3,r5",
          "add r4,r4,1", "ldc r6,15", "eq r5,r4,r6", "bf r5,.Lwss",
          f"ldw r5,dp[{d(WH_XORC)}]", "xor r3,r3,r5", "ldw r5,r0[r4]",
          "eq r3,r3,r5", "bt r3,.Lwsok", "bu .Lwsbad",
          ".Lwsok:"]
    # ---- header
    L += [f"ldw r6,dp[{d(WH_MAGIC)}]", f"ldaw r7,dp[{d(WH_RESP)}]", "stw r6,r7[0]",
          "ldw r6,r0[1]", "ldc r8,0xff", "and r6,r6,r8",
          f"stw r6,dp[{d(WH_LASTCMD)}]",
          f"ldw r8,dp[{d(DOP_HOLD)}]", "ldc r9,3", "eq r8,r8,0", "bt r8,.Lwsfl",
          "ldc r9,7", ".Lwsfl:"]
    L += _hdr_word1()
    L += ["stw r6,r7[1]", "ldw r6,r0[2]", "stw r6,r7[2]"]
    # ---- dispatch
    L += ["ldw r6,r0[1]", "ldc r8,0xff", "and r8,r6,r8"]
    for cmd in range(1, 7):
        L += [f"ldc r9,{cmd}", "eq r9,r8,r9", f"bt r9,.Lwsc{cmd}"]
    L += ["ldc r8,3", "bu .Lwserrfin"]

    # ---- 0x01 GET_STATUS
    L += [".Lwsc1:"] + _status_fill() + ["bu .Lwsfin"]

    # ---- 0x02 SET_ENABLE (atomic: validate ALL requested fields first, then commit)
    L += [".Lwsc2:", "ldw r6,r0[3]", "ldc r8,15", "and r9,r6,r8", "eq r9,r9,r6",
          "bf r9,.Lwsparam",
          # Pass 1: validate
          "ldc r8,1", "and r9,r6,r8", "bf r9,.Lwsv1",
          "ldw r10,r0[4]", "ldc r11,1", "lsu r9,r11,r10", "bt r9,.Lwsparam",
          ".Lwsv1:",
          "ldc r8,2", "and r9,r6,r8", "bf r9,.Lwsv2",
          "ldw r10,r0[5]", "ldc r11,1", "lsu r9,r11,r10", "bt r9,.Lwsparam",
          ".Lwsv2:",
          "ldc r8,4", "and r9,r6,r8", "bf r9,.Lwsv3",
          "ldw r10,r0[6]", "ldc r8,3", "and r9,r10,r8", "eq r9,r9,r8", "bf r9,.Lwsparam",
          "ldc r8,10", "shr r9,r10,r8", "bt r9,.Lwsparam",
          ".Lwsv3:",
          "ldc r8,8", "and r9,r6,r8", "bf r9,.Lwsv4",
          "ldw r10,r0[7]", "ldc r8,3", "and r9,r10,r8", "eq r9,r9,r8", "bf r9,.Lwsparam",
          "ldc r8,12", "shr r9,r10,r8", "bt r9,.Lwsparam",
          ".Lwsv4:",
          # Pass 2: commit (all checks passed)
          "ldc r8,1", "and r9,r6,r8", "bf r9,.Lwse1",
          "ldw r10,r0[4]", f"stw r10,dp[{d(PCM_ON)}]", ".Lwse1:",
          "ldc r8,2", "and r9,r6,r8", "bf r9,.Lwse2",
          "ldw r10,r0[5]", f"stw r10,dp[{d(MQA_ON)}]", ".Lwse2:",
          "ldc r8,4", "and r9,r6,r8", "bf r9,.Lwse3",
          "ldw r10,r0[6]", f"stw r10,dp[{d(PCM_MASK)}]", ".Lwse3:",
          "ldc r8,8", "and r9,r6,r8", "bf r9,.Lwse4",
          "ldw r10,r0[7]", f"stw r10,dp[{d(MQA_MASK)}]", ".Lwse4:"]
    L += _status_fill() + ["bu .Lwsfin"]

    # ---- 0x03 SET_BAND
    L += [".Lwsc3:",
          "ldw r6,r0[1]", "ldc r8,0xff",
          "shr r9,r6,8", "and r9,r9,r8",                    # target
          "ldc r11,1", "lsu r11,r11,r9", "bt r11,.Lwsparam",
          "shr r10,r6,16", "and r10,r10,r8",                # channel
          "ldc r11,2", "lsu r11,r11,r10", "bt r11,.Lwsparam",
          "shr r11,r6,24",                                  # band
          "ldc r6,10", "bf r9,.Lwsb10", "ldc r6,12", ".Lwsb10:",
          "lsu r6,r11,r6", "bf r6,.Lwsparam",
          "ldw r6,r0[8]", "ldc r8,1", "and r8,r6,r8",       # opts must be 0 or 1
          "eq r6,r8,r6", "bf r6,.Lwsparam",
          "ldc r6,2", "eq r6,r10,r6", "bf r6,.Lwsbch", "ldc r10,0", ".Lwsbch:",
          "shl r6,r11,5", "shl r8,r11,3", "add r11,r6,r8",  # 40*band
          "bf r10,.Lwsbch0", "ldc r6,800", "add r11,r11,r6",
          ".Lwsbch0:",
          f"ldc r6,{BAND0}", "add r11,r11,r6",
          "bf r9,.Lwsbt0", f"ldaw r4,dp[{d(MQA_STATE)}]", "bu .Lwsbto",
          ".Lwsbt0:", f"ldaw r4,dp[{d(PCM_STATE)}]", ".Lwsbto:",
          "add r4,r4,r11"]
    L += _block_writes("r4")
    L += ["ldw r6,r0[1]", "shr r6,r6,16", "ldc r8,0xff", "and r6,r6,r8",
          "ldc r8,2", "eq r6,r6,r8", "bf r6,.Lwsb2",
          "ldc r8,800", "add r6,r4,r8"]
    L += _block_writes("r6")
    L += [".Lwsb2:", "ldw r6,r0[8]", "ldc r8,1", "and r6,r6,r8", "bf r6,.Lwsbresp"]
    L += _clear_inline("bopt")
    L += [".Lwsbresp:", f"ldaw r7,dp[{d(WH_RESP)}]"]
    for i in range(5):
        L += [f"ldw r6,r0[{3 + i}]", f"stw r6,r7[{3 + i}]"]
    L += ["ldw r6,r0[1]", "ldc r8,0xff",
          "shr r7,r6,8", "and r7,r7,r8", "shl r7,r7,8",
          "shr r9,r6,16", "and r9,r9,r8", "or r7,r7,r9", "shl r7,r7,8",
          "shr r9,r6,24", "or r7,r7,r9",
          f"ldaw r6,dp[{d(WH_RESP)}]", "stw r7,r6[8]",
          "bu .Lwsfin"]

    # ---- 0x04 GET_BAND
    L += [".Lwsc4:",
          "ldw r6,r0[1]", "ldc r8,0xff",
          "shr r9,r6,8", "and r9,r9,r8",
          "ldc r11,1", "lsu r11,r11,r9", "bt r11,.Lwsparam",
          "shr r10,r6,16", "and r10,r10,r8",
          "ldc r11,1", "lsu r11,r11,r10", "bt r11,.Lwsparam",
          "shr r11,r6,24",
          "ldc r6,10", "bf r9,.Lwsg10", "ldc r6,12", ".Lwsg10:",
          "lsu r6,r11,r6", "bf r6,.Lwsparam",
          "shl r6,r11,5", "shl r8,r11,3", "add r11,r6,r8",
          "bf r10,.Lwsgch0", "ldc r6,800", "add r11,r11,r6",
          ".Lwsgch0:",
          f"ldc r6,{BAND0}", "add r11,r11,r6",
          "bf r9,.Lwsgt0", f"ldaw r4,dp[{d(MQA_STATE)}]", "bu .Lwsgto",
          ".Lwsgt0:", f"ldaw r4,dp[{d(PCM_STATE)}]", ".Lwsgto:",
          "add r4,r4,r11",
          f"ldaw r7,dp[{d(WH_RESP)}]"]
    for i in range(5):
        L += [f"ldw r6,r4[{i}]", f"stw r6,r7[{3 + i}]"]
    L += ["ldw r6,r0[1]", "ldc r8,0xff",
          "shr r7,r6,8", "and r7,r7,r8", "shl r7,r7,8",
          "shr r9,r6,16", "and r9,r9,r8", "or r7,r7,r9", "shl r7,r7,8",
          "shr r9,r6,24", "or r7,r7,r9",
          f"ldaw r6,dp[{d(WH_RESP)}]", "stw r7,r6[8]",
          "bu .Lwsfin"]

    # ---- 0x05 CLEAR_HISTORY
    L += [".Lwsc5:",
          "ldw r6,r0[1]", "ldc r8,0xff", "shr r9,r6,8", "and r9,r9,r8",
          "ldc r11,1", "lsu r11,r11,r9", "bt r11,.Lwsparam"]
    L += _clear_inline("clr")
    L += _status_fill() + ["bu .Lwsfin"]

    # ---- 0x06 SET_PREAMP
    L += [".Lwsc6:",
          "ldw r6,r0[1]", "ldc r8,0xff", "shr r9,r6,8", "and r9,r9,r8",
          "ldc r11,1", "lsu r11,r11,r9", "bt r11,.Lwsparam",
          "ldw r10,r0[3]",
          "ldc r8,0x2000", "shl r8,r8,16", "lsu r6,r8,r10", "bt r6,.Lwsparam",
          "bf r9,.Lwsppm", f"stw r10,dp[{d(MQA_PREAMP)}]", "bu .Lwsppd",
          ".Lwsppm:", f"stw r10,dp[{d(PCM_PREAMP)}]", ".Lwsppd:"]
    L += _status_fill() + ["bu .Lwsfin"]

    # ---- bad checksum
    L += [".Lwsbad:", f"ldw r6,dp[{d(WH_ERRS)}]", "add r6,r6,1", f"stw r6,dp[{d(WH_ERRS)}]",
          f"ldw r6,dp[{d(WH_MAGIC)}]", f"ldaw r7,dp[{d(WH_RESP)}]", "stw r6,r7[0]",
          "ldw r6,r0[1]", "ldc r8,0xff", "and r6,r6,r8",
          f"ldw r8,dp[{d(DOP_HOLD)}]", "ldc r9,3", "eq r8,r8,0", "bt r8,.Lwsbfl",
          "ldc r9,7", ".Lwsbfl:"]
    L += _hdr_word1()
    L += ["ldc r8,2", "shl r8,r8,8", "or r6,r6,r8",
          "stw r6,r7[1]", "ldw r6,r0[2]", "stw r6,r7[2]",
          "bu .Lwsfin"]

    # ---- error exits
    L += [".Lwsparam:", "ldc r8,1",
          ".Lwserrfin:",
          f"ldaw r7,dp[{d(WH_RESP)}]", "shl r8,r8,8", "ldw r5,r7[1]", "or r5,r5,r8",
          "stw r5,r7[1]",
          "bu .Lwsfin"]

    # ---- finish
    L += [".Lwsfin:", f"ldaw r7,dp[{d(WH_RESP)}]",
          "ldc r3,0", "ldc r4,0", ".Lwfs:", "ldw r5,r7[r4]", "add r3,r3,r5",
          "add r4,r4,1", "ldc r6,15", "eq r5,r4,r6", "bf r5,.Lwfs",
          f"ldw r5,dp[{d(WH_XORC)}]", "xor r3,r3,r5", "stw r3,r7[r4]",
          "ldc r5,1", f"stw r5,dp[{d(WH_RESP_VALID)}]",
          f"ldw r5,dp[{d(WH_HITS)}]", "add r5,r5,1", f"stw r5,dp[{d(WH_HITS)}]"]
    L += _restore_return()

    # ---- fallback to stock MQA HID SET
    L += [".align 4", ".global wh_set_fallback", "wh_set_fallback:"]
    L += _restore(WH_SAVE)
    L += [".global wh_set_fb_site", "wh_set_fb_site:", "nop", ".space 2"]
    L += [".global wh_set_end", "wh_set_end:"]
    return L


def _clear_inline(tag):
    """Zero words 5..9 of every band block of both channels, target in r9.
    For target 0 (PCM), also clears PEQ_OPEN so the next pair runs clean FIRST."""
    return ["bf r9,.Lclr_%s_p" % tag,
            f"ldaw r4,dp[{d(MQA_STATE)}]", "ldc r11,12", "ldc r10,320", "bu .Lclr_%s_g" % tag,
            ".Lclr_%s_p:" % tag,
            "ldc r6,0", f"stw r6,dp[{d(PEQ_OPEN)}]",
            f"ldaw r4,dp[{d(PCM_STATE)}]", "ldc r11,10", "ldc r10,400",
            ".Lclr_%s_g:" % tag,
            f"ldc r6,{BAND0}", "add r4,r4,r6", "ldc r6,0", "ldc r5,2",
            ".Lclr_%s_c:" % tag,
            "mov r3,r11",
            ".Lclr_%s_b:" % tag,
            "stw r6,r4[5]", "stw r6,r4[6]", "stw r6,r4[7]", "stw r6,r4[8]", "stw r6,r4[9]",
            f"ldc r8,{BAND_STRIDE}", "add r4,r4,r8", "sub r3,r3,1", "bt r3,.Lclr_%s_b" % tag,
            "add r4,r4,r10", "sub r5,r5,1", "bt r5,.Lclr_%s_c" % tag]


# --------------------------------------------------------------------------- GET hook
def get_hook_source():
    return [".text", ".issue_mode dual", ".align 4",
            ".global webhid_get_hook", "webhid_get_hook:",
            f"stw r4,dp[{d(WH_SAVE) + 12}]",
            f"stw r5,dp[{d(WH_SAVE) + 13}]",
            f"ldw r4,dp[{d(WH_RESP_VALID)}]",
            "bf r4,.Lwgn",
            "ldc r5,0", f"stw r5,dp[{d(WH_RESP_VALID)}]",
            f"ldaw r2,dp[{d(WH_RESP)}]",
            ".Lwgn:",
            f"ldw r4,dp[{d(WH_SAVE) + 12}]",
            f"ldw r5,dp[{d(WH_SAVE) + 13}]",
            ".global wh_get_tail", "wh_get_tail:", "nop", ".space 2",
            ".global wh_get_end", "wh_get_end:"]


# --------------------------------------------------------------------------- assembler
def _elf_sections(b):
    shoff = struct.unpack_from("<I", b, 32)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", b, 46)
    secs = [struct.unpack_from("<10I", b, shoff + shentsize * i) for i in range(shnum)]
    st = secs[shstrndx]
    names = b[st[4]:st[4] + st[5]]
    return secs, names


def _elf_symbols(b, secs, names):
    symtab = next(s for s in secs if names[s[0]:].split(b"\0")[0] == b".symtab")
    strtab = secs[symtab[6]]
    sn = b[strtab[4]:strtab[4] + strtab[5]]
    out = {}
    for i in range(symtab[5] // 16):
        nm, val, size, info, other, shndx = struct.unpack_from("<IIIBBH", b, symtab[4] + 16 * i)
        sname = sn[nm:].split(b"\0")[0].decode()
        if sname:
            out[sname] = (val, shndx)
    return out


def _run_xtc(outdir, tool, *args):
    import subprocess
    from pathlib import Path
    from xu_image_an import XTC, xtc_env
    p = subprocess.run([str(Path(XTC) / "bin" / tool), *map(str, args)],
                       cwd=outdir, env=xtc_env(), capture_output=True)
    if p.returncode:
        raise RuntimeError(p.stderr.decode(errors="replace") + p.stdout.decode(errors="replace"))


def _assemble(outdir, stem, source, entry, end):
    from pathlib import Path
    outdir = Path(outdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    src = list(source) + [f".global {entry}", f".global {end}",
                          ".global main", "main:", "waiteu"]
    (outdir / (stem + ".S")).write_text("\n".join(src) + "\n")
    _run_xtc(outdir, "xcc.exe", "-target=XCORE-AI-EXPLORER", stem + ".S", "-o", stem + ".elf")
    sp = outdir / (stem + "_split")
    sp.mkdir(exist_ok=True)
    _run_xtc(outdir, "xobjdump.exe", "-s", "--split-dir", str(sp), stem + ".elf")
    for ep in sorted(sp.glob("image_n0c0*.elf")):
        b = ep.read_bytes()
        if b[:4] != b"\x7fELF":
            continue
        secs, names = _elf_sections(b)
        syms = _elf_symbols(b, secs, names)
        if entry not in syms:
            continue
        val, shndx = syms[entry]
        eend, eshndx = syms[end]
        assert shndx == eshndx
        sec = secs[shndx]
        base = sec[3]
        blob = b[sec[4]:sec[4] + sec[5]]
        assert base <= val <= eend <= base + sec[5], (hex(base), hex(val), hex(eend))
        code = bytearray(blob[val - base:eend - base])
        labels = {k: v[0] - val for k, v in syms.items() if v[1] == shndx and val <= v[0] <= eend}
        return code, labels
    raise RuntimeError("split ELF with %s not found" % entry)


def _assemble_aligned(outdir, stem, mk, entry, end, sites):
    pad = {s: 0 for s in sites}
    for _ in range(4 * len(sites) + 1):
        code, lab = _assemble(outdir, stem, mk(pad), entry, end)
        bad = [s for s in sites if lab[s] % 4]
        if not bad:
            return code, lab
        for s in bad:
            pad[s] += 2
    raise RuntimeError("could not align %s in %s" % (sites, stem))


def _pad_line(pad, site):
    return [f".space {pad[site]}"] if pad[site] else []


def build_hooks(outdir, orig_set, orig_get, test=False):
    blocks = {}
    rec = {}
    if test:
        rimg, rlab = _assemble(outdir, "wh_rec", recorder_source(), "wh_rec_start", "wh_rec_end")
        rec = {"get": ADDR_REC + rlab["wh_rec_get"], "set": ADDR_REC + rlab["wh_rec_set"]}

    def mk_set(pad):
        src = set_hook_source()
        out = []
        for line in src:
            out.append(line)
            if line == "wh_set_fb_site:":
                out += _pad_line(pad, "wh_set_fb_site")
        return out
    img, lab = _assemble_aligned(outdir, "wh_set", mk_set, "webhid_set_hook", "wh_set_end",
                                 ["wh_set_fb_site"])
    fb = lab["wh_set_fb_site"]
    assert img[fb:fb + 2] == b"\xff\x17", img[fb:fb + 2].hex()
    img[fb:fb + 4] = enc_bu_(ADDR_SET + fb, rec["set"] if test else orig_set)
    assert ADDR_SET + len(img) <= ADDR_GET, (hex(ADDR_SET + len(img)), hex(ADDR_GET))
    blocks[ADDR_SET] = bytes(img)

    def mk_get(pad):
        src = get_hook_source()
        out = []
        for line in src:
            out.append(line)
            if line == "wh_get_tail:":
                out += _pad_line(pad, "wh_get_tail")
        return out
    gimg, glab = _assemble_aligned(outdir, "wh_get", mk_get, "webhid_get_hook", "wh_get_end",
                                   ["wh_get_tail"])
    tl = glab["wh_get_tail"]
    assert gimg[tl:tl + 2] == b"\xff\x17", gimg[tl:tl + 2].hex()
    gimg[tl:tl + 4] = enc_bu_(ADDR_GET + tl, rec["get"] if test else orig_get)
    assert ADDR_GET + len(gimg) <= 0x92000, (hex(ADDR_GET + len(gimg)), "0x92000")
    blocks[ADDR_GET] = bytes(gimg)

    if test:
        dsrc = test_driver_source()

        def mk_drv(pad):
            out = []
            for line in dsrc:
                out.append(line)
                if line in ("wh_drv_set:", "wh_drv_get:"):
                    out += _pad_line(pad, line.rstrip(":"))
            return out
        dimg, dlab = _assemble_aligned(outdir, "wh_drv", mk_drv, "wh_drv_start", "wh_drv_end",
                                       ["wh_drv_set", "wh_drv_get"])
        for name, tgt in (("wh_drv_set", ADDR_SET), ("wh_drv_get", ADDR_GET)):
            at = dlab[name]
            assert dimg[at:at + 2] == b"\xff\x17", (name, dimg[at:at + 2].hex())
            dimg[at:at + 4] = enc_bl(ADDR_DRV + at, tgt)
        assert ADDR_DRV + len(dimg) <= ADDR_REC
        blocks[ADDR_DRV] = bytes(dimg)
        assert ADDR_REC + len(rimg) <= ADDR_WRAP
        blocks[ADDR_REC] = bytes(rimg)
        # wrapper at ADDR_WRAP: bl ADDR_DRV ; bu ADDR_GATE
        wbuf = bytearray(8)
        wbuf[0:4] = enc_bl(ADDR_WRAP, ADDR_DRV)
        wbuf[4:8] = enc_bu_(ADDR_WRAP + 4, ADDR_GATE)
        blocks[ADDR_WRAP] = bytes(wbuf)
        blocks["rec"] = rec
    return blocks


# --------------------------------------------------------------------------- test harness (NEVER SHIPPED)
HS_BAND3 = PCM_STATE + BAND0 + BAND_STRIDE * 3
SNAP = [
    (PCM_ON, 25), (MQA_ON, 26), (PCM_MASK, 27), (MQA_MASK, 28), (PCM_PREAMP, 29),
    (PCM_STATE + BAND0, 30),
]
SNAP_SUM = (HS_BAND3, 31)


def test_driver_source():
    L = [".text", ".issue_mode dual", ".align 4",
         ".global wh_drv_start", "wh_drv_start:",
         "dualentsp 2", "stw lr,sp[0]"]
    L += [f"stw r{i},dp[{d(WH_SAVE2) + i}]" for i in range(12)]
    L += [f"ldw r10,dp[{d(WH_T)}]", f"ldw r11,dp[{d(WH_SEEN)}]",
          "eq r11,r10,r11", "bt r11,.Ltdout", f"stw r10,dp[{d(WH_SEEN)}]",
          "ldc r9,31", "and r10,r10,r9",
          "shl r6,r10,7",
          f"ldaw r8,dp[{d(WH_LOG)}]", "add r8,r8,r6",
          f"stw r8,dp[{d(WH_SAVE2) + 14}]",
          f"ldaw r0,dp[{d(WH_PKT)}]", "ldc r1,64", f"ldw r2,dp[{d(WH_LEN)}]",
          ".global wh_drv_set", "wh_drv_set:", "nop"]
    L += [f"ldw r8,dp[{d(WH_SAVE2) + 14}]",
          f"ldaw r10,dp[{d(WH_RESP)}]", "ldc r11,0", ".Ltdcp:",
          "ldw r5,r10[r11]", "stw r5,r8[r11]",
          "add r11,r11,1", "ldc r5,16", "eq r5,r11,r5", "bf r5,.Ltdcp"]
    L += [f"ldw r5,dp[{d(WH_T)}]", "ldc r9,16", "stw r5,r8[r9]"]
    L += [f"ldw r5,dp[{d(WH_RESP_VALID)}]", "ldc r9,17", "stw r5,r8[r9]"]
    L += ["ldw r5,sp[3]", f"stw r5,dp[{d(WH_SAVE2) + 15}]",
          "ldc r5,0x5678", "stw r5,sp[3]",
          "ldc r0,64", "ldc r1,0x1234", f"ldaw r2,dp[{d(WH_GETBUF)}]", "ldc r3,64",
          ".global wh_drv_get", "wh_drv_get:", "nop"]
    L += [f"ldw r8,dp[{d(WH_SAVE2) + 14}]",
          "ldc r9,23", "stw r0,r8[r9]",
          f"ldw r5,dp[{d(WH_SAVE2) + 15}]", "stw r5,sp[3]"]
    for i in range(5):
        L += [f"ldw r5,dp[{d(WH_GREC + 4 * i)}]", f"ldc r9,{18 + i}", "stw r5,r8[r9]"]
    L += [f"ldw r5,dp[{d(WH_RESP_VALID)}]", "ldc r9,24", "stw r5,r8[r9]"]
    for addr, off in SNAP:
        L += [f"ldw r5,dp[{d(addr)}]", f"ldc r9,{off}", "stw r5,r8[r9]"]
    L += [f"ldaw r4,dp[{d(SNAP_SUM[0])}]", "ldc r5,0"]
    for i in range(5):
        L += [f"ldw r6,r4[{5 + i}]", "add r5,r5,r6"]
    L += [f"ldc r9,{SNAP_SUM[1]}", "stw r5,r8[r9]"]
    L += [f"ldw r6,dp[{d(WH_N)}]", "add r6,r6,1", f"stw r6,dp[{d(WH_N)}]",
          ".Ltdout:"]
    L += [f"ldw r{i},dp[{d(WH_SAVE2) + i}]" for i in range(12)]
    L += ["ldw lr,sp[0]", "retsp 2",
          ".global wh_drv_end", "wh_drv_end:"]
    return L


def recorder_source():
    return [".text", ".issue_mode dual", ".align 4",
            ".global wh_rec_start", "wh_rec_start:",
            ".global wh_rec_get", "wh_rec_get:", "dualentsp 2",
            f"stw r0,dp[{d(WH_GREC)}]",
            f"stw r1,dp[{d(WH_GREC + 4)}]",
            f"stw r2,dp[{d(WH_GREC + 8)}]",
            f"stw r3,dp[{d(WH_GREC + 12)}]",
            "ldw r11,sp[5]",
            f"stw r11,dp[{d(WH_GREC + 16)}]",
            "ldc r0,0", "retsp 2",
            ".align 4", ".global wh_rec_set", "wh_rec_set:", "dualentsp 2",
            f"stw r0,dp[{d(FB_R0)}]",
            f"stw r1,dp[{d(FB_R0 + 4)}]",
            f"stw r2,dp[{d(FB_R0 + 8)}]",
            "ldc r0,0", "retsp 2",
            ".global wh_rec_end", "wh_rec_end:"]
