# frontend — `/nicelogger-history/` 歷史頁

純靜態頁 + 一支 MySQL 直讀端點，部署在 Apache docroot 下：

```
D:\docker\apache\html\nicelogger-history\
├── index.html                       頁面骨架（[stats] 卡＝兩個紀錄器即時面板）
├── app.js                           輪詢、卡片渲染、Chart.js 圖表
├── style.css                        深色主題樣式
├── chart.umd.min.js                 Chart.js（vendored, MIT）
├── chartjs-adapter-date-fns.bundle.js  Chart.js date adapter（vendored, MIT）
└── api/records.php                  MySQL 直讀端點（JSON）
```

repo 內的 `frontend/` 與伺服器目錄結構一致，可整包覆蓋。

## 資料來源

頁面只讀 MySQL `mqtt_store.mqtt_messages`（由 Windows 端 `mqtt-worker` 寫入 MQTT 資料）。
端點 `api/records.php` 一次回傳兩份資料：

- `samples` — 視窗內所有紀錄（圖表用）。
- `latest` — 每台紀錄器最新一筆，不受視窗限制；上游中斷時仍顯示最後已知值與「過期」標記。

## 設定

設定檔解析順序（見 `api/records.php`）：

1. 環境變數 `NICELOGGER_CONFIG` 指定的檔案
2. 部署位置 `<apache html>/mqtt/config.php`（容器以 `DB_*` 環境變數注入，**不入 git**）
3. 同目錄 `config.php`（本機開發用，複製 `api/config.example.php`；此檔已被 `.gitignore` 排除）

可調參數：`NICELOGGER_API_WINDOW_MINUTES`（預設 180）、`NICELOGGER_API_MAX_SAMPLES`（2000）、
`NICELOGGER_API_LATEST_SCAN`（200）。

## 輪詢

`app.js` 每 15 秒重新抓一次整個視窗並重建圖表資料點（不是 append，append 會讓圖表越跑越密）。
「更新於 X 秒前」每秒更新一次，不會重新抓資料。

## 驗證

```bash
curl -s http://127.0.0.1/nicelogger-history/api/records.php | jq '{source, count, stale, updated_ms}'
```
