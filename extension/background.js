try { importScripts('config.local.js'); } catch (_) {}

const LOCAL = self.SENTINEL_LOCAL_CONFIG || {};
const BACKEND = LOCAL.backend || 'http://127.0.0.1:8000';
const SENTINEL_TOKEN = LOCAL.token || '';

// Single shared matcher for CHECK_PANEL_HUB (tested verbatim by the
// Node fixture — see extension/hub_match.js header).
importScripts('hub_match.js');

const MAX_LOGS = 200;
let logs = [];              // [{t, line}] — newest last

const lastPointer = new Map();
const lastCaptureAt = new Map();   // windowId → ts (captureVisibleTab throttle)
const perFrameMaps = new Map();    // tabId → Map<frameId, {elements, isTop, at}>

// MV3 evicts this service worker after ~30s idle while any debugger session
// it opened survives — so the log buffer lives in session storage, not in
// worker memory, and popup roll-detection survives eviction (audit A2).
let logsReady = false;   // boot-race guard (round two, B3)
function saveLogs() {
  if (!logsReady) return;      // never clobber stored history mid-boot
  try { chrome.storage.session.set({ sentinelLogs: logs }); } catch (e) {}
}
chrome.storage.session.get('sentinelLogs', (result) => {
  const sentinelLogs = (result && result.sentinelLogs) || [];
  if (!logs.length && Array.isArray(sentinelLogs)) {
    logs = sentinelLogs.filter(e => e && typeof e.line === 'string').slice(-MAX_LOGS);
  }
  logsReady = true;
  saveLogs();
});

function authHeaders() {
    const h = { 'Content-Type': 'application/json' };
    if (SENTINEL_TOKEN) h['X-Sentinel-Token'] = SENTINEL_TOKEN;
    return h;
}

function setToolbarBadge(running) {
    chrome.action.setBadgeText({ text: running ? 'ON' : '' });
    chrome.action.setBadgeBackgroundColor({ color: '#1f7a4d' });
}

function createHud() {
    chrome.storage.local.get('hudPos', ({ hudPos }) => {
        const pos = hudPos || { left: 980, top: 64 };
        chrome.windows.create({
            url: chrome.runtime.getURL('hud.html'),
            type: 'popup',
            width: 360, height: 560,
            left: pos.left, top: pos.top,
            focused: true
        }, (w) => {
            if (w) chrome.storage.session.set({ hudWindowId: w.id });
        });
    });
}

function cdp(tabId, params) {
  return new Promise((resolve, reject) => {
    chrome.debugger.sendCommand({ tabId },
      'Input.dispatchMouseEvent', params, (result) => {
        if (chrome.runtime.lastError)
          reject(chrome.runtime.lastError);
        else resolve(result);
      });
  });
}

function stitchFrameMaps(tabId) {
  const frames = perFrameMaps.get(tabId);
  if (!frames || frames.size === 0) return [];
  const stitched = [];
  for (const [frameId, data] of frames) {
    for (const el of (data.elements || [])) {
      stitched.push({ ...el, frameId });
    }
  }
  return stitched;
}

function gauss(){ return (Math.random()+Math.random()+Math.random()-1.5)*2.4; }

function bezierPath(sx, sy, tx, ty, n) {
  const dx = tx - sx, dy = ty - sy;
  const dist = Math.max(1, Math.hypot(dx, dy));
  const px = -dy / dist, py = dx / dist;        // perpendicular unit
  const b1 = (Math.random() * 0.17 + 0.08) * dist * (Math.random() < 0.5 ? -1 : 1);
  const b2 = (Math.random() * 0.17 + 0.08) * dist * (Math.random() < 0.5 ? -1 : 1);
  const p1x = sx + dx * 0.33 + px * b1, p1y = sy + dy * 0.33 + py * b1;
  const p2x = sx + dx * 0.66 + px * b2, p2y = sy + dy * 0.66 + py * b2;
  const pts = [];
  for (let i = 1; i <= n; i++) {
    const t = i / n;
    const s = 10 * t ** 3 - 15 * t ** 4 + 6 * t ** 5;  // min-jerk ease
    const u = 1 - s;
    let x = u**3 * sx + 3 * u**2 * s * p1x + 3 * u * s**2 * p2x + s**3 * tx;
    let y = u**3 * sy + 3 * u**2 * s * p1y + 3 * u * s**2 * p2y + s**3 * ty;
    if (i < n) { x += gauss(); y += gauss(); }          // tremor mid-path only
    pts.push({ x: Math.round(x), y: Math.round(y) });
  }
  return pts;   // last point is exactly (tx, ty) — press lands clean
}

// Never trust an in-memory Set across SW restarts: ask the browser whether
// the tab is really attached right now (audit round one, A1/A2).
function isDebuggerAttached(tabId) {
  return new Promise((resolve) => {
    chrome.debugger.getTargets((targets) => resolve(
      Array.isArray(targets) &&
      targets.some(t => t.tabId === tabId && t.attached)));
  });
}

// Attach race: two concurrent TRUSTED_CLICKs for the same tab both
// observed "not attached" and both called chrome.debugger.attach; the
// second rejects (debugger busy) and that click silently failed. One
// in-flight attach per tab; callers share the promise.
const attachPromises = new Map();
function ensureDebuggerAttached(tabId) {
  if (!attachPromises.has(tabId)) {
    attachPromises.set(tabId,
      isDebuggerAttached(tabId).then((attached) => attached ? true :
        new Promise((res, rej) => chrome.debugger.attach(
          { tabId }, '1.3',
          () => chrome.runtime.lastError
            ? rej(chrome.runtime.lastError) : res(true))))
        .finally(() => attachPromises.delete(tabId)));
  }
  return attachPromises.get(tabId);
}

async function trustedMouseClick(tabId, vp) {
  try {
    await ensureDebuggerAttached(tabId);
    const tx = Math.round(vp.x + (Math.random() - 0.5) * vp.w * 0.64);
    const ty = Math.round(vp.y + (Math.random() - 0.5) * vp.h * 0.64);
    const start = lastPointer.get(tabId) ||
      { x: 120 + Math.random() * 300, y: 120 + Math.random() * 200 };
    const dist = Math.hypot(tx - start.x, ty - start.y);
    const n = Math.max(18, Math.min(60, Math.round(dist / 14)));
    const pts = bezierPath(start.x, start.y, tx, ty, n);
    const per = Math.max(260, Math.min(1400, 320 + 2.2 * dist)) / n;
    for (const p of pts) {
      await cdp(tabId, { type: 'mouseMoved', x: p.x, y: p.y, button: 'none' });
      await new Promise(r => setTimeout(r, per * (0.7 + Math.random() * 0.6)));
    }
    await new Promise(r => setTimeout(r, 40 + Math.random() * 80));
    await cdp(tabId, { type: 'mousePressed', x: tx, y: ty, button: 'left', clickCount: 1 });
    await new Promise(r => setTimeout(r, 60 + Math.random() * 80));
    await cdp(tabId, { type: 'mouseReleased', x: tx, y: ty, button: 'left', clickCount: 1 });
    lastPointer.set(tabId, { x: tx, y: ty });
    return { ok: true };
  } catch (err) { return { ok: false, error: String(err) }; }
}

async function fetchJson(url, options = {}, timeoutMs = 45000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const startedAt = Date.now();
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (error) {
      throw new Error(`HTTP ${response.status} returned non-JSON: ${text.slice(0, 160)}`);
    }
    if (!response.ok) {
      const detail = data && (data.detail || data.error);
      throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ''}`);
    }
    return { data, elapsedMs: Date.now() - startedAt };
  } catch (error) {
    if (error && error.name === 'AbortError') {
      throw new Error(`backend timeout after ${timeoutMs}ms: ${url}`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

// captureVisibleTab grabs the ACTIVE tab in the window, which may not be the
// tab that requested the screenshot if the user switched tabs mid-scan.
function captureSenderTab(tab) {
  return new Promise((resolve, reject) => {
    if (!tab || tab.id == null || tab.windowId == null) {
      reject(new Error('capture request has no sender tab'));
      return;
    }
    chrome.tabs.query({ active: true, windowId: tab.windowId }, tabs => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!tabs[0] || tabs[0].id !== tab.id) {
        reject(new Error('sender tab is no longer active; screenshot deferred'));
        return;
      }
      chrome.tabs.captureVisibleTab(
        tab.windowId,
        { format: 'jpeg', quality: 70 },
        dataUrl => {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else {
            resolve(dataUrl);
          }
        }
      );
    });
  });
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'CAPTURE_SCREENSHOT') {
    const senderTab = sender.tab || null;
    // captureVisibleTab is rate-limited (~2/sec) — space captures out
    const winId = senderTab ? senderTab.windowId : null;
    const gap = 550 - (Date.now() - (lastCaptureAt.get(winId) || 0));
    const shoot = () => {
      lastCaptureAt.set(winId, Date.now());
      captureSenderTab(senderTab)
        .then(dataUrl => sendResponse({ screenshot: dataUrl }))
        .catch(err => sendResponse({ error: err.message }));
    };
    if (gap > 0) setTimeout(shoot, gap); else shoot();
    return true;
  }

  if (request.action === 'CALL_BACKEND') {
    const cycle = request.payload && request.payload.cycle_id;
    fetchJson(`${BACKEND}/decide`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify(request.payload)
    }, 45000)
      .then(({ data, elapsedMs }) => sendResponse({
        data,
        elapsedMs,
        cycle,
        dryRun: !!(data && data.dry_run)
      }))
      .catch(error => sendResponse({
        error: error.message,
        cycle
      }));
    return true;
  }

  if (request.action === 'LEARN_RULE') {
    fetch(`${BACKEND}/learn`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ memory: request.memory })
    }).catch(() => {});
    sendResponse({ ok: true });
  }

  if (request.action === 'TRUSTED_CLICK') {
    const tabId = sender.tab ? sender.tab.id : null;
    trustedMouseClick(tabId, request.vp).then(sendResponse);
    return true;
  }

  if (request.action === 'REPORT_FRAME_MAP') {
    const tabId = sender.tab ? sender.tab.id : null;
    const frameId = request._frameId || (sender.frameId || 'unknown');
    if (!perFrameMaps[tabId]) perFrameMaps[tabId] = new Map();
    perFrameMaps[tabId].set(frameId, {
      elements: request.elements || [],
      frameId,
      isTop: request._isTop || false,
      at: Date.now()
    });
    sendResponse({ ok: true });
    return true;
  }

  if (request.action === 'SCAN_ALL_FRAMES') {
    const tabId = sender.tab ? sender.tab.id : null;
    if (!tabId) { sendResponse({ error: 'no tabId' }); return true; }
    chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      func: () => {
        try {
          const FRAME = (function() {
            try { return window === top ? 'top' : (window.frameElement ? ('frame-' + (window.frameElement.id || 'unknown')) : 'frame-unknown'); }
            catch (e) { return 'cross-origin'; }
          })();
          window.__sentinelGetLocalMap = window.__sentinelGetLocalMap || function() {
            const out = [];
            const view = document.defaultView;
            const nodes = document.querySelectorAll(
              'button, input, select, textarea, a, [role="button"], [role="radio"], ' +
              '[role="checkbox"], [role="slider"], label, [contenteditable]'
            );
            for (const el of nodes) {
              try {
                const cs = view.getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden' || Number.parseFloat(cs.opacity) === 0) continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 5 || rect.height < 5) continue;
                const semanticType = (el.type || el.getAttribute('role') || el.tagName.toLowerCase());
                out.push({
                  tag: el.tagName.toLowerCase(),
                  type: (el.type || '').toLowerCase(),
                  role: (el.getAttribute('role') || '').toLowerCase(),
                  name: (el.name || '').toLowerCase(),
                  value: (el.value || '').toString().slice(0, 200),
                  text: (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 200),
                  x: Math.round(rect.left + rect.width / 2),
                  y: Math.round(rect.top + rect.height / 2),
                  semanticType,
                  accessibleName: (el.getAttribute('aria-label') || el.innerText || '').trim().slice(0, 200),
                  frameId: FRAME
                });
              } catch (e) {}
            }
            return out;
          };
          return { frameId: FRAME, elements: window.__sentinelGetLocalMap() };
        } catch (e) {
          return { frameId: 'error', elements: [], error: e.message };
        }
      }
    }).then((results) => {
      const tabId2 = tabId;
      const stitched = [];
      for (const result of (results || [])) {
        if (result && result.frameId && Array.isArray(result.elements)) {
          for (const el of result.elements) {
            stitched.push({ ...el, frameId: result.frameId });
          }
          perFrameMaps[tabId2] = perFrameMaps[tabId2] || new Map();
          perFrameMaps[tabId2].set(result.frameId, {
            elements: result.elements,
            frameId: result.frameId,
            isTop: result.frameId === 'top',
            at: Date.now()
          });
        }
      }
      sendResponse({ ok: true, stitched, frameCount: (results || []).length });
    }).catch((e) => sendResponse({ error: e.message }));
    return true;
  }

  if (request.action === 'SET_RUN_STATE') {
    chrome.storage.session.set({
      runState: {
        running: request.running,
        tabId: request.tabId ?? null,
        runId: request.runId ?? null,
        build: request.build ?? null   // R7-A: persist so the stamp
                                       // survives refresh() clobbering
      }
    });
    setToolbarBadge(!!request.running);
    sendResponse({ ok: true });
  }

  if (request.action === 'GET_RUN_STATE') {
    const myTabId = sender.tab ? sender.tab.id : null;
    const report = (runState) => {
      const resume = !!(runState && runState.running &&
                        runState.tabId === myTabId);
      sendResponse({
        resume,
        running: !!(runState && runState.running),
        tabId: runState ? runState.tabId : null,
        runId: runState ? runState.runId : null,
        build: runState ? runState.build : null   // R7-A
      });
    };
    chrome.storage.session.get('runState', (result) => {
      const runState = (result && result.runState) || null;
      if (!runState || !runState.running || runState.tabId == null) {
        report(runState);
        return;
      }
      // Zombie guard: if the run tab vanished without firing onRemoved,
      // clear the ghost instead of ticking uptime for it (audit A4).
      chrome.tabs.get(runState.tabId, () => {
        if (chrome.runtime.lastError) {
          chrome.storage.session.set({
            runState: { running: false, tabId: null, runId: null }
          });
          setToolbarBadge(false);
          report(null);
          return;
        }
        report(runState);
      });
    });
    return true;
  }

  if (request.action === 'OPEN_HUD') {
    chrome.storage.session.get('hudWindowId', (result) => {
      const hudWindowId = (result && result.hudWindowId) != null ? result.hudWindowId : null;
      if (hudWindowId != null) {
        chrome.windows.get(hudWindowId, (w) => {
          if (chrome.runtime.lastError || !w) createHud();
          else chrome.windows.update(hudWindowId, { focused: true });
        });
      } else {
        createHud();
      }
    });
    sendResponse({ ok: true });
  }

  if (request.action === 'LOG') {
    console.log('[Sentinel]', request.line);
    logs.push({
      t: Date.now(),
      line: String(request.line),
      kind: typeof request.kind === 'string' ? request.kind : null
    });
    if (logs.length > MAX_LOGS) logs = logs.slice(-MAX_LOGS);
    saveLogs();
    sendResponse({ ok: true });
  }

  if (request.action === 'GET_LOGS') {
    sendResponse({ logs });
  }

  if (request.action === 'GET_STATUS') {
    // Single source of truth for the backend URL: the popup asks us (audit B)
    fetch(`${BACKEND}/status`, { headers: authHeaders() })
      .then(r => r.json().then(data => ({ ok: r.ok, data })))
      .then(({ ok, data }) => ok ? sendResponse({ data })
                                 : sendResponse({ error: 'HTTP error' }))
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }

  if (request.action === 'GET_PANEL_HUB') {
    // Source of truth is the backend's panel_config.json — it survives
    // browser restarts AND is shared with core.py's Playwright flow. The
    // response is mirrored into chrome.storage.local so CHECK_PANEL_HUB
    // enforcement in content.js keeps working even while the backend is
    // briefly unreachable.
    fetch(`${BACKEND}/config/panel-hub`, { headers: authHeaders() })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => {
        if (data && Array.isArray(data.panel_hubs))
          chrome.storage.local.set({ panelHubs: data.panel_hubs });
        sendResponse(data);
      })
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }

  if (request.action === 'SET_PANEL_HUB') {
    fetch(`${BACKEND}/config/panel-hub`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ url: String(request.url || '') })
    })
      .then(r => r.json().then(data => ({ ok: r.ok, data })))
      .then(({ ok, data }) => {
        if (ok && data && Array.isArray(data.panel_hubs))
          chrome.storage.local.set({ panelHubs: data.panel_hubs });
        sendResponse(ok ? data : { error: 'HTTP error' });
      })
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }

  if (request.action === 'CHECK_PANEL_HUB') {
    // Semantics live in hub_match.js — the shared, test-enforced matcher.
    chrome.storage.local.get('panelHubs', ({ panelHubs }) => {
      const list = Array.isArray(panelHubs) ? panelHubs : [];
      sendResponse({ hub: hubHit(list, request.url) });
    });
    return true;   // async response
  }

  if (request.action === 'GET_OMNI_DETAIL') {
    fetch(`${BACKEND}/omni`, { headers: authHeaders() })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => sendResponse({ data }))
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }
  if (request.action === 'OMNI_PING') {
    fetch(`${BACKEND}/omni/ping`, { headers: authHeaders() })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => sendResponse({ data }))
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }
  if (request.action === 'GET_TRACES') {
    // Trace-bus poll for the DEBUG console (round two, section F)
    const h = {};
    if (SENTINEL_TOKEN) h['X-Sentinel-Token'] = SENTINEL_TOKEN;
    fetch(`${BACKEND}/traces?since=${encodeURIComponent(request.since || 0)}`,
          { headers: h })
      .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
      .then(data => sendResponse({ data }))
      .catch(err => sendResponse({ error: err.message }));
    return true;
  }
});

chrome.debugger.onDetach.addListener((source) => {
    // infobar "Cancel", tab crash, or devtools took over — forget stale memory
    attachPromises.delete(source.tabId);
    lastPointer.delete(source.tabId);
});

// Content scripts are injected on demand now (no <all_urls> manifest
// entry), so a hard navigation on the run tab leaves it without the
// listener that performs auto-resume. Re-inject when that tab finishes
// loading; content.js's re-injection guard makes duplicates a no-op.
chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status !== 'complete') return;
  chrome.storage.session.get('runState', (result) => {
    const runState = (result && result.runState) || null;
    if (!runState || !runState.running || runState.tabId !== tabId) return;
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError || !tab ||
          !/^https?:/.test(tab.url || '')) return;
      chrome.scripting.executeScript({
        target: { tabId, allFrames: true },
        files: ['content.js']
      }).catch((e) => {
        logs.push({ t: Date.now(),
                    line: 're-inject after navigation failed: ' + e.message,
                    kind: 'warn' });
        if (logs.length > MAX_LOGS) logs = logs.slice(-MAX_LOGS);
        saveLogs();
      });
    });
  });
});

chrome.commands.onCommand.addListener((command) => {
    if (command !== 'stop-sentinel') return;
  chrome.storage.session.get('runState', (result) => {
    const runState = (result && result.runState) || null;
    if (runState && runState.tabId) {
            chrome.tabs.sendMessage(runState.tabId, { action: 'STOP' },
                () => void chrome.runtime.lastError);
        }
        chrome.storage.session.set({
            runState: { running: false, tabId: null, runId: null }
        });
        setToolbarBadge(false);
    });
});
chrome.tabs.onRemoved.addListener((tabId) => {
  lastPointer.delete(tabId);
  chrome.storage.session.get('runState', (result) => {
    const runState = (result && result.runState) || null;
    if (runState && runState.tabId === tabId) {
      chrome.storage.session.set({
        runState: { running: false, tabId: null, runId: null }
      });
      setToolbarBadge(false);
    }
  });
});

chrome.windows.onRemoved.addListener((windowId) => {
  chrome.storage.session.get('hudWindowId', (result) => {
    const hudWindowId = (result && result.hudWindowId) != null ? result.hudWindowId : null;
    if (hudWindowId === windowId)
      chrome.storage.session.remove('hudWindowId');
  });
});

// ── panel-hub mirror sync (round four, B.2) ──────────────────────
// The storage.local mirror only refreshed when the popup was open, so a
// hub added by core.py writing panel_config.json directly was invisible
// to extension enforcement indefinitely. A 60s alarm plus boot-sync keeps
// the mirror fresh with the popup closed; on fetch failure the mirror
// simply keeps its last-known list (offline fallback, never wiped).
function syncPanelHubs() {
  fetch(`${BACKEND}/config/panel-hub`, { headers: authHeaders() })
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (data && Array.isArray(data.panel_hubs))
        chrome.storage.local.set({ panelHubs: data.panel_hubs });
    })
    .catch(() => {});   // backend down — last-known list stays in force
}
try {
  chrome.runtime.onInstalled.addListener(() => {
    chrome.alarms.create('panelHubSync', { periodInMinutes: 1 });
    syncPanelHubs();
  });
  chrome.runtime.onStartup.addListener(syncPanelHubs);
  chrome.alarms.onAlarm.addListener((a) => {
    if (a.name === 'panelHubSync') syncPanelHubs();
  });
  syncPanelHubs();      // every SW boot (eviction restarts are silent)
} catch (e) {}
