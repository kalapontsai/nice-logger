// Nice Logger 88361 — 歷史記錄（MySQL / mqtt-worker 管線）
//
// 資料來源: /mqtt/api/latest_nicelogger.php
//   回傳 { samples: [...], updated_ms, stale, count }
//   每筆 sample 含 received_ms（毫秒時間戳）與 MQTT payload 原始欄位。
//
// 重要: 每次輪詢都「重建」整個 points 陣列，不要 append。
//   append 會讓每輪輪詢都把同一批資料再疊一次，圖表會越跑越密且時間軸錯亂。

const API_URL = '/nicelogger-history/api/records.php';  // MySQL 直讀
const POLL_MS = 15000;         // 儀器約每 60 秒回報一次，15 秒輪詢可在資料落地後立即反映
const STALE_MS = 3 * 60 * 1000;
const RECENT_ROWS = 3;         // 每台紀錄器顯示的「最近記錄」筆數

let samples = [];      // 視窗內的所有紀錄（圖表用）
let latestById = {};   // 每台紀錄器最新一筆（MySQL 直接給，不受視窗限制）
let lastFetchMs = 0;

const el = (id) => document.getElementById(id);
const PALETTE = ['#4ec9d0', '#fbbf24', '#6ee06e', '#e06e6e', '#a78bfa', '#f472b6'];

// 兩個紀錄器的固定顯示順序；資料庫若出現新的 device_id，會自動追加在後面
const DEVICE_ORDER = ['logger_01', 'logger_02'];

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function fmtAge(ms) {
  if (!isNum(ms)) return 'N/A';
  const age = Date.now() - Number(ms);
  if (age < 0) return '剛剛';
  const sec = Math.round(age / 1000);
  if (sec < 60) return sec + ' 秒前';
  const min = Math.floor(sec / 60);
  if (min < 60) return min + ' 分前';
  return Math.floor(min / 60) + ' 小時前';
}

function fmtClock(ms) {
  if (!isNum(ms)) return '--:--:--';
  return new Date(Number(ms)).toLocaleTimeString();
}

function isNum(v) {
  // Number(null) === 0，會把「沒有資料」變成真正的 0 度，必須先擋掉
  if (v === null || v === undefined || v === '') return false;
  return Number.isFinite(Number(v));
}

function setStatus(kind, text) {
  el('dot').className = 'dot ' + kind;
  el('stat').textContent = text;
}

const commonScales = {
  x: {
    type: 'time',
    time: { tooltipFormat: 'HH:mm:ss', displayFormats: { minute: 'HH:mm' } },
    ticks: { color: '#a1a1aa', maxTicksLimit: 8 },
    grid: { color: '#27272a' },
  },
};

const tempChart = new Chart(el('tempChart'), {
  type: 'line',
  data: { datasets: [] },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'nearest', intersect: false },
    scales: {
      x: commonScales.x,
      y: { ticks: { color: '#a1a1aa' }, grid: { color: '#27272a' }, title: { display: true, text: '°C' } },
    },
    plugins: {
      legend: { labels: { color: '#a1a1aa', boxWidth: 10 } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${Number(c.parsed.y).toFixed(2)} °C` } },
    },
  },
});

const batteryChart = new Chart(el('batteryChart'), {
  type: 'line',
  data: { datasets: [] },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'nearest', intersect: false },
    scales: {
      x: commonScales.x,
      y: { ticks: { color: '#a1a1aa' }, grid: { color: '#27272a' }, min: 0, max: 100 },
    },
    plugins: {
      legend: { labels: { color: '#a1a1aa', boxWidth: 10 } },
      tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${Number(c.parsed.y).toFixed(0)} %` } },
    },
  },
});

function buildDatasets(key, unit, suffix) {
  const byDevice = {};
  samples.forEach((s) => {
    if (!isNum(s[key])) return;
    (byDevice[s.device_id] = byDevice[s.device_id] || []).push(s);
  });
  const ids = Object.keys(byDevice).sort();
  return ids.map((id, i) => {
    const color = PALETTE[i % PALETTE.length];
    return {
      label: `${id} ${unit}`,
      data: byDevice[id].map((s) => ({ x: s.received_ms, y: Number(s[key]) })),
      borderColor: color,
      backgroundColor: color + '22',
      borderWidth: 2,
      pointRadius: 2,
      pointHoverRadius: 5,
      tension: 0.25,
      spanGaps: true,
    };
  });
}

function latestByDevice() {
  const by = {};
  samples.forEach((s) => {
    const id = s.device_id || 'unknown';
    const cur = by[id];
    if (!cur || Number(s.received_ms || 0) > Number(cur.received_ms || 0)) by[id] = s;
  });
  return by;
}

function orderedDeviceIds(by) {
  const ids = Object.keys(by);
  const known = DEVICE_ORDER.filter((id) => ids.includes(id));
  const extra = ids.filter((id) => !DEVICE_ORDER.includes(id)).sort();
  return known.concat(extra);
}

// [stats] 卡片：兩個紀錄器各自一格，顯示即時值 + 最近記錄
function renderRecorders() {
  // 以 MySQL 回傳的 latest 為主（即使資料已超出視窗，仍顯示最後已知值 + 過期時間）
  const by = latestByDevice();
  Object.keys(latestById).forEach((id) => {
    const s = latestById[id];
    const cur = by[id];
    if (!cur || Number(s.received_ms || 0) > Number(cur.received_ms || 0)) by[id] = s;
  });
  const ids = orderedDeviceIds(by);
  const host = el('recorders');

  if (!ids.length) {
    host.innerHTML = '<div class="empty">尚無紀錄器資料</div>';
    return;
  }

  host.innerHTML = ids.map((id) => {
    const s = by[id];
    const age = Date.now() - Number(s.received_ms || 0);
    const live = isNum(s.received_ms) && age <= STALE_MS;
    const temp = isNum(s.temperature_c) ? Number(s.temperature_c).toFixed(2) : 'N/A';
    const bat = isNum(s.battery_pct) ? Number(s.battery_pct).toFixed(0) + ' %' : 'N/A';

    const recent = samples
      .filter((x) => (x.device_id || 'unknown') === id)
      .slice(-RECENT_ROWS)
      .reverse()
      .map((x) => {
        const t = isNum(x.temperature_c) ? Number(x.temperature_c).toFixed(2) + ' °C' : 'N/A';
        const b = isNum(x.battery_pct) ? Number(x.battery_pct).toFixed(0) + ' %' : 'N/A';
        return '<li><span class="rt">' + esc(fmtClock(x.received_ms)) + '</span>'
             + '<span class="rv">' + esc(t) + '</span>'
             + '<span class="rb">' + esc(b) + '</span></li>';
      })
      .join('');

    return '<article class="rec" data-device="' + esc(id) + '">'
      + '<header class="rechead">'
      + '<span class="devname">' + esc(id) + '</span>'
      + '<span class="devmac">' + esc(s.mac || '-') + '</span>'
      + '<span class="badge ' + (live ? 'live' : 'stale') + '">' + (live ? '即時' : '過期') + '</span>'
      + '</header>'
      + '<div class="recbig">'
      + '<span class="temp">' + esc(temp) + '</span><span class="unit">°C</span>'
      + '<span class="upd" data-age="' + esc(s.received_ms) + '">更新於 ' + esc(fmtAge(s.received_ms)) + '</span>'
      + '</div>'
      + '<dl class="recmetrics">'
      + '<div><dt>電量</dt><dd>' + esc(bat) + '</dd></div>'
      + '<div><dt>RSSI</dt><dd>' + esc(s.rssi ?? '-') + '</dd></div>'
      + '<div><dt>記錄點</dt><dd>' + esc(s.log_count ?? '-') + '</dd></div>'
      + '</dl>'
      + '<div class="recent">'
      + '<div class="recenthead">最近記錄</div>'
      + '<ul>' + recent + '</ul>'
      + '</div>'
      + '</article>';
  }).join('');
}

function newestReceivedMs() {
  let ms = 0;
  samples.forEach((s) => { ms = Math.max(ms, Number(s.received_ms || 0)); });
  Object.keys(latestById).forEach((id) => { ms = Math.max(ms, Number(latestById[id].received_ms || 0)); });
  return ms;
}

function renderStats() {
  const devices = [...new Set(samples.map((s) => s.device_id))];
  el('devcount').textContent = String(devices.length);

  el('count').textContent = String(samples.length);
  if (samples.length > 1) {
    const spanMs = samples[samples.length - 1].received_ms - samples[0].received_ms;
    el('span').textContent = Math.round(spanMs / 60000) + ' min';
  } else {
    el('span').textContent = '0 min';
  }
}

function render() {
  tempChart.data.datasets = buildDatasets('temperature_c', '溫度 (°C)');
  batteryChart.data.datasets = buildDatasets('battery_pct', '電量 (%)');
  tempChart.update('none');
  batteryChart.update('none');
  renderRecorders();
  renderStats();
}

async function poll() {
  try {
    const res = await fetch(API_URL + '?t=' + Date.now(), { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    lastFetchMs = Date.now();
    // 每次重建整個樣本集（不是 append）
    samples = Array.isArray(data.samples) ? data.samples : [];
    latestById = (data.latest && typeof data.latest === 'object') ? data.latest : {};
    render();

    const newestMs = newestReceivedMs();
    if (!newestMs) {
      setStatus('dead', '無資料');
    } else {
      // ageMs 越大越舊 → 超過門檻才算過期（ageMs > STALE_MS）
      const ageMs = Date.now() - newestMs;
      const backendStale = data.stale === true || data.broker_connected === false;
      const stale = backendStale || ageMs > STALE_MS;
      setStatus(stale ? 'stale' : 'live', stale ? '資料過期' : '資料最新');
    }
  } catch (err) {
    console.error('poll failed:', err);
    if (!samples.length) {
      setStatus('dead', '無資料');
    } else {
      const ageMs = Date.now() - newestReceivedMs();
      setStatus(ageMs > STALE_MS ? 'stale' : 'dead', ageMs > STALE_MS ? '資料過期' : '無資料');
    }
  } finally {
    setTimeout(poll, POLL_MS);
  }
}

// 每秒只更新「更新於 X 秒前」文字，不重新抓資料
function tickAges() {
  document.querySelectorAll('.upd[data-age]').forEach((n) => {
    const ms = Number(n.dataset.age);
    if (!Number.isFinite(ms)) return;
    n.textContent = '更新於 ' + fmtAge(ms);
  });
}

poll();
setInterval(tickAges, 1000);
