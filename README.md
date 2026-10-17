# Nice Logger 88361 — PC 控制程式 (Raspberry Pi 4)

AZ Instrument 88361 藍芽雙溫度記錄器的 PC 控制程式，目標部署平台 **Raspberry Pi 4 + Raspberry Pi OS Lite 64-bit**。

## 架構

```
┌─────────────────────────────────────────────────┐
│ Raspberry Pi 4 (Raspberry Pi OS Lite 64-bit)    │
│   ├─ bluez 5.x (BLE stack)                       │
│   ├─ Python 3.11+ venv                           │
│   │   ├─ FastAPI (HTTP + WebSocket server)       │
│   │   ├─ bleak (BLE client)                      │
│   │   └─ nice_logger/                            │
│   │       ├─ ble_client.py   ← BLE 通訊核心       │
│   │       ├─ api.py          ← REST + WS API     │
│   │       └─ static/index.html ← 網頁 UI         │
│   └─ systemd / OpenClaw exec 自動啟動            │
└─────────────────────────────────────────────────┘
          ↑ HTTP/WS (SSH LocalForward 8000)
          │
   [瀏覽器] http://localhost:8000
```

## 反向工程背景

本程式來自 [Nice Logger Android App (v1.0.025)](https://apkpure.com/) 的反向工程：

- **BLE Service UUID**：`0000fff0-...`
- **Write (App→Device)**：`0000fff1-...`
- **Read/Notify (Device→App)**：`0000fff2-...`
- **App 結構**：Unity 6 + IL2CPP + Innomind (台灣) + Etek 第三方 BLE SDK (深圳)
- **完整逆向分析**：`projects/nice-logger-reverse/` 下的 `REVERSE-REPORT.md` 與 `PROTOCOL-SPEC.md`

## 快速啟動

### 在 Pi 4 上（部署）

```bash
sudo apt install -y bluez python3-pip python3-venv
cd ~/nice-logger
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m nice_logger.api
# 開瀏覽器 (Pi 4 本機或 SSH tunnel): http://localhost:8000
```

### 在開發機 (Windows / macOS / Linux)

```bash
git clone https://github.com/kalapontsai/nice-logger-Pi.git
cd nice-logger-Pi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m nice_logger.api
```

## 網頁 UI 功能

- **📡 設備掃描**：列出附近 BLE 設備（含 RSSI）
- **🌡️ 即時溫度**：T1 環境 + T2 外接測棒
- **⚙️ 設定面板**：設備名稱 / 密碼 / 取樣率 / 啟動延遲 / 警報閾值
- **▶ 開始 / ⏹ 停止 / 📥 下載**：記錄控制
- **📋 封包記錄**：TX/RX hex 即時顯示（debug opcode 用）

## BLE 編碼輔助

`nice_logger/ble_client.py` 內含從 PDF 規格書 + dump.cs 推導的編碼函式：

- `encode_temp_01c(celsius)`：溫度 0.1°C 精度 → 2 bytes little-endian signed
- `encode_interval_seconds(s)`：取樣率 10 秒增量 → 1 byte index
- `encode_start_delay_minutes(m)`：啟動延遲 10 分鐘增量 → 1 byte
- `encode_alarm_delay_minutes(m)`：警報延遲 5 分鐘增量 → 1 byte
- `encode_password(pwd)`：0~9999 密碼 → 2 bytes
- `encode_device_name(name)`：最多 7 字 UTF-8

## 注意事項

⚠️ **`Cmd` enum 的 opcode 數值是推測**，必須動態驗證後才能真正控制 88361。驗證流程見父目錄 `projects/nice-logger-reverse/stage4-protocol-spec/pc-ble-setup.md`。

## 授權

私有研究專案，未經 AZ Instrument Corp. 授權。
