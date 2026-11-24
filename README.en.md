# SMSL DO400 10-Band Hardware PEQ

[日本語](README.md) | **English**

Adds a **zero-latency 10-band PCM hardware PEQ**, a **browser-based WebHID PEQ console**, and a **guided Windows USB DFU updater** to the SMSL DO400 (XMOS XU316, based on stock FW 3.23).

> [!WARNING]
> **Disclaimer & Compatibility**
> This is an unofficial custom firmware and toolset built **exclusively for the original SMSL DO400 (VID `152A` / PID `85DD`, stock FW 3.23 base)**. **DO400 MK2 and all other SMSL models are NOT supported** — do not flash this firmware onto any other model. Flashing your device is at your own risk.
> USB DFU only writes to the upgrade slot (`image1`) and leaves the stock factory image (`image0`) untouched. If anything goes wrong while USB is available, you can restore the stock factory firmware using the "Emergency Revert to Factory" button in the DFU tool.

---

## Features

- **10-Band PCM Hardware PEQ (44.1 kHz – 192 kHz)**
  - Runs per-sample on XMOS XU316 Tile 0 right before I2S output with **zero added buffer latency (0 samples / 0 ms)**.
  - Q29 Direct Form I biquads × 10 bands + Q28 preamp + saturation clip guard (+36 dB internal headroom, 64-bit accumulator with error-feedback residue carry).
  - **Bit-exact fast bypass when PEQ is OFF** (100% identical to stock FW 3.23).
  - **352.8 / 384 / 768 kHz PCM**, **Native DSD**, and **DoP** (automatic 3-layer `0x05`/`0xFA` marker detection) are automatically bit-exact bypassed.
  - **MQA Note**: Hardware PEQ supports **standard PCM (44.1–192 kHz) only** and does **not** apply PEQ to MQA decoding/rendering. Tile 1's stock MQA decoder is completely unmodified so stock MQA playback continues to work normally; however, enabling PCM PEQ while playing an unfolded/encoded MQA stream (sent as 44.1 / 48 kHz PCM) alters the embedded MQA signaling bits and prevents MQA authentication. **Turn PEQ OFF (bit-exact bypass) when you want native MQA decoding.**
- **Direct Single-Flash USB DFU (196.2 KiB)**
  - Tile 1 is **100% byte-identical to stock FW 3.23**; Tile 0 modifies only **5 sites (17 bytes)** in stock regions (including a 1-byte HID Report Descriptor fix for native Windows WebHID support).
  - The included image `firmware/do400_peq_dfu.bin` (200,896 B) fits inside the stock 200 KiB DFU limit, so it flashes **directly in one normal USB DFU operation** with no extra prerequisite steps.
- **Browser WebHID PEQ Console (Zero Install, JP / EN Toggle)**
  - **Live Web App (GitHub Pages)**: [https://m9-9m.github.io/S.M.S.L-DO400-PEQ/](https://m9-9m.github.io/S.M.S.L-DO400-PEQ/)
  - Control all 10 bands, preamp, PEQ ON/OFF, frequency-response graph, and Equalizer APO import directly from Chrome or Edge.
- **Volatile Device RAM + Browser Auto-Save**
  - **Turning off the DO400 resets the on-device PEQ to Flat 0 dB / OFF** (the firmware never writes to SPI flash during playback, ensuring safe recovery on power cycle).
  - While the DO400 remains powered on, PEQ stays active even after you close the browser.
  - Your curve is **automatically saved in the browser (`localStorage`)** and can also be exported to JSON. After power-cycling the DO400, simply open the console and click **Connect → Safe Apply** to restore it.
- **Guided Windows DFU Updater (Zero pip Dependencies, JP / EN Toggle)**
  - Built purely on the Python standard library (`tkinter` + `ctypes`). Calls the official Thesycon USB Audio API directly with full offline CRC validation, mid-transfer cancel lock, and double confirmation.

---

## Repository Layout

```text
do400-peq/
├── README.md                          # Japanese README
├── README.en.md                       # English README (this file)
├── firmware/
│   ├── do400_peq_dfu.bin              # USB DFU firmware image (200,896 B / 196.2 KiB)
│   └── SHA256SUMS.txt                 # SHA-256 checksum
├── dfu_app/                           # Guided Windows DFU updater (JP / EN toggle)
│   ├── launch_dfu_app.bat             # Double-click launcher
│   ├── do400_dfu_app.py               # GUI & CLI entrypoint
│   ├── fw_validator.py                # Offline XMOS container & 5x CRC32 validator
│   ├── thesycon_api.py                # Thesycon DLL (v5.15) ctypes wrapper
│   └── test_dfu_app.py                # Offline automated test suite
├── webhid/                            # Browser PEQ console (Local HID bridge + WebHID, JP / EN)
│   ├── open_peq_ui.bat                # Local server + Windows HID bridge & browser launcher
│   ├── peq_server.py                  # Zero-dependency Python local server + Windows HID bridge
│   ├── do400_peq_webhid.html          # Single-file console (GitHub Pages ready)
│   ├── do400_peq_webhid.template.html # Source template
│   ├── peq_core.js                    # Biquad Q29 math, Equalizer APO parser, WebHID codec
│   ├── build_html.mjs                 # Rebuilds / --check verifies single-file HTML
│   └── test_ui.mjs                    # Automated DOM & UI smoke test
├── src/                               # Firmware source, packer & audit scripts
│   ├── build_fw.py                    # Builds Tile 0/1 ELFs from Factory 3.23
│   ├── pack_fw.py                     # Offline DFU/Flash packer + anti-contamination checks
│   ├── webhid_hooks.py                # XMOS XS3A WebHID hooks + 3-layer DoP guard
│   ├── pack_image1.py                 # XMOS SPI boot container / CRC32 / nibble-swap library
│   ├── peq_ref.py                     # Bit-exact Q29 DF1 10-band Python reference model
│   ├── audit_machine_code.py          # Static XS3A machine-code & alignment verifier
│   └── xu_image_an.py                 # Offline XU316 image analyzer
└── docs/
    ├── VERIFICATION_REPORT.md         # Architecture, static audit & 12-case xsim results
    └── FLASH_PERSISTENCE_SURVEY.md    # Reverse-engineering notes for on-device Flash save (research only)
```

---

## Quick Start (Windows)

### Prerequisites
- Official SMSL / XMOS USB Audio driver installed (`xmos_s_usbaudioapi_x64.dll`)
- Python 3.8+ (standard library only, no `pip install` needed)
- Google Chrome or Microsoft Edge (WebHID-capable browser)

### 1. Flash the Firmware
1. Close any music players, DAWs, and browser WebHID tabs.
2. Double-click `dfu_app/launch_dfu_app.bat` (you can switch between Japanese and English via the **EN / 日本語** button at the top right).
3. Follow the on-screen **STEP 1 → STEP 2 → STEP 3** guide:
   - **STEP 1**: Verify DO400 shows `Connected` (click `Refresh` if needed).
   - **STEP 2**: Click **`Select PEQ Firmware (Recommended)`** and confirm the verdict is `ALL GOOD`.
   - **STEP 3**: Click **`Flash Firmware`** (two confirmation prompts will appear).
4. After flashing finishes (~30 seconds), the DO400 **automatically reboots** and reconnects in a few seconds.
   - **Never power off the DAC or unplug the USB cable while flashing is in progress.**

### 2. Configure PEQ (WebHID Console)
1. Open the live console at [https://m9-9m.github.io/S.M.S.L-DO400-PEQ/](https://m9-9m.github.io/S.M.S.L-DO400-PEQ/) in Chrome / Edge, or click **`Open PEQ Console`** at the top right of the DFU app / double-click `webhid/open_peq_ui.bat` (local server opens `http://localhost:8136/do400_peq_webhid.html`; switch language with the **EN / 日本語** button at the top right).
   - *Note*: WebHID requires `https://` (such as GitHub Pages) or `http://localhost`.
2. Click **Connect DO400 (WebHID)**, adjust your 10 bands / preamp, and click **Safe Apply to Device**.
3. **Power-off behavior**:
   - Powering off the DO400 resets on-device PEQ to Flat 0 dB / OFF.
   - Your settings are auto-saved in the browser; after powering the DO400 back on, open the console and click **Connect → Safe Apply to Device** to restore them.

---

## Running Offline Tests (Zero Hardware Access)

```bash
# DFU updater & firmware validator tests (never touches real hardware by default)
python -m unittest -v dfu_app/test_dfu_app.py

# Validate the included DFU image & run a mock flash dry-run
python dfu_app/do400_dfu_app.py --validate firmware/do400_peq_dfu.bin
python dfu_app/do400_dfu_app.py --dry-run-flash firmware/do400_peq_dfu.bin

# WebHID console sync check & DOM smoke test (Node.js 18+)
node webhid/build_html.mjs --check
node webhid/test_ui.mjs
```

---

## Reproducible Build from Source (Developers)

With XMOS XTC Tools 15.3.1 and your own 2 MiB stock FW 3.23 SPI flash dump (`do400_dump.bin`), you can reproducibly rebuild both `do400_peq_dfu.bin` and a full 2 MiB SPI flash image locally:

```bash
# Set XMOS_TOOL_PATH to your XTC Tools installation directory, then run:
python src/build_fw.py --dump /path/to/do400_dump.bin --out-dir build
python src/pack_fw.py --build-dir build --dump /path/to/do400_dump.bin
```

---

## License & Third-Party Notice

- All original code in this repository (`dfu_app/`, `webhid/`, `src/`, `docs/`) is licensed under the [MIT License](LICENSE).
- `firmware/do400_peq_dfu.bin` is a minimally patched image derived from SMSL DO400 stock firmware (FW 3.23 / XMOS XU316); all rights to the stock firmware code belong to their respective copyright holders (SMSL / XMOS).
