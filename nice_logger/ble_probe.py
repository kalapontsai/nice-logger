"""
BLE opcode 探測器
=================
用途: 自動掃 0x00-0xFF opcode, 找出哪些會讓 88361 設備回 notify.
基於「真實 opcode 動態驗證」原則, 不用手機 App 介入.

設計:
  1. 從安全區開始 (0xC0-0xCF 讀取類, 0xD0-0xDF 讀取類)
  2. 再測設定類 (0xA0-0xBF)
  3. 最後測危險區 (0x00-0x9F, 可能 reset / format / lock)
  4. 每個 opcode: 送 1 byte, 等 1.5 秒, 統計 notify 數
  5. 觸發停止條件就立刻 abort

輸出:
  state.probe_results = {opcode: {respond: int, blocked: bool, ...}, ...}
"""

import asyncio
import logging
from typing import Optional

from nice_logger.ble_client import WRITE_UUID, READ_UUID, BleState


# 探測分區 (從安全到危險)
PROBE_ZONES = [
    ("READ_LIVE_CANDIDATES", 0xC0, 0xCF),  # 讀取類
    ("READ_CONFIG_CANDIDATES", 0xD0, 0xDF),  # 讀取類
    ("SET_CANDIDATES", 0xA0, 0xBF),         # 設定類
    ("CONTROL_CANDIDATES", 0xB0, 0xBF),     # 控制類
    ("EXTENDED", 0xE0, 0xFF),               # 擴展
    ("MANUFACTURER", 0xF0, 0xFF),           # 廠商自訂
]


class BleProbe:
    """BLE opcode 探測器"""

    def __init__(self, state: BleState, client):
        """
        Args:
            state: 共用 BleState
            client: NiceLoggerBleClient 實例
        """
        self.state = state
        self.client = client
        self.results: dict = {}
        self.running: bool = False
        self._notify_count_before: int = 0

    async def _count_notify(self) -> int:
        """統計自上次 reset 以來的 notify 數 (從 packet_log)"""
        return len(self.state.packet_log)

    async def _probe_one(self, opcode: int) -> dict:
        """對單一 opcode 探測一次

        Returns: {
            'opcode': int,
            'responded': bool,  # 收到 notify
            'blocked': bool,    # 連線被踢
            'error': str or None,
            'latency_ms': int,
        }
        """
        if not self.client._client or not self.client._client.is_connected:
            return {"opcode": opcode, "responded": False, "blocked": True,
                    "error": "not connected", "latency_ms": 0}

        # 記錄當前 packet_log 數
        before = len(self.state.packet_log)

        start = asyncio.get_event_loop().time()
        try:
            # 送 1 byte (只 opcode, 沒 payload) - 最破壞性最小
            await self.client._client.write_gatt_char(
                WRITE_UUID, bytes([opcode]), response=True
            )
            # 等 1.5 秒看有沒有 notify
            await asyncio.sleep(1.5)
        except Exception as e:
            err_str = str(e)
            # "not connected" = 被踢
            blocked = "not connected" in err_str.lower() or "disconnected" in err_str.lower()
            return {"opcode": opcode, "responded": False, "blocked": blocked,
                    "error": err_str[:100], "latency_ms": 0}

        after = len(self.state.packet_log)
        new_notifies = after - before
        latency_ms = int((asyncio.get_event_loop().time() - start) * 1000)

        return {
            "opcode": opcode,
            "responded": new_notifies > 0,
            "notifies": new_notifies,
            "blocked": False,
            "error": None,
            "latency_ms": latency_ms,
        }

    async def run(self, zones: Optional[list] = None, per_opcode_delay: float = 0.05) -> dict:
        """跑完整探測

        Args:
            zones: 要探測的區段清單 (預設全部)
            per_opcode_delay: opcode 之間的延遲 (秒), 預設 50ms
        Returns:
            {
                "total": int,
                "responded": [int, ...],  # opcode list
                "blocked": [int, ...],
                "results": {opcode: result, ...},
                "elapsed_sec": float,
            }
        """
        if self.running:
            return {"error": "probe already running"}
        if not self.client._client or not self.client._client.is_connected:
            return {"error": "not connected to device"}

        self.running = True
        self.results = {}
        zones = zones or PROBE_ZONES
        responded = []
        blocked = []
        start = asyncio.get_event_loop().time()

        try:
            for zone_name, start_op, end_op in zones:
                if not self.running:
                    break  # 使用者中止
                for opcode in range(start_op, end_op + 1):
                    if not self.running:
                        break
                    # 連線斷了就不再測
                    if not self.client._client or not self.client._client.is_connected:
                        logging.info(f"連線斷了, 探測中止 @ 0x{opcode:02X}")
                        break
                    r = await self._probe_one(opcode)
                    self.results[opcode] = r
                    if r["responded"]:
                        responded.append(opcode)
                        logging.info(f"  ✓ 0x{opcode:02X} 回了 {r.get('notifies', 1)} 個 notify")
                    if r["blocked"]:
                        blocked.append(opcode)
                        logging.warning(f"  ✗ 0x{opcode:02X} 連線被踢")
                        # 一被踢就停 (安全)
                        break
                    # opcode 之間小延遲
                    if per_opcode_delay > 0:
                        await asyncio.sleep(per_opcode_delay)
                if blocked:
                    break
        finally:
            self.running = False
            elapsed = asyncio.get_event_loop().time() - start

        summary = {
            "total": len(self.results),
            "responded": responded,
            "blocked": blocked,
            "results": self.results,
            "elapsed_sec": round(elapsed, 1),
            "zones": [z[0] for z in zones],
        }
        # 存到 state 給 API 查詢
        self.state.probe_summary = summary
        return summary

    def stop(self):
        """中斷探測"""
        self.running = False
