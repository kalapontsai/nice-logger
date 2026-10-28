"""
MQTT 遙測訂閱 (sensor/nicelogger)
================================
Pi 上的 nicelogger_gateway.py 負責掃 BLE 廣播並把遙測發到 MQTT；
本模組反過來訂閱同一個 topic，讓網頁 UI 顯示各 logger 的即時值與歷史。

為什麼不直接從 MySQL 讀？
  MySQL 那條路徑需要 Windows 端的 mqtt-worker 額外訂閱，屬於部署層依賴。
  這裡直接訂閱 MQTT，Pi 端自給自足，UI 在 Pi 掛掉 MQTT 時仍可顯示最後已知值。

資料流:
    nicelogger_gateway.py ──MQTT sensor/nicelogger/<id>──▶ MqttFeed
                                                               │
                                          ┌────────────────────┴───────────────┐
                                          ▼                                    ▼
                                  REST /api/mqtt/records              WebSocket "mqtt" 事件
"""

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - 缺套件時讓呼叫端自行降級
    mqtt = None


# ===== 預設設定 (可用環境變數覆寫) =====
DEFAULT_BROKER = "10.35.32.11"
DEFAULT_PORT = 1883
DEFAULT_TOPIC = "sensor/nicelogger/#"
DEFAULT_HISTORY = 500


@dataclass
class MqttRecord:
    """一筆已解析的遙測紀錄"""
    topic: str = ""
    device_id: str = ""
    mac: str = ""
    rssi: Optional[int] = None
    timestamp: Optional[int] = None
    received_at: str = ""
    temperature_c: Optional[float] = None
    temperature_f: Optional[float] = None
    probe_connected: Optional[bool] = None
    probe_temp_c: Optional[float] = None
    interval_sec: Optional[int] = None
    log_count: Optional[int] = None
    battery_pct: Optional[int] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "device_id": self.device_id,
            "mac": self.mac,
            "rssi": self.rssi,
            "timestamp": self.timestamp,
            "received_at": self.received_at,
            "temperature_c": self.temperature_c,
            "temperature_f": self.temperature_f,
            "probe_connected": self.probe_connected,
            "probe_temp_c": self.probe_temp_c,
            "interval_sec": self.interval_sec,
            "log_count": self.log_count,
            "battery_pct": self.battery_pct,
        }


def _as_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_record(topic: str, payload: bytes) -> Optional[MqttRecord]:
    """把 MQTT payload 解析成 MqttRecord

    gateway 發的 JSON 欄位 (見 nicelogger_gateway.py):
        device_id, mac, rssi, timestamp,
        temperature_c, temperature_f, probe_connected, probe_temp_c,
        interval_sec, log_count, battery_pct
    """
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        logging.warning("MQTT payload 解析失敗: %s", e)
        return None
    if not isinstance(data, dict):
        return None

    device_id = data.get("device_id") or topic.rsplit("/", 1)[-1]
    return MqttRecord(
        topic=topic,
        device_id=str(device_id),
        mac=str(data.get("mac") or ""),
        rssi=_as_int(data.get("rssi")),
        timestamp=_as_int(data.get("timestamp")),
        received_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        temperature_c=_as_float(data.get("temperature_c")),
        temperature_f=_as_float(data.get("temperature_f")),
        probe_connected=data.get("probe_connected"),
        probe_temp_c=_as_float(data.get("probe_temp_c")),
        interval_sec=_as_int(data.get("interval_sec")),
        log_count=_as_int(data.get("log_count")),
        battery_pct=_as_int(data.get("battery_pct")),
        raw=data,
    )


class MqttFeed:
    """背景訂閱 sensor/nicelogger/# 的 MQTT 客戶端

    使用 paho-mqtt 的 network thread；對外暴露 thread-safe 的快照。
    """

    def __init__(
        self,
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
        topic: str = DEFAULT_TOPIC,
        history: int = DEFAULT_HISTORY,
        on_record: Optional[Callable[[MqttRecord], None]] = None,
    ):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.on_record = on_record

        self._lock = threading.Lock()
        self._history: deque = deque(maxlen=history)
        self._latest: dict = {}
        self._client = None
        self._connected = False
        self._last_error: Optional[str] = None
        self._last_message_at: Optional[str] = None

    # ===== 狀態 =====
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def last_message_at(self) -> Optional[str]:
        return self._last_message_at

    def status(self) -> dict:
        with self._lock:
            return {
                "broker": self.broker,
                "port": self.port,
                "topic": self.topic,
                "connected": self._connected,
                "last_error": self._last_error,
                "last_message_at": self._last_message_at,
                "devices": sorted(self._latest.keys()),
                "history_size": len(self._history),
            }

    def latest(self) -> dict:
        """每個 device 的最新一筆"""
        with self._lock:
            return {k: v.to_dict() for k, v in self._latest.items()}

    def history(self, limit: Optional[int] = None) -> list:
        """歷史紀錄 (時間由舊到新)"""
        with self._lock:
            items = [r.to_dict() for r in self._history]
        if limit is not None and limit > 0:
            items = items[-limit:]
        return items

    # ===== 生命週期 =====
    def start(self) -> bool:
        """連線並開始訂閱。回傳是否成功啟動。"""
        if mqtt is None:
            self._last_error = "paho-mqtt 未安裝"
            logging.error("paho-mqtt 未安裝，MQTT 遙測功能停用")
            return False
        if self._client is not None:
            return True

        try:
            # paho 2.x 需要明確指定 callback API 版本；1.x 沒有這個列舉。
            if hasattr(mqtt, "CallbackAPIVersion"):
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id="nice-logger-web",
                )
            else:
                client = mqtt.Client(client_id="nice-logger-web")
        except Exception as e:
            self._last_error = f"建立 MQTT client 失敗: {e}"
            logging.error(self._last_error)
            return False

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        # 斷線後自動重連 (paho 內建 loop 會重試)
        client.reconnect_delay_set(min_delay=1, max_delay=30)

        self._client = client
        try:
            client.connect_async(self.broker, self.port, keepalive=60)
            client.loop_start()
        except Exception as e:
            self._last_error = f"MQTT 連線失敗: {e}"
            logging.error(self._last_error)
            self._client = None
            return False

        logging.info("MQTT 訂閱啟動: %s:%d topic=%s", self.broker, self.port, self.topic)
        return True

    def stop(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if client is None:
            return
        try:
            client.loop_stop()
            client.disconnect()
        except Exception as e:
            logging.warning("MQTT 斷線時發生例外: %s", e)
        logging.info("MQTT 訂閱已停止")

    # ===== paho callbacks =====
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        # paho 1.x 傳 int，2.x 傳 ReasonCode 物件
        code = getattr(rc, "value", rc)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            self._connected = True
            self._last_error = None
            client.subscribe(self.topic)
            logging.info("MQTT 已連線，訂閱 %s", self.topic)
        else:
            self._connected = False
            self._last_error = f"CONNACK rc={code}"
            logging.error("MQTT 連線被拒: rc=%s", code)

    def _on_disconnect(self, client, userdata, rc, *args):
        self._connected = False
        code = getattr(rc, "value", rc)
        logging.warning("MQTT 已斷線 (rc=%s)", code)

    def _on_message(self, client, userdata, msg):
        record = parse_record(msg.topic, msg.payload)
        if record is None:
            return
        with self._lock:
            self._history.append(record)
            self._latest[record.device_id] = record
            self._last_message_at = record.received_at
        if self.on_record is not None:
            try:
                self.on_record(record)
            except Exception as e:
                logging.exception("MQTT on_record 回呼失敗: %s", e)
