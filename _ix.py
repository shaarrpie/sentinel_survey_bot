import io, re, html
s = io.open('_insights_raw.html', encoding='utf-8', errors='replace').read()
print('LEN', len(s))
for pat in [r'<title[^>]*>(.*?)</title>', r'<meta[^>]+description[^>]*>', r'<meta[^>]+og:[^>]*>', r'<meta[^>]+name="generator"[^>]*>']:
    for m in re.findall(pat, s, re.I)[:5]:
        print('META:', re.sub(r'<[^>]+>', '', m if isinstance(m, str) else m).strip()[:200])
# script and css srcs
for m in re.findall(r'(?:src|href)="([^"]+)"', s)[:30]:
    print('ASSET:', m[:160])
# words that look like branding/app names
for kw in ['insights', 'login', 'sign', 'survey', 'panel', 'api', 'token', 'app.mount', 'createRoot', '_app', 'favicon']:
    print(kw, '::', s.lower().count(kw))