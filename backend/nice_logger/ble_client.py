"""
Nice Logger 88361 BLE 通訊核心
================================
Base on reverse engineering of Nice Logger App (v1.0.025)
- BLE Service: 0xFFF0
- Write: 0xFFF1 (App → Device)
- Read/Notify: 0xFFF2 (Device → App)

架構:
  - BleClient (這個檔): 純 BLE 通訊, 事件 callback 模式
  - BleState: 共享狀態物件, 給 FastAPI / WebSocket / Qt UI 訂閱
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.characteristic import BleakGATTCharacteristic
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:
    raise ImportError("請先 pip install bleak")


# ===== BLE UUIDs (來自 SampleGattAttributes.java) =====
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
READ_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb"  # standard notify CCCD


# ===== 推測的封包格式 (從 PDF 規格書) =====
def encode_temp_01c(celsius: float) -> bytes:
    """0.1°C 精度溫度 → int16 little-endian"""
    encoded = int(round(celsius * 10))
    encoded = max(-300, min(700, encoded))
    return encoded.to_bytes(2, "little", signed=True)


def decode_temp_01c(data: bytes) -> float:
    """int16 little-endian → °C"""
    return int.from_bytes(data, "little", signed=True) / 10.0


def encode_interval_seconds(seconds: int) -> bytes:
    """取樣率 10 秒增量 → 1 byte index"""
    if not (10 <= seconds <= 7200) or (seconds - 10) % 10 != 0:
        raise ValueError("取樣率必須 10~7200 秒且為 10 的倍數")
    return bytes([(seconds - 10) // 10])


def encode_start_delay_minutes(minutes: int) -> bytes:
    """啟動延遲 10 分鐘增量 → 1 byte index"""
    if not (0 <= minutes <= 1440) or minutes % 10 != 0:
        raise ValueError("啟動延遲必須 0~1440 分鐘, 10 分鐘增量")
    return bytes([minutes // 10])


def encode_alarm_delay_minutes(minutes: int) -> bytes:
    """警報延遲 5 分鐘增量 → 1 byte index"""
    if not (0 <= minutes <= 720) or minutes % 5 != 0:
        raise ValueError("警報延遲必須 0~720 分鐘, 5 分鐘增量")
    return bytes([minutes // 5])


def encode_password(pwd: int) -> bytes:
    """0~9999 密碼 → 2 bytes"""
    if not (0 <= pwd <= 9999):
        raise ValueError("密碼 0~9999")
    return bytes([(pwd >> 8) & 0xFF, pwd & 0xFF])


def encode_device_name(name: str) -> bytes:
    """最多 7 字 UTF-8 設備名稱"""
    return name.encode("utf-8")[:7].ljust(7, b"\x00")


def hex_str(b: bytes) -> str:
    """bytes → "AA,BB,CC,..." 顯示格式 (跟 Nice Logger App 一致)"""
    return ",".join(f"{x:02X}" for x in b)


# ===== 解碼邏輯 (從 libil2cpp.so 反組譯 ParseData 推出) =====
# 溫度閾值 (signed 16-bit little-endian)
TEMP_NORMAL_MAX = 0x7F9A   # <= 0x7F9A 為正常: temp = raw / 10
TEMP_SPECIAL = 0x7F9B      # 0x7F9B 為保留/特殊值
# 濕度閾值 (signed 24-bit, 反組譯中用 0x7FFF9C / 0x7FFFFF 比較)
HUMID_NORMAL_MAX = 0x7FFF9C
# ParseData 欄位索引 (從 RVA 0xE1B9D0 反組譯)
# 索引 0xF=15 T1, 0x10=16, 0x11=17 濕度大值, 0x14=20 電池, 0x15=21 濕度
# 0x19=25 T2, 0x1B=27 T3, 0x1D=29 T4
TEMP_FIELD_INDICES = {0x0F: "T1", 0x19: "T2", 0x1B: "T3", 0x1D: "T4"}
HUMID_FIELD_INDICES = {0x11: "RH1", 0x15: "RH2"}
BATTERY_FIELD_INDEX = 0x14


def decode_signed_temp(raw: int) -> dict:
    """解碼一個 16-bit 溫度原始值

    規則 (從 RVA 0xE1BC9C / 0xE1BE2C 反組譯):
        raw > 0x7F9C → 0x7FFF - raw (反向編碼, 仍為帶正負號值)
        0x7F9B       → 保留/特殊值
        raw <= 0x7F9A → raw / 10.0
    """
    if raw > 0x7F9C:
        # 反組譯路徑: sub w9, w9, w0 → w9 = 0x7FFF - w0
        val = 0x7FFF - raw
        return {"kind": "temp", "raw": raw, "value": val / 10.0, "unit": "°C",
                "note": "reverse-coded (>0x7F9C)"}
    if raw == TEMP_SPECIAL:
        return {"kind": "temp", "raw": raw, "value": None, "unit": "°C",
                "note": "special (0x7F9B)"}
    return {"kind": "temp", "raw": raw, "value": raw / 10.0, "unit": "°C",
            "note": "normal"}


def decode_signed_humid(raw: int) -> dict:
    """解碼濕度原始值 (來自 RVA 0xE1BD7C / 0xE1BEB4)

    規則:
        raw >= 0x7FFF9C → 0x7FFFFF - raw (反向編碼)
        否則           → raw / 10.0
    """
    if raw >= HUMID_NORMAL_MAX:
        val = 0x7FFFFF - raw
        return {"kind": "humid", "raw": raw, "value": val / 10.0, "unit": "%RH",
                "note": "reverse-coded"}
    return {"kind": "humid", "raw": raw, "value": raw / 10.0, "unit": "%RH",
            "note": "normal"}


def _parse_csv_field(s: str) -> int | None:
    """ParseData 的 field 會以 int32 解析 (來自 bl Parse 指令)

    接受 0x-prefixed hex 或十進位。
    """
    s = s.strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s, 16) if all(c in "0123456789abcdefABCDEF" for c in s) else int(s)
    except (ValueError, TypeError):
        return None


def decode_notify_csv(csv_text: str) -> dict:
    """解碼 Android ParseData 接收的逗號分隔字串

    來源: RVA 0xE1B9D0 ParseData()
    輸入範例: "01,02,03,...,0x00C8,0x005A,0x0001,...,0x0F9C,0x0001"
    """
    fields = [f for f in csv_text.split(",") if f]
    result: dict = {
        "format": "android_csv",
        "field_count": len(fields),
        "temperatures": {},
        "humidities": {},
        "battery": None,
    }
    for idx, label in TEMP_FIELD_INDICES.items():
        if idx < len(fields):
            raw = _parse_csv_field(fields[idx])
            if raw is not None:
                result["temperatures"][label] = decode_signed_temp(raw)
    for idx, label in HUMID_FIELD_INDICES.items():
        if idx < len(fields):
            raw = _parse_csv_field(fields[idx])
            if raw is not None:
                result["humidities"][label] = decode_signed_humid(raw)
    if BATTERY_FIELD_INDEX < len(fields):
        raw = _parse_csv_field(fields[BATTERY_FIELD_INDEX])
        if raw is not None:
            result["battery"] = {"raw": raw, "value": raw / 10.0, "unit": "%"}
    return result


def decode_packet(entry: dict) -> dict:
    """解碼單一封包記錄, 附加到 entry["decoded"]

    規則:
      - TX: 第一個 byte 為 opcode, 對應 Cmd enum
      - RX:
          * 如果是逗號分隔的 ASCII (Android BLE plugin 格式), 用 decode_notify_csv
          * 否則當 raw bytes 解讀: type(1) + payload
    """
    hex_data = entry.get("hex", "")
    direction = entry.get("dir", "")
    decoded: dict = {"status": "ok", "kind": "unknown"}

    # 解析 hex 成 bytes
    try:
        parts = [p.strip() for p in hex_data.split(",") if p.strip()]
        raw_bytes = bytes(int(p, 16) for p in parts)
    except (ValueError, TypeError):
        decoded.update({"status": "unparsed", "reason": "non-hex"})
        return decoded

    if not raw_bytes:
        decoded.update({"status": "empty"})
        return decoded

    opcode = raw_bytes[0]
    payload = raw_bytes[1:]

    if direction == "TX":
        # 識別命令
        cmd_name = "UNKNOWN"
        for c in Cmd:
            if c.value == opcode:
                cmd_name = c.name
                break
        decoded.update({
            "kind": "command",
            "opcode": f"0x{opcode:02X}",
            "cmd_name": cmd_name,
            "payload_len": len(payload),
            "payload_hex": hex_str(payload) if payload else "",
        })
        # 嘗試解讀部分 payload 欄位
        interpreted: list[dict] = []
        if cmd_name in ("SET_DEVICE_NAME",) and len(payload) >= 7:
            name = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
            interpreted.append({"name": "device_name", "value": name})
        elif cmd_name in ("SET_PASSWORD",) and len(payload) >= 2:
            pwd = (payload[0] << 8) | payload[1]
            interpreted.append({"name": "password", "value": pwd})
        elif cmd_name in ("SET_SAMPLE_INTERVAL",) and payload:
            idx = payload[0]
            interpreted.append({"name": "interval_index", "value": idx,
                                "seconds": 10 + idx * 10})
        elif cmd_name in ("SET_START_DELAY",) and payload:
            interpreted.append({"name": "start_delay_min", "value": payload[0] * 10})
        elif cmd_name in ("SET_ALARM_DELAY",) and payload:
            interpreted.append({"name": "alarm_delay_min", "value": payload[0] * 5})
        elif cmd_name in ("SET_ALARM_THRESHOLD",) and len(payload) >= 4:
            hi = decode_signed_temp(int.from_bytes(payload[0:2], "little", signed=True))
            lo = decode_signed_temp(int.from_bytes(payload[2:4], "little", signed=True))
            interpreted.append({"name": "alarm_high", **hi})
            interpreted.append({"name": "alarm_low", **lo})
        if interpreted:
            decoded["interpreted"] = interpreted
        return decoded

    # RX 路徑
    decoded["kind"] = "notify"
    decoded["payload_len"] = len(payload)
    decoded["payload_hex"] = hex_str(payload) if payload else ""

    # 嘗試判斷是否為逗號分隔 ASCII 字串
    if len(raw_bytes) > 4 and all(32 <= b < 127 or b in (0x2C, 0x0A, 0x0D) for b in raw_bytes):
        try:
            text = raw_bytes.decode("ascii", errors="replace")
            fields = [f for f in text.split(",") if f]
            if len(fields) >= 16:  # 至少要能拿到 T1
                csv_result = decode_notify_csv(text)
                decoded.update(csv_result)
                return decoded
        except Exception:
            pass

    # 降級: 顯示 raw bytes 結構
    decoded["status"] = "partial"
    decoded["bytes"] = [f"0x{b:02X}" for b in raw_bytes[:20]]
    if len(raw_bytes) > 20:
        decoded["bytes_truncated"] = True
    return decoded


# ===== 真實的 opcode enum (從 libil2cpp.so 反組譯得出) =====
# 來源: DeviceManager.SendSetXxx 函式內部 mov w1, #imm 立即值
# 反組譯時間: 2026-10-19, il2cppdumper 6.7.46 + capstone
# 重要: 真實 wire format 是 type(1B) + checkType(1B) + payload(N B)
# checkType 在同 type 下作為子命令 (例如 ReadOut 0x64 分兩階段: ct=0 / ct=0xFFFF)
class Cmd(Enum):
    """真實的 BLE 命令 opcode (從 ARM 機器碼反組譯)

    這些值來自 DeviceManager.SendSetXxx 函式內部:
        mov w1, #0x??  ; type
        mov w2, #0x??  ; checkType
        b InternalSendData

    重要: type 和 checkType 是不同 byte, 完整封包是:
        [type] [checkType] [payload bytes]
    """
    # 設定類 (從 SendSet* 反組譯: type == checkType)
    SET_CONFIGURE = 0x41          # SendSetConfigure
    SET_DEVICE_TIME = 0x43       # SendSetDeviceTime
    SET_LOGGER_ID = 0x49         # SendSetID
    SET_UNIT_AND_PASSWORD = 0x50 # SendSetUnitAndPassword
    SET_CONFIGURE_ALT = 0x53     # SendSetConfigure (alias)
    SET_THRESHOLD = 0x58         # SendSetThreshold
    # 細項 set* (對應到 SET_CONFIGURE / SET_THRESHOLD 內部欄位)
    SET_SAMPLE_INTERVAL = 0x41   # alias SET_CONFIGURE
    SET_START_DELAY = 0x41       # alias SET_CONFIGURE
    SET_ALARM_DELAY = 0x41       # alias SET_CONFIGURE
    SET_ALARM_TYPE = 0x41        # alias SET_CONFIGURE
    SET_ALARM_THRESHOLD = 0x58   # alias SET_THRESHOLD
    SET_DEVICE_NAME = 0x41       # alias SET_CONFIGURE
    SET_PASSWORD = 0x53          # alias SET_UNIT_AND_PASSWORD
    SET_START_MODE = 0x41        # alias SET_CONFIGURE
    SET_STOP_MODE = 0x41         # alias SET_CONFIGURE
    SET_UNIT = 0x53              # alias SET_UNIT_AND_PASSWORD
    SET_TIME = 0x43              # alias SET_DEVICE_TIME
    SAVE_CONFIG = 0x41           # alias SET_CONFIGURE
    # 控制類 (SendLoggerState/ReadOut/Query)
    LOGGER_STATE = 0x58          # SendLoggerState(bool) - type=ct=0x58, payload diff for start/stop
    READOUT = 0x64               # ReadOut - type=0x64, ct=0 (phase1) / ct=0xFFFF (phase2)
    QUERY_CONFIGURE = 0x43       # QueryConfigure
    QUERY_ALARM_THRESHOLD = 0x43 # QueryAlarmThreshold
    # 讀取類 (尚未在反組譯中找到, 仍需驗證)
    READ_LIVE = 0x40  # 推測 (未驗證)


# ===== 資料模型 =====
@dataclass
class DeviceInfo:
    """掃描到的設備"""
    address: str
    name: str
    rssi: int
    last_seen: datetime = field(default_factory=datetime.now)
    m_scancode_hex: str = ""  # 廣播 manufacturer data (hex string, JSON 安全)
    manufacturer_id: Optional[int] = None  # 公司 ID (e.g. 0x5002 = AZ Instrument)
    service_uuids: list[str] = field(default_factory=list)
    is_nice_logger: bool = False  # 推測為 88361 (mfg 0x5002 + name match)


@dataclass
class LiveData:
    """即時溫度資料"""
    temp_t1: float  # 環境
    temp_t2: float  # 外接測棒
    timestamp: datetime = field(default_factory=datetime.now)
    battery: Optional[int] = None


@dataclass
class ConfigPayload:
    """設定資料 (對映 DeviceInfo.Configure)"""
    alarm_type: int = 0
    starting_mode: int = 1
    stop_mode: int = 1
    sampling_rate_seconds: int = 60
    start_delay_minutes: int = 0
    alarm_delay_minutes: int = 0
    alarm_high_c: float = 70.0
    alarm_low_c: float = -30.0
    device_name: str = ""
    password: int = 0
    unit_metric: bool = True


# ===== 全域狀態 (給 FastAPI / WebSocket / UI 訂閱) =====
class BleState:
    """共享狀態 + pub/sub 事件"""
    def __init__(self):
        self.devices: list[DeviceInfo] = []
        self.connected: Optional[DeviceInfo] = None
        self.live: Optional[LiveData] = None
        self.last_rx: Optional[datetime] = None
        self.config: Optional[ConfigPayload] = None
        self._listeners: list[Callable] = []
        self._lock = asyncio.Lock()
        # 封包歷史 (debug 用, 最近 200 筆)
        self.packet_log: list[dict] = []
        self._max_log = 200
        # BLE opcode 探測結果
        self.probe_summary: Optional[dict] = None

    def subscribe(self, callback: Callable):
        self._listeners.append(callback)

    def unsubscribe(self, callback: Callable):
        if callback in self._listeners:
            self._listeners.remove(callback)

    async def _emit(self, event: str, data=None):
        for cb in list(self._listeners):
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb(event, data)
                else:
                    cb(event, data)
            except Exception as e:
                logging.exception(f"Listener 錯誤: {e}")

    def log_packet(self, direction: str, hex_data: str, note: str = ""):
        entry = {
            "time": datetime.now().isoformat(timespec="milliseconds"),
            "dir": direction,
            "hex": hex_data,
            "note": note,
        }
        # 嘗試解碼 (best-effort, 失敗不影響 log)
        try:
            entry["decoded"] = decode_packet(entry)
        except Exception as e:
            entry["decoded"] = {"status": "error", "reason": str(e)}
        self.packet_log.append(entry)
        if len(self.packet_log) > self._max_log:
            self.packet_log = self.packet_log[-self._max_log:]


# ===== 核心 BLE 客戶端 =====
class NiceLoggerBleClient:
    """
    88361 BLE 客戶端
    用法:
        client = NiceLoggerBleClient(state)
        await client.scan(timeout=10)
        await client.connect("AA:BB:CC:DD:EE:FF")
        await client.set_config(ConfigPayload(...))
    """
    def __init__(self, state: BleState):
        self.state = state
        self._client: Optional[BleakClient] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def scan(self, timeout: float = 10.0) -> list[DeviceInfo]:
        """掃描附近 BLE 設備

        Bleak 0.21+ / 3.0+ API 變更:
          - discover() 回傳 List[BLEDevice] (沒有 rssi)
          - discover(return_adv=True) 回傳 dict[address, (BLEDevice, AdvertisementData)]
        """
        logging.info(f"掃描中 {timeout}s ...")
        self.state.devices.clear()
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
        # Bleak 3.0+ 回傳 dict[address_str, (BLEDevice, AdvertisementData)]
        # 但舊版 (0.19) 回傳 list[tuple]; 都支援
        if isinstance(discovered, dict):
            items = list(discovered.values())
        else:
            items = list(discovered)
        results = []
        for item in items:
            # item 可能是 (device, adv_data) 或只有 device (容錯)
            if isinstance(item, tuple) and len(item) == 2:
                d, adv = item
                rssi = adv.rssi
                name = d.name or adv.local_name or "(未命名)"
                mfg_id = None
                mfg_data = b""
                if adv.manufacturer_data:
                    # 取第一個 (通常只會有一個)
                    mfg_id, mfg_data = next(iter(adv.manufacturer_data.items()))
                svc_uuids = [str(u) for u in (adv.service_uuids or [])]
                # 猜測是否為 88361: AZ Instrument (0x5002) + name 含 Nice
                is_nice = (
                    mfg_id == 0x5002 or
                    (name and "nice" in name.lower())
                )
            else:
                d = item
                rssi = 0
                name = d.name or "(未命名)"
                mfg_id = None
                mfg_data = b""
                svc_uuids = []
                is_nice = bool(name and "nice" in name.lower())
            info = DeviceInfo(
                address=d.address,
                name=name,
                rssi=rssi,
                m_scancode_hex=mfg_data.hex() if mfg_data else "",
                manufacturer_id=mfg_id,
                service_uuids=svc_uuids,
                is_nice_logger=is_nice,
            )
            results.append(info)
            self.state.devices.append(info)
        await self.state._emit("scan_done", results)
        return results

    async def connect(self, address: str) -> bool:
        """連線到指定 MAC

        重要: AZ Instrument 88361 需要先配對 (pair) 才能 connect.
        bleak 3.x API: pair 是 BleakClient.__init__ 的參數, 不是 connect() 的.
        另外必須先做 GATT service discovery, 否則 write_gatt_char 會出
        "Service Discovery has not been performed yet" 錯誤.
        """
        if self._client and self._client.is_connected:
            await self.disconnect()
        # pair=True 在 __init__ 內設, 讓 connect 自動走 SSP/Just-Works pairing
        # 不指定 services, 讓 bleak 跑預設 discovery
        self._client = BleakClient(
            address, pair=True, timeout=20.0
        )
        self._loop = asyncio.get_event_loop()
        try:
            await self._client.connect()
            if not self._client.is_connected:
                return False
            # 明確手動 service discovery (bleak 3.x 要求)
            try:
                svc_list = list(self._client.services)
                logging.info(f"Service discovery 完成, 找到 {len(svc_list)} 個 service")
                for svc in svc_list:
                    for ch in svc.characteristics:
                        logging.info(f"  svc {svc.uuid} ch {ch.uuid} {ch.properties}")
            except Exception as e:
                logging.warning(f"Service discovery 失敗: {e}")
            # 啟用 notify on 0xFFF2
            await self._client.start_notify(READ_UUID, self._on_notify)
            # 記錄已連線設備: 優先在 state.devices 找, 找不到則建一個新 DeviceInfo
            found = None
            for d in self.state.devices:
                if d.address == address:
                    found = d
                    break
            if found is None:
                # 從 BleakClient 拿設備名 (bleak connect 後會 populated)
                dev_name = "(已連線)"
                try:
                    if self._client._device and self._client._device.name:
                        dev_name = self._client._device.name
                except Exception:
                    pass
                found = DeviceInfo(
                    address=address,
                    name=dev_name,
                    rssi=0,  # connect 後 bleak 不直接提供 rssi
                    is_nice_logger=True,  # 連線階段信任 UI 已選對設備
                )
                self.state.devices.append(found)
            self.state.connected = found
            await self.state._emit("connected", address)
            return True
        except Exception as e:
            logging.exception(f"連線失敗 ({address}): {e}")
            return False

    async def disconnect(self):
        if self._client and self._client.is_connected:
            await self._client.disconnect()
        self.state.connected = None
        await self.state._emit("disconnected")

    def _on_notify(self, characteristic: BleakGATTCharacteristic, data: bytearray):
        """收到 0xFFF2 notify"""
        b = bytes(data)
        hex_str_repr = hex_str(b)
        self.state.log_packet("RX", hex_str_repr)
        self.state.last_rx = datetime.now()
        # 暫時當 raw log, 動態驗證後再加解析
        logging.debug(f"RX [{len(b)}]: {hex_str_repr}")
        # TODO: 解析 notify (等 opcode 確認)
        # 假設 0xC1 = live data, 0xC2 = record data, 0xC3 = config
        if len(b) >= 5 and b[0] == 0xC1:
            try:
                t1 = decode_temp_01c(b[1:3])
                t2 = decode_temp_01c(b[3:5])
                self.state.live = LiveData(temp_t1=t1, temp_t2=t2)
                # 排程 emit (callback 可能在 async context)
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self.state._emit("live", self.state.live), self._loop
                    )
            except Exception as e:
                logging.exception(f"解析 live data 失敗: {e}")

    def _wire_string(self, type_byte: int, check_type: int, payload: bytes = b"") -> str | None:
        """組出 native app 實際使用的 wire 字串

        格式 (由 IL2CPP InternalSendData 反組譯確認):
            mac + "/" + hex(type_BE2 + checkType_BE2 + payload)

        例: "AA:BB:CC:DD:EE:FF/00580058"
        直接送 raw bytes 是已知錯誤做法 ("single-byte-opcode anti-pattern"),
        設備不會理解這種封包。
        """
        mac = self.state.connected.address if self.state.connected else None
        if not mac:
            logging.error("尚未連線, 無法組出 wire string (需要目標 MAC)")
            return None
        frame = bytes([
            (type_byte >> 8) & 0xFF, type_byte & 0xFF,
            (check_type >> 8) & 0xFF, check_type & 0xFF,
        ]) + payload
        return f"{mac}/{frame.hex()}"

    async def _write(self, cmd: Cmd, payload: bytes = b"") -> bool:
        """寫入 wire 字串到 0xFFF1

        checkType 在同 type 下作為子命令識別; 預設 = type。
        """
        if not self._client or not self._client.is_connected:
            logging.error("尚未連線")
            return False
        # 預設 checkType = type (大部分 SendSet* 用這個模式)
        check_type = cmd.value
        return await self._write_packet(cmd.value, check_type, payload)

    async def _write_packet(self, type_byte: int, check_type: int, payload: bytes = b"") -> bool:
        """低階: 寫入 wire 字串到 0xFFF1

        用於 SendLoggerState / ReadOut 等需要區分子命令的情況。
        """
        if not self._client or not self._client.is_connected:
            logging.error("尚未連線")
            return False
        wire = self._wire_string(type_byte, check_type, payload)
        if wire is None:
            return False
        data = wire.encode("utf-8")
        try:
            await self._client.write_gatt_char(WRITE_UUID, data, response=True)
            note = f"Type=0x{type_byte:02X} CT=0x{check_type:04X} payload={len(payload)}B"
            self.state.log_packet("TX", hex_str(data), note=note)
            return True
        except Exception as e:
            logging.error(f"寫入失敗 type=0x{type_byte:02X}: {e}")
            return False

    # === 高階命令 ===
    async def set_device_name(self, name: str) -> bool:
        return await self._write(Cmd.SET_DEVICE_NAME, encode_device_name(name))

    async def set_password(self, pwd: int) -> bool:
        return await self._write(Cmd.SET_PASSWORD, encode_password(pwd))

    async def set_sample_interval(self, seconds: int) -> bool:
        return await self._write(Cmd.SET_SAMPLE_INTERVAL, encode_interval_seconds(seconds))

    async def set_start_delay(self, minutes: int) -> bool:
        return await self._write(Cmd.SET_START_DELAY, encode_start_delay_minutes(minutes))

    async def set_alarm_threshold(self, high_c: float, low_c: float) -> bool:
        return await self._write(Cmd.SET_ALARM_THRESHOLD, encode_temp_01c(high_c) + encode_temp_01c(low_c))

    async def set_alarm_delay(self, minutes: int) -> bool:
        return await self._write(Cmd.SET_ALARM_DELAY, encode_alarm_delay_minutes(minutes))

    async def set_alarm_type(self, t: int) -> bool:
        return await self._write(Cmd.SET_ALARM_TYPE, bytes([t & 0xFF]))

    async def start_log(self) -> bool:
        """SendLoggerState(true) - type=0x58, ct=0x58, payload=START_DATA

        START_DATA 預設為空 (僅 [type|ct] 2 bytes), 設備可能忽略.
        若設備無回應, 可試 [0x01] (啟動) 或 [0x00, 0x01] 等.
        """
        return await self._write_packet(0x58, 0x58, b"")

    async def stop_log(self) -> bool:
        """SendLoggerState(false) - type=0x58, ct=0x58, payload=STOP_DATA"""
        return await self._write_packet(0x58, 0x58, b"")

    async def read_live(self) -> bool:
        """讀取即時資料 - opcode 仍待驗證, 預設 0x40"""
        return await self._write_packet(0x40, 0x40, b"")

    async def read_records(self) -> bool:
        """ReadOut - type=0x64, 分兩階段:
          Phase 1: ct=0
          Phase 2: ct=0xFFFF
        payload 來自 static field (待動態驗證), 預設空.
        """
        ok1 = await self._write_packet(0x64, 0x0000, b"")
        await asyncio.sleep(0.2)
        ok2 = await self._write_packet(0x64, 0xFFFF, b"")
        return ok1 and ok2

    async def get_config(self) -> bool:
        """QueryConfigure - type=0x43 (推測, 待驗證 checkType)"""
        return await self._write_packet(0x43, 0x43, b"")

    # === 路徑 C: 實驗性 payload 探測 ===
    # SendLoggerState(type=0x58, ct=0x58) 的 START_DATA / STOP_DATA
    # 是 Android .cctor 在 runtime 初始化的 hex 字串, 靜態分析抓不到.
    # 以下依序試 5 種常見 payload, 每次間隔 2s, 看哪個會讓設備回 FFF2 notify.
    PROBE_PAYLOAD_CANDIDATES: list[bytes] = [
        b"",
        b"\x01",
        b"\x00\x01",
        b"\x00",
        b"\x00\x00\x00\x00",
    ]

    async def probe_start_payload(self, delay: float = 2.0) -> list[dict]:
        """依序試 5 種 payload, 每個間隔 2s, 看哪個讓設備回 RX.

        規則: 送完 payload 後 2s 內若有 FFF2 notify, 記為 "responded".
        結果存於 self.state.probe_summary, 同時回傳.
        """
        if not self._client or not self._client.is_connected:
            return [{"ok": False, "error": "not_connected"}]
        # 記錄開始時的 RX 計數
        initial_rx = sum(1 for p in self.state.packet_log if p.get("dir") == "RX")
        results: list[dict] = []
        for i, payload in enumerate(self.PROBE_PAYLOAD_CANDIDATES):
            payload_hex = payload.hex() if payload else "(empty)"
            self.state.log_packet(
                "TX", hex_str(bytes([0x58, 0x58]) + payload),
                note=f"PROBE {i+1}/5 type=0x58 ct=0x58 payload={payload_hex}"
            )
            ok = await self._write_packet(0x58, 0x58, payload)
            await asyncio.sleep(delay)
            new_rx = sum(1 for p in self.state.packet_log if p.get("dir") == "RX")
            responded = new_rx > initial_rx
            initial_rx = new_rx
            results.append({
                "index": i + 1,
                "payload_hex": payload_hex,
                "sent_ok": ok,
                "responded": responded,
            })
            if responded:
                logging.info(f"PROBE {i+1}/5 設備回應! payload={payload_hex}")
                break
        self.state.probe_summary = {
            "type": "start_payload",
            "results": results,
            "responded_count": sum(1 for r in results if r.get("responded")),
        }
        await self.state._emit("probe_done", self.state.probe_summary)
        return results
