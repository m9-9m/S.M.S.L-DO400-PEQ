#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""peq_server.py -- Local loopback static server + optional Windows HID bridge for DO400 PEQ Console.

Zero external dependencies (Python 3.8+ standard library only).
Binds strictly to 127.0.0.1 so only local browsers can access it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as W
import http.server
import json
import os
import struct
import sys
import threading
import time

HOST = "127.0.0.1"
PORT = int(os.environ.get("DO400_PEQ_PORT", "8136"))
WEB_DIR = os.path.dirname(os.path.abspath(__file__))

VID_DO400 = 0x152A
PID_DO400 = 0x85DD


class _WindowsHidBridge:
    """Minimal Windows hid.dll / setupapi.dll Feature Report transport for DO400 (152A:85DD)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._available = (os.name == "nt")
        if not self._available:
            return
        self._setupapi = ctypes.WinDLL("setupapi.dll", use_last_error=True)
        self._hid = ctypes.WinDLL("hid.dll", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
            _fields_ = [
                ("cbSize", W.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", W.DWORD), ("Reserved", ctypes.c_void_p),
            ]

        self._GUID = GUID
        self._SP_DID = SP_DEVICE_INTERFACE_DATA
        self._INVALID = ctypes.c_void_p(-1).value

        self._setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
        self._setupapi.SetupDiGetClassDevsW.argtypes = [
            ctypes.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD]
        self._setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
        self._setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID),
            W.DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)]
        self._setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA),
            ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.c_void_p]

        self._kernel32.CreateFileW.restype = ctypes.c_void_p
        self._kernel32.CreateFileW.argtypes = [
            W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p,
            W.DWORD, W.DWORD, ctypes.c_void_p]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

        self._hid.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
        self._hid.HidD_GetProductString.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.ULONG]
        self._hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.ULONG]
        self._hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, W.ULONG]

    def find_do400_path(self):
        if not self._available:
            return None
        g = self._GUID()
        self._hid.HidD_GetHidGuid(ctypes.byref(g))
        hdev = self._setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x12)
        if not hdev or hdev == self._INVALID:
            return None
        try:
            idx = 0
            detail_cb = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            while True:
                did = self._SP_DID()
                did.cbSize = ctypes.sizeof(did)
                if not self._setupapi.SetupDiEnumDeviceInterfaces(
                        hdev, None, ctypes.byref(g), idx, ctypes.byref(did)):
                    break
                idx += 1
                req = W.DWORD(0)
                self._setupapi.SetupDiGetDeviceInterfaceDetailW(
                    hdev, ctypes.byref(did), None, 0, ctypes.byref(req), None)
                if req.value <= 4:
                    continue
                buf = ctypes.create_string_buffer(req.value)
                struct.pack_into("<I", buf, 0, detail_cb)
                if not self._setupapi.SetupDiGetDeviceInterfaceDetailW(
                        hdev, ctypes.byref(did), buf, req.value, None, None):
                    continue
                path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
                pl = path.lower()
                if "vid_152a" in pl and "pid_85dd" in pl:
                    return path
            return None
        finally:
            self._setupapi.SetupDiDestroyDeviceInfoList(hdev)

    def probe(self):
        with self._lock:
            path = self.find_do400_path()
            if not path:
                return {"ok": True, "available": self._available, "connected": False}
            fh = self._kernel32.CreateFileW(path, 0, 3, None, 3, 0, None)
            product = "SMSL USB AUDIO"
            if fh and fh != self._INVALID:
                try:
                    sb = ctypes.create_unicode_buffer(128)
                    if self._hid.HidD_GetProductString(fh, sb, 256) and sb.value:
                        product = sb.value
                finally:
                    self._kernel32.CloseHandle(fh)
            return {
                "ok": True,
                "available": True,
                "connected": True,
                "product": product,
                "vid": VID_DO400,
                "pid": PID_DO400,
            }

    def exchange(self, req64: bytes) -> bytes:
        if len(req64) != 64:
            raise ValueError("Expected 64-byte request, got %d" % len(req64))
        with self._lock:
            path = self.find_do400_path()
            if not path:
                raise RuntimeError("DO400 HID device (152A:85DD) not found")
            fh = self._kernel32.CreateFileW(path, 0xC0000000, 3, None, 3, 0, None)
            if not fh or fh == self._INVALID:
                raise RuntimeError("CreateFileW failed (err=%d)" % ctypes.get_last_error())
            try:
                fbuf = (ctypes.c_ubyte * 65)(0, *req64)
                if not self._hid.HidD_SetFeature(fh, fbuf, 65):
                    raise RuntimeError("HidD_SetFeature failed (err=%d)" % ctypes.get_last_error())
                rbuf = (ctypes.c_ubyte * 65)()
                rbuf[0] = 0
                if not self._hid.HidD_GetFeature(fh, rbuf, 65):
                    raise RuntimeError("HidD_GetFeature failed (err=%d)" % ctypes.get_last_error())
                time.sleep(0.004)
                return bytes(rbuf[1:65])
            finally:
                self._kernel32.CloseHandle(fh)


BRIDGE = _WindowsHidBridge()


class PeqHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        # Suppress noisy polling logs; print only page loads and errors
        if args and "/api/bridge/" in str(args[0]) and " 200 " in " ".join(str(a) for a in args):
            return
        super().log_message(fmt, *args)

    def _send_json(self, code: int, obj: dict):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/api/bridge/status":
            try:
                self._send_json(200, BRIDGE.probe())
            except Exception as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
            return
        if self.path == "/" or self.path == "":
            self.path = "/do400_peq_webhid.html"
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/bridge/exchange":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            resp = BRIDGE.exchange(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
        except Exception as exc:
            self._send_json(502, {"ok": False, "error": str(exc)})


def main():
    server = http.server.ThreadingHTTPServer((HOST, PORT), PeqHandler)
    print("============================================================")
    print(" DO400 PEQ Console - local server + Windows HID bridge")
    print(" http://localhost:%d/do400_peq_webhid.html" % PORT)
    print(" Ctrl+C stops the server.")
    print("============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
