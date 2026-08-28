import io, re
s = io.open('_insights_app.js', encoding='utf-8', errors='replace').read()
for kw in ['insightsToday', 'Insights', 'TODAY', 'appName', 'appTitle', 'brand', 'logotype', 'logo', 'Powered by', 'Sample', 'Router', 'IQ', 'selfEnrollment', 'isLdapEnabled', '/api/', 'config', 'auth', 'signIn', 'signin', 'clientId', 'SIGN']:
    n = s.count(kw)
    if n:
        idxs = [m.start() for m in re.finditer(re.escape(kw), s)][:4]
        for i in idxs:
            ctx = s[max(0,i-50):i+90].replace('\n',' ')
            print(f'[{kw}] {n}x :: ...{ctx}...')
        print()