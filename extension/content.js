// Re-injection guard: the manifest injection and the popup's scripting-API
// fallback can overlap — registering twice would double-count every log line
// and spawn competing scan loops.
if (window.__sentinelContentLoaded) {
    // already instrumented; skip everything below
} else {
window.__sentinelContentLoaded = true;

const SCAN_INTERVAL = 15000;
let lastScanAt = 0;

// Per-frame identity: sender.frameId is supplied by Chrome and is stable
// across navigations within the same frame. Use it to tag every outgoing
// message so the background can stitch per-frame maps without offset math.
function frameIdentity() {
    try {
        if (window === top) return { frameId: 'top', isTop: true };
        const fe = window.frameElement;
        return { frameId: fe ? ('frame-' + (fe.id || fe.src.slice(-16))) : 'frame-unknown', isTop: false };
    } catch (e) {
        return { frameId: 'cross-origin', isTop: false };
    }
}
const FRAME = frameIdentity();
function tagged(request) {
    return { ...request, _frameId: FRAME.frameId, _isTop: FRAME.isTop };
}

// Every log line carries an explicit severity tag so the popup never has to
// regex-sniff its own output (audit round one, section B). An untagged first
// argument degrades gracefully to a plain info line.
const LOG_KINDS = new Set(['ok', 'ai', 'act', 'err', 'warn', 'dim', 'info']);
function log(kind, ...a) {
    if (typeof kind !== 'string' || !LOG_KINDS.has(kind)) {
        a.unshift(kind);
        kind = 'info';
    }
    try {
        chrome.runtime.sendMessage(
            tagged({ action: 'LOG', line: a.map(String).join(' '), kind }),
            () => void chrome.runtime.lastError
        );
    } catch (e) {}
}

// Bump on every snapshot so a stale build identifies itself immediately
// (round six, R6-A: the paste-vs-deployed divergence has bitten 3×).
const BUILD = 'r29';

let isRunning = false;
let loopId = null;
let isScanning = false;
let RUN_ID = null;
let memory = [];
let lastFingerprint = '';
let stuckSince = 0;
const elRegistry = new Map();
const frameOffsets = new WeakMap();   // el → iframe offset in tab-viewport px
// Stable per-node handle: survives remaps, so clicks can re-resolve by
// selector instead of trusting an array slot (round three, mechanical #2).
const sidMap = new WeakMap();
let sidSeq = 0;
function nodeSid(el) {
    let s = sidMap.get(el);
    if (!s) { s = 'n' + (++sidSeq).toString(36); sidMap.set(el, s); }
    return s;
}
// Drift-skip budget per stable sid — a skip that leaves the world unchanged
// is not allowed to repeat unbounded (round three, mechanical #3).
const driftBudget = new Map();
let nextId = 0;
let iframeWaitSince = 0;
let lastNavTarget = null, navTries = 0;
let frameDeferCount = 0;
// r29 diagnostics: last map + filter census, inspectable via __sentinel.debug().
const lastMapDebug = { elements: [], filtered: [] };
window.__sentinel = window.__sentinel || {};
window.__sentinel.debug = () => ({
    elements: lastMapDebug.elements,
    census: lastMapDebug.census,
});

// F12 console controls — LO can type these in devtools to start/stop the bot.
window.__sentinel.start = async function() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { console.warn('[Sentinel] no active tab'); return; }
    const runId = Date.now();
    const resp = await new Promise(resolve =>
        chrome.tabs.sendMessage(tab.id, { action: 'START', runId }, r => {
            void chrome.runtime.lastError; resolve(r || null);
        }));
    if (!resp) {
        console.warn('[Sentinel] no content script — reload the page and try again');
        return;
    }
    console.log(`[Sentinel] started on tab ${tab.id} (build ${resp.build || '?'})`);
};
window.__sentinel.stop = async function() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { console.warn('[Sentinel] no active tab'); return; }
    await chrome.tabs.sendMessage(tab.id, { action: 'STOP' }, () => {
        void chrome.runtime.lastError;
    });
    console.log('[Sentinel] stop signal sent');
};
window.__sentinel.toggle = async function() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { console.warn('[Sentinel] no active tab'); return; }
    const resp = await new Promise(resolve =>
        chrome.tabs.sendMessage(tab.id, { action: 'GET_RUN_STATE' }, r => {
            void chrome.runtime.lastError; resolve(r || null);
        }));
    if (resp && resp.running) {
        await window.__sentinel.stop();
    } else {
        await window.__sentinel.start();
    }
};
window.__sentinel.status = async function() {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab) { console.warn('[Sentinel] no active tab'); return; }
    const resp = await new Promise(resolve =>
        chrome.tabs.sendMessage(tab.id, { action: 'GET_RUN_STATE' }, r => {
            void chrome.runtime.lastError; resolve(r || null);
        }));
    console.log('[Sentinel] run state:', resp || 'no response');
    return resp;
};

// ─── Stats ────────────────────────────────────────────────────────
let questionCount = 0;
let runStartTime = 0;

function reportStats() {
    // Guarded like log(): stats are best-effort, and markStopped() reaches
    // here from user-event paths (panic key) that sit OUTSIDE scan()'s
    // try/catch — an orphaned context must not throw from them.
    try {
        chrome.runtime.sendMessage({
            action: 'REPORT_STATS',
            stats: {
                questions: questionCount,
                startTime: runStartTime,
                runtime: runStartTime ? Math.round((Date.now() - runStartTime) / 1000) : 0
            }
        });
    } catch (e) {}
}

// ─── Keyboard Panic Button ────────────────────────────────────────
document.addEventListener('keydown', (e) => {
    if (!isRunning) return;
    if (e.key === 'Escape' || (e.ctrlKey && e.shiftKey && e.key === 'X')) {
        e.preventDefault();
        e.stopPropagation();
        markStopped();
    }
}, true);

// ─── Element Map ──────────────────────────────────────────────────
// Single source of truth for an element's identity text. The map builder
// AND the drift verifier must use this — divergent extractors are a
// permanent drift machine (round twelve: signatureText never read
// placeholder, so every placeholder-only input failed verify forever).
function identityText(el) {
    const inner = el.innerText || '';
    const aria  = (el.getAttribute && el.getAttribute('aria-label')) || '';
    const ph    = (el.getAttribute && el.getAttribute('placeholder')) || '';
    const val   = el.value || '';
    const tc    = el.textContent || '';
    return (inner || aria || ph || val || tc).trim();
}

// Unified same-origin iframe collector (audit r25, canonical targets r30):
// visible control -> map the control; hidden control + visible label ->
// map the label as the pseudo-control and inherit its state.
function controlForLabel(el) {
    if (!el || el.tagName !== 'LABEL') return null;
    return el.control || el.querySelector(
        'input[type="radio"], input[type="checkbox"]'
    );
}

function isRendered(el, view) {
    if (!el || !el.isConnected) return false;
    const cs = view.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' ||
        Number.parseFloat(cs.opacity) === 0) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    const rect = el.getBoundingClientRect();
    return rect.width >= 5 && rect.height >= 5;
}

function shouldMapNode(el, view) {
    if (el.tagName === 'INPUT' && el.type === 'hidden') return false;

    if (el.tagName === 'INPUT' &&
        (el.type === 'radio' || el.type === 'checkbox')) {
        const label = (el.labels && el.labels[0]) || el.closest('label');
        if (!isRendered(el, view) && isRendered(label, view)) {
            return false; // visible label will be the one canonical click target
        }
    }

    if (el.tagName === 'LABEL') {
        const control = controlForLabel(el);
        if (!control) return false; // plain labels are not actionable
        if (isRendered(control, view)) return false; // visible input is canonical
    }

    return isRendered(el, view);
}

function renderedBox(el, view) {
  if (!el || !el.isConnected) return false;
  const style = view.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' ||
      Number.parseFloat(style.opacity) === 0) return false;
  if (el.getAttribute('aria-hidden') === 'true') return false;
  const rect = el.getBoundingClientRect();
  return rect.width >= 5 && rect.height >= 5;
}

function isHoneypot(el, view) {
  if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) return false;
  const rect = el.getBoundingClientRect();
  const offscreen = rect.bottom < 0 || rect.top > view.innerHeight ||
    rect.right < 0 || rect.left > view.innerWidth;
  return offscreen && el.getAttribute('tabindex') === '-1' &&
    el.getAttribute('autocomplete') === 'off';
}

function canonicalNode(el, view) {
  if (el.tagName === 'INPUT' && el.type === 'hidden') return false;
  if (isHoneypot(el, view)) return false;

  if (el.tagName === 'INPUT' &&
      (el.type === 'radio' || el.type === 'checkbox')) {
    const label = (el.labels && el.labels[0]) || el.closest('label');
    if (!renderedBox(el, view) && renderedBox(label, view)) {
      return false; // visible label is the canonical target
    }
  }

  if (el.tagName === 'LABEL') {
    const control = controlForLabel(el);
    if (!control) return false;
    if (renderedBox(control, view)) return false; // visible input is canonical
  }

  return renderedBox(el, view);
}

function optionText(el, stateEl) {
  if (el.tagName === 'LABEL') return identityText(el);
  if (stateEl.type === 'radio' || stateEl.type === 'checkbox') {
    const label = (stateEl.labels && stateEl.labels[0]) ||
      stateEl.closest('label');
    if (label) return identityText(label);
  }
  return identityText(el) || identityText(stateEl);
}

function questionContext(el) {
  const container = el.closest(
    'fieldset, [role="group"], [data-question-id], .question, .q, section'
  );
  if (!container) return '';
  return (container.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 240);
}

function collectDocumentElements(doc, offset, frame, elements, census) {
  const view = doc.defaultView;
  if (!view) return 0;
  const before = elements.length;
  const nodes = doc.querySelectorAll(
    'button, input, select, textarea, a, [role="button"], [role="radio"], ' +
    '[role="checkbox"], [role="slider"], label, .answer-option, ' +
    '.survey-option, .option, .choice, [contenteditable]'
  );

  for (const el of nodes) {
    try {
      if (!canonicalNode(el, view)) {
        if (census) census.filtered++;
        continue;
      }

      const control = controlForLabel(el);
      const stateEl = control || el;
      const rect = el.getBoundingClientRect();
      const sid = nodeSid(el);
      const id = nextId++;
      const role = el.getAttribute('role') || '';
      const semanticType = stateEl.type ||
        (role === 'radio' || role === 'checkbox' || role === 'range'
          ? role.replace('range', 'range') : '');

      el.setAttribute('data-sentinel-sid', sid);
      elRegistry.set(id, el);
      if (frame) frameOffsets.set(el, { dx: offset.left, dy: offset.top });

      const entry = {
        id,
        sid,
        frame,
        name: stateEl.getAttribute('name') || '',
        tag: el.tagName.toLowerCase(),
        type: semanticType,
        role,
        text: optionText(el, stateEl).slice(0, 120),
        option_value: String(stateEl.value || el.dataset.value || ''),
        required: !!stateEl.required ||
          stateEl.getAttribute('aria-required') === 'true',
        disabled: !!stateEl.disabled ||
          stateEl.getAttribute('aria-disabled') === 'true',
        x: Math.round(offset.left + rect.left + rect.width / 2),
        y: Math.round(offset.top + rect.top + rect.height / 2),
        context: questionContext(el),
        frameId: FRAME.frameId
      };

      if (semanticType === 'radio' || semanticType === 'checkbox') {
        entry.checked = 'checked' in stateEl
          ? !!stateEl.checked
          : stateEl.getAttribute('aria-checked') === 'true';
      } else if (el.tagName === 'SELECT') {
        entry.value = el.value || '';
        entry.options = [...el.options].map(option => ({
          value: option.value,
          text: option.text.trim(),
          disabled: option.disabled
        }));
      } else if (el.isContentEditable) {
        entry.editable = true;
        entry.value = (el.textContent || '').trim().slice(0, 200);
      } else if ('value' in stateEl) {
        entry.value = String(stateEl.value || '').slice(0, 200);
      }

      if ('min' in stateEl) entry.min = stateEl.min || null;
      if ('max' in stateEl) entry.max = stateEl.max || null;
      if ('step' in stateEl) entry.step = stateEl.step || null;

      elements.push(entry);
    } catch (error) {
      log('warn', `${frame ? 'iframe' : 'top'} element skipped:`,
        error && error.message);
    }
  }

  return elements.length - before;
}

function getElementMap() {
  const elements = [];
  elRegistry.clear();
  nextId = 0;
  const census = { filtered: 0 };
  collectDocumentElements(
    document,
    { left: 0, top: 0 },
    false,
    elements,
    census
  );
  lastMapDebug.elements = elements.slice();
  lastMapDebug.census = census;
  log('dim', `map: ${elements.length} top element(s), ` +
    `${census.filtered} filtered`);
  return elements;
}

function collectFrameElements(iframeDoc, frameRect, elements) {
  return collectDocumentElements(
    iframeDoc,
    { left: frameRect.left, top: frameRect.top },
    true,
    elements,
    null
  );
}

// Tier-2 DOM skeleton: compact live-DOM structure for the AI.
// Reuses identityText, walks same-origin iframes, and is serialized
// AFTER the iframe-readiness gate so the race can't bite (audit r23).
function domSkeleton(root = document) {
    const out = [];
    const walk = (el, path) => {
        for (const child of el.children) {
            const tag = child.tagName.toLowerCase();
            const interactive = child.dataset.sentinelSid ||
                ['INPUT','SELECT','TEXTAREA','BUTTON'].includes(child.tagName) ||
                child.isContentEditable;
            if (interactive) {
                out.push({
                    sid: child.dataset.sentinelSid || null,
                    tag, name: child.getAttribute('name') || '',
                    type: child.type || '',
                    text: identityText(child).slice(0, 60),
                    path: path.slice(-2).join(' > ')
                });
            }
            if (tag === 'iframe') {
                try {
                    walk(child.contentDocument.body, [...path, 'iframe']);
                } catch (e) {
                    out.push({ path: [...path,'iframe'].join(' > '), note: 'x-origin' });
                }
            } else if (!interactive) {
                walk(child, [...path, tag + (child.id ? '#'+child.id : '')]);
            }
        }
    };
    if (root.body) walk(root.body, []);
    return out;
}

// sr-only / visually-hidden inputs are legitimate controls whose labels are
// the visible UI. Exempt them from the 5px minimum when they are the labelled
// control of a visible label (r29: zero-input stall).
function isSrOnlyControl(el) {
    if (el.tagName !== 'INPUT') return false;
    const t = el.type || 'text';
    if (t !== 'radio' && t !== 'checkbox') return false;
    const lbl = (el.labels && el.labels[0]) ||
                (el.closest ? el.closest('label') : null);
    if (!lbl || !lbl.isConnected) return false;
    const b = lbl.getBoundingClientRect();
    return b.width >= 5 && b.height >= 5;   // visible label wrapping a hidden input
}

function getFingerprint() {
    const parts = [location.href];
    const hashDoc = (doc, prefix) => {
        const groups = new Map();
        for (const el of doc.querySelectorAll(
            'input[type="radio"], input[type="checkbox"], input[type="text"], input[type="email"], input[type="number"], input[type="tel"], input[type="date"], select, textarea, [contenteditable]'
        )) {
            if (el.type === 'hidden') continue;
            const key = el.getAttribute('name') || el.id || el.tagName.toLowerCase();
            const kind = el.type || el.tagName.toLowerCase();
            const val = el.isContentEditable ? (el.textContent || '').slice(0, 20) :
                (el.value || '').slice(0, 20);
            const checked = (el.type === 'radio' || el.type === 'checkbox')
                ? (el.checked ? '1' : '0') : '';
            const existing = groups.get(key);
            if (existing) {
                existing.kind = kind;
                existing.val = val;
                existing.checked = checked;
            } else {
                groups.set(key, { kind, val, checked });
            }
        }
        for (const [key, info] of groups) {
            parts.push(prefix + key + ':' + info.kind + ':' + info.checked + ':' + info.val);
        }
    };
    hashDoc(document, '');
    for (const f of document.querySelectorAll('iframe')) {
        try {
            const d = f.contentDocument;
            if (d && d.body) hashDoc(d, 'IF:');
        } catch (e) { /* cross-origin stays out of the hash */ }
    }
    const s = parts.join('|');
    let h = 5381;
    for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
    return h.toString(36);
}

function isDisqualified() {
    const url = location.href.toLowerCase();
    const text = document.body.innerText.toLowerCase().slice(0, 2000);
    if (/(disqualif|screenout|quotafull|reward=0)/.test(url)) return true;
    return /\b(disqualified|screened out|screenout|do not qualify|does not qualify|quota full|quota is full|reward=0|terminated)\b/i.test(text);
}

function isComplete() {
    const text = document.body.innerText.toLowerCase().slice(0, 1500);
    return /\b(thank you|your responses have been recorded)\b/i.test(text);
}

function detectCaptcha() {
    const indicators = [
        "iframe[src*='recaptcha']", "iframe[src*='hcaptcha']",
        "iframe[src*='turnstile']", ".g-recaptcha", "#recaptcha", ".h-captcha"
    ];
    return indicators.some(sel => document.querySelector(sel) !== null);
}

// ─── Lifecycle ────────────────────────────────────────────────────
function markStopped() {
    isRunning = false;
    if (loopId) { clearTimeout(loopId); loopId = null; }
    questionCount = 0;
    runStartTime = 0;
    reportStats();
    try {
        chrome.runtime.sendMessage(
            { action: 'SET_RUN_STATE', running: false, tabId: null, runId: null },
            () => void chrome.runtime.lastError
        );
    } catch (e) {}
}

// ─── Panel-hub guard ──────────────────────────────────────────────
// User-configured stop-domains (popup → backend → storage.local mirror).
// Landing on one means the survey TERMINATED and bounced back to a panel
// login wall — stop cleanly, never fill out a login form.
async function hitPanelHub() {
    // Returns the matched hub domain, null = definitely no hub, or the
    // string 'unknown' when the check itself failed (background dead /
    // storage error). Callers pick their own failure direction — the
    // auto-resume path FAILS CLOSED (round four, B.3).
    const resp = await new Promise(res =>
        chrome.runtime.sendMessage(
            { action: 'CHECK_PANEL_HUB', url: location.href },
            r => { void chrome.runtime.lastError; res(r); }));
    return resp ? (resp.hub || null) : 'unknown';
}

// ─── Human-like Interaction ───────────────────────────────────────
async function humanClick(el) {
    if (!el.isConnected) return;
    const rect = el.getBoundingClientRect();
    // Elements inside same-origin iframes report frame-relative rects —
    // shift them into TAB-viewport space or CDP trusted clicks land wrong.
    const o = frameOffsets.get(el) || { dx: 0, dy: 0 };
    const tx = o.dx + rect.left + rect.width / 2;
    const ty = o.dy + rect.top + rect.height / 2;

    // Try trusted CDP path first
    const trusted = await new Promise(resolve => {
        chrome.runtime.sendMessage({
            action: 'TRUSTED_CLICK',
            vp: { x: tx, y: ty, w: rect.width, h: rect.height }
        }, resolve);
    });

    if (trusted && trusted.ok) {
        return;
    }

    // Fallback to DOM events (dispatch targets the node directly, so
    // frame-local clientX/clientY are correct here)
    const lx = rect.left + rect.width / 2, ly = rect.top + rect.height / 2;
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: lx, clientY: ly }));
    await sleep(50 + Math.random() * 100);
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: lx, clientY: ly }));
    await sleep(30 + Math.random() * 50);
    el.click();
}

async function humanType(el, text) {
    if (!el.isConnected) return;
    if (el.tagName === 'LABEL') {
        const ctrl = el.control ||
            el.querySelector('input, textarea');
        if (ctrl && document.contains(ctrl)) { el = ctrl; }
    }
    if (el.isContentEditable) {
        el.focus();
        el.textContent = '';
        for (const ch of text) {
            document.execCommand('insertText', false, ch);
            await sleep(30 + Math.random() * 90);
        }
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return;
    }
    if (el.tagName !== 'INPUT' && el.tagName !== 'TEXTAREA') return;
    const proto = el.tagName === 'TEXTAREA'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;

    el.focus();
    setter.call(el, '');
    let cur = '';
    for (const ch of text) {
        cur += ch;
        setter.call(el, cur);
        el.dispatchEvent(new InputEvent('input',
            { bubbles: true, data: ch, inputType: 'insertText' }));
        await sleep(30 + Math.random() * 90);
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
}

// ─── Drift-safe actuation (round three) ───────────────────────────
// scrollIntoView → settle → measure FRESH, milliseconds before the press.

// Shared typing body for the type branch's verify-then-act loop.
async function doType(el, value) {
    if (!el.isConnected) return;
    if (el.isContentEditable) {
        el.focus();
        el.textContent = '';
        for (const ch of value) {
            document.execCommand('insertText', false, ch);
            await sleep(30 + Math.random() * 90);
        }
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return;
    }
    await humanType(el, value);
}
function signatureText(el) {
    // Same extractor as the map builder — agreement by construction.
    return identityText(el).toLowerCase().replace(/\s+/g, ' ');
}
async function settleAndVerify(el, snap) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
    await sleep(150);                       // layout settle after scroll
    if (!snap || !snap.text) return true;   // nothing to compare against
    // Signature must include the same sources the SNAPSHOT drew from.
    // For radios/checkboxes the visible text lives on the associated
    // <label>, not the control — comparing input-only signatures made a
    // perfectly-resolved node look permanently drifted (round nine artifact).
    const parts = [signatureText(el)];
    if (el.tagName === 'INPUT' &&
        (el.type === 'radio' || el.type === 'checkbox')) {
        const lbl = (el.labels && el.labels[0]) ||
                    (el.closest ? el.closest('label') : null);
        if (lbl) parts.push(signatureText(lbl));
    } else if (el.tagName === 'SELECT') {
        const sel = el.selectedOptions && el.selectedOptions[0];
        if (sel) parts.push(signatureText(sel));
    }
    const sig = parts.join(' ').replace(/\s+/g, ' ');
    const want = snap.text.replace(/\s*\[iframe\]\s*$/i, '').toLowerCase().replace(/\s+/g, ' ').trim();
    return sig.includes(want.slice(0, 40));
}

// Stable-handle lookup across frame boundaries: sid attributes inside
// same-origin iframes are invisible to top-document querySelector, so the
// remap must search those docs too (round nine artifact: "Re-resolve
// failed for ng" while the node sat in a registered iframe).
function findNodeBySid(sid, snap) {
    let el = null;
    try { el = document.querySelector(`[data-sentinel-sid="${sid}"]`); }
    catch (e) {}
    if (el) return el;
    for (const f of document.querySelectorAll('iframe')) {
        try {
            const d = f.contentDocument;
            if (!d) continue;
            el = d.querySelector(`[data-sentinel-sid="${sid}"]`);
            if (el) return el;
        } catch (e) {}   // cross-origin — unreachable by design
    }
    // Virtualized remount: sid not found → fuzzy-match by semanticType + accessibleName
    if (snap && snap.text) {
        const candidates = [];
        try {
            for (const f of [document, ...document.querySelectorAll('iframe').map(i => {
                try { return i.contentDocument; } catch (e) { return null; }
            }).filter(Boolean)]) {
                for (const node of f.querySelectorAll('input, select, textarea, button, [contenteditable], label')) {
                    const text = (node.innerText || node.textContent || node.getAttribute('aria-label') || '').trim().toLowerCase();
                    const type = (node.type || node.getAttribute('role') || node.tagName.toLowerCase());
                    if (text.includes(snap.text.toLowerCase().slice(0, 30)) ||
                        (snap.semanticType && type === snap.semanticType)) {
                        candidates.push(node);
                    }
                }
            }
        } catch (e) {}
        if (candidates.length === 1) return candidates[0];
    }
    return null;
}

async function executeAction(action, elements) {
    const { action_type, element_id, coordinates, value } = action;

    function resolveEl(eid) {
        // Identity-first: within a single cycle the registry is the contract.
        // A registry miss must never fall through to an elementFromPoint
        // stranger and then be drift-compared against element 21's snapshot.
        if (eid !== null && eid !== undefined) {
            const el = elRegistry.get(eid);
            if (el && el.isConnected) return el;   // same node, this cycle
            return null;                            // miss: remap, don't guess
        }
        // Coordinate-only actions: read the destructured closure value.
        // (The old second parameter SHADOWED this and always arrived
        // undefined, silently killing every coordinates-only action.)
        return coordinates
            ? document.elementFromPoint(coordinates[0], coordinates[1])
            : null;
    }

    if (action_type === 'click') {
        let el = resolveEl(element_id);
        if (!el) { log('warn', 'click dropped: no element for id', element_id); return; }
        const snap = elements.find(e => e.id === element_id);
        const sid = (snap && snap.sid) ||
                    el.getAttribute('data-sentinel-sid') || ('id' + element_id);
        for (let attempt = 1; attempt <= 3; attempt++) {
            if (await settleAndVerify(el, snap)) {
                await humanClick(el);
                driftBudget.set(sid, 0);        // success clears the budget
                return;
            }
            const skips = (driftBudget.get(sid) || 0) + 1;
            driftBudget.set(sid, skips);
            log('warn', `Element drifted — skipping ${sid} (${skips}/3)`);
            if (skips >= 3) {
                // Budget spent: trust the freshly measured node and press via
                // humanClick's trusted-CDP path at live coordinates.
                log('act', `Drift budget spent on ${sid} — forcing trusted click`);
                await humanClick(el);
                driftBudget.set(sid, 0);
                return;
            }
            // Remap + re-resolve by STABLE handle, not array slot
            const fresh = findNodeBySid(sid, snap);
            if (!fresh) { log('err', 'Re-resolve failed for', sid); return; }
            log('act', 'Re-resolved by stable sid', sid);
            el = fresh;
        }
    }
    else if (action_type === 'type') {
        // Budgeted like the click path — an unbounded single-skip here was
        // the same livelock pattern, one branch over (round twelve, D).
        let el = resolveEl(element_id);
        if (!el || !value) {
            log('warn', !el ? `type dropped: no element for id ${element_id}`
                            : `type dropped: empty value for id ${element_id}`);
            return;
        }
        const snap = elements.find(e => e.id === element_id);
        const sid = (snap && snap.sid) ||
                    el.getAttribute('data-sentinel-sid') || ('id' + element_id);
        for (let attempt = 1; attempt <= 3; attempt++) {
            if (await settleAndVerify(el, snap)) {
                await doType(el, value);
                driftBudget.set(sid, 0);
                return;
            }
            const skips = (driftBudget.get(sid) || 0) + 1;
            driftBudget.set(sid, skips);
            // Self-diagnosing drift: print BOTH sides' identity text so a
            // mismatch names itself instead of spawning another blind loop
            // (round thirteen — the budget survived, the mystery didn't).
            const got = signatureText(el).slice(0, 48);
            const wantTxt = String((snap && snap.text) || '').replace(/\s*\[iframe\]\s*$/i, '').slice(0, 48);
            log('warn', `Type target drifted — skipping ${sid} (${skips}/3) ` +
                `sig="${got}" want="${wantTxt}"`);
            if (skips >= 3) {
                // Budget spent: the registry already proved this node's
                // identity THIS cycle — trust it and type.
                log('act', `Type drift budget spent on ${sid} — forcing type`);
                await doType(el, value);
                driftBudget.set(sid, 0);
                // Post-write verdict: proves whether the write LANDED and on
                // what node — Case A/B/C of the round-13 tree in one line.
                log('warn', `forced type result: ${el.tagName}` +
                    `${el.type ? '[' + el.type + ']' : ''}` +
                    `${el.name ? ' name=' + el.name : ''}` +
                    ` value=${JSON.stringify(((el.value ?? el.textContent) || '').slice(0, 30))}` +
                    `${el.readOnly ? ' READONLY' : ''}${el.disabled ? ' DISABLED' : ''}`);
                return;
            }
            const fresh = findNodeBySid(sid, snap);
            if (!fresh) { log('err', 'Re-resolve failed for', sid); return; }
            log('act', 'Re-resolved by stable sid', sid);
            el = fresh;
        }
    }
    else if (action_type === 'select_option') {
        const el = resolveEl(element_id);
        if (!el || el.tagName !== 'SELECT' || !value) {
            log('warn', 'select_option dropped:',
                !el ? `no element for id ${element_id}` :
                el.tagName !== 'SELECT' ? `target is ${el.tagName}, not SELECT` :
                'no value supplied');
            return;
        }
        const opts = [...el.options];
        const opt = opts.find(o => o.text.trim().toLowerCase() === value.toLowerCase())
                 || opts.find(o => o.text.toLowerCase().includes(value.toLowerCase()));
        if (opt) {
            el.value = opt.value;
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }
    else if (action_type === 'select_multi') {
        if (value) {
            let hits = 0;
            for (const part of value.split(',')) {
                const trimmed = part.trim();
                if (/^\d+$/.test(trimmed)) {
                    const el = elRegistry.get(parseInt(trimmed));
                    if (el) { await humanClick(el); hits++; }
                } else {
                    for (const e of elements) {
                        if (e.text && e.text.toLowerCase().includes(trimmed.toLowerCase())) {
                            const el = elRegistry.get(e.id);
                            if (el) { await humanClick(el); hits++; }
                        }
                    }
                }
            }
            if (!hits) log('warn', 'select_multi matched nothing for:', value);
        }
    }
    else if (action_type === 'scroll') {
        window.scrollBy({ top: 200 + Math.random() * 400, behavior: 'smooth' });
    }
    else if (action_type === 'next') {
        await clickNext();
    }
    else if (action_type === 'wait') {
        await sleep(2000);
    }
    else if (action_type === 'human_help') {
        alert('MANUAL HELP NEEDED\n\nThe bot is stuck. Answer this question manually, then click Start again.');
        markStopped();
    }
}

async function clickNext() {
    const selectors = [
        "[data-next]", "input[type='submit']", "button",
        "[id*='next' i]", "[class*='next' i]", "[role='button']", "a"
    ];
    const keywords = ['next', 'continue', 'submit', '>>', '→', 'done',
        'siguiente', 'weiter', 'suivant', 'avanti', 'volgende',
        'następne', 'proceed', 'prosseguir', 'próximo', 'continuar', 'finalizar'];

    const docs = [{ d: document, tag: 'top' }];
    for (const f of document.querySelectorAll('iframe')) {
        try { if (f.contentDocument) docs.push({ d: f.contentDocument, tag: 'iframe' }); }
        catch (e) {}
    }

    for (const { d, tag } of docs) {
        for (const sel of selectors) {
            for (const el of d.querySelectorAll(sel)) {
                const text = (el.innerText || el.value || '').toLowerCase();
                const navish = el.matches('[data-next], input[type="submit"]');
                if ((text && keywords.some(k => text.includes(k))) || (!text && navish)) {
                    const cs = d.defaultView.getComputedStyle(el);
                    if (cs.display !== 'none' && cs.visibility !== 'hidden') {
                        if (tag === 'iframe') {
                            // group-aware completion check — r27 D
                            const fields = [...el.ownerDocument.querySelectorAll(
                                'input:not([type="hidden"]), select, textarea')];
                            const checkedKeys = new Set(fields
                                .filter(i => (i.type === 'radio' || i.type === 'checkbox') && i.checked)
                                .map(i => i.name || i));
                            const seen = new Set();
                            const open = fields.filter(i => {
                                if (i.type === 'radio' || i.type === 'checkbox') {
                                    const k = i.name || i;
                                    if (seen.has(k)) return false;
                                    seen.add(k);
                                    return !checkedKeys.has(k);
                                }
                                return !(i.value || '').trim();
                            }).length;
                            if (open > 0) {
                                frameDeferCount++;
                                const mapped = el.ownerDocument
                                    .querySelectorAll('[data-sentinel-sid]').length;
                                if (mapped === 0 && frameDeferCount >= 2) {
                                    log('err', `DEADLOCK: frame has ${open} unanswered ` +
                                        `group(s) but 0 mapped elements — mapping broken`);
                                } else {
                                    log('warn', `clickNext: frame submit deferred — ` +
                                        `${open} unanswered group(s) inside frame`);
                                }
                                return false;
                            }
                        }
                        el.scrollIntoView({ block: 'center' });
                        await sleep(200);
                        const navKey = el.getAttribute('data-sentinel-sid') ||
                            (el.innerText || '').trim().slice(0, 24);
                        if (navKey === lastNavTarget) {
                            if (++navTries >= 3) {
                                log('warn', `clickNext: "${navKey}" clicked 3× ` +
                                    `without advancing — releasing target`);
                                return false;
                            }
                        } else { lastNavTarget = navKey; navTries = 1; }
                        log('act', `clickNext: "${text.slice(0, 24) || '(no text)'}" ` +
                            `via ${sel} [${tag}]`);
                        await humanClick(el);
                        return true;
                    }
                }
            }
        }
    }
    log('warn', 'clickNext: no next button found in top document or iframes');
    return false;
}

// ─── Lifecycle ────────────────────────────────────────────────────
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// ─── Context liveness ─────────────────────────────────────────────
// An extension reload/update/disable orphans injected content scripts:
// their chrome.runtime.id goes undefined and every sendMessage throws
// "Extension context invalidated". chrome.runtime.id is the canonical
// liveness probe (audit round 5). Without this guard a dead-context run
// becomes a zombie loop — logging or throwing every SCAN_INTERVAL forever.
function contextAlive() {
    try {
        return !!(chrome && chrome.runtime && chrome.runtime.id);
    } catch (e) {
        return false;
    }
}

// killLoop = markStopped WITHOUT the reportStats/SET_RUN_STATE messages,
// which are exactly the calls that throw on an orphaned context.
function killLoop() {
    isRunning = false;
    if (loopId) { clearTimeout(loopId); loopId = null; }
}

async function scan(tabId) {
    if (!isRunning || isScanning) return;
    if (!contextAlive()) {
        // Orphaned by an extension reload. We cannot even sendMessage to
        // report it — die quietly instead of looping forever.
        killLoop();
        return;
    }
    isScanning = true;
    const now = Date.now();
    if (now - lastScanAt < SCAN_INTERVAL) {
        isScanning = false;
        loopId = setTimeout(() => scan(tabId), SCAN_INTERVAL - (now - lastScanAt));
        return;
    }
    lastScanAt = now;

    try {
        // Hub check FIRST — before visibility/DQ/captcha — so a panel login
        // wall is never scanned, fingerprinted, or acted upon (Trap 3).
        const hub = await hitPanelHub();
        if (hub === 'unknown') {
            // Fail-open ONLY here, deliberately and loudly: a dead background
            // also breaks CAPTURE and CALL_BACKEND below, so the loop cannot
            // act regardless. Never silently treat an error as permission.
            log('warn', '[?] Panel-hub check unavailable — continuing');
        } else if (hub) {
            log('ok', `[+] Panel hub reached (${hub}) — stopping cleanly`);
            markStopped();
            return;
        }

        if (document.visibilityState !== 'visible') {
            stuckSince = Date.now();
            return;
        }

        if (isDisqualified()) {
            log('err', '[!] Disqualified');
            chrome.runtime.sendMessage({ action: 'LEARN_RULE', memory, tabId });
            markStopped();
            return;
        }
    const CONF_GATE = (() => {
        try { return localStorage.getItem('__sentinelConfirmGate') === '1'; }
        catch (e) { return false; }
    })();

    if (isComplete()) {
        if (CONF_GATE) {
            const ok = confirm('Survey appears complete.\n\nPress OK to confirm submission, or Cancel to continue.');
            if (!ok) {
                log('warn', 'Confirmation gate: user declined completion');
                return;
            }
        }
        log('ok', '[+] Completed');
        markStopped();
        return;
    }
        if (detectCaptcha()) {
            log('err', '[!] CAPTCHA detected — pausing');
            alert('CAPTCHA detected. Solve it manually, then click Start.');
            markStopped();
            return;
        }

        const fp = getFingerprint();
        const now = Date.now();
        if (fp === lastFingerprint) {
            if (stuckSince && (now - stuckSince > 35000)) {
                log('warn', '[!] Stuck — emergency Next');
                await clickNext();
                stuckSince = now;
            }
        } else {
            lastFingerprint = fp;
            stuckSince = now;
            questionCount++;
            driftBudget.clear();     // new page → fresh skip budgets
            lastNavTarget = null; navTries = 0;   // new page → fresh nav eyes
            frameDeferCount = 0;                   // new page → fresh frame deferrals
            reportStats();
        }

        const screenshotData = await new Promise(resolve => {
            chrome.runtime.sendMessage({ action: 'CAPTURE_SCREENSHOT', tabId }, resolve);
        });

        if (!screenshotData || screenshotData.error) {
            log('err', 'Screenshot failed:', screenshotData ? screenshotData.error : 'no response');
            return;
        }

        const shotStr = screenshotData && screenshotData.screenshot;
        const screenshotB64 = (shotStr && shotStr.split(',').length > 1)
            ? shotStr.split(',')[1] : '';

        let elements = getElementMap();
        let pageText = document.body.innerText;
        let currentUrl = location.href;

        // ── iframe readiness gate (r22 D-1) — must sit AFTER pageText ──
        const notReady = [...document.querySelectorAll('iframe')].some(f => {
            try {
                const d = f.contentDocument;
                return d && d.readyState !== 'complete';
            } catch (e) { return false; }
        });
        if (notReady) {
            if (!iframeWaitSince) iframeWaitSince = Date.now();
            if (Date.now() - iframeWaitSince < 4000) {
                log('dim', 'iframe still loading — deferring scan');
                return;
            }
            log('warn', 'iframe load timeout — mapping without it');
        }
        iframeWaitSince = 0;

        // ── iframe collection (r25 D-1 + r27 inheritance) ──
        for (const iframe of document.querySelectorAll('iframe')) {
            try {
                const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                if (iframeDoc && iframeDoc.body) {
                    const iframeText = iframeDoc.body.innerText;
                    if (iframeText && iframeText.trim().length > 10)
                        pageText += '\n[IFRAME CONTENT]\n' + iframeText;
                    const ioff = iframe.getBoundingClientRect();
                    const n = collectFrameElements(iframeDoc, ioff, elements);
                    if (n > 0) {
                        const by = {};
                        elements.slice(-n).forEach(e => by[e.tag] = (by[e.tag] || 0) + 1);
                        log('dim', `iframe mapped: ${n} element(s) [${
                            Object.entries(by).map(([k, v]) => k + ':' + v).join(', ')}]`);
                    } else {
                        log('dim', 'iframe mapped: 0 element(s)');
                    }
                }
            } catch (e) {
                // don't guess the cause — a TDZ bug once hid here labeled
                // as cross-origin (drop 02). Say what happened, nothing more.
                log('warn', 'iframe mapping failed:', e && e.message);
            }
        }

        lastMapDebug.elements = elements.slice();
        lastMapDebug.frameCount = elements.filter(entry => entry.frame).length;

        const dom = domSkeleton();
        const cycleId = `${RUN_ID || 0}-${Date.now().toString(36)}`;

        const backendResp = await new Promise(resolve => {
            chrome.runtime.sendMessage({
                action: 'CALL_BACKEND',
                payload: {
                    cycle_id: cycleId,
                    session_id: `${tabId}-${RUN_ID || 0}`,
                    screenshot_b64: screenshotB64,
                    elements: elements,
                    url: currentUrl,
                    page_text: pageText,
                    dom_skeleton: dom
                }
            }, resolve);
        });

        if (!backendResp || backendResp.error) {
            log('err', 'Backend error:', backendResp ? backendResp.error : 'no response');
            return;
        }
        log('dim', `backend cycle ${cycleId}: ${backendResp.elapsedMs || '?'}ms`);

        const decision = backendResp.data;
        if (!decision || !Array.isArray(decision.actions)) {
            log('warn', 'decision malformed:', JSON.stringify(decision).slice(0, 160));
            return;
        }
        // r29: an empty action list is a decision too — say so, and name why.
        if (decision.actions.length === 0) {
            log('warn', 'decision returned ZERO actions - nothing answerable ' +
                'on this page (check map census / sr-only filter)');
        }

        if (backendResp.dryRun) {
            log('dim', `[DRY] ${decision.question_type} (${decision.confidence}) -> ` +
                decision.actions.map(a => `${a.action_type}:${a.element_id ?? '-'}`).join(', '));
            if (decision.memory_note) {
                memory.push(decision.memory_note);
                if (memory.length > 50) memory.shift();
            }
            return;
        }

        log('ai', `[AI] ${decision.question_type} | confidence: ${decision.confidence} | ${decision.source || 'unknown-source'}`);
        log('dim', `     Summary: ${decision.page_summary}`);

        if (decision.question_type === 'completion') {
            log('ok', '[+] AI detected completion');
            markStopped();
            return;
        }

        for (const action of decision.actions) {
            log('act', `    -> ${action.action_type} : ${action.reasoning}`);
            try {
                await executeAction(action, elements);
            } catch (err) {
                log('err', 'Action failed:', action.action_type, err && err.message);
            }
            await sleep(300 + Math.random() * 500);
        }

        // Layer-1 telemetry (audit r20/D): un-issued actions are
        // invisible unless we count the hole they leave. `elements` is
        // the cycle-start snapshot — exactly what the backend saw.
        const checkedGroups = new Set(
            elements.filter(e => (e.type === 'radio' || e.type === 'checkbox') && e.checked)
                    .map(e => e.name || e.sid));
        const seenGroups = new Set();
        const pending = elements.filter(e => {
            if (e.type === 'radio' || e.type === 'checkbox') {
                const key = e.name || e.sid;
                if (seenGroups.has(key)) return false;
                seenGroups.add(key);
                return !checkedGroups.has(key);
            }
            if (e.tag === 'input' || e.tag === 'textarea' || e.tag === 'select')
                return !(e.value || '').trim();
            if (e.editable) return !(e.value || '').trim();
            return false;   // buttons/links/labels: not answerable
        }).length;
        log('dim', `cycle coverage: ${decision.actions.length} action(s) ` +
            `issued, ${pending} group(s)/field(s) still pending on page`);

        if (decision.memory_note) {
            memory.push(decision.memory_note);
            if (memory.length > 50) memory.shift();
        }
    } catch (err) {
        if (!contextAlive()) {
            // Context died mid-cycle (extension reloaded/updated). One quiet
            // death beats an 'err' log every cycle on a line we can't even
            // deliver anywhere.
            killLoop();
        } else {
            // One bad cycle must neither kill the run nor flood the console.
            log('err', 'scan error:', err && err.message);
        }
    } finally {
        isScanning = false;
        if (isRunning && !contextAlive()) {
            killLoop();   // context died mid-run — stop cleanly, no reschedule
        }
        if (isRunning) {
            loopId = setTimeout(() => scan(tabId), SCAN_INTERVAL);
        }
    }
}

// ─── Message Handling ─────────────────────────────────────────────

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'START') {
        if (!isRunning) {
            RUN_ID = request.runId || Date.now();
            isRunning = true;
            lastFingerprint = '';
            stuckSince = Date.now();
            questionCount = 0;
            runStartTime = Date.now();
            memory = [];
            log('ok', '[+] Bot started');
            const myTab = sender.tab ? sender.tab.id : null;
            scan(myTab);
        }
        sendResponse({ status: 'started', build: BUILD });
        return true;
    }
    else if (request.action === 'STOP') {
        markStopped();
        log('ok', '[+] Bot stopped');
        sendResponse({ status: 'stopped' });
        return true;
    }
    return true;
});

// ─── Auto-resume after hard navigations ───────────────────────────

chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, async (resp) => {
    void chrome.runtime.lastError;
    // Whole body guarded: hitPanelHub() awaits inside an async callback
    // outside scan()'s try/catch — a dead context at load time must not
    // surface as an unhandled rejection here.
    try {
        if (!(resp && resp.resume && !isRunning)) return;
        // FAIL-CLOSED: a survey redirecting to its panel login wall IS a hard
        // navigation — auto-resume must verify it is NOT on a hub BEFORE
        // restarting the loop (audit Trap 3). An error here is truthy too,
        // so a failed check suppresses resume instead of permitting it (B.3).
        const hub = await hitPanelHub();
        if (hub) {
            if (hub === 'unknown')
                log('warn', '[?] Auto-resume suppressed — hub check unavailable');
            else
                log('ok', `[+] Auto-resume suppressed — panel hub (${hub})`);
            chrome.runtime.sendMessage({
                action: 'SET_RUN_STATE', running: false, tabId: null, runId: null
            }, () => void chrome.runtime.lastError);
            return;
        }
        RUN_ID = resp.runId || Date.now();
        const myTab = resp.tabId;
        isRunning = true;
        lastFingerprint = '';
        stuckSince = Date.now();
        runStartTime = Date.now();
        questionCount = 0;
        log('ok', '[+] Bot auto-resumed after navigation');
        scan(myTab);
    } catch (e) {
        try { log('err', 'resume error:', e && e.message); } catch (_) {}
    }
});

log('ok', `[+] content script live on ${location.host || 'about:blank'} ` +
    `(build ${BUILD}, frame ${window === top ? 'top' : 'child'})`);
log('[Sentinel] Content script loaded');

} // end re-injection guard
