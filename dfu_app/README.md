# DO400 DFU 更新アプリ

DO400 (XMOS XU316 / SMSL USB DAC) のファームウェアを、Windows から
**Thesycon 公式 API (`xmos_s_usbaudioapi_x64.dll`) を直接叩いて**更新するための
ステップ案内付きデスクトップアプリ。

* Python 標準ライブラリのみ（`tkinter` + `ctypes`）。**追加 pip 依存ゼロ**。
* 書込み前に必ずオフラインでコンテナ全体（Magic / サイズ / 5 種の CRC / レイアウト /
  DFU 上限）を検証し、1 つでも不一致なら書込ボタンをロックする。
* 同梱ファームウェア（`firmware/do400_peq_dfu.bin`, 196.2 KiB < 200 KiB）は **そのまま 1 回の USB DFU で直接更新** できます（200 KiB 超の独自ビルドを選んだ場合のみ、追加の確認欄が表示されます）。

---

## 1. ファイル構成

| ファイル | 役割 |
|---|---|
| `thesycon_api.py` | DLL ラッパー。実 DLL バックエンド + モックバックエンド + 書込ガード |
| `fw_validator.py` | オフライン検証器（CRC / 構造 / サイズ上限）。単体でも CLI として動く |
| `do400_dfu_app.py` | 本体。引数なしで GUI、引数付きで CLI |
| `launch_dfu_app.bat` | ダブルクリック起動用（**ASCII のみ**で記述。コンソールのコードページに依存せず壊れないようにするため） |
| `test_dfu_app.py` | 自動テスト（既定は完全オフライン / 実機テストは opt-in） |
| `THESYCON_API_NOTES.md` | API シグネチャ・構造体・状態機械の解析記録 |
| `README.md` | 本ファイル |

---

## 2. 起動方法

### 2.1 GUI

```
launch_dfu_app.bat
```
または
```
python dfu_app/do400_dfu_app.py
```

### 2.2 CLI（読み取り専用 / ドライラン）

```
python do400_dfu_app.py --check-device            # 実機の状態を読むだけ
python do400_dfu_app.py --list-devices            # デバイス一覧
python do400_dfu_app.py --validate X.bin          # イメージ検証のみ
python do400_dfu_app.py --dry-run-flash X.bin     # 書込シーケンスをモックで再生
```

* `--check-device` / `--list-devices` / `--validate` は **完全読み取り専用**。
  DFU モード遷移も書込みも行わない。
* `--dry-run-flash` はモックバックエンドで `LoadFirmwareImageFromBuffer` →
  `StartDfuDownload` → `GetDfuStatus` ポーリング → `EndDfuProc` →
  `UnloadFirmwareImage` の順序を確認する。実 DLL の DFU 関数は呼ばない。

GUI からのみ実書込みが可能（CLI に書込コマンドは用意していない）。

### 2.3 書込み経路（Buffer 標準経路と `wchar_t*` バインド）

`TUSBAUDIO_LoadFirmwareImageFromFile` の第 1 引数は **`const wchar_t*`** である
（DLL 内部 `0x2560` が 16 bit 単位の `wcslen`、モード文字列は `L"rb"`、
`CreateFileW` 経由。詳細は `THESYCON_API_NOTES.md` §2.3 を参照）。

本アプリでは次の二段構えを採用している。

1. `TUSBAUDIO_LoadFirmwareImageFromFile` は `c_wchar_p` でバインドし、`str` を渡す
   （`SIGNATURES` 表 + 回帰テスト `TestLoadImageSignature`）。
2. **標準経路は `TUSBAUDIO_LoadFirmwareImageFromBuffer`**。
   `ThesyconApi.download_image()` / `do400_dfu_app.perform_flash()` が
   ファイルを 1 回だけ読み、`fw_validator.validate_bytes()` に合格した
   *同じ* `bytes` を `LoadFirmwareImageFromBuffer(data, len(data), 0)` へ渡す。

これにより **検証〜書込間のファイル差替 (TOCTOU)** と **パス文字コード問題**が
どちらも構造的に起きない。GUI の更新と `--dry-run-flash` はこの経路だけを使う。

---

## 3. 画面の見方

```
┌ DO400 DFU 更新ツール ───────── [PEQ 設定画面を開く]  API: 5.15 ┐
│ [✓ 1. 接続確認] → [✓ 2. イメージ選択] → [▶ 3. 更新実行]       │
│ [STEP 3] 準備完了！「ファームウェア更新を実行」を押してください │
├ STEP 1. DO400 接続確認 ────────────────────────────────────────┤
│ [接続済み] SMSL USB AUDIO (index 0)               [再読み込み] │
│            VID:PID 152A:85DD  bcdDevice 0x0323 (FW 3.23)       │
├ STEP 2. ファームウェア選択 & 自動検証 ─────────────────────────┤
│ [PEQ ファームウェアを選択 (推奨)] [別のファイルを参照...]      │
│ [ALL GOOD] 200,896 B (196.2 KiB) — そのまま更新 OK             │
├ STEP 3. ファームウェア更新を実行 ──────────────────────────────┤
│ [ファームウェア更新を実行] [中断]                              │
│ [████████████████████] 100%  Finished 200896/200896            │
├ 緊急・保守操作（通常のファームウェア更新では不要） ────────────┤
│ [緊急 Factory 3.29 復帰]    [現行イメージ吸出し (保守用)]      │
├ 実行ログ ──────────────────────────────────────────────────────┤
└────────────────────────────────────────────────────────────────┘
```

* デバイス状態は **起動時 + 手動「再読み込み」のみ**。常時ポーリングはしない
  （音声転送への負荷・干渉を避けるため）。
* イメージ選択時に自動検証が走り、`ALL GOOD` / `NG` を色付きで表示する。
* 書込系ボタンは「確認ダイアログ（チェックボックス必須）」＋
  「最終確認 yes/no」の **二重確認**。実行中は全ボタンが無効化される。
* 完了判定は **`state == Finished` かつ `completion == 0`** のみ成功。
  `Finished` でも `completion != 0` ならエラーダイアログと赤字ログで
  `0x........ (TSTATUS_*)` を表示する。
* 転送中（`InProgress` / `EnteringAppMode`）は **「中断」ボタンを自動無効化**し、
  「転送中のため中断できません（完了まで待機してください）」と表示する。
  `Initializing` / `EnteringDfuMode` の間は中断できる。
* すべての DFU 操作は `EndDfuProc` を `finally` で必ず呼ぶ。

---

## 4. 補足：DFU サイズ上限（200 KiB 超の独自ビルドを使う場合）

通常の同梱ファームウェア（`firmware/do400_peq_dfu.bin`, 196.2 KiB）は純正 FW 3.23 / 3.29 の上限（200.0 KiB）に収まっているため、**そのまま 1 回で更新でき、特別な手順は不要** です。

| イメージ | サイズ | 純正 3.23 からの更新 |
|---|---|---|
| `firmware/do400_peq_dfu.bin`（本リポジトリ同梱） | 200,896 B (196.2 KiB) | **そのまま直接更新可**（512 KiB 上限拡張も内蔵） |
| 独自ビルドで 200 KiB を超えるイメージ | 204,801〜524,288 B | 先に 512 KiB 上限拡張入り FW（同梱イメージ等）の導入が必要 |

アプリは選択されたイメージの **core 0 の RAM `0x8AF98`**
（= コンテナ内 `core0.offset + 0xAF98`）を読み、そのイメージ自身が持つ上限を表示する。
`0x00032000` = 200 KiB、`0x00080000` = 512 KiB。

もし自分で 200 KiB を超えるカスタムイメージをビルドして書き込む場合のみ、先に同梱の `do400_peq_dfu.bin`（512 KiB 上限内蔵）を書き込んで再起動させたあと、200 KiB 超のイメージ選択時に自動表示される確認チェックを入れて更新する。

---

## 5. 緊急時の Factory 3.29 復旧手順

アップグレードスロット (image1) の書込みに失敗しても、**Factory イメージ
(image0 / FW 3.29) は DFU では書き換えられない**ため、ローダが自動的に
image0 へフォールバックする。

手動で戻す場合:

1. デバイスを USB 接続したまま、アプリを起動する。
2. 下段「緊急・保守操作」の [緊急 Factory 3.29 復帰] を押す。
3. 確認ダイアログ（チェックボックス）→ 最終確認 yes/no の二重確認を経て実行。
4. `StartDfuRevertToFactoryImage` → `GetDfuStatus` ポーリング → `EndDfuProc`。
5. 再起動後、[再読み込み] で `bcdDevice 0x0329`（FW 3.29）を確認する。

このときアップグレードスロットの内容は消去される（カスタム FW の設定は失われる）。

さらに最終保険として、事前にバックアップしておいた 2 MiB SPI フラッシュ全体のダンプ
（`do400_dump.bin`）の書戻しが使える。ただしこれは
別途 SPI ライタが必要で、本アプリの範囲外。

---

## 6. 事前検証（`fw_validator.py`）の判定基準

ニブルスワップを解除した「論理イメージ」に対して以下を検査する。

| # | 検査 | 不合格時 |
|---|---|---|
| 1 | ファイルサイズ >= 0x74、64 B 倍数 | 書込ロック |
| 2 | Magic `0x00FF51DE` | 書込ロック |
| 3 | Version >= 1（アップグレードイメージ） | 書込ロック |
| 4 | `size == length == 実ファイルサイズ`、4 B 境界 | 書込ロック |
| 5 | Page CRC `xmos_crc(image[0x0C:0x100])` | 書込ロック |
| 6 | Image CRC `xmos_crc(image[0x0C:size])` | 書込ロック |
| 7 | Switch Setup / Core 0 / Core 1 のセクション CRC | 書込ロック |
| 8 | セクションがヘッダ直後から隙間なく連続、末尾 == size | 書込ロック |
| 9 | 2 コア境界 `core1.offset == core0.offset + core0.size` | 書込ロック |
| 10 | DFU 上限ワード (`+0xAF98`) の読み出し・表示 | 警告のみ |
| 11 | サイズ <= 524,288 B | 書込ロック |
| 12 | サイズ > 204,800 B ならブートストラップ必須 | 警告 + チェックボックス要求 |

サイズ区分:

```
<= 204,800 B          → 200 KiB 以下 — 純正 FW 3.23 / 3.29 へ直接書込可能
204,801 .. 524,288 B  → 200 KiB 超 512 KiB 以下 — 先に 512 KiB ブートストラップが必要
>  524,288 B          → 512 KiB 超 — 書込不可 (ロック)
```

CRC は xCORE の `crc32` 命令をビット単位で模した独立実装:

```
hw_crc32(seed, data):  state = (state >> 8) ^ TAB[state & 0xFF] ^ (byte << 24)
xmos_crc(data):        ~hw_crc32(0xFFFFFFFF, data || 00 00 00 00)
page_crc(image):       xmos_crc(image[0x0C:0x100])
image_crc(image):      xmos_crc(image[0x0C:size])
section_crc(body):     xmos_crc(u32(len(body)/4) || body)
```

`src/pack_image1.py` と同一値になることをテストで相互検証している。

---

## 7. 自動テスト（完全オフライン実行）

```bash
python -m unittest -v dfu_app/test_dfu_app.py
```

実機読み取り専用テスト（`TestRealDeviceReadOnly`）は既定では
`DO400_ALLOW_LIVE_RO=1` 未設定のためスキップされ、**USB デバイスを一切オープンしない**。

| グループ | 内容 |
|---|---|
| `TestValidatorKnownImages` | 同梱 `firmware/do400_peq_dfu.bin` の全 CRC・サイズ区分・SHA-256・`pack_image1.py` 相互検証 |
| `TestValidatorRejects` | 1 ビット反転 / ヘッダ破壊 / Magic / Version 0 / 512 KiB 超 / ゴミ / 空 / 切り詰め / 存在しないパス |
| `TestMockDfu` | Download・Upload・Revert の成功/失敗/キャンセル/タイムアウト、`EndDfuProc` 1 回保証 |
| `TestWriteGuard` | `allow_write=False` で書込 API に到達しないこと |
| `TestDeviceInfoParsing` | 構造体パース、BCD バージョン表記 |
| `TestCli` | `--validate` / `--dry-run-flash`（Buffer 経路）/ 排他引数 |
| `TestLoadImageSignature` | `c_wchar_p` バインド・5 引数 StringDescriptor・Buffer シグネチャの回帰テスト |
| `TestBufferStandardPath` | 検証済み bytes がそのまま Buffer へ渡る / 検証 NG は DLL 非到達 |
| `TestDfuResultStrict` | `Finished + completion != 0` の失敗判定、API/GUI 層の転送中キャンセル抑止 |
| `TestStepGuidanceAndUi` | STEP 1→2→3 ガイド状態遷移、256 B 読出し保護判定、オフスクリーン GUI スモークテスト |
| `TestLiveReadOnlyOptIn` | 実機テストが既定でスキップされること |
| `TestRealDeviceReadOnly` | 実機からの読み取り専用確認（**既定スキップ** / `DO400_ALLOW_LIVE_RO=1` 時のみ） |

同梱ファームウェアの検証値:

| ファイル | サイズ | 判定 | Page CRC | Image CRC | DFU 上限 |
|---|---|---|---|---|---|
| `firmware/do400_peq_dfu.bin` | 200,896 B (196.2 KiB) | ALL GOOD / 直接書込可 | `0xFD9A2E77` | `0xD6DFC3E6` | 512 KiB |

---

## 8. トラブルシューティング

| 症状 | 対処 |
|---|---|
| 「ドライバ API に接続できません」 | XMOS ドライバが入っているか、`C:\Program Files\XMOS\XMOS_Audio_Driver\x64\xmos_s_usbaudioapi_x64.dll` が存在するか確認 |
| 「DO400 (152A:85DD) が見つかりません」 | 別の USB ポート / ハブを経由していないか確認。`--list-devices` で一覧を見る |
| 更新ボタンが押せない | 検証が NG、または 200 KiB 超でチェックボックス未入力、または未接続 |
| `TSTATUS_NOT_ALLOWED (0xEE000032)` | 前回の DFU が後始末されていない。アプリを再起動（`EndDfuProc` は自動で呼ばれる） |
| `TSTATUS_NO_IMAGE_LOADED (0xEE001007)` | `LoadFirmwareImage*` より前に `StartDfuDownload` が走った等。通常は起きない（ロード経路は Buffer 固定） |
| DFU 後にデバイスが見えない | 再起動に数秒かかる。USB を抜き差しし、[再読み込み] |
| 書込み後も FW バージョンが変わらない | image1 ではなく image0 で起動している可能性。`--check-device` の `bcdDevice` を確認 |

---

## 9. 制限事項

* Windows x64 専用（DLL が x64）。
* イメージの内容（音声 DSP の設定など）は検証しない。コンテナ構造と CRC のみ。
* 「DFU 上限ワード」は表示のみで、値を書き換えない。
* 吸出し (`StartDfuUpload`) は純正 FW 3.23 の読出し保護により 256 B 全 `0x00` を返す（正常な仕様）。
* GUI の実書込みは手動操作のみ。自動テストは書込系をモックでしか通らない。
