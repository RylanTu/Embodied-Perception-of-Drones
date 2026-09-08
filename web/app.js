'use strict';

const $ = id => document.getElementById(id);
const num = id => Number($(id).value);
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const colors = ['#43d8c4', '#ffbf61', '#5ba6ff', '#d58aff', '#ff6679'];
const CSV_COLUMNS = [
  'schema_version', 'batch_id', 'trial_id', 'trial_index', 'phase', 'condition', 'label',
  'obstacle_distance_mm', 'commanded_speed_mm_s', 'accel_mm', 'decel_mm', 'note',
  'host_time', 'esp_uptime_ms', 'sample_sequence',
  'pressure_pa_1', 'pressure_pa_2', 'pressure_pa_3', 'pressure_pa_4', 'pressure_pa_5',
  'temperature_c_1', 'temperature_c_2', 'temperature_c_3', 'temperature_c_4', 'temperature_c_5',
  'valid_mask', 'position_mm', 'moving', 'enabled', 'stopped', 'sensor_test_mode'
];

for (let i = 0; i < 5; i++) {
  $('sensors').insertAdjacentHTML('beforeend', `<div id="sensor${i}" class="sensor"><small>传感器 ${i + 1}</small><b>-- Pa</b><small class="temp">-- °C</small></div>`);
}

let port, reader, writer, heartbeatTimer, rx = '';
let moveRevision = 0, moveStarted = 0, moveRamps = 0, lastDataAt = 0;
let latest = null, totalFrames = 0, rateFrames = 0, rateAt = performance.now();
let history = Array.from({length: 5}, () => []);
let batch = null;
const pendingReplies = new Map();

function log(message) {
  const element = $('log');
  element.textContent += (element.textContent ? '\n' : '') + `[${new Date().toLocaleTimeString()}] ${message}`;
  element.textContent = element.textContent.slice(-16000);
  element.scrollTop = element.scrollHeight;
}

async function send(command) {
  if (!writer) throw new Error('ESP32 尚未连接');
  await writer.write(new TextEncoder().encode(command + '\n'));
}

async function sendCommand(command, timeoutMs = 3000) {
  const name = command.trim().split(/\s+/, 1)[0].toUpperCase();
  return new Promise(async (resolve, reject) => {
    const pending = {resolve, reject, timer: null};
    pending.timer = setTimeout(() => {
      const queue = pendingReplies.get(name) || [];
      const index = queue.indexOf(pending);
      if (index >= 0) queue.splice(index, 1);
      if (!queue.length) pendingReplies.delete(name);
      reject(new Error(`${name} 等待设备确认超时`));
    }, timeoutMs);
    if (!pendingReplies.has(name)) pendingReplies.set(name, []);
    pendingReplies.get(name).push(pending);
    try { await send(command); }
    catch (error) {
      clearTimeout(pending.timer);
      const queue = pendingReplies.get(name) || [], index = queue.indexOf(pending);
      if (index >= 0) queue.splice(index, 1);
      if (!queue.length) pendingReplies.delete(name);
      reject(error);
    }
  });
}

async function connect() {
  if (!navigator.serial) throw new Error('请使用 Chrome 或 Edge，并通过 http://localhost 打开本网页');
  port = await navigator.serial.requestPort();
  await port.open({baudRate: 115200, bufferSize: 65536});
  writer = port.writable.getWriter();
  $('serialState').textContent = 'USB 已连接';
  $('serialState').className = 'pill ok';
  readLoop();
  await send('HELLO');
  await send('STREAM 1');
  clearInterval(heartbeatTimer);
  heartbeatTimer = setInterval(() => send('HEARTBEAT').catch(() => {}), 200);
  log('USB 已连接，已请求 100 Hz 数据流');
}

async function readLoop() {
  const decoder = new TextDecoder();
  reader = port.readable.getReader();
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      rx += decoder.decode(value, {stream: true});
      let newline;
      while ((newline = rx.indexOf('\n')) >= 0) {
        const line = rx.slice(0, newline).trim();
        rx = rx.slice(newline + 1);
        if (line) parseLine(line);
      }
    }
  } catch (error) {
    log('读取失败：' + error.message);
  } finally {
    reader.releaseLock(); reader = null;
    writer?.releaseLock(); writer = null;
    clearInterval(heartbeatTimer);
    for (const queue of pendingReplies.values()) for (const pending of queue) {
      clearTimeout(pending.timer); pending.reject(new Error('USB 已断开'));
    }
    pendingReplies.clear();
    $('serialState').textContent = 'USB 已断开';
    $('serialState').className = 'pill bad';
    if (batch?.active) requestBatchStop('USB 断开');
  }
}

function parseLine(line) {
  if (line[0] !== '{') { log(line); return; }
  let data;
  try { data = JSON.parse(line); } catch { log('无法解析：' + line); return; }
  if (data.type === 'data') onData(data);
  else if (data.type === 'hello') log(`设备就绪，协议 v${data.protocol}`);
  else if (data.type === 'reply') {
    const name = String(data.command || '').toUpperCase(), queue = pendingReplies.get(name);
    const pending = queue?.shift();
    if (queue && !queue.length) pendingReplies.delete(name);
    if (pending) {
      clearTimeout(pending.timer);
      if (data.ok) pending.resolve(data); else pending.reject(new Error(`${name} 失败：${data.message} (${data.code})`));
    }
    if (!data.ok) log(`${name} 失败：${data.message} (${data.code})`);
  }
  else if (data.type === 'status') log(`设备状态：位置 ${data.position_mm} mm，使能=${data.enabled}`);
}

function onData(data) {
  latest = data; lastDataAt = Date.now(); totalFrames++; rateFrames++;
  const now = performance.now();
  if (now - rateAt >= 1000) {
    $('rate').textContent = `${(rateFrames * 1000 / (now - rateAt)).toFixed(1)} Hz`;
    rateFrames = 0; rateAt = now;
  }
  $('totalFrames').textContent = totalFrames;
  $('motion').textContent = `位置 ${Number(data.position_mm).toFixed(3)} mm · ${data.moving ? '运动中' : data.stopped ? '停止锁定' : '停止'}`;
  $('testMode').textContent = data.sensor_test_mode ? 'ESP32 模拟数据' : '真实传感器';
  data.p.forEach((value, index) => {
    const valid = Boolean(data.valid_mask & (1 << index));
    const card = $(`sensor${index}`);
    card.classList.toggle('bad', !valid);
    card.querySelector('b').textContent = valid ? `${Number(value).toFixed(4)} Pa` : '无效';
    card.querySelector('.temp').textContent = valid ? `${Number(data.t[index]).toFixed(2)} °C` : '-- °C';
    history[index].push(valid ? value : null);
    if (history[index].length > 300) history[index].shift();
  });
  drawLive();
  if (batch?.active && batch.recording) recordBatchRow(data);
}

function drawLive() {
  const canvas = $('liveChart'), context = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  context.clearRect(0, 0, w, h); context.strokeStyle = '#24374f';
  for (let i = 1; i < 5; i++) { context.beginPath(); context.moveTo(0, h * i / 5); context.lineTo(w, h * i / 5); context.stroke(); }
  const finite = history.flat().filter(Number.isFinite);
  const low = Math.min(-0.1, ...finite), high = Math.max(0.1, ...finite), span = high - low || 1;
  history.forEach((values, channel) => {
    context.strokeStyle = colors[channel]; context.lineWidth = 2; context.beginPath();
    let started = false;
    values.forEach((value, index) => {
      if (!Number.isFinite(value)) { started = false; return; }
      const x = index * w / 299, y = h - (value - low) / span * h;
      if (started) context.lineTo(x, y); else { context.moveTo(x, y); started = true; }
    });
    context.stroke();
  });
}

function safeName(value) { return (value.trim() || 'batch').replace(/[^0-9A-Za-z_-]+/g, '_').slice(0, 64); }
function csvCell(value) { const text = String(value ?? ''); return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text; }
function commandMotion() { return sendCommand(`CONFIG ${num('softMin')} ${num('softMax')} ${num('pulsesPerMm')} ${num('maxSpeed')}`); }
async function enableMotion() {
  if (latest?.moving) { await sendCommand('STOP'); await waitMotionStopped(); }
  await send('HEARTBEAT'); await sendCommand('CLEAR'); await sendCommand('ENABLE 1');
}
async function moveTo(target, speed, accel, decel) {
  if (batch?.active && batch.stopRequested) throw new Error('批次已停止');
  await sendCommand(`MOVE_MM ${target} ${speed} ${accel} ${decel}`);
  moveRevision = totalFrames; moveStarted = Date.now(); moveRamps = accel + decel;
}

async function waitMotionStopped(timeoutMs = 5000) {
  const after = totalFrames;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (latest && totalFrames > after && !latest.moving) return;
    await delay(50);
  }
  throw new Error('停止后滑台状态未及时释放，请检查设备日志');
}

async function recoverMotionController() {
  if (latest?.moving || latest?.stopped) {
    await sendCommand('STOP');
    await waitMotionStopped();
  }
  await sendCommand('CLEAR');
  await commandMotion();
  await send('HEARTBEAT');
  await sendCommand('ENABLE 1');
}

function readBatchSettings() {
  const settings = {
    count: Math.trunc(num('batchCount')), start: num('scanStart'), end: num('scanEnd'), speed: num('scanSpeed'),
    accel: num('scanAccel'), decel: num('scanDecel'), baselineWait: Math.trunc(num('baselineWait')),
    endDwell: Math.trunc(num('endDwell')), returnSpeed: num('returnSpeed'), returnAccel: num('returnAccel'),
    returnDecel: num('returnDecel'), betweenWait: Math.trunc(num('betweenWait')),
    softMin: num('softMin'), softMax: num('softMax'), maxSpeed: num('maxSpeed'),
    meta: {schema_version: 5, batch_id: safeName($('batchName').value), condition: $('condition').value.trim(),
      label: num('label'), obstacle_distance_mm: num('distance'), note: $('note').value.trim()}
  };
  if (!settings.meta.condition) throw new Error('实验条件不能为空');
  if (!(settings.count >= 1 && settings.count <= 500)) throw new Error('实验次数必须在 1～500 之间');
  if (!(settings.start >= settings.softMin && settings.start <= settings.softMax && settings.end >= settings.softMin && settings.end <= settings.softMax)) throw new Error('测量起点和终点必须位于软限位内');
  if (settings.start === settings.end) throw new Error('测量起点和终点不能相同');
  if (!(settings.speed > 0 && settings.returnSpeed > 0 && settings.speed <= settings.maxSpeed && settings.returnSpeed <= settings.maxSpeed)) throw new Error('测量/复位速度必须大于 0 且不超过最高速度');
  for (const key of ['accel', 'decel', 'baselineWait', 'endDwell', 'returnAccel', 'returnDecel', 'betweenWait']) if (settings[key] < 0) throw new Error('距离和时间参数不能为负数');
  return settings;
}

async function createCsvSink(settings) {
  const stamp = new Date().toISOString().replaceAll(':', '').replaceAll('-', '').slice(0, 15);
  const suggestedName = `${stamp}_${settings.meta.batch_id}.csv`;
  if ('showSaveFilePicker' in window) {
    const handle = await showSaveFilePicker({suggestedName, types: [{description: 'CSV 数据', accept: {'text/csv': ['.csv']}}]});
    const stream = await handle.createWritable();
    await stream.write('\ufeff' + CSV_COLUMNS.join(',') + '\r\n');
    return {stream, memory: null, name: handle.name};
  }
  log('浏览器不支持流式文件写入，将暂存在内存中；建议改用最新版 Chrome/Edge');
  return {stream: null, memory: ['\ufeff' + CSV_COLUMNS.join(',') + '\r\n'], name: suggestedName};
}

function queueCsv(text) {
  if (!batch?.sink) return;
  if (batch.sink.stream) batch.writeQueue = batch.writeQueue.then(() => batch.sink.stream.write(text));
  else batch.sink.memory.push(text);
}

function recordBatchRow(data) {
  if (batch.lastSequence !== null) {
    const gap = (data.seq - batch.lastSequence - 1) >>> 0;
    if (gap < 100000) batch.dropped += gap;
  }
  batch.lastSequence = data.seq;
  const s = batch.settings, trial = batch.currentTrial;
  const values = [s.meta.schema_version, s.meta.batch_id, trial.id, trial.index, 'measure', s.meta.condition,
    s.meta.label, s.meta.obstacle_distance_mm, s.speed, s.accel, s.decel, s.meta.note,
    new Date().toISOString(), data.ms, data.seq, ...data.p, ...data.t, data.valid_mask, data.position_mm,
    +data.moving, +data.enabled, +data.stopped, +data.sensor_test_mode];
  queueCsv(values.map(csvCell).join(',') + '\r\n');
  batch.rows++; trial.rows++;
  if (trial.preview.length < 1600 || trial.rows % Math.ceil(trial.rows / 1600) === 0) {
    trial.preview.push({ms: data.ms, p: [...data.p]});
  }
  $('writtenRows').textContent = batch.rows;
  $('droppedRows').textContent = batch.dropped;
}

async function interruptibleDelay(milliseconds) {
  const end = Date.now() + milliseconds;
  while (Date.now() < end) {
    if (!batch?.active || batch.stopRequested) throw new Error('批次已停止');
    await delay(Math.min(100, end - Date.now()));
  }
}

async function waitPosition(target, speed) {
  const initial = latest?.position_mm ?? target;
  const deadline = Date.now() + (Math.abs(target - initial) + 2 * moveRamps) / Math.max(0.1, speed) * 1000 + 15000;
  while (Date.now() < deadline) {
    if (!batch?.active || batch.stopRequested) throw new Error('批次已停止');
    if (!writer) throw new Error('ESP32连接中断');
    if (Date.now() - Math.max(moveStarted, lastDataAt) > 2000) throw new Error('等待到位时ESP32数据中断');
    if (latest && totalFrames > moveRevision) {
      if (latest.stopped || !latest.enabled) throw new Error('等待到位时滑台已停止锁定或失能');
      const close = Math.abs(latest.position_mm - target) <= 0.2;
      if (close && !latest.moving) return;
    }
    await delay(50);
  }
  throw new Error(`等待滑台到达 ${target} mm 超时`);
}

async function waitWhilePaused() {
  while (batch?.active && batch.pauseRequested && !batch.stopRequested) {
    batch.paused = true; setBatchState('已在起点暂停，点击“继续批次”恢复');
    await delay(100);
  }
  if (batch) batch.paused = false;
}

function setBatchState(text) { $('batchState').textContent = text; log(text); }
function updateProgress(done, total) { $('batchProgress').textContent = `${done} / ${total}`; $('progressBar').style.width = `${total ? done / total * 100 : 0}%`; }
function lockBatchUi(active) { $('startBatch').disabled = active; $('pauseBatch').disabled = !active; $('stopBatch').disabled = !active; }

async function startBatch() {
  if (batch?.active) return;
  if (!writer || !latest) throw new Error('请先连接 ESP32 并确认实时数据正在刷新');
  const settings = readBatchSettings();
  if (latest.sensor_test_mode && !confirm('当前是模拟数据模式。模拟数据不能用于训练，仍要执行联调吗？')) return;
  if (!confirm(`将自动往返 ${settings.count} 次。正向 ${settings.start}→${settings.end} mm，速度 ${settings.speed} mm/s；反向只复位。确认设备旁有人监护吗？`)) return;
  const sink = await createCsvSink(settings); // 必须保持在按钮点击触发的用户手势中
  batch = {active: true, stopRequested: false, pauseRequested: false, paused: false, recording: false,
    settings, sink, writeQueue: Promise.resolve(), rows: 0, dropped: 0, lastSequence: null,
    currentTrial: null, completed: 0, previews: [], finalized: false};
  lockBatchUi(true); updateProgress(0, settings.count); $('writtenRows').textContent = '0'; $('droppedRows').textContent = '0';
  $('pauseBatch').textContent = '完成本次后暂停';
  try {
    await recoverMotionController();
    for (let index = 1; index <= settings.count; index++) {
      await waitWhilePaused();
      if (batch.stopRequested) break;
      setBatchState(`第 ${index}/${settings.count} 次：返回测量起点`);
      await moveTo(settings.start, settings.returnSpeed, settings.returnAccel, settings.returnDecel);
      await waitPosition(settings.start, settings.returnSpeed);
      setBatchState(`第 ${index}/${settings.count} 次：基线稳定等待`);
      await interruptibleDelay(settings.baselineWait);

      batch.currentTrial = {id: `${settings.meta.batch_id}_${String(index).padStart(4, '0')}`, index, rows: 0, preview: []};
      batch.lastSequence = null; batch.recording = true;
      setBatchState(`第 ${index}/${settings.count} 次：正向测量（正在写入 CSV）`);
      await moveTo(settings.end, settings.speed, settings.accel, settings.decel);
      await waitPosition(settings.end, settings.speed);
      await interruptibleDelay(settings.endDwell);
      batch.recording = false;
      if (!batch.currentTrial.rows) throw new Error('本次实验没有收到数据');
      batch.previews.push(batch.currentTrial); batch.completed = index;
      await storeTrialComparison(batch.currentTrial, settings.meta);
      updateProgress(index, settings.count);

      setBatchState(`第 ${index}/${settings.count} 次：反向复位（不写入 CSV）`);
      await moveTo(settings.start, settings.returnSpeed, settings.returnAccel, settings.returnDecel);
      await waitPosition(settings.start, settings.returnSpeed);
      if (index < settings.count) await interruptibleDelay(settings.betweenWait);
    }
    if (!batch.stopRequested) {
      await sendCommand('ENABLE 0');
      setBatchState(`批次完成：${batch.completed} 条独立实验，正在关闭 CSV`);
    }
  } catch (error) {
    batch.recording = false;
    if (!batch.stopRequested) { batch.stopRequested = true; setBatchState('批次失败：' + error.message); }
    await sendCommand('STOP').catch(() => {});
    await waitMotionStopped().catch(() => {});
  } finally {
    await finishBatch();
  }
}

function togglePause() {
  if (!batch?.active) return;
  batch.pauseRequested = !batch.pauseRequested;
  $('pauseBatch').textContent = batch.pauseRequested ? '继续批次' : '完成本次后暂停';
  if (batch.pauseRequested && !batch.paused) setBatchState('已请求暂停，将在本次实验复位到起点后暂停');
}

async function requestBatchStop(reason = '用户停止') {
  if (!batch?.active) return;
  batch.stopRequested = true; batch.recording = false;
  setBatchState(`${reason}，正在停止滑台并保存已采数据`);
  await sendCommand('STOP').catch(() => {});
  await waitMotionStopped().catch(error => log(error.message));
}

async function finishBatch() {
  if (!batch || batch.finalized) return;
  batch.finalized = true; batch.active = false; batch.recording = false;
  await batch.writeQueue;
  if (batch.sink.stream) await batch.sink.stream.close();
  else if (batch.sink.memory) download(new Blob(batch.sink.memory, {type: 'text/csv;charset=utf-8'}), batch.sink.name);
  const interrupted = batch.stopRequested;
  $('batchState').textContent = `${interrupted ? '批次已中止' : '批次已完成'}：${batch.completed}/${batch.settings.count} 条，CSV ${batch.sink.name} 已保存`;
  lockBatchUi(false); $('pauseBatch').textContent = '完成本次后暂停';
  await renderComparison(batch.settings.meta.condition).catch(() => {});
}

function download(blob, name) { const url = URL.createObjectURL(blob), anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
function openDb() { return new Promise((resolve, reject) => { const request = indexedDB.open('drone-pressure-runs', 2); request.onupgradeneeded = () => { if (!request.result.objectStoreNames.contains('runs')) { const store = request.result.createObjectStore('runs', {keyPath: 'id'}); store.createIndex('condition', 'condition'); } }; request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); }

async function storeTrialComparison(trial, meta) {
  const rows = trial.preview, stride = Math.max(1, Math.ceil(rows.length / 1200)), first = rows[0]?.ms ?? 0;
  const run = {id: `${Date.now()}_${trial.id}`, condition: meta.condition, name: trial.id, label: meta.label, time: [], pressure: Array.from({length: 5}, () => [])};
  for (let i = 0; i < rows.length; i += stride) { run.time.push((rows[i].ms - first) / 1000); for (let channel = 0; channel < 5; channel++) run.pressure[channel].push(rows[i].p[channel]); }
  const db = await openDb(); await new Promise((resolve, reject) => { const request = db.transaction('runs', 'readwrite').objectStore('runs').put(run); request.onsuccess = resolve; request.onerror = () => reject(request.error); }); db.close();
}

async function runsFor(condition) { const db = await openDb(); const runs = await new Promise((resolve, reject) => { const request = db.transaction('runs').objectStore('runs').index('condition').getAll(condition); request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error); }); db.close(); return runs; }
async function renderComparison(condition = $('condition').value.trim()) {
  const runs = await runsFor(condition), canvas = $('comparisonChart'), context = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  context.fillStyle = '#07131f'; context.fillRect(0, 0, w, h);
  if (!runs.length) { $('comparisonState').textContent = '当前条件暂无实验'; return; }
  const maxTime = Math.max(1, ...runs.flatMap(run => run.time)), all = runs.flatMap(run => run.pressure.flat()).filter(Number.isFinite);
  const low = Math.min(...all), high = Math.max(...all), span = high - low || 1, rowHeight = h / 5;
  context.font = '12px system-ui';
  for (let channel = 0; channel < 5; channel++) {
    const top = channel * rowHeight; context.strokeStyle = '#29405e'; context.strokeRect(45, top + 6, w - 55, rowHeight - 12); context.fillStyle = '#91a6bf'; context.fillText(`CH${channel + 1}`, 7, top + 22);
    runs.forEach((run, index) => { context.strokeStyle = colors[index % colors.length]; context.lineWidth = 1.2; context.beginPath(); run.pressure[channel].forEach((value, i) => { const x = 45 + run.time[i] / maxTime * (w - 55), y = top + rowHeight - 10 - (value - low) / span * (rowHeight - 20); if (i) context.lineTo(x, y); else context.moveTo(x, y); }); context.stroke(); });
  }
  context.fillStyle = '#edf4fc'; runs.slice(-12).forEach((run, index) => context.fillText(`${index + 1}:${run.name}[${run.label}]`, 55 + index % 6 * 180, 16 + Math.floor(index / 6) * 15));
  $('comparisonState').textContent = `条件“${condition}”共有 ${runs.length} 条独立实验；标签 0=无障碍，1=有障碍。`;
}

async function clearCondition() { const condition = $('condition').value.trim(), runs = await runsFor(condition); if (!confirm(`删除浏览器中“${condition}”的 ${runs.length} 条预览缓存？原始 CSV 不会删除。`)) return; const db = await openDb(); await new Promise((resolve, reject) => { const transaction = db.transaction('runs', 'readwrite'), store = transaction.objectStore('runs'); runs.forEach(run => store.delete(run.id)); transaction.oncomplete = resolve; transaction.onerror = () => reject(transaction.error); }); db.close(); await renderComparison(condition); }

$('connect').onclick = () => connect().catch(error => alert(error.message));
$('startBatch').onclick = () => startBatch().catch(error => { log(error.message); if (error.name !== 'AbortError') alert(error.message); });
$('pauseBatch').onclick = togglePause;
$('stopBatch').onclick = () => requestBatchStop().catch(error => alert(error.message));
$('applyMotion').onclick = () => commandMotion().catch(error => alert(error.message));
$('enable').onclick = () => enableMotion().catch(error => alert(error.message));
$('move').onclick = () => moveTo(num('target'), num('moveSpeed'), num('manualAccel'), num('manualDecel')).catch(error => alert(error.message));
$('disable').onclick = () => sendCommand('ENABLE 0').catch(error => alert(error.message));
$('stop').onclick = () => (batch?.active ? requestBatchStop('软件停止') : sendCommand('STOP').then(() => waitMotionStopped())).catch(error => alert(error.message));
$('applyTest').onclick = () => sendCommand(`TEST ${$('testEnabled').value} ${num('testBaseline')} ${num('testAmplitude')} ${num('testPeriod')} ${num('testWidth')}`).catch(error => alert(error.message));
$('downloadPng').onclick = () => $('comparisonChart').toBlob(blob => download(blob, `${safeName($('condition').value)}_comparison.png`));
$('clearCondition').onclick = () => clearCondition().catch(error => alert(error.message));
$('condition').onchange = () => renderComparison().catch(() => {});
renderComparison().catch(() => {});
