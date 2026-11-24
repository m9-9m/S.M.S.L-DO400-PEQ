"""XU316 flash dump / upgrade image analyzer (read-only, offline).

usage:
  python xu_image_an.py <image.bin> <outdir>

<image.bin> may be a full SPI flash dump (loader + factory [+ upgrade]) or a single
DFU upgrade image. Produces in <outdir>:
  xflash_analyze.txt     raw output of `xflash --analyze`
  imgN_tileM.bin/.elf/.dis   descrambled code per tile + xobjdump disassembly
  report.md              port map / clocking / UART / USB descriptor / constants

Requires XTC Tools (path in XTC below). Nothing is sent to any device.
"""
import os, re, struct, subprocess, sys, collections

XTC = os.environ.get('XMOS_TOOL_PATH') or r'C:\Program Files\XMOS\XTC\15.3.1'
BASE = 0x80000
CRT = bytes.fromhex('4b684077')            # ldc r1,0xb ; entsp 0  (xcore crt0 _start)
FACTORY_VERSIONS = ['15.3', '15.2', '15.1', '15.0', '14.4', '14.3', '14.2', '14.1', '14.0', '13.2', '13.1', '13.0', '12', '11', '10']

SETC = {0x0: 'INUSE_OFF', 0x8: 'INUSE_ON', 0x1: 'COND_NONE', 0x11: 'COND_EQ', 0x19: 'COND_NEQ',
        0x7: 'RUN_STOPR', 0xf: 'RUN_STARTR', 0x17: 'RUN_CLRBUF', 0x1007: 'MS_MASTER', 0x100f: 'MS_SLAVE',
        0x2007: 'BUF_NOBUFFERS', 0x200f: 'BUF_BUFFERS', 0x3007: 'RDY_NOREADY', 0x300f: 'RDY_STROBED',
        0x3017: 'RDY_HANDSHAKE', 0x5007: 'PORT_DATAPORT', 0x500f: 'PORT_CLOCKPORT', 0x5017: 'PORT_READYPORT',
        0x6007: 'INV_NOINVERT', 0x600f: 'INV_INVERT', 0x4007: 'SDELAY_NOSDELAY', 0x400f: 'SDELAY_SDELAY',
        0x9: 'COND_AFTER', 0x2: 'IE_MODE_EVENT', 0xa: 'IE_MODE_INTERRUPT'}
MCLKS = {22579200: '22.5792MHz', 24576000: '24.576MHz', 45158400: '45.1584MHz', 49152000: '49.152MHz',
         11289600: '11.2896MHz', 12288000: '12.288MHz', 90316800: '90.3168MHz', 98304000: '98.304MHz'}


def xtc_env():
    """Same variables as XTC SetEnv.bat."""
    e = dict(os.environ)
    t = XTC
    home = os.path.join(os.path.expanduser('~'), '.xmos')
    e['PATH'] = os.pathsep.join([os.path.join(t, 'bin'), os.path.join(t, 'XTC_bin'), os.path.join(t, 'lib'), e.get('PATH', '')])
    e.update(XMOS_TOOL_PATH=t, XMOS_HOME=home,
             XCC_C_INCLUDE_PATH=f'{t}/target/include;{t}/target/include/clang',
             XCC_XC_INCLUDE_PATH=f'{t}/target/include/xc;{t}/target/include;{t}/target/include/clang',
             XCC_CPLUS_INCLUDE_PATH=f'{t}/target/include;{t}/target/include/clang;{t}/target/include/c++/v1',
             XCC_ASSEMBLER_INCLUDE_PATH=f'{t}/target/include;{t}/target/include/clang',
             XCC_LIBRARY_PATH=f'{t}/target/lib', XCC_DEVICE_PATH=f'{t}/configs;{t}/configs/.deprecated',
             XCC_TARGET_PATH=f'{home}/targets;{t}/targets;{t}/targets/.deprecated', XCC_EXEC_PREFIX=f'{t}/libexec/')
    return e


def run(args, cwd=None):
    exe = os.path.join(XTC, 'bin', args[0] + '.exe')
    p = subprocess.run([exe] + args[1:], cwd=cwd, capture_output=True, text=True, errors='replace', env=xtc_env())
    return p.returncode, p.stdout + p.stderr


def nibswap(b):
    return bytes(((x << 4) | (x >> 4)) & 0xFF for x in b)


def mkelf(code, path):
    shstr = b'\0.text\0.shstrtab\0'
    eh, ph = 52, 32
    off_code = eh + ph
    off_str = off_code + len(code)
    off_sh = (off_str + len(shstr) + 3) & ~3
    e = bytearray(b'\x7fELF\x01\x01\x01' + b'\0' * 9)
    e += struct.pack('<HHIIIIIHHHHHH', 2, 203, 1, BASE, eh, off_sh, 0, eh, ph, 1, 40, 3, 2)
    e += struct.pack('<8I', 1, off_code, BASE, BASE, len(code), len(code), 7, 4)
    e += code + shstr
    e += b'\0' * (off_sh - len(e)) + b'\0' * 40
    e += struct.pack('<10I', 1, 1, 6, BASE, off_code, len(code), 0, 0, 4, 0)
    e += struct.pack('<10I', 7, 3, 0, 0, off_str, len(shstr), 0, 0, 1, 0)
    open(path, 'wb').write(e)


def is_port(v):
    return v is not None and v & 0xFF == 0 and (v >> 16) in (1, 4, 8, 0x10, 0x20) and ((v >> 8) & 0xFF) <= 16


def pname(v):
    if is_port(v):
        return f'XS1_PORT_{v >> 16}{chr(65 + ((v >> 8) & 0xFF))}'
    if v is not None and v & 0xFF == 6 and v >> 16 == 0:
        return f'XS1_CLKBLK_{(v >> 8) & 0xFF}'
    return None


RX = re.compile(r'^\S*\s+0x([0-9a-f]{8}): ([0-9a-f ]+?):\s+(\w+) \(([^)]*)\)\s*(.*)$')


class Tile:
    def __init__(self, name, code, outdir):
        self.name, self.code = name, code
        p = os.path.join(outdir, name)
        open(p + '.bin', 'wb').write(code)
        mkelf(code, p + '.elf')
        rc, out = run(['xobjdump', '--march', 'xs3a', '-d', p + '.elf', '-o', p + '.dis'])
        self.L = open(p + '.dis', encoding='utf-8', errors='replace').read().splitlines()
        self.P = []
        for l in self.L:
            m = RX.match(l)
            if m:
                ops = re.sub(r'\s*<[^>]*>', '', m.group(5)).strip()
                self.P.append((int(m.group(1), 16), len(m.group(2).split()), m.group(3), ops))
        self.cp = self.dp = None
        self._bases()
        self._funcs()

    def w(self, a):
        o = a - BASE
        return struct.unpack_from('<I', self.code, o)[0] if 0 <= o <= len(self.code) - 4 else None

    def _bases(self):
        for i, (a, n, mn, ops) in enumerate(self.P[:400]):
            if mn == 'set' and ops.startswith('cp, r11'):
                pa, pn_, pmn, pops = self.P[i - 1]
                imm = int(re.search(r'r11, (-?0x[0-9a-f]+)', pops).group(1), 16)
                self.cp = pa + pn_ + 2 * imm
                k = re.search(r'dp, cp\[(0x[0-9a-f]+)\]', self.P[i + 1][3])
                if k:
                    self.dp = self.w(self.cp + 4 * int(k.group(1), 16))
                return

    # ---------- light inter-procedural data flow ----------
    def _funcs(self):
        self.idx = {a: i for i, (a, *_r) in enumerate(self.P)}
        self.calls = collections.defaultdict(list)       # callee addr -> [call idx]
        for i, (a, n, mn, ops) in enumerate(self.P):
            if mn == 'bl':
                m = re.match(r'(-?0x[0-9a-f]+)$', ops)
                if m:
                    self.calls[a + n + 2 * int(m.group(1), 16)].append(i)
        self.fstart = [0] * len(self.P)                   # idx -> idx of function entry
        self.fframe = {}                                  # entry idx -> frame words
        cur = 0
        for i, (a, n, mn, ops) in enumerate(self.P):
            if a in self.calls or (mn in ('entsp', 'dualentsp') and i and self.P[i - 1][2] in ('retsp', 'bu', 'stw', 'nop', 'ret')):
                cur = i
            if mn in ('entsp', 'dualentsp') and cur not in self.fframe:
                self.fframe[cur] = int(ops, 16)
            self.fstart[i] = cur

    def _frame(self, fi):
        return self.fframe.get(fi, 0)

    def resolve(self, i, reg, depth=3, seen=None):
        """Set of values held by `reg` just before instruction i (port/clk names or ints)."""
        seen = seen or set()
        key = (i, reg)
        if key in seen:
            return set()
        seen.add(key)
        fi = self.fstart[i]
        N = self._frame(fi)
        for k in range(i - 1, fi - 1, -1):
            a, n, mn, ops = self.P[k]
            if mn == 'bl' and reg in ('r0', 'r1', 'r2', 'r3', 'r11'):
                return {'?ret'}
            dst = ops.split(',')[0].strip()
            if mn == 'ldd':
                mm = re.match(r'(r\d+), (r\d+), sp\[(0x[0-9a-f]+)\]$', ops)
                if mm and reg in (mm.group(1), mm.group(2)):
                    slot = 2 * int(mm.group(3), 16) + (1 if reg == mm.group(1) else 0)
                    return self._slot(k, fi, N, slot, depth, seen)
                continue
            if dst != reg or mn in ('stw', 'st8', 'st16', 'std', 'out', 'outpw', 'outshr', 'setc', 'setclk', 'setd',
                                    'settw', 'setpt', 'eeu', 'bt', 'bf', 'ecallf', 'ecallt', 'chkct', 'outct', 'syncr'):
                continue
            m = re.match(rf'{reg}, dp\[(0x[0-9a-f]+)\]$', ops)
            if mn == 'ldw' and m:
                v = self.w(self.dp + 4 * int(m.group(1), 16))
                return {pname(v) or v}
            if mn == 'ldaw' and m:
                return {('dparr', self.dp + 4 * int(m.group(1), 16))}
            m = re.match(rf'{reg}, cp\[(0x[0-9a-f]+)\]$', ops)
            if mn == 'ldw' and m:
                v = self.w(self.cp + 4 * int(m.group(1), 16))
                return {pname(v) or v}
            m = re.match(rf'{reg}, (0x[0-9a-f]+)$', ops)
            if mn in ('ldc', 'mkmsk') and m:
                v = int(m.group(1), 16)
                return {pname((1 << v) - 1 if mn == 'mkmsk' else v) or ((1 << v) - 1 if mn == 'mkmsk' else v)}
            m = re.match(rf'{reg}, (r\d+), 0x0$', ops)
            if mn == 'add' and m:
                return self.resolve(k, m.group(1), depth, seen)
            m = re.match(rf'{reg}, sp\[(0x[0-9a-f]+)\]$', ops)
            if mn == 'ldw' and m:
                return self._slot(k, fi, N, int(m.group(1), 16), depth, seen)
            m = re.match(rf'{reg}, (r\d+)\[(0x[0-9a-f]+|r\d+)\]$', ops)
            if mn == 'ldw' and m:                         # element of a port array
                base = self.resolve(k, m.group(1), depth, seen)
                out = set()
                for b in base:
                    if isinstance(b, tuple) and b[0] == 'dparr':
                        if m.group(2).startswith('0x'):
                            v = self.w(b[1] + 4 * int(m.group(2), 16))
                            out.add(pname(v) or v)
                        else:
                            arr = []
                            for j in range(8):
                                v = self.w(b[1] + 4 * j)
                                if not pname(v):
                                    break
                                arr.append(pname(v))
                            out.add('[' + ','.join(arr) + ']' if arr else '?arr')
                return out or {'?'}
            return {f'?{mn}'}
        # reached function entry
        if reg in ('r0', 'r1', 'r2', 'r3') and depth > 0:
            out = set()
            for ci in self.calls.get(self.P[fi][0], [])[:12]:
                out |= self.resolve(ci, reg, depth - 1, seen)
            return out or {f'?arg{reg}'}
        return {f'?{reg}'}

    def _slot(self, k, fi, N, slot, depth, seen):
        if slot >= N and depth > 0:                       # caller's outgoing stack argument
            cslot = slot - N
            out = set()
            for ci in self.calls.get(self.P[fi][0], [])[:12]:
                cfi = self.fstart[ci]
                for j in range(ci - 1, cfi - 1, -1):
                    a, n, mn, ops = self.P[j]
                    m = re.match(r'(r\d+), sp\[(0x[0-9a-f]+)\]$', ops)
                    if mn == 'stw' and m and int(m.group(2), 16) == cslot:
                        out |= self.resolve(j, m.group(1), depth - 1, seen)
                        break
                    m = re.match(r'(r\d+), (r\d+), sp\[(0x[0-9a-f]+)\]$', ops)
                    if mn == 'std' and m and int(m.group(3), 16) * 2 in (cslot, cslot - 1):
                        r = m.group(2) if int(m.group(3), 16) * 2 == cslot else m.group(1)
                        out |= self.resolve(j, r, depth - 1, seen)
                        break
            return out or {f'?stack{cslot}'}
        for j in range(k - 1, fi - 1, -1):              # local spill
            a, n, mn, ops = self.P[j]
            m = re.match(r'(r\d+), sp\[(0x[0-9a-f]+)\]$', ops)
            if mn == 'stw' and m and int(m.group(2), 16) == slot:
                return self.resolve(j, m.group(1), depth, seen)
        return {f'?sp{slot}'}

    def relations(self):
        """Clocking relations: clock sources, clock outputs, clocked ports, transfer widths."""
        rel = collections.Counter()

        def fmt(vals):
            vs = sorted(str(v) if not isinstance(v, int) else hex(v) for v in vals if not (isinstance(v, str) and v.startswith('?')))
            return '/'.join(vs) if vs else '?'
        for i, (a, n, mn, ops) in enumerate(self.P):
            m = re.match(r'res\[(r\d+)\], (r\d+|0x[0-9a-f]+)$', ops)
            if not m:
                continue
            tgt, arg = m.group(1), m.group(2)
            if mn == 'setclk':
                t = self.resolve(i, tgt)
                s = self.resolve(i, arg)
                ts, ss = fmt(t), fmt(s)
                if ts == '?' and ss == '?':
                    continue
                if 'CLKBLK' in ts and 'CLKBLK' not in ss:
                    rel[f'クロック源: {ts} ← {ss}'] += 1
                else:
                    rel[f'ポートのクロック: {ts} ← {ss}'] += 1
            elif mn == 'setc':
                c = int(arg, 16) if arg.startswith('0x') else None
                if c is None:
                    cv = self.resolve(i, arg)
                    c = next((v for v in cv if isinstance(v, int)), None)
                if c in (0x500f, 0x5017, 0x1007, 0x100f, 0x300f, 0x3017, 0x600f, 0x400f):
                    rel[f'{SETC[c]}: {fmt(self.resolve(i, tgt))}'] += 1
            elif mn == 'settw':
                w_ = int(arg, 16) if arg.startswith('0x') else fmt(self.resolve(i, arg))
                rel[f'転送幅 {w_}: {fmt(self.resolve(i, tgt))}'] += 1
            elif mn == 'setrdy':
                rel[f'READY: {fmt(self.resolve(i, tgt))} ← {fmt(self.resolve(i, arg))}'] += 1
        return rel

    def ports(self):
        """Port/clock-block variables in dp data, with traced operations."""
        res = collections.OrderedDict()
        for i, (a, n, mn, ops) in enumerate(self.P):
            m = re.match(r'(r\d+), dp\[(0x[0-9a-f]+)\]$', ops)
            if not (m and mn == 'ldw'):
                continue
            va = self.dp + 4 * int(m.group(2), 16)
            v = self.w(va)
            nm = pname(v)
            if not nm:
                continue
            ent = res.setdefault(va, {'name': nm, 'val': v, 'ops': collections.Counter(), 'refs': 0})
            ent['refs'] += 1
            reg = m.group(1)
            for j in range(i + 1, min(i + 14, len(self.P))):
                _, _, mn2, ops2 = self.P[j]
                if f'res[{reg}]' in ops2:
                    if mn2 == 'setc':
                        arg = ops2.split(',')[-1].strip()
                        if arg.startswith('0x'):
                            c = int(arg, 16)
                            ent['ops'][f'setc {SETC.get(c, hex(c))}'] += 1
                        else:
                            src = self._regsrc(j, arg)
                            try:
                                c = int(src, 16)
                                ent['ops'][f'setc {SETC.get(c, hex(c))}'] += 1
                            except ValueError:
                                ent['ops'][f'setc {src}'] += 1
                    elif mn2 == 'setclk':
                        src = ops2.split(',')[-1].strip()
                        ent['ops'][f'setclk <- {self._regsrc(j, src)}'] += 1
                    elif mn2 in ('settw', 'setd', 'setpt', 'setpsc', 'setrdy'):
                        ent['ops'][f'{mn2} {self._regsrc(j, ops2.split(",")[-1].strip())}'] += 1
                    else:
                        ent['ops'][mn2] += 1
                elif mn2 in ('setclk', 'setrdy') and ops2.endswith(', ' + reg):
                    tgt = re.search(r'res\[(r\d+)\]', ops2).group(1)
                    ent['ops'][f'{mn2}: drives {self._regsrc(j, tgt)}'] += 1
                if re.match(rf'{reg},', ops2) and mn2 not in ('out', 'outpw', 'outshr', 'outct', 'st8', 'stw', 'std', 'eq', 'bt', 'bf'):
                    break
        # table entries never referenced directly (arrays)
        table = []
        if self.dp:
            for o in range(self.dp - BASE, len(self.code) - 4, 4):
                v = self.w(BASE + o)
                if pname(v):
                    table.append((BASE + o, pname(v)))
        return res, table

    def _regsrc(self, j, reg):
        """Find what a register holds by scanning backwards a few instructions."""
        if not reg.startswith('r'):
            return reg
        for k in range(j - 1, max(j - 12, 0), -1):
            _, _, mn, ops = self.P[k]
            if ops.startswith(reg + ','):
                m = re.match(rf'{reg}, dp\[(0x[0-9a-f]+)\]$', ops)
                if mn == 'ldw' and m:
                    v = self.w(self.dp + 4 * int(m.group(1), 16))
                    return pname(v) or hex(v or 0)
                m = re.match(rf'{reg}, cp\[(0x[0-9a-f]+)\]$', ops)
                if mn == 'ldw' and m:
                    v = self.w(self.cp + 4 * int(m.group(1), 16))
                    return pname(v) or f'{v} (0x{v:X})'
                m = re.match(rf'{reg}, (0x[0-9a-f]+)$', ops)
                if mn in ('ldc', 'mkmsk'):
                    v = int(m.group(1), 16) if m else None
                    if mn == 'mkmsk' and v is not None:
                        v = (1 << v) - 1
                    return pname(v) or (hex(v) if v is not None else ops)
                return f'{reg}({mn} {ops})'
        return reg

    def uart(self):
        """Bit-banged UART: `ldc rX, 0x364` (100MHz/115200) near out/outshr."""
        hits = []
        baud = {0x364: 115200, 0x1B2: 230400, 0xD9: 460800, 0x28B1: 9600, 0x1458: 19200}
        for i, (a, n, mn, ops) in enumerate(self.P):
            m = re.match(r'r\d+, (0x[0-9a-f]+)$', ops)
            if mn == 'ldc' and m and int(m.group(1), 16) in baud:
                win = self.P[max(0, i - 12):i + 30]
                if sum(1 for x in win if x[2] in ('outshr', 'out')) >= 2:
                    port = None
                    for x in self.P[max(0, i - 12):i + 1]:
                        mm = re.match(r'(r\d+), dp\[(0x[0-9a-f]+)\]$', x[3])
                        if x[2] == 'ldw' and mm:
                            port = pname(self.w(self.dp + 4 * int(mm.group(2), 16)))
                    hits.append((a, baud[int(m.group(1), 16)], port))
        return hits


def usb_desc(data):
    out = []
    for m in re.finditer(rb'\x12\x01[\x00\x01\x10][\x01\x02][\x00-\xff]{3}[\x08\x10\x20\x40]', data):
        d = data[m.start():m.start() + 18]
        if len(d) == 18:
            vid, pid, bcd = struct.unpack_from('<HHH', d, 8)
            out.append((m.start(), vid, pid, bcd, d[4], d[5], d[6]))
    return out


def strings(data, n=5):
    return [(m.start(), m.group().decode()) for m in re.finditer(rb'[\x20-\x7e]{%d,}' % n, data)
            if sum(c.isalpha() for c in m.group().decode()) >= n - 1 and ' ' in m.group().decode() or re.fullmatch(rb'[A-Za-z][A-Za-z0-9_ ()\-.]{6,}', m.group())]


def main():
    img, outdir = sys.argv[1], sys.argv[2]
    os.makedirs(outdir, exist_ok=True)
    raw = open(img, 'rb').read()
    R = [f'# XU316 イメージ解析レポート\n\n- 入力: `{img}` ({len(raw)} bytes)\n']

    # 1) xflash --analyze with candidate tools versions
    xa, used = '', None
    for v in FACTORY_VERSIONS:
        rc, out = run(['xflash', '--factory-version', v, '--analyze', os.path.abspath(img)], cwd=outdir)
        if 'Failed to parse' not in out and 'Error' not in out:
            xa, used = out, v
            break
        if not xa or ('Upgrade image' in out or 'Factory image' in out) and len(out) > len(xa):
            xa, used = out, v + ' (partial)'
    open(os.path.join(outdir, 'xflash_analyze.txt'), 'w', encoding='utf-8').write(xa)
    R.append(f'## xflash --analyze（factory-version {used}）\n```\n' + '\n'.join(
        l for l in xa.splitlines() if re.search(r'image|Offset|Size: 0x|Tools version|format|Version|CRC|Number of cores|Loader|User address|ID ', l, re.I))[:6000] + '\n```\n')

    # 2) tiles: use xflash core table; fall back to crt0 scan in nibble-swapped data
    sw = nibswap(raw)
    tiles = []
    for m in re.finditer(r'(Factory|Upgrade) image [^\n]*\[(\d+)\]:\s*\n\s*Offset: (0x[0-9A-Fa-f]+)(.*?)(?=\n(?:Factory|Upgrade) image|\nDumping|\Z)', xa, re.S):
        ii, io, body = int(m.group(2)), int(m.group(3), 16), m.group(4)
        isz = int(re.search(r'Size: (0x[0-9A-Fa-f]+) bytes', body).group(1), 16)
        cores = re.findall(r'Core [^\n]*\[(\d+)\]:\s*\n\s*Offset: (0x[0-9A-Fa-f]+)\s*\n\s*Size: (0x[0-9A-Fa-f]+)', body)
        prev_end = None
        for cj, co, cs in cores:
            co, cs = int(co, 16), int(cs, 16)
            if cs:
                s0, e0 = io + co, io + co + cs
            else:                       # xflash could not parse this core (old image format)
                if prev_end is None:
                    continue
                s0 = sw.find(CRT, prev_end, io + isz)
                if s0 < 0:
                    continue
                e0 = io + isz
            tiles.append((ii, int(cj), s0, e0))
            prev_end = e0
    if not tiles:                       # xflash failed: plain crt0 scan
        starts = [m.start() for m in re.finditer(re.escape(CRT), sw)]
        for k, s0 in enumerate(starts):
            tiles.append((0, k, s0, starts[k + 1] if k + 1 < len(starts) else len(sw)))
    R.append('## タイルのコード位置\n| イメージ | タイル | 開始 | 終了 | サイズ |\n|---|---|---|---|---|\n' +
             ''.join(f'| {i} | {t} | 0x{s:X} | 0x{e:X} | {e - s} |\n' for i, t, s, e in tiles))

    # 3) USB descriptors & strings
    R.append('## USBデバイス記述子\n| 位置 | VID | PID | bcdDevice | class/sub/proto |\n|---|---|---|---|---|\n' +
             ''.join(f'| 0x{o:X} | 0x{v:04X} | 0x{p:04X} | 0x{b:04X} | {c:02X}/{s:02X}/{pr:02X} |\n' for o, v, p, b, c, s, pr in usb_desc(sw)))
    st = strings(sw)
    R.append('## 文字列（抜粋）\n' + ''.join(f'- 0x{o:X}: `{s}`\n' for o, s in st[:80]))

    # 4) per-tile analysis
    for ii, tn, s, e in tiles:
        name = f'img{ii}_tile{tn}'
        T = Tile(name, sw[s:e], outdir)
        R.append(f'## {name}（0x{s:X}〜0x{e:X}）\n- cp=0x{(T.cp or 0):X}, dp=0x{(T.dp or 0):X}\n')
        if not T.dp:
            R.append('- dpが特定できないため、ポート解析は省略\n')
            continue
        ports, table = T.ports()
        R.append('### ポート変数と操作\n| dp変数 | ポート | 参照数 | 操作（回数） |\n|---|---|---|---|\n')
        for va, ent in sorted(ports.items()):
            ops = ', '.join(f'{k}×{c}' for k, c in ent['ops'].most_common(12))
            R.append(f'| 0x{va:X} | {ent["name"]} | {ent["refs"]} | {ops} |\n')
        R.append('\n### dp領域にあるポート/クロックブロック定数（配列を含む）\n' +
                 ', '.join(f'0x{a:X}={n}' for a, n in table) + '\n')
        R.append('\n### クロック関係（関数呼び出しをまたいで解決）\n' +
                 ''.join(f'- {k}（{c}箇所）\n' for k, c in sorted(T.relations().items())))
        u = T.uart()
        R.append('\n### ソフトUART送信の候補\n' + (''.join(f'- 0x{a:X}: {b}bps, ポート {p}\n' for a, b, p in u) or '- なし\n'))
        consts = collections.Counter()
        for o in range(0, len(T.code) - 3, 4):
            v = struct.unpack_from('<I', T.code, o)[0]
            if v in MCLKS:
                consts[MCLKS[v]] += 1
        R.append('\n### MCLK周波数の定数\n' + (', '.join(f'{k}×{c}' for k, c in consts.items()) or 'なし') + '\n')
        rates = [hex(BASE + o) for o in range(0, len(T.code) - 3, 4) if struct.unpack_from('<I', T.code, o)[0] in (44100, 48000)]
        R.append(f'\n### 44100/48000 定数の位置\n{", ".join(rates[:20]) or "なし"}\n')
    open(os.path.join(outdir, 'report.md'), 'w', encoding='utf-8').write('\n'.join(R))
    print('\n'.join(R))


if __name__ == '__main__':
    main()
