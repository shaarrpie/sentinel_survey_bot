// Shared panel-hub matcher fixture — run: node test_hub_matcher.node.js
// Round four B.1c / D.5. Asserts the SHIPPED extension matcher
// (extension/hub_match.js via require — not a copy) against the audit's
// canonical table. The Python twin (test_panel_hub_matcher.py) asserts the
// identical table against backend.is_panel_hub and core.is_survey_router_hub;
// all three must agree on every row or a login wall eventually gets filled.
const assert = require('assert');
const { hubHit } = require('./extension/hub_match.js');

const TABLE = [
    // [hub, url, expected]
    ['example.com', 'https://app.example.com/dashboard', true],
    ['example.com', 'http://www.example.com/login?next=/panel', true],
    ['example.com', 'https://evilexample.com/', false],
    ['example.com', 'https://example.com.evil.org/echo', false],
    ['app.example.com', 'https://example.com/', false],
    ['panel.io', 'https://panel.io/Router.aspx?SSID=abc123', true],
    // nested + hyphenated subdomains are genuine subdomains → HIT.
    // (my-www row also discriminates against the retired
    //  host.replace("www.", "") bug, which mangled it to my-example.com)
    ['example.com', 'https://deep.sub.example.com/x/y', true],
    ['example.com', 'https://my-www.example.com/a', true],
    ['example.com', '', false],                                       // empty URL never matches
    ['example.com', 'not a url at all', false],
    ['', 'https://example.com/', false],                              // no hubs configured → no hit
];

let failures = 0;
for (const [hub, url, expect] of TABLE) {
    let got = null, err = null;
    try { got = hubHit([hub].filter(Boolean), url); } catch (e) { err = e; }
    const ok = !err && ((got === null) === !expect);
    if (!ok) {
        failures++;
        console.log(`[FAIL] hub=${hub || '(none)'} url=${url || '(empty)'} ` +
            `expected ${expect ? 'HIT' : 'NO HIT'}, got ${err ? 'throw:' + err : JSON.stringify(got)}`);
    } else {
        console.log(`[ok]   hub=${(hub || '(none)').padEnd(16)} url=${(url || '(empty)').padEnd(44)} -> ${got ? 'HIT' : 'no hit'}`);
    }
}

if (failures) {
    console.log(`\nRESULT: ${failures} FAILURE(S)`);
    process.exit(1);
}
console.log('\nRESULT: ALL MATCHER ROWS PASS ✔');
