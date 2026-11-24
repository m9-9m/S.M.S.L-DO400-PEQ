#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fw_validator.py -- DO400 (XU316) ファームウェアイメージのオフライン安全検証器.

DFU 書込前に、選択された `.bin` が「XMOS アップグレードイメージコンテナ」として
完全に正しいかどうかを、デバイスに一切触れずに検証する。

検証内容 (DO400 XU316 実機コンテナ仕様に準拠)
--------------------------------------------------------------------------
  1. ニブルスワップ解除後の Magic `0x00FF51DE`
  2. Version >= 1 (1 = アップグレードイメージ)
  3. size == length == 実ファイルサイズ, 4 B / 64 B 倍数
  4. Page CRC   : xmos_crc(image[0x0C:0x100])
  5. Image CRC  : xmos_crc(image[0x0C:size])
  6. Section CRC: switch setup / core 0 / core 1 の全 3 セクション
                  section_crc(body) = xmos_crc(u32(len(body)/4) || body)
  7. レイアウト: ヘッダ 0x74 + セクションが隙間なく連続し、末尾が size と一致
                 (2 コア境界 = core1.offset == core0.offset + core0.size)
  8. DFU 上限ワード: core 0 の RAM 0x8AF98 (= コンテナ内 core0.off + 0xAF98) を読み、
     0x00032000 (200 KiB) / 0x00080000 (512 KiB) のどちらかを判定・表示
  9. サイズ上限:
        <= 204,800 B          -> 純正 FW 3.23 / 3.29 へ直接書込可
        204,801 .. 524,288 B  -> 先に 512 KiB ブートストラップが必要 (要確認)
        >  524,288 B          -> 書込不可 (ロック)

CRC は xCORE の `crc32` 命令をビット単位で模した独立実装。外部モジュールに
依存しない (標準ライブラリのみ)。

このモジュールはファイルを読むだけで、USB / シリアル / JTAG には触れない。
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

__all__ = [
    "IMAGE_MAGIC", "PAGE_SIZE", "HEADER_MIN_SIZE", "DFU_LIMIT_RAM_ADDR",
    "DFU_LIMIT_CORE_OFFSET", "DFU_LIMIT_200K", "DFU_LIMIT_512K",
    "SIZE_LIMIT_200K", "SIZE_LIMIT_512K", "CORE0_LOAD_ADDR",
    "SIZE_CLASS_DIRECT", "SIZE_CLASS_BOOTSTRAP", "SIZE_CLASS_TOO_LARGE",
    "SIZE_CLASS_INVALID",
    "Check", "Section", "ValidationResult",
    "nibble_swap", "hw_crc32", "xmos_crc", "page_crc", "image_crc", "section_crc",
    "validate_bytes", "validate_file", "format_report",
]

# --------------------------------------------------------------------------
# 定数
# --------------------------------------------------------------------------
IMAGE_MAGIC = 0x00FF51DE
PAGE_SIZE = 0x100              # フラッシュページ = DFU 4 ブロック (64 B x 4)
HEADER_MIN_SIZE = 0x3C         # magic..core table offset
CORE_TABLE_OFFSET = 0x3C
CORE_RECORD_SIZE = 28

CORE0_LOAD_ADDR = 0x80000      # xcore.ai SRAM 先頭 (core 0 のロード先)
DFU_LIMIT_RAM_ADDR = 0x8AF98   # FLASH_MAX_UPGRADE_SIZE の格納先 (RAM アドレス)
DFU_LIMIT_CORE_OFFSET = DFU_LIMIT_RAM_ADDR - CORE0_LOAD_ADDR   # 0xAF98

DFU_LIMIT_200K = 0x00032000
DFU_LIMIT_512K = 0x00080000
SIZE_LIMIT_200K = 204800       # 204,800 B
SIZE_LIMIT_512K = 524288       # 524,288 B

SIZE_CLASS_DIRECT = "direct"
SIZE_CLASS_BOOTSTRAP = "bootstrap_required"
SIZE_CLASS_TOO_LARGE = "too_large"
SIZE_CLASS_INVALID = "invalid"

SIZE_CLASS_LABEL = {
    SIZE_CLASS_DIRECT: "200 KiB 以下 — 純正 FW 3.23 / 3.29 へ直接書込可能",
    SIZE_CLASS_BOOTSTRAP: "200 KiB 超 512 KiB 以下 — 先に 512 KiB ブートストラップの導入が必要",
    SIZE_CLASS_TOO_LARGE: "512 KiB 超 — 書込不可 (DFU 上限外)",
    SIZE_CLASS_INVALID: "判定不能 (検証エラー)",
}

FEATURE_SWITCH_SETUP = 0x40

# --------------------------------------------------------------------------
# xCORE crc32 モデル
# --------------------------------------------------------------------------
_CRC_POLY = 0xEDB88320
_M32 = 0xFFFFFFFF


def _build_table() -> List[int]:
    tab = []
    for i in range(256):
        x = i
        for _ in range(8):
            x = (x >> 1) ^ (_CRC_POLY if x & 1 else 0)
        tab.append(x & _M32)
    return tab


_TAB = _build_table()


def nibble_swap(data: bytes) -> bytes:
    """各バイトの上位/下位ニブルを入れ替える (対合)。"""
    return bytes(((b & 0x0F) << 4) | (b >> 4) for b in data)


def hw_crc32(seed: int, data: bytes) -> int:
    """xCORE `crc32` 命令のビット単位モデル。"""
    s = seed & _M32
    tab = _TAB
    for b in data:
        s = ((s >> 8) ^ tab[s & 0xFF] ^ (b << 24)) & _M32
    return s


def xmos_crc(data: bytes, seed: int = _M32, trailing_zero_word: bool = True) -> int:
    """xflash / ステージ2ローダが保存する CRC 値 (最終反転込み)。"""
    s = hw_crc32(seed, data)
    if trailing_zero_word:
        for _ in range(4):
            s = ((s >> 8) ^ _TAB[s & 0xFF]) & _M32
    return (~s) & _M32


def page_crc(image: bytes) -> int:
    """先頭ページ (0x0C..0x100) の CRC。"""
    return xmos_crc(image[0x0C:PAGE_SIZE])


def image_crc(image: bytes, size: Optional[int] = None) -> int:
    """イメージ全体 (0x0C..size) の CRC。"""
    if size is None:
        size = struct.unpack_from("<I", image, 0x10)[0]
    return xmos_crc(image[0x0C:size])


def section_crc(body: bytes) -> int:
    """セクション末尾 4 B のトレーラ値。body はトレーラを除いた本体。"""
    if len(body) % 4:
        raise ValueError("section body length 0x%X is not word aligned" % len(body))
    return xmos_crc(struct.pack("<I", len(body) // 4) + body)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# 結果データ構造
# --------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True       # False = 警告 (書込ロックしない)

    @property
    def mark(self) -> str:
        return "OK " if self.ok else ("NG " if self.fatal else "WARN")


@dataclass
class Section:
    kind: str                # "switch_setup" / "core0" / "core1" ...
    index: int
    offset: int
    size: int
    stored_crc: Optional[int]
    calc_crc: Optional[int]
    crc_ok: bool
    sha256: str = ""
    in_bounds: bool = True

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass
class ValidationResult:
    path: str = ""
    file_size: int = 0
    sha256: str = ""
    checks: List[Check] = field(default_factory=list)
    sections: List[Section] = field(default_factory=list)

    magic: Optional[int] = None
    version: Optional[int] = None
    features: Optional[int] = None
    size: Optional[int] = None
    length: Optional[int] = None
    num_cores: Optional[int] = None
    page_crc_stored: Optional[int] = None
    page_crc_calc: Optional[int] = None
    image_crc_stored: Optional[int] = None
    image_crc_calc: Optional[int] = None

    dfu_limit: Optional[int] = None
    dfu_limit_offset: Optional[int] = None
    size_class: str = SIZE_CLASS_INVALID

    def add(self, name: str, ok: bool, detail: str = "", fatal: bool = True) -> Check:
        c = Check(name, bool(ok), detail, fatal)
        self.checks.append(c)
        return c

    # ---- 集計 -------------------------------------------------------------
    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and c.fatal]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if not c.ok and not c.fatal]

    @property
    def ok(self) -> bool:
        """致命的チェックが 1 つも無い = コンテナとして完全に正しい。"""
        return not self.failures

    @property
    def requires_bootstrap(self) -> bool:
        return self.ok and self.size_class == SIZE_CLASS_BOOTSTRAP

    @property
    def write_allowed(self) -> bool:
        return self.ok and self.size_class in (SIZE_CLASS_DIRECT, SIZE_CLASS_BOOTSTRAP)

    @property
    def verdict(self) -> str:
        if self.size_class == SIZE_CLASS_TOO_LARGE:
            return "NG (サイズ超過)"
        if not self.ok:
            return "NG"
        if not self.write_allowed:
            return "NG"
        return "ALL GOOD"

    @property
    def size_class_label(self) -> str:
        return SIZE_CLASS_LABEL.get(self.size_class, self.size_class)

    @property
    def dfu_limit_label(self) -> str:
        if self.dfu_limit is None:
            return "不明"
        if self.dfu_limit == DFU_LIMIT_200K:
            return "200 KiB (0x00032000)"
        if self.dfu_limit == DFU_LIMIT_512K:
            return "512 KiB (0x00080000)"
        return "0x%08X" % self.dfu_limit


# --------------------------------------------------------------------------
# 検証本体
# --------------------------------------------------------------------------
def _u32(buf: bytes, off: int) -> int:
    return struct.unpack_from("<I", buf, off)[0]


def validate_bytes(data: bytes, name: str = "<bytes>") -> ValidationResult:
    """生バイト列 (コンテナファイル全体) を検証する。"""
    r = ValidationResult(path=name, file_size=len(data), sha256=sha256_hex(data))

    # ---- A. 基本サイズ ----------------------------------------------------
    r.add("ファイルサイズ >= 0x74 (ヘッダ最小)",
          len(data) >= HEADER_MIN_SIZE,
          "%d B" % len(data))
    r.add("ファイルサイズが 64 B の倍数 (DFU ブロック)",
          len(data) > 0 and len(data) % 64 == 0,
          "%d B %% 64 = %d" % (len(data), len(data) % 64))
    if len(data) < HEADER_MIN_SIZE:
        r.size_class = SIZE_CLASS_INVALID
        return r                      # これ以上は解析不能

    img = nibble_swap(data)           # 論理イメージ (これを解析する)

    # ---- B. ヘッダ --------------------------------------------------------
    r.magic = _u32(img, 0x00)
    r.page_crc_stored = _u32(img, 0x04)
    r.image_crc_stored = _u32(img, 0x08)
    r.features = _u32(img, 0x0C)
    r.size = _u32(img, 0x10)
    r.version = _u32(img, 0x14)
    r.length = _u32(img, 0x18)
    r.num_cores = _u32(img, 0x1C)

    r.add("Magic == 0x00FF51DE (ニブルスワップ解除後)",
          r.magic == IMAGE_MAGIC, "0x%08X" % r.magic)
    r.add("Version >= 1 (アップグレードイメージ)",
          isinstance(r.version, int) and r.version >= 1, "version = %d" % r.version)
    r.add("size == length", r.size == r.length,
          "size=0x%X length=0x%X" % (r.size, r.length))
    r.add("size == 実ファイルサイズ", r.size == len(data),
          "0x%X (%d) vs 0x%X (%d)" % (r.size, r.size, len(data), len(data)))
    r.add("size が 64 B の倍数", r.size % 64 == 0,
          "0x%X %% 64 = %d" % (r.size, r.size % 64))
    r.add("size が 4 B 境界 (ワード整列)", r.size % 4 == 0,
          "0x%X %% 4 = %d" % (r.size, r.size % 4))
    r.add("Features に switch setup ビット (0x40)",
          bool(r.features & FEATURE_SWITCH_SETUP),
          "features = 0x%08X" % r.features)

    ncore = r.num_cores
    header_size = HEADER_MIN_SIZE + CORE_RECORD_SIZE * ncore if ncore and ncore < 1024 else None
    core_table_ok = bool(header_size and header_size <= len(img) and 1 <= ncore <= 8)
    r.add("コア数 1..8 かつヘッダがファイル内に収まる", core_table_ok,
          "num_cores = %s, header = %s" % (ncore, ("0x%X" % header_size) if header_size else "-"))
    if not core_table_ok:
        r.size_class = SIZE_CLASS_INVALID
        return r

    r.add("ヘッダの core table offset == 0x3C", _u32(img, 0x30) == CORE_TABLE_OFFSET,
          "0x%X" % _u32(img, 0x30))
    r.add("size >= ヘッダサイズ", r.size >= header_size,
          "0x%X vs 0x%X" % (r.size, header_size))
    if r.size < header_size or r.size > len(img):
        r.size_class = SIZE_CLASS_INVALID
        return r

    # ---- C. Page CRC / Image CRC -----------------------------------------
    r.page_crc_calc = page_crc(img)
    r.add("Page CRC 再計算一致", r.page_crc_calc == r.page_crc_stored,
          "stored 0x%08X / calc 0x%08X" % (r.page_crc_stored, r.page_crc_calc))

    r.image_crc_calc = image_crc(img, r.size)
    r.add("Image CRC 再計算一致", r.image_crc_calc == r.image_crc_stored,
          "stored 0x%08X / calc 0x%08X" % (r.image_crc_stored, r.image_crc_calc))

    # ---- D. セクション ----------------------------------------------------
    sw_off = _u32(img, 0x34)
    sw_words = _u32(img, 0x38)
    sw_size = sw_words * 4

    pending = []   # (kind, index, offset, size)
    pending.append(("switch_setup", 0, sw_off, sw_size))
    for i in range(ncore):
        base = CORE_TABLE_OFFSET + CORE_RECORD_SIZE * i
        off = _u32(img, base + 0x00)
        words = _u32(img, base + 0x08)
        pending.append(("core%d" % i, i, off, words * 4))

    bounds_ok = True
    for kind, idx, off, sz in pending:
        inb = (sz >= 8 and sz % 4 == 0 and off >= header_size and off + sz <= r.size)
        if not inb:
            bounds_ok = False
        if not inb:
            r.sections.append(Section(kind, idx, off, sz, None, None, False, "", False))
            continue
        sec = img[off:off + sz]
        body = sec[:-4]
        stored = _u32(sec, sz - 4)
        calc = section_crc(body)
        r.sections.append(Section(kind, idx, off, sz, stored, calc, stored == calc,
                                  sha256_hex(sec), True))

    r.add("全セクションがサイズ内・ワード整列", bounds_ok,
          ", ".join("%s@0x%X+0x%X" % (s.kind, s.offset, s.size) for s in r.sections))

    for s in r.sections:
        if not s.in_bounds:
            continue
        label = {"switch_setup": "Switch Setup CRC 再計算一致"}.get(s.kind, "Core %d CRC 再計算一致" % s.index)
        r.add(label, s.crc_ok, "stored 0x%08X / calc 0x%08X" % (s.stored_crc, s.calc_crc))

    # ---- E. レイアウト (隙間なし / 2 コア境界) ---------------------------
    ordered = sorted(r.sections, key=lambda s: s.offset)
    contiguous = bool(ordered) and ordered[0].offset == header_size
    for a, b in zip(ordered, ordered[1:]):
        if a.end != b.offset:
            contiguous = False
    end_at_size = bool(ordered) and ordered[-1].end == r.size
    r.add("セクションがヘッダ直後から隙間なく連続", contiguous,
          " / ".join("%s[0x%X..0x%X)" % (s.kind, s.offset, s.end) for s in ordered))
    r.add("最終セクション末尾 == size", end_at_size,
          "0x%X vs 0x%X" % (ordered[-1].end if ordered else -1, r.size))

    if ncore >= 2:
        c0, c1 = r.sections[1], r.sections[2]
        r.add("2 コア境界: core1.offset == core0.offset + core0.size",
              c1.offset == c0.end,
              "core0.end=0x%X core1.offset=0x%X" % (c0.end, c1.offset))
    if ncore != 2:
        r.add("コア数 == 2 (DO400 の実機構成)", False,
              "num_cores = %d" % ncore, fatal=False)

    # ---- F. DFU 上限ワード (core0 / RAM 0x8AF98) --------------------------
    core0 = r.sections[1]
    limit_ok = False
    if core0.in_bounds and core0.size > DFU_LIMIT_CORE_OFFSET + 4:
        off = core0.offset + DFU_LIMIT_CORE_OFFSET
        r.dfu_limit_offset = off
        r.dfu_limit = _u32(img, off)
        limit_ok = True
    r.add("DFU 上限ワード (RAM 0x8AF98) を読み出し", limit_ok,
          ("0x%08X @ container+0x%X" % (r.dfu_limit, r.dfu_limit_offset)) if limit_ok
          else "core 0 が小さすぎて 0x8AF98 を含まない", fatal=False)
    if limit_ok:
        known = r.dfu_limit in (DFU_LIMIT_200K, DFU_LIMIT_512K)
        r.add("DFU 上限ワードが既知の値 (200 KiB / 512 KiB)", known,
              r.dfu_limit_label, fatal=False)

    # ---- G. 適用可能サイズ判定 -------------------------------------------
    if r.size <= SIZE_LIMIT_200K:
        r.size_class = SIZE_CLASS_DIRECT
    elif r.size <= SIZE_LIMIT_512K:
        r.size_class = SIZE_CLASS_BOOTSTRAP
    else:
        r.size_class = SIZE_CLASS_TOO_LARGE

    r.add("イメージサイズが DFU 上限 512 KiB 以内", r.size <= SIZE_LIMIT_512K,
          "%d B (%.1f KiB) vs %d B" % (r.size, r.size / 1024.0, SIZE_LIMIT_512K))

    if r.size_class == SIZE_CLASS_BOOTSTRAP:
        r.add("512 KiB ブートストラップ導入が必要 (200 KiB 超)", False,
              "純正 FW 3.23/3.29 へ直接送ると失敗する。先に "
              "do400_323_dfu512k_bootstrap.bin を書き込むこと", fatal=False)

    if limit_ok and r.dfu_limit == DFU_LIMIT_200K and r.size > SIZE_LIMIT_200K:
        r.add("このイメージ自身の DFU 上限は 200 KiB のまま", False,
              "導入後も 200 KiB 超の更新には再度ブートストラップが必要", fatal=False)

    return r


def validate_file(path: str) -> ValidationResult:
    """ファイルを読み込んで検証する。読めない場合も結果オブジェクトを返す。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        r = ValidationResult(path=path)
        r.add("ファイルを読み込める", False, str(exc))
        r.size_class = SIZE_CLASS_INVALID
        return r
    r = validate_bytes(data, path)
    return r


# --------------------------------------------------------------------------
# テキスト整形
# --------------------------------------------------------------------------
def _fmt_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    return "%s B (%.1f KiB, 0x%X)" % (format(n, ","), n / 1024.0, n)


def format_report(r: ValidationResult, verbose: bool = True) -> str:
    """CLI / GUI 共通のテキストレポート。"""
    lines = []
    lines.append("=" * 68)
    lines.append(" コンテナ検証: %s" % (r.path or "<bytes>"))
    lines.append("=" * 68)
    lines.append(" 判定          : %s%s" % (r.verdict,
                 "" if r.write_allowed else "   [書込ロック]"))
    lines.append(" ファイルサイズ: %s" % _fmt_size(r.file_size))
    lines.append(" SHA-256       : %s" % (r.sha256 or "-"))
    if r.magic is not None:
        lines.append(" Magic         : 0x%08X%s" % (r.magic,
                     "" if r.magic == IMAGE_MAGIC else "  (不正)"))
        lines.append(" Version       : %s" % r.version)
        lines.append(" Size / Length : %s / %s" % (_fmt_size(r.size), _fmt_size(r.length)))
        lines.append(" Features      : 0x%08X" % (r.features or 0))
        lines.append(" コア数        : %s" % r.num_cores)
        lines.append(" Page CRC      : stored 0x%08X / calc 0x%08X  %s" % (
            r.page_crc_stored or 0, r.page_crc_calc or 0,
            "OK" if r.page_crc_stored == r.page_crc_calc else "NG"))
        lines.append(" Image CRC     : stored 0x%08X / calc 0x%08X  %s" % (
            r.image_crc_stored or 0, r.image_crc_calc or 0,
            "OK" if r.image_crc_stored == r.image_crc_calc else "NG"))
        lines.append(" DFU 上限      : %s" % r.dfu_limit_label)
        if r.dfu_limit_offset is not None:
            lines.append("   (コンテナ +0x%X = core0 +0x%X / RAM 0x%X)" % (
                r.dfu_limit_offset, DFU_LIMIT_CORE_OFFSET, DFU_LIMIT_RAM_ADDR))
    lines.append(" サイズ区分    : %s" % r.size_class_label)
    if r.sections:
        lines.append("")
        lines.append(" セクション:")
        for s in r.sections:
            lines.append("   %-13s @0x%06X + 0x%06X  CRC 0x%08X %s" % (
                s.kind, s.offset, s.size,
                s.stored_crc if s.stored_crc is not None else 0,
                "OK" if s.crc_ok else "NG"))
            if verbose:
                lines.append("       sha256 %s" % s.sha256)
    if verbose:
        lines.append("")
        lines.append(" チェック詳細:")
        for c in r.checks:
            lines.append("   [%s] %s%s" % (c.mark, c.name, ("  — " + c.detail) if c.detail else ""))
        lines.append("")
        lines.append("  合計: %d 件 / 失敗 %d / 警告 %d" % (
            len(r.checks), len(r.failures), len(r.warnings)))
    if r.size_class == SIZE_CLASS_BOOTSTRAP:
        lines.append("")
        lines.append(" ** 2 段階 DFU 手順 **")
        lines.append("   1) do400_323_dfu512k_bootstrap.bin (183,360 B) を書き込む")
        lines.append("   2) 再起動後、このイメージ (%s) を書き込む" % _fmt_size(r.size))
    elif r.size_class == SIZE_CLASS_DIRECT:
        lines.append("")
        lines.append(" 1 回の DFU で書き込み可能 (200 KiB 以内)。")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI (fw_validator.py <file>...)
# --------------------------------------------------------------------------
def _main(argv: Sequence[str]) -> int:
    if not argv:
        print("usage: python fw_validator.py <image.bin> [...]")
        return 2
    rc = 0
    for p in argv:
        r = validate_file(p)
        print(format_report(r))
        print()
        if not r.write_allowed:
            rc = 1
    return rc


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    sys.exit(_main(sys.argv[1:]))
