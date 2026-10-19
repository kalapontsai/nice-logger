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


# ===== 真實的 opcode enum (從 libil2cpp.so 反組譯得出) =====
# 來源: DeviceManager.SendSetXxx 函式內部 mov w1, #imm 立即值
# 反組譯時間: 2026-10-19, il2cppdumper 6.7.46 + capstone
class Cmd(Enum):
    """真實的 BLE 命令 opcode (從 ARM 機器碼反組譯)

    這些值來自 DeviceManager.SendSetXxx 函式內部:
        mov w1, #0x??  ; type
        mov w2, #0x??  ; checkType
        b InternalSendData
    """
    # 設定類 (從 SendSet* 反組譯)
    CLEAR_SEND_QUEUE = 0x49
    SET_CONFIGURE = 0x41          # SendSetConfigure
    SET_DEVICE_TIME = 0x43       # SendSetDeviceTime
    SET_LOGGER_ID = 0x50         # SendSetID
    SET_UNIT_AND_PASSWORD = 0x53 # SendSetUnitAndPassword
    SET_THRESHOLD = 0x58         # SendSetThreshold
    # 查詢類 (type 從外部傳入, 推測)
    QUERY_ALARM_THRESHOLD = 0x64
    # 控制類 (推測: SendLoggerState/ReadOut 用 type=w1 從 caller 傳)
    START_LOG = 0x55  # 推測 (待 SendLoggerState caller 反組譯)
    STOP_LOG = 0x56   # 推測
    READOUT = 0x57    # 推測
    QUERY_CONFIGURE = 0x59  # 推測
    # 讀取類 (推測)
    READ_LIVE = 0x40  # 推測 (0x40 是 ASCII '@' 常見開頭)


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

    async def _write(self, cmd: Cmd, payload: bytes = b"") -> bool:
        """寫入 opcode + payload 到 0xFFF1

        bleak 3.0+ 改名: write_characteristic → write_gatt_char
        """
        if not self._client or not self._client.is_connected:
            logging.error("尚未連線")
            return False
        data = bytes([cmd.value]) + payload
        try:
            await self._client.write_gatt_char(WRITE_UUID, data, response=True)
            self.state.log_packet("TX", hex_str(data), note=f"Cmd=0x{cmd.value:02X} {cmd.name}")
            return True
        except Exception as e:
            logging.error(f"寫入失敗 cmd=0x{cmd.value:02X}: {e}")
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
        return await self._write(Cmd.START_LOG)

    async def stop_log(self) -> bool:
        return await self._write(Cmd.STOP_LOG)

    async def read_live(self) -> bool:
        return await self._write(Cmd.READ_LIVE)

    async def read_records(self) -> bool:
        return await self._write(Cmd.READOUT)

    async def get_config(self) -> bool:
        return await self._write(Cmd.GET_CONFIG)
