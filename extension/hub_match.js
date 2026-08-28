// Shared panel-hub matching — ONE implementation, used everywhere.
// Loaded by the MV3 service worker via importScripts(), and require()-able
// from Node so the test fixture asserts the EXACT shipped logic, never a
// copy (round four, B.1c — parallel implementations drift silently, and
// the day one drifts is the day a login wall gets filled).

function hubNormalizeHost(url) {
    try {
        return String(url || '').trim()
            ? new URL(url).hostname.toLowerCase().replace(/^www\./, '')
            : '';
    } catch (e) { return ''; }
}

// DOT-GUARDED suffix match (B.1b): endsWith('.' + hub), never
// endsWith(hub) — the unguarded form would match evilexample.com against
// hub example.com. Exact apex equal, subdomains covered, sibling
// lookalikes and trailing-host-spoof (example.com.evil.org) rejected.
function hubHit(hubs, url) {
    const host = hubNormalizeHost(url);
    if (!host) return null;
    return hubs.find(d => host === d || host.endsWith('.' + d)) || null;
}

if (typeof module !== 'undefined' && module.exports)
    module.exports = { hubNormalizeHost, hubHit };
