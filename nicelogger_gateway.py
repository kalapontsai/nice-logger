import asyncio
import json
import time
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
import paho.mqtt.client as mqtt

# --- 配置設定 ---
MQTT_BROKER = "10.35.32.11"  # 或您的 MQTT Broker IP
MQTT_PORT = 1883
MQTT_TOPIC_PREFIX = "sensor/nicelogger"

# 監聽目標 MAC
TARGET_MACS = {
    "3C:E4:B0:81:18:48": "logger_01",
    "3C:E4:B0:81:0E:81": "logger_02",
}

# 最小發布間隔（秒），避免 1 秒內收到多次廣播重複送出
REPORT_INTERVAL = 60.0
last_report_time = {}


def parse_nicelog_adv(data: bytes):
    if len(data) < 24:
        return None
    try:
        header_tag = data[2:9].decode("latin1", errors="ignore")
        if "Nicelog" not in header_tag:
            return None

        # 溫度 1 (°F -> °C)
        raw_t1 = (data[12] << 8) | data[13]
        if raw_t1 < 0x7000:
            t1_f = round(raw_t1 / 10.0, 1)
            t1_c = round((t1_f - 32.0) * 5.0 / 9.0, 2)
        else:
            t1_f, t1_c = None, None

        # 溫度 2 / 外部探棒
        raw_t2 = (data[14] << 8) | data[15]
        probe_connected = raw_t2 < 0x7F00
        t2_c = (
            round((raw_t2 / 10.0 - 32.0) * 5.0 / 9.0, 2)
            if probe_connected
            else None
        )

        # 間隔 (秒)
        interval_sec = (
            data[20] if data[19] == 0 else ((data[20] << 8) | data[19])
        )

        # 點數
        log_count = (data[21] << 8) | data[22]

        # 電量
        battery = data[23]

        return {
            "temperature_c": t1_c,
            "temperature_f": t1_f,
            "probe_connected": probe_connected,
            "probe_temp_c": t2_c,
            "interval_sec": interval_sec,
            "log_count": log_count,
            "battery_pct": battery,
        }
    except Exception:
        return None


def on_advertisement(
    device: BLEDevice, advertisement_data: AdvertisementData, mqtt_client
):
    mac = device.address.upper()
    if mac not in TARGET_MACS:
        return

    now = time.time()
    if now - last_report_time.get(mac, 0) < REPORT_INTERVAL:
        return

    mfr_data = advertisement_data.manufacturer_data.get(
        0x5002
    ) or advertisement_data.manufacturer_data.get(20482)
    if not mfr_data:
        return

    parsed = parse_nicelog_adv(mfr_data)
    if not parsed:
        return

    last_report_time[mac] = now
    device_id = TARGET_MACS[mac]

    payload = {
        "device_id": device_id,
        "mac": mac,
        "rssi": advertisement_data.rssi,
        "timestamp": int(now),
        **parsed,
    }

    topic = f"{MQTT_TOPIC_PREFIX}/{device_id}"
    mqtt_client.publish(topic, json.dumps(payload))

    print(
        f"[{device_id} | {mac}] {parsed['temperature_c']}°C | "
        f"電量: {parsed['battery_pct']}% | 筆數: {parsed['log_count']} -> MQTT ({topic})"
    )


async def main():
    # 連接 MQTT Broker
    client = mqtt.Client()
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()
        print(f"[+] 已連接 MQTT Broker: {MQTT_BROKER}:{MQTT_PORT}")
    except Exception as e:
        print(f"[!] MQTT 連線失敗: {e} (將僅在終端機顯示)")

    # 註冊 BleakScanner 回調
    scanner = BleakScanner(
        lambda dev, adv: on_advertisement(dev, adv, client),
        scanning_mode="active",
    )

    await scanner.start()
    print("[+] NiceLogger 廣播閘道器啟動中，按 Ctrl+C 結束...\n" + "-" * 70)

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await scanner.stop()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())