// extension/modules/nav.js — Navigation, completion/DQ/captcha detection

window.__sentinelModules = window.__sentinelModules || {};

;(function() {
    'use strict';

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
                                    window.__sentinelModules.state.S.frameDeferCount++;
                                    const mapped = el.ownerDocument
                                        .querySelectorAll('[data-sentinel-sid]').length;
                                    if (mapped === 0 && window.__sentinelModules.state.S.frameDeferCount >= 2) {
                                        console.error('DEADLOCK: frame has', open, 'unanswered group(s) but 0 mapped elements — mapping broken');
                                    } else {
                                        console.warn('clickNext: frame submit deferred —', open, 'unanswered group(s) inside frame');
                                    }
                                    return false;
                                }
                            }
                            el.scrollIntoView({ block: 'center' });
                            await window.__sentinelModules.actions.sleep(200);
                            const navKey = el.getAttribute('data-sentinel-sid') ||
                                (el.innerText || '').trim().slice(0, 24);
                            if (navKey === window.__sentinelModules.state.S.lastNavTarget) {
                                if (++window.__sentinelModules.state.S.navTries >= 3) {
                                    console.warn('clickNext: "' + navKey + '" clicked 3× without advancing — releasing target');
                                    return false;
                                }
                            } else {
                                window.__sentinelModules.state.S.lastNavTarget = navKey;
                                window.__sentinelModules.state.S.navTries = 1;
                            }
                            console.log('clickNext: "' + (text.slice(0, 24) || '(no text)') + '" via ' + sel + ' [' + tag + ']');
                            await window.__sentinelModules.actions.humanClick(el);
                            return true;
                        }
                    }
                }
            }
        }
        console.warn('clickNext: no next button found in top document or iframes');
        return false;
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

    window.__sentinelModules.nav = { clickNext, isDisqualified, isComplete, detectCaptcha };
})();
