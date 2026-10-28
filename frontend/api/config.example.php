<?php
/**
 * MySQL 設定範例 — frontend/api/records.php 的設定來源。
 *
 * 解析順序（見 records.php）：
 *   1. 環境變數 NICELOGGER_CONFIG 指定的檔案
 *   2. 部署位置 <apache html>/mqtt/config.php（容器用 DB_HOST/DB_NAME/DB_USER/DB_PASS 注入）
 *   3. 同目錄 config.php（本檔的副本，供本機開發用）
 *
 * 機敏值（密碼）一律走環境變數，不要寫進版控。
 */

return [
    'db' => [
        'host'    => getenv('DB_HOST') ?: 'mysql',
        'port'    => (int)(getenv('DB_PORT') ?: 3306),
        'dbname'  => getenv('DB_NAME') ?: 'mqtt_store',
        'user'    => getenv('DB_USER') ?: 'mqtt_user',
        'pass'    => getenv('DB_PASS') ?: '',   // 機敏值：走環境變數
        'charset' => 'utf8mb4',
    ],
    'api' => [
        'window_minutes' => (int)(getenv('API_WINDOW_MINUTES') ?: 180),
        'max_samples'    => (int)(getenv('API_MAX_SAMPLES') ?: 2000),
    ],
];
