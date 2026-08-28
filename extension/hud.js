const startBtn  = document.getElementById('start');
const stopBtn   = document.getElementById('stop');
const closeBtn  = document.getElementById('close');
const openDebugBtn = document.getElementById('opendebug');
const stateword = document.getElementById('stateword');
const uptimeEl  = document.getElementById('uptime');
const logsDiv   = document.getElementById('logs');
const bufferEl  = document.getElementById('buffer');
const tabinfoEl = document.getElementById('tabinfo');
const pulseEl = document.getElementById('pulse');
const pulseTxt = document.getElementById('pulseTxt');
const stDec = document.getElementById('stDec');
const stAct = document.getElementById('stAct');
const stErr = document.getElementById('stErr');
const dragbar = document.getElementById('dragbar');

let known = [];
let runId = null;

function hhmmss(t) {
    const d = new Date(t);
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
        .map(n => String(n).padStart(2, '0')).join(':');
}
function fmtUptime(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return [h, m, s % 60].map(n => String(n).padStart(2, '0')).join(':');
}
// Kind tags decide first — regexes are only a fallback for legacy untagged
// entries (audit round two, B2).
function classify(e) {
    const k = e ? e.kind : null;
    if (k === 'err')  return 'err';
    if (k === 'warn') return 'warn';
    if (k === 'ai')   return 'ai';
    if (k === 'act')  return 'act';
    if (k === 'ok')   return 'ok';
    if (k === 'dim')  return 'dim';
    const line = e && e.line != null ? String(e.line) : '';
    if (/failed|backend error|\[!\]/i.test(line)) return 'err';
    if (/drifted|stuck|no response/i.test(line))  return 'warn';
    if (/^\[\+\]/.test(line))  return 'ok';
    if (/^\[AI\]/.test(line))  return 'ai';
    if (/^ +->/.test(line))    return 'act';
    if (/Summary:/.test(line)) return 'dim';
    return 'info';
}
function addRow(e) {
    const t = e.t;
    const line = e.line != null ? String(e.line) : '';
    const row = document.createElement('div');
    row.className = 'row ' + classify(e);
    const time = document.createElement('time');
    time.textContent = hhmmss(t);
    const msg = document.createElement('span');
    msg.className = 'msg';
    msg.textContent = line;
    row.appendChild(time); row.appendChild(msg);
    logsDiv.appendChild(row);
}
function syncLogs(logs) {
    const rolled = known.length &&
        (logs.length < known.length || logs[0].t !== known[0]);
    if (rolled) { logsDiv.innerHTML = ''; known = []; }
    const fresh = logs.slice(known.length);
    const nearBottom =
        logsDiv.scrollHeight - logsDiv.scrollTop - logsDiv.clientHeight < 28;
    if (fresh.length) {
        const empty = logsDiv.querySelector('.empty');
        if (empty) empty.remove();
        fresh.forEach(e => addRow(e));
        if (nearBottom) logsDiv.scrollTop = logsDiv.scrollHeight;
    }
    known = logs.map(e => e.t);
    let dec = 0, act = 0, err = 0;
    for (const e of logs) {
        const c = classify(e);
        if (c === 'ai') dec++;
        else if (c === 'act') act++;
        else if (c === 'err') err++;
    }
    stDec.textContent = dec;
    stAct.textContent = act;
    stErr.textContent = err;
    bufferEl.textContent = `buffer ${logs.length}/200`;
    return err;
}

function refresh() {
    chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, (resp) => {
        void chrome.runtime.lastError;
        const running = !!(resp && resp.running);
        runId = resp ? resp.runId : null;
        document.body.classList.toggle('running', running);
        if (!stateword.classList.contains('flash'))
            stateword.textContent = running ? 'RUNNING' : 'STOPPED';
        startBtn.disabled = running;
        stopBtn.disabled = !running;
        uptimeEl.textContent = (running && runId)
            ? fmtUptime(Date.now() - runId) : '--:--:--';
        tabinfoEl.textContent = (running && resp && resp.tabId)
            ? `tab ${resp.tabId}` : '';
    });
    chrome.runtime.sendMessage({ action: 'GET_LOGS' }, (resp) => {
        void chrome.runtime.lastError;
        if (resp && Array.isArray(resp.logs)) {
            const err = syncLogs(resp.logs);
            document.body.classList.toggle('alert',
                document.body.classList.contains('running') && err > 0);
        }
    });
}

function flashState(text) {
    stateword.textContent = text;
    stateword.classList.add('flash');
    setTimeout(() => stateword.classList.remove('flash'), 1400);
}

function targetTab(cb) {
    chrome.windows.getLastFocused({ windowTypes: ['normal'] }, (win) => {
        if (chrome.runtime.lastError || !win) return cb(null);
        chrome.tabs.query({ active: true, windowId: win.id },
            (tabs) => cb(tabs && tabs[0]));
    });
}

startBtn.addEventListener('click', () => {
    targetTab((tab) => {
        if (!tab || !/^https?:/.test(tab.url || '')) {
            flashState('NO HTTPS TAB');
            return;
        }
        const newRunId = Date.now();
        chrome.tabs.sendMessage(tab.id, { action: 'START', runId: newRunId }, (ack) => {
            void chrome.runtime.lastError;
            chrome.runtime.sendMessage({
                action: 'SET_RUN_STATE', running: true, tabId: tab.id,
                runId: newRunId, build: (ack && ack.build) || null   // R7-A
            });
        });
        refresh();
    });
});

stopBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, (resp) => {
        void chrome.runtime.lastError;
        if (resp && resp.tabId)
            chrome.tabs.sendMessage(resp.tabId, { action: 'STOP' }, () => void chrome.runtime.lastError);
    });
    chrome.runtime.sendMessage({
        action: 'SET_RUN_STATE', running: false, tabId: null, runId: null
    });
    refresh();
});

closeBtn.addEventListener('click', () => window.close());

openDebugBtn.addEventListener('click', () => {
    chrome.tabs.create({ url: chrome.runtime.getURL('traces/debug.html') });
});

function probeBrain() {
    // Single source of truth: ask the service worker (which owns BACKEND).
    chrome.runtime.sendMessage({ action: 'GET_STATUS' }, (resp) => {
        void chrome.runtime.lastError;
        if (!resp || resp.error || !resp.data) {
            pulseEl.className = 'pulse offline';
            pulseTxt.textContent = 'brain: offline';
            return;
        }
        const s = resp.data;
        pulseEl.className = 'pulse ' + (s.dry_run ? 'dry' : 'online');
        const o = s.omni || {};
        const omniTxt = o.loaded
            ? ` · omni ${o.provider || 'ok'}`
            : (s.omni ? ' · omni DOWN' : '');
        pulseTxt.textContent = (s.dry_run
            ? `brain: DRY (${s.traces} traces)`
            : `brain: online (${s.traces} traces)`) + omniTxt;
    });
}

let drag = null;
dragbar.addEventListener('mousedown', (e) => {
    if (e.target.closest('#close')) return;
    chrome.windows.getCurrent((w) => {
        drag = { id: w.id, startX: e.screenX, startY: e.screenY,
                 left: w.left, top: w.top, last: null };
        document.body.classList.add('dragging');
    });
});
window.addEventListener('mousemove', (e) => {
    if (!drag) return;
    const left = Math.round(drag.left + (e.screenX - drag.startX));
    const top  = Math.round(drag.top  + (e.screenY - drag.startY));
    drag.last = { left, top };
    chrome.windows.update(drag.id, { left, top });
});
window.addEventListener('mouseup', () => {
    if (drag && drag.last) chrome.storage.local.set({ hudPos: drag.last });
    drag = null;
    document.body.classList.remove('dragging');
});

refresh();
probeBrain();
setInterval(() => { refresh(); probeBrain(); }, 2000);
