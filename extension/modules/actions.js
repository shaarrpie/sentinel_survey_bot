// extension/modules/actions.js — Action execution (click, type, select, scroll)

window.__sentinelModules = window.__sentinelModules || {};

;(function() {
    'use strict';

    function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

    async function humanClick(el) {
        if (!el.isConnected) return;
        const rect = el.getBoundingClientRect();
        const o = { dx: 0, dy: 0 };
        try { o.dx = window.frameElement?.getBoundingClientRect().left || 0; } catch (e) {}
        try { o.dy = window.frameElement?.getBoundingClientRect().top || 0; } catch (e) {}
        const tx = o.dx + rect.left + rect.width / 2;
        const ty = o.dy + rect.top + rect.height / 2;

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
            const ctrl = el.control || el.querySelector('input, textarea');
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

    async function executeAction(action, elements) {
        const { action_type, element_id, coordinates, value } = action;

        function resolveEl(eid) {
            if (eid !== null && eid !== undefined) {
                const el = window.__sentinelModules.state.S.elRegistry.get(eid);
                if (el && el.isConnected) return el;
                return null;
            }
            return coordinates
                ? document.elementFromPoint(coordinates[0], coordinates[1])
                : null;
        }

        if (action_type === 'click') {
            let el = resolveEl(element_id);
            if (!el) { console.warn('click dropped: no element for id', element_id); return; }
            const snap = elements.find(e => e.id === element_id);
            const sid = (snap && snap.sid) ||
                el.getAttribute('data-sentinel-sid') || ('id' + element_id);
            for (let attempt = 1; attempt <= 3; attempt++) {
                if (await settleAndVerify(el, snap)) {
                    await humanClick(el);
                    window.__sentinelModules.state.S.driftBudget.set(sid, 0);
                    return;
                }
                const skips = (window.__sentinelModules.state.S.driftBudget.get(sid) || 0) + 1;
                window.__sentinelModules.state.S.driftBudget.set(sid, skips);
                console.warn(`Element drifted — skipping ${sid} (${skips}/3)`);
                if (skips >= 3) {
                    await humanClick(el);
                    window.__sentinelModules.state.S.driftBudget.set(sid, 0);
                    return;
                }
                const fresh = findNodeBySid(sid, snap);
                if (!fresh) { console.error('Re-resolve failed for', sid); return; }
                console.log('Re-resolved by stable sid', sid);
                el = fresh;
            }
        }
        else if (action_type === 'type') {
            let el = resolveEl(element_id);
            if (!el || !value) {
                console.warn(!el ? `type dropped: no element for id ${element_id}` : `type dropped: empty value for id ${element_id}`);
                return;
            }
            const snap = elements.find(e => e.id === element_id);
            const sid = (snap && snap.sid) ||
                el.getAttribute('data-sentinel-sid') || ('id' + element_id);
            for (let attempt = 1; attempt <= 3; attempt++) {
                if (await settleAndVerify(el, snap)) {
                    await doType(el, value);
                    window.__sentinelModules.state.S.driftBudget.set(sid, 0);
                    return;
                }
                const skips = (window.__sentinelModules.state.S.driftBudget.get(sid) || 0) + 1;
                window.__sentinelModules.state.S.driftBudget.set(sid, skips);
                const got = signatureText(el).slice(0, 48);
                const wantTxt = String((snap && snap.text) || '').replace(/\s*\[iframe\]\s*$/i, '').slice(0, 48);
                console.warn(`Type target drifted — skipping ${sid} (${skips}/3) sig="${got}" want="${wantTxt}"`);
                if (skips >= 3) {
                    await doType(el, value);
                    window.__sentinelModules.state.S.driftBudget.set(sid, 0);
                    console.warn(`forced type result: ${el.tagName}${el.type ? '[' + el.type + ']' : ''}${el.name ? ' name=' + el.name : ''} value=${JSON.stringify(((el.value ?? el.textContent) || '').slice(0, 30))}${el.readOnly ? ' READONLY' : ''}${el.disabled ? ' DISABLED' : ''}`);
                    return;
                }
                const fresh = findNodeBySid(sid, snap);
                if (!fresh) { console.error('Re-resolve failed for', sid); return; }
                console.log('Re-resolved by stable sid', sid);
                el = fresh;
            }
        }
        else if (action_type === 'select_option') {
            const el = resolveEl(element_id);
            if (!el || el.tagName !== 'SELECT' || !value) {
                console.warn('select_option dropped:', !el ? `no element for id ${element_id}` : el.tagName !== 'SELECT' ? `target is ${el.tagName}, not SELECT` : 'no value supplied');
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
                        const el = window.__sentinelModules.state.S.elRegistry.get(parseInt(trimmed));
                        if (el) { await humanClick(el); hits++; }
                    } else {
                        for (const e of elements) {
                            if (e.text && e.text.toLowerCase().includes(trimmed.toLowerCase())) {
                                const el = window.__sentinelModules.state.S.elRegistry.get(e.id);
                                if (el) { await humanClick(el); hits++; }
                            }
                        }
                    }
                }
                if (!hits) console.warn('select_multi matched nothing for:', value);
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

    async function settleAndVerify(el, snap) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        await sleep(150);
        if (!snap || !snap.text) return true;
        const parts = [signatureText(el)];
        if (el.tagName === 'INPUT' && (el.type === 'radio' || el.type === 'checkbox')) {
            const lbl = (el.labels && el.labels[0]) || (el.closest ? el.closest('label') : null);
            if (lbl) parts.push(signatureText(lbl));
        } else if (el.tagName === 'SELECT') {
            const sel = el.selectedOptions && el.selectedOptions[0];
            if (sel) parts.push(signatureText(sel));
        }
        const sig = parts.join(' ').replace(/\s+/g, ' ');
        const want = snap.text.replace(/\s*\[iframe\]\s*$/i, '').toLowerCase().replace(/\s+/g, ' ').trim();
        return sig.includes(want.slice(0, 40));
    }

    window.__sentinelModules.actions = {
        sleep, humanClick, humanType, doType, executeAction, settleAndVerify
    };
})();
