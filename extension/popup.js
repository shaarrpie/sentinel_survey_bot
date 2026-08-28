const startBtn  = document.getElementById('start');
const stopBtn   = document.getElementById('stop');
const openHudBtn = document.getElementById('openhud');
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
const hubInput = document.getElementById('hubInput');
const saveHubBtn = document.getElementById('saveHub');
const clearHubBtn = document.getElementById('clearHub');
const grabTabBtn = document.getElementById('grabTab');
const hubStatusEl = document.getElementById('hubStatus');

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
// entries, so an 'ok' line mentioning "failed" never renders red (audit R2-B2).
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
function renderRunState(resp) {
    const running = !!(resp && resp.running);
    runId = resp ? resp.runId : null;
    document.body.classList.toggle('running', running);
    // Stamp comes from PERSISTED runState now, not just the moment-of-start
    // ack — otherwise the next refresh tick strips it back to bare RUNNING
    // and a stale deployed build becomes invisible again (audit R7-A).
    stateword.textContent = running
        ? 'RUNNING' + (resp && resp.build ? ' · b' + resp.build : '')
        : 'STOPPED';
    startBtn.disabled = running;
    stopBtn.disabled = !running;
    uptimeEl.textContent = (running && runId)
        ? fmtUptime(Date.now() - runId) : '--:--:--';
    tabinfoEl.textContent = (running && resp && resp.tabId)
        ? `tab ${resp.tabId}` : '';
    return running;
}
function refresh() {
    // Sequential, not concurrent: `alert` must be judged against the settled
    // running class, not whichever response lands first (audit C race).
    chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, (resp) => {
        void chrome.runtime.lastError;
        const running = renderRunState(resp);
        chrome.runtime.sendMessage({ action: 'GET_LOGS' }, (lresp) => {
            void chrome.runtime.lastError;
            let err = 0;
            if (lresp && Array.isArray(lresp.logs)) err = syncLogs(lresp.logs);
            document.body.classList.toggle('alert', running && err > 0);
        });
    });
}

function sendToTab(tabId, msg) {
    return new Promise((resolve) => {
        chrome.tabs.sendMessage(tabId, msg, (resp) => {
            void chrome.runtime.lastError;   // no receiver → resp undefined
            resolve(resp || null);
        });
    });
}

startBtn.addEventListener('click', async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https?:/.test(tab.url || '')) {
        stateword.textContent = "CAN'T RUN HERE";
        return;
    }
    const newRunId = Date.now();
    stateword.textContent = 'LINKING…';
    let ack = await sendToTab(tab.id, { action: 'START', runId: newRunId });
    if (!ack) {
        // Page loaded before the extension (dev reloads do this constantly):
        // no content script is listening. Inject it via the scripting API
        // and ask again before claiming the run is live (audit A3).
        try {
            await chrome.scripting.executeScript({
                target: { tabId: tab.id },
                files: ['content.js']
            });
        } catch (e) { /* restricted page — fall through */ }
        ack = await sendToTab(tab.id, { action: 'START', runId: newRunId });
    }
    if (!ack) {
        stateword.textContent = 'NO CONTENT SCRIPT';
        refresh();
        return;
    }
    // Content script acknowledged — only now flip the UI to RUNNING.
    // Show the acked build so a stale deployed content.js is visible at a
    // glance (round six, R6-A).
    stateword.textContent = 'RUNNING' +
        (ack.build ? ` · b${ack.build}` : '');
    chrome.runtime.sendMessage({
        action: 'SET_RUN_STATE', running: true, tabId: tab.id, runId: newRunId,
        build: (ack && ack.build) || null   // R7-A: carry the stamp through
    });
    refresh();
});

stopBtn.addEventListener('click', async () => {
    chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, async (resp) => {
        void chrome.runtime.lastError;
        if (resp && resp.tabId) await sendToTab(resp.tabId, { action: 'STOP' });
    });
    chrome.runtime.sendMessage({
        action: 'SET_RUN_STATE', running: false, tabId: null, runId: null
    });
    refresh();
});

openHudBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'OPEN_HUD' });
});

openDebugBtn.addEventListener('click', () => {
    chrome.tabs.create({ url: chrome.runtime.getURL('traces/debug.html') });
});

// ─── Panel Hub config (user-editable stop-domains) ────────────────
function setHubStatus(text, cls) {
    hubStatusEl.textContent = text;
    hubStatusEl.className = 'hubstatus' + (cls ? ' ' + cls : '');
}
function renderHubs(list) {
    if (!list || !list.length) {
        setHubStatus('no panel hubs configured');
    } else {
        setHubStatus(`${list.length} active: ${list.join(', ')}`);
    }
}
function refreshHubs() {
    chrome.runtime.sendMessage({ action: 'GET_PANEL_HUB' }, (resp) => {
        void chrome.runtime.lastError;
        // Distinguish "no hubs" from "can't reach the config store" — an
        // error reported as empty would make saved hubs look wiped (B.2.1).
        if (!resp || resp.error) {
            setHubStatus(`hub sync failed — ${((resp && resp.error) ||
                'background not responding')} (stopping still uses last-known list)`,
                'err');
            return;
        }
        renderHubs(resp.panel_hubs);
    });
}
async function saveHub(urlOverride) {
    const raw = (urlOverride !== undefined ? urlOverride : hubInput.value).trim();
    if (!raw) { hubInput.focus(); return; }
    // Cheap client-side shape check so "banana" never takes a round-trip
    // through the backend (B.2.3). The backend re-normalizes regardless.
    try {
        new URL(/^https?:\/\//i.test(raw) ? raw : 'https://' + raw);
    } catch (e) {
        setHubStatus(`not a valid URL: ${raw.slice(0, 60)}`, 'err');
        return;
    }
    saveHubBtn.disabled = true;
    setHubStatus('saving…');
    chrome.runtime.sendMessage({ action: 'SET_PANEL_HUB', url: raw }, (resp) => {
        void chrome.runtime.lastError;
        saveHubBtn.disabled = false;
        if (!resp || resp.error) {
            setHubStatus(`save failed: ${(resp && resp.error) || 'no response from background'} ` +
                '(is the backend running?)', 'err');
            return;
        }
        renderHubs(resp.panel_hubs);
        hubInput.value = '';
        const savedTxt = saveHubBtn.textContent;
        saveHubBtn.textContent = '✓';
        setTimeout(() => { saveHubBtn.textContent = savedTxt; }, 900);
    });
}
saveHubBtn.addEventListener('click', () => saveHub());
// One-click capture: you are standing ON the login wall when you want to
// register a hub, so save the active tab's URL directly (wires urlOverride).
// Confirmation guards the "brick a host in one click" failure mode
// (round nine, D.2): host matching blocks EVERY page on the domain.
grabTabBtn.addEventListener('click', async () => {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https?:/.test(tab.url || '')) {
        setHubStatus('current tab has no http(s) URL to use', 'err');
        return;
    }
    let host = '';
    try { host = new URL(tab.url).hostname; } catch (e) {}
    const ok = confirm(
        `Register "${host}" as a panel hub?\n\n` +
        'The bot will refuse to act on EVERY page of this host —\n' +
        'surveys included. Use it only for panel login walls.');
    if (!ok) {
        setHubStatus('capture cancelled');
        return;
    }
    saveHub(tab.url);
});
clearHubBtn.addEventListener('click', () => {
    clearHubBtn.disabled = true;
    chrome.runtime.sendMessage({ action: 'SET_PANEL_HUB', url: '' }, (resp) => {
        void chrome.runtime.lastError;
        clearHubBtn.disabled = false;
        if (!resp || resp.error) {
            setHubStatus(`clear failed: ${(resp && resp.error) || 'no response'}`, 'err');
            return;
        }
        renderHubs(resp.panel_hubs);
    });
});
hubInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveHub(); }
});

function probeBrain() {
  chrome.runtime.sendMessage({ action: 'GET_STATUS' }, (resp) => {
    void chrome.runtime.lastError;
    if (!resp || resp.error || !resp.data) {
      pulseEl.className = 'pulse offline';
      pulseTxt.textContent = 'backend: offline';
      return;
    }

    const status = resp.data;
    const provider = status.provider || status.router || {};
    const model = provider.model ||
      (status.omni && status.omni.model) || '?';

    if (status.dry_run) {
      pulseEl.className = 'pulse dry';
      pulseTxt.textContent = `brain: DRY | ${model}`;
    } else if (provider.api_ready) {
      pulseEl.className = 'pulse online';
      pulseTxt.textContent = `brain: provider | ${model}`;
    } else {
      pulseEl.className = 'pulse offline';
      const reason = provider.error || provider.last_error ||
        provider.api_error || 'not ready';
      pulseTxt.textContent = `brain: heuristic | ${reason}`;
    }
  });
}

refresh();
probeBrain();
refreshHubs();
setInterval(() => { refresh(); probeBrain(); refreshHubs(); }, 2000);
// Smooth 1s uptime ticker between the 2s refresh cycles
setInterval(() => {
    if (document.body.classList.contains('running') && runId)
        uptimeEl.textContent = fmtUptime(Date.now() - runId);
}, 1000);
