# Thesycon / XMOS `xmos_s_usbaudioapi_x64.dll` DFU API 解析ノート

対象 : `C:\Program Files\XMOS\XMOS_Audio_Driver\x64\xmos_s_usbaudioapi_x64.dll`
内部名 : `tusbaudioapi.dll` (Thesycon USB Audio API)
版 : FileVersion 6.0.0.0 / **API v5.15** (`TUSBAUDIO_GetApiVersion() == 0x0005000F`)
ドライバ: `xmos_s_usbaudio.sys` 6.0.0.17477
解析手段: `pefile` によるエクスポート表 + `capstone` による x64 逆アセンブル

> 本ノートのシグネチャは「エクスポートのトランポリンが引数をどのレジスタから
> 内部関数へ渡しているか」を 1 本ずつ確認して確定した。推測ではなく、
> 各関数の RVA と根拠命令を併記する。

---

## 1. 呼び出し規約の要点

* x64 (Win64) 呼び出し規約。第 1〜4 引数 = `rcx, rdx, r8, r9`、第 5 引数以降はスタック。
* `TStatus` は 32 bit 符号なし。`0x00000000` = `TSTATUS_SUCCESS`。
* **デバイスハンドルは 32 bit の不透明値**（ポインタではない）。
  実測値は `0x00010000`（= `(世代 << 16) | デバイス索引`）。
  `GetDeviceProperties` 内部で `handle & 0xFFFF` を索引としてテーブルを引き、
  エントリの ID と 32 bit 全体を照合している（0xDC36〜0xDC53）。
* `TUSBAUDIO_GetDfuStatus` / `TUSBAUDIO_EndDfuProc` は **デバイスハンドルを取らない**。
  DFU はプロセス内で 1 本のグローバルコンテキストで管理される。
* `TUSBAUDIO_GetApiVersion` は `TStatus` ではなく **バージョン番号そのもの**を返す。
  `TUSBAUDIO_GetDeviceCount` も同様に台数を直接返す（`TStatus` ではない）。

---

## 2. 確定シグネチャ（エクスポート 83 件のうち DFU / 情報系）

```c
typedef unsigned int TStatus;               /* 0 == TSTATUS_SUCCESS */
typedef unsigned int TUsbAudioDeviceHandle; /* 32 bit 不透明値 */

/* ---- バージョン / 列挙 ---------------------------------------------- */
unsigned int TUSBAUDIO_GetApiVersion(void);
unsigned int TUSBAUDIO_GetDeviceCount(void);
TStatus      TUSBAUDIO_EnumerateDevices(void);
TStatus      TUSBAUDIO_OpenDeviceByIndex(unsigned int index, unsigned int* pHandle);
TStatus      TUSBAUDIO_CloseDevice(unsigned int handle);

/* ---- 情報取得 (読み取り専用) ---------------------------------------- */
TStatus TUSBAUDIO_GetDeviceProperties(unsigned int handle, void* pProps /* 0x410 B */);
TStatus TUSBAUDIO_GetCurrentSampleRate(unsigned int handle, unsigned int* pRate);
TStatus TUSBAUDIO_GetDeviceUsbMode(unsigned int handle, unsigned int* pMode);
TStatus TUSBAUDIO_GetUsbConfigDescriptor(unsigned int handle,
                                         void* pBuf, unsigned int bufSize,
                                         unsigned int* pNumBytes);
TStatus TUSBAUDIO_GetUsbStringDescriptorString(unsigned int handle,
                                         unsigned int descIndex /* 0..255 */,
                                         unsigned int langId,      /* 0 = 既定 */
                                         void* pBuf /* wchar_t* */,
                                         unsigned int bufChars);

/* ---- ステータス文字列 ------------------------------------------------ */
const wchar_t* TUSBAUDIO_StatusCodeStringW(TStatus status);
const char*    TUSBAUDIO_StatusCodeStringA(TStatus status);

/* ---- イメージのロード (ホスト側のみ / デバイス I/O なし) ------------- */
/* 第 1 引数は const wchar_t* (§2.3 参照) */
TStatus TUSBAUDIO_LoadFirmwareImageFromFile(const wchar_t* path, unsigned int flags);
TStatus TUSBAUDIO_LoadFirmwareImageFromBuffer(const void* pBuf,
                                              unsigned int size, unsigned int flags);
TStatus TUSBAUDIO_UnloadFirmwareImage(void);
TStatus TUSBAUDIO_GetFirmwareImageSize(unsigned int* pSize, unsigned int flags);
TStatus TUSBAUDIO_GetFirmwareImage(void* pBuf, unsigned int bufSize,
                                   unsigned int* pWritten, unsigned int flags);

/* ---- DFU 実行 -------------------------------------------------------- */
TStatus TUSBAUDIO_StartDfuDownload(unsigned int devIndex,
                                   unsigned int imageIndex, unsigned int flags);
TStatus TUSBAUDIO_StartDfuUpload(unsigned int devIndex,
                                 unsigned int imageIndex, unsigned int flags);
TStatus TUSBAUDIO_StartDfuRevertToFactoryImage(unsigned int devIndex,
                                               unsigned int flags);
TStatus TUSBAUDIO_GetDfuStatus(unsigned int* pState, unsigned int* pCur,
                               unsigned int* pTotal, unsigned int* pCompletion);
TStatus TUSBAUDIO_EndDfuProc(void);
```

### 2.1 根拠（RVA と実命令）

RVA は ImageBase `0x180000000` からのオフセット。

| 関数 | RVA | 根拠 |
|---|---|---|
| `GetApiVersion` | `0x1A60` | `mov eax, 0x5000F` / `ret` — 引数参照なし。戻り値がバージョン |
| `GetDeviceCount` | `0x1CB0` | 引数なしで内部 `0xD750` へ。戻り値 = 台数 |
| `EnumerateDevices` | `0x19C0` | 引数なし |
| `OpenDeviceByIndex` | `0x20E0` | `rcx`→索引, `rdx`→出力ポインタ（`r8 = rdx` で 3 番目へ） |
| `CloseDevice` | `0x1950` | `ecx` のみ |
| `GetDeviceProperties` | `0x1CE0` | `ecx`→32bit, `rdx`→ポインタ。内部で要求長 `0x410`（`0xDCA1` の `mov edx, 0x808820c4` / `[rsp+0x28]=0x410`） |
| `GetCurrentSampleRate` | `0x1C20` | 内部 `0xD270` が `0x80882104` を 4 B バッファで要求し、結果を `*pRate` へ |
| `GetDeviceUsbMode` | `0x1D20` | 内部 `0xDDC0` が 16 B 取得し、`+0x0C` の非ゼロを `*pMode`（0/1）へ |
| `GetUsbConfigDescriptor` | `0x1FC0` | `rcx,rdx,r8,r9` の 4 引数（`r9` を 5 番目へ退避） |
| `GetUsbStringDescriptorString` | `0x1FF0` | **5 引数**。`dl` を `movzx r8d, dl`（= index は 8 bit）、`[rsp+0x60]`(=第5引数) を退避 |
| `StatusCodeStringW` | `0x2410` | `rcx` をそのまま `0x6C90` へ。戻り値 `const wchar_t*` |
| `LoadFirmwareImageFromFile` | `0x20A0` | `rcx`→**`const wchar_t*` path**, `edx`→flags。§2.3 参照 |
| `LoadFirmwareImageFromBuffer` | `0x2080` | `rcx`→buf, `edx`→size, `r8d`→flags。内部 `0xFF80` が `ctx+0x3E0` へ**コピー**するので呼出後にバッファを解放してよい。**本アプリの標準経路** (§2.3) |
| `UnloadFirmwareImage` | `0x2420` | 引数なし |
| `GetFirmwareImageSize` | `0x1E40` | `rcx`→pSize, `edx`→flags。`flags & 1` で「吸出し結果」側を読む |
| `GetFirmwareImage` | `0x1E10` | 4 引数: buf, bufSize, pWritten, flags（第 4 引数はスタック） |
| `StartDfuDownload` | `0x23A0` | `ecx`→devIndex, `edx`→imageIndex（`cmp edi,0xFF` で上限検査）, `r8d`→flags |
| `StartDfuUpload` | `0x23E0` | 同上（内部 opcode 2） |
| `StartDfuRevertToFactoryImage` | `0x23C0` | 2 引数（devIndex, flags）。内部で imageIndex=0 固定 |
| `GetDfuStatus` | `0x1D40` | 4 つのポインタ。`0xDEC0` で `*p1=+0x390`, `*p2=+0x314`, `*p3=+0x310`, `*p4=+0x394` |
| `EndDfuProc` | `0x19B0` | 引数なし。状態/進捗/完了コードをゼロに戻す |

### 2.2 `flags` について

`StartDfu*` の `flags` は内部（`0x12CC1`〜`0x12CDD`）で次の 2 ビットだけを参照する。

* bit4 (0x10) → `ctx+0x1FC`
* bit5 (0x20) → `ctx+0x1FD`

いずれも意味は DLL 内に文字列がなく未公開。**本アプリでは常に `flags = 0`** を使う。
`LoadFirmwareImage*` / `GetFirmwareImage*` の `flags` は、`GetFirmwareImageSize` の
bit0 のみが「吸出し結果 / ロード済みイメージ」の選択に使われる（`0xE3D0`）。

### 2.3 `LoadFirmwareImageFromFile` の引数型 = `const wchar_t*`

DLL バイナリの逆アセンブルで確定した。

| 根拠 | 内容 |
|---|---|
| `0x20A0` (エクスポート) | `mov r8d, edx` / `mov rdx, rcx` / `lea rcx, [rip+...]` → 内部 `0xFFF0` へ。第 1 引数はそのまま `rcx` で渡る (ポインタ) |
| `0xFFF0` → `0x2560` | `mov rcx, rbx; call 0x180002560` の後 `test eax, eax; je 0xEE000041` = **空文字列チェック** |
| `0x2560` | `cmp word ptr [rcx], ax` / `lea rcx, [rcx + 2]` / `inc eax` → **16 bit 単位の `wcslen`** |
| `0x384F0` | `72 00 62 00 00 00` = `L"rb"` (UTF-16LE のワイド文字列モード) |
| インポート | ファイル系は `CreateFileW` のみ (`CreateFileA` なし)、`MultiByteToWideChar` も併存 |

→ 第 1 引数は **`const wchar_t*`**。`TStatus TUSBAUDIO_LoadFirmwareImageFromFile(
const wchar_t* path, unsigned int flags)`。

**本アプリでの安全な実装（二重化）**

1. `thesycon_api.SIGNATURES["TUSBAUDIO_LoadFirmwareImageFromFile"]` を
   `(c_uint, [c_wchar_p, c_uint])` でバインドし、呼出時は `str(path)` を渡す
   (`CtypesBackend.load_firmware_image_from_file`)。回帰テスト
   `TestLoadImageSignature` が `argtypes[0] is ctypes.c_wchar_p` と `str` 受け渡しを
   実 DLL 非ロードで検証する。
2. **標準経路は `LoadFirmwareImageFromBuffer`**。`ThesyconApi.download_image()` は
   ファイルを 1 回だけ読み、`fw_validator.validate_bytes()` に合格した
   *同じ* `bytes` を `TUSBAUDIO_LoadFirmwareImageFromBuffer(data, len(data), 0)` へ渡す。
   これにより
   * 検証〜ロード間のファイル差替 (TOCTOU) が構造的に不可能
   * パス文字コード (`wchar_t` / ANSI / UTF-8) 問題が発生しない

   アプリの GUI 更新・`--dry-run-flash` はどちらもこの経路だけを使う
   (`do400_dfu_app.perform_flash`)。`LoadFirmwareImageFromFile` は API 互換のため
   残すが、アプリの書込では使わない。

---

## 3. `TUsbAudioDeviceProperties`（0x410 B）

DLL はドライバから **0x410 バイト**を取得してそのままユーザーバッファへ返す
（`GetDeviceProperties` 内部 `0xDCA1` 以降）。実機でフィールドを確認した結果:

| オフセット | 型 | 内容 | 実機値 (DO400) |
|---|---|---|---|
| `0x000` | `unsigned int` | `vendorId` | `0x0000152A` |
| `0x004` | `unsigned int` | `productId` | `0x000085DD` |
| `0x008` | `unsigned int` | `bcdDevice` (BCD) | `0x00000323` (= FW 3.23) |
| `0x00C`–`0x10B` | 予約 (0x100 B) | 実機では全て 0 | — |
| `0x10C` | `wchar_t[128]` | Manufacturer | `"SMSL"` |
| `0x20C` | `wchar_t[128]` | Product | `"SMSL USB AUDIO"` |
| `0x30C` | `wchar_t[128]` | Serial | `"\u12d7"`（2 バイトのバイナリ値。文字列ではない） |
| `0x40C` | `unsigned int` | 予約 | 0 |

実測の非ゼロバイト位置: `0,1,4,5,8,9,268..274,524..,780..` — 上表と完全一致。

`bcdDevice` は **BCD 表記**。`0x0323` は `3.23`、`0x0329` は `3.29`。
（`0x23` を十進 35 と解釈してはいけない。）

### 3.1 その他の構造体

| API | 出力長 | 内容 |
|---|---|---|
| `GetCurrentSampleRate` | 4 B | 現在のサンプルレート [Hz]（実測 44100） |
| `GetDeviceUsbMode` | 16 B | `+0x0C` の非ゼロ → モード 1。実測 0 |
| `GetUsbConfigDescriptor` | 可変 | USB コンフィグディスクリプタ生バイト（実測 291 B、`bDescriptorType=0x02`） |
| `GetUsbStringDescriptorString` | 呼び出し側 `wchar_t` バッファ | index 1=Manufacturer, 3=Product（実測 `"SMSL"`, `"SMSL USB AUDIO"`）。index 0 は `0xEE000007` を返す |

---

## 4. DFU 状態機械

`TUSBAUDIO_GetDfuStatus(&state, &cur, &total, &completion)` の `state`:

| 値 | 名前 | 意味 |
|---|---|---|
| 0 | `Idle` | 待機（DFU プロセス未開始／終了後） |
| 1 | `Initializing` | 初期化 |
| 2 | `EnteringDfuMode` | DFU モードへ遷移中（デバイス再起動を伴う） |
| 3 | `InProgress` | 転送中 |
| 4 | `EnteringAppMode` | アプリモードへ復帰中 |
| 5 | `Finished` | 正常終了（`completion` を確認） |
| 6 | `Failed` | 失敗（`completion` にエラーコード） |

* `6 = Failed` は DLL 内で実確認（`0x12F00`: `mov edx, 6` → 状態セッター）。
* 終了判定は `state in (5, 6)`。
* `cur` / `total` は**進捗カウンタ**。DLL 内に書き込み側が見当たらない
  （`+0x310` / `+0x314` は `GetDfuStatus` で読まれ、`EndDfuProc` でゼロ化されるのみ）ため、
  どちらが「現在」でどちらが「総量」かは静的に確定できない。ドライバ通知バッファ経由で
  更新されると推測される。**比率は対称なので `min/max` で進捗率を出せば
  どちらの解釈でも正しい**（`thesycon_api.DfuStatus.percent` はそう実装している）。
* `completion` は DFU 終了時の `TStatus`。`0` なら成功。
* `TUSBAUDIO_EndDfuProc()` は **失敗・中断・タイムアウト時も必ず呼ぶ**。
  呼ばないと `ctx+0x1B0`（ビジー）が残り、次の `StartDfu*` が
  `TSTATUS_NOT_ALLOWED (0xEE000032)` で拒否される。

---

## 5. 主要なステータスコード（実機で文字列を取得）

| コード | 名前 | 発生しうる場面 |
|---|---|---|
| `0x00000000` | `TSTATUS_SUCCESS` | 成功 |
| `0xEE000001` | `TSTATUS_FAILED` | 汎用失敗 |
| `0xEE000002` | `TSTATUS_INTERNAL_ERROR` | 内部エラー |
| `0xEE000003` | `TSTATUS_TIMEOUT` | デバイス応答なし |
| `0xEE000005` | `TSTATUS_ABORTED` | `EndDfuProc` による中断 |
| `0xEE000032` | `TSTATUS_NOT_ALLOWED` | DFU プロセスが既に動作中 |
| `0xEE000033` | `TSTATUS_NOT_OPENED` | ハンドル無効 |
| `0xEE000034` | `TSTATUS_NOT_AVAILABLE` | 対象なし |
| `0xEE000041` | `TSTATUS_INVALID_PARAMETER` | 引数不正（NULL 等） |
| `0xEE000047` | `TSTATUS_INVALID_INDEX` | デバイス索引 / imageIndex 不正 |
| `0xEE000048` | `TSTATUS_INVALID_HANDLE` | ハンドル不正 |
| `0xEE000070` | `TSTATUS_ENUM_REQUIRED` | `EnumerateDevices` 未実行 |
| `0xEE000074` | `TSTATUS_BUFFER_TOO_SMALL` | `GetFirmwareImage` のバッファ不足 |
| `0xEE000081` | `TSTATUS_OBJECT_NOT_FOUND` | 吸出し結果が無い |
| `0xEE001007` | `TSTATUS_NO_IMAGE_LOADED` | `LoadFirmwareImage*` 前に `StartDfuDownload` |

`TUSBAUDIO_StatusCodeStringW/A` で実行時に文字列化できる（未知コードは `"unknown"`）。

---

## 6. 読み取り専用 / 書込の分類（本プロジェクトの安全設計）

| 分類 | API | 実機への影響 |
|---|---|---|
| **完全読み取り専用** | `GetApiVersion`, `EnumerateDevices`, `GetDeviceCount`, `OpenDeviceByIndex`, `CloseDevice`, `GetDeviceProperties`, `GetCurrentSampleRate`, `GetDeviceUsbMode`, `GetUsbConfigDescriptor`, `GetUsbStringDescriptorString`, `StatusCodeStringW`, `GetDfuStatus` | なし（状態を変えない） |
| **ホスト側のみ** | `LoadFirmwareImageFromFile/Buffer`, `UnloadFirmwareImage`, `GetFirmwareImageSize`, `GetFirmwareImage` | なし（DLL 内メモリのみ） |
| **実機を書き換える** | `StartDfuDownload` | アップグレードスロット (image1) を書換 |
| **実機を再起動させる** | `StartDfuUpload` | DFU モードへ遷移して吸出し |
| **実機を消去する** | `StartDfuRevertToFactoryImage` | アップグレードスロットを消去し Factory へ |
| **後始末** | `EndDfuProc` | DFU プロセス終了（必須） |

`thesycon_api.ThesyconApi` は `allow_write=True` を渡さない限り、
「実機を書き換える / 再起動させる / 消去する」と「ホスト側のみ」の API を
`SafetyError` で拒否する。`--check-device` などは `allow_write=False` で動作する。

### 6.1 完了判定とキャンセル抑止

* 成功条件は **`state == 5 (Finished)` かつ `completion == 0`** の両方。
  `Finished` でも `completion != 0` は失敗として扱う（`DfuResult.ok`、
  `ThesyconApi._ensure_finished`、GUI `describe_dfu_result` の 3 層で判定）。
  エラー表示には必ず `completion` の数値と `TSTATUS_*` 文字列を含める。
* `state == 3 (InProgress)` / `4 (EnteringAppMode)` に入ったら GUI の「中断」は
  無効化する（`do400_dfu_app.TransferCancelGuard`）。転送中に `EndDfuProc` を
  呼ぶと転送が途中で切れるため。`Initializing` / `EnteringDfuMode` 中は中断可。
  タイムアウト・例外時の `EndDfuProc` は API の `finally` が必ず呼ぶ。

---

## 7. 実機での読み取り専用確認結果（2026-11 / DO400 接続状態）

`_analysis/probe_readonly.py` の出力（要約）:

```
GetApiVersion()          = 0x0005000F  -> 5.15
EnumerateDevices()       = 0x00000000
GetDeviceCount()         = 1
OpenDeviceByIndex(0)     = 0x00000000  handle=0x10000
GetDeviceProperties      = 0x00000000
  0x000 vendorId   = 0x0000152A
  0x004 productId  = 0x000085DD
  0x008 bcdDevice  = 0x00000323      -> FW 3.23
  0x10C manufacturer = 'SMSL'
  0x20C product      = 'SMSL USB AUDIO'
GetCurrentSampleRate     = 0x00000000  44100 Hz
GetDeviceUsbMode         = 0x00000000  0
GetUsbConfigDescriptor   = 0x00000000  291 B  bDescriptorType=0x02
GetUsbStringDescriptor(1) = 0x00000000  'SMSL'
GetUsbStringDescriptor(3) = 0x00000000  'SMSL USB AUDIO'
GetDfuStatus             = 0x00000000  state=0 cur=0 total=0 completion=0x0
CloseDevice              = 0x00000000
```

期待値 `VID=0x152A / PID=0x85DD / bcdDevice=0x0323 / 44100 Hz` と一致。
**`StartDfuDownload` / `StartDfuUpload` / `StartDfuRevertToFactoryImage` は
本ラウンドで一度も呼んでいない。**

---

## 8. 実装上の注意（ハマりどころ）

1. `TUSBAUDIO_GetDeviceCount()` は `TStatus` ではなく **台数**を返す。
   `if (status) error` と書くと「1 台接続時にエラー」になる。
2. `TUSBAUDIO_GetApiVersion()` も同様にバージョン値そのもの。
3. `GetUsbStringDescriptorString` は **5 引数**。第 3 引数 `langId` に 0 を渡す。
   ctypes で `argtypes` を 4 個にすると `TypeError: takes 4 arguments (5 given)`。
4. `OpenDeviceByIndex` のハンドルは `unsigned int`（`c_void_p` ではない）。
   `c_uint` で受ける。
5. `StartDfu*` の後は必ず `GetDfuStatus` をポーリングし、`EndDfuProc` を `finally` で呼ぶ。
6. DFU 中は同じ DLL の他 API（`GetDeviceProperties` 等）を叩かない。
   再起動でハンドルが無効になるため、完了後に `EnumerateDevices` からやり直す。
