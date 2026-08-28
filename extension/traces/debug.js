'use strict';
const $ = s => document.querySelector(s);

const BACKEND_DIRECT = 'http://127.0.0.1:8000';   // fallback only, see below
const MAX_CLIENT = 3000;

const state = {
    seq: 0,
    events: [],            // full ingested history (client side)
    rendered: 0,           // index into events already in the DOM
    paused: false,
    autoscroll: true,
    src: 'all',
    level: 'all',
    q: '',
    boot: null,
    pollMs: 0,
    // Direct mode: page opened outside the extension (file:// dev) talks to
    // the backend itself. Inside the extension it routes via background.js.
    direct: !(typeof chrome !== 'undefined' && chrome.runtime &&
              chrome.runtime.sendMessage),
};
const cycles = new Map();  // cycle id -> { id, stages: [...], verdict, t0 }

/* ── transport ───────────────────────────────────────────── */
async function fetchTraces() {
    const t0 = performance.now();
    let data = null, error = null;
    if (state.direct) {
        try {
            const r = await fetch(`${BACKEND_DIRECT}/traces?since=${state.seq}`);
            if (!r.ok) throw new Error('HTTP ' + r.status);
            data = await r.json();
        } catch (e) { error = e.message; }
    } else {
        const resp = await new Promise(res =>
            chrome.runtime.sendMessage({ action: 'GET_TRACES', since: state.seq },
                r => { void chrome.runtime.lastError; res(r); }));
        if (!resp) error = 'no response from service worker';
        else if (resp.error) error = resp.error;
        else data = resp.data;
    }
    state.pollMs = Math.round(performance.now() - t0);
    return { data, error };
}
async function fetchStatus() {
    if (state.direct) {
        try {
            const r = await fetch(`${BACKEND_DIRECT}/status`);
            return r.ok ? await r.json() : null;
        } catch (e) { return null; }
    }
    const resp = await new Promise(res =>
        chrome.runtime.sendMessage({ action: 'GET_STATUS' },
            r => { void chrome.runtime.lastError; res(r); }));
    return resp && resp.data ? resp.data : null;
}

/* ── formatting ──────────────────────────────────────────── */
function fmtT(t) {
    const d = new Date(t * 1000);
    const p = n => String(n).padStart(2, '0');
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` +
           `.${String(d.getMilliseconds()).padStart(3, '0')}`;
}
function ago(t) {
    const s = Math.max(0, Math.round(Date.now() / 1000 - t));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
}
function latClass(ms) { return ms > 5000 ? 'bad' : ms > 2000 ? 'slow' : ''; }
function latBar(ms, max) {
    const pct = Math.min(100, Math.round((ms / max) * 100));
    return `<span class="latbar"><i class="${latClass(ms)}" style="width:${pct}%"></i></span>`;
}

/* ── stream rendering ────────────────────────────────────── */
const stream = $('#stream');
function passes(ev) {
    if (state.src !== 'all' && ev.src !== state.src) return false;
    if (state.level !== 'all' && ev.level !== state.level) return false;
    if (state.q) {
        const hay = (ev.msg + ' ' + ev.kind + ' ' + ev.src + ' ' +
                     (ev.data ? JSON.stringify(ev.data) : '')).toLowerCase();
        if (!hay.includes(state.q)) return false;
    }
    return true;
}
function rowFor(ev) {
    const el = document.createElement('div');
    el.className = `tr s-${ev.src} ${ev.level}` +
                   (ev.data ? ' hasdata' : '');
    const msCell = ev.ms != null
        ? `${latBar(ev.ms, 8000)}${Math.round(ev.ms)}ms` : '';
    el.innerHTML =
        `<span class="seq">#${ev.seq}</span>` +
        `<time>${fmtT(ev.t)}</time>` +
        `<span class="tag src">${ev.src.toUpperCase()}</span>` +
        `<span class="tag k">${ev.kind}</span>` +
        `<span class="ms">${msCell}</span>` +
        `<span class="msg"></span>` +
        `<span class="x">${ev.data ? '▸' : ''}</span>`;
    el.querySelector('.msg').textContent = ev.msg;
    if (ev.data) {
        const pre = document.createElement('pre');
        pre.className = 'payload';
        pre.textContent = JSON.stringify(ev.data, null, 2);
        el.appendChild(pre);
        el.addEventListener('click', () => {
            el.classList.toggle('open');
            el.querySelector('.x').textContent =
                el.classList.contains('open') ? '▾' : '▸';
        });
    }
    return el;
}
function renderAll() {
    stream.innerHTML = '';
    state.rendered = 0;
    const empty = !state.events.length;
    if (empty) {
        stream.innerHTML =
            '<div class="empty">— waiting for first trace (is the backend up?) —</div>';
        return;
    }
    const frag = document.createDocumentFragment();
    for (const ev of state.events) {
        if (passes(ev)) frag.appendChild(rowFor(ev));
    }
    stream.appendChild(frag);
    state.rendered = state.events.length;
    if (state.autoscroll) stream.scrollTop = stream.scrollHeight;
    $('#count').textContent = state.events.length + ' events';
}
function appendNew() {
    const empty = stream.querySelector('.empty');
    if (empty) empty.remove();
    const nearBottom =
        stream.scrollHeight - stream.scrollTop - stream.clientHeight < 40;
    const frag = document.createDocumentFragment();
    let shown = 0;
    for (let i = state.rendered; i < state.events.length; i++) {
        const ev = state.events[i];
        if (passes(ev)) { frag.appendChild(rowFor(ev)); shown++; }
    }
    state.rendered = state.events.length;
    if (shown) {
        stream.appendChild(frag);
        if (state.autoscroll && (nearBottom || state.autoscroll))
            stream.scrollTop = stream.scrollHeight;
    }
    $('#count').textContent = state.events.length + ' events';
}

/* ── cycle reconstruction ────────────────────────────────── */
function ingestCycle(ev) {
    const id = ev.data && ev.data.cycle;
    if (!id) return;
    let c = cycles.get(id);
    if (!c) { c = { id, stages: [], t0: ev.t, verdict: null, error: false };
              cycles.set(id, c); }
    c.stages.push({ name: ev.kind, src: ev.src, t: ev.t, ms: ev.ms,
                    level: ev.level });
    if (ev.level === 'error') c.error = true;
    if (ev.kind === 'decision') c.verdict = ev.msg;
}
function renderCycles() {
    const list = $('#cycList');
    $('#cycEmpty').style.display = cycles.size ? 'none' : 'block';
    const recent = [...cycles.values()].slice(-12).reverse();
    list.innerHTML = '';
    const SCALE = 8000;   // ms full-scale for bars
    for (const c of recent) {
        const total = c.stages.reduce((a, s) => a + (s.ms || 0), 0);
        const div = document.createElement('div');
        div.className = 'cyc';
        div.innerHTML =
            `<span class="cid">CYCLE ${c.id}</span>` +
            `<span class="ctot">${total ? Math.round(total) + 'ms' : '…'}</span>`;
        for (const s of c.stages) {
            const ms = s.ms || 0;
            const pct = Math.min(100, Math.round((ms / SCALE) * 100));
            const row = document.createElement('div');
            row.className = 'stage';
            row.innerHTML =
                `<span class="nm">${s.name}</span>` +
                `<span class="bar"><i class="${latClass(ms) || (s.level === 'error' ? 'bad' : '')}" style="width:${ms ? Math.max(pct, 3) : 0}%"></i></span>` +
                `<span class="v">${ms ? Math.round(ms) + 'ms' : '·'}</span>`;
            div.appendChild(row);
        }
        if (c.verdict) {
            const v = document.createElement('div');
            v.className = 'verdict' + (c.error ? ' bad' : '');
            v.textContent = c.verdict;
            div.appendChild(v);
        }
        list.appendChild(div);
    }
}

/* ── omni strip + header chips ───────────────────────────── */
function renderStatus(s) {
    const brainChip = $('#chipBrain'), omniChip = $('#chipOmni');
    if (!s) {
        brainChip.className = 'chip down';
        $('#chipBrainTxt').textContent = 'backend offline';
        omniChip.className = 'chip down';
        $('#chipOmniTxt').textContent = 'omni unknown';
        $('#omniState').className = 'display down';
        $('#omniState').textContent = 'OMNI · NO BACKEND';
        return;
    }
    brainChip.className = 'chip up';
    $('#chipBrainTxt').textContent =
        `backend up ${Math.round(s.uptime_s || 0)}s · boot ${s.boot_id || '?'}`;
    const o = s.omni || {};
    if (o.loaded) {
        omniChip.className = 'chip up';
        $('#chipOmniTxt').textContent =
            `omni: ${o.provider || '?'}/${(o.model || '?').split('/').pop()}`;
        $('#omniState').className = 'display up';
        $('#omniState').textContent = 'OMNI ROUTER · LOADED';
    } else {
        omniChip.className = 'chip down';
        $('#chipOmniTxt').textContent = 'omni: NOT LOADED';
        $('#omniState').className = 'display down';
        $('#omniState').textContent = 'OMNI ROUTER · NOT LOADED';
    }
    $('#oProv').textContent  = o.provider || '—';
    $('#oModel').textContent = o.model || '—';
    $('#oUrl').textContent   = o.base_url || '—';
    $('#oKey').textContent   = o.api_key_set ? 'set' : 'MISSING';
    $('#oCalls').textContent = o.calls || 0;
    $('#oErrs').textContent  = o.errors || 0;
    $('#oP50').textContent   = o.p50_ms != null ? Math.round(o.p50_ms) + 'ms' : '—';
    $('#oLast').textContent  = o.last_ms != null
        ? `${Math.round(o.last_ms)}ms · ${o.last_call_at ? ago(o.last_call_at) : ''}`
        : '—';
    const p50bar = $('#oP50bar'), lastbar = $('#oLastbar');
    p50bar.style.width = Math.min(100, (o.p50_ms || 0) / 80) + '%';
    p50bar.className = latClass(o.p50_ms || 0);
    lastbar.style.width = Math.min(100, (o.last_ms || 0) / 80) + '%';
    lastbar.className = latClass(o.last_ms || 0);
}

/* ── main loop ───────────────────────────────────────────── */
async function poll() {
    const { data, error } = await fetchTraces();
    $('#fPoll').textContent = state.pollMs + 'ms';
    if (error) {
        $('#chipBrain').className = 'chip down';
        $('#chipBrainTxt').textContent = 'backend: ' + error.slice(0, 26);
        return;
    }
    const bootId = data.boot && data.boot.boot_id;
    if (state.boot && bootId && bootId !== state.boot) {
        // backend restarted — its ring buffer reset, so reset with it
        state.events = []; cycles.clear(); state.rendered = 0;
        renderAll(); renderCycles();
        const note = { seq: '—', t: Date.now() / 1000, src: 'sys',
            kind: 'state', level: 'warn',
            msg: `backend rebooted (boot ${bootId}) — buffer reset`,
            data: null, ms: null };
        state.events.push(note);
    }
    state.boot = bootId || state.boot;
    const before = state.events.length;
    for (const ev of data.events) {
        state.events.push(ev);
        ingestCycle(ev);
    }
    if (state.events.length > MAX_CLIENT) {
        const cut = state.events.length - MAX_CLIENT;
        state.events.splice(0, cut);
        state.rendered = Math.max(0, state.rendered - cut);
        renderAll();
    }
    state.seq = data.last_seq;
    $('#fBuf').textContent = data.events.length >= 0
        ? `${Math.min(1000, before + data.events.length)}/1000` : '';
    $('#fSeq').textContent = state.seq;
    $('#fBoot').textContent = state.boot || '—';
    if (state.events.length !== before) {
        if (!state.paused) appendNew();
        renderCycles();
    }
}
setInterval(poll, 400);
setInterval(async () => renderStatus(await fetchStatus()), 2000);
(async () => renderStatus(await fetchStatus()))();

/* ── controls ────────────────────────────────────────────── */
$('#btnPause').addEventListener('click', e => {
    state.paused = !state.paused;
    e.currentTarget.classList.toggle('on', state.paused);
    e.currentTarget.textContent = state.paused ? '▶ RESUME' : '❚❚ PAUSE';
    if (!state.paused) appendNew();
});
$('#btnScroll').addEventListener('click', e => {
    state.autoscroll = !state.autoscroll;
    e.currentTarget.classList.toggle('on', state.autoscroll);
    if (state.autoscroll) stream.scrollTop = stream.scrollHeight;
});
$('#btnClear').addEventListener('click', () => {
    state.events = []; cycles.clear(); state.rendered = 0;
    renderAll(); renderCycles();
});
$('#btnExport').addEventListener('click', async () => {
    const status = await fetchStatus();
    const blob = new Blob([JSON.stringify({
        exported_at: new Date().toISOString(),
        boot_id: state.boot, omni_and_status: status,
        events: state.events,
    }, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `sentinel-traces-${state.boot || 'local'}-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
});
document.querySelectorAll('.fchip').forEach(ch => {
    ch.addEventListener('click', () => {
        document.querySelectorAll('.fchip').forEach(c => c.classList.remove('on'));
        ch.classList.add('on');
        state.src = ch.dataset.src;
        renderAll();
    });
});
$('#lvl').addEventListener('change', e => { state.level = e.target.value; renderAll(); });
$('#q').addEventListener('input', e => { state.q = e.target.value.trim().toLowerCase(); renderAll(); });

$('#fMode').textContent = state.direct
    ? 'direct mode (opened outside extension — polling backend directly)'
    : 'extension mode (routed via background.js)';

renderAll();

/* ─── Omni router detail panel (round eleven) ─────────────────── */
const omniPanel = $('#omniPanel');
let omniOpen = true;
const esc = s => String(s ?? '').replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function fetchOmni() {
    if (state.direct) {
        try {
            const r = await fetch(`${BACKEND_DIRECT}/omni`);
            return r.ok ? await r.json() : null;
        } catch (e) { return null; }
    }
    const resp = await new Promise(res =>
        chrome.runtime.sendMessage({ action: 'GET_OMNI_DETAIL' },
            r => { void chrome.runtime.lastError; res(r); }));
    return resp && resp.data ? resp.data : null;
}

function sparkBars(lat) {
    const max = Math.max(1, ...lat);
    return lat.map(ms => {
        const cls = latClass(ms);
        return `<i class="${cls}" style="height:${Math.max(8, ms / max * 100)}%"></i>`;
    }).join('');
}

function renderOmni(o) {
    if (!o) {
        $('#opState').textContent = 'OMNI ROUTER · no data';
        $('#opState').style.color = 'var(--err)';
        return;
    }
    omniPanel.style.display = omniOpen ? 'block' : 'none';
    $('#opState').textContent = o.connected ? 'OMNI ROUTER · LIVE' : 'OMNI ROUTER · DOWN';
    $('#opState').style.color = o.connected ? '#c792ea' : 'var(--err)';
    $('#opModel').textContent = o.model || '—';
    $('#opProv').textContent = o.provider || '—';
    $('#opUrl').textContent = o.base_url || '—';
    $('#opKey').textContent = o.key_hint || (o.api_key_set ? 'set' : 'MISSING');
    $('#opKey').style.color = o.api_key_set ? 'var(--ink)' : 'var(--err)';
    $('#opCallsN').textContent = o.calls || 0;
    $('#opErrs').textContent = `${o.errors || 0} (${o.error_rate || 0}%)`;
    $('#opErrs').style.color = (o.errors || 0) ? 'var(--err)' : 'var(--ink)';
    $('#opTok').textContent = (o.tokens_in || o.tokens_out)
        ? `${(o.tokens_in / 1000).toFixed(1)}k / ${(o.tokens_out / 1000).toFixed(1)}k` : '—';
    $('#opPct').textContent = o.p50_ms != null
        ? `${Math.round(o.p50_ms)} / ${o.p95_ms != null ? Math.round(o.p95_ms) : '—'} ms` : '—';
    $('#opChain').textContent = (o.fallback_chain && o.fallback_chain.length)
        ? o.fallback_chain.join(' → ') : '—';
    $('#opSpark').innerHTML = sparkBars(o.latency || []);
    $('#opLastMs').textContent = o.last_ms != null ? Math.round(o.last_ms) + ' ms last' : '';
    if (o.last_ping) {
        const p = o.last_ping;
        $('#opPing').textContent = p.ok
            ? `${Math.round(p.ms)} ms — "${esc(p.reply)}" (${ago(p.t)})`
            : `FAILED ${Math.round(p.ms)} ms — ${esc(p.reply)}`;
        $('#opPing').style.color = p.ok ? 'var(--ok)' : 'var(--err)';
    }
    // per-model bars
    const models = Object.entries(o.per_model || {});
    const maxCalls = Math.max(1, ...models.map(([, m]) => m.calls));
    $('#opModels').innerHTML = models.length ? models
        .sort((a, b) => b[1].calls - a[1].calls)
        .map(([name, m]) =>
            `<div class="mrow"><span class="nm" title="${esc(name)}">${esc(name)}</span>` +
            `<span class="bar"><i style="width:${m.calls / maxCalls * 100}%"></i></span>` +
            `<span class="st">${m.calls} calls · ${m.avg_ms} ms · ${m.errors} err</span></div>`)
        .join('') : '<div class="empty">no calls yet</div>';
    // recent calls (click to expand)
    $('#opCalls').innerHTML = (o.recent_calls || []).length ? o.recent_calls
        .map(c =>
            `<div class="crow"><span>${fmtT(c.t)}</span>` +
            `<span>cycle ${esc(c.cycle || '—')}</span>` +
            `<span>${esc(String(c.model || '').split('/').pop())}</span>` +
            `<span>${Math.round(c.ms)} ms</span>` +
            `<span class="${c.ok ? 'ok' : 'err'}">${c.ok ? 'ok' : 'ERR'}</span>` +
            `<span class="cdetail">${c.err ? esc(c.err) : ''}</span></div>`)
        .join('') : '<div class="empty">no calls yet — is the bot running?</div>';
    document.querySelectorAll('#opCalls .crow').forEach(row =>
        row.addEventListener('click', () => row.classList.toggle('open')));
}

setInterval(async () => renderOmni(await fetchOmni()), 2000);
(async () => renderOmni(await fetchOmni()))();

$('#btnPing').addEventListener('click', async (e) => {
    e.currentTarget.classList.add('busy');
    $('#opPing').textContent = 'pinging… (one real completion)';
    let resp = null;
    if (state.direct) {
        try {
            const r = await fetch(`${BACKEND_DIRECT}/omni/ping`);
            resp = r.ok ? await r.json() : { ok: false, reply: 'HTTP ' + r.status };
        } catch (err) { resp = { ok: false, reply: err.message }; }
    } else {
        resp = await new Promise(res =>
            chrome.runtime.sendMessage({ action: 'OMNI_PING' },
                r => { void chrome.runtime.lastError; res(r && r.data ? r.data : { ok: false, reply: (r && r.error) || 'no response' }); }));
    }
    e.currentTarget.classList.remove('busy');
    renderOmni(await fetchOmni());   // panel picks up last_ping + counters
});
$('#btnOmniClose').addEventListener('click', () => {
    omniOpen = !omniOpen;
    omniPanel.style.display = omniOpen ? 'block' : 'none';
});
