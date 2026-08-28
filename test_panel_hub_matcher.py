"""Shared panel-hub matcher fixture — run: python test_panel_hub_matcher.py

Round four B.1c / D.5. Asserts the IDENTICAL table as test_hub_matcher.node.js
against BOTH shipped Python matchers — backend.is_panel_hub and
core.is_survey_router_hub — driving their shared domain list through
panel_config.set_panel_hub_domains. All three matchers (these two plus the
Node one in extension/hub_match.js) must agree on every row.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import panel_config
import core          # noqa: E402  (defines is_survey_router_hub)
import backend       # noqa: E402  (defines is_panel_hub)

# [hub, url, expected] — keep byte-identical to test_hub_matcher.node.js
TABLE = [
    ('example.com', 'https://app.example.com/dashboard', True),
    ('example.com', 'http://www.example.com/login?next=/panel', True),
    ('example.com', 'https://evilexample.com/', False),
    ('example.com', 'https://example.com.evil.org/echo', False),
    ('app.example.com', 'https://example.com/', False),
    ('panel.io', 'https://panel.io/Router.aspx?SSID=abc123', True),
    # nested + hyphenated subdomains are genuine subdomains -> HIT.
    # (my-www row also discriminates against the retired
    #  host.replace("www.", "") bug, which mangled it to my-example.com)
    ('example.com', 'https://deep.sub.example.com/x/y', True),
    ('example.com', 'https://my-www.example.com/a', True),
    ('example.com', '', False),
    ('example.com', 'not a url at all', False),
    ('', 'https://example.com/', False),
]

_failures = 0


def check(matcher_name, matcher_fn):
    global _failures
    print(f"\n-- {matcher_name} --")
    for hub, url, expect in TABLE:
        # Drive the shared domain list per-row; set_panel_hub_domains
        # normalizes/dedupes exactly like runtime writes do.
        got = None
        err = None
        try:
            panel_config.set_panel_hub_domains([hub])
            got = matcher_fn(url)
        except Exception as e:                       # pragma: no cover
            err = e
        ok = err is None and got == expect
        mark = "[ok]  " if ok else "[FAIL]"
        if not ok:
            _failures += 1
            detail = f" threw {err}" if err else f", got {got}"
            print(f"{mark} hub={hub!r} url={url!r} expected "
                  f"{expect}{detail}")
        else:
            print(f"{mark} hub={hub or '(none)':16} url={url or '(empty)':44} "
                  f"-> {got}")
    panel_config.set_panel_hub_domains([])           # leave config clean


check("core.is_survey_router_hub", core.is_survey_router_hub)
check("backend.is_panel_hub", backend.is_panel_hub)

print()
print("RESULT:", "ALL MATCHER ROWS PASS [OK]"
      if not _failures else f"{_failures} FAILURE(S)")
raise SystemExit(1 if _failures else 0)
