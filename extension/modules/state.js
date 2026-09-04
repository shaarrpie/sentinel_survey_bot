// extension/modules/state.js — Global state, drift budget, element registry
// Loaded before all other modules.

window.__sentinelModules = window.__sentinelModules || {};

;(function() {
    'use strict';

    const S = {
        isRunning: false,
        loopId: null,
        isScanning: false,
        RUN_ID: null,
        memory: [],
        lastFingerprint: '',
        stuckSince: 0,
        elRegistry: new Map(),
        frameOffsets: new WeakMap(),
        sidMap: new WeakMap(),
        sidSeq: 0,
        driftBudget: new Map(),
        nextId: 0,
        iframeWaitSince: 0,
        lastNavTarget: null,
        navTries: 0,
        frameDeferCount: 0,
        questionCount: 0,
        runStartTime: 0,
        lastScanAt: 0,
        lastMapDebug: { elements: [], filtered: [] }
    };

    function nodeSid(el) {
        let s = S.sidMap.get(el);
        if (!s) { s = 'n' + (++S.sidSeq).toString(36); S.sidMap.set(el, s); }
        return s;
    }

    function markStopped() {
        S.isRunning = false;
        if (S.loopId) { clearTimeout(S.loopId); S.loopId = null; }
        S.questionCount = 0;
        S.runStartTime = 0;
        try {
            chrome.runtime.sendMessage(
                { action: 'SET_RUN_STATE', running: false, tabId: null, runId: null },
                () => void chrome.runtime.lastError
            );
        } catch (e) {}
    }

    function killLoop() {
        S.isRunning = false;
        if (S.loopId) { clearTimeout(S.loopId); S.loopId = null; }
    }

    function contextAlive() {
        try {
            return !!(chrome && chrome.runtime && chrome.runtime.id);
        } catch (e) {
            return false;
        }
    }

    window.__sentinelModules.state = {
        S, nodeSid, markStopped, killLoop, contextAlive
    };
})();
