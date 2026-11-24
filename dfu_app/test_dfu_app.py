#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_dfu_app.py -- DO400 DFU 更新アプリの自動テスト.

    python -m unittest -v test_dfu_app

実機への書込み・DFU 遷移は **一切行わない**。実機を使うテストは
`--check-device` 相当の読み取り専用 API のみで、書込系が呼ばれたら
`ReadOnlyGuardBackend` が即座に失敗させる。

**既定は 100% オフライン**。実機 DO400 を開くテスト (クラス F) は
環境変数 `DO400_ALLOW_LIVE_RO=1` を明示したときだけ動く。

カバー範囲
--------------------------------------------------------------------------
 A. fw_validator : 実ファイル 2 本 (183,360 B / 308,352 B) の全 CRC・サイズ判定
 B. fw_validator : 改ざん・サイズ超過・非コンテナゴミを確実に REJECT
 C. thesycon_api : モック DLL による Download / Upload / Revert の状態遷移、
                   進捗、エラー時の EndDfuProc 確実呼出、キャンセル、タイムアウト
 D. 書込ガード      : allow_write なしでは書込 API が呼べないこと
 E. ドライラン      : 書込シーケンスの完全ドライラン
 F. 読み取り専用実機: VID=0x152A / PID=0x85DD / bcdDevice=0x0323 / 44100 Hz
                      (DO400_ALLOW_LIVE_RO=1 の opt-in のみ)
 G. F-2            : LoadFirmwareImageFromFile の `wchar_t*` バインド、
                     LoadFirmwareImageFromBuffer 標準経路 (TOCTOU 根絶)
 H. F-4            : Finished + completion != 0 の失敗判定、転送中キャンセル抑止
 I. F-5            : 実機テストが既定でスキップされること
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import os
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fw_validator as V          # noqa: E402
import thesycon_api as T          # noqa: E402
import do400_dfu_app as APP       # noqa: E402

FW_OUT = os.path.abspath(os.path.join(HERE, "..", "firmware"))
BOOTSTRAP = os.path.join(FW_OUT, "do400_peq_dfu.bin")
UPGRADE = os.path.join(FW_OUT, "do400_upgrade_over200k.bin")

HAVE_BOOTSTRAP = os.path.isfile(BOOTSTRAP)
HAVE_UPGRADE = os.path.isfile(UPGRADE)

# 実測済みの期待値 (出荷 DFU イメージ / xflash --analyze と一致)
BOOTSTRAP_SIZE = 200896
BOOTSTRAP_PAGE_CRC = 0xFD9A2E77
BOOTSTRAP_IMAGE_CRC = 0xD6DFC3E6
BOOTSTRAP_SW_CRC = 0x7C02D193
BOOTSTRAP_CORE0_CRC = 0x42D3C4B9
BOOTSTRAP_CORE1_CRC = 0x6DCCFBBE
BOOTSTRAP_SHA256 = "ce93b58089fae5a1c7894050cd6cc9440929b558a10501fe5667bd40ba74c7fd"

UPGRADE_SIZE = 308352
UPGRADE_PAGE_CRC = 0xF3086F34
UPGRADE_IMAGE_CRC = 0xC925F218
UPGRADE_CORE0_CRC = 0x004AEDD9
UPGRADE_CORE1_CRC = 0x3957C92F
UPGRADE_SHA256 = "d3378851f73aec8122075bf1665e9de87ed54451faa4a5cd57218ace5474a803"


# --------------------------------------------------------------------------
# コンテナ組み立てヘルパ (テスト専用)
# --------------------------------------------------------------------------
def build_container(core0_body: bytes, core1_body: bytes, sw_body: bytes) -> bytes:
    """正しい CRC を持つコンテナファイル (ニブルスワップ済み) を組み立てる。"""
    for b in (core0_body, core1_body, sw_body):
        assert len(b) % 4 == 0
    sw = sw_body + struct.pack("<I", V.section_crc(sw_body))
    c0 = core0_body + struct.pack("<I", V.section_crc(core0_body))
    c1 = core1_body + struct.pack("<I", V.section_crc(core1_body))

    header = 0x3C + 28 * 2
    sw_off = header
    c0_off = sw_off + len(sw)
    c1_off = c0_off + len(c0)
    size = c1_off + len(c1)

    img = bytearray(size)
    struct.pack_into("<I", img, 0x00, V.IMAGE_MAGIC)
    struct.pack_into("<I", img, 0x0C, 0x40)          # features (switch setup)
    struct.pack_into("<I", img, 0x10, size)
    struct.pack_into("<I", img, 0x14, 1)             # version 1 = upgrade
    struct.pack_into("<I", img, 0x18, size)
    struct.pack_into("<I", img, 0x1C, 2)             # num cores
    struct.pack_into("<I", img, 0x2C, 0x34)
    struct.pack_into("<I", img, 0x30, 0x3C)
    struct.pack_into("<I", img, 0x34, sw_off)
    struct.pack_into("<I", img, 0x38, len(sw) // 4)
    img[sw_off:sw_off + len(sw)] = sw
    for i, (off, sec) in enumerate(((c0_off, c0), (c1_off, c1))):
        base = 0x3C + 28 * i
        struct.pack_into("<I", img, base + 0x00, off)
        struct.pack_into("<I", img, base + 0x04, 2 if i == 0 else 0x10002)
        struct.pack_into("<I", img, base + 0x08, len(sec) // 4)
        img[off:off + len(sec)] = sec
    struct.pack_into("<I", img, 0x04, V.page_crc(bytes(img)))
    struct.pack_into("<I", img, 0x08, V.image_crc(bytes(img), size))
    return V.nibble_swap(bytes(img))


def split_container(data: bytes):
    """(core0_body, core1_body, sw_body) を取り出す。トレーラは除く。"""
    img = V.nibble_swap(data)
    sw_off, sw_words = struct.unpack_from("<2I", img, 0x34)
    cores = []
    for i in range(struct.unpack_from("<I", img, 0x1C)[0]):
        b = 0x3C + 28 * i
        off, _ce, words = struct.unpack_from("<3I", img, b)
        cores.append(img[off:off + words * 4 - 4])
    return cores[0], cores[1], img[sw_off:sw_off + sw_words * 4 - 4]


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


@unittest.skipUnless(HAVE_BOOTSTRAP, "bootstrap パッケージがありません")
class TestValidatorKnownImages(unittest.TestCase):
    """A. 実在する 2 つの正しいイメージを完全に検証できる。"""

    @classmethod
    def setUpClass(cls):
        cls.boot = V.validate_file(BOOTSTRAP)
        cls.upg = V.validate_file(UPGRADE) if HAVE_UPGRADE else None

    def test_bootstrap_all_good(self):
        r = self.boot
        self.assertTrue(r.ok, [c.name for c in r.failures])
        self.assertTrue(r.write_allowed)
        self.assertEqual(r.verdict, "ALL GOOD")
        self.assertEqual(r.file_size, BOOTSTRAP_SIZE)
        self.assertEqual(r.size, BOOTSTRAP_SIZE)
        self.assertEqual(r.magic, V.IMAGE_MAGIC)
        self.assertEqual(r.version, 1)
        self.assertEqual(r.num_cores, 2)
        self.assertEqual(r.page_crc_stored, BOOTSTRAP_PAGE_CRC)
        self.assertEqual(r.page_crc_calc, BOOTSTRAP_PAGE_CRC)
        self.assertEqual(r.image_crc_stored, BOOTSTRAP_IMAGE_CRC)
        self.assertEqual(r.image_crc_calc, BOOTSTRAP_IMAGE_CRC)
        self.assertEqual(r.sha256, BOOTSTRAP_SHA256)

    def test_bootstrap_size_class_direct(self):
        r = self.boot
        self.assertEqual(r.size_class, V.SIZE_CLASS_DIRECT)
        self.assertFalse(r.requires_bootstrap)
        self.assertLessEqual(r.size, V.SIZE_LIMIT_200K)

    def test_bootstrap_section_crcs(self):
        secs = {s.kind: s for s in self.boot.sections}
        self.assertEqual(set(secs), {"switch_setup", "core0", "core1"})
        self.assertEqual(secs["switch_setup"].stored_crc, BOOTSTRAP_SW_CRC)
        self.assertEqual(secs["core0"].stored_crc, BOOTSTRAP_CORE0_CRC)
        self.assertEqual(secs["core1"].stored_crc, BOOTSTRAP_CORE1_CRC)
        for s in secs.values():
            self.assertTrue(s.crc_ok, s.kind)
            self.assertEqual(s.stored_crc, s.calc_crc)

    def test_bootstrap_dfu_limit_is_512k(self):
        self.assertEqual(self.boot.dfu_limit, V.DFU_LIMIT_512K)
        self.assertEqual(V.DFU_LIMIT_CORE_OFFSET, 0xAF98)      # RAM 0x8AF98 - 0x80000
        core0 = [s for s in self.boot.sections if s.kind == "core0"][0]
        self.assertEqual(self.boot.dfu_limit_offset, core0.offset + 0xAF98)
        self.assertEqual(self.boot.dfu_limit_offset, 0xCF18)
        self.assertIn("512 KiB", self.boot.dfu_limit_label)

    def test_bootstrap_report_mentions_all_good(self):
        text = V.format_report(self.boot)
        self.assertIn("ALL GOOD", text)
        self.assertIn("512 KiB", text)

    @unittest.skipUnless(HAVE_UPGRADE, "upgrade パッケージがありません")
    def test_upgrade_all_good_and_needs_bootstrap(self):
        r = self.upg
        self.assertTrue(r.ok, [c.name for c in r.failures])
        self.assertTrue(r.write_allowed)
        self.assertEqual(r.verdict, "ALL GOOD")
        self.assertEqual(r.file_size, UPGRADE_SIZE)
        self.assertEqual(r.page_crc_calc, UPGRADE_PAGE_CRC)
        self.assertEqual(r.image_crc_calc, UPGRADE_IMAGE_CRC)
        self.assertEqual(r.sha256, UPGRADE_SHA256)
        secs = {s.kind: s for s in r.sections}
        self.assertEqual(secs["core0"].stored_crc, UPGRADE_CORE0_CRC)
        self.assertEqual(secs["core1"].stored_crc, UPGRADE_CORE1_CRC)
        self.assertEqual(r.size_class, V.SIZE_CLASS_BOOTSTRAP)
        self.assertTrue(r.requires_bootstrap)
        self.assertGreater(r.size, V.SIZE_LIMIT_200K)
        self.assertLessEqual(r.size, V.SIZE_LIMIT_512K)

    @unittest.skipUnless(HAVE_UPGRADE, "upgrade パッケージがありません")
    def test_upgrade_self_limit_is_200k(self):
        """200 KiB 超イメージ自身が 200 KiB 制限のままの場合はブートストラップが必須。"""
        self.assertEqual(self.upg.dfu_limit, V.DFU_LIMIT_200K)
        warns = " ".join(c.name for c in self.upg.warnings)
        self.assertIn("200 KiB", warns)

    def test_crc_matches_reference_packer(self):
        """src/pack_image1.py と同一の CRC 実装であること (相互検証)。"""
        sys.path.insert(0, os.path.join(HERE, "..", "src"))
        try:
            import pack_image1 as P
        except Exception:                     # noqa: BLE001
            self.skipTest("pack_image1 が import できません")
        targets = [p for p in (BOOTSTRAP, UPGRADE) if os.path.isfile(p)]
        for path in targets:
            raw = read_bytes(path)
            img = P.nibble_swap(raw)
            size = struct.unpack_from("<I", img, 0x10)[0]
            self.assertEqual(V.page_crc(img), P.page_crc(img))
            self.assertEqual(V.image_crc(img, size), P.image_crc(img))
            self.assertEqual(V.hw_crc32(0xFFFFFFFF, img[:4096]),
                             P.hw_crc32(0xFFFFFFFF, img[:4096]))


class TestValidatorRejects(unittest.TestCase):
    """B. 壊れたイメージは必ず REJECT (書込ロック) される。"""

    @classmethod
    def setUpClass(cls):
        if not HAVE_BOOTSTRAP:
            raise unittest.SkipTest("bootstrap がありません")
        cls.raw = read_bytes(BOOTSTRAP)
        cls.tmp = tempfile.mkdtemp(prefix="do400_validator_")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def _assert_reject(self, path_or_bytes, expect_name_fragment=None):
        if isinstance(path_or_bytes, (bytes, bytearray)):
            r = V.validate_bytes(bytes(path_or_bytes), "<test>")
        else:
            r = V.validate_file(path_or_bytes)
        self.assertFalse(r.ok, "壊れたイメージが合格してしまった")
        self.assertFalse(r.write_allowed, "書込が許可されてしまった")
        self.assertNotEqual(r.verdict, "ALL GOOD")
        if expect_name_fragment:
            joined = " ".join(c.name for c in r.failures)
            self.assertIn(expect_name_fragment, joined)
        return r

    def test_bit_flip_in_core0(self):
        img = V.nibble_swap(self.raw)
        c0_off, _ce, c0_words = struct.unpack_from("<3I", img, 0x3C)
        data = bytearray(self.raw)
        # ニブルスワップ後の 1 バイトを反転 → 元ファイルの同じ位置を反転
        pos = c0_off + 0x2000
        data[pos] ^= 0x01
        r = self._assert_reject(bytes(data), "Image CRC")
        self.assertEqual(r.page_crc_calc, r.page_crc_stored)   # 先頭ページは無傷
        self.assertNotEqual(r.image_crc_calc, r.image_crc_stored)
        core0 = [s for s in r.sections if s.kind == "core0"][0]
        self.assertFalse(core0.crc_ok)

    def test_bit_flip_in_switch_setup(self):
        img = V.nibble_swap(self.raw)
        sw_off = struct.unpack_from("<I", img, 0x34)[0]
        data = bytearray(self.raw)
        data[sw_off + 0x100] ^= 0x80
        r = self._assert_reject(bytes(data))
        sw = [s for s in r.sections if s.kind == "switch_setup"][0]
        self.assertFalse(sw.crc_ok)

    def test_bit_flip_in_header(self):
        data = bytearray(self.raw)
        data[0x10] ^= 0xFF          # ニブルスワップ後の size フィールド
        r = self._assert_reject(bytes(data))
        self.assertNotEqual(r.size, len(data))

    def test_magic_corrupted(self):
        data = bytearray(self.raw)
        data[0x00] ^= 0xFF
        self._assert_reject(bytes(data), "Magic")

    def test_version_zero(self):
        img = bytearray(V.nibble_swap(self.raw))
        struct.pack_into("<I", img, 0x14, 0)
        struct.pack_into("<I", img, 0x04, V.page_crc(bytes(img)))
        struct.pack_into("<I", img, 0x08, V.image_crc(bytes(img)))
        self._assert_reject(V.nibble_swap(bytes(img)), "Version")

    def test_oversized_image_is_locked(self):
        """512 KiB を超える (CRC は正しい) コンテナはサイズ上限でロックされる。"""
        c0, c1, sw = split_container(self.raw)
        target = 614400                       # 600 KiB (64 B 倍数)
        extra = target - len(self.raw)
        self.assertGreater(extra, 0)
        self.assertEqual(extra % 4, 0)
        data = build_container(c0, c1 + b"\x00" * extra, sw)
        self.assertEqual(len(data), target)
        r = self._assert_reject(data, "512 KiB")
        self.assertEqual(r.size_class, V.SIZE_CLASS_TOO_LARGE)
        self.assertEqual(r.verdict, "NG (サイズ超過)")
        crc_failures = [c for c in r.failures
                        if "CRC" in c.name or "Magic" in c.name or "size" in c.name]
        self.assertEqual(crc_failures, [], "CRC/構造自体は正しいこと: %s" %
                         [c.name for c in crc_failures])
        self.assertFalse(r.write_allowed)

    def test_garbage_file(self):
        data = bytes((i * 37 + 11) & 0xFF for i in range(4096))
        r = self._assert_reject(data, "Magic")
        self.assertNotEqual(r.magic, V.IMAGE_MAGIC)

    def test_garbage_not_64_multiple(self):
        self._assert_reject(b"\x00" * 1000, "64 B")

    def test_empty_file(self):
        self._assert_reject(b"", "ファイルサイズ")

    def test_truncated_file(self):
        self._assert_reject(self.raw[:100000], "size == 実ファイルサイズ")

    def test_non_container_text_file(self):
        path = self._write("notes.bin", b"this is not a firmware image\n" * 100)
        self._assert_reject(path, "Magic")

    def test_missing_file(self):
        r = V.validate_file(os.path.join(self.tmp, "does_not_exist.bin"))
        self.assertFalse(r.ok)
        self.assertFalse(r.write_allowed)

    def test_real_file_on_disk_bitflip(self):
        data = bytearray(self.raw)
        data[len(data) // 2] ^= 0x10
        path = self._write("flipped.bin", bytes(data))
        self._assert_reject(path)


class TestMockDfu(unittest.TestCase):
    """C. モック DLL による DFU シーケンス検証 (実機に触れない)。"""

    def _api(self, **kw):
        backend = T.MockBackend(**kw)
        return backend, T.ThesyconApi(backend=backend, allow_write=True)

    # -- Download ----------------------------------------------------------
    def test_download_success_sequence(self):
        backend, api = self._api(dfu_steps=4)
        seen = []
        res = api.dfu_download(image_bytes=b"\xAA" * 1024, on_progress=seen.append,
                               poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.state, T.DFU_STATE_FINISHED)
        self.assertEqual(res.completion, 0)
        self.assertEqual(backend.ended, 1, "EndDfuProc はちょうど 1 回")
        self.assertTrue(backend.unloaded, "UnloadFirmwareImage が呼ばれる")
        names = backend.call_names()
        self.assertEqual(names[0], "load_firmware_image_from_buffer")
        self.assertEqual(names[1], "start_dfu_download")
        self.assertEqual(names[-2], "end_dfu_proc")
        self.assertEqual(names[-1], "unload_firmware_image")
        self.assertEqual(names.count("end_dfu_proc"), 1)
        pcts = [s.percent for s in seen]
        self.assertEqual(pcts, sorted(pcts), "進捗は単調増加")
        self.assertEqual(pcts[-1], 100.0)

    def test_download_from_file(self):
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        backend, api = self._api(dfu_steps=3)
        res = api.dfu_download(image_path=BOOTSTRAP, poll_interval=0.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.total, BOOTSTRAP_SIZE)
        load_call = [c for c in backend.calls if c[0] == "load_firmware_image_from_file"][0]
        self.assertEqual(load_call[1], BOOTSTRAP)
        self.assertEqual(load_call[2], 0)                    # flags

    def test_download_failure_still_ends(self):
        backend, api = self._api(dfu_steps=6, fail_at_step=3,
                                fail_status=0xEE000001)
        with self.assertRaises(T.DfuError) as ctx:
            api.dfu_download(image_bytes=b"\x00" * 512, poll_interval=0.0, timeout=5.0)
        self.assertEqual(ctx.exception.result.state, T.DFU_STATE_FAILED)
        self.assertEqual(backend.ended, 1, "失敗時も EndDfuProc は 1 回")
        self.assertTrue(backend.unloaded)
        self.assertEqual(backend.call_names().count("end_dfu_proc"), 1)

    def test_download_without_image_raises_and_ends(self):
        backend, api = self._api(dfu_steps=3, start_status=T.TSTATUS_NO_IMAGE_LOADED)
        with self.assertRaises(T.ThesyconError) as ctx:
            api.dfu_download(image_bytes=b"\x00" * 64, poll_interval=0.0)
        self.assertEqual(ctx.exception.status, T.TSTATUS_NO_IMAGE_LOADED)
        self.assertEqual(backend.ended, 1)

    def test_download_cancel(self):
        """転送開始前 (Initializing) ならキャンセルを受理して EndDfuProc する。"""
        backend, api = self._api(dfu_steps=20, stall=True,
                                 stall_state=T.DFU_STATE_INITIALIZING)
        cancel = threading.Event()
        cancel.set()
        res = api.dfu_download(image_bytes=b"\x00" * 512, poll_interval=0.0,
                               timeout=5.0, cancel=cancel)
        self.assertTrue(res.cancelled)
        self.assertFalse(res.ok)
        self.assertEqual(backend.ended, 1)

    def test_download_timeout(self):
        backend, api = self._api(dfu_steps=20, stall=True)
        with self.assertRaises(T.DfuError) as ctx:
            api.dfu_download(image_bytes=b"\x00" * 512, poll_interval=0.01, timeout=0.15)
        self.assertIn("タイムアウト", str(ctx.exception))
        self.assertEqual(backend.ended, 1)

    def test_progress_callback_exception_is_tolerated(self):
        backend, api = self._api(dfu_steps=3)

        def boom(_s):
            raise RuntimeError("UI error")
        res = api.dfu_download(image_bytes=b"\x00" * 64, on_progress=boom,
                               poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)

    # -- Upload ------------------------------------------------------------
    def test_upload_success_returns_image(self):
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        payload = read_bytes(BOOTSTRAP)
        backend, api = self._api(dfu_steps=4, upload_image=payload)
        seen = []
        res = api.dfu_upload(on_progress=seen.append, poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.image, payload)
        self.assertEqual(backend.ended, 1)
        names = backend.call_names()
        self.assertEqual(names[0], "start_dfu_upload")
        self.assertIn("get_firmware_image_size", names)
        self.assertIn("get_firmware_image", names)
        self.assertEqual(names[-1], "end_dfu_proc")
        # 吸出したイメージはそのまま検証に通る
        r = V.validate_bytes(res.image, "uploaded.bin")
        self.assertTrue(r.ok)
        self.assertEqual(r.sha256, BOOTSTRAP_SHA256)

    def test_upload_failure_still_ends(self):
        backend, api = self._api(dfu_steps=5, fail_at_step=2)
        with self.assertRaises(T.DfuError):
            api.dfu_upload(poll_interval=0.0, timeout=5.0)
        self.assertEqual(backend.ended, 1)

    def test_upload_cancel(self):
        """転送開始前 (Initializing) なら吸出しキャンセルを受理して EndDfuProc する。"""
        backend, api = self._api(dfu_steps=20, stall=True,
                                 stall_state=T.DFU_STATE_INITIALIZING,
                                 upload_image=b"\x01" * 16)
        cancel = threading.Event()
        cancel.set()
        res = api.dfu_upload(cancel=cancel, poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.cancelled)
        self.assertIsNone(res.image)
        self.assertEqual(backend.ended, 1)

    # -- Revert ------------------------------------------------------------
    def test_revert_success(self):
        backend, api = self._api(dfu_steps=4)
        res = api.dfu_revert_to_factory(poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertEqual(backend.call_names()[0], "start_dfu_revert_to_factory_image")
        self.assertEqual(backend.call_names()[-1], "end_dfu_proc")
        self.assertEqual(backend.ended, 1)

    def test_revert_failure(self):
        backend, api = self._api(dfu_steps=5, fail_at_step=2)
        with self.assertRaises(T.DfuError):
            api.dfu_revert_to_factory(poll_interval=0.0, timeout=5.0)
        self.assertEqual(backend.ended, 1)

    def test_revert_timeout(self):
        backend, api = self._api(dfu_steps=100, stall=True)
        with self.assertRaises(T.DfuError):
            api.dfu_revert_to_factory(poll_interval=0.01, timeout=0.15)
        self.assertEqual(backend.ended, 1)

    # -- 引数 --------------------------------------------------------------
    def test_download_rejects_both_sources(self):
        _b, api = self._api()
        with self.assertRaises(ValueError):
            api.dfu_download(image_path=BOOTSTRAP, image_bytes=b"\x00")

    def test_download_missing_file(self):
        _b, api = self._api()
        with self.assertRaises(FileNotFoundError):
            api.dfu_download(image_path=os.path.join(HERE, "no_such_image.bin"))


class TestWriteGuard(unittest.TestCase):
    """D. allow_write なしでは書込系 API を呼べない。"""

    def test_all_write_ops_refused(self):
        backend = T.MockBackend()
        api = T.ThesyconApi(backend=backend, allow_write=False)
        with self.assertRaises(T.SafetyError):
            api.dfu_download(image_bytes=b"\x00" * 64)
        with self.assertRaises(T.SafetyError):
            api.dfu_upload()
        with self.assertRaises(T.SafetyError):
            api.dfu_revert_to_factory()
        self.assertEqual(backend.calls, [], "バックエンドに一切到達しないこと")

    def test_read_only_path_never_calls_write_apis(self):
        backend = T.ReadOnlyGuardBackend()
        api = T.ThesyconApi(backend=backend, allow_write=False)
        snaps = api.scan_devices(with_descriptors=True)
        self.assertEqual(len(snaps), 1)
        self.assertTrue(snaps[0].properties.is_do400)
        self.assertEqual(api.get_dfu_status().state, T.DFU_STATE_IDLE)
        self.assertEqual(api.api_version(), "5.15")
        for name in backend.call_names():
            self.assertNotIn(name, T.WRITE_OPERATIONS)

    def test_read_only_guard_raises_on_write(self):
        backend = T.ReadOnlyGuardBackend()
        with self.assertRaises(AssertionError):
            backend.start_dfu_download(0, 0, 0)


class TestDeviceInfoParsing(unittest.TestCase):
    """モック上の構造体パース (実機と同じレイアウト)。"""

    def test_properties_and_version(self):
        api = T.ThesyconApi(backend=T.MockBackend())
        snap = api.scan_devices(with_descriptors=True)[0]
        p = snap.properties
        self.assertEqual(p.vendor_id, T.VID_DO400)
        self.assertEqual(p.product_id, T.PID_DO400)
        self.assertEqual(p.bcd_device, 0x0323)
        self.assertEqual(p.firmware_version, "3.23")
        self.assertEqual(p.manufacturer, "SMSL")
        self.assertEqual(p.product, "SMSL USB AUDIO")
        self.assertEqual(snap.sample_rate, 44100)
        self.assertEqual(snap.api_version, "5.15")
        self.assertEqual(len(p.raw), T.PROPERTIES_BUFFER_SIZE)
        self.assertEqual(snap.string_descriptors.get(1), "SMSL")

    def test_bcd_version_format(self):
        cases = {0x0323: "3.23", 0x0329: "3.29", 0x0100: "1.00"}
        for bcd, want in cases.items():
            d = T.DeviceProperties(vendor_id=1, product_id=2, bcd_device=bcd)
            self.assertEqual(d.firmware_version, want)

    def test_status_names(self):
        self.assertEqual(T.status_name(0), "TSTATUS_SUCCESS")
        self.assertEqual(T.status_name(T.TSTATUS_NO_IMAGE_LOADED), "TSTATUS_NO_IMAGE_LOADED")


class TestCli(unittest.TestCase):
    """E. CLI / ドライランの検証 (実機に触れない)。"""

    def _run(self, argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = APP.main(argv)
        return rc, buf.getvalue()

    def test_validate_ok(self):
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        rc, out = self._run(["--validate", BOOTSTRAP])
        self.assertEqual(rc, 0)
        self.assertIn("ALL GOOD", out)
        self.assertIn("%s B" % format(BOOTSTRAP_SIZE, ","), out)

    def test_validate_ng(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"\x00" * 2048)
            path = fh.name
        try:
            rc, out = self._run(["--validate", path])
            self.assertEqual(rc, 1)
            self.assertIn("NG", out)
        finally:
            os.unlink(path)

    def test_dry_run_flash_sequence(self):
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        rc, out = self._run(["--dry-run-flash", BOOTSTRAP])
        self.assertEqual(rc, 0, out)
        self.assertIn("ドライラン", out)
        # F-2: 標準経路は LoadFirmwareImageFromBuffer (ファイル API は使わない)
        self.assertIn("TUSBAUDIO_LoadFirmwareImageFromBuffer", out)
        self.assertIn("load_firmware_image_from_buffer", out)
        self.assertNotIn("load_firmware_image_from_file", out)
        self.assertIn("start_dfu_download", out)
        self.assertIn("end_dfu_proc", out)
        self.assertIn("実機の書込み・DFU 遷移は行っていません", out)

    def test_dry_run_refuses_bad_image(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"not an image" * 100)
            path = fh.name
        try:
            rc, out = self._run(["--dry-run-flash", path])
            self.assertEqual(rc, 1)
            self.assertIn("事前検証 NG", out)
        finally:
            os.unlink(path)

    def test_parser_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            APP.build_parser().parse_args(["--check-device", "--validate", "x.bin"])


class TestLoadImageSignature(unittest.TestCase):
    """G. F-2: DLL シグネチャ (wchar_t* / Buffer) の宣言と ctypes バインド。"""

    def test_file_api_first_arg_is_wchar_p(self):
        """`TUSBAUDIO_LoadFirmwareImageFromFile(const wchar_t*, unsigned int)`."""
        restype, argtypes = T.SIGNATURES["TUSBAUDIO_LoadFirmwareImageFromFile"]
        self.assertEqual(len(argtypes), 2)
        self.assertIs(argtypes[0], ctypes.c_wchar_p)
        self.assertIsNot(argtypes[0], ctypes.c_char_p)
        self.assertIs(argtypes[1], ctypes.c_uint)
        self.assertIs(restype, ctypes.c_uint)

    def test_buffer_api_signature(self):
        """`TUSBAUDIO_LoadFirmwareImageFromBuffer(const void*, unsigned int, unsigned int)`."""
        restype, argtypes = T.SIGNATURES["TUSBAUDIO_LoadFirmwareImageFromBuffer"]
        self.assertEqual(len(argtypes), 3)
        self.assertIs(argtypes[0], ctypes.c_void_p)
        self.assertIs(argtypes[1], ctypes.c_uint)
        self.assertIs(argtypes[2], ctypes.c_uint)
        self.assertIs(restype, ctypes.c_uint)

    def test_bindings_cover_every_signature(self):
        self.assertEqual({name for _attr, name in T.BINDINGS}, set(T.SIGNATURES))
        self.assertEqual(len(T.BINDINGS), len(T.SIGNATURES))

    def test_backend_binds_c_wchar_p_and_passes_str(self):
        """実 DLL をロードせずに、c_wchar_p バインドと `str` 受け渡しを確認する。"""
        fake_dll = mock.MagicMock()
        fd, dll_path = tempfile.mkstemp(suffix=".dll")
        os.close(fd)
        try:
            with mock.patch.object(ctypes, "WinDLL", return_value=fake_dll):
                backend = T.CtypesBackend(dll_path)
        finally:
            os.unlink(dll_path)

        self.assertIs(backend._load_file.argtypes[0], ctypes.c_wchar_p)
        self.assertIs(backend._load_buf.argtypes[0], ctypes.c_void_p)

        path = r"C:\テスト\イメージ 3.23.bin"          # 非 ASCII + 空白を含む
        backend.load_firmware_image_from_file(path, 0)
        # os.fsencode / c_char_p 経由なら bytes が渡ってしまう (F-2 の再発検知)
        backend._load_file.assert_called_once_with(path, 0)

    def test_string_descriptor_signature_has_five_args(self):
        """N-1: TUSBAUDIO_GetUsbStringDescriptorString は 5 引数 (実 DLL RVA 0x1FF0 一致)。"""
        restype, argtypes = T.SIGNATURES["TUSBAUDIO_GetUsbStringDescriptorString"]
        self.assertEqual(len(argtypes), 5)
        self.assertEqual(argtypes, [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,
                                    ctypes.c_void_p, ctypes.c_uint])
        self.assertIs(restype, ctypes.c_uint)

        # ctypes.CFUNCTYPE で実 DLL なしに 5 引数呼出の型検査を再現する
        seen = []

        @ctypes.CFUNCTYPE(restype, *argtypes)
        def fake_strdesc(handle, index, lang_id, buf_ptr, max_chars):
            seen.append((handle, index, lang_id, max_chars))
            ctypes.memmove(buf_ptr, "SMSL DO400\x00".encode("utf-16-le"), 22)
            return 0

        class _StubDll:
            def __getattr__(self, name):
                if name == "TUSBAUDIO_GetUsbStringDescriptorString":
                    return fake_strdesc
                return mock.MagicMock()

        fd, dll_path = tempfile.mkstemp(suffix=".dll")
        os.close(fd)
        try:
            with mock.patch.object(ctypes, "WinDLL", return_value=_StubDll()):
                backend = T.CtypesBackend(dll_path)
        finally:
            os.unlink(dll_path)

        st, text = backend.get_usb_string_descriptor_string(0x10001, 2, 0x0409, 64)
        self.assertEqual(st, 0)
        self.assertEqual(text, "SMSL DO400")
        self.assertEqual(seen, [(0x10001, 2, 0x0409, 64)])


class _CapturingBackend(T.MockBackend):
    """`LoadFirmwareImageFromBuffer` に渡された bytes を Unload 後も保持する。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.captured = None
        self.capture_flags = None

    def load_firmware_image_from_buffer(self, data, flags: int = 0):
        self.captured = bytes(data)
        self.capture_flags = flags
        return super().load_firmware_image_from_buffer(data, flags)


class TestBufferStandardPath(unittest.TestCase):
    """G. F-2: 更新経路は検証済みバイト列を LoadFirmwareImageFromBuffer へ渡す。"""

    def _api(self, **kw):
        backend = _CapturingBackend(**kw)
        return backend, T.ThesyconApi(backend=backend, allow_write=True)

    def test_download_image_uses_buffer_with_file_bytes(self):
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        backend, api = self._api(dfu_steps=3)
        res = api.download_image(BOOTSTRAP, poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertEqual(res.source, "buffer")
        names = backend.call_names()
        self.assertEqual(names[0], "load_firmware_image_from_buffer")
        self.assertNotIn("load_firmware_image_from_file", names)
        self.assertEqual(backend.captured, read_bytes(BOOTSTRAP))
        self.assertEqual(backend.capture_flags, 0)
        self.assertEqual(res.validation.sha256, BOOTSTRAP_SHA256)
        self.assertEqual(backend.ended, 1)
        self.assertTrue(backend.unloaded)

    def test_app_perform_flash_is_buffer_path(self):
        """do400_dfu_app の更新パス (GUI / ドライラン共通) も Buffer 経由。"""
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        backend, api = self._api(dfu_steps=3)
        res = APP.perform_flash(api, BOOTSTRAP, poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertEqual(backend.call_names()[0], "load_firmware_image_from_buffer")
        self.assertNotIn("load_firmware_image_from_file", backend.call_names())
        self.assertEqual(backend.captured, read_bytes(BOOTSTRAP))

    def test_validated_bytes_are_the_loaded_bytes(self):
        """検証に渡した bytes と DLL へ渡した bytes が同一 (TOCTOU 不可)。"""
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        backend, api = self._api(dfu_steps=3)
        expected = read_bytes(BOOTSTRAP)
        seen = []
        real = V.validate_bytes

        def spy(data, name="<bytes>"):
            seen.append(bytes(data))
            return real(data, name)

        with mock.patch.object(V, "validate_bytes", side_effect=spy):
            api.download_image(BOOTSTRAP, poll_interval=0.0, timeout=5.0)

        self.assertEqual(seen, [expected])
        self.assertEqual(backend.captured, seen[0])

    def test_invalid_image_never_reaches_backend(self):
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as fh:
            fh.write(b"not a firmware image" * 70)
            path = fh.name
        try:
            backend, api = self._api(dfu_steps=3)
            with self.assertRaises(T.ImageValidationError) as ctx:
                api.download_image(path, poll_interval=0.0)
            self.assertEqual(backend.calls, [], "検証 NG なら DLL を一切呼ばない")
            self.assertIsNotNone(ctx.exception.report)
            self.assertFalse(ctx.exception.report.write_allowed)
            self.assertIn("Magic", str(ctx.exception))
        finally:
            os.unlink(path)

    def test_missing_file(self):
        backend, api = self._api()
        with self.assertRaises(FileNotFoundError):
            api.download_image(os.path.join(HERE, "no_such_image.bin"))
        self.assertEqual(backend.calls, [])

    def test_download_image_requires_allow_write(self):
        backend = T.MockBackend()
        api = T.ThesyconApi(backend=backend, allow_write=False)
        with self.assertRaises(T.SafetyError):
            api.download_image(BOOTSTRAP)
        self.assertEqual(backend.calls, [])


class TestDfuResultStrict(unittest.TestCase):
    """H. F-4: `result.ok` の厳密判定と、転送中のキャンセル抑止。"""

    def test_finished_with_zero_completion_is_success(self):
        res = T.DfuResult(operation="download", state=T.DFU_STATE_FINISHED,
                          completion=0, transferred=1024, total=1024, elapsed=0.5)
        self.assertTrue(res.ok)
        ok, msg = APP.describe_dfu_result("ファームウェア更新", res)
        self.assertTrue(ok)
        self.assertIn("完了", msg)

    def test_finished_with_nonzero_completion_is_failure(self):
        """Finished でも completion != 0 は「書込み完了」ではない (F-4 の核心)。"""
        res = T.DfuResult(operation="download", state=T.DFU_STATE_FINISHED,
                          completion=0xEE000001)
        self.assertFalse(res.ok)
        ok, msg = APP.describe_dfu_result("ファームウェア更新", res)
        self.assertFalse(ok)
        self.assertIn("0xEE000001", msg)
        self.assertIn("TSTATUS_FAILED", msg)
        self.assertIn("失敗", msg)
        self.assertIn("Finished", msg)

    def test_failed_state_is_failure(self):
        res = T.DfuResult(operation="revert", state=T.DFU_STATE_FAILED,
                          completion=0xEE000005)
        ok, msg = APP.describe_dfu_result("Factory 3.29 復帰", res)
        self.assertFalse(ok)
        self.assertIn("TSTATUS_ABORTED", msg)

    def test_cancelled_is_not_success(self):
        res = T.DfuResult(operation="download", state=T.DFU_STATE_IN_PROGRESS,
                          cancelled=True)
        ok, msg = APP.describe_dfu_result("現行イメージ吸出し", res)
        self.assertFalse(ok)
        self.assertIn("中断", msg)

    def test_api_raises_on_finished_with_nonzero_completion(self):
        """API 層でも Finished + completion != 0 は DfuError (防御の二重化)。"""

        class _FinishedNonZero(T.MockBackend):
            def get_dfu_status(self):
                self._rec("get_dfu_status")
                return 0, T.DFU_STATE_FINISHED, self._total, self._total, 0xEE000001

        backend = _FinishedNonZero()
        api = T.ThesyconApi(backend=backend, allow_write=True)
        with self.assertRaises(T.DfuError) as ctx:
            api.dfu_download(image_bytes=b"\x00" * 64, poll_interval=0.0, timeout=2.0)
        self.assertEqual(ctx.exception.status, 0xEE000001)
        self.assertIn("0xEE000001", str(ctx.exception))
        self.assertEqual(backend.ended, 1, "失敗判定でも EndDfuProc は 1 回")

    # -- キャンセル抑止 -----------------------------------------------------
    def test_cancel_guard_locks_at_in_progress(self):
        g = APP.TransferCancelGuard()
        self.assertTrue(g.can_cancel)
        # Initializing / EnteringDfuMode はキャンセル可
        self.assertFalse(g.observe(T.DfuStatus(T.DFU_STATE_INITIALIZING, 0, 0, 0)))
        self.assertTrue(g.can_cancel)
        self.assertFalse(g.observe(T.DfuStatus(T.DFU_STATE_ENTERING_DFU_MODE, 0, 0, 0)))
        self.assertTrue(g.can_cancel)
        # InProgress で自動ロック
        self.assertTrue(g.observe(T.DfuStatus(T.DFU_STATE_IN_PROGRESS, 1, 2, 0)))
        self.assertFalse(g.can_cancel)
        # EnteringAppMode でもロック継続 (新規ロック通知は出さない)
        self.assertFalse(g.observe(T.DfuStatus(T.DFU_STATE_ENTERING_APP_MODE, 2, 2, 0)))
        self.assertFalse(g.can_cancel)
        # Finished でも解除しない (完了処理は _on_done の reset で行う)
        g.observe(T.DfuStatus(T.DFU_STATE_FINISHED, 2, 2, 0))
        self.assertFalse(g.can_cancel)
        g.reset()
        self.assertTrue(g.can_cancel)

    def test_cancel_guard_constants(self):
        self.assertEqual(APP.TRANSFER_LOCK_STATES, (3, 4))
        self.assertIn("転送中のため中断できません", APP.CANCEL_LOCK_MESSAGE)

    def test_progress_callback_locks_guard_during_transfer(self):
        """進捗コールバック列を通すと、転送中は必ずキャンセル不可になる。"""
        if not HAVE_BOOTSTRAP:
            self.skipTest("bootstrap がありません")
        guard = APP.TransferCancelGuard()
        seen = []
        backend = T.MockBackend(dfu_steps=4)
        api = T.ThesyconApi(backend=backend, allow_write=True)

        def cb(s):
            guard.observe(s)
            seen.append((s.state, guard.can_cancel))

        res = api.download_image(BOOTSTRAP, on_progress=cb, poll_interval=0.0, timeout=5.0)
        self.assertTrue(res.ok)
        self.assertIn(T.DFU_STATE_IN_PROGRESS, [st for st, _c in seen])
        for st, can in seen:
            if st in APP.TRANSFER_LOCK_STATES:
                self.assertFalse(can, "state=%d でキャンセル可能になっている" % st)

    def test_poll_ignores_cancel_once_in_progress_race(self):
        """N-2: API 層 _poll() も InProgress/EnteringAppMode 中のキャンセルを抑止する。"""
        cancel = threading.Event()
        end_states = []

        class _RaceBackend(T.MockBackend):
            def get_dfu_status(self):
                self._rec("get_dfu_status")
                self._step += 1
                seq = [
                    (0, T.DFU_STATE_INITIALIZING, 0, self._total, 0),
                    (0, T.DFU_STATE_IN_PROGRESS, 32, self._total, 0),
                    (0, T.DFU_STATE_ENTERING_APP_MODE, self._total, self._total, 0),
                    (0, T.DFU_STATE_FINISHED, self._total, self._total, 0),
                ]
                idx = min(self._step - 1, len(seq) - 1)
                _, self._state, self._cur, self._total, self._completion = seq[idx]
                # Initializing の後に GUI 非同期クリックが InProgress と競合する状況を再現
                if self._step == 2:
                    cancel.set()
                return seq[idx]

            def end_dfu_proc(self):
                end_states.append(self._state)
                return super().end_dfu_proc()

        backend = _RaceBackend()
        api = T.ThesyconApi(backend=backend, allow_write=True)
        res = api.dfu_download(image_bytes=b"\x00" * 64, poll_interval=0.0,
                               timeout=5.0, cancel=cancel)
        self.assertTrue(res.ok)
        self.assertFalse(res.cancelled)
        self.assertEqual(res.state, T.DFU_STATE_FINISHED)
        self.assertEqual(end_states, [T.DFU_STATE_FINISHED],
                         "InProgress 中に EndDfuProc で中断してはならない")

    def test_poll_still_times_out_if_stalled_in_progress_with_cancel_set(self):
        """N-2 補足: InProgress でハングした場合は cancel 抑止下でも timeout で安全終了。"""
        backend = T.MockBackend(dfu_steps=20, stall=True,
                                stall_state=T.DFU_STATE_IN_PROGRESS)
        api = T.ThesyconApi(backend=backend, allow_write=True)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(T.DfuError) as ctx:
            api.dfu_download(image_bytes=b"\x00" * 64, poll_interval=0.01,
                             timeout=0.12, cancel=cancel)
        self.assertIn("タイムアウト", str(ctx.exception))
        self.assertEqual(backend.ended, 1)


# 実機読み取り専用テストは **opt-in** (F-5)。既定では USB デバイスを一切開かない。
LIVE_RO_ENV = "DO400_ALLOW_LIVE_RO"
LIVE_RO_ALLOWED = os.environ.get(LIVE_RO_ENV) == "1"


def _find_real_do400():
    try:
        api = T.ThesyconApi()
        snaps = api.scan_devices(with_descriptors=True)
    except Exception:
        return None, None
    for s in snaps:
        if s.properties.is_do400:
            return api, s
    return api, None


@unittest.skipUnless(LIVE_RO_ALLOWED,
                     "set DO400_ALLOW_LIVE_RO=1 to run live read-only hardware checks")
@unittest.skipUnless(os.name == "nt" and os.path.isfile(T.DEFAULT_DLL_PATH),
                     "Thesycon DLL がないため実機テストをスキップ")
class TestRealDeviceReadOnly(unittest.TestCase):
    """F. 実機 DO400 からの読み取り専用確認 (書込みは一切しない)。"""

    @classmethod
    def setUpClass(cls):
        cls.api, cls.snap = _find_real_do400()
        if cls.snap is None:
            raise unittest.SkipTest("DO400 (152A:85DD) が接続されていません")

    def test_identity(self):
        p = self.snap.properties
        self.assertEqual(p.vendor_id, 0x152A)
        self.assertEqual(p.product_id, 0x85DD)
        self.assertEqual(p.bcd_device, 0x0323, "FW 3.23 の実機を想定")

    def test_sample_rate(self):
        self.assertEqual(self.snap.sample_rate, 44100)

    def test_api_version(self):
        self.assertEqual(self.api.api_version(), "5.15")

    def test_config_descriptor(self):
        self.assertGreater(len(self.snap.config_descriptor), 9)
        self.assertEqual(self.snap.config_descriptor[1], 0x02)   # CONFIGURATION

    def test_dfu_status_readable(self):
        st = self.api.get_dfu_status()
        self.assertIn(st.state, T.DFU_STATE_NAMES)

    def test_no_write_api_was_called(self):
        for name, *_ in self.api.calls:
            self.assertNotIn(name, T.WRITE_OPERATIONS,
                             "%s が呼ばれた (読み取り専用違反)" % name)

    def test_check_device_cli(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = APP.cmd_check_device(None)
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("152A / 85DD", out)
        self.assertIn("0x0323", out)
        self.assertIn("44100 Hz", out)
        self.assertIn("読み取りのみ", out)


class TestLiveReadOnlyOptIn(unittest.TestCase):
    """I. F-5: 実機テストが既定でスキップされ、USB を一切開かないこと。"""

    def test_env_gate_constant(self):
        self.assertEqual(LIVE_RO_ENV, "DO400_ALLOW_LIVE_RO")
        self.assertEqual(LIVE_RO_ALLOWED, os.environ.get(LIVE_RO_ENV) == "1")

    def test_class_f_is_class_level_skipped_by_default(self):
        if LIVE_RO_ALLOWED:
            self.skipTest("DO400_ALLOW_LIVE_RO=1 のため実機テストが有効")
        cls = TestRealDeviceReadOnly
        self.assertTrue(getattr(cls, "__unittest_skip__", False),
                        "クラス F がデフォルトでスキップされていない")
        why = str(getattr(cls, "__unittest_skip_why__", ""))
        self.assertIn("DO400_ALLOW_LIVE_RO=1", why)

    def test_class_f_never_opened_device(self):
        if LIVE_RO_ALLOWED:
            self.skipTest("DO400_ALLOW_LIVE_RO=1 のため実機テストが有効")
        # setUpClass が呼ばれていれば api / snap が生えているはず
        self.assertIsNone(getattr(TestRealDeviceReadOnly, "api", None))
        self.assertIsNone(getattr(TestRealDeviceReadOnly, "snap", None))


class TestStepGuidanceAndUi(unittest.TestCase):
    """J. 3 ステップ案内ロジックとオフライン GUI スモークテスト (実 DLL 不使用)。"""

    def _fake_val(self, write_allowed=True, requires_bootstrap=False):
        class _V:
            pass
        v = _V()
        v.write_allowed = write_allowed
        v.requires_bootstrap = requires_bootstrap
        return v

    def test_guidance_step1_not_connected(self):
        g = APP.compute_guidance(connected=False, validation=None)
        self.assertEqual(g["badge"], "STEP 1")
        self.assertEqual(g["step_states"], ("active", "pending", "pending"))
        self.assertEqual(g["tone"], "warn")

    def test_guidance_step2_select_image(self):
        g = APP.compute_guidance(connected=True, validation=None, has_recommended=True)
        self.assertEqual(g["badge"], "STEP 2")
        self.assertEqual(g["step_states"], ("done", "active", "pending"))
        self.assertIn("PEQ ファームウェアを選択", g["detail"])

    def test_guidance_step2_invalid_image(self):
        g = APP.compute_guidance(connected=True, validation=self._fake_val(write_allowed=False))
        self.assertEqual(g["badge"], "STEP 2")
        self.assertEqual(g["tone"], "error")
        self.assertEqual(g["step_states"], ("done", "error", "pending"))

    def test_guidance_step3_direct_ready(self):
        g = APP.compute_guidance(connected=True, validation=self._fake_val(True, False))
        self.assertEqual(g["badge"], "STEP 3")
        self.assertEqual(g["tone"], "ok")
        self.assertEqual(g["step_states"], ("done", "done", "active"))
        self.assertIn("そのまま 1 回で更新完了", g["detail"])

    def test_guidance_step3_bootstrap_needed_and_checked(self):
        v = self._fake_val(True, True)
        g1 = APP.compute_guidance(connected=True, validation=v, bootstrap_checked=False)
        self.assertEqual(g1["tone"], "warn")
        self.assertEqual(g1["step_states"], ("done", "done", "warn"))
        g2 = APP.compute_guidance(connected=True, validation=v, bootstrap_checked=True)
        self.assertEqual(g2["tone"], "ok")
        self.assertEqual(g2["step_states"], ("done", "done", "active"))

    def test_guidance_busy_locked_and_done(self):
        g_busy = APP.compute_guidance(connected=True, validation=self._fake_val(),
                                      busy=True, cancel_locked=True)
        self.assertEqual(g_busy["badge"], "書込み中")
        self.assertEqual(g_busy["tone"], "warn")
        g_done = APP.compute_guidance(connected=True, validation=self._fake_val(),
                                      last_flash_ok=True)
        self.assertEqual(g_done["badge"], "完了")
        self.assertEqual(g_done["tone"], "ok")

    def test_guidance_english_mode(self):
        g1 = APP.compute_guidance(connected=False, validation=None, lang="en")
        self.assertEqual(g1["badge"], "STEP 1")
        self.assertIn("Refresh", g1["headline"])
        g3 = APP.compute_guidance(connected=True, validation=self._fake_val(True, False), lang="en")
        self.assertEqual(g3["badge"], "STEP 3")
        self.assertIn("Flash Firmware", g3["headline"])

    def test_describe_dfu_result_english(self):
        res_ok = T.DfuResult(operation="download", state=T.DFU_STATE_FINISHED, completion=0,
                             transferred=200896, total=200896, elapsed=12.3)
        ok, msg = APP.describe_dfu_result("Firmware Update", res_ok, lang="en")
        self.assertTrue(ok)
        self.assertIn("Completed", msg)
        self.assertIn("transferred", msg)
        res_cancel = T.DfuResult(operation="download", state=T.DFU_STATE_IN_PROGRESS,
                                 cancelled=True, elapsed=1.0)
        ok2, msg2 = APP.describe_dfu_result("Firmware Update", res_cancel, lang="en")
        self.assertFalse(ok2)
        self.assertIn("Cancelled", msg2)
        self.assertIn("cannot cancel", APP.cancel_lock_message("en"))

    def test_read_protected_upload_detection(self):
        self.assertTrue(APP.is_read_protected_upload(b"\x00" * 256))
        self.assertFalse(APP.is_read_protected_upload(b"\x00" * 255))
        self.assertFalse(APP.is_read_protected_upload(b"\x01" + b"\x00" * 255))

    @unittest.skipUnless(APP._TK_AVAILABLE, "tkinter が利用できません")
    def test_gui_offscreen_smoke_with_mock_api(self):
        """モック API で GUI をオフスクリーン構築し、STEP 1→2→3 と枠表示切替を確認。"""
        backend = T.MockBackend()
        api = T.ThesyconApi(backend=backend, allow_write=False)
        try:
            app = APP.DfuApp(api=api, auto_refresh=False)
        except Exception as exc:
            self.skipTest("ヘッドレス環境のため Tk 起動スキップ: %s" % exc)
        try:
            app.withdraw()
            app.update_idletasks()
            # 初期状態: 未接続 -> STEP 1, bootstrap 枠は非表示, JP/EN 切替確認
            self.assertEqual(app.guide_badge.cget("text"), "STEP 1")
            self.assertEqual(app.bootstrap_panel.winfo_manager(), "")
            app.toggle_language()
            self.assertEqual(app.lang, "en")
            self.assertEqual(str(app.btn_flash.cget("text")), "Flash Firmware")
            self.assertIn("Started. Follow STEP 1", app.log_text.get("1.0", "end"))
            app.toggle_language()
            self.assertEqual(app.lang, "ja")
            self.assertEqual(str(app.btn_flash.cget("text")), "ファームウェア更新を実行")
            self.assertIn("起動しました", app.log_text.get("1.0", "end"))
            # MockBackend でデバイス認識 -> STEP 2
            app.refresh_device()
            self.assertIsNotNone(app.snapshot)
            self.assertEqual(app.guide_badge.cget("text"), "STEP 2")
            # 通常サイズイメージ (<= 200 KiB) 読込 -> STEP 3 実行可能, bootstrap 枠は非表示のまま
            rec = APP.find_recommended_image() or (BOOTSTRAP if HAVE_BOOTSTRAP else None)
            if rec:
                app._load_selected_file(rec)
                self.assertEqual(app.guide_badge.cget("text"), "STEP 3")
                self.assertEqual(app.bootstrap_panel.winfo_manager(), "")
                self.assertEqual(str(app.btn_flash.cget("state")), "normal")
                # 英語切替で既存ログ・デバイス詳細・検証サマリも即座に英語化されること
                app.toggle_language()
                en_log = app.log_text.get("1.0", "end")
                self.assertIn("Device detected:", en_log)
                self.assertIn("Verification OK:", en_log)
                self.assertIn("Direct Flash OK", str(app.val_summary.cget("text")))
                self.assertIn("Sample Rate", str(app.dev_extra.cget("text")))
                app.toggle_language()
            # 2 段階イメージ (> 200 KiB) 読込 -> bootstrap 枠が表示され、チェック前は書込不可
            tmp_big = None
            big_path = UPGRADE if HAVE_UPGRADE else None
            if big_path is None and HAVE_BOOTSTRAP:
                c0, c1, sw = split_container(read_bytes(BOOTSTRAP))
                big_data = build_container(c0, c1 + b"\x00" * 65536, sw)
                fd, tmp_big = tempfile.mkstemp(suffix=".bin")
                os.close(fd)
                with open(tmp_big, "wb") as fh:
                    fh.write(big_data)
                big_path = tmp_big
            try:
                if big_path:
                    app._load_selected_file(big_path)
                    self.assertEqual(app.bootstrap_panel.winfo_manager(), "grid")
                    self.assertEqual(str(app.btn_flash.cget("state")), "disabled")
                    app.bootstrap_var.set(True)
                    app._update_buttons()
                    self.assertEqual(str(app.btn_flash.cget("state")), "normal")
            finally:
                if tmp_big and os.path.isfile(tmp_big):
                    os.unlink(tmp_big)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
