// extension/modules/element-map.js — Role-based element collection

window.__sentinelModules = window.__sentinelModules || {};

;(function() {
    'use strict';

    const INTERACTIVE = 'button, input, select, textarea, a[href], [role], label, [contenteditable]';

    function accessibleName(el) {
        const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
        if (ids.length) {
            const parts = ids.map(i => {
                const n = document.getElementById(i);
                return n ? (n.innerText || n.textContent || '').trim() : '';
            }).filter(Boolean);
            if (parts.length) return parts.join(' ');
        }
        if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
        return (el.getAttribute('aria-label') || el.innerText || el.value || '').trim();
    }

    function semanticType(el) {
        const role = (el.getAttribute('role') || '').toLowerCase();
        const tag = el.tagName.toLowerCase();
        const type = (el.type || '').toLowerCase();
        if (role === 'slider' || role === 'spinbutton') return 'range';
        if (role === 'listbox' || role === 'combobox') return 'select';
        if (role === 'switch') return 'checkbox';
        if (role === 'radio' || type === 'radio') return 'radio';
        if (role === 'checkbox' || type === 'checkbox') return 'checkbox';
        if (role === 'textbox' || type === 'text' || type === 'email' || type === 'number' ||
            type === 'tel' || type === 'date' || type === 'url' || tag === 'textarea') return 'text';
        if (tag === 'select') return 'select';
        if (tag === 'button') return 'button';
        if (tag === 'a' && el.getAttribute('href')) return 'link';
        if (el.isContentEditable) return 'text';
        return 'unknown';
    }

    function isEffectivelyVisible(el) {
        try {
            if (el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return true;
        } catch (e) {
            const s = getComputedStyle(el);
            if (s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0') return true;
        }
        const parentLabel = el.closest('label');
        if (parentLabel) {
            try {
                if (parentLabel.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return true;
            } catch (e) {}
        }
        return false;
    }

    function* walk(root, depth) {
        if (depth > 12) return;
        try {
            const all = root.querySelectorAll('*');
            for (const el of all) {
                if (el.shadowRoot) yield* walk(el.shadowRoot, depth + 1);
            }
            for (const el of root.querySelectorAll(INTERACTIVE)) {
                yield el;
            }
        } catch (e) {}
    }

    function collectElements(doc, offset, frameId) {
        const view = doc.defaultView;
        if (!view) return [];
        const elements = [];
        const seen = new Set();
        let nextId = 1;

        for (const el of walk(doc, 0)) {
            if (!isEffectivelyVisible(el)) continue;
            const key = (el.tagName + '|' + (el.type || '') + '|' + (el.name || '') + '|' + (el.value || '') + '|' + accessibleName(el)).slice(0, 120);
            if (seen.has(key)) continue;
            seen.add(key);

            const rect = el.getBoundingClientRect();
            const entry = {
                id: nextId++,
                tag: el.tagName.toLowerCase(),
                type: (el.type || el.getAttribute('type') || '').toLowerCase(),
                role: (el.getAttribute('role') || '').toLowerCase(),
                name: (el.name || '').toLowerCase(),
                value: (el.value || '').toString().slice(0, 200),
                text: accessibleName(el).slice(0, 200),
                x: Math.round(offset.left + rect.left + rect.width / 2),
                y: Math.round(offset.top + rect.top + rect.height / 2),
                semanticType: semanticType(el),
                accessibleName: accessibleName(el).slice(0, 200),
                isVisible: true,
                frameId: frameId,
                options: []
            };

            if (el.tagName.toLowerCase() === 'select' && el.options) {
                for (const opt of el.options) {
                    entry.options.push({
                        value: (opt.value || '').toString(),
                        text: (opt.text || '').trim().slice(0, 100)
                    });
                }
            }
            elements.push(entry);
        }
        return elements;
    }

    function getQuestionText(doc) {
        const candidates = doc.querySelectorAll('[role="heading"], h1, h2, h3, h4, .question-text, .surveyQuestionText, .qtext, .question-title');
        if (candidates.length) return candidates[0].innerText.trim().slice(0, 500);
        const bodyText = doc.body ? doc.body.innerText.trim() : '';
        const lines = bodyText.split('\n');
        const found = lines.find(l => l.trim().length > 10);
        return found ? found.trim().slice(0, 500) : '';
    }

    window.__sentinelModules.elementMap = {
        accessibleName, semanticType, isEffectivelyVisible,
        collectElements, getQuestionText, walk
    };
})();
