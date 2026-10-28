# deploy — systemd 與部署說明

## systemd units

| Unit | 角色 |
|---|---|
| `nicelogger_gateway.service` | BLE 廣播 → MQTT 發布器（`nicelogger_env` venv，`Restart=always`） |
| `nice-logger-pi.service` | FastAPI 面板（`.venv` venv，uvicorn :8000） |

```bash
scp backend/deploy/systemd/*.service kali@<pi>:/tmp/
ssh kali@<pi> 'sudo install -m 644 /tmp/nicelogger_gateway.service /tmp/nice-logger-pi.service /etc/systemd/system/ \
  && sudo systemctl daemon-reload && sudo systemctl enable --now nicelogger_gateway.service nice-logger-pi.service'
```

重啟其中一個服務（不需 sudo 的方式）：

```bash
sudo kill -9 $(systemctl show -p MainPID --value nicelogger_gateway.service)   # Restart=always 會拉起來
```

## Pi 上程式位置

```
/home/kali/nice-logger-pi/
├── nicelogger_gateway.py      ← backend/nicelogger_gateway.py
├── nice_logger/               ← backend/nice_logger/
├── nicelogger_env/            閘道器 venv（bleak + paho-mqtt）
└── .venv/                     面板 venv（fastapi + uvicorn + bleak）
```

## Apache（Windows）

- 歷史頁：`D:\docker\apache\html\nicelogger-history\`（docroot 靜態目錄，從未被代理）。
- MQTT worker：`D:\docker\apache\html\mqtt\`（`worker.php` + `config.php` + `sql/`）。
- MySQL 設定檔 `html/mqtt/config.php` **不放進版控**。
