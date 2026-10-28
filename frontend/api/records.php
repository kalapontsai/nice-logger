<?php
/**
 * Nice Logger 88361 — 歷史頁 MySQL 直讀端點
 *
 * 資料來源：MySQL `mqtt_store`.`mqtt_messages`（由 Windows 端 mqtt-worker 寫入）。
 * 本端點不訂閱 MQTT，直接查資料庫；頁面與圖表都走這支。
 *
 * 回應格式：
 *   {
 *     "samples": [...],                  // 視窗內所有紀錄（圖表用）
 *     "latest": { "<device_id>": {...} }, // 每台紀錄器最新一筆（不受視窗限制）
 *     "updated_ms": int, "stale": bool, "broker_connected": bool,
 *     "count": int, "window_minutes": int, "source": "mysql"
 *   }
 * 每筆 sample 含 received_ms（毫秒時間戳）與 MQTT payload 原始欄位。
 */

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store, no-cache, must-revalidate');

// 資料庫設定來源（依序嘗試）：
//   1. 環境變數 NICELOGGER_CONFIG 指定的檔案
//   2. 部署位置：<apache html>/mqtt/config.php（含 DB 主機／帳密，機敏值不入 git）
//   3. 本機：同目錄 config.php（由 config.example.php 複製）
$configCandidates = [
    getenv('NICELOGGER_CONFIG') ?: '',
    __DIR__ . '/../../mqtt/config.php',
    __DIR__ . '/config.php',
];
$configPath = '';
foreach ($configCandidates as $candidate) {
    if ($candidate !== '' && is_file($candidate)) {
        $configPath = $candidate;
        break;
    }
}
if ($configPath === '') {
    http_response_code(503);
    echo json_encode(['error' => 'config_missing', 'samples' => [], 'latest' => [],
        'updated_ms' => 0, 'broker_connected' => false, 'stale' => true, 'count' => 0,
        'window_minutes' => 180, 'source' => 'mysql'], JSON_UNESCAPED_UNICODE);
    exit;
}
$config = require $configPath;

$windowMinutes = (int)(getenv('NICELOGGER_API_WINDOW_MINUTES') ?: ($config['api']['window_minutes'] ?? 180));
$maxSamples    = (int)(getenv('NICELOGGER_API_MAX_SAMPLES') ?: 2000);
$latestScan    = (int)(getenv('NICELOGGER_API_LATEST_SCAN') ?: 200);

try {
    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=%s',
        $config['db']['host'],
        $config['db']['port'],
        $config['db']['dbname'],
        $config['db']['charset']
    );
    $pdo = new PDO($dsn, $config['db']['user'], $config['db']['pass'], [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
} catch (PDOException $e) {
    http_response_code(503);
    echo json_encode([
        'error'            => 'db_unavailable',
        'samples'          => [],
        'latest'           => [],
        'updated_ms'       => 0,
        'broker_connected' => false,
        'stale'            => true,
        'count'            => 0,
        'window_minutes'   => $windowMinutes,
        'source'           => 'mysql',
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

/** 把資料列轉成前端用的 sample（含 received_ms）。 */
function to_sample(array $row): ?array
{
    $payload = json_decode($row['payload'], true);
    if (!is_array($payload)) {
        return null;   // 壞掉的 payload 直接略過，不讓整個 API 失敗
    }
    $ts = strtotime($row['created_at'] . ' UTC');
    if ($ts === false) {
        return null;
    }
    $payload['received_ms'] = $ts * 1000;
    $payload['topic']       = $row['topic'];
    return $payload;
}

// 1) 視窗內的所有紀錄（圖表 + 最近記錄）
$sqlWindow = 'SELECT id, topic, payload, created_at '
          . 'FROM mqtt_messages '
          . 'WHERE topic LIKE :topic '
          . 'AND created_at >= (UTC_TIMESTAMP() - INTERVAL :window MINUTE) '
          . 'ORDER BY id DESC LIMIT :limit';

$stmt = $pdo->prepare($sqlWindow);
$stmt->bindValue(':topic', 'sensor/nicelogger/%', PDO::PARAM_STR);
$stmt->bindValue(':window', $windowMinutes, PDO::PARAM_INT);
$stmt->bindValue(':limit', $maxSamples, PDO::PARAM_INT);
$stmt->execute();

$samples = [];
foreach ($stmt->fetchAll() as $row) {
    $sample = to_sample($row);
    if ($sample !== null) {
        $samples[] = $sample;
    }
}
// 由新到舊撈出來，要翻正序給圖表用
$samples = array_reverse($samples);

// 2) 每台紀錄器最新一筆（不受視窗限制：即使斷線也能顯示最後已知值 + 過期時間）
$sqlLatest = 'SELECT id, topic, payload, created_at '
          . 'FROM mqtt_messages '
          . 'WHERE topic LIKE :topic '
          . 'ORDER BY id DESC LIMIT :limit';

$stmtLatest = $pdo->prepare($sqlLatest);
$stmtLatest->bindValue(':topic', 'sensor/nicelogger/%', PDO::PARAM_STR);
$stmtLatest->bindValue(':limit', $latestScan, PDO::PARAM_INT);
$stmtLatest->execute();

$latest = [];
$latestMs = 0;
foreach ($stmtLatest->fetchAll() as $row) {
    $sample = to_sample($row);
    if ($sample === null) {
        continue;
    }
    $latestMs = max($latestMs, (int)$sample['received_ms']);
    $key = (string)($sample['device_id'] ?? $row['topic']);
    if (!isset($latest[$key])) {
        $latest[$key] = $sample;   // 由新到舊，第一筆就是最新
    }
}

$updatedMs = $latestMs;
foreach ($samples as $s) {
    $updatedMs = max($updatedMs, (int)$s['received_ms']);
}

// 最新一筆距離現在太久 → 視為上游斷線，前端才能顯示「資料過期」
$stale = false;
if ($updatedMs > 0) {
    $stale = (int)(microtime(true) * 1000) - $updatedMs > 3 * 60 * 1000;
}

echo json_encode([
    'samples'          => $samples,
    'latest'           => $latest,
    'updated_ms'       => $updatedMs,
    'broker_connected' => !$stale,
    'stale'            => $stale,
    'count'            => count($samples),
    'window_minutes'   => $windowMinutes,
    'source'           => 'mysql',
], JSON_UNESCAPED_UNICODE);
