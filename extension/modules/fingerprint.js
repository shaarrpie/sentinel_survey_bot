// extension/modules/fingerprint.js — Semantic fingerprinting

window.__sentinelModules = window.__sentinelModules || {};

;(function() {
    'use strict';

    function identityText(el) {
        const inner = el.innerText || '';
        const aria  = (el.getAttribute && el.getAttribute('aria-label')) || '';
        const ph    = (el.getAttribute && el.getAttribute('placeholder')) || '';
        const val   = el.value || '';
        const tc    = el.textContent || '';
        return (inner || aria || ph || val || tc).trim();
    }

    function signatureText(el) {
        return identityText(el).toLowerCase().replace(/\s+/g, ' ');
    }

    function getFingerprint() {
        const parts = [location.href];
        const groups = new Map();
        const scanDoc = (doc, prefix) => {
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
        };
        scanDoc(document, '');
        for (const f of document.querySelectorAll('iframe')) {
            try {
                const d = f.contentDocument;
                if (d && d.body) scanDoc(d, 'IF:');
            } catch (e) { /* cross-origin stays out of the hash */ }
        }
        for (const [key, info] of groups) {
            parts.push(key + ':' + info.kind + ':' + info.checked + ':' + info.val);
        }
        const s = parts.join('|');
        let h = 5381;
        for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
        return h.toString(36);
    }

    window.__sentinelModules.fingerprint = {
        identityText, signatureText, getFingerprint
    };
})();
