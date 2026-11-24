#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_image1.py -- XMOS xCORE-200/XCORE.AI (XU316) SPI boot-image packer.

OFF-LINE ONLY: this script never opens a USB
device, never talks to a programmer and never touches the real DO400 hardware.

It implements the XMOS "xCORE flash format" upgrade image container that the
stage-2 loader (factory loader, tools 3.29 / xflash --factory-version 15.1)
accepts, as reverse engineered from `do400_dump.bin` and verified bit-exactly
against `factory_review/image1.bin`.

--------------------------------------------------------------------------
FLASH BYTE ORDER
--------------------------------------------------------------------------
Everything in the boot partition is stored **nibble-swapped per byte** in the
SPI NOR device (each stored byte = swap_nibbles(byte in CPU address space)).
The stage-1 ROM undoes this when it streams the loader, and the stage-2 loader
undoes it when it copies an image into RAM.  `nibble_swap()` implements it;
it is an involution, so the same function encodes and decodes.

--------------------------------------------------------------------------
IMAGE LAYOUT (little endian, all offsets relative to the image start)
--------------------------------------------------------------------------
  0x00  u32  magic            0x00FF51DE
  0x04  u32  page_crc         CRC over bytes [0x0C .. page_size)
  0x08  u32  image_crc        CRC over bytes [0x0C .. size)
  0x0C  u32  features         0x40 = switch setup present
  0x10  u32  size             total image size in bytes (incl. this header)
  0x14  u32  version          0 = factory image, >=1 = upgrade image
  0x18  u32  length           == size for non-overlay images
  0x1C  u32  num_cores
  0x20  u32  boot_address     0
  0x24  u32  overlay_address  0
  0x28  u32  overlay_offset   0
  0x2C  u32  switch_setup_table_offset   0x34
  0x30  u32  core_table_offset           0x3C
  0x34  section entry for switch setup: {u32 offset, u32 words}
  0x3C  core table, one 28-byte record per core:
            +0x00 u32 offset
            +0x04 u32 channel_end_info
            +0x08 u32 words
            +0x0C u32 swmem_address
            +0x10 u32 swmem_offset
            +0x14 u32 swmem_words
            +0x18 u32 extmem_size
  0x74  switch setup blob
        ... core 0 payload (.text of tile 0)
        ... core 1 payload (.text of tile 1)

Detected by decoding the factory dump:
  image1 header size 0x74, swsetup 0x1F0C @0x74, core0 0xEBAC @0x1F80,
  core1 0x1C114 @0x10B2C, total 0x2CC40  == xflash "Size".

--------------------------------------------------------------------------
CRC  (verified: bit-exact against xsim's `crc32` builtin AND against the
stage-2 loader's own Stage2Loader_CRC.o object code)
--------------------------------------------------------------------------
  hw_crc32(checksum, data, poly):
      for i in 0..31:
          xor_bit    = checksum & 1
          checksum   = (checksum >> 1) | ((data & 1) << 31)
          data     >>= 1
          if xor_bit: checksum ^= poly

  crc(blob) = ~ hw_crc32 over blob, seed 0xFFFFFFFF, poly 0xEDB88320,
              then one extra all-zero 32-bit word
            (stage-2 loader always calls crc32() once more with r2 = 0)

  page_crc  = crc(image[0x0C : page_size])     page_size = flash page = 0x100
  image_crc = crc(image[0x0C : size])
  loader_crc= crc(flash[0x0000 : 0x14D0])  (loader block incl. its size word)

The 0x0C-byte prologue (magic + the two CRC fields) is deliberately excluded,
which is exactly what Stage2Loader_Image.o does:
    crc(image + 0x0C, (page_words - 3), ...)   ; layout of the check

------------------------------------------------------------------
SECTION CRC  (the 4-byte trailer of switch setup / each core)
------------------------------------------------------------------
Every section inside the container ends with one CRC word.  xflash prints it
as "CRC: 0x........ (GOOD|FAIL)" for Switch setup / Core 0 / Core 1.

    body              = section[0 : n_bytes - 4]        ; the CRC word removed
    n_body_words      = len(body) // 4
    section_crc(body) = xmos_crc(pack("<I", n_body_words) + body)

i.e. the same crc32 routine, but the word count is fed in *before* the
payload (the loader's CRC helper takes (data, n_words) and the section check
uses the string "length word || payload" rather than a trailing zero word).

Verified bit-exact on all six stock sections of the shipping firmware:

    image0_swsetup 0x7C02D193   image0_core0 0xD8F12906   image0_core1 0x6DCCFBBE
    image1_swsetup 0x7C02D193   image1_core0 0x39B08745   image1_core1 0x6DCCFBBE

The stock ELFs produced by the link step already carry this word at the end of
.text, so a bit-exact round trip must *keep* it.  ELFs that were not passed
through that link step (custom per-tile ELFs) still hold a placeholder
(0x00000000 / 0xFFFFFFFF); `--fix-section-crc` recomputes and patches them
after checking that the 16 bytes in front of the trailer are untouched
padding (all 0x00 or all 0xFF), so no code or data byte is ever overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys

M32 = 0xFFFFFFFF
CRC_POLY = 0xEDB88320
IMAGE_MAGIC = 0x00FF51DE
DEFAULT_PAGE_SIZE = 0x100
HEADER_SIZE = 0x74
FLASH_SIZE = 0x200000

EM_XCORE = 203

_TAB = []
for _i in range(256):
    _x = _i
    for _ in range(8):
        _x = (_x >> 1) ^ (CRC_POLY if _x & 1 else 0)
    _TAB.append(_x & M32)


# --------------------------------------------------------------------------
# byte order
# --------------------------------------------------------------------------
def nibble_swap(data: bytes) -> bytes:
    """Swap the two nibbles of every byte (involution)."""
    return bytes(((b & 0x0F) << 4) | (b >> 4) for b in data)


# --------------------------------------------------------------------------
# CRC
# --------------------------------------------------------------------------
def _crc_step(state: int, byte: int) -> int:
    return ((state >> 8) ^ _TAB[state & 0xFF] ^ (byte << 24)) & M32


def hw_crc32(seed: int, data: bytes) -> int:
    """Bit-exact model of the xCORE `crc32` instruction over a byte stream."""
    state = seed
    for b in data:
        state = _crc_step(state, b)
    return state


def xmos_crc(data: bytes, seed: int = M32, trailing_zero_word: bool = True) -> int:
    """The CRC value xflash stores (final complement included)."""
    state = hw_crc32(seed, data)
    if trailing_zero_word:
        for _ in range(4):
            state = _crc_step(state, 0)
    return (~state) & M32


def page_crc(image: bytes, page_size: int = DEFAULT_PAGE_SIZE) -> int:
    return xmos_crc(image[0x0C:page_size])


def image_crc(image: bytes) -> int:
    size = struct.unpack_from("<I", image, 0x10)[0]
    return xmos_crc(image[0x0C:size])


# --------------------------------------------------------------------------
# section CRC (switch setup / core trailer word)
# --------------------------------------------------------------------------
PADDING_16 = (b"\x00" * 16, b"\xFF" * 16)


def section_crc(body: bytes) -> int:
    """The 4-byte trailer value xflash stores at the end of every section.

    `body` is the section payload with its trailing CRC word removed.
    """
    if len(body) % 4:
        raise ValueError("section body length 0x%X is not word aligned" % len(body))
    return xmos_crc(struct.pack("<I", len(body) // 4) + body)


def split_section(section: bytes) -> tuple[bytes, int, int]:
    """-> (body, stored_crc, recomputed_crc)."""
    if len(section) < 8 or len(section) % 4:
        raise ValueError("section length 0x%X is not a word-aligned body+CRC" % len(section))
    body = section[:-4]
    stored = struct.unpack_from("<I", section, len(section) - 4)[0]
    return body, stored, section_crc(body)


def fix_section_trailer(section: bytes, label: str,
                        allow_non_padding: bool = False) -> tuple[bytes, str]:
    """Return (section, state) with a correct section CRC word.

    state is one of
        "native"      -- the trailer already equals section_crc(body): keep it
                         byte for byte (this is what makes the stock round trip
                         bit-exact),
        "patched(..)" -- a placeholder was overwritten with the recomputed CRC,
        "rewritten(..)"-- the trailer was a valid CRC of the *pre-patch* body and
                         is recomputed after a deliberate code patch.

    A placeholder (or, with allow_non_padding, any stale trailer) is only
    overwritten when the caller knows the trailer is a CRC word.  Without that
    knowledge the 16 bytes in front must be pure padding (all 0x00 or all 0xFF);
    otherwise the trailer may be real code or data and the packer refuses.
    """
    body, stored, calc = split_section(section)
    if stored == calc:
        return section, "native"
    if not allow_non_padding and body[-16:] not in PADDING_16:
        raise ValueError(
            "%s: trailing word 0x%08X != section CRC 0x%08X and the preceding 16 "
            "bytes are not untouched padding (%s) -- refusing to patch"
            % (label, stored, calc, body[-16:].hex()))
    out = body + struct.pack("<I", calc)
    return out, "%s(0x%08X->0x%08X)" % ("rewritten" if allow_non_padding else "patched",
                                        stored, calc)


# --------------------------------------------------------------------------
# ELF -> core payload
# --------------------------------------------------------------------------
def elf_core_payload(path: str) -> tuple[bytes, int]:
    """Concatenate the PT_LOAD file images of a per-tile xcore ELF.

    Returns (payload, load_address).  The factory/toolchain emits exactly one
    PT_LOAD whose p_filesz == p_memsz == .text size (verified for
    img1_tile0.elf / img1_tile1.elf / custom tile0.elf / tile1.elf).
    """
    with open(path, "rb") as fh:
        elf = fh.read()
    if elf[:4] != b"\x7fELF":
        raise ValueError("%s: not an ELF file" % path)
    if elf[4] != 1:
        raise ValueError("%s: not a 32-bit ELF (EI_CLASS=%d)" % (path, elf[4]))
    machine = struct.unpack_from("<H", elf, 18)[0]
    if machine != EM_XCORE:
        raise ValueError("%s: e_machine=%d, expected %d (EM_XCORE)" % (path, machine, EM_XCORE))

    e_phoff = struct.unpack_from("<I", elf, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", elf, 42)

    chunks = []
    load_addr = None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, p_offset, p_vaddr, p_paddr, p_filesz, p_memsz, _f, _a = struct.unpack_from(
            "<8I", elf, off)
        if p_type != 1:  # PT_LOAD
            continue
        if p_filesz != p_memsz:
            raise ValueError("%s: PT_LOAD %d has filesz=0x%X != memsz=0x%X "
                             "(BSS would need a zero fill the boot image cannot express)"
                             % (path, i, p_filesz, p_memsz))
        if load_addr is None:
            load_addr = p_paddr
        elif p_paddr != load_addr + len(chunks[0]) and p_paddr != load_addr:
            pass
        chunks.append(elf[p_offset:p_offset + p_filesz])
    if not chunks:
        raise ValueError("%s: no PT_LOAD segment" % path)
    payload = b"".join(chunks)
    if len(payload) % 4:
        raise ValueError("%s: payload 0x%X is not word aligned" % (path, len(payload)))
    return payload, load_addr


# --------------------------------------------------------------------------
# image builder
# --------------------------------------------------------------------------
class CoreSpec:
    def __init__(self, payload: bytes, channel_end: int = 0):
        self.payload = payload
        self.channel_end = channel_end


def build_image(cores, swsetup: bytes, version: int = 1, features: int = 0x40,
                boot_address: int = 0, overlay_address: int = 0,
                overlay_offset: int = 0) -> bytes:
    """Build the *logical* (de-nibbled) image container."""
    if len(swsetup) % 4:
        raise ValueError("switch setup blob must be word aligned")
    n = len(cores)
    hdr_size = 0x3C + 28 * n
    if hdr_size != HEADER_SIZE:
        raise ValueError("only 2-core images are supported (header size 0x74)")

    sw_off = hdr_size
    cur = sw_off + len(swsetup)
    entries = []
    for c in cores:
        if len(c.payload) % 4:
            raise ValueError("core payload must be word aligned")
        entries.append((cur, c))
        cur += len(c.payload)
    size = cur

    img = bytearray(size)
    struct.pack_into("<I", img, 0x00, IMAGE_MAGIC)
    struct.pack_into("<I", img, 0x04, 0)          # page_crc, filled below
    struct.pack_into("<I", img, 0x08, 0)          # image_crc, filled below
    struct.pack_into("<I", img, 0x0C, features)
    struct.pack_into("<I", img, 0x10, size)
    struct.pack_into("<I", img, 0x14, version)
    struct.pack_into("<I", img, 0x18, size)       # length
    struct.pack_into("<I", img, 0x1C, n)
    struct.pack_into("<I", img, 0x20, boot_address)
    struct.pack_into("<I", img, 0x24, overlay_address)
    struct.pack_into("<I", img, 0x28, overlay_offset)
    struct.pack_into("<I", img, 0x2C, 0x34)       # switch setup table offset
    struct.pack_into("<I", img, 0x30, 0x3C)       # core table offset
    struct.pack_into("<I", img, 0x34, sw_off)
    struct.pack_into("<I", img, 0x38, len(swsetup) // 4)
    img[sw_off:sw_off + len(swsetup)] = swsetup

    for i, (off, c) in enumerate(entries):
        base = 0x3C + 28 * i
        struct.pack_into("<I", img, base + 0x00, off)
        struct.pack_into("<I", img, base + 0x04, c.channel_end)
        struct.pack_into("<I", img, base + 0x08, len(c.payload) // 4)
        struct.pack_into("<I", img, base + 0x0C, 0)   # swmem address
        struct.pack_into("<I", img, base + 0x10, 0)   # swmem offset
        struct.pack_into("<I", img, base + 0x14, 0)   # swmem words
        struct.pack_into("<I", img, base + 0x18, 0)   # extmem size
        img[off:off + len(c.payload)] = c.payload

    struct.pack_into("<I", img, 0x04, page_crc(bytes(img)))
    struct.pack_into("<I", img, 0x08, image_crc(bytes(img)))
    return bytes(img)


def pack_image1_from_elfs(tile0_elf: str, tile1_elf: str, swsetup_path: str,
                          version: int = 1, chanend0: int = 2, chanend1: int = 0x10002,
                          features: int = 0x40, flash_order: bool = True,
                          fix_section_crc: bool = True,
                          patches: list | None = None,
                          section_report: list | None = None) -> bytes:
    """Pack an upgrade image from two per-tile ELFs.

    `fix_section_crc` recomputes a missing/placeholder section CRC trailer
    (default).  When the trailer is already correct it is preserved, so the
    stock round trip stays bit-exact.

    `patches` is an optional list of (load_address, u32_value, u32_expected)
    applied to core 0's payload after it is extracted; the expected old value
    is asserted so a wrong build fails loudly instead of silently patching air.
    """
    c0, addr0 = elf_core_payload(tile0_elf)
    c1, addr1 = elf_core_payload(tile1_elf)
    if addr0 != 0x80000 or addr1 != 0x80000:
        raise ValueError("unexpected load address (tile0=%#x tile1=%#x)" % (addr0, addr1))

    log = [] if section_report is None else section_report

    # a trailer that is already the correct section CRC is a CRC word, so after
    # a deliberate code patch it may be recomputed even though the bytes right
    # in front of it are real code (the stock FW 3.23 tile 0 case).
    c0_was_native = split_section(c0)[1] == split_section(c0)[2]
    patched0 = False

    for pa, val, expect in (patches or []):
        off = pa - addr0
        if not (0 <= off <= len(c0) - 4):
            raise ValueError("patch address 0x%X is outside the tile 0 payload" % pa)
        old = struct.unpack_from("<I", c0, off)[0]
        if old != expect:
            raise ValueError("patch at 0x%X: expected 0x%08X, found 0x%08X"
                             % (pa, expect, old))
        c0 = c0[:off] + struct.pack("<I", val) + c0[off + 4:]
        patched0 = True
        log.append(("core0[0x%X]" % pa, "patched(0x%08X->0x%08X)" % (old, val)))

    with open(swsetup_path, "rb") as fh:
        sw = fh.read()

    if fix_section_crc:
        sw, st = fix_section_trailer(sw, "switch_setup")
        log.append(("switch_setup", st))
        c0, st = fix_section_trailer(c0, "core0",
                                     allow_non_padding=(c0_was_native and patched0))
        log.append(("core0", st))
        c1, st = fix_section_trailer(c1, "core1")
        log.append(("core1", st))

    img = build_image([CoreSpec(c0, chanend0), CoreSpec(c1, chanend1)], sw,
                      version=version, features=features)
    return nibble_swap(img) if flash_order else img


# --------------------------------------------------------------------------
# flash image assembly
# --------------------------------------------------------------------------
def build_flash_image(base_dump: bytes, image_flash: bytes, image_offset: int = 0x2E000,
                      erase_to: int | None = None) -> bytes:
    """Copy `base_dump`, erase [image_offset, erase_to) to 0xFF and place the image."""
    flash = bytearray(base_dump)
    if len(flash) != FLASH_SIZE:
        raise ValueError("base dump is 0x%X bytes, expected 0x%X" % (len(flash), FLASH_SIZE))
    if erase_to is None:
        # erase everything after the upgrade image offset (data partition unused)
        erase_to = len(flash)
    if image_offset + len(image_flash) > erase_to:
        raise ValueError("image (0x%X @0x%X) exceeds the erase window end 0x%X"
                         % (len(image_flash), image_offset, erase_to))
    flash[image_offset:erase_to] = b"\xFF" * (erase_to - image_offset)
    flash[image_offset:image_offset + len(image_flash)] = image_flash
    return bytes(flash)


# --------------------------------------------------------------------------
# header dump helper
# --------------------------------------------------------------------------
def describe_image(image_logical: bytes) -> dict:
    magic, pc, ic, feat, size, ver, length, ncore = struct.unpack_from("<8I", image_logical, 0)
    out = dict(magic=magic, page_crc=pc, image_crc=ic, features=feat, size=size,
               version=ver, length=length, num_cores=ncore,
               page_crc_ok=(pc == page_crc(image_logical)),
               image_crc_ok=(ic == image_crc(image_logical)))
    cores = []
    for i in range(ncore):
        b = 0x3C + 28 * i
        off, ce, words, sma, smo, smw, ext = struct.unpack_from("<7I", image_logical, b)
        _body, stored, calc = split_section(image_logical[off:off + words * 4])
        cores.append(dict(offset=off, channel_end=ce, words=words,
                          size=words * 4, crc=calc, crc_stored=stored,
                          crc_ok=(stored == calc)))
    sw_off, sw_words = struct.unpack_from("<2I", image_logical, 0x34)
    _sbody, sw_stored, sw_calc = split_section(image_logical[sw_off:sw_off + sw_words * 4])
    out["switch_setup"] = dict(offset=sw_off, words=sw_words,
                               crc=sw_calc, crc_stored=sw_stored,
                               crc_ok=(sw_stored == sw_calc))
    out["cores"] = cores
    return out


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Pack an XMOS XU316 upgrade image (offline only)")
    ap.add_argument("--tile0-elf", required=True)
    ap.add_argument("--tile1-elf", required=True)
    ap.add_argument("--swsetup", required=True)
    ap.add_argument("--out", required=True, help="output file")
    ap.add_argument("--version", type=lambda s: int(s, 0), default=1)
    ap.add_argument("--chanend0", type=lambda s: int(s, 0), default=2)
    ap.add_argument("--chanend1", type=lambda s: int(s, 0), default=0x10002)
    ap.add_argument("--logical", action="store_true",
                    help="write the de-nibbled image instead of the flash byte order")
    ap.add_argument("--fix-section-crc", dest="fix_section_crc", action="store_true",
                    default=True,
                    help="recompute a placeholder section CRC trailer (default: on; "
                         "a trailer that is already correct is preserved untouched)")
    ap.add_argument("--no-fix-section-crc", dest="fix_section_crc", action="store_false",
                    help="keep every section trailer exactly as it is in the ELF")
    ap.add_argument("--patch-u32", action="append", default=[], metavar="ADDR=VALUE[:OLD]",
                    help="patch one u32 of the tile 0 payload by RAM address "
                         "(offline only; OLD is asserted when given)")
    ap.add_argument("--flash-base", help="2 MiB factory dump to use as the base")
    ap.add_argument("--flash-out", help="output path for the full 2 MiB flash image")
    ap.add_argument("--image-offset", type=lambda s: int(s, 0), default=0x2E000)
    a = ap.parse_args(argv)

    patches = []
    for spec in a.patch_u32:
        addr_s, val_s = spec.split("=", 1)
        old = None
        if ":" in val_s:
            val_s, old_s = val_s.split(":", 1)
            old = int(old_s, 0)
        patches.append((int(addr_s, 0), int(val_s, 0), old))
    if any(p[2] is None for p in patches):
        # resolve the missing "expected" values from the ELF itself
        base, _ = elf_core_payload(a.tile0_elf)
        patches = [(pa, v, struct.unpack_from("<I", base, pa - 0x80000)[0])
                   if e is None else (pa, v, e) for pa, v, e in patches]

    report = []
    img_flash = pack_image1_from_elfs(a.tile0_elf, a.tile1_elf, a.swsetup,
                                      version=a.version, chanend0=a.chanend0,
                                      chanend1=a.chanend1, flash_order=not a.logical,
                                      fix_section_crc=a.fix_section_crc,
                                      patches=patches, section_report=report)
    with open(a.out, "wb") as fh:
        fh.write(img_flash)
    logical = nibble_swap(img_flash) if not a.logical else img_flash
    info = describe_image(logical)
    print("%s: %d bytes sha256=%s" % (a.out, len(img_flash), sha256(img_flash)))
    for name, state in report:
        print("  trailer %-14s %s" % (name, state))
    print("  size=0x%X version=%d page_crc=%08X(%s) image_crc=%08X(%s)" % (
        info["size"], info["version"], info["page_crc"],
        "ok" if info["page_crc_ok"] else "SELF-CHECK FAILED",
        info["image_crc"], "ok" if info["image_crc_ok"] else "SELF-CHECK FAILED"))
    print("  switch_setup CRC %08X (%s)  core0 CRC %08X (%s)  core1 CRC %08X (%s)" % (
        info["switch_setup"]["crc"], "ok" if info["switch_setup"]["crc_ok"] else "FAIL",
        info["cores"][0]["crc"], "ok" if info["cores"][0]["crc_ok"] else "FAIL",
        info["cores"][1]["crc"], "ok" if info["cores"][1]["crc_ok"] else "FAIL"))
    if not (info["page_crc_ok"] and info["image_crc_ok"]):
        return 1
    if not (info["switch_setup"]["crc_ok"] and all(x["crc_ok"] for x in info["cores"])):
        print("  ERROR: section CRC mismatch")
        return 1

    if a.flash_base:
        if not a.flash_out:
            ap.error("--flash-base requires --flash-out")
        base = open(a.flash_base, "rb").read()
        flash = build_flash_image(base, img_flash, a.image_offset)
        with open(a.flash_out, "wb") as fh:
            fh.write(flash)
        print("%s: %d bytes sha256=%s" % (a.flash_out, len(flash), sha256(flash)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
