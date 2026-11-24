#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""do400_dfu_app.py -- DO400 (XMOS XU316) 簡易 DFU 更新アプリ.

Thesycon 公式 API (`xmos_s_usbaudioapi_x64.dll`, API v5.15) を ctypes で直接叩く、
追加 pip 依存ゼロ (Python 標準ライブラリのみ) のデスクトップアプリ。

起動方法
--------------------------------------------------------------------------
    python do400_dfu_app.py                    # GUI 起動
    python do400_dfu_app.py --check-device     # 読み取り専用の状態表示
    python do400_dfu_app.py --validate X.bin   # オフライン検証のみ
    python do400_dfu_app.py --dry-run-flash X.bin  # 書込シーケンスのドライラン
    python do400_dfu_app.py --list-devices     # 接続デバイス一覧 (読み取り専用)

安全設計
--------------------------------------------------------------------------
* `--check-device` / `--validate` / `--list-devices` は **完全読み取り専用**。
  書込系 API は `ThesyconApi._require_write()` が `allow_write=False` で拒否する。
* `--dry-run-flash` はモックバックエンドで書込シーケンスを再生するだけで、
  実 DLL の DFU 関数を一切呼ばない。
* 更新の標準経路は **`fw_validator.validate_bytes()` に合格したメモリ上の
  `bytes` を `TUSBAUDIO_LoadFirmwareImageFromBuffer` へ渡す**方式 (F-2)。
  検証済みバイト列しか DLL に渡らないので、検証〜書込間のファイル差替
  (TOCTOU) と `wchar_t*` パス問題が構造的に起きない。
* 完了判定は `state == Finished` かつ `completion == 0` のみを成功とする (F-4)。
  転送中 (`state >= InProgress`) は「中断」ボタンを無効化する。
* GUI の書込操作は「確認ダイアログ + チェックボックス」の二重確認を経た場合のみ実行し、
  実行中は全ボタンを無効化、`EndDfuProc` は必ず `finally` で呼ぶ。
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser

import fw_validator as V
import thesycon_api as T

APP_TITLE = "DO400 DFU 更新ツール (XMOS / Thesycon API)"
APP_VERSION = "1.0"

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
RECOMMENDED_FW_IMAGE = os.path.abspath(
    os.path.join(_APP_DIR, "..", "firmware", "do400_peq_dfu.bin"))
RECOMMENDED_WEBHID_HTML = os.path.abspath(
    os.path.join(_APP_DIR, "..", "webhid", "do400_peq_webhid.html"))

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    _TK_AVAILABLE = True
except Exception:                                    # pragma: no cover
    tk = None
    ttk = filedialog = messagebox = None
    _TK_AVAILABLE = False


# ==========================================================================
# 共通ヘルパ
# ==========================================================================
def _fmt_bytes(n) -> str:
    if n is None:
        return "-"
    return "%s B (%.1f KiB)" % (format(n, ","), n / 1024.0)


def _setup_stdout():
    """コンソールのコードページを尊重しつつ、表示できない文字で落ちないようにする。

    (UTF-8 を強制すると cp932 コンソールで日本語が化けるため、encoding は触らない)
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


# ==========================================================================
# DFU 結果判定 / 転送中キャンセル抑止 (GUI 非依存の純ロジック)
# ==========================================================================
#: 転送が始まったとみなす状態 (3 = InProgress, 4 = EnteringAppMode)。
#: ここに入ったら「中断」を受け付けない (EndDfuProc による転送切断を防ぐ)。
TRANSFER_LOCK_STATES = (T.DFU_STATE_IN_PROGRESS, T.DFU_STATE_ENTERING_APP_MODE)

CANCEL_LOCK_MESSAGE = "転送中のため中断できません（完了まで待機してください）"
CANCEL_LOCK_MESSAGE_EN = "Transfer in progress — cannot cancel (please wait until complete)"


def cancel_lock_message(lang: str = "ja") -> str:
    return CANCEL_LOCK_MESSAGE_EN if lang == "en" else CANCEL_LOCK_MESSAGE

#: バックエンド操作名 -> 実 DLL のエクスポート名 (ドライラン表示用)
DLL_API_LABELS = {
    "load_firmware_image_from_buffer": "TUSBAUDIO_LoadFirmwareImageFromBuffer",
    "load_firmware_image_from_file": "TUSBAUDIO_LoadFirmwareImageFromFile",
    "unload_firmware_image": "TUSBAUDIO_UnloadFirmwareImage",
    "start_dfu_download": "TUSBAUDIO_StartDfuDownload",
    "start_dfu_upload": "TUSBAUDIO_StartDfuUpload",
    "start_dfu_revert_to_factory_image": "TUSBAUDIO_StartDfuRevertToFactoryImage",
    "get_dfu_status": "TUSBAUDIO_GetDfuStatus",
    "end_dfu_proc": "TUSBAUDIO_EndDfuProc",
}


class DfuOperationError(RuntimeError):
    """DFU は終了したが ``result.ok`` ではない (completion != 0 / Failed)。"""


class WorkerOutcome:
    """GUI ワーカーの戻り値。

    ``ok=False`` はエラー扱い (赤字ログ + エラーダイアログ)。
    ``cancelled`` はユーザー操作による中断なので、エラーではなく情報表示にする。
    """

    def __init__(self, ok: bool, text: str, cancelled: bool = False):
        self.ok = bool(ok)
        self.text = text
        self.cancelled = bool(cancelled)

    def __str__(self):
        return self.text


def describe_dfu_result(label: str, result: T.DfuResult, lang: str = "ja"):
    """``result.ok`` を厳密に判定して ``(ok, メッセージ)`` を返す (F-4).

    * ``Finished`` + ``completion == 0`` のみ成功
    * ``Finished`` + ``completion != 0`` は **失敗** (従来は「完了」と誤表示していた)
    * ``Failed`` / キャンセルも失敗 (メッセージにステータスコード文字列を含める)
    """
    if lang == "en":
        detail = "state=%s, completion=0x%08X (%s), transferred %s / %.1f s" % (
            result.state_name, result.completion, result.completion_name,
            _fmt_bytes(result.transferred), result.elapsed)
        if result.cancelled:
            return False, "%s: Cancelled (EndDfuProc completed) — %s" % (label, detail)
        if result.state == T.DFU_STATE_FAILED:
            return False, "%s: Failed — %s" % (label, detail)
        if not result.ok:
            return False, "%s: Failed (unexpected completion code) — %s" % (label, detail)
        return True, "%s: Completed — %s" % (label, detail)

    detail = "state=%s, completion=0x%08X (%s), 転送 %s / %.1f 秒" % (
        result.state_name, result.completion, result.completion_name,
        _fmt_bytes(result.transferred), result.elapsed)

    if result.cancelled:
        return False, "%s: 中断されました (EndDfuProc 実行済み) — %s" % (label, detail)
    if result.state == T.DFU_STATE_FAILED:
        return False, "%s: 失敗 — %s" % (label, detail)
    if not result.ok:
        return False, "%s: 完了コード異常のため失敗 — %s" % (label, detail)
    return True, "%s: 完了 — %s" % (label, detail)


class TransferCancelGuard:
    """転送中 (``state >= InProgress``) のキャンセル抑止 (GUI 非依存).

    ``Initializing`` / ``EnteringDfuMode`` の間はキャンセル可能。
    ``InProgress`` / ``EnteringAppMode`` に入った時点でロックし、完了後
    (:meth:`reset` まで) は解除しない。タイムアウト・例外時の ``EndDfuProc`` は
    API 側の ``finally`` が必ず呼ぶので、ここでは抑止だけを行う。
    """

    def __init__(self):
        self.locked = False
        self.state = None

    def observe(self, status) -> bool:
        """進捗を観測する。新たにロックしたときだけ True を返す。"""
        self.state = status.state
        if status.state in TRANSFER_LOCK_STATES and not self.locked:
            self.locked = True
            return True
        return False

    @property
    def can_cancel(self) -> bool:
        return not self.locked

    def reset(self):
        self.locked = False
        self.state = None


def perform_flash(api: T.ThesyconApi, path: str, device_index: int = 0,
                  on_progress=None, timeout: float = 600.0,
                  poll_interval: float = 0.2, cancel=None) -> T.DfuResult:
    """**更新実行の唯一の経路** (F-2).

    ファイルを 1 回読み → `fw_validator.validate_bytes()` で検証 →
    *同じ* `bytes` を ``TUSBAUDIO_LoadFirmwareImageFromBuffer`` へ渡す。
    したがって検証〜書込間のファイル差替 (TOCTOU) もパス文字コード
    (``wchar_t``) 問題も構造的に起きない。
    """
    return api.download_image(path=path, device_index=device_index, image_index=0,
                              flags=0, on_progress=on_progress, timeout=timeout,
                              poll_interval=poll_interval, cancel=cancel)


# ==========================================================================
# CLI: 読み取り専用チェック
# ==========================================================================
def cmd_list_devices(args) -> int:
    api = T.ThesyconApi()
    try:
        snaps = api.scan_devices(with_descriptors=False)
    except Exception as exc:
        print("デバイス列挙に失敗しました: %s" % exc)
        return 1
    print("ドライバ API バージョン: %s" % api.api_version())
    print("検出デバイス数: %d" % len(snaps))
    for s in snaps:
        print("  [%d] %s  bcdDevice=0x%04X (FW %s)  %s / %s  %s Hz" % (
            s.index, s.properties.vid_pid, s.properties.bcd_device,
            s.properties.firmware_version, s.properties.manufacturer or "?",
            s.properties.product or "?", s.sample_rate or 0))
    return 0 if snaps else 1


def cmd_check_device(args) -> int:
    """完全読み取り専用。DFU 状態遷移は一切行わない。"""
    print("=" * 68)
    print(" DO400 デバイス状態 (読み取り専用 / 非破壊)")
    print("=" * 68)
    api = T.ThesyconApi()
    print(" DLL                : %s" % T.DEFAULT_DLL_PATH)
    print(" ドライバ API       : %s" % api.api_version())
    try:
        enumerate_status = api.enumerate_devices()
        count = api.device_count()
    except Exception as exc:
        print(" 列挙失敗           : %s" % exc)
        return 1
    print(" EnumerateDevices   : %s" % T.status_name(enumerate_status))
    print(" 検出デバイス数     : %d" % count)
    if count == 0:
        print("\n DO400 が見つかりません (USB 接続とドライバを確認してください)。")
        return 1

    found = None
    for i in range(count):
        try:
            snap = api.read_device_snapshot(i, with_descriptors=True)
        except Exception as exc:
            print("  [%d] 読み取り失敗: %s" % (i, exc))
            continue
        p = snap.properties
        print("")
        print("  [%d] %s" % (i, "DO400" if p.is_do400 else "その他"))
        print("    VID / PID        : %04X / %04X" % (p.vendor_id, p.product_id))
        print("    bcdDevice        : 0x%04X  (FW %s)" % (p.bcd_device, p.firmware_version))
        print("    Manufacturer     : %s" % (p.manufacturer or "-"))
        print("    Product          : %s" % (p.product or "-"))
        print("    Serial           : %s" % (p.serial or "-"))
        print("    現在サンプルレート: %s Hz" % (snap.sample_rate if snap.sample_rate else "-"))
        print("    USB モード       : %s" % (snap.usb_mode if snap.usb_mode is not None else "-"))
        print("    Config Descriptor: %d B" % len(snap.config_descriptor))
        if p.is_do400:
            found = snap

    try:
        status = api.get_dfu_status()
        print("")
        print("  DFU 状態 (読み取りのみ): %s  %d/%d  completion=0x%08X (%s)" % (
            status.state_name, status.current, status.total,
            status.completion, T.status_name(status.completion)))
    except Exception as exc:
        print("  DFU 状態の取得に失敗: %s" % exc)

    if found is None:
        print("\n 152A:85DD (DO400) は見つかりませんでした。")
        return 1
    print("")
    print(" 結果: DO400 を確認 (%s, bcdDevice=0x%04X, %s Hz)" % (
        found.properties.vid_pid, found.properties.bcd_device, found.sample_rate))
    print(" ※ このコマンドは読み取りのみ。書込み・DFU 遷移は行っていません。")
    return 0


def cmd_validate(args) -> int:
    r = V.validate_file(args.validate)
    print(V.format_report(r))
    return 0 if r.write_allowed else 1


def cmd_dry_run_flash(args) -> int:
    path = args.dry_run_flash
    print("=" * 68)
    print(" DFU 書込シーケンス ドライラン (実機には一切アクセスしません)")
    print("=" * 68)
    r = V.validate_file(path)
    print(V.format_report(r, verbose=False))
    print()
    if not r.write_allowed:
        print("[DRY-RUN] 事前検証 NG のため書込シーケンスを実行しません。")
        return 1
    if r.requires_bootstrap:
        print("[DRY-RUN] 注意: このイメージは 200 KiB 超。先に 512 KiB ブートストラップ"
              " (do400_323_dfu512k_bootstrap.bin) の導入が必要です。")
        print()

    backend = T.MockBackend(dfu_steps=10)
    api = T.ThesyconApi(backend=backend, allow_write=True)
    seq = []

    print(" ロード方式: TUSBAUDIO_LoadFirmwareImageFromBuffer "
          "(検証済みバイト列をそのまま渡す / TOCTOU なし)")
    print()

    def on_progress(s: T.DfuStatus):
        line = "   progress: %-14s %3.0f%%  (%d / %d)" % (
            s.state_name, s.percent, s.current, s.total)
        if not seq or seq[-1] != line:
            seq.append(line)
            print(line)

    try:
        result = perform_flash(api, path, device_index=0, on_progress=on_progress,
                               timeout=5.0, poll_interval=0.0)
    except Exception as exc:
        print("[DRY-RUN] 失敗: %s" % exc)
        return 1

    print()
    print(" 呼び出しシーケンス:")
    for i, call in enumerate(backend.calls, 1):
        name = call[0]
        args_repr = ", ".join(str(a) for a in call[1:])
        label = DLL_API_LABELS.get(name)
        print("   %2d. %s(%s)%s" % (i, name, args_repr,
                                    ("  [%s]" % label) if label else ""))
    print()
    ok = result.ok and backend.ended == 1 and backend.unloaded
    print(" 結果: %s  (state=%s, %d ステップ, EndDfuProc %d 回, Unload %s)" % (
        "OK" if ok else "NG", result.state_name, result.steps, backend.ended,
        "済" if backend.unloaded else "未"))
    print(" ※ モックバックエンドのみ。実機の書込み・DFU 遷移は行っていません。")
    return 0 if ok else 1


# ==========================================================================
# GUI ガイダンス / 状態判定 (GUI 非依存の純ロジック)
# ==========================================================================
def find_recommended_image() -> str | None:
    """同梱の推奨 PEQ DFU イメージが存在すればその絶対パスを返す。"""
    return RECOMMENDED_FW_IMAGE if os.path.isfile(RECOMMENDED_FW_IMAGE) else None


def find_webhid_ui() -> str | None:
    """WebHID PEQ 設定画面 HTML が存在すればその絶対パスを返す。"""
    return RECOMMENDED_WEBHID_HTML if os.path.isfile(RECOMMENDED_WEBHID_HTML) else None


def is_read_protected_upload(data: bytes) -> bool:
    """XMOS / SMSL 純正 FW の DFU_UPLOAD 読出し保護 (256 B 全ゼロ) かを判定する。"""
    return len(data) == 256 and all(b == 0 for b in data)


UI_STRINGS = {
    "ja": {
        "win_title": "DO400 DFU 更新ツール (XMOS / Thesycon API)",
        "head_title": "DO400 DFU 更新ツール",
        "head_sub": "STEP 1 → STEP 2 → STEP 3 の順に進めてください（完了後は自動再起動します）",
        "btn_lang": "EN",
        "btn_webhid": "PEQ 設定画面を開く",
        "steps": ("1. 接続確認", "2. イメージ選択", "3. 更新実行"),
        "sec_step1": " STEP 1. DO400 接続確認 ",
        "sec_step2": " STEP 2. ファームウェア選択 & 自動検証 ",
        "sec_step3": " STEP 3. ファームウェア更新を実行 ",
        "sec_maint": " 緊急・保守操作（通常のファームウェア更新では不要） ",
        "sec_log": " 実行ログ ",
        "btn_refresh": "再読み込み",
        "btn_quick_fw": "PEQ ファームウェアを選択 (推奨)",
        "btn_browse": "別のファイルを参照...",
        "unselected": "(未選択)",
        "verdict_none": "未選択",
        "val_prompt": "上のボタンから書き込む DFU イメージ (.bin) を選択してください。",
        "bs_warn": "⚠ このイメージは 200 KiB 超のため事前ブートストラップが必要です（同梱 196 KiB イメージなら不要）",
        "bs_chk": "[手動確認] 先に 512 KiB 上限入りファームウェアを書込み、再起動を完了しました",
        "btn_flash": "ファームウェア更新を実行",
        "btn_cancel": "中断",
        "flash_hint": "※ 書込み前に確認画面が 2 回出ます。完了後、DO400 は自動で再起動します（約 30 秒）。",
        "idle": "待機中",
        "btn_revert": "緊急 Factory 復帰",
        "hint_revert": "カスタム FW を消去し工場出荷ファームウェアへ戻す",
        "btn_backup": "現行イメージ吸出し (保守用)",
        "hint_backup": "※ 純正 FW 3.23 は保護のため 256 B ゼロを返します",
        "dev_checking": "確認中...",
        "dev_connected": "接続済み",
        "dev_disconnected": "未接続",
        "verdict_checking": "検証中...",
        "dev_err_title": "ドライバ API に接続できません",
        "dev_err_hint": "SMSL / XMOS USB Audio ドライバのインストールを確認してください。",
        "dev_not_found": "DO400 (152A:85DD) が見つかりません",
        "dev_not_found_sub": "検出デバイス数: {count}（電源と USB 接続を確認して「再読み込み」を押してください）",
        "dev_not_found_hint": "※ 音楽再生アプリ・ブラウザ (WebHID) は閉じた状態で更新してください。",
        "dev_extra_fmt": "サンプルレート {sr} Hz   USB モード {mode}   ドライバ API {api}",
        "val_direct": "そのまま更新 OK",
        "val_bootstrap": "200 KiB 超（要ブートストラップ）",
        "val_crc_line": "Image CRC 0x{crc:08X} / このファイル内の DFU 上限 {limit}",
        "val_fatal_hdr": "致命的な問題:",
        "val_bs_hdr": "** 2 段階 DFU 手順が必要 **",
        "val_bs_1": "  1) 先に 512 KiB 上限入りファームウェアを書き込む",
        "val_bs_2": "  2) 再起動後、STEP 3 の確認チェックを入れてこのイメージ ({size}) を書き込む",
        "op_flash": "ファームウェア更新",
        "op_backup": "現行イメージ吸出し",
        "op_revert": "Factory 復帰",
        "log_startup": "起動しました。STEP 1 → STEP 2 → STEP 3 の順に進めてください（書込みは未実行）。",
        "log_webhid_bat": "PEQ 設定画面 (localhost サーバー) を起動しました: {path}",
        "log_webhid_url": "PEQ 設定画面をブラウザで開きました: {path}",
        "log_dev_fail": "デバイス読み取り失敗: {err}",
        "log_dev_ok": "デバイス確認: {vid_pid} / bcdDevice 0x{bcd:04X} / {sr} Hz",
        "log_img_selected": "イメージ選択: {path}",
        "log_val_bootstrap": "注意: 200 KiB 超のイメージです。先に 512 KiB ブートストラップを書込み、再起動後に STEP 3 の確認チェックを入れてください。",
        "log_val_ok": "検証 OK: STEP 3 の「ファームウェア更新を実行」からそのまま書き込めます。",
        "log_op_start": "=== {label} 開始 ===",
        "log_op_cancelled": "=== {label} 中断 === {text}",
        "log_op_done": "=== {label} 完了 ===",
        "log_op_failed": "!!! {label} 失敗: {err}",
        "log_auto_reboot": "デバイスは自動再起動します。数秒後に自動で再読み込みします。",
        "log_cancel_req": "中断を要求しました (EndDfuProc で後始末します)。",
        "log_cancel_locked": "転送中のため中断できません（完了まで待機してください）。",
        "log_backup_cancel": "吸出しをキャンセルしました。",
        "log_flash_cancel": "更新をキャンセルしました。",
        "log_revert_cancel": "Factory 復帰をキャンセルしました。",
        "prog_start": "{label}: 開始",
        "prog_cancelled": "{label}: 中断",
        "prog_done": "{label}: 完了",
        "prog_failed": "{label}: 失敗",
        "dlg_cancel": "キャンセル",
        "dlg_exec": "実行する",
        "dlg_chk_title": "確認",
        "dlg_chk_req": "チェックボックスにチェックしてください。",
        "dlg_info": "案内",
        "dlg_no_webhid": "PEQ 設定画面ファイルが見つかりません。",
        "dlg_no_rec_title": "ファイルなし",
        "dlg_no_rec_msg": "同梱の PEQ ファームウェアが見つかりませんでした。\n「別のファイルを参照...」から選択してください。",
        "dlg_browse_title": "DFU イメージ (.bin) を選択",
        "dlg_ft_bin": "XMOS アップグレードイメージ",
        "dlg_ft_all": "すべてのファイル",
        "dlg_done_title": "完了",
        "dlg_done_body": "{label} が完了しました。\n\n{text}",
        "dlg_cancelled_title": "中断",
        "dlg_failed_title": "失敗",
        "dlg_failed_body": "{label} が失敗しました。\n\n{err}",
        "dlg_unconn_title": "未接続",
        "dlg_unconn_msg": "DO400 が接続されていません。",
        "dlg_val_ng_title": "検証 NG",
        "dlg_val_ng_msg": "イメージが検証に合格していません。",
        "dlg_bs_title": "ブートストラップ未確認",
        "dlg_bs_msg": "このイメージは 200 KiB を超えています。\n先に 512 KiB ブートストラップを導入し、チェックボックスを入れてください。",
        "dlg_close_locked_title": "転送中",
        "dlg_close_locked_msg": "転送中のため中断・終了できません。\n完了まで待機してください。",
        "dlg_close_busy_title": "実行中",
        "dlg_close_busy_msg": "DFU 操作が実行中です。中断して終了しますか？",
    },
    "en": {
        "win_title": "DO400 DFU Updater (XMOS / Thesycon API)",
        "head_title": "DO400 DFU Updater",
        "head_sub": "Follow STEP 1 → STEP 2 → STEP 3 (DO400 reboots automatically when finished)",
        "btn_lang": "日本語",
        "btn_webhid": "Open PEQ Console",
        "steps": ("1. Connect", "2. Select Image", "3. Flash"),
        "sec_step1": " STEP 1. Connect DO400 ",
        "sec_step2": " STEP 2. Select Firmware & Auto-Verify ",
        "sec_step3": " STEP 3. Flash Firmware ",
        "sec_maint": " Emergency / Diagnostic (Not needed for normal updates) ",
        "sec_log": " Log ",
        "btn_refresh": "Refresh",
        "btn_quick_fw": "Select PEQ Firmware (Recommended)",
        "btn_browse": "Browse...",
        "unselected": "(Not selected)",
        "verdict_none": "None",
        "val_prompt": "Click a button above to select a DFU firmware image (.bin).",
        "bs_warn": "⚠ This image exceeds 200 KiB and requires a 512 KiB-enabled FW first (not needed for the bundled 196 KiB image)",
        "bs_chk": "[Manual Confirm] I have already flashed a 512 KiB DFU-enabled firmware and rebooted the device",
        "btn_flash": "Flash Firmware",
        "btn_cancel": "Cancel",
        "flash_hint": "Two confirmation prompts will appear. DO400 automatically reboots after flashing (~30 s).",
        "idle": "Idle",
        "btn_revert": "Emergency Revert to Factory",
        "hint_revert": "Erase custom FW and boot stock factory firmware",
        "btn_backup": "Upload Current Image (Diag)",
        "hint_backup": "Note: Stock FW 3.23 returns 256 zero bytes (read-protected)",
        "dev_checking": "Checking...",
        "dev_connected": "Connected",
        "dev_disconnected": "Not Found",
        "verdict_checking": "Verifying...",
        "dev_err_title": "Cannot connect to driver API",
        "dev_err_hint": "Verify that the SMSL / XMOS USB Audio driver is installed.",
        "dev_not_found": "DO400 (152A:85DD) not found",
        "dev_not_found_sub": "Detected devices: {count} (check power and USB cable, then click Refresh)",
        "dev_not_found_hint": "Note: Close audio players and browser WebHID tabs before updating.",
        "dev_extra_fmt": "Sample Rate {sr} Hz   USB Mode {mode}   Driver API {api}",
        "val_direct": "Direct Flash OK",
        "val_bootstrap": "> 200 KiB (Bootstrap required)",
        "val_crc_line": "Image CRC 0x{crc:08X} / In-file DFU limit {limit}",
        "val_fatal_hdr": "Fatal issues:",
        "val_bs_hdr": "** 2-step DFU required **",
        "val_bs_1": "  1) Flash a 512 KiB DFU-enabled firmware first",
        "val_bs_2": "  2) After reboot, check the confirmation box in STEP 3 and flash this image ({size})",
        "op_flash": "Firmware Update",
        "op_backup": "Upload Current Image",
        "op_revert": "Revert to Factory",
        "log_startup": "Started. Follow STEP 1 → STEP 2 → STEP 3 (no write executed yet).",
        "log_webhid_bat": "Launched PEQ Console (localhost server): {path}",
        "log_webhid_url": "Opened PEQ Console in browser: {path}",
        "log_dev_fail": "Device read failed: {err}",
        "log_dev_ok": "Device detected: {vid_pid} / bcdDevice 0x{bcd:04X} / {sr} Hz",
        "log_img_selected": "Image selected: {path}",
        "log_val_bootstrap": "Note: Image exceeds 200 KiB. Flash a 512 KiB-enabled FW first, reboot, then check the box in STEP 3.",
        "log_val_ok": "Verification OK: Ready to flash via 'Flash Firmware' in STEP 3.",
        "log_op_start": "=== {label} Started ===",
        "log_op_cancelled": "=== {label} Cancelled === {text}",
        "log_op_done": "=== {label} Completed ===",
        "log_op_failed": "!!! {label} Failed: {err}",
        "log_auto_reboot": "Device is rebooting automatically. Refreshing in a few seconds...",
        "log_cancel_req": "Cancel requested (cleaning up via EndDfuProc).",
        "log_cancel_locked": "Transfer in progress — cannot cancel (please wait until complete).",
        "log_backup_cancel": "Image upload cancelled.",
        "log_flash_cancel": "Firmware update cancelled.",
        "log_revert_cancel": "Factory revert cancelled.",
        "prog_start": "{label}: Starting",
        "prog_cancelled": "{label}: Cancelled",
        "prog_done": "{label}: Done",
        "prog_failed": "{label}: Failed",
        "dlg_cancel": "Cancel",
        "dlg_exec": "Proceed",
        "dlg_chk_title": "Confirmation",
        "dlg_chk_req": "Please check the confirmation checkbox first.",
        "dlg_info": "Notice",
        "dlg_no_webhid": "PEQ Console HTML file not found.",
        "dlg_no_rec_title": "File Not Found",
        "dlg_no_rec_msg": "Bundled PEQ firmware was not found.\nPlease choose a file via 'Browse...'.",
        "dlg_browse_title": "Select DFU Image (.bin)",
        "dlg_ft_bin": "XMOS Upgrade Image",
        "dlg_ft_all": "All Files",
        "dlg_done_title": "Completed",
        "dlg_done_body": "{label} completed.\n\n{text}",
        "dlg_cancelled_title": "Cancelled",
        "dlg_failed_title": "Failed",
        "dlg_failed_body": "{label} failed.\n\n{err}",
        "dlg_unconn_title": "Not Connected",
        "dlg_unconn_msg": "DO400 is not connected.",
        "dlg_val_ng_title": "Verification Failed",
        "dlg_val_ng_msg": "The selected image did not pass verification.",
        "dlg_bs_title": "Bootstrap Not Confirmed",
        "dlg_bs_msg": "This image exceeds 200 KiB.\nFlash a 512 KiB DFU-enabled image first and check the confirmation box.",
        "dlg_close_locked_title": "Transfer in Progress",
        "dlg_close_locked_msg": "Cannot cancel or exit while flashing is in progress.\nPlease wait until complete.",
        "dlg_close_busy_title": "Operation in Progress",
        "dlg_close_busy_msg": "A DFU operation is running. Cancel and exit?",
    },
}


def _fmt_size_class(r, lang: str = "ja") -> str:
    if lang != "en":
        return r.size_class_label
    return {
        V.SIZE_CLASS_DIRECT: "<= 200 KiB — Direct flash supported",
        V.SIZE_CLASS_BOOTSTRAP: "200–512 KiB — Requires 512 KiB DFU firmware first",
        V.SIZE_CLASS_TOO_LARGE: "> 512 KiB — Exceeds DFU limit",
        V.SIZE_CLASS_INVALID: "Invalid (verification failed)",
    }.get(r.size_class, r.size_class)


def _fmt_dfu_limit(r, lang: str = "ja") -> str:
    lbl = r.dfu_limit_label
    if lang == "en" and lbl == "不明":
        return "Unknown"
    return lbl


def _translate_raw_log(msg: str, lang: str) -> str:
    """Translate known low-level API log prefixes when viewing in English."""
    if lang != "en":
        return msg
    reps = (
        ("GetCurrentSampleRate 失敗:", "GetCurrentSampleRate failed:"),
        ("GetDeviceUsbMode 失敗:", "GetDeviceUsbMode failed:"),
        ("GetUsbConfigDescriptor 失敗:", "GetUsbConfigDescriptor failed:"),
        ("DFU 転送中のため中断要求を抑止しました", "Cancel request ignored during active DFU transfer"),
        ("DFU 中断要求を受け取りました", "DFU cancel request received"),
    )
    out = msg
    for ja, en in reps:
        if ja in out:
            out = out.replace(ja, en)
    return out


def compute_guidance(connected: bool, validation, bootstrap_checked: bool = False,
                     busy: bool = False, cancel_locked: bool = False,
                     last_flash_ok: bool = False, has_recommended: bool = False,
                     lang: str = "ja") -> dict:
    """現在の状態から 3 ステップ表示と「今やること」案内を組み立てる (ja / en)。"""
    en = (lang == "en")
    if busy and cancel_locked:
        return {
            "step_states": ("done", "done", "warn"),
            "tone": "warn",
            "badge": "FLASHING" if en else "書込み中",
            "headline": ("Flashing firmware — DO NOT power off, unplug USB, or cancel" if en
                         else "ファームウェア転送中 — 電源 OFF・USB 抜去・中断はしないでください"),
            "detail": ("Wait until the completion dialog appears and DO400 reboots automatically." if en
                       else "完了ダイアログが出て DO400 が自動再起動するまで、そのまま待機してください。"),
        }
    if busy:
        return {
            "step_states": ("done", "done", "active"),
            "tone": "info",
            "badge": "PREPARING" if en else "準備中",
            "headline": ("Switching device into DFU mode..." if en
                         else "DFU モードへ切り替え中です..."),
            "detail": ("Preparing device. Do not touch power or USB cable." if en
                       else "デバイスを準備しています。完了まで電源や USB ケーブルには触れないでください。"),
        }
    if last_flash_ok:
        return {
            "step_states": ("done" if connected else "active", "done", "done"),
            "tone": "ok",
            "badge": "DONE" if en else "完了",
            "headline": ("Update complete! DO400 is rebooting automatically" if en
                         else "更新完了！DO400 は自動で再起動します"),
            "detail": (
                ("DO400 reconnected. Click 'Open PEQ Console' at top right to configure PEQ." if en
                 else "DO400 の再接続を確認しました。必要なら右上の「PEQ 設定画面を開く」から調整できます。")
                if connected else
                ("Refreshing automatically in a few seconds. Click Refresh in STEP 1 if needed." if en
                 else "数秒後に自動で再読み込みします。再接続されない場合は STEP 1 の「再読み込み」を押してください。")
            ),
        }

    if not connected:
        if validation is None:
            s2 = "pending"
        elif validation.write_allowed:
            s2 = "done"
        else:
            s2 = "error"
        return {
            "step_states": ("active", s2, "pending"),
            "tone": "warn",
            "badge": "STEP 1",
            "headline": ("Connect DO400 via USB and click 'Refresh'" if en
                         else "DO400 を USB 接続して「再読み込み」を押してください"),
            "detail": ("Power on DO400 and connect USB. Close any audio players or browser WebHID tabs first." if en
                       else "DO400 の電源を入れ、PC と USB 接続します。音楽再生アプリやブラウザの WebHID は閉じてください。"),
        }

    if validation is None:
        return {
            "step_states": ("done", "active", "pending"),
            "tone": "info",
            "badge": "STEP 2",
            "headline": ("Select a firmware image (.bin) to flash" if en
                         else "書き込むファームウェア (.bin) を選択してください"),
            "detail": (
                ("Click 'Select PEQ Firmware (Recommended)' or 'Browse...' (flashes directly in one step)." if en
                 else "「PEQ ファームウェアを選択 (推奨)」または「別のファイルを参照...」を押してください（そのまま 1 回で更新できます）。")
                if has_recommended else
                ("Click 'Browse...' to choose a DFU image (.bin)." if en
                 else "「別のファイルを参照...」から書き込む DFU イメージ (.bin) を選択してください。")
            ),
        }

    if not validation.write_allowed:
        return {
            "step_states": ("done", "error", "pending"),
            "tone": "error",
            "badge": "STEP 2",
            "headline": ("Verification failed — please choose a valid .bin image" if en
                         else "選択したファイルは検証 NG です — 正しい .bin を選び直してください"),
            "detail": ("Corrupt, oversized, or non-DFU files cannot be flashed. See details below." if en
                       else "CRC 不一致・サイズ超過・形式違いのファイルは安全のため書き込めません。下の検証詳細を確認してください。"),
        }

    if validation.requires_bootstrap and not bootstrap_checked:
        return {
            "step_states": ("done", "done", "warn"),
            "tone": "warn",
            "badge": "STEP 3",
            "headline": ("Image exceeds 200 KiB — confirm 512 KiB DFU firmware is installed" if en
                         else "200 KiB 超のイメージです — 512 KiB ブートストラップ導入の確認が必要です"),
            "detail": ("Flash a 512 KiB-enabled image first and let DO400 reboot, then check the confirmation box in STEP 3." if en
                       else "先に 512 KiB 上限入りイメージを書込み、自動再起動した後に STEP 3 の確認チェックを入れてください。"),
        }

    return {
        "step_states": ("done", "done", "active"),
        "tone": "ok",
        "badge": "STEP 3",
        "headline": ("Ready! Click 'Flash Firmware' to start" if en
                     else "準備完了！「ファームウェア更新を実行」を押してください"),
        "detail": (
            ("Two confirmation prompts will appear; flashing and auto-reboot complete in ~30 s." if en
             else "確認が 2 回出たあと、自動再起動まで約 30 秒で完了します（そのまま 1 回で更新完了）。")
            if not validation.requires_bootstrap else
            ("Confirmed. Do not power off or unplug USB until flashing and auto-reboot finish." if en
             else "確認済み。確認が 2 回出たあと、自動再起動まで電源 OFF・USB 抜去をしないでください。")
        ),
    }


# ==========================================================================
# GUI
# ==========================================================================
if _TK_AVAILABLE:

    class DfuApp(tk.Tk):
        POLL_MS = 120

        _TONE_COLORS = {
            "info":  ("#e7f1ff", "#084298", "#0d6efd", "#ffffff"),
            "ok":    ("#d1e7dd", "#0f5132", "#198754", "#ffffff"),
            "warn":  ("#fff3cd", "#664d03", "#cc8800", "#ffffff"),
            "error": ("#f8d7da", "#842029", "#dc3545", "#ffffff"),
        }
        _STEP_COLORS = {
            "done":    ("#d1e7dd", "#0f5132"),
            "active":  ("#cfe2ff", "#084298"),
            "pending": ("#e9ecef", "#6c757d"),
            "warn":    ("#fff3cd", "#664d03"),
            "error":   ("#f8d7da", "#842029"),
        }

        def __init__(self, api=None, auto_refresh: bool = True, lang: str = "ja"):
            super().__init__()
            self.lang = "en" if lang == "en" else "ja"
            self.title("%s v%s" % (UI_STRINGS[self.lang]["win_title"], APP_VERSION))
            self.geometry("1080x920")
            self.minsize(920, 760)
            self.configure(bg="#f4f5f7")

            self.api = api if api is not None else T.ThesyconApi(
                logger=lambda m: self.log("  " + m))
            self.snapshot = None
            self.validation = None
            self.selected_path = None
            self.busy = False
            self.last_flash_ok = False
            self._log_entries = []
            self._dev_error = None
            self._dev_count = 0
            self.cancel_event = threading.Event()
            self.cancel_guard = TransferCancelGuard()
            self.cancel_locked = False
            self.queue = queue.Queue()
            self._default_font = ("Yu Gothic UI", 10)
            self._recommended_image = find_recommended_image()
            self._webhid_html = find_webhid_ui()

            self._build_style()
            self._build_ui()
            self._apply_language()
            self.after(self.POLL_MS, self._drain_queue)
            if auto_refresh:
                self.after(80, self.refresh_device)

        # -- 見た目 ---------------------------------------------------------
        def _build_style(self):
            style = ttk.Style(self)
            try:
                style.theme_use("vista")
            except Exception:
                pass
            style.configure(".", font=self._default_font)
            style.configure("TLabelframe.Label", font=("Yu Gothic UI", 10, "bold"))
            style.configure("Head.TLabel", font=("Yu Gothic UI", 14, "bold"))
            style.configure("Sub.TLabel", foreground="#555555")
            style.configure("Hint.TLabel", foreground="#555555", font=("Yu Gothic UI", 9))
            style.configure("Big.TButton", font=("Yu Gothic UI", 10, "bold"), padding=(14, 9))
            style.configure("Quick.TButton", font=("Yu Gothic UI", 9, "bold"), padding=(10, 5))
            style.configure("Danger.TButton", font=("Yu Gothic UI", 9, "bold"), padding=(10, 6))

        def _build_ui(self):
            root = ttk.Frame(self, padding=(12, 8, 12, 10))
            root.pack(fill="both", expand=True)
            root.columnconfigure(0, weight=1)

            # ヘッダ
            head = ttk.Frame(root)
            head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
            head.columnconfigure(0, weight=1)
            self.head_title_lbl = ttk.Label(head, text="DO400 DFU 更新ツール", style="Head.TLabel")
            self.head_title_lbl.grid(row=0, column=0, sticky="w")
            self.head_sub_lbl = ttk.Label(
                head, text="STEP 1 → STEP 2 → STEP 3 の順に進めてください（完了後は自動再起動します）",
                style="Sub.TLabel")
            self.head_sub_lbl.grid(row=1, column=0, sticky="w")

            head_right = ttk.Frame(head)
            head_right.grid(row=0, column=1, rowspan=2, sticky="e")
            self.api_label = ttk.Label(head_right, text="API: -", style="Sub.TLabel")
            self.api_label.pack(side="right", padx=(10, 0))
            self.btn_lang = ttk.Button(head_right, text="EN", width=7, command=self.toggle_language)
            self.btn_lang.pack(side="right", padx=(8, 0))
            self.btn_webhid = None
            if self._webhid_html:
                self.btn_webhid = ttk.Button(
                    head_right, text="PEQ 設定画面を開く", command=self.open_webhid_ui)
                self.btn_webhid.pack(side="right")

            # ステップバー + 今やることガイドバナー
            guide_wrap = tk.Frame(root, bg="#e7f1ff", highlightthickness=1,
                                  highlightbackground="#b6d4fe")
            guide_wrap.grid(row=1, column=0, sticky="ew", pady=(0, 8))
            guide_wrap.columnconfigure(0, weight=1)
            self.guide_wrap = guide_wrap

            stepper = tk.Frame(guide_wrap, bg="#e7f1ff")
            stepper.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
            self.stepper_frame = stepper
            self.step_labels = []
            self._step_arrows = []
            step_titles = ("1. 接続確認", "2. イメージ選択", "3. 更新実行")
            for idx, title in enumerate(step_titles):
                if idx > 0:
                    arr = tk.Label(stepper, text=" → ", bg="#e7f1ff", fg="#6c757d",
                                   font=("Yu Gothic UI", 10, "bold"))
                    arr.pack(side="left")
                    self._step_arrows.append(arr)
                lbl = tk.Label(stepper, text=title, bg="#e9ecef", fg="#6c757d",
                               font=("Yu Gothic UI", 9, "bold"), padx=10, pady=3)
                lbl.pack(side="left")
                self.step_labels.append(lbl)

            banner = tk.Frame(guide_wrap, bg="#e7f1ff")
            banner.grid(row=1, column=0, sticky="ew", padx=10, pady=(2, 8))
            banner.columnconfigure(1, weight=1)
            self.banner_frame = banner

            self.guide_badge = tk.Label(banner, text="STEP 1", bg="#0d6efd", fg="#ffffff",
                                        font=("Yu Gothic UI", 9, "bold"), padx=8, pady=3)
            self.guide_badge.grid(row=0, column=0, rowspan=2, sticky="nw", padx=(0, 10), pady=2)
            self.guide_headline = tk.Label(banner, text="デバイスを確認しています...",
                                           bg="#e7f1ff", fg="#084298",
                                           font=("Yu Gothic UI", 11, "bold"), anchor="w", justify="left")
            self.guide_headline.grid(row=0, column=1, sticky="ew")
            self.guide_detail = tk.Label(banner, text="", bg="#e7f1ff", fg="#333333",
                                         font=("Yu Gothic UI", 9), anchor="w", justify="left")
            self.guide_detail.grid(row=1, column=1, sticky="ew", pady=(1, 0))

            # STEP 1: デバイス接続確認
            self.sec_step1 = ttk.LabelFrame(root, text=" STEP 1. DO400 接続確認 ", padding=8)
            dev = self.sec_step1
            dev.grid(row=2, column=0, sticky="ew")
            dev.columnconfigure(1, weight=1)

            self.dev_status = tk.Label(dev, text="確認中...", bg="#e9ecef", fg="#333333",
                                       font=("Yu Gothic UI", 10, "bold"), padx=10, pady=4, width=10)
            self.dev_status.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 12))

            self.dev_main = ttk.Label(dev, text="-", font=("Yu Gothic UI", 10, "bold"))
            self.dev_main.grid(row=0, column=1, sticky="w")
            self.dev_sub = ttk.Label(dev, text="-", style="Sub.TLabel")
            self.dev_sub.grid(row=1, column=1, sticky="w", pady=(1, 0))
            self.dev_extra = ttk.Label(dev, text="-", style="Hint.TLabel")
            self.dev_extra.grid(row=2, column=1, sticky="w", pady=(1, 0))

            self.btn_refresh = ttk.Button(dev, text="再読み込み", command=self.refresh_device)
            self.btn_refresh.grid(row=0, column=2, rowspan=3, sticky="e")

            # STEP 2: イメージ選択 & 自動検証
            self.sec_step2 = ttk.LabelFrame(root, text=" STEP 2. ファームウェア選択 & 自動検証 ", padding=8)
            img = self.sec_step2
            img.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            img.columnconfigure(1, weight=1)

            pick_row = ttk.Frame(img)
            pick_row.grid(row=0, column=0, columnspan=2, sticky="ew")
            pick_row.columnconfigure(2, weight=1)

            self.btn_quick_fw = None
            col = 0
            if self._recommended_image:
                self.btn_quick_fw = ttk.Button(
                    pick_row, text="PEQ ファームウェアを選択 (推奨)",
                    style="Quick.TButton", command=self.select_recommended_fw)
                self.btn_quick_fw.grid(row=0, column=col, sticky="w", padx=(0, 8))
                col += 1

            self.btn_browse = ttk.Button(pick_row, text="別のファイルを参照...",
                                         command=self.choose_file)
            self.btn_browse.grid(row=0, column=col, sticky="w")
            col += 1

            self.path_var = tk.StringVar(value="(未選択)")
            ttk.Label(pick_row, textvariable=self.path_var, style="Sub.TLabel").grid(
                row=0, column=col, sticky="w", padx=(10, 0))

            self.verdict_label = tk.Label(img, text="未選択", bg="#e9ecef", fg="#555555",
                                          font=("Yu Gothic UI", 11, "bold"), padx=10, pady=4, width=10)
            self.verdict_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

            self.val_summary = ttk.Label(
                img, text="上のボタンから書き込む DFU イメージ (.bin) を選択してください。",
                font=("Yu Gothic UI", 10))
            self.val_summary.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(6, 0))

            crc_box = ttk.Frame(img)
            crc_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
            crc_box.columnconfigure(0, weight=1)
            self.crc_text = tk.Text(crc_box, height=5, width=10, wrap="none",
                                    font=("Consolas", 9), relief="solid", borderwidth=1,
                                    bg="#ffffff")
            self.crc_text.grid(row=0, column=0, sticky="ew")
            crc_sb = ttk.Scrollbar(crc_box, orient="vertical", command=self.crc_text.yview)
            crc_sb.grid(row=0, column=1, sticky="ns")
            self.crc_text.configure(yscrollcommand=crc_sb.set, state="disabled")

            # STEP 3: 更新実行
            self.sec_step3 = ttk.LabelFrame(root, text=" STEP 3. ファームウェア更新を実行 ", padding=8)
            ops = self.sec_step3
            ops.grid(row=4, column=0, sticky="ew", pady=(8, 0))
            ops.columnconfigure(0, weight=1)

            # 200 KiB 超イメージを選んだときだけ表示する確認枠
            self.bootstrap_panel = tk.Frame(ops, bg="#fff3cd", highlightthickness=1,
                                            highlightbackground="#ffda6a", padx=8, pady=6)
            self.bootstrap_panel.columnconfigure(0, weight=1)
            self.bs_warn_lbl = tk.Label(
                self.bootstrap_panel,
                text="⚠ このイメージは 200 KiB 超のため事前ブートストラップが必要です（同梱 196 KiB イメージなら不要）",
                bg="#fff3cd", fg="#664d03", font=("Yu Gothic UI", 9, "bold"), anchor="w")
            self.bs_warn_lbl.grid(row=0, column=0, sticky="w")
            self.bootstrap_var = tk.BooleanVar(value=False)
            self.chk_bootstrap = ttk.Checkbutton(
                self.bootstrap_panel,
                text="[手動確認] 先に 512 KiB 上限入りファームウェアを書込み、再起動を完了しました",
                variable=self.bootstrap_var, command=self._update_buttons,
                state="disabled")
            self.chk_bootstrap.grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.bootstrap_panel.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            self.bootstrap_panel.grid_remove()

            btns = ttk.Frame(ops)
            btns.grid(row=1, column=0, sticky="ew")
            self.btn_flash = ttk.Button(btns, text="ファームウェア更新を実行",
                                        style="Big.TButton", command=self.do_flash)
            self.btn_flash.grid(row=0, column=0, padx=(0, 8))
            self.btn_cancel = ttk.Button(btns, text="中断", command=self.do_cancel, state="disabled")
            self.btn_cancel.grid(row=0, column=1, padx=(0, 12))
            self.flash_hint = ttk.Label(
                btns,
                text="※ 書込み前に確認画面が 2 回出ます。完了後、DO400 は自動で再起動します（約 30 秒）。",
                style="Hint.TLabel")
            self.flash_hint.grid(row=0, column=2, sticky="w")

            prog = ttk.Frame(ops)
            prog.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            prog.columnconfigure(0, weight=1)
            self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
            self.progress.grid(row=0, column=0, sticky="ew")
            self.progress_text = ttk.Label(prog, text="待機中", width=40, style="Sub.TLabel")
            self.progress_text.grid(row=0, column=1, sticky="e", padx=(10, 0))

            # 緊急・保守操作 (通常更新の動線から分離)
            self.sec_maint = ttk.LabelFrame(root, text=" 緊急・保守操作（通常のファームウェア更新では不要） ", padding=8)
            maint = self.sec_maint
            maint.grid(row=5, column=0, sticky="ew", pady=(8, 0))
            maint.columnconfigure(1, weight=1)
            maint.columnconfigure(3, weight=1)

            self.btn_revert = ttk.Button(maint, text="緊急 Factory 復帰",
                                         style="Danger.TButton", command=self.do_revert)
            self.btn_revert.grid(row=0, column=0, sticky="w")
            self.hint_revert_lbl = ttk.Label(maint, text="カスタム FW を消去し工場出荷ファームウェアへ戻す",
                                             style="Hint.TLabel")
            self.hint_revert_lbl.grid(row=0, column=1, sticky="w", padx=(8, 16))

            self.btn_backup = ttk.Button(maint, text="現行イメージ吸出し (保守用)",
                                         command=self.do_backup)
            self.btn_backup.grid(row=0, column=2, sticky="w")
            self.hint_backup_lbl = ttk.Label(maint, text="※ 純正 FW 3.23 は保護のため 256 B ゼロを返します",
                                             style="Hint.TLabel")
            self.hint_backup_lbl.grid(row=0, column=3, sticky="w", padx=(8, 0))

            # ログ
            self.sec_log = ttk.LabelFrame(root, text=" 実行ログ ", padding=6)
            logf = self.sec_log
            logf.grid(row=6, column=0, sticky="nsew", pady=(8, 0))
            root.rowconfigure(6, weight=1)
            logf.columnconfigure(0, weight=1)
            logf.rowconfigure(0, weight=1)
            self.log_text = tk.Text(logf, height=9, wrap="word", font=("Consolas", 9),
                                    bg="#111418", fg="#d6dde6", insertbackground="#ffffff",
                                    relief="flat")
            self.log_text.grid(row=0, column=0, sticky="nsew")
            sb = ttk.Scrollbar(logf, orient="vertical", command=self.log_text.yview)
            sb.grid(row=0, column=1, sticky="ns")
            self.log_text.configure(yscrollcommand=sb.set, state="disabled")
            self.log_text.tag_configure("err", foreground="#ff6b6b")

            self._update_buttons()
            self.log_i18n("log_startup")

        # -- 翻訳 & ログ ----------------------------------------------------
        def _tr(self, key: str, **kwargs) -> str:
            tmpl = UI_STRINGS[self.lang].get(key, UI_STRINGS["ja"].get(key, key))
            return tmpl.format(**kwargs) if kwargs else tmpl

        def _op_label(self, op_key: str) -> str:
            return self._tr("op_" + op_key) if ("op_" + op_key) in UI_STRINGS[self.lang] else op_key

        def _format_log_entry(self, entry) -> str:
            stamp, kind, key_or_msg, kwargs, _is_err = entry
            if kind == "i18n":
                fmt_kw = dict(kwargs)
                if "op_key" in fmt_kw:
                    fmt_kw["label"] = self._op_label(fmt_kw.pop("op_key"))
                text = self._tr(key_or_msg, **fmt_kw)
            else:
                text = _translate_raw_log(key_or_msg, self.lang)
            return "[%s] %s\n" % (stamp, text)

        def _append_log_entry(self, entry):
            self._log_entries.append(entry)
            is_err = entry[4]
            self.log_text.configure(state="normal")
            if is_err:
                self.log_text.insert("end", self._format_log_entry(entry), "err")
            else:
                self.log_text.insert("end", self._format_log_entry(entry))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def _rerender_logs(self):
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            for entry in self._log_entries:
                if entry[4]:
                    self.log_text.insert("end", self._format_log_entry(entry), "err")
                else:
                    self.log_text.insert("end", self._format_log_entry(entry))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

        def log_i18n(self, key: str, is_err: bool = False, **kwargs):
            stamp = time.strftime("%H:%M:%S")
            self._append_log_entry((stamp, "i18n", key, kwargs, bool(is_err)))

        def log(self, msg: str):
            stamp = time.strftime("%H:%M:%S")
            self._append_log_entry((stamp, "raw", msg, {}, False))

        def log_error(self, msg: str):
            """失敗行を赤字で記録する (F-4: 完了コード異常もここに出る)。"""
            stamp = time.strftime("%H:%M:%S")
            self._append_log_entry((stamp, "raw", msg, {}, True))

        def _qlog(self, msg: str):
            self.queue.put(("log", msg))

        def open_webhid_ui(self):
            if self.busy:
                return
            if not self._webhid_html or not os.path.isfile(self._webhid_html):
                messagebox.showinfo(self._tr("dlg_info"), self._tr("dlg_no_webhid"))
                return
            bat = os.path.join(os.path.dirname(self._webhid_html), "open_peq_ui.bat")
            if os.name == "nt" and os.path.isfile(bat):
                os.startfile(bat)
                self.log_i18n("log_webhid_bat", path=bat)
                return
            url = "file:///" + self._webhid_html.replace("\\", "/")
            webbrowser.open(url)
            self.log_i18n("log_webhid_url", path=self._webhid_html)

        # -- キュー処理 -----------------------------------------------------
        def _drain_queue(self):
            try:
                while True:
                    item = self.queue.get_nowait()
                    kind = item[0]
                    if kind == "log":
                        self.log(item[1])
                    elif kind == "progress":
                        _, pct, text = item
                        if pct is None:
                            self.progress.configure(mode="indeterminate")
                            self.progress.start(60)
                        else:
                            if str(self.progress.cget("mode")) != "determinate":
                                self.progress.stop()
                                self.progress.configure(mode="determinate")
                            self.progress.configure(value=pct)
                        if self.cancel_locked:
                            text = cancel_lock_message(self.lang)
                        self.progress_text.configure(text=text)
                    elif kind == "cancel_lock":
                        self._lock_cancel()
                    elif kind == "done":
                        self._on_done(*item[1:])
            except queue.Empty:
                pass
            self.after(self.POLL_MS, self._drain_queue)

        # -- デバイス -------------------------------------------------------
        def _render_device_info(self):
            if self._dev_error is not None:
                self.dev_status.configure(text=self._tr("dev_disconnected"), bg="#f8d7da", fg="#842029")
                self.dev_main.configure(text=self._tr("dev_err_title"))
                self.dev_sub.configure(text=self._dev_error)
                self.dev_extra.configure(text=self._tr("dev_err_hint"))
            elif self.snapshot is None:
                self.dev_status.configure(text=self._tr("dev_disconnected"), bg="#f8d7da", fg="#842029")
                self.dev_main.configure(text=self._tr("dev_not_found"))
                self.dev_sub.configure(text=self._tr("dev_not_found_sub", count=self._dev_count))
                self.dev_extra.configure(text=self._tr("dev_not_found_hint"))
            else:
                do400 = self.snapshot
                p = do400.properties
                self.dev_status.configure(text=self._tr("dev_connected"), bg="#d1e7dd", fg="#0f5132")
                self.dev_main.configure(text="%s  (index %d)" % (p.product or "SMSL USB AUDIO",
                                                                 do400.index))
                self.dev_sub.configure(
                    text="VID:PID %s   bcdDevice 0x%04X (FW %s)" % (
                        p.vid_pid, p.bcd_device, p.firmware_version))
                self.dev_extra.configure(
                    text=self._tr(
                        "dev_extra_fmt",
                        sr=do400.sample_rate if do400.sample_rate else "-",
                        mode=do400.usb_mode if do400.usb_mode is not None else "-",
                        api=do400.api_version))

        def refresh_device(self):
            if self.busy:
                return
            self.dev_status.configure(text=self._tr("dev_checking"), bg="#e9ecef", fg="#333333")
            self.update_idletasks()
            try:
                snaps = self.api.scan_devices(with_descriptors=False)
                api_version = self.api.api_version()
            except Exception as exc:
                self.snapshot = None
                self._dev_error = str(exc)
                self._dev_count = 0
                self.api_label.configure(text="API: -")
                self._render_device_info()
                self.log_i18n("log_dev_fail", err=str(exc))
                self._update_buttons()
                return
            self._dev_error = None
            self._dev_count = len(snaps)
            self.api_label.configure(text="API: %s" % api_version)
            do400 = None
            for s in snaps:
                if s.properties.is_do400:
                    do400 = s
                    break
            self.snapshot = do400
            self._render_device_info()
            if do400 is not None:
                p = do400.properties
                self.log_i18n("log_dev_ok", vid_pid=p.vid_pid, bcd=p.bcd_device,
                              sr=do400.sample_rate)
            self._update_buttons()

        # -- イメージ -------------------------------------------------------
        def select_recommended_fw(self):
            if self.busy:
                return
            path = find_recommended_image()
            if not path:
                messagebox.showwarning(self._tr("dlg_no_rec_title"), self._tr("dlg_no_rec_msg"))
                return
            self._load_selected_file(path)

        def choose_file(self):
            if self.busy:
                return
            init_dir = None
            rec = find_recommended_image()
            if rec:
                init_dir = os.path.dirname(rec)
            elif os.path.isdir(_APP_DIR):
                init_dir = _APP_DIR
            path = filedialog.askopenfilename(
                title=self._tr("dlg_browse_title"),
                initialdir=init_dir,
                filetypes=[(self._tr("dlg_ft_bin"), "*.bin"), (self._tr("dlg_ft_all"), "*.*")])
            if not path:
                return
            self._load_selected_file(path)

        def _load_selected_file(self, path: str):
            self.last_flash_ok = False
            self.selected_path = path
            self.path_var.set(path)
            self.log_i18n("log_img_selected", path=path)
            self.validate_selected()

        def _render_validation_info(self):
            r = self.validation
            if r is None:
                return
            if r.ok and r.write_allowed:
                self.verdict_label.configure(text="ALL GOOD", bg="#d1e7dd", fg="#0f5132")
            else:
                self.verdict_label.configure(text="NG", bg="#f8d7da", fg="#842029")
            step_mode = self._tr("val_bootstrap") if r.requires_bootstrap else self._tr("val_direct")
            self.val_summary.configure(text="%s  —  %s\n%s" % (
                _fmt_bytes(r.file_size),
                step_mode if r.write_allowed else _fmt_size_class(r, self.lang),
                self._tr("val_crc_line", crc=(r.image_crc_calc or 0),
                         limit=_fmt_dfu_limit(r, self.lang))))

            lines = []
            lines.append("SHA-256 : %s" % (r.sha256 or "-"))
            lines.append("Page CRC: 0x%08X  %s" % (
                r.page_crc_calc or 0, "OK" if r.page_crc_calc == r.page_crc_stored else "NG"))
            lines.append("ImageCRC: 0x%08X  %s" % (
                r.image_crc_calc or 0, "OK" if r.image_crc_calc == r.image_crc_stored else "NG"))
            for s in r.sections:
                lines.append("%-8s: 0x%08X  %s  (0x%06X +0x%06X)" % (
                    s.kind, s.stored_crc or 0, "OK" if s.crc_ok else "NG", s.offset, s.size))
            if r.failures:
                lines.append("")
                lines.append(self._tr("val_fatal_hdr"))
                for c in r.failures:
                    lines.append("  NG  %s  — %s" % (c.name, c.detail))
            if r.requires_bootstrap:
                lines.append("")
                lines.append(self._tr("val_bs_hdr"))
                lines.append(self._tr("val_bs_1"))
                lines.append(self._tr("val_bs_2", size=_fmt_bytes(r.file_size)))
            if r.warnings:
                lines.append("")
                for c in r.warnings:
                    lines.append("  WARN %s  — %s" % (c.name, c.detail))
            self.crc_text.configure(state="normal")
            self.crc_text.delete("1.0", "end")
            self.crc_text.insert("1.0", "\n".join(lines))
            self.crc_text.configure(state="disabled")

        def validate_selected(self):
            if not self.selected_path:
                return
            self.verdict_label.configure(text=self._tr("verdict_checking"), bg="#e9ecef", fg="#333333")
            self.update_idletasks()
            r = V.validate_file(self.selected_path)
            self.validation = r
            self._render_validation_info()

            self.bootstrap_var.set(False)
            if r.requires_bootstrap:
                self.log_i18n("log_val_bootstrap")
            elif r.write_allowed:
                self.log_i18n("log_val_ok")
            self._update_buttons()

        # -- 言語切替 & ガイド & ボタン状態 ---------------------------------
        def toggle_language(self):
            self.lang = "en" if self.lang == "ja" else "ja"
            self._apply_language()

        def _apply_language(self):
            s = UI_STRINGS[self.lang]
            self.title("%s v%s" % (s["win_title"], APP_VERSION))
            self.head_title_lbl.configure(text=s["head_title"])
            self.head_sub_lbl.configure(text=s["head_sub"])
            self.btn_lang.configure(text=s["btn_lang"])
            if self.btn_webhid is not None:
                self.btn_webhid.configure(text=s["btn_webhid"])
            self.sec_step1.configure(text=s["sec_step1"])
            self.sec_step2.configure(text=s["sec_step2"])
            self.sec_step3.configure(text=s["sec_step3"])
            self.sec_maint.configure(text=s["sec_maint"])
            self.sec_log.configure(text=s["sec_log"])
            self.btn_refresh.configure(text=s["btn_refresh"])
            if self.btn_quick_fw is not None:
                self.btn_quick_fw.configure(text=s["btn_quick_fw"])
            self.btn_browse.configure(text=s["btn_browse"])
            self.bs_warn_lbl.configure(text=s["bs_warn"])
            self.chk_bootstrap.configure(text=s["bs_chk"])
            self.btn_flash.configure(text=s["btn_flash"])
            self.btn_cancel.configure(text=s["btn_cancel"])
            self.flash_hint.configure(text=s["flash_hint"])
            self.btn_revert.configure(text=s["btn_revert"])
            self.hint_revert_lbl.configure(text=s["hint_revert"])
            self.btn_backup.configure(text=s["btn_backup"])
            self.hint_backup_lbl.configure(text=s["hint_backup"])
            if not self.selected_path:
                self.path_var.set(s["unselected"])
                self.verdict_label.configure(text=s["verdict_none"])
                self.val_summary.configure(text=s["val_prompt"])
            else:
                self._render_validation_info()
            if not self.busy:
                self.progress_text.configure(text=s["idle"])
            elif self.cancel_locked:
                self.progress_text.configure(text=cancel_lock_message(self.lang))
            if self.snapshot is not None or self._dev_error is not None:
                self._render_device_info()
            else:
                self.dev_status.configure(text=s["dev_disconnected"])
            self._rerender_logs()
            self._update_guidance()

        def _update_guidance(self):
            g = compute_guidance(
                connected=self.snapshot is not None,
                validation=self.validation,
                bootstrap_checked=bool(self.bootstrap_var.get()),
                busy=self.busy,
                cancel_locked=self.cancel_locked,
                last_flash_ok=self.last_flash_ok,
                has_recommended=bool(self._recommended_image),
                lang=self.lang,
            )
            bg, fg, badge_bg, badge_fg = self._TONE_COLORS[g["tone"]]
            border = {
                "info": "#b6d4fe", "ok": "#a3cfbb", "warn": "#ffda6a", "error": "#f1aeb5"
            }[g["tone"]]
            self.guide_wrap.configure(bg=bg, highlightbackground=border)
            self.stepper_frame.configure(bg=bg)
            self.banner_frame.configure(bg=bg)
            for arr in self._step_arrows:
                arr.configure(bg=bg)
            self.guide_badge.configure(text=g["badge"], bg=badge_bg, fg=badge_fg)
            self.guide_headline.configure(text=g["headline"], bg=bg, fg=fg)
            self.guide_detail.configure(text=g["detail"], bg=bg, fg="#222222")

            base_titles = UI_STRINGS[self.lang]["steps"]
            for idx, (lbl, st) in enumerate(zip(self.step_labels, g["step_states"])):
                sbg, sfg = self._STEP_COLORS[st]
                prefix = "✓ " if st == "done" else ("▶ " if st == "active" else ("! " if st in ("warn", "error") else ""))
                lbl.configure(text=prefix + base_titles[idx], bg=sbg, fg=sfg)

        def _update_buttons(self):
            connected = self.snapshot is not None
            busy = self.busy
            ok = (self.validation is not None and self.validation.write_allowed)
            need_bs = bool(self.validation and self.validation.requires_bootstrap)
            self.btn_refresh.configure(state="disabled" if busy else "normal")
            self.btn_browse.configure(state="disabled" if busy else "normal")
            if self.btn_quick_fw is not None:
                self.btn_quick_fw.configure(state="disabled" if busy else "normal")
            if self.btn_webhid is not None:
                self.btn_webhid.configure(state="disabled" if busy else "normal")
            self.btn_backup.configure(state="normal" if (connected and not busy) else "disabled")
            self.btn_revert.configure(state="normal" if (connected and not busy) else "disabled")
            self.btn_cancel.configure(
                state="normal" if (busy and not self.cancel_locked) else "disabled")

            if need_bs:
                self.bootstrap_panel.grid()
                self.chk_bootstrap.configure(state="disabled" if busy else "normal")
            else:
                self.bootstrap_panel.grid_remove()
                self.chk_bootstrap.configure(state="disabled")

            allow = connected and not busy and ok and (not need_bs or self.bootstrap_var.get())
            self.btn_flash.configure(state="normal" if allow else "disabled")
            self._update_guidance()

        # -- 確認ダイアログ (二重確認の 1 段目) -----------------------------
        def _confirm_with_checkbox(self, title: str, message: str,
                                   checkbox_text: str) -> bool:
            dlg = tk.Toplevel(self)
            dlg.title(title)
            dlg.transient(self)
            dlg.grab_set()
            dlg.resizable(False, False)
            dlg.configure(bg="#ffffff")
            result = {"ok": False}

            ttk.Label(dlg, text=title, font=("Yu Gothic UI", 12, "bold")).pack(
                anchor="w", padx=16, pady=(14, 6))
            est_lines = sum(max(1, (len(ln) + 75) // 76) for ln in message.splitlines())
            box_h = max(10, min(22, est_lines + 2))
            txt_wrap = ttk.Frame(dlg)
            txt_wrap.pack(fill="both", expand=True, padx=16)
            txt = tk.Text(txt_wrap, width=84, height=box_h, wrap="word", relief="flat",
                          bg="#ffffff", font=self._default_font)
            txt.insert("1.0", message)
            txt.configure(state="disabled")
            txt.pack(side="left", fill="both", expand=True)
            sb = ttk.Scrollbar(txt_wrap, orient="vertical", command=txt.yview)
            sb.pack(side="right", fill="y")
            txt.configure(yscrollcommand=sb.set)
            var = tk.BooleanVar(value=False)
            ttk.Checkbutton(dlg, text=checkbox_text, variable=var).pack(
                anchor="w", padx=16, pady=(10, 4))

            bar = ttk.Frame(dlg)
            bar.pack(fill="x", padx=16, pady=(4, 14))

            def ok():
                if not var.get():
                    messagebox.showwarning(self._tr("dlg_chk_title"), self._tr("dlg_chk_req"), parent=dlg)
                    return
                result["ok"] = True
                dlg.destroy()

            ttk.Button(bar, text=self._tr("dlg_cancel"), command=dlg.destroy).pack(side="right")
            ttk.Button(bar, text=self._tr("dlg_exec"), command=ok).pack(side="right", padx=(0, 8))
            dlg.update_idletasks()
            dlg.geometry("+%d+%d" % (self.winfo_rootx() + 80, self.winfo_rooty() + 80))
            self.wait_window(dlg)
            return result["ok"]

        # -- 実行基盤 -------------------------------------------------------
        def _run(self, op_key: str, worker):
            if self.busy:
                return
            self.busy = True
            self.last_flash_ok = False
            self.cancel_event.clear()
            self.cancel_guard.reset()
            self.cancel_locked = False
            self._update_buttons()
            label = self._op_label(op_key)
            self.progress.configure(mode="determinate", value=0)
            self.progress_text.configure(text=self._tr("prog_start", label=label))
            self.log_i18n("log_op_start", op_key=op_key)
            threading.Thread(target=self._worker_entry, args=(op_key, worker), daemon=True).start()

        def _worker_entry(self, op_key, worker):
            try:
                api = T.ThesyconApi(allow_write=True, logger=self._qlog)
                payload = worker(api)
                self.queue.put(("done", op_key, payload, None))
            except Exception as exc:
                self._qlog(traceback.format_exc())
                self.queue.put(("done", op_key, None, exc))

        def _progress_cb(self, s: T.DfuStatus):
            # 転送中 (InProgress / EnteringAppMode) に入ったらキャンセルを無効化する
            if self.cancel_guard.observe(s):
                self.queue.put(("cancel_lock",))
            pct = None if s.total <= 0 else s.percent
            self.queue.put(("progress", pct, "%s  %d / %d" % (
                s.state_name, s.current, s.total)))

        def _lock_cancel(self):
            """転送中のキャンセル抑止 (F-4)。"""
            if self.cancel_locked:
                return
            self.cancel_locked = True
            self.btn_cancel.configure(state="disabled")
            self.progress_text.configure(text=cancel_lock_message(self.lang))
            self._update_guidance()
            self.log_i18n("log_cancel_locked")

        def _on_done(self, op_key, payload, exc):
            self.busy = False
            self.cancel_locked = False
            self.cancel_guard.reset()
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")

            label = self._op_label(op_key)
            outcome = payload if isinstance(payload, WorkerOutcome) else None
            if exc is not None:
                self._on_failed(op_key, exc)
            elif outcome is not None and outcome.cancelled:
                self.progress.configure(value=0)
                self.progress_text.configure(text=self._tr("prog_cancelled", label=label))
                self.log_i18n("log_op_cancelled", op_key=op_key, text=outcome.text)
                messagebox.showinfo(self._tr("dlg_cancelled_title"), outcome.text)
            elif outcome is not None and not outcome.ok:
                # Finished + completion != 0 もここに来る (エラーダイアログ + 赤字ログ)
                self._on_failed(op_key, DfuOperationError(outcome.text))
            else:
                text = outcome.text if outcome is not None else str(payload or "")
                self.progress.configure(value=100)
                self.progress_text.configure(text=self._tr("prog_done", label=label))
                self.log_i18n("log_op_done", op_key=op_key)
                for line in text.splitlines():
                    self.log("  " + line)
                if op_key == "flash":
                    self.last_flash_ok = True
                messagebox.showinfo(self._tr("dlg_done_title"),
                                    self._tr("dlg_done_body", label=label, text=text))
                if op_key in ("flash", "revert"):
                    self.log_i18n("log_auto_reboot")
                    self.after(4000, self.refresh_device)
            self._update_buttons()

        def _on_failed(self, op_key, exc):
            label = self._op_label(op_key)
            self.progress.configure(value=0)
            self.progress_text.configure(text=self._tr("prog_failed", label=label))
            self.log_i18n("log_op_failed", is_err=True, op_key=op_key, err=str(exc))
            messagebox.showerror(self._tr("dlg_failed_title"),
                                 self._tr("dlg_failed_body", label=label, err=str(exc)))

        def do_cancel(self):
            if not self.busy:
                return
            if self.cancel_locked:
                self.log_i18n("log_cancel_locked")
                return
            self.cancel_event.set()
            self.log_i18n("log_cancel_req")

        # -- 保守操作: 現行イメージ吸出し -----------------------------------
        def do_backup(self):
            if not self.snapshot:
                messagebox.showwarning(self._tr("dlg_unconn_title"), self._tr("dlg_unconn_msg"))
                return
            en = (self.lang == "en")
            default = "do400_current_image_%s.bin" % time.strftime("%Y%m%d_%H%M%S")
            path = filedialog.asksaveasfilename(
                title="Save Uploaded Image As" if en else "吸出したイメージの保存先",
                defaultextension=".bin",
                initialfile=default,
                filetypes=[("Binary" if en else "バイナリ", "*.bin")])
            if not path:
                return
            if en:
                msg = (
                    "[Diagnostic] Upload firmware from upgrade slot via DFU_UPLOAD.\n\n"
                    "Note: Not needed before a normal firmware update.\n"
                    "Note: Stock SMSL FW 3.23 is read-protected and returns 256 zero bytes\n"
                    "      (this is normal behavior, not a fault).\n\n"
                    "- DO400 will reboot into DFU mode and return to app mode automatically.\n"
                    "- Audio playback will pause briefly.\n"
                    "- Destination: %s\n\n"
                    "Proceed?" % path)
                dlg_title = "Upload Current Image (Diag)"
                dlg_chk = "I understand this is a diagnostic read and the device will reboot"
                final_t, final_m = "Final Confirmation", "Start image upload now?"
            else:
                msg = (
                    "【保守用】アップグレードスロットからの DFU_UPLOAD 吸出しを実行します。\n\n"
                    "※ 通常のファームウェア更新前に実行する必要はありません。\n"
                    "※ SMSL 純正 FW 3.23 は読出し保護により 256 B のゼロデータを返します\n"
                    "   （これは正常な仕様で、故障ではありません）。\n\n"
                    "・デバイスは一度 DFU モードへ再起動し、完了後に自動でアプリモードへ戻ります。\n"
                    "・音声再生は一時停止します。\n"
                    "・保存先: %s\n\n"
                    "実行しますか？" % path)
                dlg_title = "現行イメージ吸出し (保守用)"
                dlg_chk = "保守用の吸出し操作であり、デバイスが再起動することを理解しました"
                final_t, final_m = "最終確認", "本当に吸出しを開始しますか？"
            if not self._confirm_with_checkbox(dlg_title, msg, dlg_chk):
                self.log_i18n("log_backup_cancel")
                return
            if not messagebox.askyesno(final_t, final_m, parent=self):
                return
            device_index = self.snapshot.index
            lang = self.lang
            op_lbl = self._op_label("backup")

            def worker(api: T.ThesyconApi):
                result = api.dfu_upload(device_index=device_index,
                                        on_progress=self._progress_cb,
                                        timeout=300.0, cancel=self.cancel_event)
                if result.cancelled:
                    return WorkerOutcome(
                        False,
                        ("%s: Cancelled (EndDfuProc completed)" if lang == "en"
                         else "%s: 中断されました (EndDfuProc 実行済み)") % op_lbl,
                        cancelled=True)
                ok, text = describe_dfu_result(op_lbl, result, lang=lang)
                if not ok:
                    return WorkerOutcome(False, text)
                data = result.image or b""
                with open(path, "wb") as fh:
                    fh.write(data)
                if lang == "en":
                    lines = [text, "Saved: %s" % path,
                             "Size: %s" % _fmt_bytes(len(data)),
                             "SHA-256: %s" % V.sha256_hex(data)]
                    if is_read_protected_upload(data):
                        lines.append("Result: Read-protected response (256 bytes of 0x00)")
                        lines.append("  Note: Normal behavior for stock XMOS / SMSL FW.")
                        lines.append("  Note: DFU communication and auto-reboot succeeded.")
                    else:
                        r = V.validate_bytes(data, path)
                        lines.append("Verify: %s" % r.verdict)
                        for c in r.failures:
                            lines.append("  NG  %s — %s" % (c.name, c.detail))
                else:
                    lines = [text, "保存: %s" % path,
                             "サイズ: %s" % _fmt_bytes(len(data)),
                             "SHA-256: %s" % V.sha256_hex(data)]
                    if is_read_protected_upload(data):
                        lines.append("結果: 読出し保護応答 (256 B すべて 0x00)")
                        lines.append("  ※ XMOS / SMSL 純正 FW の正常な仕様です（故障ではありません）。")
                        lines.append("  ※ DFU 通信と自動再起動の動作確認は正常に完了しました。")
                    else:
                        r = V.validate_bytes(data, path)
                        lines.append("検証: %s" % r.verdict)
                        for c in r.failures:
                            lines.append("  NG  %s — %s" % (c.name, c.detail))
                return WorkerOutcome(True, "\n".join(lines))

            self._run("backup", worker)

        # -- STEP 3: ファームウェア更新 -------------------------------------
        def do_flash(self):
            r = self.validation
            if not self.snapshot:
                messagebox.showwarning(self._tr("dlg_unconn_title"), self._tr("dlg_unconn_msg"))
                return
            if r is None or not r.write_allowed:
                messagebox.showwarning(self._tr("dlg_val_ng_title"), self._tr("dlg_val_ng_msg"))
                return
            if r.requires_bootstrap and not self.bootstrap_var.get():
                messagebox.showwarning(self._tr("dlg_bs_title"), self._tr("dlg_bs_msg"))
                return

            en = (self.lang == "en")
            p = self.snapshot.properties
            if en:
                mode_label = ("> 200 KiB (Bootstrap required)" if r.requires_bootstrap
                              else "Standard USB DFU (Direct Flash)")
                detail = (
                    "Ready to flash the following firmware image to DO400:\n\n"
                    "  File     : %s\n"
                    "  Size     : %s\n"
                    "  Mode     : %s\n"
                    "  SHA-256  : %s\n"
                    "  ImageCRC : 0x%08X (verified)\n"
                    "  Target   : %s  bcdDevice 0x%04X (FW %s)\n\n"
                    "- Do NOT power off or unplug USB during flashing.\n"
                    "- DO400 reboots automatically when complete (~30 s).\n"
                    "- The stock factory image (image0) is kept untouched;\n"
                    "  if needed, you can recover via 'Emergency Revert to Factory'." % (
                        self.selected_path, _fmt_bytes(r.file_size), mode_label, r.sha256,
                        r.image_crc_calc or 0, p.vid_pid, p.bcd_device,
                        p.firmware_version))
                if r.requires_bootstrap:
                    detail += ("\n\nNote: Image exceeds 200 KiB — only flash onto a device\n"
                               "      already running 512 KiB DFU-enabled firmware.")
                t1, c1 = "Confirm Firmware Update (1/2)", "I verified the details and will not power off or unplug USB"
                t2, m2 = "Final Confirmation (2/2)", "Write firmware to DO400 now?\nStart flashing?"
            else:
                mode_label = ("200 KiB 超（要ブートストラップ）" if r.requires_bootstrap
                              else "通常 USB DFU 更新 (直接書込み)")
                detail = (
                    "以下の内容で DO400 のファームウェアを書き込みます。\n\n"
                    "  ファイル : %s\n"
                    "  サイズ   : %s\n"
                    "  更新方式 : %s\n"
                    "  SHA-256  : %s\n"
                    "  ImageCRC : 0x%08X (再計算一致)\n"
                    "  対象機器 : %s  bcdDevice 0x%04X (FW %s)\n\n"
                    "・書込み中は電源を切ったり USB を抜いたりしないでください。\n"
                    "・書込み完了後、DO400 は自動的に再起動します（約 30 秒）。\n"
                    "・工場出荷ファームウェア (Factory / image0) は上書きされず保護されます。\n"
                    "  万一の場合は「緊急 Factory 復帰」で純正状態へ戻せます。" % (
                        self.selected_path, _fmt_bytes(r.file_size), mode_label, r.sha256,
                        r.image_crc_calc or 0, p.vid_pid, p.bcd_device,
                        p.firmware_version))
                if r.requires_bootstrap:
                    detail += ("\n\n※ 200 KiB 超のため、512 KiB ブートストラップ導入済みの\n"
                               "   デバイスに対してのみ実行してください。")
                t1, c1 = "ファームウェア更新の確認 (1/2)", "内容を確認し、書込み中は電源・USB に触れないことを理解しました"
                t2, m2 = "最終確認 (2/2)", "DO400 のファームウェアを書き換えます。\n本当に開始しますか？"
            if not self._confirm_with_checkbox(t1, detail, c1):
                self.log_i18n("log_flash_cancel")
                return
            if not messagebox.askyesno(t2, m2, icon="warning", parent=self):
                self.log_i18n("log_flash_cancel")
                return
            device_index = self.snapshot.index
            path = self.selected_path
            lang = self.lang
            op_lbl = self._op_label("flash")

            def worker(api: T.ThesyconApi):
                result = perform_flash(api, path, device_index=device_index,
                                       on_progress=self._progress_cb,
                                       timeout=600.0, cancel=self.cancel_event)
                if result.cancelled:
                    return WorkerOutcome(
                        False,
                        ("%s: Cancelled (EndDfuProc completed)" if lang == "en"
                         else "%s: 中断されました (EndDfuProc 実行済み)") % op_lbl,
                        cancelled=True)
                ok, text = describe_dfu_result(op_lbl, result, lang=lang)
                if not ok:
                    return WorkerOutcome(False, text)
                sha = result.validation.sha256 if result.validation is not None else "-"
                if lang == "en":
                    return WorkerOutcome(
                        True,
                        "%s\nLoader: LoadFirmwareImageFromBuffer\nSHA-256: %s\n\n"
                        "DO400 will reboot automatically. Check reconnection in a few seconds."
                        % (text, sha))
                return WorkerOutcome(
                    True,
                    "%s\nロード: LoadFirmwareImageFromBuffer\nSHA-256: %s\n\n"
                    "DO400 は自動で再起動します。数秒後に再接続を確認してください。"
                    % (text, sha))

            self._run("flash", worker)

        # -- 緊急操作: Factory 復帰 -----------------------------------------
        def do_revert(self):
            if not self.snapshot:
                messagebox.showwarning(self._tr("dlg_unconn_title"), self._tr("dlg_unconn_msg"))
                return
            en = (self.lang == "en")
            p = self.snapshot.properties
            if en:
                msg = (
                    "Revert to the stock factory image (image0).\n\n"
                    "- The upgrade slot (image1) will be erased.\n"
                    "- Custom firmware will be removed.\n"
                    "- Target: %s  bcdDevice 0x%04X (FW %s)\n\n"
                    "This action cannot be undone." % (p.vid_pid, p.bcd_device, p.firmware_version))
                t1, c1 = "Emergency Revert to Factory (1/2)", "I understand this erases the upgrade slot and reverts to the stock factory image"
                t2, m2 = "Final Confirmation (2/2)", "Erase upgrade slot and revert to stock factory firmware?\nProceed?"
            else:
                msg = (
                    "工場出荷ファームウェア (Factory / image0) へ復帰します。\n\n"
                    "・アップグレードスロット (image1) は消去されます。\n"
                    "・カスタムファームウェアは削除されます。\n"
                    "・対象: %s  bcdDevice 0x%04X (FW %s)\n\n"
                    "この操作は元に戻せません。" % (p.vid_pid, p.bcd_device, p.firmware_version))
                t1, c1 = "緊急 Factory 復帰 (1/2)", "危険な操作であることを理解し、工場出荷ファームウェアへの復帰を実行します"
                t2, m2 = "最終確認 (2/2)", "アップグレードスロットを消去し工場出荷ファームウェアへ戻します。\n実行しますか？"
            if not self._confirm_with_checkbox(t1, msg, c1):
                self.log_i18n("log_revert_cancel")
                return
            if not messagebox.askyesno(t2, m2, icon="warning", parent=self):
                self.log_i18n("log_revert_cancel")
                return
            device_index = self.snapshot.index
            lang = self.lang
            op_lbl = self._op_label("revert")

            def worker(api: T.ThesyconApi):
                result = api.dfu_revert_to_factory(device_index=device_index,
                                                   on_progress=self._progress_cb,
                                                   timeout=300.0, cancel=self.cancel_event)
                if result.cancelled:
                    return WorkerOutcome(
                        False,
                        ("%s: Cancelled (EndDfuProc completed)" if lang == "en"
                         else "%s: 中断されました (EndDfuProc 実行済み)") % op_lbl,
                        cancelled=True)
                ok, text = describe_dfu_result(op_lbl, result, lang=lang)
                if not ok:
                    return WorkerOutcome(False, text)
                return WorkerOutcome(True, text)

            self._run("revert", worker)

        # -- 終了 -----------------------------------------------------------
        def on_close(self):
            if self.busy and self.cancel_locked:
                messagebox.showwarning(self._tr("dlg_close_locked_title"), self._tr("dlg_close_locked_msg"))
                return
            if self.busy and not messagebox.askyesno(
                    self._tr("dlg_close_busy_title"), self._tr("dlg_close_busy_msg")):
                return
            self.cancel_event.set()
            self.destroy()


def run_gui(argv=None) -> int:
    if not _TK_AVAILABLE:
        print("tkinter が利用できないため GUI を起動できません。", file=sys.stderr)
        return 2
    app = DfuApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
    return 0


# ==========================================================================
# エントリポイント
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="DO400 DFU 更新ツール (XMOS / Thesycon API)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="引数なしで起動すると GUI が開きます。")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check-device", action="store_true",
                   help="実機の状態を読み取り専用で表示する (書込み・DFU 遷移なし)")
    g.add_argument("--list-devices", action="store_true",
                   help="接続されているオーディオデバイスを列挙する (読み取り専用)")
    g.add_argument("--validate", metavar="BIN",
                   help="イメージをオフライン検証する (デバイスに触れない)")
    g.add_argument("--dry-run-flash", metavar="BIN",
                   help="書込シーケンスをモックで完全ドライランする (実機に触れない)")
    p.add_argument("--version", action="version", version=APP_VERSION)
    return p


def main(argv=None) -> int:
    _setup_stdout()
    args = build_parser().parse_args(argv)
    if args.check_device:
        return cmd_check_device(args)
    if args.list_devices:
        return cmd_list_devices(args)
    if args.validate:
        return cmd_validate(args)
    if args.dry_run_flash:
        return cmd_dry_run_flash(args)
    return run_gui(argv)


if __name__ == "__main__":
    sys.exit(main())
