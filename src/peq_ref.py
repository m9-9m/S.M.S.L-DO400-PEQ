"""Bit-exact fixed-point reference model for the DO400 Tile 0 10-band PEQ.

Matches the exact XMOS XS3A DF1 Q29 arithmetic executed in `src/build_fw.py`:

    x   = (raw >> 6) * preamp >> 28            (raw = 32-bit wire word, arithmetic shift)
    per band (DF1, 5 taps in Q29):
        acc = residue + b0*x + b1*x1 + b2*x2 + na1*y1 + na2*y2       (64-bit)
        y   = sat32(acc >> 29)                                       (lsats r3=29 / lextract)
        residue' = acc & (2**29 - 1)                                 (zext r11,r3=29)
        x2' = x1, x1' = x, y2' = y1, y1' = y
    out = clip(x, -2**25, 2**25-1) << 6

Band block layout in firmware memory (10 words per band, contiguous):

    [0]=b0 [1]=b1 [2]=b2 [3]=na1 [4]=na2 [5]=residue [6]=x1 [7]=x2 [8]=y1 [9]=y2

The clear-on-FIRST loop writes words 5..9 of every band block to zero (coefficients are NOT
touched), exactly as the generated body does (`.Lclear0:` / `.Lclear1:`).
"""
M32 = 0xFFFFFFFF
Q29 = 1 << 29
PREAMP_UNITY = 0x10000000          # 1.0 in Q28 (the body's gain stage)
CLIP_HI = 0x01FFFFFF               # +2**25-1
CLIP_LO = -0x02000000              # -2**25


def s32(x):
    x &= M32
    return x - (1 << 32) if x & 0x80000000 else x


def sat32(x):
    if x > 0x7FFFFFFF:
        return 0x7FFFFFFF
    if x < -0x80000000:
        return -0x80000000
    return x


class Body:
    """One PEQ body instance (one channel-independent state block of `bands` sections)."""

    def __init__(self, bands, first_const=0x50455101, next_const=0x50455102,
                 preamp=PREAMP_UNITY, mask=None):
        self.bands = bands
        self.first_const = first_const
        self.next_const = next_const
        self.preamp = preamp
        self.mask = (1 << bands) - 1 if mask is None else mask
        self.dec = 0                    # state[14]
        self.open = 0                   # state[13]
        # per channel: list of [b0,b1,b2,na1,na2,residue,x1,x2,y1,y2]
        self.sections = [[[0] * 10 for _ in range(bands)] for _ in range(2)]

    # -- decision -----------------------------------------------------------
    def decide(self, tag, pcm_first_by_nibble=False):
        """Reproduce the body's `.Lhook` decision block (wire mode, tag from 0x96054)."""
        if pcm_first_by_nibble:
            first = (tag >> 28) == 0xA
        else:
            first = tag == self.first_const
        if first:
            d = 2
        elif tag == self.next_const and self.open:
            d = 1
        else:
            d = 0
        self.dec = d & self.mask
        self.open = 1 if self.dec else 0
        return self.dec

    # -- DSP ----------------------------------------------------------------
    def clear(self, ch):
        for s in self.sections[ch]:
            s[5:] = [0] * 5

    def process(self, ch, raw, dec=None):
        if dec is None:
            dec = self.dec
        if dec == 0:
            return raw & M32              # DRY / bypassed: the word is passed through untouched
        if dec == 2:
            self.clear(ch)
        x = (s32(raw) >> 6) * self.preamp >> 28
        for s in self.sections[ch]:
            acc = (s[5] + s[0] * x + s[1] * s[6] + s[2] * s[7]
                   + s[3] * s[8] + s[4] * s[9])
            y = sat32(acc >> 29)
            s[5] = acc & (Q29 - 1)
            s[7] = s[6]
            s[6] = x
            s[9] = s[8]
            s[8] = y
            x = y
        if x > CLIP_HI:
            x = CLIP_HI
        if x < CLIP_LO:
            x = CLIP_LO
        return (x << 6) & M32

    # -- firmware image helpers --------------------------------------------
    def state_words(self):
        """464 words as the body expects them at its MEM base."""
        w = [0] * 464
        w[13] = self.open
        w[14] = self.dec
        w[15] = self.first_const
        w[16] = self.next_const
        w[17] = self.preamp
        w[18] = CLIP_HI
        w[19] = CLIP_LO
        w[24] = self.mask
        for ch in (0, 1):
            for i, s in enumerate(self.sections[ch]):
                k = 64 + 200 * ch + 10 * i
                w[k:k + 10] = [v & M32 for v in s]
        return w


# --------------------------------------------------------------------------- design
def rbj_peaking(fs, f0, gain_db, q):
    import math
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    b0 = 1 + alpha * A
    b1 = -2 * cw
    b2 = 1 - alpha * A
    a0 = 1 + alpha / A
    a1 = -2 * cw
    a2 = 1 - alpha / A
    return (b0, b1, b2, a0, a1, a2)


def rbj_lowshelf(fs, f0, gain_db, s=1.0):
    import math
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / 2 * math.sqrt((A + 1 / A) * (1 / s - 1) + 2)
    cw = math.cos(w0)
    sq = 2 * math.sqrt(A) * alpha
    b0 = A * ((A + 1) - (A - 1) * cw + sq)
    b1 = 2 * A * ((A - 1) - (A + 1) * cw)
    b2 = A * ((A + 1) - (A - 1) * cw - sq)
    a0 = (A + 1) + (A - 1) * cw + sq
    a1 = -2 * ((A - 1) + (A + 1) * cw)
    a2 = (A + 1) + (A - 1) * cw - sq
    return (b0, b1, b2, a0, a1, a2)


def rbj_highshelf(fs, f0, gain_db, s=1.0):
    import math
    A = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    alpha = math.sin(w0) / 2 * math.sqrt((A + 1 / A) * (1 / s - 1) + 2)
    cw = math.cos(w0)
    sq = 2 * math.sqrt(A) * alpha
    b0 = A * ((A + 1) + (A - 1) * cw + sq)
    b1 = -2 * A * ((A - 1) + (A + 1) * cw)
    b2 = A * ((A + 1) + (A - 1) * cw - sq)
    a0 = (A + 1) - (A - 1) * cw + sq
    a1 = 2 * ((A - 1) - (A + 1) * cw)
    a2 = (A + 1) - (A - 1) * cw - sq
    return (b0, b1, b2, a0, a1, a2)


def quantise(coefs, bits=29):
    """(b0,b1,b2,a0,a1,a2) -> integer taps [b0,b1,b2,na1,na2] in Q`bits`."""
    b0, b1, b2, a0, a1, a2 = coefs
    q = 1 << bits
    rnd = lambda v: int(round(v * q))
    return [rnd(b0 / a0), rnd(b1 / a0), rnd(b2 / a0), rnd(-a1 / a0), rnd(-a2 / a0)]


def identity():
    return [1 << 29, 0, 0, 0, 0]


# The 10-band set used for the normal-PCM proof (a deliberately non-trivial response:
# low boost, mid dip, presence/air lifts - the first 8 bands are real filters, 9/10 too).
def pcm10_taps(fs=44100.0):
    bands = [
        rbj_lowshelf(fs, 80.0, 6.0),
        rbj_peaking(fs, 250.0, 4.0, 1.0),
        rbj_peaking(fs, 700.0, -6.0, 1.2),
        rbj_peaking(fs, 1500.0, 3.0, 1.4),
        rbj_peaking(fs, 3000.0, -4.0, 1.8),
        rbj_peaking(fs, 5000.0, 2.0, 2.0),
        rbj_highshelf(fs, 8000.0, 4.0),
        rbj_peaking(fs, 11000.0, -3.0, 2.0),
        rbj_peaking(fs, 14000.0, 2.5, 2.0),
        rbj_peaking(fs, 18000.0, 3.0, 1.5),
    ]
    return [quantise(b) for b in bands]


def response_db(taps, freqs, fs):
    """Cascade magnitude response (dB) of the quantised taps, for documentation."""
    import cmath
    import math
    out = []
    for f in freqs:
        z = cmath.exp(-2j * math.pi * f / fs)
        h = 1 + 0j
        for b0, b1, b2, na1, na2 in taps:
            h *= (b0 + b1 * z + b2 * z * z) / (Q29 - na1 * z - na2 * z * z)
        out.append(20 * math.log10(abs(h)))
    return out
