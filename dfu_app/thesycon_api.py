#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""thesycon_api.py -- XMOS / Thesycon `xmos_s_usbaudioapi_x64.dll` (API v5.15) ラッパー.

このモジュールは 2 つのバックエンドを持つ:

  * ``CtypesBackend`` -- 実 DLL を ctypes で叩く (引数型は DLL の逆アセンブルで確認済み)
  * ``MockBackend``   -- 実機に一切触れないシミュレータ (ドライラン / 自動テスト用)

公開クラス ``ThesyconApi`` は両者を同じインタフェースで扱う。**書込系 API
(StartDfuDownload / StartDfuUpload / StartDfuRevertToFactoryImage) は
``allow_write=True`` を明示しない限り ``SafetyError`` で拒否する。**

実 DLL のシグネチャ (静的解析で確定。詳細は THESYCON_API_NOTES.md)
--------------------------------------------------------------------------
`TUSBAUDIO_LoadFirmwareImageFromFile` の第 1 引数は **`const wchar_t*`** である
(DLL 静的解析: 内部 `0x2560` が 16 bit 単位の `wcslen`、
モード文字列 `0x384F0` は `72 00 62 00` = `L"rb"`、ファイルは `CreateFileW` 経由)。
したがって ctypes では `c_wchar_p` でバインドし、`str` を渡す。

**標準の書込経路は `LoadFirmwareImageFromBuffer`** (`ThesyconApi.download_image`)。
`fw_validator.validate_bytes()` に合格したメモリ上の `bytes` をそのまま渡すので、
検証〜ロード間のファイル差替 (TOCTOU) もパス文字コード問題も構造的に起きない。

    unsigned int  TUSBAUDIO_GetApiVersion(void);
    unsigned int  TUSBAUDIO_GetDeviceCount(void);
    TStatus       TUSBAUDIO_EnumerateDevices(void);
    TStatus       TUSBAUDIO_OpenDeviceByIndex(unsigned int index, unsigned int* pHandle);
    TStatus       TUSBAUDIO_CloseDevice(unsigned int handle);
    TStatus       TUSBAUDIO_GetDeviceProperties(unsigned int handle, void* pProps /*0x410 B*/);
    TStatus       TUSBAUDIO_GetCurrentSampleRate(unsigned int handle, unsigned int* pRate);
    TStatus       TUSBAUDIO_GetDeviceUsbMode(unsigned int handle, unsigned int* pMode);
    TStatus       TUSBAUDIO_GetUsbConfigDescriptor(unsigned int handle, void* pBuf,
                                                   unsigned int bufSize, unsigned int* pNumBytes);
    TStatus       TUSBAUDIO_GetUsbStringDescriptorString(unsigned int handle,
                                                   unsigned int descIndex, unsigned int langId,
                                                   void* pBuf /*wchar_t*/, unsigned int bufLen);
    const wchar_t* TUSBAUDIO_StatusCodeStringW(TStatus status);
    TStatus       TUSBAUDIO_LoadFirmwareImageFromFile(const wchar_t* path, unsigned int flags);
    TStatus       TUSBAUDIO_LoadFirmwareImageFromBuffer(const void* pBuf, unsigned int size,
                                                        unsigned int flags);
    TStatus       TUSBAUDIO_UnloadFirmwareImage(void);
    TStatus       TUSBAUDIO_GetFirmwareImageSize(unsigned int* pSize, unsigned int flags);
    TStatus       TUSBAUDIO_GetFirmwareImage(void* pBuf, unsigned int bufSize,
                                             unsigned int* pWritten, unsigned int flags);
    TStatus       TUSBAUDIO_StartDfuDownload(unsigned int devIndex, unsigned int imageIndex,
                                             unsigned int flags);
    TStatus       TUSBAUDIO_StartDfuUpload(unsigned int devIndex, unsigned int imageIndex,
                                           unsigned int flags);
    TStatus       TUSBAUDIO_StartDfuRevertToFactoryImage(unsigned int devIndex,
                                                         unsigned int flags);
    TStatus       TUSBAUDIO_GetDfuStatus(unsigned int* pState, unsigned int* pCur,
                                         unsigned int* pTotal, unsigned int* pCompletion);
    TStatus       TUSBAUDIO_EndDfuProc(void);

`flags` の既定値はすべて 0。DFU の `imageIndex` は DFU の alt setting 番号
(lib_xua のアップグレードスロット = 0)。
"""

from __future__ import annotations

import ctypes as C
import os
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = [
    "DEFAULT_DLL_PATH", "VID_DO400", "PID_DO400",
    "DFU_STATE_NAMES", "DFU_STATE_IDLE", "DFU_STATE_INITIALIZING",
    "DFU_STATE_ENTERING_DFU_MODE", "DFU_STATE_IN_PROGRESS",
    "DFU_STATE_ENTERING_APP_MODE", "DFU_STATE_FINISHED", "DFU_STATE_FAILED",
    "DFU_TRANSFER_LOCK_STATES",
    "TSTATUS_SUCCESS", "TSTATUS_NO_IMAGE_LOADED",
    "SIGNATURES", "BINDINGS",
    "ThesyconError", "SafetyError", "DfuError", "ImageValidationError",
    "DeviceProperties", "DeviceSnapshot", "DfuStatus", "DfuResult",
    "CtypesBackend", "MockBackend", "ThesyconApi", "list_audio_devices",
]

DEFAULT_DLL_PATH = r"C:\Program Files\XMOS\XMOS_Audio_Driver\x64\xmos_s_usbaudioapi_x64.dll"

VID_DO400 = 0x152A
PID_DO400 = 0x85DD
BCD_DO400_FW323 = 0x0323

TSTATUS_SUCCESS = 0x00000000
TSTATUS_NO_IMAGE_LOADED = 0xEE001007
TSTATUS_INVALID_PARAMETER = 0xEE000041
TSTATUS_NOT_ALLOWED = 0xEE000032

# TUSBAUDIO_GetDfuStatus の state 値 (DLL 内で 6 = Failed を確認済み)
DFU_STATE_IDLE = 0
DFU_STATE_INITIALIZING = 1
DFU_STATE_ENTERING_DFU_MODE = 2
DFU_STATE_IN_PROGRESS = 3
DFU_STATE_ENTERING_APP_MODE = 4
DFU_STATE_FINISHED = 5
DFU_STATE_FAILED = 6
DFU_STATE_NAMES = {
    DFU_STATE_IDLE: "Idle",
    DFU_STATE_INITIALIZING: "Initializing",
    DFU_STATE_ENTERING_DFU_MODE: "EnteringDfuMode",
    DFU_STATE_IN_PROGRESS: "InProgress",
    DFU_STATE_ENTERING_APP_MODE: "EnteringAppMode",
    DFU_STATE_FINISHED: "Finished",
    DFU_STATE_FAILED: "Failed",
}
# 転送中 (3=InProgress, 4=EnteringAppMode) はフラッシュ破損防止のため中断を抑止
DFU_TRANSFER_LOCK_STATES = (DFU_STATE_IN_PROGRESS, DFU_STATE_ENTERING_APP_MODE)

PROPERTIES_BUFFER_SIZE = 0x410
PROP_OFF_VID = 0x000
PROP_OFF_PID = 0x004
PROP_OFF_BCD = 0x008
PROP_OFF_MANUFACTURER = 0x10C
PROP_OFF_PRODUCT = 0x20C
PROP_OFF_SERIAL = 0x30C
PROP_STR_CHARS = 128

STATUS_NAMES = {
    0x00000000: "TSTATUS_SUCCESS",
    0xEE000001: "TSTATUS_FAILED",
    0xEE000002: "TSTATUS_INTERNAL_ERROR",
    0xEE000003: "TSTATUS_TIMEOUT",
    0xEE000005: "TSTATUS_ABORTED",
    0xEE000032: "TSTATUS_NOT_ALLOWED",
    0xEE000033: "TSTATUS_NOT_OPENED",
    0xEE000034: "TSTATUS_NOT_AVAILABLE",
    0xEE000041: "TSTATUS_INVALID_PARAMETER",
    0xEE000042: "TSTATUS_INVALID_BUFFER_SIZE",
    0xEE000047: "TSTATUS_INVALID_INDEX",
    0xEE000048: "TSTATUS_INVALID_HANDLE",
    0xEE000070: "TSTATUS_ENUM_REQUIRED",
    0xEE000074: "TSTATUS_BUFFER_TOO_SMALL",
    0xEE000081: "TSTATUS_OBJECT_NOT_FOUND",
    0xEE001007: "TSTATUS_NO_IMAGE_LOADED",
}

#: 実機の状態を変える可能性がある操作 (allow_write=True が必須)
WRITE_OPERATIONS = (
    "start_dfu_download",
    "start_dfu_upload",
    "start_dfu_revert_to_factory_image",
    "load_firmware_image",
    "unload_firmware_image",
    "get_firmware_image",
)

#: 完全読み取り専用 (いつでも安全)
READ_OPERATIONS = (
    "get_api_version",
    "enumerate_devices",
    "get_device_count",
    "open_device_by_index",
    "close_device",
    "get_device_properties",
    "get_current_sample_rate",
    "get_device_usb_mode",
    "get_usb_config_descriptor",
    "get_usb_string_descriptor_string",
    "status_code_string",
    "get_dfu_status",
)


# --------------------------------------------------------------------------
# 例外
# --------------------------------------------------------------------------
class ThesyconError(RuntimeError):
    """DLL が TSTATUS_SUCCESS 以外を返した。"""

    def __init__(self, message: str, status: Optional[int] = None, operation: str = ""):
        self.status = status
        self.operation = operation
        if status is not None:
            message = "%s (0x%08X %s)" % (message, status, status_name(status))
        super().__init__(message)


class SafetyError(RuntimeError):
    """書込系 API を allow_write なしで呼ぼうとした。"""


class DfuError(ThesyconError):
    """DFU シーケンスが Failed / タイムアウトした。"""

    def __init__(self, message, status=None, operation="", result=None):
        super().__init__(message, status, operation)
        self.result = result


class ImageValidationError(ValueError):
    """オフライン検証に合格しなかった (バックエンドには一切触れない)。"""

    def __init__(self, message: str, report=None):
        super().__init__(message)
        self.report = report


def status_name(status: Optional[int]) -> str:
    if status is None:
        return "-"
    return STATUS_NAMES.get(status, "0x%08X" % status)


# --------------------------------------------------------------------------
# データクラス
# --------------------------------------------------------------------------
@dataclass
class DeviceProperties:
    vendor_id: int = 0
    product_id: int = 0
    bcd_device: int = 0
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    raw: bytes = b""

    @property
    def vid_pid(self) -> str:
        return "%04X:%04X" % (self.vendor_id, self.product_id)

    @property
    def is_do400(self) -> bool:
        return (self.vendor_id, self.product_id) == (VID_DO400, PID_DO400)

    @property
    def firmware_version(self) -> str:
        """bcdDevice は BCD 表記 (0x0323 -> '3.23')。"""
        major = (self.bcd_device >> 8) & 0xFF
        hi = (self.bcd_device >> 4) & 0x0F
        lo = self.bcd_device & 0x0F
        return "%d.%d%d" % (major, hi, lo)


@dataclass
class DeviceSnapshot:
    """1 台分の読み取り専用スナップショット。"""
    index: int
    handle: int
    properties: DeviceProperties
    sample_rate: Optional[int] = None
    usb_mode: Optional[int] = None
    config_descriptor: bytes = b""
    string_descriptors: dict = field(default_factory=dict)
    api_version_raw: int = 0

    @property
    def api_version(self) -> str:
        return "%d.%02d" % (self.api_version_raw >> 16, self.api_version_raw & 0xFFFF)


@dataclass
class DfuStatus:
    state: int
    current: int
    total: int
    completion: int

    @property
    def state_name(self) -> str:
        return DFU_STATE_NAMES.get(self.state, "Unknown(%d)" % self.state)

    @property
    def percent(self) -> float:
        """進捗 %。

        DLL は 2 つのカウンタ (state 構造体の +0x310 / +0x314) を返すが、
        どちらが「現在」でどちらが「総量」かは静的に確定できない (DLL 内に
        書き込み側が無く、ドライバの通知バッファ経由で更新されるため)。
        比率は対称なので min/max で算出すれば、どちらの解釈でも正しい進捗率になる。
        """
        lo = min(self.current, self.total)
        hi = max(self.current, self.total)
        if hi <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * lo / hi))

    @property
    def finished(self) -> bool:
        return self.state in (DFU_STATE_FINISHED, DFU_STATE_FAILED)


@dataclass
class DfuResult:
    operation: str
    state: int = DFU_STATE_IDLE
    completion: int = 0
    transferred: int = 0
    total: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    image: Optional[bytes] = None
    steps: int = 0
    #: ロード経路 ("buffer" = LoadFirmwareImageFromBuffer / "file" = ...FromFile)
    source: str = ""
    #: fw_validator.ValidationResult (download_image 経由のときだけ入る)
    validation: Optional[object] = None

    @property
    def ok(self) -> bool:
        return (not self.cancelled and self.state == DFU_STATE_FINISHED
                and self.completion == TSTATUS_SUCCESS)

    @property
    def state_name(self) -> str:
        return DFU_STATE_NAMES.get(self.state, "Unknown(%d)" % self.state)

    @property
    def completion_name(self) -> str:
        return status_name(self.completion)


# --------------------------------------------------------------------------
# 生の DLL バックエンド
# --------------------------------------------------------------------------
_U = C.c_uint

#: 実 DLL のエクスポート名 -> (restype, [argtypes])。
#:
#: 実 DLL (`xmos_s_usbaudioapi_x64.dll` v5.15) の静的解析で確定した型。
#: 特に ``TUSBAUDIO_LoadFirmwareImageFromFile`` の第 1 引数は **``const wchar_t*``**
#: (DLL 内部 ``0x2560`` が 16 bit 単位の wcslen、モード ``L"rb"``、``CreateFileW``)。
#: ここを ``c_char_p`` にすると実機でイメージロードが必ず失敗する。
#:
#: テストは実 DLL をロードせずにこの表を検査できる (`TestLoadImageSignature`)。
SIGNATURES = {
    "TUSBAUDIO_GetApiVersion": (_U, []),
    "TUSBAUDIO_EnumerateDevices": (_U, []),
    "TUSBAUDIO_GetDeviceCount": (_U, []),
    "TUSBAUDIO_OpenDeviceByIndex": (_U, [_U, C.POINTER(_U)]),
    "TUSBAUDIO_CloseDevice": (_U, [_U]),
    "TUSBAUDIO_GetDeviceProperties": (_U, [_U, C.c_void_p]),
    "TUSBAUDIO_GetCurrentSampleRate": (_U, [_U, C.POINTER(_U)]),
    "TUSBAUDIO_GetDeviceUsbMode": (_U, [_U, C.POINTER(_U)]),
    "TUSBAUDIO_GetUsbConfigDescriptor": (_U, [_U, C.c_void_p, _U, C.POINTER(_U)]),
    # 実 DLL RVA 0x1FF0: (handle, uchar index, langId, wchar_t* buf, bufChars) = 5 引数
    "TUSBAUDIO_GetUsbStringDescriptorString": (
        _U, [_U, _U, _U, C.c_void_p, _U]),
    "TUSBAUDIO_StatusCodeStringW": (C.c_wchar_p, [_U]),
    # --- ここが F-2 の修正点: const wchar_t* path, unsigned int flags ---
    "TUSBAUDIO_LoadFirmwareImageFromFile": (_U, [C.c_wchar_p, _U]),
    "TUSBAUDIO_LoadFirmwareImageFromBuffer": (_U, [C.c_void_p, _U, _U]),
    "TUSBAUDIO_UnloadFirmwareImage": (_U, []),
    "TUSBAUDIO_GetFirmwareImageSize": (_U, [C.POINTER(_U), _U]),
    "TUSBAUDIO_GetFirmwareImage": (_U, [C.c_void_p, _U, C.POINTER(_U), _U]),
    "TUSBAUDIO_StartDfuDownload": (_U, [_U, _U, _U]),
    "TUSBAUDIO_StartDfuUpload": (_U, [_U, _U, _U]),
    "TUSBAUDIO_StartDfuRevertToFactoryImage": (_U, [_U, _U]),
    "TUSBAUDIO_GetDfuStatus": (_U, [C.POINTER(_U)] * 4),
    "TUSBAUDIO_EndDfuProc": (_U, []),
}

#: インスタンス属性名 -> エクスポート名 (バインド順)
BINDINGS = (
    ("_api_version", "TUSBAUDIO_GetApiVersion"),
    ("_enumerate", "TUSBAUDIO_EnumerateDevices"),
    ("_count", "TUSBAUDIO_GetDeviceCount"),
    ("_open", "TUSBAUDIO_OpenDeviceByIndex"),
    ("_close", "TUSBAUDIO_CloseDevice"),
    ("_props", "TUSBAUDIO_GetDeviceProperties"),
    ("_rate", "TUSBAUDIO_GetCurrentSampleRate"),
    ("_usbmode", "TUSBAUDIO_GetDeviceUsbMode"),
    ("_cfgdesc", "TUSBAUDIO_GetUsbConfigDescriptor"),
    ("_strdesc", "TUSBAUDIO_GetUsbStringDescriptorString"),
    ("_strstatus", "TUSBAUDIO_StatusCodeStringW"),
    ("_load_file", "TUSBAUDIO_LoadFirmwareImageFromFile"),
    ("_load_buf", "TUSBAUDIO_LoadFirmwareImageFromBuffer"),
    ("_unload", "TUSBAUDIO_UnloadFirmwareImage"),
    ("_imgsize", "TUSBAUDIO_GetFirmwareImageSize"),
    ("_image", "TUSBAUDIO_GetFirmwareImage"),
    ("_start_down", "TUSBAUDIO_StartDfuDownload"),
    ("_start_up", "TUSBAUDIO_StartDfuUpload"),
    ("_start_revert", "TUSBAUDIO_StartDfuRevertToFactoryImage"),
    ("_dfustatus", "TUSBAUDIO_GetDfuStatus"),
    ("_end", "TUSBAUDIO_EndDfuProc"),
)


class CtypesBackend:
    """実 DLL。読み取り専用 API と DFU API の両方を生ステータスで返す。"""

    name = "real-dll"

    def __init__(self, dll_path: str = DEFAULT_DLL_PATH):
        if os.name != "nt":
            raise OSError("Thesycon DLL backend requires Windows")
        if not os.path.isfile(dll_path):
            raise FileNotFoundError(dll_path)
        self.dll_path = dll_path
        self._dll = C.WinDLL(dll_path)
        self._bind()

    # -- バインディング -----------------------------------------------------
    def _f(self, name):
        restype, argtypes = SIGNATURES[name]
        fn = getattr(self._dll, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    def _bind(self):
        for attr, name in BINDINGS:
            setattr(self, attr, self._f(name))

    # -- 読み取り専用 -------------------------------------------------------
    def get_api_version(self) -> int:
        return int(self._api_version())

    def enumerate_devices(self) -> int:
        return int(self._enumerate())

    def get_device_count(self) -> int:
        return int(self._count())

    def open_device_by_index(self, index: int):
        h = C.c_uint(0)
        st = int(self._open(index, C.byref(h)))
        return st, int(h.value)

    def close_device(self, handle: int) -> int:
        return int(self._close(handle))

    def get_device_properties(self, handle: int):
        buf = C.create_string_buffer(PROPERTIES_BUFFER_SIZE)
        st = int(self._props(handle, buf))
        return st, buf.raw

    def get_current_sample_rate(self, handle: int):
        v = C.c_uint(0)
        st = int(self._rate(handle, C.byref(v)))
        return st, int(v.value)

    def get_device_usb_mode(self, handle: int):
        v = C.c_uint(0)
        st = int(self._usbmode(handle, C.byref(v)))
        return st, int(v.value)

    def get_usb_config_descriptor(self, handle: int):
        size = 64 * 1024
        buf = C.create_string_buffer(size)
        n = C.c_uint(0)
        st = int(self._cfgdesc(handle, buf, size, C.byref(n)))
        return st, buf.raw[:n.value]

    def get_usb_string_descriptor_string(self, handle: int, index: int, lang_id: int = 0,
                                         max_chars: int = 256):
        buf = C.create_unicode_buffer(max_chars)
        st = int(self._strdesc(handle, index, lang_id, C.cast(buf, C.c_void_p), max_chars))
        return st, buf.value

    def status_code_string(self, status: int) -> str:
        try:
            s = self._strstatus(status)
            return s or status_name(status)
        except Exception:
            return status_name(status)

    # -- DFU ---------------------------------------------------------------
    def load_firmware_image_from_file(self, path: str, flags: int = 0) -> int:
        """`const wchar_t*` なので必ず `str` を渡す (UTF-8 バイト列は不可)。"""
        return int(self._load_file(str(path), flags))

    def load_firmware_image_from_buffer(self, data: bytes, flags: int = 0) -> int:
        buf = C.create_string_buffer(bytes(data), len(data))
        return int(self._load_buf(buf, len(data), flags))

    def unload_firmware_image(self) -> int:
        return int(self._unload())

    def get_firmware_image_size(self, flags: int = 0):
        n = C.c_uint(0)
        st = int(self._imgsize(C.byref(n), flags))
        return st, int(n.value)

    def get_firmware_image(self, buf_size: int, flags: int = 0):
        buf = C.create_string_buffer(max(1, buf_size))
        written = C.c_uint(0)
        st = int(self._image(buf, buf_size, C.byref(written), flags))
        return st, buf.raw[:written.value]

    def start_dfu_download(self, device_index: int, image_index: int = 0,
                           flags: int = 0) -> int:
        return int(self._start_down(device_index, image_index, flags))

    def start_dfu_upload(self, device_index: int, image_index: int = 0, flags: int = 0) -> int:
        return int(self._start_up(device_index, image_index, flags))

    def start_dfu_revert_to_factory_image(self, device_index: int, flags: int = 0) -> int:
        return int(self._start_revert(device_index, flags))

    def get_dfu_status(self):
        st_, cur, tot, comp = C.c_uint(0), C.c_uint(0), C.c_uint(0), C.c_uint(0)
        status = int(self._dfustatus(C.byref(st_), C.byref(cur), C.byref(tot), C.byref(comp)))
        return status, int(st_.value), int(cur.value), int(tot.value), int(comp.value)

    def end_dfu_proc(self) -> int:
        return int(self._end())


# --------------------------------------------------------------------------
# モックバックエンド (実機に触れない)
# --------------------------------------------------------------------------
class MockBackend:
    """実機を使わずに DFU シーケンス全体を再現するシミュレータ。

    ``calls`` に ``(操作名, 引数...)`` を順番に記録するので、書込シーケンスの
    完全なドライラン確認に使える。

    Parameters
    ----------
    devices : list[dict]
        接続されていることにするデバイス。既定は DO400 1 台。
    dfu_steps : int
        ``GetDfuStatus`` を何回呼んだら完了するか。
    fail_at_step : int | None
        このステップで Failed にする (エラー注入)。
    fail_status : int
        Failed 時の completion コード。
    stall : bool
        進捗が進まない (タイムアウト試験用)。
    upload_image : bytes | None
        アップロードで返す「現行イメージ」。
    """

    name = "mock"

    def __init__(self, devices: Optional[List[dict]] = None,
                 dfu_steps: int = 8,
                 fail_at_step: Optional[int] = None,
                 fail_status: int = 0xEE000001,
                 stall: bool = False,
                 stall_state: int = DFU_STATE_IN_PROGRESS,
                 upload_image: Optional[bytes] = None,
                 end_status: int = 0,
                 start_status: Optional[int] = None,
                 api_version: int = 0x0005000F):
        if devices is None:
            devices = [dict(vid=VID_DO400, pid=PID_DO400, bcd=BCD_DO400_FW323,
                            manufacturer="SMSL", product="SMSL USB AUDIO",
                            serial="", sample_rate=44100, usb_mode=0, index=0)]
        self.devices = devices
        self.dfu_steps = dfu_steps
        self.fail_at_step = fail_at_step
        self.fail_status = fail_status
        self.stall = stall
        self.stall_state = stall_state
        self.upload_image = upload_image
        self.end_status = end_status
        self.start_status = start_status
        self.api_version = api_version

        self.calls: List[tuple] = []
        self.open_handles: List[int] = []
        self.loaded_image: Optional[bytes] = None
        self.loaded_from: Optional[str] = None
        self.unloaded = False
        self.started: Optional[str] = None
        self.ended = 0
        self._state = DFU_STATE_IDLE
        self._cur = 0
        self._total = 0
        self._completion = 0
        self._step = 0

    # -- 記録 --------------------------------------------------------------
    def _rec(self, name, *args):
        self.calls.append((name,) + args)

    def call_names(self) -> List[str]:
        return [c[0] for c in self.calls]

    # -- 読み取り専用 -------------------------------------------------------
    def get_api_version(self) -> int:
        self._rec("get_api_version")
        return self.api_version

    def enumerate_devices(self) -> int:
        self._rec("enumerate_devices")
        return 0

    def get_device_count(self) -> int:
        self._rec("get_device_count")
        return len(self.devices)

    def open_device_by_index(self, index: int):
        self._rec("open_device_by_index", index)
        if not 0 <= index < len(self.devices):
            return TSTATUS_INVALID_PARAMETER, 0
        handle = 0x10000 + index
        self.open_handles.append(handle)
        return 0, handle

    def close_device(self, handle: int) -> int:
        self._rec("close_device", handle)
        return 0

    def get_device_properties(self, handle: int):
        self._rec("get_device_properties", handle)
        dev = self._device_of(handle)
        if dev is None:
            return TSTATUS_INVALID_PARAMETER, b"\x00" * PROPERTIES_BUFFER_SIZE
        buf = bytearray(PROPERTIES_BUFFER_SIZE)
        struct.pack_into("<3I", buf, 0, dev["vid"], dev["pid"], dev["bcd"])
        for off, key in ((PROP_OFF_MANUFACTURER, "manufacturer"),
                         (PROP_OFF_PRODUCT, "product"),
                         (PROP_OFF_SERIAL, "serial")):
            s = (dev.get(key) or "")[:PROP_STR_CHARS - 1]
            enc = s.encode("utf-16-le")
            buf[off:off + len(enc)] = enc
        return 0, bytes(buf)

    def get_current_sample_rate(self, handle: int):
        self._rec("get_current_sample_rate", handle)
        dev = self._device_of(handle)
        return (0, dev.get("sample_rate", 44100)) if dev else (TSTATUS_INVALID_PARAMETER, 0)

    def get_device_usb_mode(self, handle: int):
        self._rec("get_device_usb_mode", handle)
        dev = self._device_of(handle)
        return (0, dev.get("usb_mode", 0)) if dev else (TSTATUS_INVALID_PARAMETER, 0)

    def get_usb_config_descriptor(self, handle: int):
        self._rec("get_usb_config_descriptor", handle)
        if self._device_of(handle) is None:
            return TSTATUS_INVALID_PARAMETER, b""
        # 最小限の合成コンフィグディスクリプタ (先頭 9 B は実機と同じ形)
        desc = bytes([0x09, 0x02, 0x23, 0x01, 0x04, 0x01, 0x00, 0xC0, 0x00])
        return 0, desc

    def get_usb_string_descriptor_string(self, handle: int, index: int, lang_id: int = 0,
                                         max_chars: int = 256):
        self._rec("get_usb_string_descriptor_string", handle, index, lang_id)
        dev = self._device_of(handle)
        if dev is None:
            return TSTATUS_INVALID_PARAMETER, ""
        if index == 1:
            return 0, dev.get("manufacturer", "")
        if index in (3, 4, 5):
            return 0, dev.get("product", "")
        if index == 0:
            return 0xEE000007, ""
        return 0, ""

    def status_code_string(self, status: int) -> str:
        self._rec("status_code_string", status)
        return status_name(status)

    # -- DFU ---------------------------------------------------------------
    def load_firmware_image_from_file(self, path: str, flags: int = 0) -> int:
        self._rec("load_firmware_image_from_file", path, flags)
        try:
            with open(path, "rb") as fh:
                self.loaded_image = fh.read()
        except OSError:
            return TSTATUS_INVALID_PARAMETER
        self.loaded_from = path
        return 0

    def load_firmware_image_from_buffer(self, data: bytes, flags: int = 0) -> int:
        self._rec("load_firmware_image_from_buffer", len(data), flags)
        self.loaded_image = bytes(data)
        self.loaded_from = None
        return 0

    def unload_firmware_image(self) -> int:
        self._rec("unload_firmware_image")
        self.loaded_image = None
        self.loaded_from = None
        self.unloaded = True
        return 0

    def get_firmware_image_size(self, flags: int = 0):
        self._rec("get_firmware_image_size", flags)
        if flags & 1:
            img = self.upload_image
        else:
            img = self.loaded_image
        if img is None:
            return TSTATUS_NO_IMAGE_LOADED, 0
        return 0, len(img)

    def get_firmware_image(self, buf_size: int, flags: int = 0):
        self._rec("get_firmware_image", buf_size, flags)
        img = self.upload_image if flags & 1 else self.loaded_image
        if img is None:
            return TSTATUS_NO_IMAGE_LOADED, b""
        if buf_size < len(img):
            return 0xEE000074, b""
        return 0, bytes(img)

    def start_dfu_download(self, device_index: int, image_index: int = 0, flags: int = 0) -> int:
        self._rec("start_dfu_download", device_index, image_index, flags)
        if self.start_status is not None:
            return self.start_status
        if self.loaded_image is None:
            return TSTATUS_NO_IMAGE_LOADED
        if not 0 <= device_index < len(self.devices):
            return 0xEE000047                      # TSTATUS_INVALID_INDEX
        self.started = "download"
        self._total = len(self.loaded_image)
        self._begin()
        return 0

    def start_dfu_upload(self, device_index: int, image_index: int = 0, flags: int = 0) -> int:
        self._rec("start_dfu_upload", device_index, image_index, flags)
        if self.start_status is not None:
            return self.start_status
        if not 0 <= device_index < len(self.devices):
            return 0xEE000047
        self.started = "upload"
        self._total = len(self.upload_image or b"")
        self._begin()
        return 0

    def start_dfu_revert_to_factory_image(self, device_index: int, flags: int = 0) -> int:
        self._rec("start_dfu_revert_to_factory_image", device_index, flags)
        if self.start_status is not None:
            return self.start_status
        if not 0 <= device_index < len(self.devices):
            return 0xEE000047
        self.started = "revert"
        self._total = 0x80000
        self._begin()
        return 0

    def get_dfu_status(self):
        self._rec("get_dfu_status")
        if self.started is None:
            # DFU プロセスが開始されていない = 常に Idle (読み取り専用で安全)
            return 0, self._state, 0, 0, self._completion
        if self._state in (DFU_STATE_FINISHED, DFU_STATE_FAILED):
            return 0, self._state, self._cur, self._total, self._completion

        self._step += 1
        if self.fail_at_step is not None and self._step >= self.fail_at_step:
            self._state = DFU_STATE_FAILED
            self._completion = self.fail_status
            return 0, self._state, self._cur, self._total, self._completion

        if self.stall:
            self._state = self.stall_state
            return 0, self._state, self._cur, self._total, self._completion

        if self._step == 1:
            self._state = DFU_STATE_INITIALIZING
        elif self._step < self.dfu_steps:
            self._state = DFU_STATE_IN_PROGRESS
            self._cur = int(self._total * self._step / self.dfu_steps)
        else:
            self._state = DFU_STATE_FINISHED
            self._cur = self._total
            self._completion = 0
        return 0, self._state, self._cur, self._total, self._completion

    def end_dfu_proc(self) -> int:
        self._rec("end_dfu_proc")
        self.ended += 1
        self._state = DFU_STATE_IDLE
        return self.end_status

    # -- helpers -----------------------------------------------------------
    def _begin(self):
        self._state = DFU_STATE_INITIALIZING
        self._cur = 0
        self._completion = 0
        self._step = 0

    def _device_of(self, handle: int):
        idx = handle - 0x10000
        if 0 <= idx < len(self.devices):
            return self.devices[idx]
        return None


class ReadOnlyGuardBackend(MockBackend):
    """読み取り専用試験用。書込系が呼ばれたら即座に失敗させる。"""

    name = "readonly-guard"

    def _forbidden(self, name):
        raise AssertionError("write API must not be called: %s" % name)

    def load_firmware_image_from_file(self, *a, **k):
        self._forbidden("load_firmware_image_from_file")

    def load_firmware_image_from_buffer(self, *a, **k):
        self._forbidden("load_firmware_image_from_buffer")

    def unload_firmware_image(self, *a, **k):
        self._forbidden("unload_firmware_image")

    def get_firmware_image(self, *a, **k):
        self._forbidden("get_firmware_image")

    def start_dfu_download(self, *a, **k):
        self._forbidden("start_dfu_download")

    def start_dfu_upload(self, *a, **k):
        self._forbidden("start_dfu_upload")

    def start_dfu_revert_to_factory_image(self, *a, **k):
        self._forbidden("start_dfu_revert_to_factory_image")

    def end_dfu_proc(self, *a, **k):
        self._forbidden("end_dfu_proc")


# --------------------------------------------------------------------------
# 高レベルラッパー
# --------------------------------------------------------------------------
class ThesyconApi:
    """読み取り専用操作用の安全なフロントエンド + 明示許可制の DFU 操作。"""

    def __init__(self, backend=None, dll_path: str = DEFAULT_DLL_PATH,
                 allow_write: bool = False, logger: Optional[Callable[[str], None]] = None):
        self.backend = backend if backend is not None else CtypesBackend(dll_path)
        self.allow_write = allow_write
        self._log = logger or (lambda msg: None)
        self.calls: List[tuple] = []
        self._write_lock = threading.Lock()

    # -- 内部 --------------------------------------------------------------
    def _record(self, name, *args):
        self.calls.append((name,) + args)

    def _require_write(self, operation: str):
        if operation in WRITE_OPERATIONS and not self.allow_write:
            raise SafetyError(
                "%s は書込系操作のため拒否しました (allow_write=True が必要)" % operation)

    def _check(self, status: int, operation: str):
        self._record(operation, status)
        if status != TSTATUS_SUCCESS:
            raise ThesyconError("%s が失敗しました" % operation, status, operation)
        return status

    @property
    def backend_name(self) -> str:
        return getattr(self.backend, "name", type(self.backend).__name__)

    # -- 読み取り専用 API ---------------------------------------------------
    def get_api_version_raw(self) -> int:
        v = self.backend.get_api_version()
        self._record("get_api_version", v)
        return v

    def api_version(self) -> str:
        v = self.get_api_version_raw()
        return "%d.%02d" % (v >> 16, v & 0xFFFF)

    def enumerate_devices(self) -> int:
        st = self.backend.enumerate_devices()
        self._record("enumerate_devices", st)
        return st

    def device_count(self) -> int:
        n = self.backend.get_device_count()
        self._record("get_device_count", n)
        return n

    def open_device(self, index: int):
        st, handle = self.backend.open_device_by_index(index)
        self._check(st, "open_device_by_index")
        return handle

    def close_device(self, handle: int):
        st = self.backend.close_device(handle)
        self._record("close_device", st)
        return st

    def read_properties(self, handle: int) -> DeviceProperties:
        st, raw = self.backend.get_device_properties(handle)
        self._check(st, "get_device_properties")
        if len(raw) < PROP_OFF_SERIAL + 4:
            raise ThesyconError("GetDeviceProperties の戻りが短すぎます: %d B" % len(raw))
        return DeviceProperties(
            vendor_id=struct.unpack_from("<I", raw, PROP_OFF_VID)[0],
            product_id=struct.unpack_from("<I", raw, PROP_OFF_PID)[0],
            bcd_device=struct.unpack_from("<I", raw, PROP_OFF_BCD)[0],
            manufacturer=_decode_wstr(raw, PROP_OFF_MANUFACTURER, PROP_STR_CHARS),
            product=_decode_wstr(raw, PROP_OFF_PRODUCT, PROP_STR_CHARS),
            serial=_decode_wstr(raw, PROP_OFF_SERIAL, PROP_STR_CHARS),
            raw=raw,
        )

    def read_sample_rate(self, handle: int) -> int:
        st, rate = self.backend.get_current_sample_rate(handle)
        self._check(st, "get_current_sample_rate")
        return rate

    def read_usb_mode(self, handle: int) -> int:
        st, mode = self.backend.get_device_usb_mode(handle)
        self._check(st, "get_device_usb_mode")
        return mode

    def read_config_descriptor(self, handle: int) -> bytes:
        st, desc = self.backend.get_usb_config_descriptor(handle)
        self._check(st, "get_usb_config_descriptor")
        return desc

    def read_string_descriptor(self, handle: int, index: int, lang_id: int = 0) -> str:
        st, s = self.backend.get_usb_string_descriptor_string(handle, index, lang_id)
        self._record("get_usb_string_descriptor_string", index, st)
        if st != TSTATUS_SUCCESS:
            raise ThesyconError("GetUsbStringDescriptorString(%d) 失敗" % index, st)
        return s

    def status_string(self, status: int) -> str:
        return self.backend.status_code_string(status)

    def read_device_snapshot(self, index: int, with_descriptors: bool = True) -> DeviceSnapshot:
        """1 台分を読み取り専用で取得する (ハンドルは必ず閉じる)。"""
        handle = self.open_device(index)
        try:
            props = self.read_properties(handle)
            snap = DeviceSnapshot(
                index=index, handle=handle, properties=props,
                api_version_raw=self.get_api_version_raw(),
            )
            try:
                snap.sample_rate = self.read_sample_rate(handle)
            except ThesyconError as exc:
                self._log("GetCurrentSampleRate 失敗: %s" % exc)
            try:
                snap.usb_mode = self.read_usb_mode(handle)
            except ThesyconError as exc:
                self._log("GetDeviceUsbMode 失敗: %s" % exc)
            if with_descriptors:
                try:
                    snap.config_descriptor = self.read_config_descriptor(handle)
                except ThesyconError as exc:
                    self._log("GetUsbConfigDescriptor 失敗: %s" % exc)
                for idx in (1, 2, 3):
                    try:
                        snap.string_descriptors[idx] = self.read_string_descriptor(handle, idx)
                    except ThesyconError:
                        pass
            return snap
        finally:
            self.close_device(handle)

    def scan_devices(self, with_descriptors: bool = False) -> List[DeviceSnapshot]:
        """全デバイスを列挙してスナップショットを返す (読み取り専用)。"""
        self._require_read_environment()
        self.enumerate_devices()
        count = self.device_count()
        out = []
        for i in range(count):
            out.append(self.read_device_snapshot(i, with_descriptors=with_descriptors))
        return out

    def _require_read_environment(self):
        if self.backend_name == "real-dll":
            # 実 DLL では EnumerateDevices 前に他 API を叩かないことだけ守る
            pass

    # -- DFU 状態 ----------------------------------------------------------
    def get_dfu_status(self) -> DfuStatus:
        status, state, cur, tot, comp = self.backend.get_dfu_status()
        self._check(status, "get_dfu_status")
        return DfuStatus(state=state, current=cur, total=tot, completion=comp)

    # -- DFU 本体 ----------------------------------------------------------
    def download_image(self, path: str, device_index: int = 0, image_index: int = 0,
                       flags: int = 0,
                       on_progress: Optional[Callable[[DfuStatus], None]] = None,
                       timeout: float = 600.0, poll_interval: float = 0.2,
                       cancel: Optional[threading.Event] = None) -> DfuResult:
        """**標準の更新経路**: 検証済みバイト列を `LoadFirmwareImageFromBuffer` へ渡す。

        1. ファイルを 1 回だけ読む (この後ファイルが差し替わっても影響しない)
        2. `fw_validator.validate_bytes()` で検証し、不合格なら *バックエンドに一切
           触れずに* `ImageValidationError`
        3. 検証したのと同じ `bytes` を `TUSBAUDIO_LoadFirmwareImageFromBuffer(
           data, len(data), 0)` へ渡す

        したがって TOCTOU (検証〜書込間のファイル差替) とパス文字コード問題
        (wchar_t/UTF-8) の双方が構造的に起きない。
        """
        self._require_write("start_dfu_download")
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as fh:
            data = fh.read()

        report = _validate_image_bytes(data, path)
        if not report.write_allowed:
            raise ImageValidationError(_validation_failure_text(report), report=report)

        result = self.dfu_download(image_bytes=data, device_index=device_index,
                                   image_index=image_index, flags=flags,
                                   on_progress=on_progress, timeout=timeout,
                                   poll_interval=poll_interval, cancel=cancel)
        result.source = "buffer"
        result.validation = report
        return result

    def dfu_download(self, image_path: Optional[str] = None, image_bytes: Optional[bytes] = None,
                     device_index: int = 0, image_index: int = 0, flags: int = 0,
                     on_progress: Optional[Callable[[DfuStatus], None]] = None,
                     timeout: float = 600.0, poll_interval: float = 0.2,
                     cancel: Optional[threading.Event] = None) -> DfuResult:
        """ファームウェア書込 (LoadFirmwareImage* -> StartDfuDownload -> poll -> EndDfuProc).

        `image_path` 経路も残すが (回帰テスト用)、アプリの標準経路は
        `download_image()` = `LoadFirmwareImageFromBuffer` のみ。
        """
        self._require_write("start_dfu_download")
        if (image_path is None) == (image_bytes is None):
            raise ValueError("image_path か image_bytes のどちらか一方だけを指定してください")
        if image_path is not None and not os.path.isfile(image_path):
            raise FileNotFoundError(image_path)

        started = False
        try:
            if image_path is not None:
                st = self.backend.load_firmware_image_from_file(image_path, flags)
                self._record("load_firmware_image_from_file", image_path, flags, st)
                source = "file"
            else:
                st = self.backend.load_firmware_image_from_buffer(image_bytes, flags)
                self._record("load_firmware_image_from_buffer", len(image_bytes), flags, st)
                source = "buffer"
            self._check(st, "load_firmware_image")

            st = self.backend.start_dfu_download(device_index, image_index, flags)
            self._record("start_dfu_download", device_index, image_index, flags, st)
            self._check(st, "start_dfu_download")
            started = True

            result = self._poll("download", on_progress, timeout, poll_interval, cancel)
            result.source = source
            return self._ensure_finished("DFU 書込", "start_dfu_download", result)
        finally:
            # 失敗・キャンセル・タイムアウトでも必ず後始末する
            try:
                est = self.backend.end_dfu_proc()
                self._record("end_dfu_proc", est)
            finally:
                try:
                    ust = self.backend.unload_firmware_image()
                    self._record("unload_firmware_image", ust)
                except Exception:
                    pass
            _ = started

    def dfu_upload(self, device_index: int = 0, image_index: int = 0, flags: int = 0,
                   on_progress: Optional[Callable[[DfuStatus], None]] = None,
                   timeout: float = 300.0, poll_interval: float = 0.2,
                   cancel: Optional[threading.Event] = None) -> DfuResult:
        """現行イメージ吸出し (StartDfuUpload -> poll -> GetFirmwareImage* -> EndDfuProc)."""
        self._require_write("start_dfu_upload")
        try:
            st = self.backend.start_dfu_upload(device_index, image_index, flags)
            self._record("start_dfu_upload", device_index, image_index, flags, st)
            self._check(st, "start_dfu_upload")

            result = self._poll("upload", on_progress, timeout, poll_interval, cancel)
            if result.cancelled:
                return result
            self._ensure_finished("DFU 吸出し", "start_dfu_upload", result)

            st, size = self.backend.get_firmware_image_size(1)
            self._record("get_firmware_image_size", 1, st, size)
            self._check(st, "get_firmware_image_size")
            if size <= 0:
                raise ThesyconError("吸出したイメージサイズが 0 です", TSTATUS_NO_IMAGE_LOADED,
                                    "get_firmware_image_size")
            st, data = self.backend.get_firmware_image(size, 1)
            self._record("get_firmware_image", size, 1, st, len(data))
            self._check(st, "get_firmware_image")
            result.image = data
            result.total = size
            result.transferred = len(data)
            return result
        finally:
            try:
                est = self.backend.end_dfu_proc()
                self._record("end_dfu_proc", est)
            except Exception:
                pass

    def dfu_revert_to_factory(self, device_index: int = 0, flags: int = 0,
                              on_progress: Optional[Callable[[DfuStatus], None]] = None,
                              timeout: float = 300.0, poll_interval: float = 0.2,
                              cancel: Optional[threading.Event] = None) -> DfuResult:
        """Factory イメージ復帰 (StartDfuRevertToFactoryImage -> poll -> EndDfuProc)."""
        self._require_write("start_dfu_revert_to_factory_image")
        try:
            st = self.backend.start_dfu_revert_to_factory_image(device_index, flags)
            self._record("start_dfu_revert_to_factory_image", device_index, flags, st)
            self._check(st, "start_dfu_revert_to_factory_image")

            result = self._poll("revert", on_progress, timeout, poll_interval, cancel)
            return self._ensure_finished("Factory 復帰", "start_dfu_revert_to_factory_image",
                                         result)
        finally:
            try:
                est = self.backend.end_dfu_proc()
                self._record("end_dfu_proc", est)
            except Exception:
                pass

    def _ensure_finished(self, what: str, operation: str, result: DfuResult) -> DfuResult:
        """`state == Finished` **かつ** `completion == 0` のときだけ成功とみなす (F-4).

        `Finished` でも `completion != 0` は失敗、`Failed` はもちろん失敗。
        キャンセル済みは呼び出し側が先に処理する (ここでは成功扱いにしない)。
        """
        if result.cancelled:
            return result
        if (result.state == DFU_STATE_FINISHED
                and result.completion == TSTATUS_SUCCESS):
            return result
        raise DfuError("%sが失敗しました (state=%s, completion=0x%08X %s)" % (
            what, result.state_name, result.completion, result.completion_name),
            result.completion, operation, result=result)

    # -- ポーリング --------------------------------------------------------
    def _poll(self, operation: str, on_progress, timeout: float, poll_interval: float,
              cancel: Optional[threading.Event]) -> DfuResult:
        started_at = time.monotonic()
        deadline = started_at + max(0.1, timeout)
        result = DfuResult(operation=operation)
        cancel_locked = False
        cancel_suppressed_logged = False
        while True:
            status = self.get_dfu_status()
            result.state = status.state
            result.completion = status.completion
            result.transferred = status.current
            result.total = status.total
            result.steps += 1
            if status.state in DFU_TRANSFER_LOCK_STATES:
                cancel_locked = True
            if on_progress is not None:
                try:
                    on_progress(status)
                except Exception as exc:      # UI 側の例外で DFU を壊さない
                    self._log("progress callback error: %s" % exc)
            if status.finished:
                result.elapsed = time.monotonic() - started_at
                return result
            if cancel is not None and cancel.is_set():
                if cancel_locked:
                    if not cancel_suppressed_logged:
                        cancel_suppressed_logged = True
                        self._log("DFU 転送中のため中断要求を抑止しました (state=%s)" % status.state_name)
                else:
                    result.cancelled = True
                    result.elapsed = time.monotonic() - started_at
                    self._log("DFU 中断要求を受け取りました")
                    return result
            if time.monotonic() > deadline:
                result.elapsed = time.monotonic() - started_at
                raise DfuError("DFU がタイムアウトしました (%.0f 秒)" % timeout,
                               None, operation, result=result)
            time.sleep(poll_interval)

    # -- 便利関数 ----------------------------------------------------------
    def find_do400(self, snapshots: Optional[List[DeviceSnapshot]] = None) -> Optional[DeviceSnapshot]:
        snaps = snapshots if snapshots is not None else self.scan_devices()
        for s in snaps:
            if s.properties.is_do400:
                return s
        return None

    def describe_device(self, snap: DeviceSnapshot) -> str:
        p = snap.properties
        return ("index=%d  %s  bcdDevice=0x%04X (FW %s)  %s / %s  %d Hz" % (
            snap.index, p.vid_pid, p.bcd_device, p.firmware_version,
            p.manufacturer or "?", p.product or "?", snap.sample_rate or 0))


def _validate_image_bytes(data: bytes, name: str):
    """`fw_validator.validate_bytes()` を遅延 import で呼ぶ。

    thesycon_api を単体で import したときに fw_validator への依存を持ち込まない
    ようにするための遅延 import。呼出時点では同一ディレクトリに fw_validator.py が
    必要 (アプリ本体と同じ配置なら常に満たされる)。
    """
    import fw_validator
    return fw_validator.validate_bytes(data, name)


def _validation_failure_text(report) -> str:
    names = ", ".join(c.name for c in report.failures) or "-"
    return "イメージが事前検証に不合格のため書込みを中止しました (%s): %s" % (
        report.verdict, names)


def _decode_wstr(raw: bytes, offset: int, chars: int) -> str:
    blob = raw[offset:offset + chars * 2]
    try:
        return blob.decode("utf-16-le", "replace").split("\x00", 1)[0]
    except Exception:
        return ""


def list_audio_devices(api: Optional[ThesyconApi] = None) -> List[DeviceSnapshot]:
    """読み取り専用のデバイス列挙 (ショートカット)。"""
    api = api or ThesyconApi()
    return api.scan_devices()
