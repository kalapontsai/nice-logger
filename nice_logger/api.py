"""
FastAPI server: 給瀏覽器 UI 用的 REST + WebSocket 介面
=====================================================
啟動:
  uvicorn nice_logger.api:app --host 0.0.0.0 --port 8000
或
  python -m nice_logger.api

然後瀏覽器開 http://localhost:8000
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from nice_logger.ble_client import (
    NiceLoggerBleClient, BleState, DeviceInfo, LiveData, ConfigPayload,
    Cmd, encode_temp_01c, encode_interval_seconds, encode_start_delay_minutes,
    encode_alarm_delay_minutes, encode_password, encode_device_name, hex_str,
)


# ===== 共用狀態 (process 全域) =====
state = BleState()
ble = NiceLoggerBleClient(state)


# ===== Pydantic models (HTTP request body) =====
class ScanRequest(BaseModel):
    timeout: float = 10.0


class ConnectRequest(BaseModel):
    address: str


class SetConfigRequest(BaseModel):
    device_name: str | None = None
    password: int | None = None
    sampling_rate_seconds: int | None = None
    start_delay_minutes: int | None = None
    alarm_delay_minutes: int | None = None
    alarm_high_c: float | None = None
    alarm_low_c: float | None = None
    alarm_type: int | None = None
    unit_metric: bool | None = None


# ===== WebSocket 連線管理 =====
class WsHub:
    def __init__(self):
        self.conns: set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.conns.add(ws)
        # 推目前狀態
        await ws.send_json(self.snapshot())

    def disconnect(self, ws: WebSocket):
        self.conns.discard(ws)

    async def broadcast(self, event: str, data):
        if not self.conns:
            return
        msg = json.dumps({"event": event, "data": data}, default=str)
        dead = []
        for ws in list(self.conns):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.conns.discard(ws)

    def snapshot(self) -> dict:
        return {
            "event": "snapshot",
            "data": {
                "connected": state.connected.address if state.connected else None,
                "devices": [
                    {"address": d.address, "name": d.name, "rssi": d.rssi}
                    for d in state.devices
                ],
                "live": {
                    "temp_t1": state.live.temp_t1,
                    "temp_t2": state.live.temp_t2,
                    "timestamp": state.live.timestamp.isoformat(),
                } if state.live else None,
                "config": state.config.__dict__ if state.config else None,
                "last_rx": state.last_rx.isoformat() if state.last_rx else None,
            },
        }


hub = WsHub()


# ===== FastAPI app =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動: 訂閱 BleState 事件轉推到 WebSocket
    async def forward(event: str, data):
        await hub.broadcast(event, data)
    state.subscribe(forward)
    logging.info("API 啟動")
    yield
    # 關閉
    await ble.disconnect()
    logging.info("API 關閉")


app = FastAPI(title="Nice Logger 88361", lifespan=lifespan)


# ===== REST endpoints =====
@app.get("/api/health")
async def health():
    return {"ok": True, "connected": state.connected.address if state.connected else None}


@app.post("/api/scan")
async def api_scan(req: ScanRequest):
    devices = await ble.scan(timeout=req.timeout)
    return {"devices": [
        {"address": d.address, "name": d.name, "rssi": d.rssi} for d in devices
    ]}


@app.post("/api/connect")
async def api_connect(req: ConnectRequest):
    ok = await ble.connect(req.address)
    if not ok:
        raise HTTPException(400, f"連線 {req.address} 失敗")
    return {"ok": True, "address": req.address}


@app.post("/api/disconnect")
async def api_disconnect():
    await ble.disconnect()
    return {"ok": True}


@app.get("/api/live")
async def api_live():
    if not state.live:
        await ble.read_live()
        await asyncio.sleep(0.5)
    if not state.live:
        return {"live": None}
    return {"live": {
        "temp_t1": state.live.temp_t1,
        "temp_t2": state.live.temp_t2,
        "timestamp": state.live.timestamp.isoformat(),
    }}


@app.get("/api/packets")
async def api_packets(limit: int = 50):
    """最近封包歷史 (debug 用)"""
    return {"packets": state.packet_log[-limit:]}


@app.get("/api/config")
async def api_get_config():
    await ble.get_config()
    await asyncio.sleep(0.5)
    return {"config": state.config.__dict__ if state.config else None}


@app.post("/api/config")
async def api_set_config(req: SetConfigRequest):
    if not state.connected:
        raise HTTPException(400, "尚未連線")
    results = {}
    if req.device_name is not None:
        results["device_name"] = await ble.set_device_name(req.device_name)
    if req.password is not None:
        results["password"] = await ble.set_password(req.password)
    if req.sampling_rate_seconds is not None:
        results["sampling_rate"] = await ble.set_sample_interval(req.sampling_rate_seconds)
    if req.start_delay_minutes is not None:
        results["start_delay"] = await ble.set_start_delay(req.start_delay_minutes)
    if req.alarm_delay_minutes is not None:
        results["alarm_delay"] = await ble.set_alarm_delay(req.alarm_delay_minutes)
    if req.alarm_high_c is not None and req.alarm_low_c is not None:
        results["alarm_threshold"] = await ble.set_alarm_threshold(req.alarm_high_c, req.alarm_low_c)
    if req.alarm_type is not None:
        results["alarm_type"] = await ble.set_alarm_type(req.alarm_type)
    return {"ok": all(results.values()), "results": results}


@app.post("/api/log/start")
async def api_log_start():
    return {"ok": await ble.start_log()}


@app.post("/api/log/stop")
async def api_log_stop():
    return {"ok": await ble.stop_log()}


@app.post("/api/log/readout")
async def api_log_readout():
    return {"ok": await ble.read_records()}


# ===== WebSocket =====
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            # 雙向命令 (預留: UI 可透過 ws 送命令)
            msg = json.loads(data)
            event = msg.get("event")
            if event == "ping":
                await ws.send_json({"event": "pong"})
    except WebSocketDisconnect:
        hub.disconnect(ws)


# ===== 靜態檔案 + 網頁 UI =====
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# ===== 入口 =====
if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
