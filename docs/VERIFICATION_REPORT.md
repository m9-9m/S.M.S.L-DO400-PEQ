# SMSL DO400 Hardware PEQ ファームウェア 設計・検証レポート

## 1. アーキテクチャ概要

純正 FW 3.23（XMOS XU316 / 2 タイル構成）を直接ベースとし、純正動作への影響を最小限に抑えた構成です。

| 項目 | 仕様 |
|---|---|
| **ベースファームウェア** | SMSL DO400 純正 FW 3.23（`image1` Tile 0 + Tile 1） |
| **Tile 1 変更箇所** | **0 箇所（純正 FW 3.23 と 100% バイト同一）** |
| **Tile 0 純正領域の変更箇所** | **5 箇所（計 17 B）のみ**：`0x80908` (WebHID SET), `0x809B4` (WebHID GET), `0x82B34` (PCM PEQ ゲート), `0x8AF98` (DFU 上限 512 KiB 化), `0x8B3EB` (HID Feature Report `0x01`→`0x02` Data,Var,Abs / Windows WebHID 対応) |
| **USB EP0 (`XUD_*`) / 初期レート** | **100% 純正のまま**（44.1 kHz 起動） |
| **PCM PEQ 処理** | 10 バンド Q29 Direct Form I + Q28 プリアンプ（44.1〜192 kHz 連続動作、追加遅延 0 サンプル）。352.8 / 384 / 768 kHz・Native DSD・PEQ OFF 時は高速純正バイパス |
| **DoP 保護** | 3 層ガード（`>= 176.4 kHz` ゲート + 16 ペア厳密 `0x05`/`0xFA` 交互検出 + `DOP_HOLD=32` ビット完全バイパス & 自動復帰） |
| **WebHID プロトコル** | `PEQ1` v1（`WH_BUILD=0x38000101`）、`SET_ENABLE` 全項目事前検証による原子的更新 |
| **DFU コンテナサイズ** | **200,896 B（196.19 KiB < 200 KiB）** — 純正 200 KiB 制限下で **そのまま直接 DFU 書込可能**（512 KiB 上限拡張も内蔵） |

---

## 2. コンテナ梱包・混入防止の自動検証

`src/build_fw.py` および `src/pack_fw.py` により、出荷 DFU コンテナ自体に対して以下を自動検証しています：

- `Core 1 == 純正 FW 3.23 img1_tile1.bin`（114,964 B 100% バイト一致）
- `Core 0` 純正区間 `0x80000..0x8EBAC` の差分は **`{0x80908, 0x809B4, 0x82B34, 0x8AF98, 0x8B3EB}` の 5 箇所のみ**
- USB EP0 関連の全純正関数（11 箇所）が純正 FW 3.23 と 100% バイト一致
- Tile 0 BSS 領域 `0x8EBAC..0x91000` が全 `0x00`
- ブートデフォルト: `PCM_ON=0`（安全バイパス起動）, `DFU_LIMIT=0x00080000`, `WH_BUILD=0x38000101`, `DOP_RATE_MIN=176400`, `DOP_THRESH=16`, `PEQ_RATE_MAX=192000`
- `xflash --analyze --noinq --factory-version 15.1`: **11 GOOD / 0 FAIL**
- `dfu_app/fw_validator.py`: **ALL GOOD（失敗 0 / 警告 0 / 直接書込可能）**
- クリーン再ビルド決定論: 2 回ビルドで DFU / 2 MiB Flash とも **SHA-256 完全一致**

---

## 3. 静的機械語監査 & `xsim` 12 ケース実測結果

- **静的機械語監査** (`src/audit_machine_code.py`): 全 PASS（4 バイト分岐先一致、32-bit 命令の 2 mod 4 不整合ゼロ、禁止命令ゼロ、`SET_ENABLE` 原子性確認、`xobjdump --split` ELF 一致）。
- **`xsim` 12 ケース実測**（純正 44.1 kHz 起動 + 非ゼロ実音声で全件リファレンス照合）: **12/12 PASS**

| ケース | 判定 | 実測値 |
|---|---|---|
| `bypass_off` | **PASS** | 非ゼロ音声 794 ペアが `PCM_ON=0` 高速純正バイパス通過（`seq=0`, `dec=0`, trap なし） |
| `pcm44_on_long` | **PASS** | 1,711 ペア連続（非ゼロ 557 ペア）フィルタ動作・失効なし・**誤差 0/0 LSB** |
| `rate_change_reset` | **PASS** | 非ゼロ音声中の 44.1→96 kHz 切替で自動履歴クリア（2 回）・**誤差 0/0 LSB** |
| `rate_ceiling_384k` | **PASS** | 384 kHz で高速純正バイパス（`during384=0`）→ 96 kHz 復帰で自動再開・**誤差 0/0 LSB** |
| `mono_f1_44k` | **PASS** | 44.1 kHz -12 dBFS モノラル正弦波 `DOP_HITS=0`・**誤差 0/0 LSB** |
| `mono_f1_176k` | **PASS** | 176.4 kHz -27 dBFS モノラル正弦波 `DOP_HITS=0`・**誤差 0/0 LSB** |
| `dop16_recover` | **PASS** | 176.4 kHz 24 ペア DoP：`#16` で発火（`hits=9`, `held=40`）→ DoP ビット完全通過 → 非ゼロ 422 ペア自動復帰・**誤差 0 LSB** |
| `dop15_notrigger` | **PASS** | 176.4 kHz 12 ペア DoP（<16）：`hits=0`・**誤差 0/0 LSB** |
| `dop44_gate` | **PASS** | 44.1 kHz 24 ペア DoP：レートゲート抑止 `hits=0`・**誤差 0/0 LSB** |
| `hid_proto_atomic` | **PASS** | 31/31 全応答チェックサム一致、`WH_BUILD=0x38000100`、複合不正 `SET_ENABLE` が `status=1` かつ `PCM_ON=0` 維持（完全原子性）、非 `PEQ1`/短長フォールバック確認 |
| `hid_js_crosscheck` | **PASS** | ブラウザ UI (`webhid/peq_core.js`) 生成パケット 6/6 全 PASS |
| `hid_live_audio` | **PASS** | 非ゼロ音声中に WebHID で係数書換→ON→293 ペア **0/0 LSB**→`CLEAR_HISTORY` 自動リセット→OFF 高速バイパス復帰 |

---

## 4. バイナリチェックサム（SHA-256）

| 用途 | パス | サイズ | SHA-256 |
|---|---|---|---|
| **USB DFU 用イメージ（本リポジトリ同梱）** | `firmware/do400_peq_dfu.bin` | 200,896 B (196.19 KiB) | `ce93b58089fae5a1c7894050cd6cc9440929b558a10501fe5667bd40ba74c7fd` |
| SPI 直接書込用 2 MiB Flash（`src/pack_fw.py --dump` でローカル生成） | `build/out/do400_peq_flash_2mb.bin` | 2,097,152 B | `17b698837fa6cda8f9aa5784eb8ddb74de097a7daaa6d794bcf34510a35e05cf` |
