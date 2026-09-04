// Sentinel Survey Bot — content script orchestrator (r30)
// Loads modules in order, wires state, runs scan loop.

if (window.__sentinelContentLoaded) {
    // already instrumented; skip everything below
} else {
window.__sentinelContentLoaded = true;

// ── Load modules in dependency order ──────────────────────────────────
const MODULES = [
    'extension/modules/state.js',
    'extension/modules/fingerprint.js',
    'extension/modules/element-map.js',
    'extension/modules/actions.js',
    'extension/modules/nav.js'
];

function loadScript(src) {
    return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = reject;
        document.head.appendChild(s);
    });
}

(async function init() {
    try {
        for (const src of MODULES) {
            await loadScript(chrome.runtime.getURL(src));
        }
    } catch (e) {
        console.error('[Sentinel] module load failed:', e);
        return;
    }

    const { state } = window.__sentinelModules;
    const { fingerprint } = window.__sentinelModules;
    const { elementMap } = window.__sentinelModules;
    const { actions } = window.__sentinelModules;
    const { nav } = window.__sentinelModules;
    const S = state.S;

    // ── Helpers wired to state ────────────────────────────────────────
    const sleep = actions.sleep;
    const log = (kind, ...a) => {
        if (typeof kind !== 'string' || !['ok','ai','act','err','warn','dim','info'].includes(kind)) {
            a.unshift(kind);
            kind = 'info';
        }
        try {
            chrome.runtime.sendMessage(
                { action: 'LOG', line: a.map(String).join(' '), kind },
                () => void chrome.runtime.lastError
            );
        } catch (e) {}
    };
    const markStopped = state.markStopped;
    const killLoop = state.killLoop;
    const contextAlive = state.contextAlive;

    // ── Frame identity ────────────────────────────────────────────────
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

    // ── Drift-safe actuation ──────────────────────────────────────────
    async function settleAndVerify(el, snap) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        await sleep(150);
        if (!snap || !snap.text) return true;
        const parts = [fingerprint.signatureText(el)];
        if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox')) {
            const lbl = (el.labels && el.labels[0]) || (el.closest ? el.closest('label') : null);
            if (lbl) parts.push(fingerprint.signatureText(lbl));
        } else if (el.tagName === 'SELECT') {
            const sel = el.selectedOptions && el.selectedOptions[0];
            if (sel) parts.push(fingerprint.signatureText(sel));
        }
        const sig = parts.join(' ').replace(/\s+/g, ' ');
        const want = snap.text.replace(/\s*\[iframe\]\s*$/i, '').toLowerCase().replace(/\s+/g, ' ').trim();
        return sig.includes(want.slice(0, 40));
    }

    // ── Stable-handle lookup ──────────────────────────────────────────
    function findNodeBySid(sid, snap) {
        let el = null;
        try { el = document.querySelector(`[data-sentinel-sid="${sid}"]`); } catch (e) {}
        if (el) return el;
        for (const f of document.querySelectorAll('iframe')) {
            try {
                const d = f.contentDocument;
                if (!d) continue;
                el = d.querySelector(`[data-sentinel-sid="${sid}"]`);
                if (el) return el;
            } catch (e) {}
        }
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

    // ── Element map ───────────────────────────────────────────────────
    function getElementMap() {
        const elements = [];
        S.elRegistry.clear();
        S.nextId = 0;
        const census = { filtered: 0 };
        const topElements = elementMap.collectElements(document, { left: 0, top: 0 }, FRAME.frameId);
        elements.push(...topElements);
        for (const el of topElements) {
            S.elRegistry.set(el.id, document.querySelector(`[data-sentinel-sid="${el.sid}"]`) || el);
        }
        S.lastMapDebug.elements = elements.slice();
        S.lastMapDebug.census = census;
        log('dim', 'map: ' + elements.length + ' top element(s), ' + census.filtered + ' filtered');
        return elements;
    }

    // ── Scan loop ─────────────────────────────────────────────────────
    async function scan(tabId) {
        if (!S.isRunning || S.isScanning) return;
        if (!contextAlive()) { killLoop(); return; }
        S.isScanning = true;
        const now = Date.now();
        if (now - S.lastScanAt < 15000) {
            S.isScanning = false;
            S.loopId = setTimeout(() => scan(tabId), 15000 - (now - S.lastScanAt));
            return;
        }
        S.lastScanAt = now;

        try {
            const hubResult = await new Promise(res => {
                chrome.runtime.sendMessage({ action: 'CHECK_PANEL_HUB', url: location.href }, r => {
                    void chrome.runtime.lastError; res(r || {});
                });
            });
            if (hubResult && hubResult.hub) {
                log('ok', '[+] Panel hub reached — stopping');
                markStopped();
                return;
            } else if (hubResult === 'unknown') {
                log('warn', '[?] Panel-hub check unavailable — continuing');
            }

            if (document.visibilityState !== 'visible') {
                S.stuckSince = Date.now();
                S.isScanning = false;
                return;
            }

            if (nav.isDisqualified()) {
                log('err', '[!] Disqualified');
                chrome.runtime.sendMessage({ action: 'LEARN_RULE', memory: S.memory, tabId });
                markStopped();
                return;
            }

            const CONF_GATE = (() => {
                try { return localStorage.getItem('__sentinelConfirmGate') === '1'; }
                catch (e) { return false; }
            })();

            if (nav.isComplete()) {
                if (CONF_GATE) {
                    const ok = confirm('Survey appears complete.\n\nPress OK to confirm submission, or Cancel to continue.');
                    if (!ok) {
                        log('warn', 'Confirmation gate: user declined completion');
                        S.isScanning = false;
                        return;
                    }
                }
                log('ok', '[+] Completed');
                markStopped();
                return;
            }
            if (nav.detectCaptcha()) {
                log('err', '[!] CAPTCHA detected — pausing');
                alert('CAPTCHA detected. Solve it manually, then click Start.');
                markStopped();
                return;
            }

            const fp = fingerprint.getFingerprint();
            if (fp === S.lastFingerprint) {
                if (S.stuckSince && (now - S.stuckSince > 35000)) {
                    log('warn', '[!] Stuck — emergency Next');
                    await nav.clickNext();
                    S.stuckSince = now;
                }
            } else {
                S.lastFingerprint = fp;
                S.stuckSince = now;
                S.questionCount++;
                S.driftBudget.clear();
                S.lastNavTarget = null; S.navTries = 0;
                S.frameDeferCount = 0;
            }

            const screenshotData = await new Promise(resolve => {
                chrome.runtime.sendMessage({ action: 'CAPTURE_SCREENSHOT', tabId }, resolve);
            });

            if (!screenshotData || screenshotData.error) {
                log('err', 'Screenshot failed:', screenshotData ? screenshotData.error : 'no response');
                S.isScanning = false;
                return;
            }

            const shotStr = screenshotData && screenshotData.screenshot;
            const screenshotB64 = (shotStr && shotStr.split(',').length > 1)
                ? shotStr.split(',')[1] : '';

            let elements = getElementMap();
            let pageText = document.body.innerText;
            let currentUrl = location.href;

            const notReady = [...document.querySelectorAll('iframe')].some(f => {
                try {
                    const d = f.contentDocument;
                    return d && d.readyState !== 'complete';
                } catch (e) { return false; }
            });
            if (notReady) {
                if (!S.iframeWaitSince) S.iframeWaitSince = Date.now();
                if (Date.now() - S.iframeWaitSince < 4000) {
                    log('dim', 'iframe still loading — deferring scan');
                    S.isScanning = false;
                    return;
                }
                log('warn', 'iframe load timeout — mapping without it');
            }
            S.iframeWaitSince = 0;

            for (const iframe of document.querySelectorAll('iframe')) {
                try {
                    const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
                    if (iframeDoc && iframeDoc.body) {
                        const iframeText = iframeDoc.body.innerText;
                        if (iframeText && iframeText.trim().length > 10)
                            pageText += '\n[IFRAME CONTENT]\n' + iframeText;
                    }
                } catch (e) {
                    log('warn', 'iframe mapping failed:', e && e.message);
                }
            }

            const dom = [];
            const walkDom = (el, path) => {
                for (const child of el.children) {
                    const tag = child.tagName.toLowerCase();
                    const interactive = child.dataset.sentinelSid ||
                        ['INPUT','SELECT','TEXTAREA','BUTTON'].includes(child.tagName) ||
                        child.isContentEditable;
                    if (interactive) {
                        dom.push({
                            sid: child.dataset.sentinelSid || null,
                            tag, name: child.getAttribute('name') || '',
                            type: child.type || '',
                            text: fingerprint.identityText(child).slice(0, 60),
                            path: path.slice(-2).join(' > ')
                        });
                    }
                    if (tag === 'iframe') {
                        try { walkDom(child.contentDocument.body, [...path, 'iframe']); }
                        catch (e) { dom.push({ path: [...path,'iframe'].join(' > '), note: 'x-origin' }); }
                    } else if (!interactive) {
                        walkDom(child, [...path, tag + (child.id ? '#'+child.id : '')]);
                    }
                }
            };
            if (document.body) walkDom(document.body, []);

            const cycleId = `${S.RUN_ID || 0}-${Date.now().toString(36)}`;

            const backendResp = await new Promise(resolve => {
                chrome.runtime.sendMessage({
                    action: 'CALL_BACKEND',
                    payload: {
                        cycle_id: cycleId,
                        session_id: `${tabId}-${S.RUN_ID || 0}`,
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
                S.isScanning = false;
                return;
            }
            log('dim', 'backend cycle ' + cycleId + ': ' + (backendResp.elapsedMs || '?') + 'ms');

            const decision = backendResp.data;
            if (!decision || !Array.isArray(decision.actions)) {
                log('warn', 'decision malformed:', JSON.stringify(decision).slice(0, 160));
                S.isScanning = false;
                return;
            }
            if (decision.actions.length === 0) {
                log('warn', 'decision returned ZERO actions - nothing answerable on this page');
            }

            if (backendResp.dryRun) {
                log('dim', '[DRY] ' + decision.question_type + ' (' + decision.confidence + ') -> ' +
                    decision.actions.map(a => a.action_type + ':' + (a.element_id ?? '-')).join(', '));
                if (decision.memory_note) {
                    S.memory.push(decision.memory_note);
                    if (S.memory.length > 50) S.memory.shift();
                }
                S.isScanning = false;
                return;
            }

            log('ai', '[AI] ' + decision.question_type + ' | confidence: ' + decision.confidence + ' | ' + (decision.source || 'unknown-source'));
            log('dim', '     Summary: ' + decision.page_summary);

            if (decision.question_type === 'completion') {
                log('ok', '[+] AI detected completion');
                markStopped();
                S.isScanning = false;
                return;
            }

            for (const action of decision.actions) {
                log('act', '    -> ' + action.action_type + ' : ' + action.reasoning);
                try {
                    await actions.executeAction(action, elements);
                } catch (err) {
                    log('err', 'Action failed:', action.action_type, err && err.message);
                }
                await sleep(300 + Math.random() * 500);
            }

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
                return false;
            }).length;
            log('dim', 'cycle coverage: ' + decision.actions.length + ' action(s) issued, ' + pending + ' group(s)/field(s) still pending on page');

            if (decision.memory_note) {
                S.memory.push(decision.memory_note);
                if (S.memory.length > 50) S.memory.shift();
            }
        } catch (err) {
            if (!contextAlive()) {
                killLoop();
            } else {
                log('err', 'scan error:', err && err.message);
            }
        } finally {
            S.isScanning = false;
            if (S.isRunning && !contextAlive()) killLoop();
            if (S.isRunning) {
                S.loopId = setTimeout(() => scan(tabId), 15000);
            }
        }
    }

    // ── Message handling ──────────────────────────────────────────────
    chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
        if (request.action === 'START') {
            if (!S.isRunning) {
                S.RUN_ID = request.runId || Date.now();
                S.isRunning = true;
                S.lastFingerprint = '';
                S.stuckSince = Date.now();
                S.questionCount = 0;
                S.runStartTime = Date.now();
                S.memory = [];
                log('ok', '[+] Bot started');
                const myTab = sender.tab ? sender.tab.id : null;
                scan(myTab);
            }
            sendResponse({ status: 'started', build: 'r30' });
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

    // ── Auto-resume after hard navigations ────────────────────────────
    chrome.runtime.sendMessage({ action: 'GET_RUN_STATE' }, async (resp) => {
        void chrome.runtime.lastError;
        try {
            if (!(resp && resp.resume && !S.isRunning)) return;
            const hubResult = await new Promise(res => {
                chrome.runtime.sendMessage({ action: 'CHECK_PANEL_HUB', url: location.href }, r => {
                    void chrome.runtime.lastError; res(r || {});
                });
            });
            if (hubResult && hubResult.hub) {
                if (hubResult === 'unknown')
                    log('warn', '[?] Auto-resume suppressed — hub check unavailable');
                else
                    log('ok', '[+] Auto-resume suppressed — panel hub');
                chrome.runtime.sendMessage({
                    action: 'SET_RUN_STATE', running: false, tabId: null, runId: null
                }, () => void chrome.runtime.lastError);
                return;
            }
            S.RUN_ID = resp.runId || Date.now();
            const myTab = resp.tabId;
            S.isRunning = true;
            S.lastFingerprint = '';
            S.stuckSince = Date.now();
            S.runStartTime = Date.now();
            S.questionCount = 0;
            log('ok', '[+] Bot auto-resumed after navigation');
            scan(myTab);
        } catch (e) {
            try { log('err', 'resume error:', e && e.message); } catch (_) {}
        }
    });

    log('ok', '[+] content script live on ' + (location.host || 'about:blank') + ' (build r30, frame ' + (window === top ? 'top' : 'child') + ')');
    log('[Sentinel] Content script loaded');

} // end async init
)(); // end IIFE

} // end re-injection guard
