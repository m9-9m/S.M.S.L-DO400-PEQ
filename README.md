# SMSL DO400 10-Band Hardware PEQ

**日本語** | [English](README.en.md)

SMSL DO400（XMOS XU316 / 純正 FW 3.23 ベース）に **0 サンプル追加遅延・10 バンド PCM ハードウェア PEQ** と **ブラウザ WebHID 操作 UI**、**Windows 用 USB DFU 更新ツール** を追加するプロジェクトです。

> [!WARNING]
> **免責事項・対応機種 / Disclaimer & Compatibility**
> 本プロジェクトは **初代 SMSL DO400（VID `152A` / PID `85DD`・純正 FW 3.23 ベース）専用** の非公式カスタムファームウェアおよびツール群です。**DO400 MK2 や他の SMSL 機種には非対応** ですので、別機種へは絶対に書き込まないでください。実機への書込みは自己責任で行ってください。
> なお、USB DFU はアップグレードスロット（`image1`）のみを書き換え、工場出荷ファームウェア（`image0`）は上書きされず保護されます。万一問題が起きた場合は DFU ツールから「緊急 Factory 復帰」で純正状態へ戻せます。

---

## 主な特徴

- **10 バンド PCM ハードウェア PEQ（44.1 kHz 〜 192 kHz）**
  - XMOS XU316 Tile 0 上で I2S 出力直前に 1 サンプル単位で処理するため、**バッファ遅延ゼロ（0 サンプル / 0 ms）**。
  - Q29 Direct Form I バイクワッド × 10 バンド + Q28 プリアンプ + クリップ保護。
  - **PEQ OFF 時は高速純正バイパス**（純正 FW 3.23 とビット完全一致）。
  - **352.8 / 384 / 768 kHz** および **Native DSD**、さらに **DoP（3 層ガード自動検出）** は自動的にビット完全バイパス。
  - **MQA 再生について**: 本 PEQ は **通常 PCM（44.1〜192 kHz）専用** です。Tile 1 の純正 MQA デコーダは無改変のため MQA 再生自体は純正同様に動作しますが、**MQA への PEQ 適用は非対応**です。未展開 MQA（44.1 / 48 kHz PCM として送出される MQA）再生中に PEQ を ON にすると下位ビットの MQA シグナリングが変化して MQA 認証が外れるため、**MQA を純正デコード再生したい場合は PEQ を OFF にしてください**。
- **USB DFU でそのまま直接導入可能（196.2 KiB）**
  - Tile 1 は純正 FW 3.23 と **100% バイト同一（無改変）**、Tile 0 の純正領域改変もわずか **5 箇所（17 B）** のみ（Windows WebHID 互換のための 1 B HID ディスクリプタ修正を含む）。
  - 同梱イメージ `firmware/do400_peq_dfu.bin`（200,896 B）は、純正の 200 KiB 上限内に収まっているため、**事前の準備や複数回の書込みは不要で、そのまま 1 回の USB DFU で導入できます**。
- **ブラウザ WebHID PEQ コンソール（インストール不要）**
  - **オンライン版（GitHub Pages）**: [https://m9-9m.github.io/S.M.S.L-DO400-PEQ/](https://m9-9m.github.io/S.M.S.L-DO400-PEQ/)
  - Chrome / Edge から USB 経由でリアルタイムに 10 バンド PEQ・プリアンプ・ON/OFF・Equalizer APO インポート・周波数特性グラフを操作可能。
- **安全設計（RAM 保持＆ブラウザ自動保存）**
  - **DO400 本体の電源を切ると、本体側の PEQ 設定は初期状態（フラット 0 dB・PEQ OFF）にリセットされます**（本体フラッシュへは書き込まない安全設計）。
  - DO400 の電源が入っている間はブラウザを閉じても PEQ が効き続けます。
  - 作成したカーブは **ブラウザ側（`localStorage`）に自動保存** されるため、DO400 の電源を入れ直した後はコンソールを開いて「接続 → 実機へ安全適用」を押すだけですぐ復元できます。
- **Windows 用ステップ案内付き DFU 更新アプリ（追加 pip 依存ゼロ）**
  - Python 標準ライブラリ（`tkinter` + `ctypes`）のみで動作。Thesycon 公式ドライバ API を直接呼び出し、事前 CRC 検証・転送中キャンセル抑止・二重確認を備えています。

---

## リポジトリ構成

```text
do400-peq/
├── firmware/
│   ├── do400_peq_dfu.bin              # USB DFU ファームウェア (200,896 B / 196.2 KiB)
│   └── SHA256SUMS.txt                 # SHA-256 チェックサム
├── dfu_app/                           # Windows 用 DFU 更新ツール (STEP 1→2→3 ガイド付)
│   ├── launch_dfu_app.bat             # ダブルクリック起動ランチャー
│   ├── do400_dfu_app.py               # GUI / CLI 本体
│   ├── fw_validator.py                # XMOS コンテナ・5 系統 CRC32 検証器
│   ├── thesycon_api.py                # Thesycon DLL (v5.15) ctypes ラッパー
│   └── test_dfu_app.py                # オフライン自動テスト
├── webhid/                            # ブラウザ PEQ コンソール (ローカル HID ブリッジ + WebHID 対応)
│   ├── open_peq_ui.bat                # ローカルサーバー＋HID ブリッジ起動 & ブラウザ自動起動
│   ├── peq_server.py                  # Python 標準ライブラリのみのローカルサーバー＋Windows HID ブリッジ
│   ├── do400_peq_webhid.html          # 単一ファイル版コンソール (GitHub Pages 対応)
│   ├── do400_peq_webhid.template.html # ソーステンプレート
│   ├── peq_core.js                    # フィルタ係数計算・Equalizer APO パーサ・WebHID コーデック
│   ├── build_html.mjs                 # HTML 再生成 / 同期チェック
│   └── test_ui.mjs                    # UI スモークテスト
├── src/                               # ファームウェア生成・梱包・監査スクリプト (開発者向け)
│   ├── build_fw.py                    # Factory 3.23 から Tile 0/1 ELF を生成
│   ├── pack_fw.py                     # DFU コンテナ梱包 & 混入防止自動検証
│   ├── webhid_hooks.py                # XMOS XS3A WebHID フック + DoP ガードアセンブラ生成
│   ├── pack_image1.py                 # XMOS SPI コンテナ / ニブルスワップ / CRC32 ライブラリ
│   ├── peq_ref.py                     # Q29 DF1 10 バンド PEQ ビット厳密リファレンスモデル
│   ├── audit_machine_code.py          # 静的 XS3A 機械語・アライメント監査
│   └── xu_image_an.py                 # オフライン XU316 イメージ解析ツール
└── docs/
    ├── VERIFICATION_REPORT.md         # 設計仕様・静的監査・xsim 12 ケース実測レポート
    └── FLASH_PERSISTENCE_SURVEY.md    # 本体 Flash 保存の逆アセンブル調査メモ (未実装)
```

---

## クイックスタート（Windows）

### 前提条件
- SMSL 公式 USB Audio ドライバ（Thesycon `xmos_s_usbaudioapi_x64.dll`）がインストール済みであること
- Python 3.8 以上（DFU アプリ・ローカル Web サーバー起動用、追加パッケージ不要）
- Google Chrome または Microsoft Edge（WebHID 対応ブラウザ）

### 1. ファームウェアを更新する
1. 音楽再生アプリ・DAW・ブラウザの WebHID タブをすべて閉じます。
2. `dfu_app/launch_dfu_app.bat` をダブルクリックして起動します。
3. 画面上部のガイドに従って進めます：
   - **STEP 1**: DO400 が `接続済み` になっていることを確認（未接続なら `再読み込み`）
   - **STEP 2**: **`PEQ ファームウェアを選択 (推奨)`** をクリックし、判定が `ALL GOOD` になることを確認
   - **STEP 3**: **`ファームウェア更新を実行`** をクリック（確認ダイアログが 2 回表示されます）
4. 書込み完了（約 30 秒）後、DO400 は **自動で再起動** し、数秒後に再認識されます（1 回の更新で完了します）。
   - **書込み中は絶対に電源 OFF・USB ケーブル抜去を行わないでください。**

### 2. PEQ を操作する（WebHID コンソール）
1. オンライン版（[https://m9-9m.github.io/S.M.S.L-DO400-PEQ/](https://m9-9m.github.io/S.M.S.L-DO400-PEQ/)）を Chrome / Edge で開くか、DFU アプリ右上の **`PEQ 設定画面を開く`** または `webhid/open_peq_ui.bat` をダブルクリックします（ローカル起動時は `http://localhost:8136/do400_peq_webhid.html` が開きます）。
   - ※ WebHID はブラウザ仕様上 `https://`（GitHub Pages 等）または `http://localhost` が必要です。
2. 画面の **DO400 に接続 (WebHID)** から SMSL DO400 を選択し、10 バンドの周波数 / ゲイン / Q・プリアンプを調整して **実機へ安全適用** を押します。
3. **電源 OFF 時の動作について**：
   - DO400 本体の電源を落とすと、本体側の PEQ 設定は安全な初期状態（フラット 0 dB・PEQ OFF）にリセットされます。
   - 調整したカーブはブラウザに自動保存されるため、再度電源を入れたときは画面を開いて **DO400 に接続 → 実機へ安全適用** を押すだけで元に戻せます。

---

## テストの実行（完全オフライン・実機非接触）

```bash
# DFU アプリ & ファームウェア検証器テスト (既定では実機に一切アクセスしません)
python -m unittest -v dfu_app/test_dfu_app.py

# 同梱 DFU イメージの事前検証 & 書込みドライラン
python dfu_app/do400_dfu_app.py --validate firmware/do400_peq_dfu.bin
python dfu_app/do400_dfu_app.py --dry-run-flash firmware/do400_peq_dfu.bin

# WebHID コンソール同期確認 & DOM スモークテスト (Node.js 18+)
node webhid/build_html.mjs --check
node webhid/test_ui.mjs
```

---

## ソースからの再現ビルド（開発者向け）

XMOS XTC Tools 15.3.1 とご自身の DO400 からダンプした純正 FW 3.23 SPI イメージ（`do400_dump.bin`）がある場合、ローカルで `do400_peq_dfu.bin` と SPI 直接書込用 2 MiB イメージを再現ビルドできます。

```bash
# 環境変数 XMOS_TOOL_PATH に XTC Tools のパスを設定したうえで実行
python src/build_fw.py --dump /path/to/do400_dump.bin --out-dir build
python src/pack_fw.py --build-dir build --dump /path/to/do400_dump.bin
```

技術詳細・検証結果の全文は [docs/VERIFICATION_REPORT.md](docs/VERIFICATION_REPORT.md) および [dfu_app/README.md](dfu_app/README.md) を参照してください。

---

## ライセンスと権利表記

- 本リポジトリ内の独自コード（`dfu_app/`, `webhid/`, `src/`, `docs/`）は [MIT License](LICENSE) で公開しています。
- `firmware/do400_peq_dfu.bin` は SMSL DO400 純正ファームウェア（FW 3.23 / XMOS XU316）に対する最小パッチ適用済みイメージであり、純正コード部分の権利は原著作権者（SMSL / XMOS）に帰属します。
